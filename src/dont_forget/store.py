from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from .models import Intention


class SQLiteStore:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
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

    def save_intention(self, intention: Intention) -> None:
        with self._lock:
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
                    intention.deadline_at.isoformat(),
                    intention.next_check_at.isoformat() if intention.next_check_at else None,
                    intention.updated_at.isoformat(),
                    intention.model_dump_json(),
                ),
            )
            self._connection.commit()

    def append_event(
        self,
        intention_id: str,
        event_type: str,
        payload: dict[str, Any],
        created_at: datetime,
    ) -> None:
        with self._lock:
            self._connection.execute(
                """
                INSERT INTO intention_events(id, intention_id, type, payload, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(uuid4()), intention_id, event_type, json.dumps(payload), created_at.isoformat()),
            )
            self._connection.commit()

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
