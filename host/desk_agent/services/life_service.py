"""
Desk Agent R1 Life Services: Reminders, Alarms, Notes, and Notification Outbox.

Features:
- SQLite WAL mode with atomic transaction guarantees (no lost updates upon power failure).
- Occurrence scheduling and due occurrence evaluation for proactive triggers.
- Snooze / Stop Ringing / Complete decoupling (stopping ringing never completes the task).
- Full CRUD for Reminders and Notes with tag and text filtering.
- Persistent Outbox table for reliable offline event dispatching.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


class LifeService:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON;")
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL;")
        return self._conn

    def close(self) -> None:
        """Closes the underlying SQLite database connection."""
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

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

            CREATE TABLE IF NOT EXISTS outbox (
                outbox_id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                retry_count INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                dispatched_at TEXT
            );
            """)
            conn.commit()

    @staticmethod
    def _now_utc_str() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    # =========================================================================
    # Reminder Management (CRUD)
    # =========================================================================

    def create_reminder(
        self,
        reminder_id: str,
        title: str,
        due_at: str,
        timezone_str: str = "Asia/Shanghai",
        repeat_rule: Optional[str] = None,
        priority: str = "normal",
    ) -> dict[str, Any]:
        now_str = self._now_utc_str()
        occ_id = f"occ-{reminder_id}-0"
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO reminders (reminder_id, title, due_at, timezone, repeat_rule, priority, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'scheduled', ?, ?)
                """,
                (reminder_id, title, due_at, timezone_str, repeat_rule, priority, now_str, now_str),
            )
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

    def list_reminders(self, status: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            if status:
                rows = conn.execute("SELECT * FROM reminders WHERE status = ? ORDER BY due_at ASC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM reminders ORDER BY due_at ASC").fetchall()
            return [dict(r) for r in rows]

    def update_reminder(
        self,
        reminder_id: str,
        title: Optional[str] = None,
        due_at: Optional[str] = None,
        repeat_rule: Optional[str] = None,
    ) -> dict[str, Any]:
        now_str = self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            current = conn.execute("SELECT * FROM reminders WHERE reminder_id = ?", (reminder_id,)).fetchone()
            if not current:
                raise KeyError(f"Reminder '{reminder_id}' not found")

            new_title = title if title is not None else current["title"]
            new_due = due_at if due_at is not None else current["due_at"]
            new_rule = repeat_rule if repeat_rule is not None else current["repeat_rule"]

            conn.execute(
                """
                UPDATE reminders
                SET title = ?, due_at = ?, repeat_rule = ?, updated_at = ?
                WHERE reminder_id = ?
                """,
                (new_title, new_due, new_rule, now_str, reminder_id),
            )

            # If due_at changed, update the active pending occurrence schedule
            if due_at is not None and due_at != current["due_at"]:
                conn.execute(
                    """
                    UPDATE occurrences
                    SET scheduled_at = ?
                    WHERE reminder_id = ? AND state = 'pending'
                    """,
                    (new_due, reminder_id),
                )

            conn.commit()

        return {
            "reminder_id": reminder_id,
            "title": new_title,
            "due_at": new_due,
            "repeat_rule": new_rule,
            "updated_at": now_str,
        }

    def delete_reminder(self, reminder_id: str) -> bool:
        with self._lock, self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM reminders WHERE reminder_id = ?", (reminder_id,))
            conn.commit()
            return cursor.rowcount > 0

    # =========================================================================
    # Occurrence Scheduling & Ringing Control (Decoupled)
    # =========================================================================

    def get_due_occurrences(self, now_iso: Optional[str] = None) -> list[dict[str, Any]]:
        threshold = now_iso or self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                """
                SELECT o.occurrence_id, o.reminder_id, o.scheduled_at, o.state,
                       r.title, r.priority, r.repeat_rule, r.timezone
                FROM occurrences o
                JOIN reminders r ON o.reminder_id = r.reminder_id
                WHERE o.state = 'pending' AND o.scheduled_at <= ?
                ORDER BY o.scheduled_at ASC
                """,
                (threshold,),
            ).fetchall()
            return [dict(r) for r in rows]

    def mark_occurrence_delivered(self, occurrence_id: str) -> None:
        now_str = self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            conn.execute(
                "UPDATE occurrences SET state = 'delivered', delivered_at = ? WHERE occurrence_id = ?",
                (now_str, occurrence_id),
            )
            conn.commit()

    def mark_occurrence_ringing(self, occurrence_id: str) -> None:
        """Transitions occurrence state from pending to ringing and updates reminder status."""
        now_str = self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT reminder_id FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
            if not row:
                raise KeyError(f"Occurrence '{occurrence_id}' not found")
            reminder_id = row["reminder_id"]
            conn.execute(
                "UPDATE occurrences SET state = 'ringing' WHERE occurrence_id = ?",
                (occurrence_id,),
            )
            conn.execute(
                "UPDATE reminders SET status = 'ringing', updated_at = ? WHERE reminder_id = ?",
                (now_str, reminder_id),
            )
            conn.commit()

    def stop_ringing(self, occurrence_id: str) -> dict[str, Any]:
        """
        User dismisses the active alarm/ringing.
        IMPORTANT: This strictly marks ringing stopped and does NOT mark the reminder completed!
        """
        now_str = self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT reminder_id FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)).fetchone()
            if not row:
                raise KeyError(f"Occurrence '{occurrence_id}' not found")

            reminder_id = row["reminder_id"]
            conn.execute(
                "UPDATE occurrences SET state = 'dismissed', delivered_at = ? WHERE occurrence_id = ?",
                (now_str, occurrence_id),
            )
            conn.execute(
                "UPDATE reminders SET status = 'ringing_stopped', updated_at = ? WHERE reminder_id = ?",
                (now_str, reminder_id),
            )
            conn.commit()

        return {"reminder_id": reminder_id, "occurrence_id": occurrence_id, "status": "ringing_stopped"}

    def snooze_reminder(self, reminder_id: str, snooze_minutes: int = 10, occurrence_id: Optional[str] = None) -> dict[str, Any]:
        """
        Snoozes a reminder: advances due_at and schedules a new pending occurrence.
        Does NOT mark the task completed.
        """
        now = datetime.now(timezone.utc)
        now_str = now.isoformat().replace("+00:00", "Z")
        new_due = (now + timedelta(minutes=snooze_minutes)).isoformat().replace("+00:00", "Z")
        new_occ_id = f"occ-{reminder_id}-{int(now.timestamp())}"

        with self._lock, self._get_connection() as conn:
            # Mark current occurrence snoozed if supplied
            if occurrence_id:
                conn.execute("UPDATE occurrences SET state = 'snoozed' WHERE occurrence_id = ?", (occurrence_id,))
            else:
                conn.execute("UPDATE occurrences SET state = 'snoozed' WHERE reminder_id = ? AND state = 'pending'", (reminder_id,))

            # Create new occurrence
            conn.execute(
                "INSERT INTO occurrences (occurrence_id, reminder_id, scheduled_at, state) VALUES (?, ?, ?, 'pending')",
                (new_occ_id, reminder_id, new_due),
            )
            conn.execute(
                "UPDATE reminders SET due_at = ?, status = 'snoozed', updated_at = ? WHERE reminder_id = ?",
                (new_due, now_str, reminder_id),
            )
            conn.commit()

        return {
            "reminder_id": reminder_id,
            "occurrence_id": new_occ_id,
            "snoozed_until": new_due,
            "status": "snoozed",
        }

    def complete_reminder(self, reminder_id: str, occurrence_id: Optional[str] = None) -> dict[str, Any]:
        """Explicitly marks reminder complete."""
        now_str = self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            conn.execute(
                "UPDATE reminders SET status = 'completed', updated_at = ? WHERE reminder_id = ?",
                (now_str, reminder_id),
            )
            conn.execute(
                "UPDATE occurrences SET state = 'completed' WHERE reminder_id = ? AND state = 'pending'",
                (reminder_id,),
            )
            conn.commit()
        return {"reminder_id": reminder_id, "status": "completed"}

    # =========================================================================
    # Outbox Notifications (Reliable Proactive Delivery)
    # =========================================================================

    def enqueue_outbox(self, event_type: str, payload: Mapping[str, Any]) -> str:
        outbox_id = f"outbox-{uuid.uuid4().hex[:12]}"
        now_str = self._now_utc_str()
        payload_str = json.dumps(payload, ensure_ascii=False)

        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO outbox (outbox_id, event_type, payload_json, state, retry_count, created_at)
                VALUES (?, ?, ?, 'pending', 0, ?)
                """,
                (outbox_id, event_type, payload_str, now_str),
            )
            conn.commit()

        return outbox_id

    def get_pending_outbox(self) -> list[dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE state = 'pending' ORDER BY created_at ASC"
            ).fetchall()
            result = []
            for r in rows:
                item = dict(r)
                item["payload"] = json.loads(item.pop("payload_json", "{}"))
                result.append(item)
            return result

    def mark_outbox_dispatched(self, outbox_id: str) -> None:
        now_str = self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            conn.execute(
                "UPDATE outbox SET state = 'dispatched', dispatched_at = ? WHERE outbox_id = ?",
                (now_str, outbox_id),
            )
            conn.commit()

    def record_outbox_failure(self, outbox_id: str, error_message: str, max_retries: int = 5) -> None:
        """Records an outbox delivery failure, increments retry count, and marks failed if max retries exceeded."""
        with self._lock, self._get_connection() as conn:
            row = conn.execute("SELECT retry_count FROM outbox WHERE outbox_id = ?", (outbox_id,)).fetchone()
            if not row:
                return
            new_retries = row["retry_count"] + 1
            new_state = "failed" if new_retries >= max_retries else "pending"
            conn.execute(
                "UPDATE outbox SET retry_count = ?, state = ? WHERE outbox_id = ?",
                (new_retries, new_state, outbox_id),
            )
            conn.commit()

    def queue_outbox_message(self, topic: str, payload: Mapping[str, Any]) -> str:
        """Alias for enqueue_outbox with topic-oriented signature."""
        return self.enqueue_outbox(event_type=topic, payload=payload)

    def get_pending_outbox_messages(self, limit: int = 100) -> list[dict[str, Any]]:
        """Returns pending outbox entries with 'topic' field populated."""
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE state = 'pending' ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
            result = []
            for r in rows:
                item = dict(r)
                item["topic"] = item["event_type"]
                item["payload"] = json.loads(item.pop("payload_json", "{}"))
                result.append(item)
            return result

    # =========================================================================
    # Notes API
    # =========================================================================

    def create_note(
        self,
        note_id: str,
        title: str,
        content: str,
        tags: Optional[Sequence[str]] = None,
    ) -> dict[str, Any]:
        now_str = self._now_utc_str()
        tags_json = json.dumps(list(tags) if tags else [])
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
            "tags": list(tags) if tags else [],
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

    def list_notes(self, query: Optional[str] = None, tag: Optional[str] = None) -> list[dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM notes ORDER BY updated_at DESC").fetchall()
            results = []
            for r in rows:
                item = dict(r)
                item["tags"] = json.loads(item.pop("tags_json", "[]"))

                # Filter by tag
                if tag and tag not in item["tags"]:
                    continue

                # Filter by query (title or content substring)
                if query:
                    q = query.lower()
                    if q not in item["title"].lower() and q not in item["content"].lower():
                        continue

                results.append(item)
            return results

    def delete_note(self, note_id: str) -> bool:
        with self._lock, self._get_connection() as conn:
            cursor = conn.execute("DELETE FROM notes WHERE note_id = ?", (note_id,))
            conn.commit()
            return cursor.rowcount > 0
