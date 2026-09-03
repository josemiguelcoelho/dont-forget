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


def build_flow(
    tmp_path: Path, requirements: list[str]
) -> tuple[SQLiteStore, DontForgetAgent, IntentionChecker, ControlledClock, Path]:
    clock = ControlledClock(datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc))
    source_page = tmp_path / "hackathon.txt"
    source_page.write_text(
        "Hackathon: Tiny Agents\n"
        "Deadline: 2026-09-05T12:00:00+00:00\n"
        "Requirements:\n"
        + "".join(f"- {requirement}\n" for requirement in requirements),
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
    return store, agent, checker, clock, repository


def test_check_classifies_readme_setup_as_agent_capable(tmp_path: Path) -> None:
    store, _, checker, _, repository = build_flow(tmp_path, ["README setup"])

    assert checker.run_due() == ["one thing. readme setup is still missing."]

    intention = store.list_intentions()[0]
    assert intention.most_important_unresolved_requirement == "README setup"
    assert intention.requirement_capability == "agent_can_handle"
    assert intention.next_action is not None
    assert intention.next_action.action_type == "repair_readme_setup"
    assert intention.next_action.status == "proposed"
    assert not (repository / "README.md").exists()


def test_check_classifies_demo_as_user_only_without_inventing_action(tmp_path: Path) -> None:
    store, _, checker, _, _ = build_flow(tmp_path, ["Demo video"])

    assert checker.run_due() == ["one thing. your demo is still missing."]

    intention = store.list_intentions()[0]
    assert intention.most_important_unresolved_requirement == "Demo video"
    assert intention.requirement_capability == "user_must_handle"
    assert intention.next_action is None


def test_act_requires_explicit_approval_then_repairs_and_rechecks(tmp_path: Path) -> None:
    store, agent, checker, _, repository = build_flow(
        tmp_path, ["README setup", "Demo video"]
    )
    readme = repository / "README.md"

    assert checker.run_due() == ["one thing. readme setup is still missing."]
    intention = store.list_intentions()[0]
    assert intention.next_action is not None

    with pytest.raises(ValueError):
        agent.receive("go ahead")
    assert not readme.exists()

    assert agent.receive("handle what you can") == (
        "done. README setup is fixed. you still need to record the demo."
    )
    assert readme.exists()
    assert "uv sync --extra test" in readme.read_text(encoding="utf-8")

    updated = store.list_intentions()[0]
    assert updated.most_important_unresolved_requirement == "Demo video"
    assert updated.requirement_capability == "user_must_handle"
    assert updated.next_action is None
    assert store.count_events(updated.id, "checked") == 2
    assert store.count_events(updated.id, "action_completed") == 1
    assert store.list_event_payloads(updated.id, "action_completed") == [
        {"action": "repair_readme_setup", "path": str(readme)}
    ]


def test_repeated_act_is_idempotent(tmp_path: Path) -> None:
    store, agent, checker, _, repository = build_flow(
        tmp_path, ["README setup", "Demo video"]
    )
    checker.run_due()
    agent.receive("handle what you can")
    readme = repository / "README.md"
    contents = readme.read_text(encoding="utf-8")
    intention = store.list_intentions()[0]

    assert agent.receive("handle what you can") == "you still need to record the demo."
    assert readme.read_text(encoding="utf-8") == contents
    assert store.count_events(intention.id, "action_completed") == 1
    assert store.count_events(intention.id, "checked") == 2
