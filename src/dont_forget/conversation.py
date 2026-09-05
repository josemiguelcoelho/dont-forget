from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import ClassVar

from .models import Intention


class ConversationKind(str, Enum):
    REMEMBER = "remember"
    CHECK = "check"
    ACT = "act"
    ADD_CONTEXT = "add_context"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RoutedMessage:
    kind: ConversationKind
    reference: str | None = None
    repository: str | None = None


class NaturalLanguageRouter:
    """Conservatively maps natural messages onto deterministic lifecycle operations."""

    _CHECK_PATTERNS = (
        r"am i forgetting anything",
        r"(?:is there )?anything (?:else )?i(?:'m| am) forgetting",
        r"anything (?:else )?i need to remember",
        r"what (?:am i forgetting|did i forget)",
        r"do i have anything (?:left|coming up|due)",
        r"what(?:'s| is) (?:still )?(?:left|pending|due)",
    )

    def route(self, message: str) -> RoutedMessage:
        normalized = self._normalize(message)
        action_match = re.fullmatch(
            r"(?:please )?handle what you can(?: safely)? for (?P<reference>.+)",
            normalized,
        )
        if action_match:
            return RoutedMessage(
                ConversationKind.ACT,
                reference=action_match.group("reference"),
            )
        if any(re.fullmatch(pattern, normalized) for pattern in self._CHECK_PATTERNS):
            return RoutedMessage(ConversationKind.CHECK)
        reference_match = re.fullmatch(
            r"(?:what about|how(?:'s| is)|anything (?:new )?(?:on|with)) (?P<reference>.+)",
            normalized,
        )
        if reference_match:
            return RoutedMessage(
                ConversationKind.CHECK,
                reference=reference_match.group("reference"),
            )
        repository_match = re.fullmatch(
            r"(?:my )?(?:project|repository) is (?:in|at) (?P<repository>.+)",
            message.strip(),
            flags=re.IGNORECASE,
        )
        if repository_match:
            return RoutedMessage(
                ConversationKind.ADD_CONTEXT,
                repository=repository_match.group("repository").strip(),
            )
        return RoutedMessage(ConversationKind.REMEMBER)

    @staticmethod
    def _normalize(message: str) -> str:
        return re.sub(r"\s+", " ", message.strip().casefold()).rstrip(".!?")


class ConversationPresenter:
    _REQUIREMENT_LABELS: ClassVar[dict[str, str]] = {
        "Demo video": "the demo",
        "Public repository": "the public repository",
    }

    def check_summary(self, intentions: list[Intention], now: datetime) -> str:
        unresolved = [item for item in intentions if item.status in {"active", "blocked"}]
        if not unresolved:
            return "nothing's slipping through right now."
        if len(unresolved) == 1:
            return self._single_summary(unresolved[0], now)
        visible = [self._short_item(item) for item in unresolved[:3]]
        detail = "; ".join(visible)
        remaining = len(unresolved) - len(visible)
        if remaining:
            detail += f"; plus {remaining} more"
        return f"{len(unresolved)} things. {detail}."

    def _short_item(self, intention: Intention) -> str:
        requirement = intention.most_important_unresolved_requirement
        if requirement:
            label = self._REQUIREMENT_LABELS.get(requirement, requirement.casefold())
            return f"finish {label}"
        if intention.next_action is not None and intention.next_action.mode == "user":
            return intention.next_action.description.casefold()
        return intention.objective.casefold()

    def _single_summary(self, intention: Intention, now: datetime) -> str:
        parts = ["one thing."]
        deadline = self._deadline_phrase(intention, now)
        if deadline:
            parts.append(deadline)
        requirement = intention.most_important_unresolved_requirement
        if requirement:
            label = self._REQUIREMENT_LABELS.get(requirement, requirement.casefold())
            parts.append(f"you're still missing {label}.")
        elif intention.next_action is not None and intention.next_action.mode == "user":
            parts.append(f"you still need to {intention.next_action.description.casefold()}.")
        elif not deadline:
            parts.append(f"{intention.objective.casefold()} is still pending.")
        return " ".join(parts)

    @staticmethod
    def _deadline_phrase(intention: Intention, now: datetime) -> str | None:
        deadline = intention.deadline_at
        if deadline is None or not intention.deadline_evidence:
            return None
        remaining = deadline - now
        subject = "the hackathon" if "hackathon" in intention.objective.casefold() else "it"
        if timedelta(0) < remaining <= timedelta(days=1):
            return f"{subject} closes tomorrow."
        if remaining <= timedelta(0):
            return f"{subject} has closed."
        return None
