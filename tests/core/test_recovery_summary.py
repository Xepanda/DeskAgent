from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfoNotFoundError

from host.desk_agent.services import life_service as life_service_module
from host.desk_agent.services.life_service import LifeService


class TestRecoverySummary(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "life.db"
        self.service = LifeService(self.db_path)

    def tearDown(self) -> None:
        self.service.close()
        self.temp_dir.cleanup()

    def _reminder(self, reminder_id: str, scheduled_at: str, timezone_name: str = "Asia/Shanghai") -> str:
        self.service.create_reminder(
            reminder_id=reminder_id,
            title=f"Title {reminder_id}",
            due_at=scheduled_at,
            timezone_str=timezone_name,
        )
        return f"occ-{reminder_id}-0"

    def test_zoneinfo_uses_only_the_locked_tzdata_package_path(self) -> None:
        with patch.object(
            life_service_module.zoneinfo,
            "reset_tzpath",
            wraps=life_service_module.zoneinfo.reset_tzpath,
        ) as reset_tzpath:
            life_service_module._pin_zoneinfo_to_locked_tzdata()

        reset_tzpath.assert_called_once_with(())
        self.assertEqual(life_service_module.zoneinfo.TZPATH, ())

    def test_scan_batches_only_due_uncompleted_occurrences_inside_inclusive_24h_window(self) -> None:
        boundary = self._reminder("boundary", "2026-10-22T12:00:00Z", "Asia/Shanghai")
        expired = self._reminder("expired", "2026-10-22T11:59:59Z")
        future = self._reminder("future", "2026-10-23T12:00:01Z")
        completed = self._reminder("completed", "2026-10-23T11:00:00Z")
        self.service.complete_reminder("completed", completed)

        batch = self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:00:00Z")

        self.assertIsNotNone(batch)
        self.assertEqual([item["occurrence_id"] for item in batch["payload"]["items"]], [boundary])
        self.assertEqual(batch["payload"]["items"][0]["timezone"], "Asia/Shanghai")
        self.assertEqual(batch["payload"]["items"][0]["display_time"], "2026-10-22 20:00 UTC+08:00")
        self.assertEqual(self.service.get_all_occurrences("expired")[0]["state"], "expired")
        self.assertEqual(self.service.get_all_occurrences("future")[0]["state"], "pending")
        self.assertEqual(self.service.get_all_occurrences("completed")[0]["state"], "completed")
        self.assertEqual(self.service.get_due_occurrences("2026-10-23T12:00:00Z"), [])
        self.assertIsNone(self.service.trigger_occurrence_to_outbox(boundary))
        outbox = self.service.get_pending_outbox_messages(now_iso="2026-10-23T12:00:00Z")
        self.assertEqual([message["topic"] for message in outbox], ["notification.summary.deliver"])

    def test_scan_excludes_cancelled_or_archived_reminders_even_if_occurrence_is_pending(self) -> None:
        self._reminder("cancelled", "2026-10-23T11:00:00Z")
        self._reminder("archived", "2026-10-23T11:30:00Z")
        conn = self.service._get_connection()
        conn.execute("UPDATE reminders SET status = 'cancelled' WHERE reminder_id = 'cancelled'")
        conn.execute("UPDATE reminders SET status = 'archived' WHERE reminder_id = 'archived'")
        conn.commit()

        batch = self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:00:00Z")

        self.assertIsNone(batch)
        self.assertEqual(self.service.get_all_occurrences("cancelled")[0]["state"], "pending")
        self.assertEqual(self.service.get_all_occurrences("archived")[0]["state"], "pending")

    def test_summary_orders_utc_limits_to_twenty_and_counts_omitted_exactly(self) -> None:
        for index in range(25):
            self._reminder(
                f"item-{index:02d}",
                f"2026-10-23T{index // 60:02d}:{index % 60:02d}:00Z",
                "UTC" if index % 2 else "Asia/Shanghai",
            )

        batch = self.service.create_recovery_summary_batch(now_iso="2026-10-23T01:00:00Z")

        self.assertEqual(len(batch["payload"]["items"]), 20)
        self.assertEqual(batch["payload"]["omitted_count"], 5)
        scheduled = [item["scheduled_at"] for item in batch["payload"]["items"]]
        self.assertEqual(scheduled, sorted(scheduled))
        self.assertEqual(len({item["timezone"] for item in batch["payload"]["items"]}), 2)

    def test_display_time_uses_utc_and_shanghai_zone_offsets(self) -> None:
        self._reminder("display-utc", "2026-10-23T00:30:00Z", "UTC")
        self._reminder("display-shanghai", "2026-10-23T00:30:00Z", "Asia/Shanghai")

        batch = self.service.create_recovery_summary_batch(now_iso="2026-10-23T01:00:00Z")

        by_id = {item["reminder_id"]: item for item in batch["payload"]["items"]}
        self.assertEqual(by_id["display-utc"]["display_time"], "2026-10-23 00:30 UTC+00:00")
        self.assertEqual(by_id["display-shanghai"]["display_time"], "2026-10-23 08:30 UTC+08:00")
        self.assertEqual(by_id["display-shanghai"]["timezone"], "Asia/Shanghai")

    def test_display_time_computes_new_york_dst_offset_at_each_scheduled_instant(self) -> None:
        # The fall-back hour occurs twice; the explicit offset makes the labels distinct.
        self._reminder("dst-daylight", "2026-11-01T05:30:00Z", "America/New_York")
        self._reminder("dst-standard", "2026-11-01T06:30:00Z", "America/New_York")

        batch = self.service.create_recovery_summary_batch(now_iso="2026-11-01T07:00:00Z")

        by_id = {item["reminder_id"]: item for item in batch["payload"]["items"]}
        self.assertEqual(by_id["dst-daylight"]["display_time"], "2026-11-01 01:30 UTC-04:00")
        self.assertEqual(by_id["dst-standard"]["display_time"], "2026-11-01 01:30 UTC-05:00")

    def test_display_time_tracks_new_york_spring_forward_transition(self) -> None:
        self._reminder("dst-before", "2026-03-08T06:30:00Z", "America/New_York")
        self._reminder("dst-after", "2026-03-08T07:30:00Z", "America/New_York")

        batch = self.service.create_recovery_summary_batch(now_iso="2026-03-08T08:00:00Z")

        by_id = {item["reminder_id"]: item for item in batch["payload"]["items"]}
        self.assertEqual(by_id["dst-before"]["display_time"], "2026-03-08 01:30 UTC-05:00")
        self.assertEqual(by_id["dst-after"]["display_time"], "2026-03-08 03:30 UTC-04:00")

    def test_unavailable_iana_database_fails_closed_without_consuming_occurrences(self) -> None:
        occurrence_id = self._reminder("no-tzdb", "2026-10-23T11:00:00Z", "Asia/Shanghai")

        with patch("host.desk_agent.services.life_service.zoneinfo.ZoneInfo", side_effect=ZoneInfoNotFoundError("missing tzdb")):
            with self.assertRaises(ZoneInfoNotFoundError):
                self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:00:00Z")

        self.assertEqual(self.service.get_all_occurrences("no-tzdb")[0]["state"], "pending")
        due = self.service.get_due_occurrences("2026-10-23T12:00:00Z")
        self.assertEqual([item["occurrence_id"] for item in due], [occurrence_id])
        conn = self.service._get_connection()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM recovery_batches").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)

    def test_rescan_and_restart_reuse_frozen_batch_payload_and_summary_id(self) -> None:
        self._reminder("stable", "2026-10-23T11:00:00Z")
        first = self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:00:00Z")
        self.service.close()
        self.service = LifeService(self.db_path)

        replay = self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:05:00Z")

        self.assertEqual(replay["batch_id"], first["batch_id"])
        self.assertEqual(replay["payload"], first["payload"])
        self.assertEqual(replay["outbox_id"], first["outbox_id"])
        self.assertEqual(len(self.service.get_pending_outbox_messages(now_iso="2026-10-23T12:05:00Z")), 1)

    def test_summary_batch_has_deterministic_ids_and_exact_24_hour_ttl(self) -> None:
        self._reminder("identity", "2026-10-23T11:00:00Z")

        first = self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:00:00+00:00")
        again = self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:00:00Z")

        self.assertEqual(first["summary_id"], again["summary_id"])
        self.assertRegex(first["summary_id"], r"^[A-Za-z0-9._:-]+$")
        self.assertEqual(first["payload"]["expires_at"], "2026-10-24T12:00:00Z")
        self.assertEqual(first["payload"]["created_at"], "2026-10-23T12:00:00Z")

    def test_summary_ack_is_idempotent_and_does_not_change_occurrence(self) -> None:
        occurrence_id = self._reminder("ack", "2026-10-23T11:00:00Z")
        batch = self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:00:00Z")
        before = self.service.get_all_occurrences("ack")[0]

        event = {"type": "notification.acknowledged", "summary_id": batch["summary_id"], "source": "button"}
        first = self.service.record_recovery_summary_ack(event, now_iso="2026-10-23T12:01:00Z")
        duplicate = self.service.record_recovery_summary_ack(event, now_iso="2026-10-23T12:02:00Z")

        self.assertEqual(first, "recorded")
        self.assertEqual(duplicate, "duplicate")
        self.assertEqual(self.service.get_all_occurrences("ack")[0], before)
        self.assertEqual(before["occurrence_id"], occurrence_id)
        receipt = self.service.get_recovery_summary_receipt(batch["summary_id"])
        self.assertTrue(receipt["acknowledged"])
        self.assertEqual(self.service.get_pending_outbox_messages(now_iso="2026-10-23T12:02:00Z"), [])
        conn = self.service._get_connection()
        self.assertEqual(
            conn.execute("SELECT state FROM recovery_batches WHERE batch_id = ?", (batch["batch_id"],)).fetchone()[0],
            "acknowledged",
        )
        self.assertEqual(
            conn.execute("SELECT state FROM outbox WHERE outbox_id = ?", (batch["outbox_id"],)).fetchone()[0],
            "acknowledged",
        )
        self.service.close()
        self.service = LifeService(self.db_path)
        self.assertTrue(self.service.get_recovery_summary_receipt(batch["summary_id"])["acknowledged"])
        self.assertEqual(self.service.get_all_occurrences("ack")[0], before)

    def test_expired_summary_is_not_pending_and_late_ack_is_ignored(self) -> None:
        self._reminder("ttl", "2026-10-23T11:00:00Z")
        batch = self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:00:00Z")

        pending = self.service.get_pending_outbox_messages(now_iso="2026-10-24T12:00:00Z")
        late_ack = self.service.record_recovery_summary_ack(
            {"type": "notification.acknowledged", "summary_id": batch["summary_id"], "source": "button"},
            now_iso="2026-10-24T12:00:00Z",
        )

        self.assertEqual(pending, [])
        self.assertEqual(late_ack, "ignored")
        receipt = self.service.get_recovery_summary_receipt(batch["summary_id"])
        self.assertFalse(receipt["acknowledged"])

    def test_batch_membership_and_outbox_rollback_together_on_sqlite_failure(self) -> None:
        self._reminder("rollback", "2026-10-23T11:00:00Z")
        conn = self.service._get_connection()
        conn.execute(
            "CREATE TRIGGER fail_summary_outbox BEFORE INSERT ON outbox "
            "WHEN NEW.event_type = 'notification.summary.deliver' "
            "BEGIN SELECT RAISE(FAIL, 'simulated outbox write failure'); END;"
        )

        with self.assertRaises(sqlite3.DatabaseError):
            self.service.create_recovery_summary_batch(now_iso="2026-10-23T12:00:00Z")

        self.assertEqual(conn.execute("SELECT COUNT(*) FROM recovery_batches").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM recovery_batch_items").fetchone()[0], 0)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 0)
        self.assertEqual(self.service.get_all_occurrences("rollback")[0]["state"], "pending")
