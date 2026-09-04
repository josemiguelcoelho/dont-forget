from __future__ import annotations

from datetime import datetime, timedelta
from typing import Callable

from .actions import LocalActions
from .llm import Interpreter
from .models import Evidence, Intention, NextAction, RepositoryEvidence, Requirement
from .store import ConcurrentUpdateError, SQLiteStore


class IntentionChecker:
    def __init__(
        self,
        store: SQLiteStore,
        interpreter: Interpreter,
        actions: LocalActions,
        clock: Callable[[], datetime],
    ) -> None:
        self.store = store
        self.interpreter = interpreter
        self.actions = actions
        self.clock = clock

    def run_due(self) -> list[str]:
        now = self.clock()
        notices: list[str] = []
        for intention in self.store.list_due(now):
            try:
                notice = self._check(intention, now)
            except ConcurrentUpdateError:
                continue
            except Exception as error:
                try:
                    self._record_check_failure(intention, now, error)
                except ConcurrentUpdateError:
                    pass
                continue
            if notice:
                notices.append(notice)
        return notices

    def check_now(self, intention_id: str) -> str | None:
        intention = self.store.get_intention(intention_id)
        if intention is None:
            raise ValueError(f"Unknown intention: {intention_id}")
        return self._check(intention, self.clock())

    def check_now_safely(self, intention_id: str) -> tuple[bool, str | None]:
        intention = self.store.get_intention(intention_id)
        if intention is None:
            raise ValueError(f"Unknown intention: {intention_id}")
        now = self.clock()
        try:
            return True, self._check(intention, now)
        except ConcurrentUpdateError:
            return False, None
        except Exception as error:
            try:
                self._record_check_failure(intention, now, error)
            except ConcurrentUpdateError:
                pass
            return False, None

    def _check(self, intention: Intention, now: datetime) -> str | None:
        expected_version = intention.version
        source_url = next(
            (source.value for source in intention.sources if source.kind == "url"),
            None,
        )
        if source_url is None:
            return self._check_without_source(intention, now)
        repository_path = next(
            (source.value for source in intention.sources if source.kind == "repository"),
            None,
        )
        source_text = self.actions.read_source(source_url)
        if repository_path is None:
            return self._check_source_only(intention, source_url, source_text, now)
        repository = self.actions.inspect_repository(repository_path)
        source_enrichment = self.interpreter.enrich_source(source_url, source_text, now)
        if source_enrichment.deadline_at is None or not source_enrichment.deadline_evidence:
            return self._block_repository_deadline(
                intention,
                source_url,
                now,
                deadline_at=None,
                deadline_evidence=[],
                state="The deadline can no longer be verified.",
                notice="the deadline can no longer be verified.",
            )
        if source_enrichment.deadline_at <= now:
            return self._block_repository_deadline(
                intention,
                source_url,
                now,
                deadline_at=source_enrichment.deadline_at,
                deadline_evidence=source_enrichment.deadline_evidence,
                state="The verified deadline has passed.",
                notice="the verified deadline has passed.",
            )
        if not source_enrichment.requirements:
            return self._block_repository_deadline(
                intention,
                source_url,
                now,
                deadline_at=source_enrichment.deadline_at,
                deadline_evidence=source_enrichment.deadline_evidence,
                state="The source requirements can no longer be verified.",
                notice="the source requirements can no longer be verified.",
            )
        intention.context_evidence = source_enrichment.context_evidence
        intention.requirements = [
            Requirement(description=item.description, evidence=[item.evidence])
            for item in source_enrichment.requirements
        ]
        assessment = self.interpreter.check(
            intention, source_enrichment, repository, now
        )
        intention.deadline_at = assessment.deadline_at
        intention.deadline_evidence = source_enrichment.deadline_evidence
        missing_requirements = self._assess_requirements(
            intention, repository, assessment.deadline_near, now
        )
        most_important = max(missing_requirements, key=lambda item: item[1], default=None)
        intention.most_important_unresolved_requirement = (
            most_important[0] if most_important else None
        )
        intention.requirement_capability = self._classify_requirement(
            intention.most_important_unresolved_requirement
        )

        checked_payload = {
            "deadline_near": assessment.deadline_near,
            "deadline_verified": bool(intention.deadline_evidence),
            "repository_public": repository.is_public,
            "demo_present": repository.has_demo,
            "most_important_unresolved_requirement": (
                intention.most_important_unresolved_requirement
            ),
        }

        notice: str | None = None
        if missing_requirements:
            descriptions = ", ".join(item[0] for item in missing_requirements)
            intention.current_state = (
                f"Unresolved requirements: {descriptions}. Most important: "
                f"{intention.most_important_unresolved_requirement}."
            )
        else:
            intention.current_state = "All known requirements are satisfied."

        if (
            intention.requirement_capability == "agent_can_handle"
            and not repository.has_useful_setup
            and repository.setup_commands
        ):
            intention.next_action = NextAction(
                description="Add setup instructions to README.md",
                mode="agent",
                action_type="repair_readme_setup",
                parameters={
                    "repository": repository.repository,
                    "commands": repository.setup_commands,
                },
            )
            intention.current_state += " README setup instructions are missing; repair awaits approval."
        else:
            intention.next_action = None

        if missing_requirements:
            intention.status = "active"
            intention.resolved_at = None
            intention.next_check_at = min(
                now + timedelta(hours=6), assessment.deadline_at
            )
            if notice is None and intention.most_important_unresolved_requirement:
                notice = self._missing_notice(intention.most_important_unresolved_requirement)
        else:
            intention.status = "completed"
            intention.resolved_at = now
            intention.next_check_at = None
            notice = "all set. your requirements are covered."
        intention.updated_at = now
        intention.version += 1
        self.store.save_intention_with_event(
            intention,
            expected_version=expected_version,
            event_type="checked",
            event_payload=checked_payload,
            event_created_at=now,
        )
        return notice

    def _check_without_source(self, intention: Intention, now: datetime) -> str:
        expected_version = intention.version
        intention.status = "active"
        intention.current_state = (
            "No source evidence is available; the user's objective remains pending."
        )
        intention.next_check_at = None
        intention.updated_at = now
        intention.version += 1
        self.store.save_intention_with_event(
            intention,
            expected_version=expected_version,
            event_type="checked",
            event_payload={"source_available": False, "objective_pending": True},
            event_created_at=now,
        )
        return f"one thing. {intention.objective.casefold()} is still pending."

    def _record_check_failure(
        self,
        intention: Intention,
        now: datetime,
        error: Exception,
    ) -> None:
        expected_version = intention.version
        if (
            intention.deadline_at is not None
            and intention.deadline_evidence
            and intention.deadline_at > now
        ):
            intention.next_check_at = min(
                now + timedelta(hours=1), intention.deadline_at
            )
            intention.current_state = "CHECK failed; source refresh will be retried."
        else:
            intention.status = "blocked"
            intention.next_check_at = None
            intention.current_state = "CHECK failed and no future verified deadline is available."
        intention.updated_at = now
        intention.version += 1
        self.store.save_intention_with_event(
            intention,
            expected_version=expected_version,
            event_type="check_failed",
            event_payload={"error_type": type(error).__name__},
            event_created_at=now,
        )

    def _block_repository_deadline(
        self,
        intention: Intention,
        source_url: str,
        now: datetime,
        *,
        deadline_at: datetime | None,
        deadline_evidence: list[Evidence],
        state: str,
        notice: str,
    ) -> str:
        expected_version = intention.version
        intention.deadline_at = deadline_at
        intention.deadline_evidence = deadline_evidence
        intention.status = "blocked"
        intention.resolved_at = None
        intention.current_state = state
        intention.most_important_unresolved_requirement = None
        intention.requirement_capability = None
        intention.next_action = None
        intention.next_check_at = None
        intention.updated_at = now
        intention.version += 1
        self.store.save_intention_with_event(
            intention,
            expected_version=expected_version,
            event_type="checked",
            event_payload={
                "deadline_verified": bool(deadline_evidence),
                "source": source_url,
            },
            event_created_at=now,
        )
        return notice

    def _check_source_only(
        self,
        intention: Intention,
        source_url: str,
        source_text: str,
        now: datetime,
    ) -> str | None:
        expected_version = intention.version
        enrichment = self.interpreter.enrich_source(source_url, source_text, now)
        intention.deadline_at = enrichment.deadline_at
        intention.deadline_evidence = enrichment.deadline_evidence
        intention.context_evidence = enrichment.context_evidence
        intention.requirements = [
            Requirement(description=item.description, evidence=[item.evidence])
            for item in enrichment.requirements
        ]

        notice = None
        if intention.deadline_at is not None and intention.deadline_evidence:
            if intention.deadline_at <= now:
                intention.status = "blocked"
                intention.next_check_at = None
                intention.current_state = "The verified deadline has passed."
                notice = "the verified deadline has passed."
            else:
                intention.status = "active"
                intention.next_check_at = min(now + timedelta(hours=6), intention.deadline_at)
                intention.current_state = "Source facts refreshed from verified evidence."
                if intention.deadline_at - now <= timedelta(days=1):
                    notice = f"deadline is coming up: {intention.deadline_at.isoformat()}."
        else:
            intention.next_check_at = None
            intention.current_state = "Source checked; important details remain unknown."

        intention.updated_at = now
        intention.version += 1
        self.store.save_intention_with_event(
            intention,
            expected_version=expected_version,
            event_type="checked",
            event_payload={
                "deadline_verified": bool(intention.deadline_evidence),
                "source": source_url,
            },
            event_created_at=now,
        )
        return notice

    def _assess_requirements(
        self,
        intention: Intention,
        repository: RepositoryEvidence,
        deadline_near: bool,
        now: datetime,
    ) -> list[tuple[str, int]]:
        missing: list[tuple[str, int]] = []
        for requirement in intention.requirements:
            normalized = requirement.description.casefold()
            if "demo" in normalized:
                satisfied = repository.has_demo
                claim = f"Demo file is {'present' if satisfied else 'missing'}"
                importance = 100 if deadline_near else 80
            elif "public" in normalized and "repositor" in normalized:
                satisfied = repository.is_public
                claim = f"Repository public marker is {'present' if satisfied else 'missing'}"
                importance = 90
            elif "readme" in normalized or "setup instruction" in normalized:
                satisfied = repository.has_useful_setup
                claim = f"README setup instructions are {'present' if satisfied else 'missing'}"
                importance = 85
            else:
                satisfied = False
                claim = "No local evidence satisfies this requirement"
                importance = 10
            self._update_requirement(
                intention,
                requirement.description,
                "satisfied" if satisfied else "missing",
                claim,
                repository.repository,
                now,
            )
            if not satisfied:
                missing.append((requirement.description, importance))
        return missing

    @staticmethod
    def _classify_requirement(description: str | None) -> str | None:
        if description is None:
            return None
        normalized = description.casefold()
        if "readme" in normalized or "setup instruction" in normalized:
            return "agent_can_handle"
        return "user_must_handle"

    @staticmethod
    def _missing_notice(description: str) -> str:
        labels = {
            "Demo video": "your demo",
            "Public repository": "your public repository",
        }
        subject = labels.get(description, description.casefold())
        return f"one thing. {subject} is still missing."

    @staticmethod
    def _update_requirement(
        intention: Intention,
        description: str,
        status: str,
        claim: str,
        source: str,
        observed_at: datetime,
    ) -> None:
        requirement = next(
            (item for item in intention.requirements if item.description == description), None
        )
        if requirement is None:
            return
        requirement.status = status  # type: ignore[assignment]
        requirement.evidence.append(
            Evidence(
                claim=claim,
                source=source,
                observed_at=observed_at,
                confidence=1.0,
            )
        )
