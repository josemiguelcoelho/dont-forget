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


def test_local_remember_check_act_flow_is_persistent_and_idempotent(tmp_path: Path) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    clock = ControlledClock(now)

    source_page = tmp_path / "hackathon.txt"
    source_page.write_text(
        "Hackathon: Tiny Agents\n"
        "Deadline: 2026-09-03T11:00:00+00:00\n"
        "Requirements:\n"
        "- Public repository\n"
        "- Demo video\n",
        encoding="utf-8",
    )

    repository = tmp_path / "project"
    repository.mkdir()
    (repository / "README.md").write_text("# Tiny Agents\n", encoding="utf-8")
    (repository / ".public").write_text("yes\n", encoding="utf-8")

    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions(allowed_workspaces=[repository])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)

    reply = agent.receive(
        f"don't let me forget this hackathon: {source_page.as_uri()}. "
        f"my project is in {repository}"
    )

    assert reply == "got it"
    assert len(reply) < 40

    intentions = store.list_intentions()
    assert len(intentions) == 1
    intention = intentions[0]
    assert intention.objective == "Submit a valid project to Tiny Agents"
    assert intention.original_message.startswith("don't let me forget")
    assert intention.deadline_at == datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
    assert intention.next_check_at == now + timedelta(hours=1)
    assert intention.confidence == 0.95
    assert {source.kind for source in intention.sources} == {"url", "repository"}
    assert {requirement.description for requirement in intention.requirements} == {
        "Public repository",
        "Demo video",
    }
    assert all(requirement.evidence for requirement in intention.requirements)
    assert all(requirement.evidence[0].source == source_page.as_uri() for requirement in intention.requirements)
    assert store.count_events(intention.id, "created") == 1

    store.close()
    store = SQLiteStore(tmp_path / "dont-forget.db")
    persisted = store.get_intention(intention.id)
    assert persisted is not None
    assert persisted.deadline_at == intention.deadline_at

    checker = IntentionChecker(store, interpreter, actions, clock)
    assert checker.run_due() == []

    clock.advance(hours=1)
    notices = checker.run_due()

    assert notices == ["one thing. your demo is still missing."]
    assert len(notices[0]) < 120

    checklist = repository / "DEMO_CHECKLIST.md"
    assert not checklist.exists()

    updated = store.get_intention(intention.id)
    assert updated is not None
    assert updated.current_state == "Unresolved requirements: Demo video. Most important: Demo video."
    demo = next(req for req in updated.requirements if req.description == "Demo video")
    assert demo.status == "missing"
    assert any(item.source == str(repository) for item in demo.evidence)
    assert store.count_events(intention.id, "checked") == 1
    assert store.count_events(intention.id, "action_completed") == 0

    assert checker.run_due() == []
    assert store.count_events(intention.id, "checked") == 1
    assert store.count_events(intention.id, "action_completed") == 0
