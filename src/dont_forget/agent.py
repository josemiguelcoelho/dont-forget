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
