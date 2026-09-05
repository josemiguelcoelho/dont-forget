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



def build_agent(
    tmp_path: Path,
    *,
    source_name: str = "hackathon.txt",
    title: str = "Tiny Agents Hackathon",
    deadline: datetime | None = None,
    requirements: tuple[str, ...] = ("Demo video",),
    repository_name: str = "project",
) -> tuple[SQLiteStore, DontForgetAgent, ControlledClock, Path, Path]:
    clock = ControlledClock(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
    source = tmp_path / source_name
    deadline = deadline or clock.current + timedelta(days=1)
    source.write_text(
        f"Hackathon: {title}\n"
        f"Deadline: {deadline.isoformat()}\n"
        "Requirements:\n"
        + "".join(f"- {requirement}\n" for requirement in requirements),
        encoding="utf-8",
    )
    repository = tmp_path / repository_name
    repository.mkdir()
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([repository], allowed_source_roots=[tmp_path])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, clock)
    return store, DontForgetAgent(store, interpreter, actions, clock, checker), clock, source, repository



def test_natural_check_refreshes_work_without_creating_a_new_intention(tmp_path: Path) -> None:
    store, agent, _, source, repository = build_agent(tmp_path)
    agent.receive(
        f"don't let me forget this hackathon: {source.as_uri()}. "
        f"my project is in {repository}"
    )

    assert agent.receive("am I forgetting anything?") == (
        "one thing. the hackathon closes tomorrow. you're still missing the demo."
    )

    intentions = store.list_intentions()
    assert len(intentions) == 1
    assert intentions[0].most_important_unresolved_requirement == "Demo video"
    assert store.count_events(intentions[0].id, "checked") == 1


def test_repository_follow_up_is_attached_to_the_only_open_intention(tmp_path: Path) -> None:
    store, agent, _, source, repository = build_agent(tmp_path)
    assert agent.receive(f"{source.as_uri()} — don't let me forget this") == "got you."

    assert agent.receive(f"my project is in {repository}") == "got it."

    intention = store.list_intentions()[0]
    assert [(item.kind, item.value) for item in intention.sources] == [
        ("url", source.as_uri()),
        ("repository", str(repository.resolve())),
    ]
    assert intention.most_important_unresolved_requirement == "Demo video"
    assert store.count_events(intention.id, "context_added") == 1
    assert store.count_events(intention.id, "checked") == 1


def test_natural_check_can_refer_to_a_specific_existing_intention(tmp_path: Path) -> None:
    store, agent, _, source, repository = build_agent(tmp_path)
    agent.receive(
        f"don't let me forget this hackathon: {source.as_uri()}. "
        f"my project is in {repository}"
    )
    other = tmp_path / "conference.txt"
    other.write_text("Event: Design Conference\n", encoding="utf-8")
    agent.receive(f"{other.as_uri()} — don't let me forget this")

    assert agent.receive("what about Tiny Agents?") == (
        "one thing. the hackathon closes tomorrow. you're still missing the demo."
    )

    intentions = store.list_intentions()
    tiny_agents = next(item for item in intentions if "Tiny Agents" in item.objective)
    conference = next(item for item in intentions if "Design Conference" in item.objective)
    assert store.count_events(tiny_agents.id, "checked") == 1
    assert store.count_events(conference.id, "checked") == 0


def test_unknown_conversational_reference_does_not_guess_or_refresh(tmp_path: Path) -> None:
    store, agent, _, source, repository = build_agent(tmp_path)
    agent.receive(
        f"don't let me forget this hackathon: {source.as_uri()}. "
        f"my project is in {repository}"
    )

    assert agent.receive("what about the fellowship?") == "which one do you mean?"

    intention = store.list_intentions()[0]
    assert store.count_events(intention.id, "checked") == 0


def test_repository_follow_up_does_not_guess_between_open_intentions(tmp_path: Path) -> None:
    store, agent, _, source, repository = build_agent(tmp_path)
    agent.receive(f"{source.as_uri()} — don't let me forget this")
    other = tmp_path / "conference.txt"
    other.write_text("Event: Design Conference\n", encoding="utf-8")
    agent.receive(f"{other.as_uri()} — don't let me forget this")

    assert agent.receive(f"my project is in {repository}") == (
        "which one should I connect that project to?"
    )

    assert all(
        not any(source.kind == "repository" for source in intention.sources)
        for intention in store.list_intentions()
    )


def test_natural_check_does_not_present_stale_evidence_as_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, agent, _, source, repository = build_agent(tmp_path)
    agent.receive(
        f"don't let me forget this hackathon: {source.as_uri()}. "
        f"my project is in {repository}"
    )

    def fail_refresh(source_url: str) -> str:
        raise OSError("unavailable")

    monkeypatch.setattr(agent.actions, "read_source", fail_refresh)
    assert agent.receive("am I forgetting anything?") == (
        "I couldn't refresh that right now. I'll try again."
    )

    intention = store.list_intentions()[0]
    assert store.count_events(intention.id, "check_failed") == 1


