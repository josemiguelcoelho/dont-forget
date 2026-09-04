from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

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


class ControlledClock:
    def __init__(self, current: datetime) -> None:
        self.current = current

    def __call__(self) -> datetime:
        return self.current


def build_source_flow(
    tmp_path: Path, source_url: str, source_text: str, now: datetime
) -> tuple[SQLiteStore, DontForgetAgent, IntentionChecker]:
    actions = SourceActions({source_url: source_text})
    store = SQLiteStore(tmp_path / "dont-forget.db")
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, lambda: now)
    agent = DontForgetAgent(store, interpreter, actions, lambda: now, checker)
    return store, agent, checker


def test_remember_extracts_only_an_explicit_verified_deadline(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    source_url = "https://example.com/hackathon"
    store, agent, _ = build_source_flow(
        tmp_path,
        source_url,
        """
        <html><head><title>Tiny Agents Hackathon</title></head><body>
        <p>Deadline: 2026-09-10T17:00:00+00:00</p>
        </body></html>
        """,
        now,
    )

    assert agent.receive(f"{source_url} — don't let me forget this") == "got you."

    intention = store.list_intentions()[0]
    assert intention.deadline_at == datetime(2026, 9, 10, 17, 0, tzinfo=timezone.utc)
    assert intention.next_check_at == now + timedelta(hours=1)
    assert len(intention.deadline_evidence) == 1
    evidence = intention.deadline_evidence[0]
    assert evidence.source == source_url
    assert evidence.excerpt == "Deadline: 2026-09-10T17:00:00+00:00"
    assert evidence.confidence == 1.0


def test_remember_extracts_explicit_participation_requirements(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    source_url = "https://example.com/hackathon"
    store, agent, _ = build_source_flow(
        tmp_path,
        source_url,
        """
        <html><head><title>Tiny Agents Hackathon</title></head><body>
        <h2>How to submit</h2>
        <ul><li>Public GitHub repository</li><li>Two-minute demo video</li></ul>
        <h2>Prizes</h2><ul><li>Community award</li></ul>
        </body></html>
        """,
        now,
    )

    agent.receive(f"{source_url} — don't let me forget this")

    intention = store.list_intentions()[0]
    assert [requirement.description for requirement in intention.requirements] == [
        "Public GitHub repository",
        "Two-minute demo video",
    ]
    assert all(requirement.status == "unknown" for requirement in intention.requirements)
    assert [requirement.evidence[0].excerpt for requirement in intention.requirements] == [
        "Public GitHub repository",
        "Two-minute demo video",
    ]
    assert all(
        requirement.evidence[0].source == source_url
        and requirement.evidence[0].confidence == 1.0
        for requirement in intention.requirements
    )


def test_remember_leaves_ambiguous_source_facts_unknown(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    source_url = "https://example.com/hackathon"
    store, agent, _ = build_source_flow(
        tmp_path,
        source_url,
        """
        <html><head><title>Tiny Agents Hackathon</title></head><body>
        <script>Deadline: 2026-09-10T17:00:00+00:00</script>
        <p>Applications close soon. Bring whatever you think is useful.</p>
        <h2>Ideas</h2><ul><li>A public repository might help</li></ul>
        </body></html>
        """,
        now,
    )

    agent.receive(f"{source_url} — don't let me forget this")

    intention = store.list_intentions()[0]
    assert intention.deadline_at is None
    assert intention.deadline_evidence == []
    assert intention.requirements == []
    assert intention.next_check_at is None
    assert "inferred" in intention.current_state.casefold()
    assert intention.context_evidence[0].excerpt == "Tiny Agents Hackathon"


def test_source_enrichment_evidence_survives_store_reopen(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    source_url = "https://example.com/hackathon"
    database = tmp_path / "dont-forget.db"
    store, agent, _ = build_source_flow(
        tmp_path,
        source_url,
        """
        <title>Tiny Agents Hackathon</title>
        <p>Deadline: 2026-09-10T17:00:00+00:00</p>
        <h2>Requirements</h2><ul><li>Public repository</li></ul>
        """,
        now,
    )
    agent.receive(f"{source_url} — don't let me forget this")
    intention_id = store.list_intentions()[0].id
    store.close()

    reopened = SQLiteStore(database)
    intention = reopened.get_intention(intention_id)

    assert intention is not None
    assert intention.deadline_evidence[0].excerpt == "Deadline: 2026-09-10T17:00:00+00:00"
    assert intention.requirements[0].evidence[0].excerpt == "Public repository"
    assert intention.context_evidence[0].excerpt == "Tiny Agents Hackathon"


def test_check_refreshes_source_only_intention_on_verified_deadline_schedule(
    tmp_path: Path,
) -> None:
    clock = ControlledClock(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
    source_url = "https://example.com/hackathon"
    deadline = datetime(2026, 9, 3, 17, 0, tzinfo=timezone.utc)
    actions = SourceActions(
        {
            source_url: (
                "<title>Tiny Agents Hackathon</title>"
                "<p>Deadline: 2026-09-03T17:00:00+00:00</p>"
            )
        }
    )
    store = SQLiteStore(tmp_path / "dont-forget.db")
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)
    agent.receive(f"{source_url} — don't let me forget this")
    clock.current += timedelta(hours=1)

    assert checker.run_due() == ["deadline is coming up: 2026-09-03T17:00:00+00:00."]

    refreshed = store.list_intentions()[0]
    assert refreshed.deadline_at == deadline
    assert refreshed.deadline_evidence[0].observed_at == clock.current
    assert refreshed.next_check_at == deadline
    assert store.count_events(refreshed.id, "checked") == 1


def test_source_fetching_pins_the_validated_address_and_is_bounded() -> None:
    from dont_forget.sources import UrlSourceFetcher

    calls: list[tuple[str, str, float, int]] = []

    def transport(url: str, address: str, timeout: float, max_bytes: int) -> bytes:
        calls.append((url, address, timeout, max_bytes))
        return b"0123456789"

    fetcher = UrlSourceFetcher(
        transport=transport,
        resolver=lambda *args, **kwargs: [
            (None, None, None, None, ("93.184.216.34", 443))
        ],
        timeout=2.0,
        max_bytes=10,
    )

    assert fetcher.fetch("https://example.com/source") == "0123456789"
    assert calls == [
        ("https://example.com/source", "93.184.216.34", 2.0, 10)
    ]


def test_check_clears_a_deadline_that_is_no_longer_verifiable(tmp_path: Path) -> None:
    clock = ControlledClock(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
    source_url = "https://example.com/hackathon"
    actions = SourceActions(
        {
            source_url: (
                "<title>Tiny Agents Hackathon</title>"
                "<p>Deadline: 2026-09-03T17:00:00+00:00</p>"
            )
        }
    )
    store = SQLiteStore(tmp_path / "dont-forget.db")
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)
    agent.receive(f"{source_url} — don't let me forget this")
    actions.pages[source_url] = "<title>Tiny Agents Hackathon</title><p>Details soon.</p>"
    clock.current += timedelta(hours=1)

    assert checker.run_due() == []

    refreshed = store.list_intentions()[0]
    assert refreshed.deadline_at is None
    assert refreshed.deadline_evidence == []
    assert refreshed.next_check_at is None


def test_remember_marks_an_already_passed_verified_deadline_blocked(tmp_path: Path) -> None:
    now = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
    source_url = "https://example.com/hackathon"
    store, agent, _ = build_source_flow(
        tmp_path,
        source_url,
        "<title>Tiny Agents Hackathon</title>"
        "<p>Deadline: 2026-09-03T11:00:00+00:00</p>",
        now,
    )

    assert agent.receive(f"{source_url} — don't let me forget this") == "got you."

    intention = store.list_intentions()[0]
    assert intention.deadline_evidence
    assert intention.status == "blocked"
    assert intention.next_check_at is None
    assert "passed" in intention.current_state.casefold()


def test_url_fetcher_rejects_private_network_destinations() -> None:
    from dont_forget.sources import UrlSourceFetcher

    opened = False

    def transport(*args: object, **kwargs: object) -> bytes:
        nonlocal opened
        opened = True
        return b""

    fetcher = UrlSourceFetcher(transport=transport)

    with pytest.raises(PermissionError, match="public network"):
        fetcher.fetch("http://127.0.0.1/private")
    assert not opened


def test_url_fetcher_rejects_files_outside_approved_source_roots(tmp_path: Path) -> None:
    from dont_forget.sources import UrlSourceFetcher

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    fetcher = UrlSourceFetcher(allowed_file_roots=[allowed])

    with pytest.raises(PermissionError, match="approved source roots"):
        fetcher.fetch(outside.as_uri())


def test_local_actions_does_not_treat_mutation_workspace_as_source_root(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    source = workspace / "private.txt"
    source.write_text("private", encoding="utf-8")
    actions = LocalActions([workspace], allowed_source_roots=[])

    with pytest.raises(PermissionError, match="approved source roots"):
        actions.read_source(source.as_uri())


def test_run_due_isolates_a_failed_source_from_later_intentions(tmp_path: Path) -> None:
    clock = ControlledClock(datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc))
    failed_url = "https://example.com/failed-hackathon"
    healthy_url = "https://example.com/healthy-hackathon"
    actions = SourceActions(
        {
            failed_url: (
                "<title>Failed Hackathon</title>"
                "<p>Deadline: 2026-09-03T17:00:00+00:00</p>"
            ),
            healthy_url: (
                "<title>Healthy Hackathon</title>"
                "<p>Deadline: 2026-09-03T18:00:00+00:00</p>"
            ),
        }
    )
    store = SQLiteStore(tmp_path / "dont-forget.db")
    interpreter = DeterministicInterpreter()
    checker = IntentionChecker(store, interpreter, actions, clock)
    agent = DontForgetAgent(store, interpreter, actions, clock, checker)
    agent.receive(f"{failed_url} — don't let me forget this")
    clock.current += timedelta(minutes=1)
    agent.receive(f"{healthy_url} — don't let me forget this")
    del actions.pages[failed_url]
    clock.current += timedelta(hours=1)

    assert checker.run_due() == ["deadline is coming up: 2026-09-03T18:00:00+00:00."]

    intentions = {item.sources[0].value: item for item in store.list_intentions()}
    assert store.count_events(intentions[failed_url].id, "check_failed") == 1
    assert store.count_events(intentions[healthy_url].id, "checked") == 1
