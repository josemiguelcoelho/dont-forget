from datetime import datetime, timezone
from pathlib import Path

from dont_forget.actions import LocalActions
from dont_forget.agent import DontForgetAgent
from dont_forget.checker import IntentionChecker
from dont_forget.llm import DeterministicInterpreter
from dont_forget.store import SQLiteStore


class SourceActions(LocalActions):
    def __init__(self, pages: dict[str, str]) -> None:
        super().__init__(allowed_workspaces=[])
        self.pages = pages

    def read_source(self, source_url: str) -> str:
        return self.pages[source_url]


def test_remember_infers_event_participation_from_natural_url_message(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    source_url = "https://example.com/hackathon"
    actions = SourceActions(
        {
            source_url: (
                "<html><head><title>Tiny Agents Hackathon</title></head>"
                "<body><h1>Tiny Agents Hackathon</h1>"
                "<p>Join builders for a weekend hackathon.</p></body></html>"
            )
        }
    )
    store = SQLiteStore(tmp_path / "dont-forget.db")
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)

    reply = agent.receive(f"{source_url} — don't let me forget this")

    assert reply == "got you."
    intentions = store.list_intentions()
    assert len(intentions) == 1
    intention = intentions[0]
    assert intention.objective == "Participate in Tiny Agents Hackathon"
    assert [(source.kind, source.value) for source in intention.sources] == [
        ("url", source_url)
    ]
    assert intention.original_message == f"{source_url} — don't let me forget this"
    assert intention.deadline_at is None
    assert intention.requirements == []
    assert intention.next_action is None
    assert intention.next_check_at is None
    assert 0 < intention.confidence < 1
    assert store.count_events(intention.id, "created") == 1

    store.close()
    reopened = SQLiteStore(tmp_path / "dont-forget.db")
    assert reopened.get_intention(intention.id) == intention


def test_remember_prefers_an_explicit_intention_in_the_message(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    source_url = "https://example.com/fellowship"
    actions = SourceActions(
        {
            source_url: (
                "<html><head><title>Builders Fellowship</title></head>"
                "<body><p>Applications are open.</p></body></html>"
            )
        }
    )
    store = SQLiteStore(tmp_path / "dont-forget.db")
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)

    agent.receive(f"Don't let me forget to apply: {source_url}.")

    intention = store.list_intentions()[0]
    assert intention.objective == "Apply to Builders Fellowship"
    assert intention.sources[0].value == source_url
    assert intention.confidence > 0.75


def test_remember_keeps_ambiguous_source_intention_uncertain(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    source_url = "https://example.com/event-sourcing"
    actions = SourceActions(
        {
            source_url: (
                "<html><head><title>Event Sourcing Notes</title></head>"
                "<body><p>A technical article about storing state changes.</p></body></html>"
            )
        }
    )
    store = SQLiteStore(tmp_path / "dont-forget.db")
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)

    agent.receive(f"{source_url} — remember this")

    intention = store.list_intentions()[0]
    assert intention.objective == "Follow up on Event Sourcing Notes"
    assert intention.confidence < 0.5
    assert "uncertain" in intention.current_state.casefold()
    assert intention.requirements == []
    assert intention.next_action is None