def test_targeted_action_approval_only_changes_the_referenced_project(tmp_path: Path) -> None:
    clock = ControlledClock(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
    repositories = [tmp_path / "alpha", tmp_path / "beta"]
    sources = [tmp_path / "alpha.txt", tmp_path / "beta.txt"]
    for name, repository, source in zip(("Alpha", "Beta"), repositories, sources):
        repository.mkdir()
        (repository / "pyproject.toml").write_text(
            f'[project]\nname = "{name.casefold()}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        source.write_text(
            f"Hackathon: {name} Hackathon\n"
            "Deadline: 2026-09-05T12:00:00+00:00\n"
            "Requirements:\n- README setup\n",
            encoding="utf-8",
        )
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions(repositories, allowed_source_roots=[tmp_path])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)
    for source, repository in zip(sources, repositories):
        agent.receive(
            f"don't let me forget this hackathon: {source.as_uri()}. "
            f"my project is in {repository}"
        )
    for intention in store.list_intentions():
        checker.check_now(intention.id)

    assert agent.receive("handle what you can for Alpha Hackathon") == (
        "done. README setup is fixed. all requirements are covered."
    )

    assert (repositories[0] / "README.md").exists()
    assert not (repositories[1] / "README.md").exists()
    intentions = store.list_intentions()
    alpha = next(item for item in intentions if "Alpha" in item.objective)
    beta = next(item for item in intentions if "Beta" in item.objective)
    assert store.count_events(alpha.id, "action_completed") == 1
    assert store.count_events(beta.id, "action_completed") == 0


@pytest.mark.parametrize(
    ("already_fixed", "expected_reply"),
    [
        (
            False,
            "done. I fixed the README setup in 2 projects. everything else is covered.",
        ),
        (
            True,
            "done. I fixed the README setup in 1 project. everything else is covered.",
        ),
    ],
)
def test_broad_action_approval_completes_every_currently_safe_action(
    tmp_path: Path, already_fixed: bool, expected_reply: str
) -> None:
    clock = ControlledClock(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
    repositories = [tmp_path / "alpha", tmp_path / "beta"]
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions(repositories, allowed_source_roots=[tmp_path])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)
    for name, repository in zip(("Alpha", "Beta"), repositories):
        repository.mkdir()
        (repository / "pyproject.toml").write_text(
            f'[project]\nname = "{name.casefold()}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        source = tmp_path / f"{name.casefold()}.txt"
        source.write_text(
            f"Hackathon: {name} Hackathon\n"
            "Deadline: 2026-09-05T12:00:00+00:00\n"
            "Requirements:\n- README setup\n",
            encoding="utf-8",
        )
        agent.receive(
            f"don't let me forget this hackathon: {source.as_uri()}. "
            f"my project is in {repository}"
        )
    for intention in store.list_intentions():
        checker.check_now(intention.id)
    if already_fixed:
        actions.repair_readme_setup(repositories[1])

    assert agent.receive("handle what you can") == expected_reply

    assert all((repository / "README.md").exists() for repository in repositories)
    assert all(
        store.count_events(intention.id, "action_completed") == 1
        for intention in store.list_intentions()
    )


@pytest.mark.parametrize(
    ("message", "objective"),
    [
        ("I need to call the dentist tomorrow", "Call the dentist tomorrow"),
        ("remind me to renew my passport", "Renew my passport"),
        ("make sure I submit the form", "Submit the form"),
    ],
)
def test_everyday_phrasings_are_remembered_without_commands(
    tmp_path: Path, message: str, objective: str
) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)

    assert agent.receive(message) == "got you."
    assert store.list_intentions()[0].objective == objective


def test_source_reminder_is_kept_when_enrichment_is_temporarily_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)

    def fail_fetch(source_url: str) -> str:
        raise OSError("offline")

    monkeypatch.setattr(actions, "read_source", fail_fetch)
    assert agent.receive("https://example.com/hackathon don't let me forget this") == (
        "got you."
    )

    intention = store.list_intentions()[0]
    assert intention.sources[0].value == "https://example.com/hackathon"
    assert intention.deadline_at is None
    assert intention.next_check_at is None
    assert store.count_events(intention.id, "created") == 1


