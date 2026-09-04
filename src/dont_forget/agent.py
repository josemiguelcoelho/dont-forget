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
        source_text = (
            self.actions.read_source(context.source_url) if context.source_url else ""
        )
        intention = self.interpreter.remember(message, context, source_text, now)
        event_payload = {"objective": intention.objective}
        if context.source_url:
            event_payload["source"] = context.source_url
        self.store.save_intention_with_event(
            intention,
            event_type="created",
            event_payload=event_payload,
            event_created_at=now,
        )
        return "got you." if context.repository is None else "got it"

    def _act(self, now: datetime) -> str:
        awaiting_check = next(
            (
                intention
                for intention in self.store.list_intentions()
                if intention.next_action is not None
                and intention.next_action.status == "completed"
                and intention.next_action.post_check_pending
            ),
            None,
        )
        if awaiting_check is not None:
            checked, _ = self.checker.check_now_safely(awaiting_check.id)
            if not checked:
                return "follow-up CHECK failed and will be retried."
            updated = self.store.get_intention(awaiting_check.id)
            if updated is None:
                raise RuntimeError(
                    f"Intention disappeared after CHECK: {awaiting_check.id}"
                )
            return self._remaining_user_work(
                updated.most_important_unresolved_requirement
            ).strip()

        intention = self.store.claim_next_agent_action(
            "repair_readme_setup", now
        )
        if intention is not None:
            action = intention.next_action
            if action is None or action.execution_id is None:
                raise RuntimeError("Claimed action is missing execution state.")
            try:
                readme, repaired = self.actions.repair_readme_setup(
                    action.parameters["repository"]
                )
            except Exception as error:
                self.store.release_claimed_action(
                    intention.id,
                    action.execution_id,
                    now,
                    type(error).__name__,
                )
                raise
            recovered = action.execution_attempts > 1
            event_payload = {
                "action": "repair_readme_setup",
                "path": str(readme),
            }
            if not repaired:
                event_payload["changed"] = False
            if recovered:
                event_payload["recovered"] = True
            completed = self.store.complete_claimed_action(
                intention.id,
                action.execution_id,
                now,
                event_payload,
            )
            if completed is None:
                raise RuntimeError("Action claim was lost before completion.")
            checked, _ = self.checker.check_now_safely(intention.id)
            if not checked:
                prefix = "done. README setup is fixed. " if repaired else ""
                return f"{prefix}follow-up CHECK failed and will be retried."
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
        user_action = next(
            (
                intention.next_action
                for intention in self.store.list_intentions()
                if intention.next_action is not None
                and intention.next_action.mode == "user"
                and intention.next_action.status in {"pending", "proposed"}
            ),
            None,
        )
        if user_action:
            return f"you still need to {user_action.description.casefold()}."
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
