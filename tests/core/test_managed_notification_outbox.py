from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from host.desk_agent.main import DeskAgentHostApp


class _OneIterationStop:
    def __init__(self) -> None:
        self.stopped = False

    def is_set(self) -> bool:
        return self.stopped

    def wait(self, _seconds: float) -> None:
        self.stopped = True

    def set(self) -> None:
        self.stopped = True


class _FakeCompanion:
    def __init__(self, **_kwargs) -> None:
        pass

    def add_event_listener(self, _callback) -> None:
        pass

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass


class _FakeNotificationAdapter:
    def __init__(self) -> None:
        self.notifications = []

    def deliver(self, notification):
        self.notifications.append(notification)
        return {
            "transport_acknowledged": True,
            "unverified_device_acceptance_observed": True,
            "durable_acceptance_confirmed": False,
            "delivery_state": "unknown",
        }

    def close(self) -> None:
        pass


class ManagedNotificationOutboxTests(unittest.TestCase):
    def test_managed_outbox_retries_same_id_and_never_marks_dispatched(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory, patch(
            "host.desk_agent.main.CodexCompanion", _FakeCompanion
        ):
            config_path = Path(directory) / "host.json"
            db_path = Path(directory) / "life.db"
            config_path.write_text(
                '{"storage":{"db_path":"' + str(db_path).replace("\\", "\\\\")
                + '"},"home_assistant":{"subscribe_events":false}}',
                encoding="utf-8",
            )
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            try:
                # Stop the background delivery thread before installing controlled one-shot ticks.
                app._scheduler_stop.set()
                app._delivery_thread.join(timeout=2)
                self.assertFalse(app._delivery_thread.is_alive())
                adapter = _FakeNotificationAdapter()
                app.managed_notification_adapter = adapter

                app.life_service.create_reminder(
                    reminder_id="rem-managed-outbox",
                    title="Managed outbox test",
                    due_at="2099-02-03T04:05:00Z",
                )
                occurrence_id = app.life_service.get_all_occurrences("rem-managed-outbox")[0]["occurrence_id"]
                created = app.life_service.trigger_occurrence_to_outbox(occurrence_id)
                self.assertIsNotNone(created)
                outbox_id = created["outbox_id"]

                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()
                row = app.life_service._get_connection().execute(
                    "SELECT state, retry_count, dispatched_at FROM outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                self.assertEqual(row["state"], "pending")
                self.assertEqual(row["retry_count"], 1)
                self.assertIsNone(row["dispatched_at"])
                receipt = app.life_service.get_notification_receipt(occurrence_id)
                self.assertEqual(receipt["delivery_status"], "unknown")

                app.life_service._get_connection().execute(
                    "UPDATE outbox SET next_retry_at = NULL WHERE outbox_id = ?", (outbox_id,)
                )
                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()
                self.assertEqual(len(adapter.notifications), 2)
                self.assertEqual(
                    [item["notification_id"] for item in adapter.notifications],
                    [occurrence_id, occurrence_id],
                )
                row = app.life_service._get_connection().execute(
                    "SELECT state, retry_count, dispatched_at FROM outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                self.assertEqual(row["state"], "pending")
                self.assertEqual(row["retry_count"], 2)
                self.assertIsNone(row["dispatched_at"])
                self.assertIsNone(app.robot_adapter, "managed notification must not use legacy TCP")
            finally:
                app.close()

    def test_persisted_non_unknown_stage_blocks_duplicate_presentation(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory, patch(
            "host.desk_agent.main.CodexCompanion", _FakeCompanion
        ):
            config_path = Path(directory) / "host.json"
            db_path = Path(directory) / "life.db"
            config_path.write_text(
                '{"storage":{"db_path":"' + str(db_path).replace("\\", "\\\\")
                + '"},"home_assistant":{"subscribe_events":false}}',
                encoding="utf-8",
            )
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            try:
                app._scheduler_stop.set()
                app._delivery_thread.join(timeout=2)
                adapter = _FakeNotificationAdapter()
                app.managed_notification_adapter = adapter
                app.life_service.create_reminder(
                    reminder_id="rem-managed-stage-guard",
                    title="Stage guard",
                    due_at="2099-02-03T04:05:00Z",
                )
                occurrence_id = app.life_service.get_all_occurrences("rem-managed-stage-guard")[0]["occurrence_id"]
                created = app.life_service.trigger_occurrence_to_outbox(occurrence_id)
                outbox_id = created["outbox_id"]
                item = app.life_service.get_pending_outbox_messages(include_delayed=True)[0]
                notification = app.life_service.build_reminder_notification(item)
                self.assertEqual(app.life_service.ensure_notification_receipt(notification), "ready")

                db = app.life_service._get_connection()
                for status in ("accepted", "rendered", "played", "failed", "expired"):
                    with self.subTest(status=status):
                        db.execute(
                            "UPDATE notification_receipts SET delivery_status = ? WHERE notification_id = ?",
                            (status, occurrence_id),
                        )
                        db.execute(
                            "UPDATE outbox SET state = 'pending', next_retry_at = NULL, dispatched_at = NULL "
                            "WHERE outbox_id = ?",
                            (outbox_id,),
                        )
                        db.commit()
                        app._scheduler_stop = _OneIterationStop()
                        app._delivery_loop()
                        self.assertEqual(adapter.notifications, [])
                        outbox = db.execute(
                            "SELECT state, dispatched_at FROM outbox WHERE outbox_id = ?", (outbox_id,)
                        ).fetchone()
                        self.assertEqual(outbox["state"], "pending")
                        self.assertIsNone(outbox["dispatched_at"])
                        self.assertEqual(
                            app.life_service.get_notification_receipt(occurrence_id)["delivery_status"],
                            status,
                        )
            finally:
                app.close()

    def test_user_acknowledgement_blocks_reminder_replay_after_restart(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory, patch(
            "host.desk_agent.main.CodexCompanion", _FakeCompanion
        ):
            config_path = Path(directory) / "host.json"
            db_path = Path(directory) / "life.db"
            config_path.write_text(
                '{"storage":{"db_path":"' + str(db_path).replace("\\", "\\\\")
                + '"},"home_assistant":{"subscribe_events":false}}',
                encoding="utf-8",
            )
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            app._scheduler_stop.set()
            app._delivery_thread.join(timeout=2)
            self.assertFalse(app._delivery_thread.is_alive())
            adapter = _FakeNotificationAdapter()
            app.managed_notification_adapter = adapter
            try:
                app.life_service.create_reminder(
                    reminder_id="rem-managed-ack-replay",
                    title="Ack replay test",
                    due_at="2099-02-03T04:05:00Z",
                )
                occurrence_id = app.life_service.get_all_occurrences(
                    "rem-managed-ack-replay"
                )[0]["occurrence_id"]
                created = app.life_service.trigger_occurrence_to_outbox(occurrence_id)
                outbox_id = created["outbox_id"]

                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()
                self.assertEqual(len(adapter.notifications), 1)
                self.assertEqual(
                    app.life_service.get_notification_receipt(occurrence_id)["delivery_status"],
                    "unknown",
                )

                acknowledgement = {
                    "type": "notification.acknowledged",
                    "notification_id": occurrence_id,
                    "source": "button",
                }
                self.assertEqual(
                    app.life_service.record_managed_notification_event(acknowledgement), "recorded"
                )
                app.close()

                app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
                app._scheduler_stop.set()
                app._delivery_thread.join(timeout=2)
                self.assertFalse(app._delivery_thread.is_alive())
                app.managed_notification_adapter = adapter
                app.life_service._get_connection().execute(
                    "UPDATE outbox SET next_retry_at = NULL WHERE outbox_id = ?", (outbox_id,)
                )
                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()

                self.assertEqual(
                    len(adapter.notifications), 1,
                    "durably acknowledged reminder must not be presented again after restart",
                )
                receipt = app.life_service.get_notification_receipt(occurrence_id)
                self.assertEqual(receipt["user_acknowledged"], 1)
                row = app.life_service._get_connection().execute(
                    "SELECT state FROM outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                self.assertEqual(row["state"], "pending")
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()
