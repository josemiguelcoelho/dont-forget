from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Protocol
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
        match = re.search(
            r"hackathon:\s*(?P<url>\S+?)\.\s*my project is in\s+(?P<repository>.+)$",
            message,
            flags=re.IGNORECASE,
        )
        if not match:
            raise ValueError("I need a hackathon source and project folder.")
        return MessageContext(
            source_url=match.group("url"),
            repository=str(Path(match.group("repository").strip()).resolve()),
        )

    def remember(
        self,
        message: str,
        context: MessageContext,
        source_text: str,
        now: datetime,
    ) -> Intention:
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
