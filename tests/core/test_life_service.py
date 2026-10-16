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

import tempfile
import unittest
from pathlib import Path
from host.desk_agent.services.life_service import LifeService


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


if __name__ == "__main__":
    unittest.main()