def test_broad_approval_reports_partial_completion_without_crossing_boundaries(
    tmp_path: Path,
) -> None:
    clock = ControlledClock(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
    repositories = [tmp_path / "alpha", tmp_path / "beta"]
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions(repositories, allowed_source_roots=[tmp_path])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)
    for name, repository in zip(("Alpha", "Beta"), repositories):
        repository.mkdir()
        (repository / "pyproject.toml").write_text(
            f'[project]\nname = "{name.casefold()}"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        source = tmp_path / f"{name.casefold()}.txt"
        source.write_text(
            f"Hackathon: {name} Hackathon\n"
            "Deadline: 2026-09-05T12:00:00+00:00\n"
            "Requirements:\n- README setup\n",
            encoding="utf-8",
        )
        agent.receive(
            f"don't let me forget this hackathon: {source.as_uri()}. "
            f"my project is in {repository}"
        )
    for intention in store.list_intentions():
        checker.check_now(intention.id)
    beta = next(item for item in store.list_intentions() if "Beta" in item.objective)
    assert beta.next_action is not None
    beta.next_action.parameters["repository"] = str(tmp_path / "outside")
    store.save_intention(beta)

    assert agent.receive("handle what you can") == (
        "done. I fixed the README setup in 1 project. "
        "I couldn't safely handle 1 other item."
    )

    alpha = next(item for item in store.list_intentions() if "Alpha" in item.objective)
    beta = next(item for item in store.list_intentions() if "Beta" in item.objective)
    assert store.count_events(alpha.id, "action_completed") == 1
    assert store.count_events(beta.id, "action_completed") == 0
    assert beta.next_action is not None
    assert beta.next_action.status == "proposed"


def test_natural_check_summarizes_multiple_open_items_without_internal_details(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)
    agent.receive("I need to call the dentist")
    agent.receive("remind me to renew my passport")

    reply = agent.receive("am I forgetting anything?")

    assert reply == "2 things. call the dentist; renew my passport."
    assert "id" not in reply.casefold()
    assert "check" not in reply.casefold()
    assert "pending" not in reply.casefold()


@pytest.mark.parametrize(
    "message",
    [
        "is there anything else I'm forgetting?",
        "anything I need to remember?",
        "what did I forget?",
        "do I have anything coming up?",
    ],
)
def test_common_check_phrasings_are_understood(tmp_path: Path, message: str) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)
    agent.receive("I need to call the dentist")

    assert agent.receive(message) == "one thing. you still need to call the dentist."
    assert len(store.list_intentions()) == 1


def test_incomplete_action_reference_never_authorizes_a_mutation(tmp_path: Path) -> None:
    store, agent, _, source, repository = build_agent(
        tmp_path, requirements=("README setup",)
    )
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "tiny-agents"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    agent.receive(
        f"don't let me forget this hackathon: {source.as_uri()}. "
        f"my project is in {repository}"
    )
    intention = store.list_intentions()[0]
    agent.checker.check_now(intention.id)

    assert agent.receive("handle what you can for a") == "which one do you mean?"
    assert not (repository / "README.md").exists()
    assert store.count_events(intention.id, "action_completed") == 0


def test_targeted_approval_does_not_report_work_from_another_intention(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    store = SQLiteStore(tmp_path / "dont-forget.db")
    actions = LocalActions([])
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)
    agent.receive("I need to call the dentist")
    agent.receive("remind me to renew my passport")

    assert agent.receive("handle what you can for passport") == (
        "you still need to renew my passport."
    )


def test_approval_revalidates_the_deadline_before_changing_files(tmp_path: Path) -> None:
    deadline = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
    store, agent, clock, source, repository = build_agent(
        tmp_path,
        deadline=deadline,
        requirements=("README setup",),
    )
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "tiny-agents"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    agent.receive(
        f"don't let me forget this hackathon: {source.as_uri()}. "
        f"my project is in {repository}"
    )
    intention = store.list_intentions()[0]
    agent.checker.check_now(intention.id)
    assert store.get_intention(intention.id).next_action is not None  # type: ignore[union-attr]
    clock.current = deadline + timedelta(minutes=1)

    assert agent.receive("handle what you can") == "one thing. the hackathon has closed."
    assert not (repository / "README.md").exists()
    blocked = store.get_intention(intention.id)
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.next_action is None
    assert store.count_events(intention.id, "action_completed") == 0


def test_approval_stops_if_the_deadline_passes_during_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    deadline = datetime(2026, 9, 3, 13, 0, tzinfo=timezone.utc)
    store, agent, clock, source, repository = build_agent(
        tmp_path,
        deadline=deadline,
        requirements=("README setup",),
    )
    (repository / "pyproject.toml").write_text(
        '[project]\nname = "tiny-agents"\nversion = "0.1.0"\n',
        encoding="utf-8",
    )
    (repository / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    agent.receive(
        f"don't let me forget this hackathon: {source.as_uri()}. "
        f"my project is in {repository}"
    )
    intention = store.list_intentions()[0]
    agent.checker.check_now(intention.id)
    original_read_source = agent.actions.read_source
    refresh_count = 0

    def cross_deadline_during_refresh(source_url: str) -> str:
        nonlocal refresh_count
        refresh_count += 1
        if refresh_count == 1:
            clock.current = deadline + timedelta(seconds=1)
        return original_read_source(source_url)

    monkeypatch.setattr(agent.actions, "read_source", cross_deadline_during_refresh)

    assert agent.receive("handle what you can") == "one thing. the hackathon has closed."
    assert not (repository / "README.md").exists()
    blocked = store.get_intention(intention.id)
    assert blocked is not None
    assert blocked.status == "blocked"
    assert blocked.next_action is None
    assert store.count_events(intention.id, "action_completed") == 0
