from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .actions import LocalActions
from .llm import Interpreter
from .models import Evidence, Intention, NextAction, RepositoryEvidence
from .store import SQLiteStore


class IntentionChecker:
    def __init__(
        self,
        store: SQLiteStore,
        interpreter: Interpreter,
        actions: LocalActions,
        clock: Callable[[], datetime],
    ) -> None:
        self.store = store
        self.interpreter = interpreter
        self.actions = actions
        self.clock = clock

    def run_due(self) -> list[str]:
        now = self.clock()
        notices: list[str] = []
        for intention in self.store.list_due(now):
            notice = self._check(intention, now)
            if notice:
                notices.append(notice)
        return notices

    def check_now(self, intention_id: str) -> str | None:
        intention = self.store.get_intention(intention_id)
        if intention is None:
            raise ValueError(f"Unknown intention: {intention_id}")
        return self._check(intention, self.clock())

    def _check(self, intention: Intention, now: datetime) -> str | None:
        source_url = next(source.value for source in intention.sources if source.kind == "url")
        repository_path = next(
            source.value for source in intention.sources if source.kind == "repository"
        )
        source_text = self.actions.read_source(source_url)
        repository = self.actions.inspect_repository(repository_path)
        assessment = self.interpreter.check(intention, source_text, repository, now)
        intention.deadline_at = assessment.deadline_at
        missing_requirements = self._assess_requirements(
            intention, repository, assessment.deadline_near, now
        )
        most_important = max(missing_requirements, key=lambda item: item[1], default=None)
        intention.most_important_unresolved_requirement = (
            most_important[0] if most_important else None
        )
        intention.requirement_capability = self._classify_requirement(
            intention.most_important_unresolved_requirement
        )

        self.store.append_event(
            intention.id,
            "checked",
            {
                "deadline_near": assessment.deadline_near,
                "repository_public": repository.is_public,
                "demo_present": repository.has_demo,
                "most_important_unresolved_requirement": (
                    intention.most_important_unresolved_requirement
                ),
            },
            now,
        )

        notice: str | None = None
        if missing_requirements:
            descriptions = ", ".join(item[0] for item in missing_requirements)
            intention.current_state = (
                f"Unresolved requirements: {descriptions}. Most important: "
                f"{intention.most_important_unresolved_requirement}."
            )
        else:
            intention.current_state = "All known requirements are satisfied."

        if (
            intention.requirement_capability == "agent_can_handle"
            and not repository.has_useful_setup
            and repository.setup_commands
        ):
            intention.next_action = NextAction(
                description="Add setup instructions to README.md",
                mode="agent",
                action_type="repair_readme_setup",
                parameters={
                    "repository": repository.repository,
                    "commands": repository.setup_commands,
                },
            )
            intention.current_state += " README setup instructions are missing; repair awaits approval."
        else:
            intention.next_action = None

        if missing_requirements:
            intention.status = "active"
            intention.resolved_at = None
            intention.next_check_at = now + timedelta(hours=6)
            if notice is None and intention.most_important_unresolved_requirement:
                notice = self._missing_notice(intention.most_important_unresolved_requirement)
        else:
            intention.status = "completed"
            intention.resolved_at = now
            intention.next_check_at = None
            notice = "all set. your requirements are covered."
        intention.updated_at = now
        intention.version += 1
        self.store.save_intention(intention)
        return notice

    def _assess_requirements(
        self,
        intention: Intention,
        repository: RepositoryEvidence,
        deadline_near: bool,
        now: datetime,
    ) -> list[tuple[str, int]]:
        missing: list[tuple[str, int]] = []
        for requirement in intention.requirements:
            normalized = requirement.description.casefold()
            if "demo" in normalized:
                satisfied = repository.has_demo
                claim = f"Demo file is {'present' if satisfied else 'missing'}"
                importance = 100 if deadline_near else 80
            elif "public" in normalized and "repositor" in normalized:
                satisfied = repository.is_public
                claim = f"Repository public marker is {'present' if satisfied else 'missing'}"
                importance = 90
            elif "readme" in normalized or "setup instruction" in normalized:
                satisfied = repository.has_useful_setup
                claim = f"README setup instructions are {'present' if satisfied else 'missing'}"
                importance = 85
            else:
                satisfied = False
                claim = "No local evidence satisfies this requirement"
                importance = 10
            self._update_requirement(
                intention,
                requirement.description,
                "satisfied" if satisfied else "missing",
                claim,
                repository.repository,
                now,
            )
            if not satisfied:
                missing.append((requirement.description, importance))
        return missing

    @staticmethod
    def _classify_requirement(description: str | None) -> str | None:
        if description is None:
            return None
        normalized = description.casefold()
        if "readme" in normalized or "setup instruction" in normalized:
            return "agent_can_handle"
        return "user_must_handle"

    @staticmethod
    def _missing_notice(description: str) -> str:
        labels = {
            "Demo video": "your demo",
            "Public repository": "your public repository",
        }
        subject = labels.get(description, description.casefold())
        return f"one thing. {subject} is still missing."

    @staticmethod
    def _update_requirement(
        intention: Intention,
        description: str,
        status: str,
        claim: str,
        source: str,
        observed_at: datetime,
    ) -> None:
        requirement = next(
            (item for item in intention.requirements if item.description == description), None
        )
        if requirement is None:
            return
        requirement.status = status  # type: ignore[assignment]
        requirement.evidence.append(
            Evidence(
                claim=claim,
                source=source,
                observed_at=observed_at,
                confidence=1.0,
            )
        )
