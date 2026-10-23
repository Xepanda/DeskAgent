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


def _pin_zoneinfo_to_locked_tzdata() -> None:
    """Use the pinned tzdata package instead of host-provided zoneinfo files."""
    zoneinfo.reset_tzpath(())
    # reset_tzpath does not invalidate ZoneInfo's key cache; clear any entries
    # created from the host TZPATH before applying the package-only policy.
    zoneinfo.ZoneInfo.clear_cache()


_pin_zoneinfo_to_locked_tzdata()


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


def _validate_timezone_name(tz_name: str) -> str:
    """Reject unknown reminder zones instead of silently scheduling in UTC."""
    cleaned = tz_name.strip()
    if not cleaned:
        raise ValueError("timezone must be a non-empty IANA timezone name")
    try:
        zoneinfo.ZoneInfo(cleaned)
    except Exception as exc:
        if cleaned.lower() not in {
            "america/new_york", "us/eastern", "est5edt",
            "asia/shanghai", "asia/chongqing", "prc", "utc", "gmt",
        }:
            raise ValueError(f"Unknown timezone: {cleaned}") from exc
    return cleaned


def _format_summary_display_time(scheduled_at: str, tz_name: str) -> str:
    """Format an occurrence with strict IANA data and its instant-specific offset."""
    utc_dt = datetime.fromisoformat(_normalize_utc_iso(scheduled_at).replace("Z", "+00:00"))
    local_dt = utc_dt.astimezone(zoneinfo.ZoneInfo(tz_name))
    offset = local_dt.utcoffset()
    if offset is None:
        raise ValueError(f"Timezone has no UTC offset: {tz_name}")
    offset_seconds = int(offset.total_seconds())
    if offset_seconds % 60 != 0:
        raise ValueError(f"Timezone offset is not representable as UTC±HH:MM: {tz_name}")
    sign = "+" if offset_seconds >= 0 else "-"
    offset_minutes = abs(offset_seconds) // 60
    hours, minutes = divmod(offset_minutes, 60)
    if hours > 23:
        raise ValueError(f"Timezone offset is out of range: {tz_name}")
    return f"{local_dt:%Y-%m-%d %H:%M} UTC{sign}{hours:02d}:{minutes:02d}"


