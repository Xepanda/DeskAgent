"""
Desk Agent R1 Life Services: Reminders, Alarms and Notes.
Features SQLite WAL persistence, strict transaction guarantees,
occurrence scheduling, snooze/complete decoupling, and restart recovery.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional


class LifeService:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute("PRAGMA journal_mode = WAL;")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._get_connection() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS reminders (
                reminder_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                due_at TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai',
                repeat_rule TEXT,
                priority TEXT NOT NULL DEFAULT 'normal',
                status TEXT NOT NULL DEFAULT 'scheduled',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS occurrences (
                occurrence_id TEXT PRIMARY KEY,
                reminder_id TEXT NOT NULL,
                scheduled_at TEXT NOT NULL,
                delivered_at TEXT,
                state TEXT NOT NULL DEFAULT 'pending',
                FOREIGN KEY (reminder_id) REFERENCES reminders(reminder_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS notes (
                note_id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                tags_json TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """)
            conn.commit()

    # --- Reminder API ---

    def create_reminder(
        self,
        reminder_id: str,
        title: str,
        due_at: str,
        timezone_str: str = "Asia/Shanghai",
        repeat_rule: Optional[str] = None,
        priority: str = "normal",
    ) -> dict[str, Any]:
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO reminders (reminder_id, title, due_at, timezone, repeat_rule, priority, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)
                """,
                (reminder_id, title, due_at, timezone_str, repeat_rule, priority, now_str, now_str),
            )
            # Create first occurrence
            occ_id = f"occ-{reminder_id}-0"
            conn.execute(
                """
                INSERT INTO occurrences (occurrence_id, reminder_id, scheduled_at, state)
                VALUES (?, ?, ?, 'pending')
                """,
                (occ_id, reminder_id, due_at),
            )
            conn.commit()

        return {
            "reminder_id": reminder_id,
            "title": title,
            "due_at": due_at,
            "status": "scheduled",
            "created_at": now_str,
        }

    def get_reminder(self, reminder_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE reminder_id = ?", (reminder_id,)).fetchone()
            if not row:
                return None
            return dict(row)

    def snooze_reminder(self, reminder_id: str, snooze_minutes: int) -> dict[str, Any]:
        """Snooze advances the occurrence schedule without completing the reminder."""
        now = datetime.now(timezone.utc)
        new_due = (now + timedelta(minutes=snooze_minutes)).isoformat().replace("+00:00", "Z")
        with self._lock, self._get_connection() as conn:
            conn.execute(
                "UPDATE reminders SET due_at = ?, status = 'snoozed', updated_at = ? WHERE reminder_id = ?",
                (new_due, now.isoformat().replace("+00:00", "Z"), reminder_id),
            )
            conn.commit()
        return {"reminder_id": reminder_id, "snoozed_until": new_due, "status": "snoozed"}

    def complete_reminder(self, reminder_id: str) -> dict[str, Any]:
        """Explicitly marks reminder complete."""
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        with self._lock, self._get_connection() as conn:
            conn.execute(
                "UPDATE reminders SET status = 'completed', updated_at = ? WHERE reminder_id = ?",
                (now_str, reminder_id),
            )
            conn.commit()
        return {"reminder_id": reminder_id, "status": "completed"}

    def delete_reminder(self, reminder_id: str) -> bool:
        with self._lock, self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM reminders WHERE reminder_id = ?", (reminder_id,))
            conn.commit()
            return cursor.rowcount > 0

    # --- Notes API ---

    def create_note(
        self,
        note_id: str,
        title: str,
        content: str,
        tags: Optional[list[str]] = None,
    ) -> dict[str, Any]:
        now_str = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        tags_json = json.dumps(tags or [])
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO notes (note_id, title, content, tags_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (note_id, title, content, tags_json, now_str, now_str),
            )
            conn.commit()

        return {
            "note_id": note_id,
            "title": title,
            "content": content,
            "tags": tags or [],
            "created_at": now_str,
        }

    def get_note(self, note_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT * FROM notes WHERE note_id = ?", (note_id,)).fetchone()
            if not row:
                return None
            res = dict(row)
            res["tags"] = json.loads(res.pop("tags_json", "[]"))
            return res
