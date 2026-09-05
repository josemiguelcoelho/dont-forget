from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

from .actions import LocalActions
from .checker import IntentionChecker
from .conversation import ConversationKind, ConversationPresenter, NaturalLanguageRouter
from .llm import Interpreter
from .models import Intention, Source
from .store import SQLiteStore


class DontForgetAgent:
    def __init__(
        self,
        store: SQLiteStore,
        interpreter: Interpreter,
        actions: LocalActions,
        clock: Callable[[], datetime],
        checker: IntentionChecker,
        router: NaturalLanguageRouter | None = None,
        presenter: ConversationPresenter | None = None,
    ) -> None:
        self.store = store
        self.interpreter = interpreter
        self.actions = actions
        self.clock = clock
        self.checker = checker
        self.router = router or NaturalLanguageRouter()
        self.presenter = presenter or ConversationPresenter()

    def receive(self, message: str) -> str:
        now = self.clock()
        if self.interpreter.is_action_approval(message):
            return self._act(now)
        routed = self.router.route(message)
        if routed.kind == ConversationKind.ACT:
            intention = self._resolve_reference(routed.reference)
            if intention is None:
                return "which one do you mean?"
            return self._act(now, intention.id)
        if routed.kind == ConversationKind.CHECK:
            return self._check(now, routed.reference)
        if routed.kind == ConversationKind.ADD_CONTEXT:
            return self._add_repository_context(routed.repository, now)
        context = self.interpreter.parse_message(message)
        source_text = ""
        source_read = context.source_url is None
        if context.source_url:
            try:
                source_text = self.actions.read_source(context.source_url)
                source_read = True
            except (OSError, PermissionError):
                if context.repository is not None:
                    raise
        intention = self.interpreter.remember(message, context, source_text, now)
        event_payload: dict[str, Any] = {"objective": intention.objective}
        if context.source_url:
            event_payload["source"] = context.source_url
            event_payload["source_read"] = source_read
        self.store.save_intention_with_event(
            intention,
            event_type="created",
            event_payload=event_payload,
            event_created_at=now,
        )
        return "got you." if context.repository is None else "got it"

    def _add_repository_context(self, repository: str | None, now: datetime) -> str:
        if repository is None:
            return "which project do you mean?"
        candidates = [
            intention
            for intention in self.store.list_intentions()
            if intention.status == "active"
            and any(source.kind == "url" for source in intention.sources)
            and not any(source.kind == "repository" for source in intention.sources)
        ]
        if len(candidates) != 1:
            return "which one should I connect that project to?"
        resolved = str(Path(repository).resolve())
        self.actions.inspect_repository(resolved)
        intention = candidates[0]
        expected_version = intention.version
        intention.sources.append(Source(kind="repository", value=resolved, observed_at=now))
        intention.updated_at = now
        intention.version += 1
        self.store.save_intention_with_event(
            intention,
            expected_version=expected_version,
            event_type="context_added",
            event_payload={"kind": "repository"},
            event_created_at=now,
        )
        self.checker.check_now_safely(intention.id)
        return "got it."

    def _check(self, now: datetime, reference: str | None = None) -> str:
        intentions = [
            intention
            for intention in self.store.list_intentions()
            if intention.status in {"active", "blocked"}
        ]
        if reference is not None:
            matched = self._resolve_reference(reference, intentions)
            if matched is None:
                return "which one do you mean?"
            intentions = [matched]
        refreshed = []
        refresh_failed = False
        for intention in intentions:
            checked, _ = self.checker.check_now_safely(intention.id)
            current = self.store.get_intention(intention.id)
            if checked and current is not None:
                refreshed.append(current)
            elif current is not None:
                refresh_failed = True
        if refresh_failed:
            if len(intentions) == 1:
                return "I couldn't refresh that right now. I'll try again."
            return "I couldn't refresh everything right now. I'll try again."
        return self.presenter.check_summary(refreshed, now)

    @staticmethod
    def _matches_reference(intention: Intention, reference: str) -> bool:
        needle = reference.casefold().removeprefix("the ").strip()
        if len(needle) < 2:
            return False
        searchable = [
            intention.objective,
            *(evidence.excerpt or "" for evidence in intention.context_evidence),
            *(source.value for source in intention.sources if source.kind == "url"),
        ]
        return any(needle in value.casefold() for value in searchable)

    def _resolve_reference(
        self,
        reference: str | None,
        intentions: list[Intention] | None = None,
    ) -> Intention | None:
        if reference is None:
            return None
        candidates = intentions if intentions is not None else self.store.list_intentions()
        matches = [
            intention
            for intention in candidates
            if self._matches_reference(intention, reference)
        ]
        return matches[0] if len(matches) == 1 else None

    def _act(self, now: datetime, intention_id: str | None = None) -> str:
        if intention_id is None:
            actionable = [
                intention
                for intention in self.store.list_intentions()
                if intention.next_action is not None
                and intention.next_action.mode == "agent"
                and intention.next_action.action_type == "repair_readme_setup"
                and (
                    intention.next_action.status == "proposed"
                    or (
                        intention.next_action.status == "completed"
                        and intention.next_action.post_check_pending
                    )
                )
            ]
            if len(actionable) > 1:
                completed_count = 0
                failed_count = 0
                for intention in actionable:
                    completed_before = self.store.count_events(
                        intention.id, "action_completed"
                    )
                    try:
                        self._act(now, intention.id)
                    except (OSError, PermissionError, ValueError):
                        failed_count += 1
                        continue
                    completed_after = self.store.count_events(
                        intention.id, "action_completed"
                    )
                    if completed_after > completed_before:
                        new_payloads = self.store.list_event_payloads(
                            intention.id, "action_completed"
                        )[completed_before:completed_after]
                        if any(payload.get("changed", True) for payload in new_payloads):
                            completed_count += 1
                if failed_count:
                    failed_item = "item" if failed_count == 1 else "items"
                    if completed_count:
                        project = "project" if completed_count == 1 else "projects"
                        return (
                            f"done. I fixed the README setup in {completed_count} {project}. "
                            f"I couldn't safely handle {failed_count} other {failed_item}."
                        )
                    return f"I couldn't safely handle {failed_count} {failed_item}."
                remaining = next(
                    (
                        item.most_important_unresolved_requirement
                        for item in self.store.list_intentions()
                        if item.requirement_capability == "user_must_handle"
                        and item.most_important_unresolved_requirement
                    ),
                    None,
                )
                suffix = (
                    self._remaining_user_work(remaining)
                    if remaining
                    else " everything else is covered."
                )
                project = "project" if completed_count == 1 else "projects"
                if completed_count == 0:
                    return suffix.strip()
                return (
                    f"done. I fixed the README setup in {completed_count} {project}."
                    f"{suffix}"
                )
        awaiting_check = next(
            (
                intention
                for intention in self.store.list_intentions()
                if (intention_id is None or intention.id == intention_id)
                if intention.next_action is not None
                and intention.next_action.status == "completed"
                and intention.next_action.post_check_pending
            ),
            None,
        )
        if awaiting_check is not None:
            checked, _ = self.checker.check_now_safely(awaiting_check.id)
            if not checked:
                return "I couldn't refresh things afterward. I'll try again."
            updated = self.store.get_intention(awaiting_check.id)
            if updated is None:
                raise RuntimeError(
                    f"Intention disappeared after CHECK: {awaiting_check.id}"
                )
            return self._remaining_user_work(
                updated.most_important_unresolved_requirement
            ).strip()

        proposed = next(
            (
                item
                for item in self.store.list_intentions()
                if intention_id is None or item.id == intention_id
                if item.next_action is not None
                and item.next_action.mode == "agent"
                and item.next_action.action_type == "repair_readme_setup"
                and item.next_action.status == "proposed"
            ),
            None,
        )
        if proposed is not None:
            proposed_action = proposed.next_action
            if proposed_action is None:
                raise RuntimeError("Proposed action disappeared before validation.")
            self.actions.inspect_repository(proposed_action.parameters["repository"])
            checked, _ = self.checker.check_now_safely(proposed.id)
            current = self.store.get_intention(proposed.id)
            if not checked or current is None:
                return "I couldn't refresh that right now. I'll try again."
            if (
                current.next_action is None
                or current.next_action.mode != "agent"
                or current.next_action.action_type != "repair_readme_setup"
                or current.next_action.status != "proposed"
            ):
                if current.status == "completed":
                    self.store.append_event(
                        current.id,
                        "action_completed",
                        {
                            "action": "repair_readme_setup",
                            "path": str(
                                Path(proposed_action.parameters["repository"])
                                / "README.md"
                            ),
                            "changed": False,
                        },
                        now,
                    )
                    return self._remaining_user_work(None).strip()
                return self.presenter.check_summary([current], now)
            execution_now = self.clock()
            if (
                current.deadline_at is not None
                and current.deadline_evidence
                and current.deadline_at <= execution_now
            ):
                checked, _ = self.checker.check_now_safely(current.id)
                latest = self.store.get_intention(current.id)
                if not checked or latest is None:
                    return "I couldn't refresh that right now. I'll try again."
                if (
                    latest.next_action is None
                    or latest.next_action.mode != "agent"
                    or latest.next_action.action_type != "repair_readme_setup"
                    or latest.next_action.status != "proposed"
                ):
                    return self.presenter.check_summary([latest], execution_now)
                current = latest
            if current.next_action.parameters != proposed_action.parameters:
                return "things changed, so I didn't do anything. ask me again if you want."

        claimed = self.store.claim_next_agent_action(
            "repair_readme_setup", now, intention_id=intention_id
        )
        if claimed is not None:
            action = claimed.next_action
            if action is None or action.execution_id is None:
                raise RuntimeError("Claimed action is missing execution state.")
            try:
                readme, repaired = self.actions.repair_readme_setup(
                    action.parameters["repository"]
                )
            except Exception as error:
                self.store.release_claimed_action(
                    claimed.id,
                    action.execution_id,
                    now,
                    type(error).__name__,
                )
                raise
            recovered = action.execution_attempts > 1
            event_payload: dict[str, Any] = {
                "action": "repair_readme_setup",
                "path": str(readme),
            }
            if not repaired:
                event_payload["changed"] = False
            if recovered:
                event_payload["recovered"] = True
            completed = self.store.complete_claimed_action(
                claimed.id,
                action.execution_id,
                now,
                event_payload,
            )
            if completed is None:
                raise RuntimeError("Action claim was lost before completion.")
            checked, _ = self.checker.check_now_safely(claimed.id)
            if not checked:
                if repaired:
                    return (
                        "done. README setup is fixed, but I couldn't refresh things "
                        "afterward. I'll try again."
                    )
                return "I couldn't refresh things afterward. I'll try again."
            updated = self.store.get_intention(claimed.id)
            if updated is None:
                raise RuntimeError(f"Intention disappeared after ACT: {claimed.id}")
            if repaired:
                remaining = self._remaining_user_work(
                    updated.most_important_unresolved_requirement
                )
                return f"done. README setup is fixed.{remaining}"
            return self._remaining_user_work(
                updated.most_important_unresolved_requirement
            ).strip()
        user_requirement = next(
            (
                intention.most_important_unresolved_requirement
                for intention in self.store.list_intentions()
                if intention_id is None or intention.id == intention_id
                if intention.requirement_capability == "user_must_handle"
            ),
            None,
        )
        if user_requirement:
            return self._remaining_user_work(user_requirement).strip()
        user_action = next(
            (
                intention.next_action
                for intention in self.store.list_intentions()
                if intention_id is None or intention.id == intention_id
                if intention.next_action is not None
                and intention.next_action.mode == "user"
                and intention.next_action.status in {"pending", "proposed"}
            ),
            None,
        )
        if user_action:
            return f"you still need to {user_action.description.casefold()}."
        return "nothing else I can handle."

    @staticmethod
    def _remaining_user_work(requirement: str | None) -> str:
        if requirement is None:
            return " all requirements are covered."
        labels = {
            "Demo video": "record the demo",
            "Public repository": "make the repository public",
        }
        work = labels.get(requirement, f"handle {requirement.casefold()}")
        return f" you still need to {work}."
