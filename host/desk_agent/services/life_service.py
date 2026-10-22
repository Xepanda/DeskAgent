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
import hashlib
import sqlite3
import threading
import uuid
import time
import zoneinfo
from datetime import datetime, timezone, timedelta, tzinfo
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence


class _FallbackEasternTz(tzinfo):
    """Fallback for America/New_York when system tzdata is unavailable (e.g. bare Windows)."""

    def utcoffset(self, dt: Optional[datetime]) -> timedelta:
        if dt is None:
            return timedelta(hours=-5)
        # US Eastern DST: 2nd Sunday in March to 1st Sunday in November at 02:00 local standard time
        y = dt.year
        m1_w = datetime(y, 3, 1).weekday()
        dst_start = datetime(y, 3, 1 + (6 - m1_w) % 7 + 7, 2, 0)
        n1_w = datetime(y, 11, 1).weekday()
        dst_end = datetime(y, 11, 1 + (6 - n1_w) % 7, 2, 0)
        naive = dt.replace(tzinfo=None)
        # 02:00-03:00 on the spring transition date is a nonexistent local
        # interval. Match ZoneInfo's default (fold=0) interpretation as EST.
        if naive.date() == dst_start.date() and naive.hour == 2:
            return timedelta(hours=-5)
        # On the fall transition date, 01:00-02:00 occurs twice. fold=0 is
        # the daylight occurrence and fold=1 is standard time.
        if naive.date() == dst_end.date() and naive.hour == 1:
            return timedelta(hours=-5 if dt.fold else -4)
        if dst_start + timedelta(hours=1) <= naive < dst_end:
            return timedelta(hours=-4)
        return timedelta(hours=-5)

    def dst(self, dt: Optional[datetime]) -> timedelta:
        if self.utcoffset(dt) == timedelta(hours=-4):
            return timedelta(hours=1)
        return timedelta(0)

    def tzname(self, dt: Optional[datetime]) -> str:
        return "EDT" if self.dst(dt) else "EST"

    def fromutc(self, dt: datetime) -> datetime:
        """Convert UTC to Eastern time while preserving the autumn fold bit."""
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        year = dt.year
        march_first_weekday = datetime(year, 3, 1).weekday()
        dst_start_day = 1 + (6 - march_first_weekday) % 7 + 7
        november_first_weekday = datetime(year, 11, 1).weekday()
        dst_end_day = 1 + (6 - november_first_weekday) % 7
        start_utc = datetime(year, 3, dst_start_day, 7)
        end_utc = datetime(year, 11, dst_end_day, 6)
        utc_naive = dt.replace(tzinfo=None)
        is_dst = start_utc <= utc_naive < end_utc
        local_naive = utc_naive + timedelta(hours=-4 if is_dst else -5)
        fold = int(end_utc <= utc_naive < end_utc + timedelta(hours=1))
        return local_naive.replace(tzinfo=self, fold=fold)


def _resolve_timezone(tz_name: Optional[str]) -> timezone | zoneinfo.ZoneInfo:
    if not tz_name:
        return timezone.utc
    cleaned = tz_name.strip()
    try:
        return zoneinfo.ZoneInfo(cleaned)
    except Exception:
        lowered = cleaned.lower()
        if lowered in ("america/new_york", "us/eastern", "est5edt"):
            return _FallbackEasternTz()
        elif lowered in ("asia/shanghai", "asia/chongqing", "prc"):
            return timezone(timedelta(hours=8), "CST")
        elif lowered in ("utc", "gmt"):
            return timezone.utc
        return timezone.utc


