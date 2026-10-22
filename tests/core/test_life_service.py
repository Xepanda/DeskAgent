"""
Comprehensive unit tests for LifeService (Reminders, Occurrences, Alarms, Notes, Outbox).
Covers:
- SQLite WAL transactions and restart persistence
- Full Reminder CRUD: create, get, list, update, delete
- Occurrence generation, due queries, and delivered tracking
- Snooze / stop_ringing / complete decoupling
- Notes CRUD and tag / query search
- Outbox persistence for proactive notifications
"""

from __future__ import annotations

import sqlite3
import os
import subprocess
import sys
import tempfile
import unittest
import gc
from unittest.mock import patch
from datetime import datetime, timezone, timedelta
from pathlib import Path
from host.desk_agent.services.life_service import LifeService, compute_next_occurrence


class TestLifeService(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_life.db"
        self.service = LifeService(self.db_path)

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Reminder CRUD & Listing
    # -------------------------------------------------------------------------
    def test_reminder_crud(self) -> None:
        # Create
        res = self.service.create_reminder(
            reminder_id="rem-001",
            title="Drink water",
            due_at="2026-10-14T10:00:00Z",
            timezone_str="Asia/Shanghai",
            priority="normal",
        )
        self.assertEqual(res["reminder_id"], "rem-001")
        self.assertEqual(res["status"], "scheduled")

        # Get
        item = self.service.get_reminder("rem-001")
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "Drink water")

        # Update
        upd = self.service.update_reminder(
            reminder_id="rem-001",
            title="Drink lemon water",
            due_at="2026-10-14T10:30:00Z",
        )
        self.assertEqual(upd["title"], "Drink lemon water")
        item2 = self.service.get_reminder("rem-001")
        self.assertEqual(item2["title"], "Drink lemon water")
        self.assertEqual(item2["due_at"], "2026-10-14T10:30:00Z")

        # List
        items = self.service.list_reminders()
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["reminder_id"], "rem-001")

        # Delete
        deleted = self.service.delete_reminder("rem-001")
        self.assertTrue(deleted)
        self.assertIsNone(self.service.get_reminder("rem-001"))

    def test_reminder_due_time_with_offset_is_normalized_to_utc(self) -> None:
        created = self.service.create_reminder(
            reminder_id="rem-offset-time",
            title="Local time reminder",
            due_at="2026-10-15T09:00:00+08:00",
            timezone_str="Asia/Shanghai",
        )

        self.assertEqual(created["due_at"], "2026-10-15T01:00:00Z")
        self.assertEqual(self.service.get_reminder("rem-offset-time")["due_at"], "2026-10-15T01:00:00Z")
        self.assertEqual(self.service.get_due_occurrences("2026-10-15T00:59:00Z"), [])
        self.assertEqual(len(self.service.get_due_occurrences("2026-10-15T01:01:00Z")), 1)

    def test_reminder_creation_rejects_unknown_timezone(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unknown timezone"):
            self.service.create_reminder(
                reminder_id="rem-invalid-timezone",
                title="Invalid timezone",
                due_at="2035-01-01T09:00:00Z",
                timezone_str="Mars/Olympus_Mons",
            )

        self.assertIsNone(self.service.get_reminder("rem-invalid-timezone"))

    # -------------------------------------------------------------------------
    # 2. Occurrence Scheduling & Due Triggering
    # -------------------------------------------------------------------------
    def test_occurrences_and_due_queries(self) -> None:
        self.service.create_reminder(
            reminder_id="rem-due-01",
            title="Time for pills",
            due_at="2026-10-14T08:00:00Z",
        )
        self.service.create_reminder(
            reminder_id="rem-future-01",
            title="Doctor appointment",
            due_at="2026-10-14T18:00:00Z",
        )

        # Query due at 09:00:00Z (should include rem-due-01 but not rem-future-01)
        due_items = self.service.get_due_occurrences("2026-10-14T09:00:00Z")
        self.assertEqual(len(due_items), 1)
        self.assertEqual(due_items[0]["reminder_id"], "rem-due-01")

        occ_id = due_items[0]["occurrence_id"]
        # Mark occurrence delivered
        self.service.mark_occurrence_delivered(occ_id)

        # Next check should have 0 pending due items
        due_items2 = self.service.get_due_occurrences("2026-10-14T09:00:00Z")
        self.assertEqual(len(due_items2), 0)

    # -------------------------------------------------------------------------
    # 3. Decoupling: Snooze vs Stop Ringing vs Complete
    # -------------------------------------------------------------------------
    def test_snooze_and_stop_ringing_decoupling(self) -> None:
        self.service.create_reminder(
            reminder_id="rem-alarm-01",
            title="Morning Alarm",
            due_at="2026-10-14T07:00:00Z",
        )

        # User presses robot head/button to stop ringing
        # stop_ringing must NOT complete the reminder
        occ_list = self.service.get_due_occurrences("2026-10-14T07:05:00Z")
        self.assertEqual(len(occ_list), 1)
        occ_id = occ_list[0]["occurrence_id"]
        self.service.trigger_occurrence_to_outbox(occ_id)

        self.service.stop_ringing(occ_id)
        rem = self.service.get_reminder("rem-alarm-01")
        self.assertNotEqual(rem["status"], "completed", "stop_ringing must NOT mark reminder completed")
        self.assertEqual(rem["status"], "ringing_stopped")

        # Snooze creates next occurrence and sets status to snoozed
        self.service.snooze_reminder("rem-alarm-01", snooze_minutes=15)
        rem_snoozed = self.service.get_reminder("rem-alarm-01")
        self.assertEqual(rem_snoozed["status"], "snoozed")

        # Explicit complete
        self.service.complete_reminder("rem-alarm-01")
        rem_completed = self.service.get_reminder("rem-alarm-01")
        self.assertEqual(rem_completed["status"], "completed")

    def test_stop_ringing_does_not_dismiss_pending_occurrence(self) -> None:
        self.service.create_reminder(
            reminder_id="rem-pending-stop",
            title="Future alert",
            due_at="2026-10-14T07:00:00Z",
        )
        occurrence_id = self.service.get_all_occurrences("rem-pending-stop")[0]["occurrence_id"]

        with self.assertRaisesRegex(ValueError, "not ringing"):
            self.service.stop_ringing(occurrence_id)

        self.assertEqual(self.service.get_all_occurrences("rem-pending-stop")[0]["state"], "pending")
        self.assertEqual(self.service.get_reminder("rem-pending-stop")["status"], "scheduled")

    def test_stop_ringing_ack_is_idempotent_for_dismissed_occurrence(self) -> None:
        occurrence_id = self._create_pending_reminder_alert("rem-repeat-ack")

        first = self.service.stop_ringing(occurrence_id)
        second = self.service.stop_ringing(occurrence_id)

        self.assertEqual(first, second)
        self.assertEqual(second["status"], "ringing_stopped")

    def _create_pending_reminder_alert(self, reminder_id: str) -> str:
        self.service.create_reminder(
            reminder_id=reminder_id,
            title=f"Alert {reminder_id}",
            due_at="2020-01-01T00:00:00Z",
        )
        occurrence_id = self.service.get_all_occurrences(reminder_id)[0]["occurrence_id"]
        self.assertIsNotNone(self.service.trigger_occurrence_to_outbox(occurrence_id))
        return occurrence_id

    def _outbox_state_for_occurrence(self, occurrence_id: str) -> str:
        with self.service._get_connection() as conn:
            row = conn.execute(
                "SELECT state FROM outbox WHERE event_type = 'reminder.alert' "
                "AND json_extract(payload_json, '$.occurrence_id') = ?",
                (occurrence_id,),
            ).fetchone()
        return row["state"]

    def test_stop_ringing_cancels_only_matching_pending_alert(self) -> None:
        occurrence_id = self._create_pending_reminder_alert("rem-dismiss-outbox")
        other_occurrence_id = self._create_pending_reminder_alert("rem-other-alert")

        self.service.stop_ringing(occurrence_id)

        self.assertEqual(self._outbox_state_for_occurrence(occurrence_id), "cancelled")
        self.assertEqual(self._outbox_state_for_occurrence(other_occurrence_id), "pending")

    def test_complete_reminder_cancels_its_pending_alert(self) -> None:
        occurrence_id = self._create_pending_reminder_alert("rem-complete-outbox")

        self.service.complete_reminder("rem-complete-outbox")

        self.assertEqual(self._outbox_state_for_occurrence(occurrence_id), "cancelled")

    def test_claimed_alert_is_cancelled_by_confirmation(self) -> None:
        occurrence_id = self._create_pending_reminder_alert("rem-claimed-outbox")
        with self.service._get_connection() as conn:
            outbox_id = conn.execute(
                "SELECT outbox_id FROM outbox WHERE event_type = 'reminder.alert' "
                "AND json_extract(payload_json, '$.occurrence_id') = ?",
                (occurrence_id,),
            ).fetchone()["outbox_id"]

        self.assertTrue(self.service.claim_reminder_alert(outbox_id))
        self.service.stop_ringing(occurrence_id)
        self.service.record_outbox_failure(outbox_id, "cancel raced with send")
        self.service.mark_outbox_dispatched(outbox_id)
        self.assertEqual(self._outbox_state_for_occurrence(occurrence_id), "cancelled")
        self.service.close()
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("UPDATE outbox SET state = 'dispatching' WHERE outbox_id = ?", (outbox_id,))
            conn.commit()
        finally:
            conn.close()
        self.service = LifeService(self.db_path)
        self.assertEqual(self._outbox_state_for_occurrence(occurrence_id), "cancelled")

    def test_unconfirmed_claimed_alert_is_retried_after_service_restart(self) -> None:
        occurrence_id = self._create_pending_reminder_alert("rem-claimed-restart")
        with self.service._get_connection() as conn:
            outbox_id = conn.execute(
                "SELECT outbox_id FROM outbox WHERE event_type = 'reminder.alert' "
                "AND json_extract(payload_json, '$.occurrence_id') = ?",
                (occurrence_id,),
            ).fetchone()["outbox_id"]
        self.assertTrue(self.service.claim_reminder_alert(outbox_id))
        self.service.close()
        self.service = LifeService(self.db_path)
        self.assertEqual(self._outbox_state_for_occurrence(occurrence_id), "pending")

    def test_forced_process_exit_preserves_due_reminder_and_recovers_claim(self) -> None:
        """A hard process exit must retain a due reminder and reclaim an unacked alert."""
        self.service.close()
        due_at = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat().replace("+00:00", "Z")
        create_script = r"""
import os, sys
from host.desk_agent.services.life_service import LifeService
service = LifeService(sys.argv[1])
service.create_reminder("rem-process-crash", "Crash recovery", sys.argv[2])
os._exit(0)
"""
        create = subprocess.run(
            [sys.executable, "-c", create_script, str(self.db_path), due_at],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(create.returncode, 0, create.stderr)

        self.service = LifeService(self.db_path)
        reminder = self.service.get_reminder("rem-process-crash")
        self.assertIsNotNone(reminder)
        due = self.service.get_due_occurrences()
        self.assertEqual(len(due), 1)
        self.assertTrue(due[0]["is_catchup"])
        occurrence_id = due[0]["occurrence_id"]
        alert = self.service.trigger_occurrence_to_outbox(occurrence_id)
        self.assertIsNotNone(alert)
        self.service.close()

        claim_script = r"""
import os, sys
from host.desk_agent.services.life_service import LifeService
service = LifeService(sys.argv[1])
items = service.get_pending_outbox()
alert = next(item for item in items if item["event_type"] == "reminder.alert")
assert service.claim_reminder_alert(alert["outbox_id"])
os._exit(0)
"""
        claim = subprocess.run(
            [sys.executable, "-c", claim_script, str(self.db_path)],
            cwd=Path(__file__).resolve().parents[2],
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(claim.returncode, 0, claim.stderr)

        self.service = LifeService(self.db_path)
        pending = self.service.get_pending_outbox()
        recovered = [item for item in pending if item["event_type"] == "reminder.alert"]
        self.assertEqual(len(recovered), 1)
        self.assertEqual(recovered[0]["payload"]["occurrence_id"], occurrence_id)
        self.assertEqual(self._outbox_state_for_occurrence(occurrence_id), "pending")
        occurrence = self.service.get_all_occurrences("rem-process-crash")[0]
        self.assertEqual(occurrence["state"], "ringing")

    def test_snooze_occurrence_is_idempotent_across_retry_and_restart(self) -> None:
        occurrence_id = self._create_pending_reminder_alert("rem-snooze-outbox")
        fixed_now = datetime(2026, 10, 22, 1, 2, 3, tzinfo=timezone.utc)

        class FrozenDateTime(datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)

        with patch("host.desk_agent.services.life_service.datetime", FrozenDateTime):
            first = self.service.snooze_reminder(
                "rem-snooze-outbox", snooze_minutes=10, occurrence_id=occurrence_id
            )
            second = self.service.snooze_reminder(
                "rem-snooze-outbox", snooze_minutes=10, occurrence_id=occurrence_id
            )

        self.assertEqual(self._outbox_state_for_occurrence(occurrence_id), "cancelled")
        self.assertEqual(first, second)
        self.assertEqual(len(self.service.get_all_occurrences("rem-snooze-outbox")), 2)

        self.service.close()
        self.service = LifeService(self.db_path)
        after_restart = self.service.snooze_reminder(
            "rem-snooze-outbox", snooze_minutes=10, occurrence_id=occurrence_id
        )
        self.assertEqual(after_restart, first)
        self.assertEqual(len(self.service.get_all_occurrences("rem-snooze-outbox")), 2)

    def test_snooze_rejects_non_positive_duration_without_mutating_reminder(self) -> None:
        self.service.create_reminder(
            reminder_id="rem-invalid-snooze",
            title="Future reminder",
            due_at="2030-01-01T00:00:00Z",
        )
        for minutes in (0, -1):
            with self.subTest(minutes=minutes), self.assertRaisesRegex(ValueError, "positive"):
                self.service.snooze_reminder("rem-invalid-snooze", snooze_minutes=minutes)

        self.assertEqual(self.service.get_reminder("rem-invalid-snooze")["status"], "scheduled")
        self.assertEqual(len(self.service.get_all_occurrences("rem-invalid-snooze")), 1)

    # -------------------------------------------------------------------------
    # 4. Outbox Notification Queue
    # -------------------------------------------------------------------------
    def test_outbox_queue(self) -> None:
        outbox_id = self.service.enqueue_outbox(
            event_type="reminder.triggered",
            payload={"reminder_id": "rem-001", "title": "Hydrate"},
        )
        self.assertIsNotNone(outbox_id)

        pending = self.service.get_pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["outbox_id"], outbox_id)
        self.assertEqual(pending[0]["event_type"], "reminder.triggered")

        # Mark dispatched
        self.service.mark_outbox_dispatched(outbox_id)
        pending_after = self.service.get_pending_outbox()
        self.assertEqual(len(pending_after), 0)

    def test_outbox_idempotency_preserves_existing_pending_item(self) -> None:
        payload = {"task_id": "task-1", "turn_id": "turn-1", "summary": "safe"}
        first = self.service.enqueue_outbox_once(
            "codex.completed", payload, idempotency_key="task-1:turn-1"
        )
        second = self.service.enqueue_outbox_once(
            "codex.completed", {**payload, "summary": "replayed"}, idempotency_key="task-1:turn-1"
        )
        self.assertEqual(first, second)
        pending = self.service.get_pending_outbox_messages()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["payload"]["summary"], "safe")

    def test_managed_notification_receipts_are_durable_deduplicated_and_lifecycle_separate(self) -> None:
        self.service.create_reminder(
            reminder_id="rem-managed-receipt",
            title="Managed receipt",
            due_at="2026-10-22T12:00:00Z",
        )
        occurrence_id = self.service.get_all_occurrences("rem-managed-receipt")[0]["occurrence_id"]
        self.service.trigger_occurrence_to_outbox(occurrence_id)
        fixed_now = "2026-10-22T12:00:00Z"
        pending = self.service.get_pending_outbox_messages(now_iso=fixed_now, include_delayed=True)
        item = next(row for row in pending if row["topic"] == "reminder.alert")
        notification = self.service.build_reminder_notification(item)
        self.assertEqual(notification["notification_id"], occurrence_id)
        self.assertEqual(notification["expires_at"], "2026-10-23T12:00:00Z")
        self.assertEqual(
            self.service.get_active_notification_contexts(now_iso=fixed_now)[occurrence_id],
            notification["expires_at"],
        )
        self.assertEqual(self.service.ensure_notification_receipt(notification, now_iso=fixed_now), "ready")

        accepted = {
            "type": "notification.status", "notification_id": occurrence_id,
            "status": "accepted",
        }
        self.assertEqual(self.service.record_managed_notification_event(accepted, now_iso=fixed_now), "recorded")
        self.assertEqual(self.service.record_managed_notification_event(accepted, now_iso=fixed_now), "duplicate")
        self.assertEqual(
            self.service.get_notification_receipt(occurrence_id)["delivery_status"], "unknown"
        )

        rendered = {"type": "notification.status", "notification_id": occurrence_id, "status": "rendered"}
        played = {"type": "notification.status", "notification_id": occurrence_id, "status": "played"}
        self.assertEqual(self.service.record_managed_notification_event(rendered, now_iso=fixed_now), "recorded")
        self.assertEqual(self.service.record_managed_notification_event(played, now_iso=fixed_now), "recorded")
        self.assertEqual(self.service.record_managed_notification_event(rendered, now_iso=fixed_now), "duplicate")
        conflict = {"type": "notification.status", "notification_id": occurrence_id, "status": "failed"}
        self.assertEqual(self.service.record_managed_notification_event(conflict, now_iso=fixed_now), "recorded")
        ack = {"type": "notification.acknowledged", "notification_id": occurrence_id, "source": "button"}
        self.assertEqual(self.service.record_managed_notification_event(ack, now_iso=fixed_now), "recorded")
        self.assertEqual(self.service.record_managed_notification_event(ack, now_iso=fixed_now), "duplicate")

        receipt = self.service.get_notification_receipt(occurrence_id)
        self.assertEqual(receipt["delivery_status"], "played")
        self.assertEqual(receipt["protocol_anomaly_count"], 1)
        self.assertEqual(receipt["user_acknowledged"], 1)
        self.assertEqual(receipt["ack_source"], "button")
        self.assertEqual(self._outbox_state_for_occurrence(occurrence_id), "pending")
        self.assertEqual(self.service.get_all_occurrences("rem-managed-receipt")[0]["state"], "ringing")

        self.service.close()
        self.service = LifeService(self.db_path)
        restored = self.service.get_notification_receipt(occurrence_id)
        self.assertEqual(restored["delivery_status"], "played")
        self.assertEqual(restored["user_acknowledged"], 1)
        self.assertEqual(self.service.record_managed_notification_event(ack, now_iso=fixed_now), "duplicate")

    def test_managed_notification_events_after_expiry_are_ignored(self) -> None:
        self.service.create_reminder(
            reminder_id="rem-managed-expiry",
            title="Expiry receipt",
            due_at="2026-10-22T12:00:00Z",
        )
        occurrence_id = self.service.get_all_occurrences("rem-managed-expiry")[0]["occurrence_id"]
        self.service.trigger_occurrence_to_outbox(occurrence_id)
        event = {"type": "notification.acknowledged", "notification_id": occurrence_id, "source": "button"}
        self.assertEqual(
            self.service.record_managed_notification_event(event, now_iso="2026-10-23T12:00:01Z"),
            "ignored",
        )
        self.assertIsNone(self.service.get_notification_receipt(occurrence_id))

    def test_outbox_managed_failure_can_remain_pending_after_retry_limit(self) -> None:
        outbox_id = self.service.enqueue_outbox_once(
            "codex.failed", {"task_id": "task-2"}, idempotency_key="task-2:turn-2"
        )
        for _ in range(6):
            self.service.record_outbox_failure(
                outbox_id, "device unavailable", max_retries=1,
                backoff_seconds=1, keep_pending=True,
            )
        row = self.service._get_connection().execute(
            "SELECT state, retry_count FROM outbox WHERE outbox_id = ?", (outbox_id,)
        ).fetchone()
        self.assertEqual(row["state"], "pending")
        self.assertEqual(row["retry_count"], 6)

    # -------------------------------------------------------------------------
    # 5. Notes CRUD & Search
    # -------------------------------------------------------------------------
    def test_notes_crud_and_search(self) -> None:
        self.service.create_note(
            note_id="note-01",
            title="Groceries",
            content="Apples, milk, bread",
            tags=["shopping", "home"],
        )
        self.service.create_note(
            note_id="note-02",
            title="Book recommendations",
            content="Designing Data-Intensive Applications",
            tags=["reading", "tech"],
        )

        # Get
        n1 = self.service.get_note("note-01")
        self.assertIsNotNone(n1)
        self.assertIn("shopping", n1["tags"])

        # List all
        all_notes = self.service.list_notes()
        self.assertEqual(len(all_notes), 2)

        # Search by query
        found = self.service.list_notes(query="milk")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["note_id"], "note-01")

        # Search by tag
        tagged = self.service.list_notes(tag="tech")
        self.assertEqual(len(tagged), 1)
        self.assertEqual(tagged[0]["note_id"], "note-02")

        # Delete
        self.assertTrue(self.service.delete_note("note-01"))
        self.assertIsNone(self.service.get_note("note-01"))

    # -------------------------------------------------------------------------
    # 6. Service Restart Recovery (WAL Persistence)
    # -------------------------------------------------------------------------
    def test_restart_recovery(self) -> None:
        self.service.create_reminder("rem-persist-01", "Persistent Task", "2026-10-15T00:00:00Z")
        self.service.create_note("note-persist-01", "Persistent Note", "Data survives restart")
        self.service.enqueue_outbox("test.event", {"hello": "world"})

        # Close and instantiate brand new LifeService instance pointing to same file
        self.service.close()
        new_instance = LifeService(self.db_path)
        rem = new_instance.get_reminder("rem-persist-01")
        self.assertIsNotNone(rem)
        self.assertEqual(rem["title"], "Persistent Task")

        note = new_instance.get_note("note-persist-01")
        self.assertIsNotNone(note)
        self.assertEqual(note["title"], "Persistent Note")

        pending_outbox = new_instance.get_pending_outbox()
        self.assertEqual(len(pending_outbox), 1)
        new_instance.close()

    def test_snooze_idempotency_migration_preserves_legacy_database(self) -> None:
        legacy_path = Path(self.temp_dir.name) / "legacy_life.db"
        conn = sqlite3.connect(legacy_path)
        try:
            conn.executescript(
                """
                CREATE TABLE reminders (
                    reminder_id TEXT PRIMARY KEY, title TEXT NOT NULL, due_at TEXT NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Shanghai', repeat_rule TEXT,
                    priority TEXT NOT NULL DEFAULT 'normal', status TEXT NOT NULL DEFAULT 'scheduled',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE occurrences (
                    occurrence_id TEXT PRIMARY KEY, reminder_id TEXT NOT NULL,
                    scheduled_at TEXT NOT NULL, delivered_at TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    FOREIGN KEY (reminder_id) REFERENCES reminders(reminder_id) ON DELETE CASCADE
                );
                INSERT INTO reminders VALUES (
                    'legacy-reminder', 'Legacy', '2026-10-15T08:00:00Z', 'UTC', NULL,
                    'normal', 'ringing', '2026-10-15T08:00:00Z', '2026-10-15T08:00:00Z'
                );
                INSERT INTO occurrences VALUES (
                    'legacy-occurrence', 'legacy-reminder', '2026-10-15T08:00:00Z', NULL, 'ringing'
                );
                """
            )
            conn.commit()
        finally:
            conn.close()

        migrated = LifeService(legacy_path)
        try:
            result = migrated.snooze_reminder(
                "legacy-reminder", snooze_minutes=5, occurrence_id="legacy-occurrence"
            )
            retry = migrated.snooze_reminder(
                "legacy-reminder", snooze_minutes=5, occurrence_id="legacy-occurrence"
            )
            self.assertEqual(retry, result)
            self.assertEqual(len(migrated.get_all_occurrences("legacy-reminder")), 2)
        finally:
            migrated.close()

    # -------------------------------------------------------------------------
    # 7. Recurrence Rules & Automatic Next Occurrence
    # -------------------------------------------------------------------------
    def test_repeat_rule_daily_auto_occurrence(self) -> None:
        """Verifies daily repeating reminder automatically creates next pending occurrence upon ringing."""
        self.service.create_reminder(
            reminder_id="rem-repeat-daily",
            title="Daily Standup",
            due_at="2026-10-15T09:30:00Z",
            repeat_rule="daily",
        )

        due = self.service.get_due_occurrences("2026-10-15T10:00:00Z")
        self.assertEqual(len(due), 1)
        occ0 = due[0]
        self.assertEqual(occ0["reminder_id"], "rem-repeat-daily")

        # Ring occurrence 0 -> must automatically generate next occurrence for +1 day
        self.service.mark_occurrence_ringing(occ0["occurrence_id"])

        all_occs = self.service.get_all_occurrences("rem-repeat-daily")
        self.assertEqual(len(all_occs), 2)

        # Occurrence 0 is ringing
        self.assertEqual(all_occs[0]["state"], "ringing")

        # Occurrence 1 is pending for next day at 09:30:00Z
        occ1 = all_occs[1]
        self.assertEqual(occ1["state"], "pending")
        self.assertEqual(occ1["scheduled_at"], "2026-10-16T09:30:00Z")

        # Reminder due_at is updated to next day
        rem = self.service.get_reminder("rem-repeat-daily")
        self.assertEqual(rem["due_at"], "2026-10-16T09:30:00Z")

    def test_repeat_rule_interval_and_weekly(self) -> None:
        """Verifies interval:30 and weekly recurrence calculation."""
        # Interval: 30 minutes
        self.service.create_reminder(
            reminder_id="rem-repeat-interval",
            title="Drink Water",
            due_at="2026-10-15T10:00:00Z",
            repeat_rule="interval:30",
        )
        due = self.service.get_due_occurrences("2026-10-15T10:05:00Z")
        self.service.mark_occurrence_ringing(due[0]["occurrence_id"])
        occs = self.service.get_all_occurrences("rem-repeat-interval")
        self.assertEqual(len(occs), 2)
        self.assertEqual(occs[1]["scheduled_at"], "2026-10-15T10:30:00Z")

        # Weekly
        self.service.create_reminder(
            reminder_id="rem-repeat-weekly",
            title="Weekly Review",
            due_at="2026-10-15T10:00:00Z",
            repeat_rule="weekly",
        )
        due_wk = self.service.get_due_occurrences("2026-10-15T10:05:00Z")
        self.service.mark_occurrence_ringing(due_wk[0]["occurrence_id"])
        occs_wk = self.service.get_all_occurrences("rem-repeat-weekly")
        self.assertEqual(len(occs_wk), 2)
        self.assertEqual(occs_wk[1]["scheduled_at"], "2026-10-22T10:00:00Z")

    # -------------------------------------------------------------------------
    # 8. Missed Reminder Catch-up & Expiry Policy
    # -------------------------------------------------------------------------
    def test_missed_reminder_catchup_flag(self) -> None:
        """Verifies reminders scheduled long in the past during power off are flagged with is_catchup."""
        # 1. Past due reminder from 4 hours ago (power off scenario)
        self.service.create_reminder(
            reminder_id="rem-missed-01",
            title="Missed Alert",
            due_at="2026-10-15T06:00:00Z",
        )
        # 2. Freshly due reminder from 10 seconds ago
        self.service.create_reminder(
            reminder_id="rem-fresh-01",
            title="Fresh Alert",
            due_at="2026-10-15T09:59:50Z",
        )

        due = self.service.get_due_occurrences("2026-10-15T10:00:00Z")
        due_map = {item["reminder_id"]: item for item in due}

        self.assertTrue(due_map["rem-missed-01"]["is_catchup"])
        self.assertGreater(due_map["rem-missed-01"]["delay_seconds"], 14000)

        self.assertFalse(due_map["rem-fresh-01"]["is_catchup"])
        self.assertLess(due_map["rem-fresh-01"]["delay_seconds"], 60)

    def test_very_old_one_shot_reminder_expires_after_restart(self) -> None:
        """A one-shot missed by more than the 24-hour recovery window is not replayed."""
        self.service.create_reminder(
            reminder_id="rem-expired-one-shot",
            title="Old one-shot",
            due_at="2026-10-14T08:00:00Z",
        )

        due = self.service.get_due_occurrences("2026-10-15T09:00:00Z")

        self.assertEqual(due, [])
        self.assertEqual(self.service.get_all_occurrences("rem-expired-one-shot")[0]["state"], "expired")
        self.assertEqual(self.service.get_reminder("rem-expired-one-shot")["status"], "missed")

    def test_expired_recurring_reminder_resumes_at_next_future_occurrence(self) -> None:
        """After long downtime, recurring reminders skip stale alerts and resume their cadence."""
        self.service.create_reminder(
            reminder_id="rem-expired-daily",
            title="Daily medicine",
            due_at="2026-10-14T08:00:00Z",
            repeat_rule="daily",
            timezone_str="UTC",
        )

        due = self.service.get_due_occurrences("2026-10-15T09:00:00Z")

        self.assertEqual(due, [])
        occurrences = self.service.get_all_occurrences("rem-expired-daily")
        self.assertEqual(occurrences[0]["state"], "expired")
        pending = [item for item in occurrences if item["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["scheduled_at"], "2026-10-16T08:00:00Z")

    # -------------------------------------------------------------------------
    # 9. Online SQLite Backup & Restore
    # -------------------------------------------------------------------------
    def test_online_sqlite_backup_and_restore(self) -> None:
        """Verifies atomic online backup and full restore of LifeService database."""
        self.service.create_reminder("rem-bk-01", "Backup Item 1", "2026-10-15T12:00:00Z")
        self.service.create_note("note-bk-01", "Backup Note", "Important Content")

        backup_file = Path(self.temp_dir.name) / "life_backup.db"
        self.service.backup(backup_file)
        self.assertTrue(backup_file.exists())
        self.assertGreater(backup_file.stat().st_size, 0)

        # Mutate current DB
        self.service.delete_reminder("rem-bk-01")
        self.assertIsNone(self.service.get_reminder("rem-bk-01"))

        # Restore from backup
        self.service.restore(backup_file)
        restored = self.service.get_reminder("rem-bk-01")
        self.assertIsNotNone(restored)
        self.assertEqual(restored["title"], "Backup Item 1")

    # -------------------------------------------------------------------------
    # 10. Outbox Exponential Backoff & Dead-Letter State
    # -------------------------------------------------------------------------
    def test_outbox_backoff_and_dead_letter(self) -> None:
        """Verifies repeated outbox delivery failure transitions to dead-letter 'failed' state."""
        msg_id = self.service.enqueue_outbox("test.failing.event", {"target": "robot"})

        for i in range(1, 5):
            self.service.record_outbox_failure(msg_id, "Channel unavailable", max_retries=5, backoff_seconds=5)
            # Immediate worker query excludes it due to backoff
            active_ready = self.service.get_pending_outbox()
            self.assertEqual(len(active_ready), 0, "Backoff must delay immediate retry")

            # Query including delayed shows message with incremented retry count
            pending = self.service.get_pending_outbox(include_delayed=True)
            self.assertEqual(len(pending), 1)
            self.assertEqual(pending[0]["retry_count"], i)

        # 5th failure -> state becomes failed
        self.service.record_outbox_failure(msg_id, "Channel unavailable", max_retries=5, backoff_seconds=5)
        pending_after = self.service.get_pending_outbox(include_delayed=True)
        self.assertEqual(len(pending_after), 0, "Failed message must transition out of pending state into failed")

    def test_reminder_delivery_retries_until_expiry_then_is_not_replayed(self) -> None:
        """Robot-offline alerts retry, but are discarded after the 24-hour reminder window."""
        self.service.create_reminder(
            "offline-reminder", "Offline reminder", "2026-10-14T08:00:00Z"
        )
        occurrence_id = self.service.get_due_occurrences("2026-10-14T08:05:00Z")[0]["occurrence_id"]
        message_id = self.service.trigger_occurrence_to_outbox(occurrence_id)["outbox_id"]
        for _ in range(8):
            self.service.record_outbox_failure(message_id, "Robot disconnected", backoff_seconds=1)

        pending = self.service.get_pending_outbox_messages(
            now_iso="2026-10-14T09:00:00Z", include_delayed=True
        )
        self.assertEqual(len(pending), 1, "Reminder delivery should remain queued through transient disconnection")

        expired = self.service.get_pending_outbox_messages(
            now_iso="2026-10-15T09:00:00Z", include_delayed=True
        )
        self.assertEqual(expired, [])
        row = self.service._get_connection().execute(
            "SELECT state FROM outbox WHERE outbox_id = ?", (message_id,)
        ).fetchone()
        self.assertEqual(row["state"], "expired")
        self.assertEqual(self.service.get_all_occurrences("offline-reminder")[0]["state"], "expired")
        self.assertEqual(self.service.get_reminder("offline-reminder")["status"], "missed")


    # -------------------------------------------------------------------------
    # 11. Atomic trigger_occurrence_to_outbox
    # -------------------------------------------------------------------------
    def test_trigger_occurrence_to_outbox_atomic_success(self) -> None:
        self.service.create_reminder(
            reminder_id="rem-atomic-01",
            title="Atomic Pill",
            due_at="2026-10-15T08:00:00Z",
        )
        due = self.service.get_due_occurrences("2026-10-15T09:00:00Z")
        self.assertEqual(len(due), 1)
        occ_id = due[0]["occurrence_id"]

        result = self.service.trigger_occurrence_to_outbox(occ_id, topic="reminder.alert", is_catchup=False)
        self.assertIsNotNone(result)
        self.assertEqual(result["occurrence_id"], occ_id)
        self.assertEqual(result["reminder_id"], "rem-atomic-01")

        # Occurrence state is ringing
        all_occs = self.service.get_all_occurrences("rem-atomic-01")
        self.assertEqual(len(all_occs), 1)
        self.assertEqual(all_occs[0]["state"], "ringing")

        # Reminder status is ringing
        rem = self.service.get_reminder("rem-atomic-01")
        self.assertEqual(rem["status"], "ringing")

        # Outbox message is created
        pending = self.service.get_pending_outbox_messages(now_iso="2026-10-15T09:00:00Z")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["topic"], "reminder.alert")
        self.assertEqual(pending[0]["payload"]["reminder_id"], "rem-atomic-01")
        self.assertEqual(pending[0]["payload"]["occurrence_id"], occ_id)
        self.assertEqual(pending[0]["payload"]["title"], "Atomic Pill")
        self.assertFalse(pending[0]["payload"]["is_catchup"])

    def test_trigger_occurrence_to_outbox_does_not_replay_non_pending_occurrence(self) -> None:
        """A scheduler retry after a committed trigger must not enqueue a duplicate alert."""
        self.service.create_reminder(
            reminder_id="rem-no-replay",
            title="One alert only",
            due_at="2026-10-15T08:00:00Z",
        )
        due = self.service.get_due_occurrences("2026-10-15T09:00:00Z")
        occurrence_id = due[0]["occurrence_id"]

        first = self.service.trigger_occurrence_to_outbox(occurrence_id)
        second = self.service.trigger_occurrence_to_outbox(occurrence_id)

        self.assertIsNotNone(first)
        self.assertIsNone(second)
        pending = self.service.get_pending_outbox_messages(now_iso="2026-10-15T09:00:00Z")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["payload"]["occurrence_id"], occurrence_id)

    def test_trigger_occurrence_to_outbox_recurring_schedules_next(self) -> None:
        self.service.create_reminder(
            reminder_id="rem-atomic-rec",
            title="Daily Exercise",
            due_at="2026-10-15T07:00:00Z",
            repeat_rule="daily",
            timezone_str="Asia/Shanghai",
        )
        due = self.service.get_due_occurrences("2026-10-15T07:05:00Z")
        occ_id = due[0]["occurrence_id"]

        result = self.service.trigger_occurrence_to_outbox(occ_id, topic="reminder.alert", is_catchup=False)
        self.assertIsNotNone(result)

        occs = self.service.get_all_occurrences("rem-atomic-rec")
        self.assertEqual(len(occs), 2)
        self.assertEqual(occs[0]["state"], "ringing")
        self.assertEqual(occs[1]["state"], "pending")
        self.assertEqual(occs[1]["scheduled_at"], "2026-10-16T07:00:00Z")

        pending = self.service.get_pending_outbox_messages(now_iso="2026-10-15T09:00:00Z")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["payload"]["reminder_id"], "rem-atomic-rec")

    def test_catchup_recurring_alert_skips_missed_intervals(self) -> None:
        due_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat().replace("+00:00", "Z")
        self.service.create_reminder(
            reminder_id="rem-interval-catchup",
            title="Hydration interval",
            due_at=due_at,
            repeat_rule="interval:30",
            timezone_str="UTC",
        )
        occurrence_id = self.service.get_all_occurrences("rem-interval-catchup")[0]["occurrence_id"]

        self.service.trigger_occurrence_to_outbox(occurrence_id, is_catchup=True)

        pending = [item for item in self.service.get_all_occurrences("rem-interval-catchup") if item["state"] == "pending"]
        self.assertEqual(len(pending), 1)
        self.assertGreater(
            datetime.fromisoformat(pending[0]["scheduled_at"].replace("Z", "+00:00")),
            datetime.now(timezone.utc),
            "A catch-up must not cause a rapid burst of each missed interval",
        )

    def test_trigger_occurrence_to_outbox_atomic_rollback_on_failure(self) -> None:
        """Verifies that an error during triggering rolls back ringing state and does NOT leave orphaned records."""
        self.service.create_reminder(
            reminder_id="rem-rollback-01",
            title="Rollback Test",
            due_at="2026-10-15T08:00:00Z",
        )
        due = self.service.get_due_occurrences("2026-10-15T09:00:00Z")
        occ_id = due[0]["occurrence_id"]

        original_conn = self.service._get_connection()
        original_conn.execute("""
            CREATE TRIGGER fail_outbox_insert BEFORE INSERT ON outbox
            BEGIN
                SELECT RAISE(FAIL, 'Simulated disk/power failure during outbox insert');
            END;
        """)

        try:
            with self.assertRaises(sqlite3.DatabaseError):
                self.service.trigger_occurrence_to_outbox(occ_id)
        finally:
            original_conn.execute("DROP TRIGGER IF EXISTS fail_outbox_insert;")

        # Verify ATOMIC ROLLBACK:
        # Occurrence MUST still be 'pending' (NOT orphaned into 'ringing' without outbox message!)
        all_occs = self.service.get_all_occurrences("rem-rollback-01")
        self.assertEqual(len(all_occs), 1)
        self.assertEqual(all_occs[0]["state"], "pending")

        # Reminder MUST still be 'scheduled'
        rem = self.service.get_reminder("rem-rollback-01")
        self.assertEqual(rem["status"], "scheduled")

        # Outbox MUST have 0 records
        pending = self.service.get_pending_outbox_messages()
        self.assertEqual(len(pending), 0)

    # -------------------------------------------------------------------------
    # 12. Timezone-Aware Recurrence
    # -------------------------------------------------------------------------
    def test_compute_next_occurrence_timezone_aware(self) -> None:
        from host.desk_agent.services.life_service import compute_next_occurrence

        # 1. DST transition test: America/New_York (clocks spring forward 1 hour on 2026-03-08)
        # 2026-03-07 09:00 EST is 2026-03-07 14:00:00 UTC
        # With tz_name="America/New_York", daily recurrence should remain at 09:00 local EDT (2026-03-08 13:00:00 UTC)
        next_dst = compute_next_occurrence("2026-03-07T14:00:00Z", "daily", tz_name="America/New_York")
        self.assertEqual(next_dst, "2026-03-08T13:00:00Z")

        # Without timezone (UTC naive step), +24h gives 14:00:00 UTC
        next_utc = compute_next_occurrence("2026-03-07T14:00:00Z", "daily")
        self.assertEqual(next_utc, "2026-03-08T14:00:00Z")

        # 2. Weekday calculation in local timezone:
        next_sh = compute_next_occurrence("2026-10-16T08:00:00Z", "daily", tz_name="Asia/Shanghai")
        self.assertEqual(next_sh, "2026-10-17T08:00:00Z")

        # 3. Graceful fallback for invalid timezone:
        next_fallback = compute_next_occurrence("2026-10-16T08:00:00Z", "daily", tz_name="Invalid/Unknown_Zone")
        self.assertEqual(next_fallback, "2026-10-17T08:00:00Z")

        # 4. Keyword argument backwards compatibility with timezone_str
        next_kw = compute_next_occurrence("2026-03-07T14:00:00Z", "daily", timezone_str="America/New_York")
        self.assertEqual(next_kw, "2026-03-08T13:00:00Z")

        # Elapsed-time schedules remain hourly across the autumn DST fold.
        # 01:30 EDT is 05:30 UTC; the next hourly occurrence is 06:30 UTC
        # (01:30 EST), not 07:30 UTC (02:30 EST).
        next_hourly_fold = compute_next_occurrence(
            "2026-11-01T05:30:00Z", "hourly", tz_name="America/New_York"
        )
        self.assertEqual(next_hourly_fold, "2026-11-01T06:30:00Z")

    def test_compute_next_occurrence_supports_contract_rrule_intervals(self) -> None:
        daily = compute_next_occurrence(
            "2026-10-15T08:00:00Z", "FREQ=DAILY;INTERVAL=2", tz_name="Asia/Shanghai"
        )
        weekly = compute_next_occurrence(
            "2026-10-15T08:00:00Z", "FREQ=WEEKLY;INTERVAL=2", tz_name="Asia/Shanghai"
        )

        self.assertEqual(daily, "2026-10-17T08:00:00Z")
        self.assertEqual(weekly, "2026-10-29T08:00:00Z")

    def test_new_york_fallback_preserves_second_fold_wall_time(self) -> None:
        """The bundled Windows fallback must distinguish the repeated 01:30 after DST ends."""
        from host.desk_agent.services.life_service import compute_next_occurrence

        with patch(
            "host.desk_agent.services.life_service.zoneinfo.ZoneInfo",
            side_effect=KeyError("tzdata unavailable"),
        ):
            next_daily = compute_next_occurrence(
                "2026-11-01T06:30:00Z", "daily", tz_name="America/New_York"
            )

        # 06:30Z is the second 01:30 (EST). The next daily occurrence is
        # 01:30 EST on Nov 2, which is still 06:30Z.
        self.assertEqual(next_daily, "2026-11-02T06:30:00Z")

    # -------------------------------------------------------------------------
    # 13. LifeService Close & Context Manager
    # -------------------------------------------------------------------------
    def test_life_service_close_and_context_manager(self) -> None:
        # 1. Explicit close sets _conn to None
        svc = LifeService(self.db_path)
        self.assertIsNotNone(svc._get_connection())
        svc.close()
        self.assertIsNone(svc._conn)

        # 2. Context manager support
        with LifeService(self.db_path) as s:
            s.create_reminder("rem-cm-01", "Context Test", "2026-10-15T12:00:00Z")
            self.assertIsNotNone(s._conn)
        self.assertIsNone(s._conn)

    def test_discarded_service_releases_sqlite_file_without_gc(self) -> None:
        """Dropping a service must release its SQLite handle on Windows without a GC sweep."""
        if os.name != "nt":
            self.skipTest("Windows prevents unlinking an open SQLite database")
        db_path = Path(self.temp_dir.name) / "discarded_service.db"
        service = LifeService(db_path)
        service.create_reminder("rem-drop", "Release handle", "2026-10-15T12:00:00Z")

        del service
        try:
            db_path.unlink()
        except PermissionError:
            gc.collect()
            if db_path.exists():
                db_path.unlink()
            self.fail("LifeService leaked its SQLite connection until cyclic garbage collection")


if __name__ == "__main__":
    unittest.main()

