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


def build_flow(
    tmp_path: Path,
    *,
    deadline: datetime,
    public: bool,
    demo: bool,
) -> tuple[SQLiteStore, IntentionChecker, ControlledClock]:
    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    clock = ControlledClock(now)
    source_page = tmp_path / "hackathon.txt"
    source_page.write_text(
        "Hackathon: Tiny Agents\n"
        f"Deadline: {deadline.isoformat()}\n"
        "Requirements:\n"
        "- Public repository\n"
        "- Demo video\n",
        encoding="utf-8",
    )
    repository = tmp_path / "project"
    repository.mkdir()
    (repository / "README.md").write_text("# Tiny Agents\n", encoding="utf-8")
    if public:
        (repository / ".public").write_text("yes\n", encoding="utf-8")
    if demo:
        (repository / "demo.mp4").write_bytes(b"demo")

    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([repository])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)
    agent.receive(
        f"don't let me forget this hackathon: {source_page.as_uri()}. "
        f"my project is in {repository}"
    )
    clock.advance(hours=1)
    return store, checker, clock


def test_check_resolves_intention_when_all_requirements_are_satisfied(tmp_path: Path) -> None:
    deadline = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    store, checker, clock = build_flow(
        tmp_path, deadline=deadline, public=True, demo=True
    )

    assert checker.run_due() == ["all set. your requirements are covered."]

    intention = store.list_intentions()[0]
    assert {requirement.status for requirement in intention.requirements} == {"satisfied"}
    assert intention.status == "completed"
    assert intention.most_important_unresolved_requirement is None
    assert intention.resolved_at == clock.current
    assert intention.next_check_at is None


def test_check_reports_the_one_missing_requirement(tmp_path: Path) -> None:
    deadline = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    store, checker, _ = build_flow(
        tmp_path, deadline=deadline, public=True, demo=False
    )

    assert checker.run_due() == ["one thing. your demo is still missing."]

    intention = store.list_intentions()[0]
    statuses = {item.description: item.status for item in intention.requirements}
    assert statuses == {"Public repository": "satisfied", "Demo video": "missing"}
    assert intention.status == "active"
    assert intention.most_important_unresolved_requirement == "Demo video"


def test_check_prioritizes_public_repository_when_multiple_items_are_missing(
    tmp_path: Path,
) -> None:
    deadline = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    store, checker, _ = build_flow(
        tmp_path, deadline=deadline, public=False, demo=False
    )

    assert checker.run_due() == ["one thing. your public repository is still missing."]

    intention = store.list_intentions()[0]
    assert [item.status for item in intention.requirements] == ["missing", "missing"]
    assert intention.most_important_unresolved_requirement == "Public repository"


def test_check_prioritizes_demo_as_the_deadline_gets_close(tmp_path: Path) -> None:
    deadline = datetime(2026, 9, 3, 11, 0, tzinfo=timezone.utc)
    store, checker, _ = build_flow(
        tmp_path, deadline=deadline, public=False, demo=False
    )

    assert checker.run_due() == ["one thing. your demo is still missing."]
    intention = store.list_intentions()[0]
    assert intention.most_important_unresolved_requirement == "Demo video"


def test_repeated_checks_preserve_requirement_state(tmp_path: Path) -> None:
    deadline = datetime(2026, 9, 5, 12, 0, tzinfo=timezone.utc)
    store, checker, clock = build_flow(
        tmp_path, deadline=deadline, public=True, demo=False
    )

    assert checker.run_due() == ["one thing. your demo is still missing."]
    first = store.list_intentions()[0]
    first_next_check = first.next_check_at
    clock.advance(hours=6)
    assert checker.run_due() == ["one thing. your demo is still missing."]

    repeated = store.list_intentions()[0]
    assert len(repeated.requirements) == 2
    assert {item.description: item.status for item in repeated.requirements} == {
        "Public repository": "satisfied",
        "Demo video": "missing",
    }
    assert repeated.most_important_unresolved_requirement == "Demo video"
    assert repeated.next_check_at is not None
    assert first_next_check is not None
    assert repeated.next_check_at > first_next_check
    assert store.count_events(repeated.id, "checked") == 2
