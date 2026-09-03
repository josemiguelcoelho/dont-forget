from __future__ import annotations

import re
from html import unescape
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit
from uuid import uuid4

from .models import (
    CheckAssessment,
    Evidence,
    Intention,
    MessageContext,
    NextAction,
    RepositoryEvidence,
    Requirement,
    Source,
)


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

    def check(
        self,
        intention: Intention,
        source_text: str,
        repository: RepositoryEvidence,
        now: datetime,
    ) -> CheckAssessment: ...


class DeterministicInterpreter:
    """Fixture-friendly stand-in for a future structured-output LLM."""

    def is_action_approval(self, message: str) -> bool:
        return bool(re.search(r"\bhandle what you can\b", message, flags=re.IGNORECASE))

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

        match = re.search(r"(?:https?://|file://)\S+", message, flags=re.IGNORECASE)
        if not match:
            raise ValueError("I need a message containing a URL.")
        source_url = match.group(0).rstrip(".,;:!?)]}")
        parsed = urlsplit(source_url)
        if parsed.scheme in {"http", "https"} and not parsed.netloc:
            raise ValueError("I need a valid source URL.")
        return MessageContext(
            source_url=source_url,
        )

    def remember(
        self,
        message: str,
        context: MessageContext,
        source_text: str,
        now: datetime,
    ) -> Intention:
        if context.repository is None:
            return self._remember_source(message, context, source_text, now)

        name, deadline, requirements = self._parse_source(source_text)
        requirement_models = [
            Requirement(
                description=requirement,
                evidence=[
                    Evidence(
                        claim=f"The hackathon requires: {requirement}",
                        source=context.source_url,
                        observed_at=now,
                        confidence=1.0,
                    )
                ],
            )
            for requirement in requirements
        ]
        return Intention(
            id=str(uuid4()),
            objective=f"Submit a valid project to {name}",
            original_message=message,
            sources=[
                Source(kind="url", value=context.source_url, observed_at=now),
                Source(kind="repository", value=context.repository, observed_at=now),
            ],
            deadline_at=deadline,
            requirements=requirement_models,
            current_state="Remembered; project has not been checked yet.",
            next_action=NextAction(
                description="Inspect the project before the deadline",
                mode="agent",
                action_type="inspect_repository",
                parameters={"repository": context.repository},
            ),
            next_check_at=min(now + timedelta(hours=1), deadline),
            confidence=0.95,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _remember_source(
        message: str,
        context: MessageContext,
        source_text: str,
        now: datetime,
    ) -> Intention:
        title_match = re.search(
            r"<title[^>]*>(?P<title>.*?)</title>",
            source_text,
            flags=re.IGNORECASE | re.DOTALL,
        )
        title = None
        if title_match:
            title = re.sub(r"\s+", " ", unescape(title_match.group("title"))).strip()
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
        return Intention(
            id=str(uuid4()),
            objective=objective,
            original_message=message,
            sources=[Source(kind="url", value=context.source_url, observed_at=now)],
            deadline_at=None,
            requirements=[],
            current_state=current_state,
            next_action=None,
            next_check_at=None,
            confidence=confidence,
            created_at=now,
            updated_at=now,
        )

    def check(
        self,
        intention: Intention,
        source_text: str,
        repository: RepositoryEvidence,
        now: datetime,
    ) -> CheckAssessment:
        _, deadline, _ = self._parse_source(source_text)
        return CheckAssessment(
            deadline_at=deadline,
            deadline_near=timedelta(0) <= deadline - now <= timedelta(days=1),
            repository=repository,
        )

    @staticmethod
    def _parse_source(source_text: str) -> tuple[str, datetime, list[str]]:
        name_match = re.search(r"^Hackathon:\s*(.+)$", source_text, re.MULTILINE)
        deadline_match = re.search(r"^Deadline:\s*(.+)$", source_text, re.MULTILINE)
        requirements = re.findall(r"^-\s+(.+)$", source_text, re.MULTILINE)
        if not name_match or not deadline_match or not requirements:
            raise ValueError("The hackathon source is missing a name, deadline, or requirements.")
        return (
            name_match.group(1).strip(),
            datetime.fromisoformat(deadline_match.group(1).strip()),
            [item.strip() for item in requirements],
        )
