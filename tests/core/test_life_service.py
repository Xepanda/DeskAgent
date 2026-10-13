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


if __name__ == "__main__":
    unittest.main()
