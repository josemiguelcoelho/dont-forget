from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .actions import LocalActions
from .llm import Interpreter
from .models import Evidence, Intention, NextAction
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

    def _check(self, intention: Intention, now: datetime) -> str | None:
        source_url = next(source.value for source in intention.sources if source.kind == "url")
        repository_path = next(
            source.value for source in intention.sources if source.kind == "repository"
        )
        source_text = self.actions.read_source(source_url)
        repository = self.actions.inspect_repository(repository_path)
        assessment = self.interpreter.check(intention, source_text, repository, now)

        self.store.append_event(
            intention.id,
            "checked",
            {
                "deadline_near": assessment.deadline_near,
                "repository_public": repository.is_public,
                "demo_present": repository.has_demo,
            },
            now,
        )

        intention.deadline_at = assessment.deadline_at
        self._update_requirement(
            intention,
            "Public repository",
            "satisfied" if repository.is_public else "missing",
            f"Repository public marker is {'present' if repository.is_public else 'missing'}",
            repository.repository,
            now,
        )
        self._update_requirement(
            intention,
            "Demo video",
            "satisfied" if repository.has_demo else "missing",
            f"Demo file is {'present' if repository.has_demo else 'missing'}",
            repository.repository,
            now,
        )

        notice: str | None = None
        if assessment.deadline_near and not repository.has_demo:
            checklist, created = self.actions.create_demo_checklist(repository.repository)
            if created:
                self.store.append_event(
                    intention.id,
                    "action_completed",
                    {"action": "create_demo_checklist", "path": str(checklist)},
                    now,
                )
                notice = (
                    "one thing. the deadline is tomorrow and the demo is still missing. "
                    "i made you a checklist."
                )
            intention.current_state = (
                "Repository is public; demo video is missing; checklist created."
                if repository.is_public
                else "Repository and demo video are missing; checklist created."
            )
            if intention.next_action:
                intention.next_action.status = "completed"
        else:
            intention.current_state = "Project checked; no urgent action was needed."

        if not repository.has_useful_setup and repository.setup_commands:
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

        intention.next_check_at = now + timedelta(hours=6)
        intention.updated_at = now
        intention.version += 1
        self.store.save_intention(intention)
        return notice

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
