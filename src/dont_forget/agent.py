from __future__ import annotations

from datetime import datetime
from typing import Callable

from .actions import LocalActions
from .llm import Interpreter
from .store import SQLiteStore


class DontForgetAgent:
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
        changed = False
        for intention in self.store.list_intentions():
            action = intention.next_action
            if not action or action.status != "proposed" or action.action_type != "repair_readme_setup":
                continue
            readme, repaired = self.actions.repair_readme_setup(action.parameters["repository"])
            action.status = "completed"
            intention.updated_at = now
            intention.version += 1
            if repaired:
                changed = True
                intention.current_state = "README setup instructions added; demo video still requires the user."
                self.store.append_event(
                    intention.id,
                    "action_completed",
                    {"action": "repair_readme_setup", "path": str(readme)},
                    now,
                )
            self.store.save_intention(intention)
        if changed:
            return "Added README setup instructions. You still need to record the demo."
        return "Nothing else I can handle."
