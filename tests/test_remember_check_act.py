from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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
    actions = LocalActions(
        allowed_workspaces=[repository], allowed_source_roots=[tmp_path]
    )
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
    assert intention.deadline_evidence[0].excerpt == (
        "Deadline: 2026-09-03T11:00:00+00:00"
    )
    assert intention.deadline_evidence[0].source == source_page.as_uri()
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
    assert updated.deadline_evidence[0].observed_at == clock.current
    assert store.count_events(intention.id, "checked") == 1
    assert store.count_events(intention.id, "action_completed") == 0

    assert checker.run_due() == []
    assert store.count_events(intention.id, "checked") == 1
    assert store.count_events(intention.id, "action_completed") == 0


def test_repository_remember_ignores_bullets_outside_requirements(tmp_path: Path) -> None:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    source_page = tmp_path / "hackathon.txt"
    source_page.write_text(
        "Hackathon: Tiny Agents\n"
        "Deadline: 2026-09-05T12:00:00+00:00\n"
        "Requirements:\n"
        "- Public repository\n"
        "Prizes:\n"
        "- 100 dollars\n",
        encoding="utf-8",
    )
    repository = tmp_path / "project"
    repository.mkdir()
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([repository], allowed_source_roots=[tmp_path])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)

    agent.receive(
        f"don't let me forget this hackathon: {source_page.as_uri()}. "
        f"my project is in {repository}"
    )

    assert [item.description for item in store.list_intentions()[0].requirements] == [
        "Public repository"
    ]


def test_repository_remember_rejects_a_timezone_naive_deadline(tmp_path: Path) -> None:
    source_page = tmp_path / "hackathon.txt"
    source_page.write_text(
        "Hackathon: Tiny Agents\n"
        "Deadline: 2026-09-05T12:00:00\n"
        "Requirements:\n"
        "- Public repository\n",
        encoding="utf-8",
    )
    repository = tmp_path / "project"
    repository.mkdir()
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([repository], allowed_source_roots=[tmp_path])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)

    with pytest.raises(ValueError, match="verified timezone-aware deadline"):
        agent.receive(
            f"don't let me forget this hackathon: {source_page.as_uri()}. "
            f"my project is in {repository}"
        )


def test_repository_remember_marks_a_passed_deadline_blocked(tmp_path: Path) -> None:
    source_page = tmp_path / "hackathon.txt"
    source_page.write_text(
        "Hackathon: Tiny Agents\n"
        "Deadline: 2026-09-02T11:00:00+00:00\n"
        "Requirements:\n"
        "- Public repository\n",
        encoding="utf-8",
    )
    repository = tmp_path / "project"
    repository.mkdir()
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([repository], allowed_source_roots=[tmp_path])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)

    assert agent.receive(
        f"don't let me forget this hackathon: {source_page.as_uri()}. "
        f"my project is in {repository}"
    ) == "got it"

    intention = store.list_intentions()[0]
    assert intention.status == "blocked"
    assert intention.next_check_at is None
    assert intention.next_action is None
    assert "passed" in intention.current_state.casefold()
