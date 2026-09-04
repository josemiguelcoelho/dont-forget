from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from .models import (
    CheckAssessment,
    Intention,
    MessageContext,
    NextAction,
    RepositoryEvidence,
    Requirement,
    Source,
)
from .sources import DeterministicSourceExtractor, SourceEnrichment, SourceExtractor


class Interpreter(Protocol):
    def is_action_approval(self, message: str) -> bool: ...

    def parse_message(self, message: str) -> MessageContext: ...

    def remember(
        self,
        message: str,
        context: MessageContext,
        source_text: str,
        now: datetime,
    ) -> Intention: ...

    def enrich_source(
        self, source_url: str, source_text: str, now: datetime
    ) -> SourceEnrichment: ...

    def check(
        self,
        intention: Intention,
        enrichment: SourceEnrichment,
        repository: RepositoryEvidence,
        now: datetime,
    ) -> CheckAssessment: ...


class DeterministicInterpreter:
    """Fixture-friendly stand-in for a future structured-output LLM."""

    def __init__(self, source_extractor: SourceExtractor | None = None) -> None:
        self.source_extractor = source_extractor or DeterministicSourceExtractor()

    def is_action_approval(self, message: str) -> bool:
        return bool(
            re.fullmatch(
                r"\s*(?:please\s+)?handle what you can(?:\s+safely)?[.!]?\s*",
                message,
                flags=re.IGNORECASE,
            )
        )

    def enrich_source(
        self, source_url: str, source_text: str, now: datetime
    ) -> SourceEnrichment:
        return self.source_extractor.extract(source_url, source_text, now)

    def parse_message(self, message: str) -> MessageContext:
        legacy_match = re.search(
            r"hackathon:\s*(?P<url>\S+?)\.\s*my project is in\s+(?P<repository>.+)$",
            message,
            flags=re.IGNORECASE,
        )
        if legacy_match:
            return MessageContext(
                source_url=legacy_match.group("url"),
                repository=str(Path(legacy_match.group("repository").strip()).resolve()),
            )

        repository_match = re.search(
            r"\b(?:my\s+)?(?:project|repository)\s+is\s+(?:in|at)\s+"
            r"(?P<repository>.+?)\s*$",
            message,
            flags=re.IGNORECASE,
        )
        match = re.search(r"(?:https?://|file://)\S+", message, flags=re.IGNORECASE)
        if not match:
            if self._explicit_objective(message) is None:
                raise ValueError("I need a clear intention, such as 'remember to ...'.")
            return MessageContext(
                repository=(
                    str(Path(repository_match.group("repository").strip()).resolve())
                    if repository_match
                    else None
                )
            )
        source_url = match.group(0).rstrip(".,;:!?)]}")
        parsed = urlsplit(source_url)
        if parsed.scheme in {"http", "https"} and not parsed.netloc:
            raise ValueError("I need a valid source URL.")
        return MessageContext(
            source_url=source_url,
            repository=(
                str(Path(repository_match.group("repository").strip()).resolve())
                if repository_match
                else None
            ),
        )

    def remember(
        self,
        message: str,
        context: MessageContext,
        source_text: str,
        now: datetime,
    ) -> Intention:
        if context.source_url is None:
            return self._remember_without_source(message, context, now)
        if context.repository is None:
            return self._remember_source(message, context, source_text, now)

        enrichment = self.enrich_source(context.source_url, source_text, now)
        if (
            enrichment.title is None
            or enrichment.deadline_at is None
            or not enrichment.deadline_evidence
            or not enrichment.requirements
        ):
            raise ValueError(
                "The hackathon source needs a name, a verified timezone-aware deadline, "
                "and explicit requirements."
            )
        requirement_models = [
            Requirement(description=item.description, evidence=[item.evidence])
            for item in enrichment.requirements
        ]
        deadline_passed = enrichment.deadline_at <= now
        return Intention(
            id=str(uuid4()),
            objective=(
                self._explicit_objective(message)
                or f"Submit a valid project to {enrichment.title}"
            ),
            original_message=message,
            status="blocked" if deadline_passed else "active",
            sources=[
                Source(kind="url", value=context.source_url, observed_at=now),
                Source(kind="repository", value=context.repository, observed_at=now),
            ],
            deadline_at=enrichment.deadline_at,
            deadline_evidence=enrichment.deadline_evidence,
            requirements=requirement_models,
            context_evidence=enrichment.context_evidence,
            current_state=(
                "Remembered; the verified deadline has passed."
                if deadline_passed
                else "Remembered; project has not been checked yet."
            ),
            next_action=(
                None
                if deadline_passed
                else NextAction(
                    description="Inspect the project before the deadline",
                    mode="agent",
                    action_type="inspect_repository",
                    parameters={"repository": context.repository},
                )
            ),
            next_check_at=(
                None
                if deadline_passed
                else min(now + timedelta(hours=1), enrichment.deadline_at)
            ),
            confidence=0.95,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _explicit_objective(message: str) -> str | None:
        match = re.search(
            r"\b(?:don't let me forget to|remember to)\s+"
            r"(?P<objective>[^.!?\r\n]+)",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            return None
        objective = match.group("objective").strip().rstrip(":;, ")
        return objective[:1].upper() + objective[1:] if objective else None

    def _remember_without_source(
        self,
        message: str,
        context: MessageContext,
        now: datetime,
    ) -> Intention:
        objective = self._explicit_objective(message)
        if objective is None:
            raise ValueError("I need a clear intention, such as 'remember to ...'.")
        sources = (
            [Source(kind="repository", value=context.repository, observed_at=now)]
            if context.repository
            else []
        )
        return Intention(
            id=str(uuid4()),
            objective=objective,
            original_message=message,
            status="active",
            sources=sources,
            deadline_at=None,
            requirements=[],
            current_state=(
                "Remembered from the user's stated intention; no verified source facts "
                "or supported agent action are available."
            ),
            next_action=NextAction(
                description=objective,
                mode="user",
                action_type="user_follow_up",
                status="pending",
            ),
            next_check_at=None,
            confidence=0.9,
            created_at=now,
            updated_at=now,
        )

    def _remember_source(
        self,
        message: str,
        context: MessageContext,
        source_text: str,
        now: datetime,
    ) -> Intention:
        enrichment = self.enrich_source(context.source_url, source_text, now)
        title = enrichment.title
        explicit_match = re.search(
            r"\b(?:don't let me forget to|remember to)\s+"
            r"(?P<action>apply|register|attend|participate|submit|read|buy)\b",
            message,
            re.IGNORECASE,
        )
        event_context = " ".join(
            part for part in (context.source_url, title, source_text[:1000]) if part
        )
        if explicit_match:
            action = explicit_match.group("action").casefold()
            prefixes = {
                "apply": "Apply to",
                "register": "Register for",
                "attend": "Attend",
                "participate": "Participate in",
                "submit": "Submit to",
                "read": "Read",
                "buy": "Buy",
            }
            objective = f"{prefixes[action]} {title or 'this source'}"
            confidence = 0.9
            current_state = "Remembered from the user's stated intention; details are unconfirmed."
        elif re.search(
            r"\b(hackathon|conference|meetup|workshop)\b",
            event_context,
            re.IGNORECASE,
        ):
            objective = f"Participate in {title}" if title else "Participate in the event"
            confidence = 0.75
            current_state = (
                "Inferred event participation from source context; details are unconfirmed."
            )
        else:
            objective = f"Follow up on {title}" if title else "Follow up on this source"
            confidence = 0.35
            current_state = (
                "Remembered with an uncertain intention; source details are unconfirmed."
            )
        deadline_passed = bool(
            enrichment.deadline_at
            and enrichment.deadline_evidence
            and enrichment.deadline_at <= now
        )
        if deadline_passed:
            current_state += " The verified deadline has passed."
        return Intention(
            id=str(uuid4()),
            objective=objective,
            original_message=message,
            status="blocked" if deadline_passed else "active",
            sources=[Source(kind="url", value=context.source_url, observed_at=now)],
            deadline_at=enrichment.deadline_at,
            deadline_evidence=enrichment.deadline_evidence,
            requirements=[
                Requirement(
                    description=item.description,
                    evidence=[item.evidence],
                )
                for item in enrichment.requirements
            ],
            context_evidence=enrichment.context_evidence,
            current_state=current_state,
            next_action=None,
            next_check_at=(
                min(now + timedelta(hours=1), enrichment.deadline_at)
                if enrichment.deadline_at and enrichment.deadline_at > now
                else None
            ),
            confidence=confidence,
            created_at=now,
            updated_at=now,
        )

    def check(
        self,
        intention: Intention,
        enrichment: SourceEnrichment,
        repository: RepositoryEvidence,
        now: datetime,
    ) -> CheckAssessment:
        if enrichment.deadline_at is None or not enrichment.deadline_evidence:
            raise ValueError("The source no longer provides a verified timezone-aware deadline.")
        return CheckAssessment(
            deadline_at=enrichment.deadline_at,
            deadline_near=(
                timedelta(0)
                <= enrichment.deadline_at - now
                <= timedelta(days=1)
            ),
            repository=repository,
        )
