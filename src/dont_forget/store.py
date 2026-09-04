from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import Intention


class ConcurrentUpdateError(RuntimeError):
    pass


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS intentions (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                deadline_at TEXT NOT NULL,
                next_check_at TEXT,
                updated_at TEXT NOT NULL,
                snapshot TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_intentions_due
                ON intentions(status, next_check_at);
            CREATE TABLE IF NOT EXISTS intention_events (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                id TEXT NOT NULL UNIQUE,
                intention_id TEXT NOT NULL,
                type TEXT NOT NULL,
                payload TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY(intention_id) REFERENCES intentions(id)
            );
            """
        )
        self._connection.commit()

    def save_intention(
        self, intention: Intention, *, expected_version: int | None = None
    ) -> None:
        self.save_intention_with_event(
            intention, expected_version=expected_version
        )

    def save_intention_with_event(
        self,
        intention: Intention,
        *,
        expected_version: int | None = None,
        event_type: str | None = None,
        event_payload: dict[str, Any] | None = None,
        event_created_at: datetime | None = None,
    ) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                if expected_version is not None:
                    row = self._connection.execute(
                        "SELECT snapshot FROM intentions WHERE id = ?", (intention.id,)
                    ).fetchone()
                    current_version = (
                        Intention.model_validate_json(row["snapshot"]).version
                        if row
                        else None
                    )
                    if current_version != expected_version:
                        raise ConcurrentUpdateError(
                            f"Intention {intention.id} changed concurrently."
                        )
                self._save_intention_row(intention)
                if event_type is not None:
                    if event_payload is None or event_created_at is None:
                        raise ValueError("Event payload and timestamp are required.")
                    self._append_event_row(
                        intention.id, event_type, event_payload, event_created_at
                    )
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _save_intention_row(self, intention: Intention) -> None:
        self._connection.execute(
            """
            INSERT INTO intentions(id, status, deadline_at, next_check_at, updated_at, snapshot)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status=excluded.status,
                deadline_at=excluded.deadline_at,
                next_check_at=excluded.next_check_at,
                updated_at=excluded.updated_at,
                snapshot=excluded.snapshot
            """,
            (
                intention.id,
                intention.status,
                intention.deadline_at.isoformat() if intention.deadline_at else "",
                intention.next_check_at.isoformat() if intention.next_check_at else None,
                intention.updated_at.isoformat(),
                intention.model_dump_json(),
            ),
        )

    def claim_next_agent_action(
        self,
        action_type: str,
        now: datetime,
        *,
        stale_after: timedelta = timedelta(minutes=5),
    ) -> Intention | None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                rows = self._connection.execute(
                    "SELECT snapshot FROM intentions ORDER BY updated_at"
                ).fetchall()
                for row in rows:
                    intention = Intention.model_validate_json(row["snapshot"])
                    action = intention.next_action
                    stale_execution = bool(
                        action
                        and action.status == "executing"
                        and action.execution_started_at
                        and action.execution_started_at <= now - stale_after
                    )
                    if (
                        not action
                        or action.mode != "agent"
                        or action.action_type != action_type
                        or (action.status != "proposed" and not stale_execution)
                    ):
                        continue
                    action.status = "executing"
                    action.execution_id = str(uuid4())
                    action.execution_started_at = now
                    action.execution_attempts += 1
                    intention.updated_at = now
                    intention.version += 1
                    self._save_intention_row(intention)
                    self._connection.commit()
                    return intention
                self._connection.commit()
                return None
            except Exception:
                self._connection.rollback()
                raise

    def complete_claimed_action(
        self,
        intention_id: str,
        execution_id: str,
        now: datetime,
        event_payload: dict[str, Any],
    ) -> Intention | None:
        if (
            not isinstance(event_payload, dict)
            or not isinstance(event_payload.get("action"), str)
            or not event_payload["action"]
            or not isinstance(event_payload.get("path"), str)
            or not event_payload["path"]
        ):
            raise ValueError(
                "A dictionary completion event payload with action and path is required."
            )
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT snapshot FROM intentions WHERE id = ?", (intention_id,)
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return None
                intention = Intention.model_validate_json(row["snapshot"])
                action = intention.next_action
                if (
                    action is None
                    or action.status != "executing"
                    or action.execution_id != execution_id
                ):
                    self._connection.commit()
                    return None
                action.status = "completed"
                action.post_check_pending = True
                intention.updated_at = now
                intention.version += 1
                self._save_intention_row(intention)
                self._append_event_row(
                    intention_id, "action_completed", event_payload, now
                )
                self._connection.commit()
                return intention
            except Exception:
                self._connection.rollback()
                raise

    def release_claimed_action(
        self,
        intention_id: str,
        execution_id: str,
        now: datetime,
        error_type: str,
    ) -> bool:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                row = self._connection.execute(
                    "SELECT snapshot FROM intentions WHERE id = ?", (intention_id,)
                ).fetchone()
                if row is None:
                    self._connection.commit()
                    return False
                intention = Intention.model_validate_json(row["snapshot"])
                action = intention.next_action
                if (
                    action is None
                    or action.status != "executing"
                    or action.execution_id != execution_id
                ):
                    self._connection.commit()
                    return False
                action.status = "proposed"
                action.execution_id = None
                action.execution_started_at = None
                intention.current_state = "ACT failed safely; the proposed action remains pending."
                intention.updated_at = now
                intention.version += 1
                self._save_intention_row(intention)
                self._connection.execute(
                    """
                    INSERT INTO intention_events(id, intention_id, type, payload, created_at)
                    VALUES (?, ?, 'action_failed', ?, ?)
                    """,
                    (
                        str(uuid4()),
                        intention_id,
                        json.dumps({"error_type": error_type}),
                        now.isoformat(),
                    ),
                )
                self._connection.commit()
                return True
            except Exception:
                self._connection.rollback()
                raise

    def append_event(
        self,
        intention_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                self._append_event_row(intention_id, event_type, payload, created_at)
                self._connection.commit()
            except Exception:
                self._connection.rollback()
                raise

    def _append_event_row(
        self,
        intention_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO intention_events(id, intention_id, type, payload, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                str(uuid4()),
                intention_id,
                event_type,
                json.dumps(payload),
                created_at.isoformat(),
            ),
        )

    def get_intention(self, intention_id: str) -> Intention | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT snapshot FROM intentions WHERE id = ?", (intention_id,)
            ).fetchone()
        return Intention.model_validate_json(row["snapshot"]) if row else None

    def list_intentions(self) -> list[Intention]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT snapshot FROM intentions ORDER BY updated_at"
            ).fetchall()
        return [Intention.model_validate_json(row["snapshot"]) for row in rows]

    def list_due(self, now: datetime) -> list[Intention]:
        return [
            intention
            for intention in self.list_intentions()
            if intention.status == "active"
            and intention.next_check_at is not None
            and intention.next_check_at <= now
        ]

    def count_events(self, intention_id: str, event_type: str) -> int:
        with self._lock:
            row = self._connection.execute(
                "SELECT COUNT(*) AS count FROM intention_events WHERE intention_id = ? AND type = ?",
                (intention_id, event_type),
            ).fetchone()
        return int(row["count"])

    def list_event_payloads(
        self, intention_id: str, event_type: str
    ) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT payload FROM intention_events
                WHERE intention_id = ? AND type = ?
                ORDER BY sequence
                """,
                (intention_id, event_type),
            ).fetchall()
        return [json.loads(row["payload"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()
