from __future__ import annotations

from datetime import datetime
from typing import Callable

from .actions import LocalActions
from .checker import IntentionChecker
from .llm import Interpreter
from .store import SQLiteStore


class DontForgetAgent:
    def __init__(
        self,
        store: SQLiteStore,
        interpreter: Interpreter,
        actions: LocalActions,
        clock: Callable[[], datetime],
        checker: IntentionChecker,
    ) -> None:
        self.store = store
        self.interpreter = interpreter
        self.actions = actions
        self.clock = clock
        self.checker = checker

    def receive(self, message: str) -> str:
        now = self.clock()
        if self.interpreter.is_action_approval(message):
            return self._act(now)
        context = self.interpreter.parse_message(message)
        source_text = self.actions.read_source(context.source_url)
        intention = self.interpreter.remember(message, context, source_text, now)
        self.store.save_intention(intention)
        self.store.append_event(
            intention.id,
            "created",
            {"objective": intention.objective, "source": context.source_url},
            now,
        )
        return "got it"

    def _act(self, now: datetime) -> str:
        for intention in self.store.list_intentions():
            action = intention.next_action
            if (
                not action
                or action.status != "proposed"
                or action.mode != "agent"
                or action.action_type != "repair_readme_setup"
            ):
                continue
            readme, repaired = self.actions.repair_readme_setup(action.parameters["repository"])
            action.status = "completed"
            intention.updated_at = now
            intention.version += 1
            if repaired:
                self.store.append_event(
                    intention.id,
                    "action_completed",
                    {"action": "repair_readme_setup", "path": str(readme)},
                    now,
                )
            self.store.save_intention(intention)
            self.checker.check_now(intention.id)
            updated = self.store.get_intention(intention.id)
            if updated is None:
                raise RuntimeError(f"Intention disappeared after ACT: {intention.id}")
            if repaired:
                remaining = self._remaining_user_work(
                    updated.most_important_unresolved_requirement
                )
                return f"done. README setup is fixed.{remaining}"
            return self._remaining_user_work(
                updated.most_important_unresolved_requirement
            ).strip()
        user_requirement = next(
            (
                intention.most_important_unresolved_requirement
                for intention in self.store.list_intentions()
                if intention.requirement_capability == "user_must_handle"
            ),
            None,
        )
        if user_requirement:
            return self._remaining_user_work(user_requirement).strip()
        return "nothing else I can handle."

    @staticmethod
    def _remaining_user_work(requirement: str | None) -> str:
        if requirement is None:
            return " all requirements are covered."
        labels = {
            "Demo video": "record the demo",
            "Public repository": "make the repository public",
        }
        work = labels.get(requirement, f"handle {requirement.casefold()}")
        return f" you still need to {work}."
