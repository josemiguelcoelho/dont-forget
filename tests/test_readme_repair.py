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


def build_flow(tmp_path: Path, repository: Path) -> tuple[
    SQLiteStore, DontForgetAgent, IntentionChecker, ControlledClock
]:
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
    repository.mkdir()
    (repository / ".public").write_text("yes\n", encoding="utf-8")
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
    agent = DontForgetAgent(store, interpreter, actions, clock)
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent.receive(
        f"don't let me forget this hackathon: {source_page.as_uri()}. "
        f"my project is in {repository}"
    )
    return store, agent, checker, clock


def test_approval_repairs_existing_readme_once(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    store, agent, checker, clock = build_flow(tmp_path, repository)
    original = "# Tiny Agents\n\nExisting project description.\n"
    readme = repository / "README.md"
    readme.write_text(original, encoding="utf-8")

    clock.advance(hours=1)
    checker.run_due()

    assert readme.read_text(encoding="utf-8") == original
    pending = store.list_intentions()[0]
    assert pending.next_action is not None
    assert pending.next_action.action_type == "repair_readme_setup"
    assert pending.next_action.status == "proposed"

    assert agent.receive("handle what you can") == (
        "Added README setup instructions. You still need to record the demo."
    )
    repaired = readme.read_text(encoding="utf-8")
    assert repaired.startswith(original)
    assert repaired.count("## Setup") == 1
    assert "uv sync --extra test" in repaired
    assert "uv run pytest" in repaired
    assert store.count_events(pending.id, "action_completed") == 2

    assert agent.receive("handle what you can") == "Nothing else I can handle."
    assert readme.read_text(encoding="utf-8") == repaired
    assert store.count_events(pending.id, "action_completed") == 2


def test_approval_creates_missing_readme_with_derived_setup(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _, agent, checker, clock = build_flow(tmp_path, repository)
    readme = repository / "README.md"

    clock.advance(hours=1)
    checker.run_due()

    assert not readme.exists()
    assert agent.receive("please handle what you can") == (
        "Added README setup instructions. You still need to record the demo."
    )
    assert readme.read_text(encoding="utf-8") == (
        "# Tiny Agents\n\n"
        "## Setup\n\n"
        "```text\n"
        "uv sync --extra test\n"
        "uv run pytest\n"
        "```\n"
    )


def test_approval_completes_partial_setup_without_duplicate_commands(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    _, agent, checker, clock = build_flow(tmp_path, repository)
    readme = repository / "README.md"
    readme.write_text(
        "# Tiny Agents\n\n## Setup\n\n```text\nuv sync --extra test\n```\n",
        encoding="utf-8",
    )

    clock.advance(hours=1)
    checker.run_due()
    assert agent.receive("handle what you can") == (
        "Added README setup instructions. You still need to record the demo."
    )

    repaired = readme.read_text(encoding="utf-8")
    assert repaired.count("## Setup") == 1
    assert repaired.count("uv sync --extra test") == 1
    assert repaired.count("uv run pytest") == 1


def test_approval_rejects_pending_repair_outside_workspace(tmp_path: Path) -> None:
    repository = tmp_path / "project"
    store, agent, checker, clock = build_flow(tmp_path, repository)
    (repository / "README.md").write_text("# Tiny Agents\n", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "pyproject.toml").write_text(
        '[project]\nname = "outside"\nversion = "0.1.0"\n', encoding="utf-8"
    )
    (outside / "uv.lock").write_text("version = 1\n", encoding="utf-8")

    clock.advance(hours=1)
    checker.run_due()
    pending = store.list_intentions()[0]
    assert pending.next_action is not None
    pending.next_action.parameters["repository"] = str(outside)
    store.save_intention(pending)

    with pytest.raises(PermissionError):
        agent.receive("handle what you can")

    assert not (outside / "README.md").exists()