def _normalize_utc_iso(value: str) -> str:
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _is_valid_local_time(dt: datetime, target_tz: tzinfo) -> bool:
    """Return false when a local wall time falls inside a timezone transition gap."""
    round_trip = dt.astimezone(timezone.utc).astimezone(target_tz)
    return round_trip.replace(tzinfo=None) == dt.replace(tzinfo=None)


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

    rrule_frequency: Optional[str] = None
    rrule_interval = 1
    if rule.startswith("freq="):
        try:
            rrule_parts = dict(part.split("=", 1) for part in rule.split(";"))
            if set(rrule_parts) - {"freq", "interval"}:
                return None
            rrule_frequency = rrule_parts["freq"]
            rrule_interval = int(rrule_parts.get("interval", "1"))
            if rrule_interval <= 0:
                return None
        except (KeyError, TypeError, ValueError):
            return None

    # Hourly and interval schedules describe elapsed time. Adding a timedelta
    # directly to a ZoneInfo local datetime can skip/repeat an hour at DST
    # transitions, so advance these rules on the UTC timeline instead.
    elapsed_minutes: Optional[int] = None
    if rule == "hourly" or rrule_frequency == "hourly":
        elapsed_minutes = 60 * rrule_interval
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

    if rule == "daily" or rrule_frequency == "daily":
        next_dt = local_dt + timedelta(days=rrule_interval)
    elif rule == "weekly" or rrule_frequency == "weekly":
        next_dt = local_dt + timedelta(days=7 * rrule_interval)
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

    # A recurring wall-clock time can land in the spring-forward gap (for
    # example, 02:30 in New York). ZoneInfo assigns that nonexistent time an
    # offset, which would move the reminder to 03:30 and permanently drift its
    # local time. Skip the nonexistent calendar slot while retaining the
    # original wall-clock time for the next valid recurrence.
    if rule == "daily" or rrule_frequency == "daily":
        stride_days = rrule_interval
    elif rule == "weekly" or rrule_frequency == "weekly":
        stride_days = 7 * rrule_interval
    else:
        stride_days = 0
    for _ in range(366):
        if not stride_days or _is_valid_local_time(next_dt, target_tz):
            break
        next_dt += timedelta(days=stride_days)

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
                snoozed_from_occurrence_id TEXT,
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

            CREATE TABLE IF NOT EXISTS notification_receipts (
                notification_id TEXT PRIMARY KEY,
                reminder_id TEXT NOT NULL,
                occurrence_id TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                delivery_status TEXT NOT NULL DEFAULT 'unknown',
                user_acknowledged INTEGER NOT NULL DEFAULT 0,
                ack_source TEXT,
                acknowledged_at TEXT,
                protocol_anomaly_count INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notification_receipt_events (
                fingerprint TEXT PRIMARY KEY,
                notification_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                received_at TEXT NOT NULL,
                FOREIGN KEY (notification_id) REFERENCES notification_receipts(notification_id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recovery_batches (
                batch_id TEXT PRIMARY KEY,
                summary_id TEXT NOT NULL UNIQUE,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending'
            );

            CREATE TABLE IF NOT EXISTS recovery_batch_items (
                occurrence_id TEXT PRIMARY KEY,
                batch_id TEXT NOT NULL,
                FOREIGN KEY (occurrence_id) REFERENCES occurrences(occurrence_id) ON DELETE CASCADE,
                FOREIGN KEY (batch_id) REFERENCES recovery_batches(batch_id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS recovery_summary_receipts (
                summary_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                acknowledged_at TEXT NOT NULL,
                FOREIGN KEY (summary_id) REFERENCES recovery_batches(summary_id) ON DELETE CASCADE
            );
            """)
            try:
                conn.execute("ALTER TABLE outbox ADD COLUMN next_retry_at TEXT;")
            except sqlite3.OperationalError:
                pass
            receipt_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(notification_receipts)").fetchall()
            }
            if "protocol_anomaly_count" not in receipt_columns:
                conn.execute(
                    "ALTER TABLE notification_receipts ADD COLUMN protocol_anomaly_count "
                    "INTEGER NOT NULL DEFAULT 0"
                )
            occurrence_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(occurrences)").fetchall()
            }
            if "snoozed_from_occurrence_id" not in occurrence_columns:
                conn.execute("ALTER TABLE occurrences ADD COLUMN snoozed_from_occurrence_id TEXT")
            conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_occurrences_snoozed_from "
                "ON occurrences(snoozed_from_occurrence_id) "
                "WHERE snoozed_from_occurrence_id IS NOT NULL"
            )
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
            # A process may exit after claiming a recovery summary but before
            # recording its transport attempt. Requeue only summaries whose
            # frozen batch is still active; cancelled/expired batches stay dead.
            conn.execute(
                """UPDATE outbox SET state = 'pending' WHERE event_type = 'notification.summary.deliver'
                   AND state = 'dispatching'
                   AND EXISTS (
                       SELECT 1 FROM recovery_batches b
                       WHERE b.summary_id = json_extract(outbox.payload_json, '$.summary_id')
                         AND b.state = 'pending'
                   )"""
            )
            conn.execute(
                """UPDATE outbox SET state = 'cancelled' WHERE event_type = 'notification.summary.deliver'
                   AND state = 'dispatching'
                   AND NOT EXISTS (
                       SELECT 1 FROM recovery_batches b
                       WHERE b.summary_id = json_extract(outbox.payload_json, '$.summary_id')
                         AND b.state = 'pending'
                   )"""
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
        timezone_str = _validate_timezone_name(timezone_str)
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
                pending = conn.execute(
                    "SELECT occurrence_id FROM occurrences WHERE reminder_id = ? AND state = 'pending'",
                    (reminder_id,),
                ).fetchall()
                pending_ids = [row["occurrence_id"] for row in pending]
                if pending_ids:
                    placeholders = ",".join("?" for _ in pending_ids)
                    batches = conn.execute(
                        f"SELECT DISTINCT batch_id FROM recovery_batch_items WHERE occurrence_id IN ({placeholders})",
                        pending_ids,
                    ).fetchall()
                    for batch in batches:
                        batch_id = batch["batch_id"]
                        summary = conn.execute(
                            "SELECT summary_id FROM recovery_batches WHERE batch_id = ?", (batch_id,)
                        ).fetchone()
                        if summary:
                            conn.execute(
                                "UPDATE outbox SET state = 'cancelled', next_retry_at = NULL "
                                "WHERE event_type = 'notification.summary.deliver' "
                                "AND json_extract(payload_json, '$.summary_id') = ? "
                                "AND state IN ('pending', 'dispatching')",
                                (summary["summary_id"],),
                            )
                        # A frozen summary is no longer valid after a member is
                        # rescheduled. Release every member so the next scan can
                        # build a fresh snapshot for the remaining pending items.
                        conn.execute(
                            "UPDATE recovery_batches SET state = 'cancelled' WHERE batch_id = ? "
                            "AND state NOT IN ('expired', 'cancelled')",
                            (batch_id,),
                        )
                        conn.execute("DELETE FROM recovery_batch_items WHERE batch_id = ?", (batch_id,))

                    if batches:
                        # A new occurrence identity prevents the replacement
                        # schedule from colliding with the immutable summary's
                        # deterministic member digest.
                        conn.execute(
                            "UPDATE occurrences SET state = 'cancelled' "
                            f"WHERE occurrence_id IN ({placeholders})",
                            pending_ids,
                        )
                        conn.execute(
                            "INSERT INTO occurrences (occurrence_id, reminder_id, scheduled_at, state) "
                            "VALUES (?, ?, ?, 'pending')",
                            (f"occ-{reminder_id}-rescheduled-{uuid.uuid4().hex}", reminder_id, new_due),
                        )
                    else:
                        conn.execute(
                            "UPDATE occurrences SET scheduled_at = ? "
                            "WHERE reminder_id = ? AND state = 'pending'",
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
                  AND NOT EXISTS (
                      SELECT 1 FROM recovery_batch_items bi
                      WHERE bi.occurrence_id = o.occurrence_id
                  )
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

    def create_recovery_summary_batch(self, *, now_iso: Optional[str] = None) -> Optional[dict[str, Any]]:
        """Freeze one idempotent summary batch for overdue occurrences inside 24 hours.

        This operation intentionally leaves occurrence lifecycle state untouched. The
        durable membership relation prevents the regular CR-017 scheduler from
        converting summarized occurrences into ringing alerts.
        """
        now_str = _normalize_utc_iso(now_iso or self._now_utc_str())
        now_dt = datetime.fromisoformat(now_str.replace("Z", "+00:00")).astimezone(timezone.utc)
        cutoff_dt = now_dt - timedelta(hours=24)
        cutoff_str = cutoff_dt.isoformat().replace("+00:00", "Z")

        with self._lock, self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._expire_recovery_summaries(conn, now_str)

            # Older pending occurrences follow the existing missed/expired path;
            # only the inclusive 24-hour window is eligible for a summary.
            expired = conn.execute(
                """SELECT o.occurrence_id, o.reminder_id, r.repeat_rule, r.timezone
                   FROM occurrences o JOIN reminders r ON r.reminder_id = o.reminder_id
                   WHERE o.state = 'pending' AND o.scheduled_at < ?""",
                (cutoff_str,),
            ).fetchall()
            for row in expired:
                conn.execute("UPDATE occurrences SET state = 'expired' WHERE occurrence_id = ?", (row["occurrence_id"],))
                if row["repeat_rule"]:
                    next_due = _next_occurrence_after(
                        cutoff_str, row["repeat_rule"], now_str, row["timezone"]
                    )
                    if next_due:
                        next_ts = int(datetime.fromisoformat(next_due.replace("Z", "+00:00")).timestamp())
                        next_occ_id = f"occ-{row['reminder_id']}-{next_ts}"
                        conn.execute(
                            "INSERT OR IGNORE INTO occurrences (occurrence_id, reminder_id, scheduled_at, state) "
                            "VALUES (?, ?, ?, 'pending')",
                            (next_occ_id, row["reminder_id"], next_due),
                        )
                        conn.execute(
                            "UPDATE reminders SET due_at = ?, status = 'scheduled', updated_at = ? WHERE reminder_id = ?",
                            (next_due, now_str, row["reminder_id"]),
                        )
                else:
                    conn.execute(
                        "UPDATE reminders SET status = 'missed', updated_at = ? WHERE reminder_id = ? "
                        "AND status NOT IN ('completed', 'cancelled', 'archived')",
                        (now_str, row["reminder_id"]),
                    )

            rows = conn.execute(
                """SELECT o.occurrence_id, o.reminder_id, o.scheduled_at,
                          r.title, r.timezone
                   FROM occurrences o JOIN reminders r ON r.reminder_id = o.reminder_id
                   WHERE o.state = 'pending' AND r.status NOT IN ('completed', 'cancelled', 'archived')
                     AND o.scheduled_at <= ? AND o.scheduled_at >= ?
                   ORDER BY o.scheduled_at ASC, o.occurrence_id ASC""",
                (now_str, cutoff_str),
            ).fetchall()
            if not rows:
                conn.commit()
                return None

            assigned: dict[str, str] = {}
            for member in conn.execute(
                "SELECT occurrence_id, batch_id FROM recovery_batch_items"
            ).fetchall():
                assigned[member["occurrence_id"]] = member["batch_id"]
            unassigned = [row for row in rows if row["occurrence_id"] not in assigned]
            if not unassigned:
                batch_ids = {assigned[row["occurrence_id"]] for row in rows}
                if len(batch_ids) == 1:
                    result = self._recovery_batch_result(conn, next(iter(batch_ids)))
                    conn.commit()
                    return result
                # Older batches are immutable; return the earliest eligible one
                # when a scan spans multiple already-frozen recovery windows.
                batch_id = assigned[rows[0]["occurrence_id"]]
                result = self._recovery_batch_result(conn, batch_id)
                conn.commit()
                return result

            members = unassigned
            member_ids = [row["occurrence_id"] for row in members]
            digest = hashlib.sha256("\n".join(member_ids).encode("utf-8")).hexdigest()[:32]
            batch_id = f"recovery-batch-{digest}"
            summary_id = f"summary-{digest}"
            existing = conn.execute("SELECT batch_id FROM recovery_batches WHERE batch_id = ?", (batch_id,)).fetchone()
            if existing:
                result = self._recovery_batch_result(conn, batch_id)
                conn.commit()
                return result

            items = [
                {
                    "reminder_id": row["reminder_id"],
                    "occurrence_id": row["occurrence_id"],
                    "title": row["title"],
                    "scheduled_at": _normalize_utc_iso(row["scheduled_at"]),
                    "timezone": _validate_timezone_name(row["timezone"]),
                    "display_time": _format_summary_display_time(
                        row["scheduled_at"], _validate_timezone_name(row["timezone"])
                    ),
                }
                for row in members
            ]
            visible_items = items[:20]
            created_dt = now_dt
            expires_at = (created_dt + timedelta(hours=24)).isoformat().replace("+00:00", "Z")
            payload = {
                "summary_id": summary_id,
                "batch_id": batch_id,
                "created_at": now_str,
                "expires_at": expires_at,
                "items": visible_items,
                "omitted_count": max(0, len(items) - len(visible_items)),
            }
            payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            outbox_digest = hashlib.sha256(summary_id.encode("utf-8")).hexdigest()[:32]
            outbox_id = f"outbox-summary-{outbox_digest}"

            conn.execute(
                "INSERT INTO recovery_batches (batch_id, summary_id, payload_json, created_at, expires_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (batch_id, summary_id, payload_json, now_str, expires_at),
            )
            conn.executemany(
                "INSERT INTO recovery_batch_items (occurrence_id, batch_id) VALUES (?, ?)",
                [(occurrence_id, batch_id) for occurrence_id in member_ids],
            )
            conn.execute(
                "INSERT INTO outbox (outbox_id, event_type, payload_json, state, retry_count, created_at) "
                "VALUES (?, 'notification.summary.deliver', ?, 'pending', 0, ?)",
                (outbox_id, payload_json, now_str),
            )
            result = {
                "batch_id": batch_id,
                "summary_id": summary_id,
                "outbox_id": outbox_id,
                "payload": payload,
            }
            conn.commit()
            return result

    @staticmethod
    def _recovery_batch_result(conn: sqlite3.Connection, batch_id: str) -> Optional[dict[str, Any]]:
        row = conn.execute(
            "SELECT batch_id, summary_id, payload_json FROM recovery_batches WHERE batch_id = ?",
            (batch_id,),
        ).fetchone()
        if not row:
            return None
        outbox = conn.execute(
            "SELECT outbox_id FROM outbox WHERE event_type = 'notification.summary.deliver' "
            "AND json_extract(payload_json, '$.summary_id') = ? ORDER BY created_at LIMIT 1",
            (row["summary_id"],),
        ).fetchone()
        return {
            "batch_id": row["batch_id"],
            "summary_id": row["summary_id"],
            "outbox_id": outbox["outbox_id"] if outbox else None,
            "payload": json.loads(row["payload_json"]),
        }

    def claim_recovery_summary_delivery(
        self,
        outbox_id: str,
        summary_id: str,
        *,
        now_iso: Optional[str] = None,
    ) -> bool:
        """Atomically claim only a current summary immediately before device send.

        A successful pending->dispatching transition is the send-start
        linearization point. Later reminder edits cannot promise to retract a
        send already claimed by the delivery loop.
        """
        with self._lock:
            conn = self._get_connection()
            now_str = _normalize_utc_iso(now_iso) if now_iso else self._now_utc_str()
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._expire_recovery_summaries(conn, now_str)
                row = conn.execute(
                    """SELECT o.state AS outbox_state, o.payload_json AS outbox_payload,
                              b.batch_id, b.state AS batch_state, b.expires_at,
                              b.payload_json AS batch_payload
                       FROM outbox o JOIN recovery_batches b
                         ON json_extract(o.payload_json, '$.summary_id') = b.summary_id
                       WHERE o.outbox_id = ? AND o.event_type = 'notification.summary.deliver'
                         AND b.summary_id = ?""",
                    (outbox_id, summary_id),
                ).fetchone()
                valid = False
                if row:
                    try:
                        outbox_payload = json.loads(row["outbox_payload"])
                        batch_payload = json.loads(row["batch_payload"])
                        visible_items = batch_payload.get("items", [])
                        valid = (
                            row["outbox_state"] == "pending"
                            and row["batch_state"] == "pending"
                            and row["expires_at"] > now_str
                            and isinstance(outbox_payload, dict)
                            and isinstance(batch_payload, dict)
                            and outbox_payload == batch_payload
                            and isinstance(visible_items, list)
                        )
                    except (TypeError, ValueError, AttributeError):
                        valid = False

                if valid:
                    member_rows = conn.execute(
                        """SELECT o.occurrence_id, o.reminder_id, o.scheduled_at, o.state,
                                  r.title, r.timezone, r.status AS reminder_status
                           FROM recovery_batch_items bi
                           JOIN occurrences o ON o.occurrence_id = bi.occurrence_id
                           JOIN reminders r ON r.reminder_id = o.reminder_id
                           WHERE bi.batch_id = ?""",
                        (row["batch_id"],),
                    ).fetchall()
                    omitted_count = batch_payload.get("omitted_count")
                    valid = (
                        isinstance(omitted_count, int)
                        and not isinstance(omitted_count, bool)
                        and omitted_count >= 0
                        and len(member_rows) == len(visible_items) + omitted_count
                        and all(
                            member["state"] == "pending"
                            and member["reminder_status"] not in {"completed", "cancelled", "archived"}
                            for member in member_rows
                        )
                    )
                    by_id = {member["occurrence_id"]: member for member in member_rows}
                    for item in visible_items:
                        if not isinstance(item, dict):
                            valid = False
                            break
                        member = by_id.get(item.get("occurrence_id"))
                        if not member or any(
                            member[key] != item.get(payload_key)
                            for key, payload_key in (
                                ("reminder_id", "reminder_id"),
                                ("scheduled_at", "scheduled_at"),
                                ("title", "title"),
                                ("timezone", "timezone"),
                            )
                        ):
                            valid = False
                            break

                if not valid and row and row["batch_state"] == "pending" and row["outbox_state"] == "pending":
                    conn.execute(
                        "UPDATE recovery_batches SET state = 'cancelled' WHERE batch_id = ?",
                        (row["batch_id"],),
                    )
                    conn.execute(
                        "UPDATE outbox SET state = 'cancelled', next_retry_at = NULL "
                        "WHERE outbox_id = ? AND state IN ('pending', 'dispatching')",
                        (outbox_id,),
                    )
                    conn.execute(
                        "DELETE FROM recovery_batch_items WHERE batch_id = ?", (row["batch_id"],)
                    )
                    conn.commit()
                    return False

                if not valid or not row:
                    conn.commit()
                    return False
                cursor = conn.execute(
                    """UPDATE outbox SET state = 'dispatching'
                       WHERE outbox_id = ? AND event_type = 'notification.summary.deliver'
                         AND state = 'pending'
                         AND EXISTS (
                           SELECT 1 FROM recovery_batches b
                           WHERE b.summary_id = ? AND b.batch_id = ?
                             AND b.state = 'pending' AND b.expires_at > ?
                         )""",
                    (outbox_id, summary_id, row["batch_id"], now_str),
                )
                conn.commit()
                return cursor.rowcount == 1
            except Exception:
                conn.rollback()
                raise

    def get_recovery_summary_receipt(self, summary_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT summary_id, source, acknowledged_at FROM recovery_summary_receipts WHERE summary_id = ?",
                (summary_id,),
            ).fetchone()
            if row:
                result = dict(row)
                result["acknowledged"] = True
                return result
            batch = conn.execute(
                "SELECT summary_id FROM recovery_batches WHERE summary_id = ?", (summary_id,)
            ).fetchone()
            return {"summary_id": summary_id, "acknowledged": False} if batch else None

    def record_recovery_summary_ack(
        self, event: Mapping[str, Any], *, now_iso: Optional[str] = None
    ) -> str:
        """Record a summary receipt only; it never completes a reminder occurrence."""
        summary_id = event.get("summary_id")
        source = event.get("source")
        if event.get("type") != "notification.acknowledged" or not isinstance(summary_id, str) or not summary_id:
            return "ignored"
        if not isinstance(source, str) or not source:
            return "ignored"
        now_str = _normalize_utc_iso(now_iso or self._now_utc_str())
        with self._lock, self._get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            batch = conn.execute(
                "SELECT expires_at FROM recovery_batches WHERE summary_id = ?", (summary_id,)
            ).fetchone()
            if not batch:
                conn.commit()
                return "ignored"
            expiry = datetime.fromisoformat(_normalize_utc_iso(batch["expires_at"]).replace("Z", "+00:00"))
            now_dt = datetime.fromisoformat(now_str.replace("Z", "+00:00"))
            if now_dt >= expiry:
                self._expire_recovery_summaries(conn, now_str)
                conn.commit()
                return "ignored"
            inserted = conn.execute(
                "INSERT OR IGNORE INTO recovery_summary_receipts (summary_id, source, acknowledged_at) "
                "VALUES (?, ?, ?)",
                (summary_id, source, now_str),
            ).rowcount
            conn.execute(
                "UPDATE recovery_batches SET state = 'acknowledged' WHERE summary_id = ? AND state != 'expired'",
                (summary_id,),
            )
            conn.execute(
                "UPDATE outbox SET state = 'acknowledged', next_retry_at = NULL "
                "WHERE event_type = 'notification.summary.deliver' "
                "AND json_extract(payload_json, '$.summary_id') = ? AND state != 'expired'",
                (summary_id,),
            )
            conn.commit()
            return "recorded" if inserted else "duplicate"

    @staticmethod
    def _expire_recovery_summaries(conn: sqlite3.Connection, now_iso: str) -> None:
        """Stop expired summaries and resolve their still-pending occurrences."""
        conn.execute(
            "UPDATE recovery_batches SET state = 'expired' WHERE expires_at <= ? AND state = 'pending'",
            (now_iso,),
        )
        conn.execute(
            "UPDATE outbox SET state = 'expired' WHERE event_type = 'notification.summary.deliver' "
            "AND state IN ('pending', 'dispatching') AND json_extract(payload_json, '$.expires_at') <= ?",
            (now_iso,),
        )
        overdue = conn.execute(
            """SELECT o.occurrence_id, o.reminder_id, o.scheduled_at,
                      r.repeat_rule, r.timezone
               FROM recovery_batch_items bi
               JOIN recovery_batches b ON b.batch_id = bi.batch_id
               JOIN occurrences o ON o.occurrence_id = bi.occurrence_id
               JOIN reminders r ON r.reminder_id = o.reminder_id
               WHERE b.state = 'expired' AND b.expires_at <= ? AND o.state = 'pending'
                 AND r.status NOT IN ('completed', 'cancelled', 'archived')""",
            (now_iso,),
        ).fetchall()
        for row in overdue:
            conn.execute(
                "UPDATE occurrences SET state = 'expired' WHERE occurrence_id = ? AND state = 'pending'",
                (row["occurrence_id"],),
            )
            next_due = (
                _next_occurrence_after(row["scheduled_at"], row["repeat_rule"], now_iso, row["timezone"])
                if row["repeat_rule"]
                else None
            )
            if next_due:
                next_ts = int(datetime.fromisoformat(next_due.replace("Z", "+00:00")).timestamp())
                next_occurrence_id = f"occ-{row['reminder_id']}-{next_ts}"
                conn.execute(
                    "INSERT OR IGNORE INTO occurrences (occurrence_id, reminder_id, scheduled_at, state) "
                    "VALUES (?, ?, ?, 'pending')",
                    (next_occurrence_id, row["reminder_id"], next_due),
                )
                conn.execute(
                    "UPDATE reminders SET due_at = ?, status = 'scheduled', updated_at = ? "
                    "WHERE reminder_id = ? AND status NOT IN ('completed', 'cancelled', 'archived')",
                    (next_due, now_iso, row["reminder_id"]),
                )
            else:
                conn.execute(
                    "UPDATE reminders SET status = 'missed', updated_at = ? "
                    "WHERE reminder_id = ? AND status NOT IN ('completed', 'cancelled', 'archived')",
                    (now_iso, row["reminder_id"]),
                )

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
            if topic == "reminder.alert" and conn.execute(
                "SELECT 1 FROM recovery_batch_items WHERE occurrence_id = ?",
                (occurrence_id,),
            ).fetchone():
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
                "expires_at": self._notification_expiry(scheduled_at),
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
            reminder = conn.execute(
                "SELECT reminder_id FROM reminders WHERE reminder_id = ?", (reminder_id,)
            ).fetchone()
            if not reminder:
                raise KeyError(f"Reminder '{reminder_id}' not found")

            # Mark current occurrence snoozed if supplied
            if occurrence_id:
                source = conn.execute(
                    "SELECT state FROM occurrences WHERE occurrence_id = ? AND reminder_id = ?",
                    (occurrence_id, reminder_id),
                ).fetchone()
                if not source:
                    raise KeyError(f"Occurrence '{occurrence_id}' not found for reminder '{reminder_id}'")
                if source["state"] == "snoozed":
                    existing = conn.execute(
                        "SELECT occurrence_id, scheduled_at FROM occurrences "
                        "WHERE snoozed_from_occurrence_id = ?",
                        (occurrence_id,),
                    ).fetchone()
                    if existing:
                        return {
                            "reminder_id": reminder_id,
                            "occurrence_id": existing["occurrence_id"],
                            "snoozed_until": existing["scheduled_at"],
                            "status": "snoozed",
                        }
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
                "INSERT INTO occurrences "
                "(occurrence_id, reminder_id, scheduled_at, state, snoozed_from_occurrence_id) "
                "VALUES (?, ?, ?, 'pending', ?)",
                (new_occ_id, reminder_id, new_due, occurrence_id),
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
            self._expire_recovery_summaries(conn, now_str)
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

    @staticmethod
    def _notification_expiry(scheduled_at: str) -> str:
        scheduled = datetime.fromisoformat(_normalize_utc_iso(scheduled_at).replace("Z", "+00:00"))
        return (scheduled.astimezone(timezone.utc) + timedelta(hours=24)).isoformat().replace("+00:00", "Z")

    @classmethod
    def build_reminder_notification(cls, item: Mapping[str, Any]) -> dict[str, Any]:
        """Map an existing outbox row to the CR-017 allowlisted device payload."""
        payload = item.get("payload")
        if not isinstance(payload, Mapping):
            raise ValueError("reminder outbox payload is invalid")
        occurrence_id = payload.get("occurrence_id")
        reminder_id = payload.get("reminder_id")
        scheduled_at = payload.get("scheduled_at") or item.get("created_at")
        if not all(isinstance(value, str) and value for value in (occurrence_id, reminder_id, scheduled_at)):
            raise ValueError("reminder outbox is missing notification identity or schedule")
        expires_at = payload.get("expires_at") or cls._notification_expiry(scheduled_at)
        return {
            "notification_id": occurrence_id,
            "reminder_id": reminder_id,
            "occurrence_id": occurrence_id,
            "title": payload.get("title", "Reminder"),
            "scheduled_at": scheduled_at,
            "expires_at": expires_at,
            "is_catchup": payload.get("is_catchup", False),
        }

    def get_active_notification_contexts(self, now_iso: Optional[str] = None) -> dict[str, str]:
        """Load pending/claimed reminder IDs and their existing 24-hour expiry on startup."""
        now_str = now_iso or self._now_utc_str()
        with self._lock, self._get_connection() as conn:
            self._expire_stale_reminder_outbox(conn, now_str)
            rows = conn.execute(
                "SELECT payload_json, created_at FROM outbox "
                "WHERE event_type = 'reminder.alert' AND state IN ('pending', 'dispatching', 'dispatched', 'cancelled')"
            ).fetchall()
            result: dict[str, str] = {}
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    occurrence_id = payload["occurrence_id"]
                    scheduled_at = payload.get("scheduled_at") or row["created_at"]
                    expires_at = payload.get("expires_at") or self._notification_expiry(scheduled_at)
                    expiry_dt = datetime.fromisoformat(_normalize_utc_iso(expires_at).replace("Z", "+00:00"))
                    now_dt = datetime.fromisoformat(_normalize_utc_iso(now_str).replace("Z", "+00:00"))
                    if isinstance(occurrence_id, str) and occurrence_id and now_dt <= expiry_dt:
                        result[occurrence_id] = expires_at
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            conn.commit()
            return result

    def ensure_notification_receipt(self, notification: Mapping[str, Any], *, now_iso: Optional[str] = None) -> str:
        """Create the durable unknown receipt before transport I/O for a claimed outbox row."""
        notification_id = notification.get("notification_id")
        if not isinstance(notification_id, str) or not notification_id or notification_id != notification.get("occurrence_id"):
            return "ignored"
        reminder_id = notification.get("reminder_id")
        expires_at = notification.get("expires_at")
        if not isinstance(reminder_id, str) or not isinstance(expires_at, str):
            return "ignored"
        now_str = _normalize_utc_iso(now_iso or self._now_utc_str())
        now_dt = datetime.fromisoformat(now_str.replace("Z", "+00:00")).astimezone(timezone.utc)
        expiry_dt = datetime.fromisoformat(_normalize_utc_iso(expires_at).replace("Z", "+00:00"))
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                "SELECT payload_json, created_at FROM outbox "
                "WHERE event_type = 'reminder.alert' AND state IN ('pending', 'dispatching')"
            ).fetchall()
            linked = False
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    occurrence = payload.get("occurrence_id")
                    scheduled = payload.get("scheduled_at") or row["created_at"]
                    stored_expiry = payload.get("expires_at") or self._notification_expiry(scheduled)
                    if occurrence == notification_id and _normalize_utc_iso(stored_expiry) == _normalize_utc_iso(expires_at):
                        linked = True
                        break
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            if not linked or now_dt > expiry_dt:
                conn.commit()
                return "ignored"
            inserted = conn.execute(
                "INSERT OR IGNORE INTO notification_receipts "
                "(notification_id, reminder_id, occurrence_id, expires_at, delivery_status, updated_at) "
                "VALUES (?, ?, ?, ?, 'unknown', ?)",
                (notification_id, reminder_id, notification_id, _normalize_utc_iso(expires_at), now_str),
            )
            row = conn.execute(
                "SELECT reminder_id, expires_at FROM notification_receipts WHERE notification_id = ?",
                (notification_id,),
            ).fetchone()
            if not row or row["reminder_id"] != reminder_id or row["expires_at"] != _normalize_utc_iso(expires_at):
                conn.rollback()
                return "conflict"
            conn.commit()
            return "ready" if inserted.rowcount == 1 else "exists"

    def get_notification_receipt(self, notification_id: str) -> Optional[dict[str, Any]]:
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM notification_receipts WHERE notification_id = ?", (notification_id,)
            ).fetchone()
            return dict(row) if row else None

    def record_managed_notification_event(
        self, event: Mapping[str, Any], *, now_iso: Optional[str] = None
    ) -> str:
        """Durably record an associated CR-017 event without changing reminder lifecycle."""
        event_type = event.get("type")
        notification_id = event.get("notification_id")
        if not isinstance(notification_id, str) or not notification_id:
            return "ignored"
        if event_type == "notification.acknowledged":
            source = event.get("source")
            if not isinstance(source, str) or not source:
                return "ignored"
            normalized: dict[str, Any] = {
                "type": event_type, "notification_id": notification_id, "source": source,
            }
        elif event_type == "notification.status":
            status = event.get("status")
            if status not in {"accepted", "rendered", "played", "failed", "expired", "unknown"}:
                return "ignored"
            # CR-018 is blocked: accepted remains an unverified observation, never a durable stage.
            normalized = {
                "type": event_type,
                "notification_id": notification_id,
                "status": "unknown" if status == "accepted" else status,
            }
            if status == "accepted" or event.get("unverified_device_status") == "accepted":
                normalized["unverified_device_status"] = "accepted"
        else:
            return "ignored"

        canonical = json.dumps(normalized, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        fingerprint = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        now_str = _normalize_utc_iso(now_iso or self._now_utc_str())
        now_dt = datetime.fromisoformat(now_str.replace("Z", "+00:00")).astimezone(timezone.utc)
        with self._lock, self._get_connection() as conn:
            rows = conn.execute(
                "SELECT payload_json, created_at FROM outbox "
                "WHERE event_type = 'reminder.alert' AND state IN ('pending', 'dispatching', 'dispatched', 'cancelled')"
            ).fetchall()
            linked: tuple[dict[str, Any], str] | None = None
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"])
                    if payload.get("occurrence_id") != notification_id:
                        continue
                    scheduled_at = payload.get("scheduled_at") or row["created_at"]
                    expires_at = payload.get("expires_at") or self._notification_expiry(scheduled_at)
                    linked = (payload, expires_at)
                    break
                except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
                    continue
            if linked is None:
                conn.commit()
                return "ignored"
            payload, expires_at = linked
            expiry_dt = datetime.fromisoformat(_normalize_utc_iso(expires_at).replace("Z", "+00:00"))
            if now_dt > expiry_dt:
                conn.commit()
                return "ignored"

            conn.execute(
                "INSERT OR IGNORE INTO notification_receipts "
                "(notification_id, reminder_id, occurrence_id, expires_at, delivery_status, updated_at) "
                "VALUES (?, ?, ?, ?, 'unknown', ?)",
                (notification_id, payload.get("reminder_id", ""), notification_id, expires_at, now_str),
            )
            inserted = conn.execute(
                "INSERT OR IGNORE INTO notification_receipt_events "
                "(fingerprint, notification_id, event_type, received_at) VALUES (?, ?, ?, ?)",
                (fingerprint, notification_id, event_type, now_str),
            ).rowcount
            if not inserted:
                conn.commit()
                return "duplicate"

            if event_type == "notification.acknowledged":
                conn.execute(
                    "UPDATE notification_receipts SET user_acknowledged = 1, ack_source = COALESCE(ack_source, ?), "
                    "acknowledged_at = COALESCE(acknowledged_at, ?), updated_at = ? "
                    "WHERE notification_id = ?",
                    (normalized["source"], now_str, now_str, notification_id),
                )
            else:
                current_row = conn.execute(
                    "SELECT delivery_status FROM notification_receipts WHERE notification_id = ?",
                    (notification_id,),
                ).fetchone()
                current = current_row["delivery_status"]
                incoming = normalized["status"]
                terminal = {"failed", "expired"}
                rank = {"unknown": 0, "accepted": 1, "rendered": 2, "played": 3}
                if current in terminal:
                    merged = current
                    if incoming not in {current, "unknown"}:
                        conn.execute(
                            "UPDATE notification_receipts SET protocol_anomaly_count = protocol_anomaly_count + 1 "
                            "WHERE notification_id = ?",
                            (notification_id,),
                        )
                elif incoming in terminal:
                    if current == "played":
                        conn.execute(
                            "UPDATE notification_receipts SET protocol_anomaly_count = protocol_anomaly_count + 1 "
                            "WHERE notification_id = ?",
                            (notification_id,),
                        )
                        merged = current
                    else:
                        merged = incoming
                elif rank.get(incoming, 0) > rank.get(current, 0):
                    merged = incoming
                else:
                    merged = current
                conn.execute(
                    "UPDATE notification_receipts SET delivery_status = ?, updated_at = ? "
                    "WHERE notification_id = ?",
                    (merged, now_str, notification_id),
                )
            conn.commit()
            return "recorded"

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

    def record_terminal_reminder_alert_failure(self, outbox_id: str) -> bool:
        """Mark only a claimed, still-ringing reminder alert terminally failed.

        Classified non-retryable transport NACKs must not be rescheduled. The
        transition is intentionally limited to reminder.alert rows currently
        in dispatching state and leaves reminder/occurrence lifecycle intact.
        """
        with self._lock, self._get_connection() as conn:
            row = conn.execute(
                "SELECT payload_json FROM outbox "
                "WHERE outbox_id = ? AND event_type = 'reminder.alert' AND state = 'dispatching'",
                (outbox_id,),
            ).fetchone()
            if not row:
                return False
            try:
                occurrence_id = json.loads(row["payload_json"]).get("occurrence_id")
            except (TypeError, json.JSONDecodeError):
                return False
            if not isinstance(occurrence_id, str) or not occurrence_id:
                return False
            occurrence = conn.execute(
                "SELECT state FROM occurrences WHERE occurrence_id = ?", (occurrence_id,)
            ).fetchone()
            if not occurrence or occurrence["state"] != "ringing":
                return False
            cursor = conn.execute(
                "UPDATE outbox SET state = 'failed', retry_count = retry_count + 1, next_retry_at = NULL "
                "WHERE outbox_id = ? AND event_type = 'reminder.alert' AND state = 'dispatching'",
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
            is_summary_delivery = row["event_type"] == "notification.summary.deliver"
            if is_summary_delivery and row["state"] not in {"pending", "dispatching"}:
                # Pre-claim availability failures may back off a pending item;
                # post-claim adapter failures may back off a dispatching item.
                # ACK/expiry/cancellation are terminal and must not be revived.
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

            if is_summary_delivery:
                conn.execute(
                    "UPDATE outbox SET retry_count = ?, state = ?, next_retry_at = ? "
                    "WHERE outbox_id = ? AND event_type = 'notification.summary.deliver' "
                    "AND state = ?",
                    (new_retries, new_state, next_retry, outbox_id, row["state"]),
                )
            else:
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
            self._expire_recovery_summaries(conn, now_str)
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

    @staticmethod
    def _validate_restore_source(src_conn: sqlite3.Connection) -> None:
        """Reject damaged or unrelated SQLite files before touching the live database."""
        try:
            integrity = [row[0] for row in src_conn.execute("PRAGMA integrity_check").fetchall()]
            if integrity != ["ok"]:
                raise ValueError("Invalid SQLite backup: integrity_check did not return ok")

            required_columns = {
                "reminders": {
                    "reminder_id", "title", "due_at", "timezone", "repeat_rule", "priority",
                    "status", "created_at", "updated_at",
                },
                "occurrences": {
                    "occurrence_id", "reminder_id", "scheduled_at", "delivered_at", "state",
                    "snoozed_from_occurrence_id",
                },
                "notes": {
                    "note_id", "title", "content", "tags_json", "created_at", "updated_at",
                },
                "outbox": {
                    "outbox_id", "event_type", "payload_json", "state", "retry_count", "created_at",
                    "dispatched_at", "next_retry_at",
                },
                "notification_receipts": {
                    "notification_id", "reminder_id", "occurrence_id", "expires_at", "delivery_status",
                    "user_acknowledged", "ack_source", "acknowledged_at", "protocol_anomaly_count", "updated_at",
                },
                "notification_receipt_events": {
                    "fingerprint", "notification_id", "event_type", "received_at",
                },
                "recovery_batches": {
                    "batch_id", "summary_id", "payload_json", "created_at", "expires_at", "state",
                },
                "recovery_batch_items": {"occurrence_id", "batch_id"},
                "recovery_summary_receipts": {"summary_id", "source", "acknowledged_at"},
            }
            tables = {
                row[0] for row in src_conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            missing_tables = set(required_columns) - tables
            if missing_tables:
                raise ValueError(
                    "Invalid SQLite backup: required canonical tables are missing: "
                    f"{', '.join(sorted(missing_tables))}"
                )
            for table, required in required_columns.items():
                columns = {
                    row[1] for row in src_conn.execute(f"PRAGMA table_info({table})").fetchall()
                }
                missing = required - columns
                if missing:
                    raise ValueError(
                        f"Invalid SQLite backup: required table {table!r} is missing columns: "
                        f"{', '.join(sorted(missing))}"
                    )
        except sqlite3.DatabaseError as exc:
            raise ValueError(f"Invalid SQLite backup: integrity/schema validation failed: {exc}") from exc

    def restore(self, source_path: str | Path) -> bool:
        """Restores database from an online backup file."""
        source_str = str(source_path)
        if not Path(source_str).exists():
            raise FileNotFoundError(f"Backup file '{source_str}' not found")
        with self._lock:
            try:
                src_conn = sqlite3.connect(source_str)
            except sqlite3.DatabaseError as exc:
                raise ValueError(f"Invalid SQLite backup: unable to open database: {exc}") from exc
            try:
                self._validate_restore_source(src_conn)
                if self._conn is not None:
                    self._conn.close()
                    self._conn = None
                dest_conn = sqlite3.connect(self.db_path)
                try:
                    src_conn.backup(dest_conn)
                finally:
                    dest_conn.close()
            finally:
                src_conn.close()
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