def _normalize_utc_iso(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def compute_next_occurrence(
    current_iso: str,
    repeat_rule: str,
    tz_name: Optional[str] = None,
    timezone_str: Optional[str] = None,
) -> Optional[str]:
    """Computes the next ISO 8601 UTC timestamp based on repeat rule and timezone."""
    if not repeat_rule:
        return None

    try:
        dt = datetime.fromisoformat(current_iso.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None

    target_tz_str = tz_name or timezone_str
    target_tz = _resolve_timezone(target_tz_str)

    local_dt = dt.astimezone(target_tz)
    rule = repeat_rule.strip().lower()

    # Hourly and interval schedules describe elapsed time. Adding a timedelta
    # directly to a ZoneInfo local datetime can skip/repeat an hour at DST
    # transitions, so advance these rules on the UTC timeline instead.
    elapsed_minutes: Optional[int] = None
    if rule in ("hourly", "freq=hourly"):
        elapsed_minutes = 60
    elif rule.startswith("interval:"):
        try:
            elapsed_minutes = int(rule.split(":", 1)[1])
        except ValueError:
            return None
    elif "interval=" in rule and "minutely" in rule:
        try:
            elapsed_minutes = int(
                next(part for part in rule.split(";") if part.startswith("interval=")).split("=", 1)[1]
            )
        except (StopIteration, ValueError):
            return None
    if elapsed_minutes is not None:
        if elapsed_minutes <= 0:
            return None
        next_utc = dt.astimezone(timezone.utc) + timedelta(minutes=elapsed_minutes)
        return next_utc.isoformat().replace("+00:00", "Z")

    if rule in ("daily", "freq=daily"):
        next_dt = local_dt + timedelta(days=1)
    elif rule in ("weekly", "freq=weekly"):
        next_dt = local_dt + timedelta(days=7)
    elif rule == "weekdays":
        weekday = local_dt.weekday()
        if weekday < 4:  # Mon-Thu -> next day
            next_dt = local_dt + timedelta(days=1)
        elif weekday == 4:  # Friday -> Monday (+3 days)
            next_dt = local_dt + timedelta(days=3)
        elif weekday == 5:  # Saturday -> Monday (+2 days)
            next_dt = local_dt + timedelta(days=2)
        else:  # Sunday -> Monday (+1 day)
            next_dt = local_dt + timedelta(days=1)
    else:
        return None

    return next_dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _next_occurrence_after(
    current_iso: str, repeat_rule: str, threshold_iso: str, tz_name: Optional[str]
) -> Optional[str]:
    """Advance a recurring schedule past downtime without replaying every missed slot."""
    try:
        current = datetime.fromisoformat(current_iso.replace("Z", "+00:00"))
        threshold = datetime.fromisoformat(threshold_iso.replace("Z", "+00:00"))
        if current.tzinfo is None:
            current = current.replace(tzinfo=timezone.utc)
        if threshold.tzinfo is None:
            threshold = threshold.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None

    rule = repeat_rule.strip().lower()
    step_minutes: Optional[int] = None
    if rule == "hourly" or rule == "freq=hourly":
        step_minutes = 60
    elif rule.startswith("interval:"):
        try:
            step_minutes = int(rule.split(":", 1)[1])
        except ValueError:
            return None
    elif "interval=" in rule and "minutely" in rule:
        try:
            step_minutes = int(next(p for p in rule.split(";") if p.startswith("interval=")).split("=", 1)[1])
        except (StopIteration, ValueError):
            return None

    if step_minutes is not None:
        if step_minutes <= 0:
            return None
        start_utc = current.astimezone(timezone.utc)
        threshold_utc = threshold.astimezone(timezone.utc)
        elapsed_steps = max(1, int((threshold_utc - start_utc).total_seconds() // (step_minutes * 60)) + 1)
        next_utc = start_utc + timedelta(minutes=step_minutes * elapsed_steps)
        return next_utc.isoformat().replace("+00:00", "Z")

    next_iso = current_iso
    for _ in range(100_000):
        next_iso = compute_next_occurrence(next_iso, repeat_rule, tz_name=tz_name)
        if not next_iso:
            return None
        next_dt = datetime.fromisoformat(next_iso.replace("Z", "+00:00"))
        if next_dt > threshold.astimezone(timezone.utc):
            return next_iso
    return None


class LifeService:
    def __init__(self, db_path: str | Path = ":memory:") -> None:
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def __enter__(self) -> LifeService:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()

    def __del__(self) -> None:
        """Release the SQLite handle when callers drop the service without close()."""
        try:
            self.close()
        except Exception:
            # Destructors must not raise during interpreter shutdown.
            pass

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
                try:
                    self._conn.close()
                finally:
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
                dispatched_at TEXT,
                next_retry_at TEXT
            );
            """)
            try:
                conn.execute("ALTER TABLE outbox ADD COLUMN next_retry_at TEXT;")
            except sqlite3.OperationalError:
                pass
            # Recover only alerts whose occurrence is still active; confirmations
            # committed before a crash must never be replayed after restart.
            dispatching = conn.execute(
                "SELECT outbox_id, payload_json FROM outbox "
                "WHERE event_type = 'reminder.alert' AND state = 'dispatching'"
            ).fetchall()
            for row in dispatching:
                try:
                    occurrence_id = json.loads(row["payload_json"]).get("occurrence_id")
                except (TypeError, json.JSONDecodeError):
                    occurrence_id = None
                occurrence = conn.execute(
                    "SELECT state FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)
                ).fetchone() if occurrence_id else None
                recovered_state = "pending" if occurrence and occurrence["state"] == "ringing" else "cancelled"
                conn.execute(
                    "UPDATE outbox SET state = ? WHERE outbox_id = ?",
                    (recovered_state, row["outbox_id"]),
                )
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
        due_at = _normalize_utc_iso(due_at)
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
            new_due = _normalize_utc_iso(due_at) if due_at is not None else current["due_at"]
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

    def get_due_occurrences(self, now_iso: Optional[str] = None, max_catchup_hours: Optional[int] = 24) -> list[dict[str, Any]]:
        """Return due reminders, expiring downtime misses older than 24 hours by default.

        Pass ``None`` as ``max_catchup_hours`` to disable expiry for callers that
        explicitly want every missed occurrence returned.
        """
        try:
            threshold = _normalize_utc_iso(now_iso) if now_iso else self._now_utc_str()
            now_dt = datetime.fromisoformat(threshold.replace("Z", "+00:00"))
        except Exception:
            threshold = self._now_utc_str()
            now_dt = datetime.fromisoformat(threshold.replace("Z", "+00:00"))

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

            due_list = []
            for r in rows:
                item = dict(r)
                try:
                    sched_dt = datetime.fromisoformat(item["scheduled_at"].replace("Z", "+00:00"))
                    delay_sec = max(0.0, (now_dt - sched_dt).total_seconds())
                except Exception:
                    delay_sec = 0.0

                # Check if expired beyond maximum catchup threshold if specified
                if max_catchup_hours is not None and delay_sec > max_catchup_hours * 3600:
                    conn.execute(
                        "UPDATE occurrences SET state = 'expired' WHERE occurrence_id = ?",
                        (item["occurrence_id"],),
                    )
                    if item["repeat_rule"]:
                        next_due = _next_occurrence_after(
                            item["scheduled_at"], item["repeat_rule"], threshold, item["timezone"]
                        )
                        if next_due:
                            try:
                                next_ts = int(datetime.fromisoformat(next_due.replace("Z", "+00:00")).timestamp())
                            except ValueError:
                                next_ts = int(time.time())
                            next_occ_id = f"occ-{item['reminder_id']}-{next_ts}"
                            conn.execute(
                                "INSERT OR IGNORE INTO occurrences (occurrence_id, reminder_id, scheduled_at, state) VALUES (?, ?, ?, 'pending')",
                                (next_occ_id, item["reminder_id"], next_due),
                            )
                            conn.execute(
                                "UPDATE reminders SET due_at = ?, status = 'scheduled', updated_at = ? WHERE reminder_id = ?",
                                (next_due, self._now_utc_str(), item["reminder_id"]),
                            )
                    else:
                        conn.execute(
                            "UPDATE reminders SET status = 'missed', updated_at = ? WHERE reminder_id = ?",
                            (self._now_utc_str(), item["reminder_id"]),
                        )
                    continue

                item["is_catchup"] = delay_sec > 120.0  # More than 2 minutes delayed = catchup
                item["delay_seconds"] = delay_sec
                due_list.append(item)

            conn.commit()
            return due_list

    def get_all_occurrences(self, reminder_id: str) -> list[dict[str, Any]]:
        """Returns all occurrences for a reminder ordered by scheduled_at."""
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM occurrences WHERE reminder_id = ? ORDER BY scheduled_at ASC",
                (reminder_id,),
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
        """Transitions occurrence state from pending to ringing and updates reminder status.
        If the reminder has a repeat rule, automatically generates the next pending occurrence.
        """
        now_str = self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT o.occurrence_id, o.reminder_id, o.scheduled_at,
                       r.repeat_rule, r.timezone, r.status as rem_status
                FROM occurrences o
                JOIN reminders r ON o.reminder_id = r.reminder_id
                WHERE o.occurrence_id = ?
                """,
                (occurrence_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Occurrence '{occurrence_id}' not found")

            reminder_id = row["reminder_id"]
            scheduled_at = row["scheduled_at"]
            repeat_rule = row["repeat_rule"]
            rem_tz = row["timezone"] if "timezone" in row.keys() else "Asia/Shanghai"

            conn.execute(
                "UPDATE occurrences SET state = 'ringing' WHERE occurrence_id = ?",
                (occurrence_id,),
            )
            conn.execute(
                "UPDATE reminders SET status = 'ringing', updated_at = ? WHERE reminder_id = ?",
                (now_str, reminder_id),
            )

            # Auto-schedule next occurrence for repeating reminder if not already scheduled
            if repeat_rule and row["rem_status"] != "cancelled":
                pending_count = conn.execute(
                    "SELECT COUNT(*) FROM occurrences WHERE reminder_id = ? AND state = 'pending'",
                    (reminder_id,),
                ).fetchone()[0]

                if pending_count == 0:
                    next_due = compute_next_occurrence(scheduled_at, repeat_rule, tz_name=rem_tz)
                    if next_due:
                        try:
                            next_ts = int(datetime.fromisoformat(next_due.replace("Z", "+00:00")).timestamp())
                        except Exception:
                            next_ts = int(time.time())
                        next_occ_id = f"occ-{reminder_id}-{next_ts}"
                        conn.execute(
                            """
                            INSERT INTO occurrences (occurrence_id, reminder_id, scheduled_at, state)
                            VALUES (?, ?, ?, 'pending')
                            """,
                            (next_occ_id, reminder_id, next_due),
                        )
                        conn.execute(
                            "UPDATE reminders SET due_at = ?, updated_at = ? WHERE reminder_id = ?",
                            (next_due, now_str, reminder_id),
                        )

            conn.commit()

    def trigger_occurrence_to_outbox(
        self,
        occurrence_id: str,
        topic: str = "reminder.alert",
        is_catchup: bool = False,
    ) -> Optional[dict[str, Any]]:
        """
        Transitions occurrence state to ringing, updates reminder status,
        computes and schedules next pending occurrence if recurring, and creates outbox record.
        All executed within a SINGLE SQLite transaction committed once.
        """
        now_str = self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT o.occurrence_id, o.reminder_id, o.scheduled_at, o.state as occ_state,
                       r.title, r.repeat_rule, r.timezone, r.status as rem_status
                FROM occurrences o
                JOIN reminders r ON o.reminder_id = r.reminder_id
                WHERE o.occurrence_id = ?
                """,
                (occurrence_id,),
            ).fetchone()
            if not row:
                return None

            # Scheduler retries may revisit an occurrence after the first
            # transaction committed. Only pending occurrences may produce a
            # new alert; otherwise a retry would enqueue duplicate playback.
            if row["occ_state"] != "pending":
                return None

            reminder_id = row["reminder_id"]
            scheduled_at = row["scheduled_at"]
            repeat_rule = row["repeat_rule"]
            title = row["title"]
            rem_tz = row["timezone"] if "timezone" in row.keys() else "Asia/Shanghai"

            conn.execute(
                "UPDATE occurrences SET state = 'ringing' WHERE occurrence_id = ?",
                (occurrence_id,),
            )
            conn.execute(
                "UPDATE reminders SET status = 'ringing', updated_at = ? WHERE reminder_id = ?",
                (now_str, reminder_id),
            )

            # Auto-schedule next occurrence for repeating reminder if not already scheduled
            if repeat_rule and row["rem_status"] != "cancelled":
                pending_count = conn.execute(
                    "SELECT COUNT(*) FROM occurrences WHERE reminder_id = ? AND state = 'pending'",
                    (reminder_id,),
                ).fetchone()[0]

                if pending_count == 0:
                    next_due = (
                        _next_occurrence_after(scheduled_at, repeat_rule, now_str, rem_tz)
                        if is_catchup
                        else compute_next_occurrence(scheduled_at, repeat_rule, tz_name=rem_tz)
                    )
                    if next_due:
                        try:
                            next_ts = int(datetime.fromisoformat(next_due.replace("Z", "+00:00")).timestamp())
                        except Exception:
                            next_ts = int(time.time())
                        next_occ_id = f"occ-{reminder_id}-{next_ts}"
                        conn.execute(
                            """
                            INSERT INTO occurrences (occurrence_id, reminder_id, scheduled_at, state)
                            VALUES (?, ?, ?, 'pending')
                            """,
                            (next_occ_id, reminder_id, next_due),
                        )
                        conn.execute(
                            "UPDATE reminders SET due_at = ?, updated_at = ? WHERE reminder_id = ?",
                            (next_due, now_str, reminder_id),
                        )

            outbox_id = f"outbox-{uuid.uuid4().hex[:12]}"
            payload = {
                "occurrence_id": occurrence_id,
                "reminder_id": reminder_id,
                "title": title,
                "scheduled_at": scheduled_at,
                "is_catchup": is_catchup,
            }
            payload_str = json.dumps(payload, ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO outbox (outbox_id, event_type, payload_json, state, retry_count, created_at)
                VALUES (?, ?, ?, 'pending', 0, ?)
                """,
                (outbox_id, topic, payload_str, now_str),
            )

            conn.commit()

        return {
            "outbox_id": outbox_id,
            "occurrence_id": occurrence_id,
            "reminder_id": reminder_id,
            "topic": topic,
            "payload": payload,
            "scheduled_at": scheduled_at,
            "is_catchup": is_catchup,
        }

    def stop_ringing(self, occurrence_id: str) -> dict[str, Any]:
        """
        User dismisses the active alarm/ringing.
        IMPORTANT: This strictly marks ringing stopped and does NOT mark the reminder completed!
        """
        now_str = self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT reminder_id, state FROM occurrences WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Occurrence '{occurrence_id}' not found")

            reminder_id = row["reminder_id"]
            if row["state"] == "dismissed":
                return {
                    "reminder_id": reminder_id,
                    "occurrence_id": occurrence_id,
                    "status": "ringing_stopped",
                }
            if row["state"] != "ringing":
                raise ValueError(
                    f"Occurrence '{occurrence_id}' is not ringing (state={row['state']})"
                )
            conn.execute(
                "UPDATE occurrences SET state = 'dismissed', delivered_at = ? "
                "WHERE occurrence_id = ? AND state = 'ringing'",
                (now_str, occurrence_id),
            )
            self._cancel_reminder_alerts(conn, occurrence_id=occurrence_id)
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
        if snooze_minutes <= 0:
            raise ValueError("snooze_minutes must be positive")
        now = datetime.now(timezone.utc)
        now_str = now.isoformat().replace("+00:00", "Z")
        new_due = (now + timedelta(minutes=snooze_minutes)).isoformat().replace("+00:00", "Z")
        new_occ_id = f"occ-{reminder_id}-{uuid.uuid4().hex}"

        with self._lock, self._get_connection() as conn:
            # Mark current occurrence snoozed if supplied
            if occurrence_id:
                cursor = conn.execute(
                    "UPDATE occurrences SET state = 'snoozed' WHERE occurrence_id = ? AND reminder_id = ?",
                    (occurrence_id, reminder_id),
                )
                if cursor.rowcount == 0:
                    raise KeyError(f"Occurrence '{occurrence_id}' not found for reminder '{reminder_id}'")
                self._cancel_reminder_alerts(conn, occurrence_id=occurrence_id)
            else:
                conn.execute("UPDATE occurrences SET state = 'snoozed' WHERE reminder_id = ? AND state = 'pending'", (reminder_id,))
                self._cancel_reminder_alerts(conn, reminder_id=reminder_id)

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
                "UPDATE occurrences SET state = 'completed' WHERE reminder_id = ? AND state IN ('pending', 'ringing')",
                (reminder_id,),
            )
            self._cancel_reminder_alerts(conn, reminder_id=reminder_id)
            conn.commit()
        return {"reminder_id": reminder_id, "status": "completed"}

    @staticmethod
    def _cancel_reminder_alerts(
        conn: sqlite3.Connection,
        *,
        reminder_id: Optional[str] = None,
        occurrence_id: Optional[str] = None,
    ) -> None:
        """Cancel queued reminder alerts in the same transaction as user confirmation."""
        rows = conn.execute(
            "SELECT outbox_id, payload_json FROM outbox "
            "WHERE event_type = 'reminder.alert' AND state IN ('pending', 'dispatching')"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
            except (TypeError, json.JSONDecodeError):
                continue
            if occurrence_id is not None and payload.get("occurrence_id") != occurrence_id:
                continue
            if reminder_id is not None and payload.get("reminder_id") != reminder_id:
                continue
            conn.execute("UPDATE outbox SET state = 'cancelled' WHERE outbox_id = ?", (row["outbox_id"],))

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

    def enqueue_outbox_once(
        self, event_type: str, payload: Mapping[str, Any], *, idempotency_key: str
    ) -> str:
        """Insert one durable outbox item for an event identity, retaining its state on replay."""
        if not idempotency_key:
            raise ValueError("idempotency_key is required")
        digest = hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:32]
        outbox_id = f"outbox-once-{digest}"
        now_str = self._now_utc_str()
        payload_str = json.dumps(payload, ensure_ascii=False)
        with self._lock, self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR IGNORE INTO outbox (outbox_id, event_type, payload_json, state, retry_count, created_at)
                VALUES (?, ?, ?, 'pending', 0, ?)
                """,
                (outbox_id, event_type, payload_str, now_str),
            )
            conn.commit()
        return outbox_id

    def get_pending_outbox(self, now_iso: Optional[str] = None, include_delayed: bool = False) -> list[dict[str, Any]]:
        now_str = now_iso or self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            self._expire_stale_reminder_outbox(conn, now_str)
            if include_delayed:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE state = 'pending' ORDER BY created_at ASC"
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM outbox
                    WHERE state = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY created_at ASC
                    """,
                    (now_str,),
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
                "UPDATE outbox SET state = 'dispatched', dispatched_at = ? "
                "WHERE outbox_id = ? AND state != 'cancelled'",
                (now_str, outbox_id),
            )
            conn.commit()

    def claim_reminder_alert(self, outbox_id: str) -> bool:
        """Atomically reserve a queued reminder alert before the worker sends it."""
        with self._lock, self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE outbox SET state = 'dispatching' "
                "WHERE outbox_id = ? AND event_type = 'reminder.alert' AND state = 'pending'",
                (outbox_id,),
            )
            conn.commit()
            return cursor.rowcount == 1

    def record_outbox_failure(
        self,
        outbox_id: str,
        error_message: str,
        max_retries: int = 5,
        backoff_seconds: Optional[int] = None,
        keep_pending: bool = False,
    ) -> None:
        """Records an outbox delivery failure, increments retry count, applies optional backoff, and marks failed if max retries exceeded."""
        now = datetime.now(timezone.utc)
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT retry_count, event_type, state, payload_json FROM outbox WHERE outbox_id = ?",
                (outbox_id,),
            ).fetchone()
            if not row:
                return
            if row["state"] == "cancelled":
                return
            new_retries = row["retry_count"] + 1
            if row["event_type"] == "reminder.alert":
                try:
                    occurrence_id = json.loads(row["payload_json"]).get("occurrence_id")
                except (TypeError, json.JSONDecodeError):
                    occurrence_id = None
                if occurrence_id:
                    occurrence = conn.execute(
                        "SELECT state FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)
                    ).fetchone()
                    if not occurrence or occurrence["state"] != "ringing":
                        conn.execute(
                            "UPDATE outbox SET state = 'cancelled', retry_count = ? WHERE outbox_id = ?",
                            (new_retries, outbox_id),
                        )
                        conn.commit()
                        return
            if not keep_pending and row["event_type"] != "reminder.alert" and new_retries >= max_retries:
                new_state = "failed"
                next_retry = None
            else:
                new_state = "pending"
                if backoff_seconds is not None and backoff_seconds > 0:
                    next_retry = (now + timedelta(seconds=backoff_seconds)).isoformat().replace("+00:00", "Z")
                else:
                    next_retry = None

            conn.execute(
                "UPDATE outbox SET retry_count = ?, state = ?, next_retry_at = ? "
                "WHERE outbox_id = ? AND state != 'cancelled'",
                (new_retries, new_state, next_retry, outbox_id),
            )
            conn.commit()

    def queue_outbox_message(self, topic: str, payload: Mapping[str, Any]) -> str:
        """Alias for enqueue_outbox with topic-oriented signature."""
        return self.enqueue_outbox(event_type=topic, payload=payload)

    def get_pending_outbox_messages(self, limit: int = 100, now_iso: Optional[str] = None, include_delayed: bool = False) -> list[dict[str, Any]]:
        """Returns pending outbox entries with 'topic' field populated."""
        now_str = now_iso or self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            self._expire_stale_reminder_outbox(conn, now_str)
            if include_delayed:
                rows = conn.execute(
                    "SELECT * FROM outbox WHERE state = 'pending' ORDER BY created_at ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM outbox
                    WHERE state = 'pending' AND (next_retry_at IS NULL OR next_retry_at <= ?)
                    ORDER BY created_at ASC LIMIT ?
                    """,
                    (now_str, limit),
                ).fetchall()
            result = []
            for r in rows:
                item = dict(r)
                item["topic"] = item["event_type"]
                item["payload"] = json.loads(item.pop("payload_json", "{}"))
                result.append(item)
            return result

    @staticmethod
    def _expire_stale_reminder_outbox(conn: sqlite3.Connection, now_iso: str) -> None:
        """Expire reminder alerts older than 24h so offline devices do not replay stale prompts."""
        try:
            now_dt = datetime.fromisoformat(_normalize_utc_iso(now_iso).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return
        rows = conn.execute(
            "SELECT outbox_id, payload_json, created_at FROM outbox WHERE state = 'pending' AND event_type = 'reminder.alert'"
        ).fetchall()
        for row in rows:
            try:
                payload = json.loads(row["payload_json"])
                timestamp = payload.get("scheduled_at") or row["created_at"]
                scheduled_dt = datetime.fromisoformat(_normalize_utc_iso(timestamp).replace("Z", "+00:00"))
            except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                continue
            if now_dt - scheduled_dt > timedelta(hours=24):
                conn.execute("UPDATE outbox SET state = 'expired' WHERE outbox_id = ?", (row["outbox_id"],))
                occurrence_id = payload.get("occurrence_id")
                reminder_id = payload.get("reminder_id")
                if occurrence_id:
                    conn.execute(
                        "UPDATE occurrences SET state = 'expired' WHERE occurrence_id = ? AND state = 'ringing'",
                        (occurrence_id,),
                    )
                if reminder_id:
                    conn.execute(
                        """
                        UPDATE reminders SET status = CASE
                            WHEN EXISTS (SELECT 1 FROM occurrences WHERE reminder_id = ? AND state = 'pending') THEN 'scheduled'
                            ELSE 'missed'
                        END
                        WHERE reminder_id = ? AND status = 'ringing'
                        """,
                        (reminder_id, reminder_id),
                    )
        conn.commit()

    # =========================================================================
    # Online Backup & Restore
    # =========================================================================

    def backup(self, target_path: str | Path) -> str:
        """Performs a transaction-safe SQLite online backup to the specified file path."""
        target_str = str(target_path)
        Path(target_str).parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            src_conn = self._get_connection()
            backup_conn = sqlite3.connect(target_str)
            try:
                src_conn.backup(backup_conn)
            finally:
                backup_conn.close()
        return target_str

    def restore(self, source_path: str | Path) -> bool:
        """Restores database from an online backup file."""
        source_str = str(source_path)
        if not Path(source_str).exists():
            raise FileNotFoundError(f"Backup file '{source_str}' not found")
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None
            src_conn = sqlite3.connect(source_str)
            dest_conn = sqlite3.connect(self.db_path)
            try:
                src_conn.backup(dest_conn)
            finally:
                src_conn.close()
                dest_conn.close()
            # Reopen connection
            self._get_connection()
        return True

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
