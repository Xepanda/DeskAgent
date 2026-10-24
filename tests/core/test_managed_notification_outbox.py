from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from host.desk_agent.main import DeskAgentHostApp
from host.desk_agent.core.managed_notification import ManagedNotificationNackError


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
        self.summaries = []
        self.failures = []

    def deliver(self, notification):
        self.notifications.append(notification)
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure
        return {
            "transport_acknowledged": True,
            "unverified_device_acceptance_observed": True,
            "durable_acceptance_confirmed": False,
            "delivery_state": "unknown",
        }

    def deliver_summary(self, summary):
        self.summaries.append(summary)
        failure = self.failures.pop(0) if self.failures else None
        if failure is not None:
            raise failure
        return {"transport_acknowledged": True, "durable_acceptance_confirmed": False, "delivery_state": "unknown"}

    def close(self) -> None:
        pass


class ManagedNotificationOutboxTests(unittest.TestCase):
    @staticmethod
    def _write_test_config(directory: str) -> tuple[Path, Path]:
        config_path = Path(directory) / "host.json"
        db_path = Path(directory) / "life.db"
        config_path.write_text(
            '{"storage":{"db_path":"' + str(db_path).replace("\\", "\\\\")
            + '"},"home_assistant":{"subscribe_events":false}}',
            encoding="utf-8",
        )
        return config_path, db_path

    @staticmethod
    def _stop_delivery_thread(app) -> None:
        app._scheduler_stop.set()
        app._delivery_thread.join(timeout=2)
        if app._delivery_thread.is_alive():
            raise AssertionError("background outbox worker did not stop")

    def test_startup_scans_recovery_once_and_scheduler_poll_does_not_regenerate_batches(self) -> None:
        from unittest.mock import patch
        from host.desk_agent.services.life_service import LifeService

        calls = []
        original = LifeService.create_recovery_summary_batch

        def tracked(service, **kwargs):
            calls.append(kwargs)
            return original(service, **kwargs)

        with tempfile.TemporaryDirectory() as directory, patch(
            "host.desk_agent.main.CodexCompanion", _FakeCompanion
        ), patch.object(LifeService, "create_recovery_summary_batch", tracked):
            config_path, _ = self._write_test_config(directory)
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            try:
                self._stop_delivery_thread(app)
                app._scheduler_stop = _OneIterationStop()
                app._scheduler_loop()
                self.assertEqual(len(calls), 1)
            finally:
                app.close()

    def test_summary_transport_ack_stays_pending_and_without_managed_session_never_uses_audio(self) -> None:
        from unittest.mock import patch
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as directory, patch(
            "host.desk_agent.main.CodexCompanion", _FakeCompanion
        ):
            config_path, _ = self._write_test_config(directory)
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            try:
                self._stop_delivery_thread(app)
                now = datetime.now(timezone.utc)
                app.life_service.create_reminder(
                    reminder_id="rem-recovery-summary",
                    title="Summary item",
                    due_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                )
                batch = app.life_service.create_recovery_summary_batch()
                self.assertIsNotNone(batch)
                outbox_id = batch["outbox_id"]
                adapter = _FakeNotificationAdapter()
                app.managed_notification_adapter = adapter
                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()
                row = app.life_service._get_connection().execute(
                    "SELECT state, retry_count, next_retry_at, dispatched_at FROM outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                self.assertEqual((row["state"], row["retry_count"]), ("pending", 1))
                self.assertIsNotNone(row["next_retry_at"])
                self.assertIsNone(row["dispatched_at"])
                self.assertEqual([item["summary_id"] for item in adapter.summaries], [batch["summary_id"]])
                self.assertEqual(adapter.notifications, [])
                self.assertEqual(app.life_service.get_all_occurrences("rem-recovery-summary")[0]["state"], "pending")

                app.life_service._get_connection().execute(
                    "UPDATE outbox SET next_retry_at = NULL WHERE outbox_id = ?", (outbox_id,)
                )
                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()
                self.assertEqual([item["summary_id"] for item in adapter.summaries], [batch["summary_id"]] * 2)
                retry_row = app.life_service._get_connection().execute(
                    "SELECT state, retry_count, dispatched_at FROM outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                self.assertEqual((retry_row["state"], retry_row["retry_count"], retry_row["dispatched_at"]), ("pending", 2, None))

                app.managed_sdk_session = False
                app.managed_notification_adapter = None
                app.register_announcement_channel(lambda *_args: self.fail("summary fell back to legacy audio"))
                app.life_service._get_connection().execute(
                    "UPDATE outbox SET next_retry_at = NULL WHERE outbox_id = ?", (outbox_id,)
                )
                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()
                self.assertEqual(
                    app.life_service._get_connection().execute(
                        "SELECT state, dispatched_at FROM outbox WHERE outbox_id = ?", (outbox_id,)
                    ).fetchone()["state"],
                    "pending",
                )
                self.assertIsNone(app.life_service._get_connection().execute(
                    "SELECT dispatched_at FROM outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()["dispatched_at"])

                ack = {
                    "type": "notification.acknowledged",
                    "summary_id": batch["summary_id"],
                    "source": "button",
                }
                self.assertEqual(app._record_managed_notification_event(ack), "recorded")
                self.assertEqual(app._record_managed_notification_event(ack), "duplicate")
                self.assertTrue(app.life_service.get_recovery_summary_receipt(batch["summary_id"])["acknowledged"])
                acknowledged_row = app.life_service._get_connection().execute(
                    "SELECT state, next_retry_at, dispatched_at FROM outbox WHERE outbox_id = ?", (outbox_id,)
                ).fetchone()
                self.assertEqual(acknowledged_row["state"], "acknowledged")
                self.assertIsNone(acknowledged_row["next_retry_at"])
                self.assertIsNone(acknowledged_row["dispatched_at"])
                self.assertEqual(app.life_service.get_all_occurrences("rem-recovery-summary")[0]["state"], "pending")

                app.managed_sdk_session = True
                app.managed_notification_adapter = adapter
                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()
                self.assertEqual(len(adapter.summaries), 2, "acknowledged summary must not be retried")
            finally:
                app.close()

    def test_summary_delivery_claim_failure_skips_adapter(self) -> None:
        from unittest.mock import patch
        from datetime import datetime, timedelta, timezone
        from host.desk_agent.services.life_service import LifeService

        with tempfile.TemporaryDirectory() as directory, patch(
            "host.desk_agent.main.CodexCompanion", _FakeCompanion
        ):
            config_path, _ = self._write_test_config(directory)
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            try:
                self._stop_delivery_thread(app)
                now = datetime.now(timezone.utc)
                app.life_service.create_reminder(
                    reminder_id="rem-summary-claim-failure",
                    title="Claim failure test",
                    due_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                    timezone_str="UTC",
                )
                with patch("host.desk_agent.services.life_service._format_summary_display_time", return_value="test time"):
                    batch = app.life_service.create_recovery_summary_batch()
                self.assertIsNotNone(batch)
                adapter = _FakeNotificationAdapter()
                app.managed_notification_adapter = adapter

                with patch.object(LifeService, "claim_recovery_summary_delivery", return_value=False) as claim:
                    app._scheduler_stop = _OneIterationStop()
                    app._delivery_loop()

                claim.assert_called_once_with(batch["outbox_id"], batch["summary_id"])
                self.assertEqual(adapter.summaries, [], "failed claim must not reach the device adapter")
                row = app.life_service._get_connection().execute(
                    "SELECT state FROM outbox WHERE outbox_id = ?", (batch["outbox_id"],)
                ).fetchone()
                self.assertEqual(row["state"], "pending")
            finally:
                app.close()

    def test_unavailable_summary_adapter_applies_pending_backoff(self) -> None:
        from unittest.mock import patch
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as directory, patch(
            "host.desk_agent.main.CodexCompanion", _FakeCompanion
        ), patch(
            "host.desk_agent.services.life_service._format_summary_display_time", return_value="test time"
        ):
            config_path, _ = self._write_test_config(directory)
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            try:
                self._stop_delivery_thread(app)
                now = datetime.now(timezone.utc)
                app.life_service.create_reminder(
                    reminder_id="rem-summary-adapter-unavailable",
                    title="Unavailable adapter test",
                    due_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                    timezone_str="UTC",
                )
                batch = app.life_service.create_recovery_summary_batch()
                self.assertIsNotNone(batch)
                app.managed_sdk_session = False
                app.managed_notification_adapter = None

                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()

                row = app.life_service._get_connection().execute(
                    "SELECT state, retry_count, next_retry_at FROM outbox WHERE outbox_id = ?",
                    (batch["outbox_id"],),
                ).fetchone()
                self.assertEqual((row["state"], row["retry_count"]), ("pending", 1))
                self.assertIsNotNone(row["next_retry_at"], "pre-claim unavailability must apply backoff")

                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()
                unchanged = app.life_service._get_connection().execute(
                    "SELECT retry_count, next_retry_at FROM outbox WHERE outbox_id = ?",
                    (batch["outbox_id"],),
                ).fetchone()
                self.assertEqual(unchanged["retry_count"], 1, "backoff must prevent a hot retry loop")
                self.assertEqual(unchanged["next_retry_at"], row["next_retry_at"])
            finally:
                app.close()

    def test_ack_during_summary_adapter_call_is_not_resurrected_by_failure(self) -> None:
        from unittest.mock import patch
        from datetime import datetime, timedelta, timezone

        with tempfile.TemporaryDirectory() as directory, patch(
            "host.desk_agent.main.CodexCompanion", _FakeCompanion
        ), patch(
            "host.desk_agent.services.life_service._format_summary_display_time", return_value="test time"
        ):
            config_path, _ = self._write_test_config(directory)
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            try:
                self._stop_delivery_thread(app)
                now = datetime.now(timezone.utc)
                app.life_service.create_reminder(
                    reminder_id="rem-summary-ack-inflight",
                    title="ACK in-flight test",
                    due_at=(now - timedelta(minutes=1)).isoformat().replace("+00:00", "Z"),
                    timezone_str="UTC",
                )
                batch = app.life_service.create_recovery_summary_batch()
                self.assertIsNotNone(batch)

                class AckThenFailAdapter:
                    def __init__(self) -> None:
                        self.calls = 0

                    def deliver_summary(_self, _payload):
                        _self.calls += 1
                        self.assertEqual(
                            app._record_managed_notification_event({
                                "type": "notification.acknowledged",
                                "summary_id": batch["summary_id"],
                                "source": "button",
                            }),
                            "recorded",
                        )
                        raise RuntimeError("adapter failed after in-flight ACK")

                    def close(_self) -> None:
                        pass

                adapter = AckThenFailAdapter()
                app.managed_notification_adapter = adapter
                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()

                row = app.life_service._get_connection().execute(
                    "SELECT state, next_retry_at FROM outbox WHERE outbox_id = ?", (batch["outbox_id"],)
                ).fetchone()
                self.assertEqual(row["state"], "acknowledged")
                self.assertIsNone(row["next_retry_at"])
                self.assertEqual(app.life_service.get_pending_outbox_messages(include_delayed=True), [])

                app._scheduler_stop = _OneIterationStop()
                app._delivery_loop()
                self.assertEqual(adapter.calls, 1, "ACKed summaries must not be delivered again")
            finally:
                app.close()

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

    def test_nonretryable_nacks_terminally_fail_only_outbox_without_lifecycle_changes(self) -> None:
        from unittest.mock import patch

        cases = (
            (401, "AUTH_REQUIRED"),
            (507, "STORAGE_FULL"),
            (410, "EXPIRED"),
            (400, "INVALID_ARGUMENT"),
        )
        for index, (code, error_name) in enumerate(cases):
            with self.subTest(error=error_name), tempfile.TemporaryDirectory() as directory, patch(
                "host.desk_agent.main.CodexCompanion", _FakeCompanion
            ):
                config_path, _db_path = self._write_test_config(directory)
                app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
                try:
                    self._stop_delivery_thread(app)
                    adapter = _FakeNotificationAdapter()
                    adapter.failures.append(ManagedNotificationNackError(
                        code=code,
                        error=error_name,
                        command_id=f"{index + 1:032x}",
                        operation="notification.deliver",
                    ))
                    app.managed_notification_adapter = adapter
                    reminder_id = f"rem-terminal-{index}"
                    app.life_service.create_reminder(
                        reminder_id=reminder_id,
                        title="Terminal NACK test",
                        due_at="2099-02-03T04:05:00Z",
                    )
                    occurrence_id = app.life_service.get_all_occurrences(reminder_id)[0]["occurrence_id"]
                    created = app.life_service.trigger_occurrence_to_outbox(occurrence_id)
                    outbox_id = created["outbox_id"]

                    app._scheduler_stop = _OneIterationStop()
                    app._delivery_loop()

                    row = app.life_service._get_connection().execute(
                        "SELECT state, retry_count, next_retry_at, dispatched_at "
                        "FROM outbox WHERE outbox_id = ?",
                        (outbox_id,),
                    ).fetchone()
                    self.assertEqual(row["state"], "failed")
                    self.assertEqual(row["retry_count"], 1)
                    self.assertIsNone(row["next_retry_at"])
                    self.assertIsNone(row["dispatched_at"])
                    self.assertEqual(
                        app.life_service.get_all_occurrences(reminder_id)[0]["state"], "ringing"
                    )
                    self.assertEqual(len(adapter.notifications), 1)
                    self.assertEqual(adapter.notifications[0]["notification_id"], occurrence_id)

                    app._scheduler_stop = _OneIterationStop()
                    app._delivery_loop()
                    self.assertEqual(len(adapter.notifications), 1, "terminal failure must not retry")
                    self.assertEqual(app.life_service.get_pending_outbox_messages(include_delayed=True), [])
                finally:
                    app.close()

    def test_retryable_nack_and_transport_timeout_keep_same_occurrence_pending(self) -> None:
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory, patch(
            "host.desk_agent.main.CodexCompanion", _FakeCompanion
        ):
            config_path, _db_path = self._write_test_config(directory)
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            try:
                self._stop_delivery_thread(app)
                adapter = _FakeNotificationAdapter()
                adapter.failures.extend((
                    ManagedNotificationNackError(
                        code=503,
                        error="STORAGE_UNAVAILABLE",
                        command_id="b" * 32,
                        operation="notification.deliver",
                    ),
                    TimeoutError("transport timed out"),
                ))
                app.managed_notification_adapter = adapter
                app.life_service.create_reminder(
                    reminder_id="rem-retryable-nack",
                    title="Retryable NACK test",
                    due_at="2099-02-03T04:05:00Z",
                )
                occurrence_id = app.life_service.get_all_occurrences("rem-retryable-nack")[0]["occurrence_id"]
                created = app.life_service.trigger_occurrence_to_outbox(occurrence_id)
                outbox_id = created["outbox_id"]

                for expected_retry_count in (1, 2):
                    app._scheduler_stop = _OneIterationStop()
                    app._delivery_loop()
                    row = app.life_service._get_connection().execute(
                        "SELECT state, retry_count, next_retry_at, dispatched_at "
                        "FROM outbox WHERE outbox_id = ?",
                        (outbox_id,),
                    ).fetchone()
                    self.assertEqual(row["state"], "pending")
                    self.assertEqual(row["retry_count"], expected_retry_count)
                    self.assertIsNotNone(row["next_retry_at"])
                    self.assertIsNone(row["dispatched_at"])
                    app.life_service._get_connection().execute(
                        "UPDATE outbox SET next_retry_at = NULL WHERE outbox_id = ?", (outbox_id,)
                    )
                self.assertEqual(len(adapter.notifications), 2)
                self.assertEqual(
                    [item["notification_id"] for item in adapter.notifications],
                    [occurrence_id, occurrence_id],
                )
                self.assertEqual(
                    app.life_service.get_all_occurrences("rem-retryable-nack")[0]["state"], "ringing"
                )
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
            config_path, db_path = self._write_test_config(directory)
            app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
            self._stop_delivery_thread(app)
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
                    app._record_managed_notification_event(acknowledgement), "recorded"
                )
                app.close()

                app = DeskAgentHostApp(str(config_path), managed_sdk_session=True)
                self._stop_delivery_thread(app)
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
