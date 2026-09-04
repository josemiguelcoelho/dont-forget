from datetime import datetime, timedelta, timezone
from pathlib import Path

from dont_forget.actions import LocalActions
from dont_forget.agent import DontForgetAgent
from dont_forget.checker import IntentionChecker
from dont_forget.llm import DeterministicInterpreter
from dont_forget.store import SQLiteStore


class ControlledClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current

    def advance(self, **kwargs: int) -> None:
        self.current += timedelta(**kwargs)


def test_natural_intention_completes_a_persistent_closed_loop(tmp_path: Path) -> None:
    clock = ControlledClock(datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc))
    source = tmp_path / "tiny-agents.txt"
    source.write_text(
        "Hackathon: Tiny Agents\n"
        "Deadline: 2026-09-05T12:00:00+00:00\n"
        "Requirements:\n"
        "- README setup\n"
        "- Demo video\n",
        encoding="utf-8",
    )
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / "pyproject.toml").write_text(
        "[project]\n"
        'name = "tiny-agents"\n'
        'version = "0.1.0"\n\n'
        "[project.optional-dependencies]\n"
        'test = ["pytest>=8,<9"]\n',
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    database = tmp_path / "dont-forget.db"
    actions = LocalActions([repository], allowed_source_roots=[tmp_path])
    interpreter = DeterministicInterpreter()
    store = SQLiteStore(database)
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)

    message = (
        f"Remember to submit my Tiny Agents project. Use {source.as_uri()} for the rules. "
        f"My project is in {repository}"
    )
    assert agent.receive(message) == "got it"

    remembered = store.list_intentions()[0]
    intention_id = remembered.id
    assert remembered.objective == "Submit my Tiny Agents project"
    assert remembered.original_message == message
    assert remembered.status == "active"
    assert remembered.deadline_evidence[0].excerpt == (
        "Deadline: 2026-09-05T12:00:00+00:00"
    )
    assert [item.description for item in remembered.requirements] == [
        "README setup",
        "Demo video",
    ]
    assert remembered.next_check_at == clock.current + timedelta(hours=1)

    store.close()
    store = SQLiteStore(database)
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)

    source.write_text(
        "Hackathon: Tiny Agents\n"
        "Deadline: 2026-09-06T12:00:00+00:00\n"
        "Requirements:\n"
        "- README setup\n"
        "- Demo video\n",
        encoding="utf-8",
    )
    clock.advance(hours=1)

    assert checker.run_due() == ["one thing. readme setup is still missing."]
    checked = store.get_intention(intention_id)
    assert checked is not None
    assert checked.objective == remembered.objective
    assert checked.deadline_at == datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    assert checked.deadline_evidence[0].observed_at == clock.current
    assert checked.most_important_unresolved_requirement == "README setup"
    assert checked.requirement_capability == "agent_can_handle"
    assert checked.next_action is not None
    assert checked.next_action.action_type == "repair_readme_setup"
    assert checked.next_action.status == "proposed"
    assert not (repository / "README.md").exists()

    assert agent.receive("Please handle what you can safely.") == (
        "done. README setup is fixed. you still need to record the demo."
    )
    acted = store.get_intention(intention_id)
    assert acted is not None
    assert acted.objective == remembered.objective
    assert acted.most_important_unresolved_requirement == "Demo video"
    assert acted.requirement_capability == "user_must_handle"
    assert acted.next_action is None
    assert store.count_events(intention_id, "created") == 1
    assert store.count_events(intention_id, "checked") == 2
    assert store.count_events(intention_id, "action_completed") == 1

    readme = repository / "README.md"
    contents = readme.read_text(encoding="utf-8")
    assert contents.count("uv sync --extra test") == 1
    assert contents.count("uv run pytest") == 1

    assert agent.receive("Please handle what you can safely.") == (
        "you still need to record the demo."
    )
    assert readme.read_text(encoding="utf-8") == contents
    assert store.count_events(intention_id, "checked") == 2
    assert store.count_events(intention_id, "action_completed") == 1


def test_remember_without_a_url_stays_truthfully_pending(tmp_path: Path) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)

    message = "Remember to call the dentist tomorrow"
    assert agent.receive(message) == "got you."

    intention = store.list_intentions()[0]
    assert intention.objective == "Call the dentist tomorrow"
    assert intention.original_message == message
    assert intention.status == "active"
    assert intention.sources == []
    assert intention.deadline_at is None
    assert intention.deadline_evidence == []
    assert intention.requirements == []
    assert intention.next_check_at is None
    assert intention.next_action is not None
    assert intention.next_action.mode == "user"
    assert intention.next_action.action_type == "user_follow_up"
    assert intention.next_action.status == "pending"

    assert checker.run_due() == []
    assert checker.check_now(intention.id) == (
        "one thing. call the dentist tomorrow is still pending."
    )
    checked = store.get_intention(intention.id)
    assert checked is not None
    assert checked.next_action == intention.next_action
    assert store.count_events(intention.id, "checked") == 1
    assert agent.receive("Please handle what you can safely.") == (
        "you still need to call the dentist tomorrow."
    )
    unchanged = store.get_intention(intention.id)
    assert unchanged is not None
    assert unchanged.next_action == intention.next_action
    assert store.count_events(intention.id, "action_completed") == 0
