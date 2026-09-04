from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from time import sleep

import pytest

from dont_forget.actions import LocalActions
from dont_forget.agent import DontForgetAgent
from dont_forget.checker import IntentionChecker
from dont_forget.llm import DeterministicInterpreter
from dont_forget.store import ConcurrentUpdateError, SQLiteStore


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
    actions = LocalActions([repository], allowed_source_roots=[tmp_path])
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


def test_negated_approval_does_not_execute_an_action(tmp_path: Path) -> None:
    store, agent, checker, _, repository = build_flow(tmp_path, ["README setup"])
    checker.run_due()

    with pytest.raises(ValueError, match="clear intention"):
        agent.receive("Do not handle what you can; I haven't approved it.")

    intention = store.list_intentions()[0]
    assert intention.next_action is not None
    assert intention.next_action.status == "proposed"
    assert not (repository / "README.md").exists()
    assert store.count_events(intention.id, "action_completed") == 0


def test_concurrent_approval_claims_the_action_once(tmp_path: Path) -> None:
    store, agent, checker, _, repository = build_flow(tmp_path, ["README setup"])
    checker.run_due()
    original_repair = agent.actions.repair_readme_setup
    call_count = 0
    count_lock = Lock()

    def slow_repair(path: str) -> tuple[Path, bool]:
        nonlocal call_count
        with count_lock:
            call_count += 1
        sleep(0.1)
        return original_repair(path)

    agent.actions.repair_readme_setup = slow_repair  # type: ignore[method-assign]
    with ThreadPoolExecutor(max_workers=2) as executor:
        replies = list(executor.map(agent.receive, ["handle what you can"] * 2))

    intention = store.list_intentions()[0]
    assert call_count == 1
    assert any(reply.startswith("done. README setup is fixed.") for reply in replies)
    assert (repository / "README.md").exists()
    assert store.count_events(intention.id, "action_completed") == 1
    assert store.count_events(intention.id, "checked") == 2


def test_post_action_check_failure_is_durable_and_retryable(tmp_path: Path) -> None:
    store, agent, checker, _, repository = build_flow(
        tmp_path, ["README setup", "Demo video"]
    )
    checker.run_due()
    original_read_source = agent.actions.read_source

    def fail_source_refresh(source_url: str) -> str:
        raise OSError("source unavailable")

    agent.actions.read_source = fail_source_refresh  # type: ignore[method-assign]
    assert agent.receive("handle what you can") == (
        "done. README setup is fixed. follow-up CHECK failed and will be retried."
    )

    interrupted = store.list_intentions()[0]
    assert (repository / "README.md").exists()
    assert interrupted.next_action is not None
    assert interrupted.next_action.status == "completed"
    assert interrupted.next_action.post_check_pending is True
    assert store.count_events(interrupted.id, "action_completed") == 1
    assert store.count_events(interrupted.id, "check_failed") == 1

    agent.actions.read_source = original_read_source  # type: ignore[method-assign]
    assert agent.receive("handle what you can") == "you still need to record the demo."

    recovered = store.list_intentions()[0]
    assert recovered.next_action is None
    assert recovered.most_important_unresolved_requirement == "Demo video"
    assert store.count_events(recovered.id, "action_completed") == 1
    assert store.count_events(recovered.id, "check_failed") == 1
    assert store.count_events(recovered.id, "checked") == 2


def test_stale_claim_recovery_records_the_completed_mutation(tmp_path: Path) -> None:
    store, agent, checker, clock, repository = build_flow(tmp_path, ["README setup"])
    checker.run_due()
    intention = store.list_intentions()[0]

    claimed = store.claim_next_agent_action("repair_readme_setup", clock.current)
    assert claimed is not None
    assert claimed.next_action is not None
    readme, repaired = agent.actions.repair_readme_setup(
        claimed.next_action.parameters["repository"]
    )
    assert repaired is True
    assert store.count_events(intention.id, "action_completed") == 0

    clock.advance(minutes=6)
    assert agent.receive("handle what you can") == "all requirements are covered."

    recovered = store.get_intention(intention.id)
    assert recovered is not None
    assert recovered.status == "completed"
    assert store.count_events(intention.id, "action_completed") == 1
    assert store.list_event_payloads(intention.id, "action_completed") == [
        {
            "action": "repair_readme_setup",
            "path": str(readme),
            "changed": False,
            "recovered": True,
        }
    ]


def test_stale_check_cannot_overwrite_an_action_claim(tmp_path: Path) -> None:
    store, _, checker, clock, _ = build_flow(tmp_path, ["README setup"])
    checker.run_due()
    stale = store.list_intentions()[0]
    expected_version = stale.version

    competing_store = SQLiteStore(store.path)
    claimed = competing_store.claim_next_agent_action(
        "repair_readme_setup", clock.current
    )
    assert claimed is not None
    assert claimed.next_action is not None
    assert claimed.next_action.status == "executing"

    stale.current_state = "stale CHECK result"
    stale.version += 1
    with pytest.raises(ConcurrentUpdateError):
        store.save_intention_with_event(
            stale,
            expected_version=expected_version,
            event_type="checked",
            event_payload={"stale": True},
            event_created_at=clock.current,
        )

    current = store.get_intention(stale.id)
    assert current is not None
    assert current.next_action is not None
    assert current.next_action.status == "executing"
    assert store.count_events(stale.id, "checked") == 1
    competing_store.close()


def test_act_audits_an_already_satisfied_proposal_as_unchanged(tmp_path: Path) -> None:
    store, agent, checker, _, repository = build_flow(tmp_path, ["README setup"])
    checker.run_due()
    intention = store.list_intentions()[0]
    readme, repaired = agent.actions.repair_readme_setup(repository)
    assert repaired is True

    assert agent.receive("handle what you can") == "all requirements are covered."

    assert store.count_events(intention.id, "action_completed") == 1
    assert store.list_event_payloads(intention.id, "action_completed") == [
        {
            "action": "repair_readme_setup",
            "path": str(readme),
            "changed": False,
        }
    ]


@pytest.mark.parametrize(
    "invalid_payload",
    [None, [], "not an object", {}, {"action": "repair_readme_setup"}],
)
def test_store_rejects_an_unaudited_action_completion(
    tmp_path: Path, invalid_payload: object
) -> None:
    store, _, checker, clock, _ = build_flow(tmp_path, ["README setup"])
    checker.run_due()
    claimed = store.claim_next_agent_action("repair_readme_setup", clock.current)
    assert claimed is not None
    assert claimed.next_action is not None
    assert claimed.next_action.execution_id is not None

    with pytest.raises(ValueError, match="completion event payload"):
        store.complete_claimed_action(
            claimed.id,
            claimed.next_action.execution_id,
            clock.current,
            invalid_payload,  # type: ignore[arg-type]
        )

    persisted = store.get_intention(claimed.id)
    assert persisted is not None
    assert persisted.next_action is not None
    assert persisted.next_action.status == "executing"
    assert store.count_events(claimed.id, "action_completed") == 0
