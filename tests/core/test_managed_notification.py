from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from host.desk_agent.core.managed_notification import (
    ManagedNotificationAdapter,
    ManagedNotificationNackError,
)


ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "contracts" / "dependency_lock.json"


class FakeTransport:
    def __init__(self) -> None:
        self.listeners = []
        self.commands = []
        self.response = {"type": "sys.ack", "code": 0, "data": {"status": "accepted", "accepted": True}}

    def add_message_listener(self, callback) -> None:
        if callback not in self.listeners:
            self.listeners.append(callback)

    def remove_message_listener(self, callback) -> None:
        if callback in self.listeners:
            self.listeners.remove(callback)

    def send_command(self, *args):
        self.commands.append(args)
        return self.response

    def emit(self, message) -> None:
        for callback in tuple(self.listeners):
            callback(message)


class FakeContext:
    def __init__(self) -> None:
        self._transport = FakeTransport()


def notification(**changes):
    payload = {
        "notification_id": "occ-01",
        "reminder_id": "rem-01",
        "occurrence_id": "occ-01",
        "title": "Take a break",
        "scheduled_at": "2026-10-22T09:00:00Z",
        "expires_at": "2026-10-23T09:00:00Z",
        "is_catchup": False,
    }
    payload.update(changes)
    return payload


class ManagedNotificationAdapterTests(unittest.TestCase):
    def test_expired_summary_is_not_sent(self) -> None:
        context = FakeContext()
        summary = {
            "summary_id": "summary-expired", "batch_id": "batch-expired",
            "created_at": "2026-10-22T12:00:00Z", "expires_at": "2026-10-23T12:00:00Z",
            "items": [{
                "reminder_id": "rem-01", "occurrence_id": "occ-01", "title": "Water",
                "scheduled_at": "2026-10-22T11:00:00Z", "timezone": "Asia/Shanghai",
            }],
            "omitted_count": 0,
        }
        adapter = ManagedNotificationAdapter(
            context, lambda _event: None, sdk_version="0.1.11", lock_path=LOCK,
            now_utc=lambda: datetime(2026, 10, 23, 12, 0, tzinfo=timezone.utc),
        )
        result = adapter.deliver_summary(summary)
        self.assertEqual(result["delivery_state"], "expired")
        self.assertEqual(context._transport.commands, [])
        adapter.close()

    def test_summary_uses_separate_command_and_summary_ack_identity(self) -> None:
        context = FakeContext()
        events = []
        adapter = ManagedNotificationAdapter(
            context, events.append, sdk_version="0.1.11", lock_path=LOCK,
            known_summaries={"summary-01": "2026-10-24T12:00:00Z"},
        )
        summary = {
            "summary_id": "summary-01",
            "batch_id": "batch-01",
            "created_at": "2026-10-23T12:00:00Z",
            "expires_at": "2026-10-24T12:00:00Z",
            "items": [{
                "reminder_id": "rem-01", "occurrence_id": "occ-01", "title": "Water",
                "scheduled_at": "2026-10-23T11:00:00Z", "timezone": "Asia/Shanghai",
            }],
            "omitted_count": 0,
        }
        result = adapter.deliver_summary(summary)
        self.assertEqual(context._transport.commands, [("notification.summary.deliver", summary, 3.0)])
        self.assertEqual(result["summary_id"], "summary-01")
        self.assertEqual(result["delivery_state"], "unknown")
        ack = {"type": "notification.acknowledged", "data": {"summary_id": "summary-01", "source": "button"}}
        context._transport.emit(ack)
        context._transport.emit(ack)
        self.assertEqual(events, [{"type": "notification.acknowledged", "summary_id": "summary-01", "source": "button"}])
        adapter.close()

    def test_pinned_command_ack_is_not_device_render_play_or_user_ack(self) -> None:
        context = FakeContext()
        adapter = ManagedNotificationAdapter(context, lambda _event: None, sdk_version="0.1.11", lock_path=LOCK)
        result = adapter.deliver(notification())
        self.assertEqual(context._transport.commands, [("notification.deliver", notification(), 3.0)])
        self.assertTrue(result["transport_acknowledged"])
        self.assertTrue(result["unverified_device_acceptance_observed"])
        self.assertFalse(result["durable_acceptance_confirmed"])
        self.assertEqual(result["delivery_state"], "unknown")
        self.assertFalse(result["rendered"])
        self.assertFalse(result["played"])
        self.assertFalse(result["user_acknowledged"])
        adapter.close()

    def test_known_notification_nacks_preserve_contract_and_registry_retryability(self) -> None:
        cases = (
            (401, "AUTH_REQUIRED", False),
            (507, "STORAGE_FULL", False),
            (410, "EXPIRED", False),
            (400, "INVALID_ARGUMENT", False),
            (503, "STORAGE_UNAVAILABLE", True),
            (500, "STORAGE_ERROR", True),
            (503, "NOTIFICATION_DELIVERY_FAILED", True),
        )
        registry = json.loads((ROOT / "contracts" / "error_codes.json").read_text(encoding="utf-8"))
        for code, wire_error, retryable in cases:
            with self.subTest(wire_error=wire_error):
                context = FakeContext()
                context._transport.response = {
                    "type": "sys.nack",
                    "code": code,
                    "data": {
                        "type": "notification.deliver",
                        "command_id": "a" * 32,
                        "error": wire_error,
                    },
                }
                adapter = ManagedNotificationAdapter(
                    context, lambda _event: None, sdk_version="0.1.11", lock_path=LOCK
                )
                with self.assertRaises(ManagedNotificationNackError) as raised:
                    adapter.deliver(notification())
                error = raised.exception
                self.assertEqual(error.code, code)
                self.assertEqual(error.error, wire_error)
                self.assertEqual(error.retryable, retryable)
                self.assertEqual(error.retryable, registry["errors"][wire_error]["retryable"])
                self.assertEqual(error.command_id, "a" * 32)
                self.assertEqual(error.operation, "notification.deliver")
                self.assertTrue(str(error))
                adapter.close()

    def test_unknown_or_malformed_nack_fails_closed_without_inventing_code(self) -> None:
        responses = (
            {
                "type": "sys.nack",
                "code": 599,
                "data": {
                    "type": "notification.deliver",
                    "command_id": "a" * 32,
                    "error": "FUTURE_ERROR",
                },
            },
            {
                "type": "sys.nack",
                "code": 400,
                "data": {
                    "type": "notification.deliver",
                    "command_id": "a" * 32,
                    "error": "AUTH_REQUIRED",
                },
            },
            {"type": "sys.nack", "code": 503, "data": {}},
        )
        for response in responses:
            with self.subTest(response=response):
                context = FakeContext()
                context._transport.response = response
                adapter = ManagedNotificationAdapter(
                    context, lambda _event: None, sdk_version="0.1.11", lock_path=LOCK
                )
                with self.assertRaises(RuntimeError) as raised:
                    adapter.deliver(notification())
                self.assertNotIsInstance(raised.exception, ManagedNotificationNackError)
                self.assertFalse(hasattr(raised.exception, "code"))
                self.assertFalse(hasattr(raised.exception, "error"))
                self.assertTrue(raised.exception.retryable)
                adapter.close()

    def test_transport_timeout_remains_original_unknown_exception(self) -> None:
        context = FakeContext()
        timeout = TimeoutError("transport response timed out")

        def raise_timeout(*_args):
            raise timeout

        context._transport.send_command = raise_timeout
        adapter = ManagedNotificationAdapter(
            context, lambda _event: None, sdk_version="0.1.11", lock_path=LOCK
        )
        with self.assertRaises(TimeoutError) as raised:
            adapter.deliver(notification())
        self.assertIs(raised.exception, timeout)
        self.assertFalse(hasattr(raised.exception, "code"))
        adapter.close()

    def test_retries_keep_same_occurrence_identity_and_extra_fields_are_not_forwarded(self) -> None:
        context = FakeContext()
        adapter = ManagedNotificationAdapter(context, lambda _event: None, sdk_version="0.1.11", lock_path=LOCK)
        noisy = notification(private_note="must not be forwarded")
        adapter.deliver(noisy)
        adapter.deliver(noisy)
        self.assertEqual(len(context._transport.commands), 2)
        first = context._transport.commands[0][1]
        second = context._transport.commands[1][1]
        self.assertEqual(first, second)
        self.assertEqual(first["notification_id"], "occ-01")
        self.assertNotIn("private_note", first)
        adapter.close()

    def test_missing_or_mismatched_sdk_pin_fails_closed(self) -> None:
        context = FakeContext()
        with self.assertRaisesRegex(RuntimeError, "SDK 0.1.11"):
            ManagedNotificationAdapter(context, lambda _event: None, sdk_version="0.1.12", lock_path=LOCK)
        locked = json.loads(LOCK.read_text(encoding="utf-8"))
        locked["repositories"]["watcherobot_python_sdk"]["commit"] = "0" * 40
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dependency_lock.json"
            path.write_text(json.dumps(locked), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "audited SDK lock pin"):
                ManagedNotificationAdapter(context, lambda _event: None, sdk_version="0.1.11", lock_path=path)

    def test_missing_transport_methods_fails_closed(self) -> None:
        context = type("Context", (), {"_transport": object()})()
        with self.assertRaisesRegex(TypeError, "send_command"):
            ManagedNotificationAdapter(context, lambda _event: None, sdk_version="0.1.11", lock_path=LOCK)

    def test_validation_requires_stable_id_and_whitelisted_payload(self) -> None:
        context = FakeContext()
        adapter = ManagedNotificationAdapter(context, lambda _event: None, sdk_version="0.1.11", lock_path=LOCK)
        with self.assertRaisesRegex(ValueError, "equal occurrence_id"):
            adapter.deliver(notification(notification_id="other-id"))
        with self.assertRaisesRegex(ValueError, "is_catchup"):
            adapter.deliver(notification(is_catchup="yes"))
        adapter.close()

    def test_expired_delivery_is_not_sent_and_late_events_are_ignored(self) -> None:
        context = FakeContext()
        events = []
        adapter = ManagedNotificationAdapter(
            context,
            events.append,
            sdk_version="0.1.11",
            lock_path=LOCK,
            now_utc=lambda: datetime(2026, 10, 24, tzinfo=timezone.utc),
        )
        result = adapter.deliver(notification())
        self.assertEqual(result["delivery_state"], "expired")
        self.assertEqual(context._transport.commands, [])
        context._transport.emit({"type": "notification.status", "data": {"notification_id": "occ-01", "status": "rendered"}})
        self.assertEqual(events, [])
        adapter.close()

    def test_status_and_ack_events_are_correlated_deduplicated_and_lifecycle_separate(self) -> None:
        context = FakeContext()
        events = []
        adapter = ManagedNotificationAdapter(context, events.append, sdk_version="0.1.11", lock_path=LOCK)
        adapter.deliver(notification())
        status = {"type": "notification.status", "data": {"notification_id": "occ-01", "status": "rendered"}}
        ack = {"type": "notification.acknowledged", "data": {"notification_id": "occ-01", "source": "button"}}
        context._transport.emit(status)
        context._transport.emit(status)
        context._transport.emit({"type": "notification.status", "data": {"notification_id": "occ-01", "status": "accepted"}})
        context._transport.emit({"type": "notification.status", "data": {"notification_id": "occ-01", "status": "played"}})
        context._transport.emit(ack)
        context._transport.emit(ack)
        context._transport.emit({"type": "notification.status", "data": {"notification_id": "occ-01", "status": "unsupported"}})
        context._transport.emit({"type": "notification.acknowledged", "data": {"notification_id": "occ-01"}})
        context._transport.emit({"type": "notification.status", "data": {"notification_id": "unrelated", "status": "played"}})
        self.assertEqual(events, [
            {"type": "notification.status", "notification_id": "occ-01", "status": "rendered"},
            {"type": "notification.status", "notification_id": "occ-01", "status": "unknown", "unverified_device_status": "accepted"},
            {"type": "notification.status", "notification_id": "occ-01", "status": "played"},
            {"type": "notification.acknowledged", "notification_id": "occ-01", "source": "button"},
        ])
        # The adapter has no LifeService reference and cannot stop or complete a reminder.
        self.assertFalse(any(event["type"] in {"reminder.stop_ringing", "reminder.complete"} for event in events))
        adapter.close()
        self.assertEqual(context._transport.listeners, [])
        context._transport.emit(status)
        self.assertEqual(len(events), 4)

    def test_disconnect_reconnect_same_transport_keeps_listener_and_expired_state_is_forwarded(self) -> None:
        context = FakeContext()
        events = []
        adapter = ManagedNotificationAdapter(context, events.append, sdk_version="0.1.11", lock_path=LOCK)
        adapter.deliver(notification())
        adapter.deliver(notification(
            notification_id="occ-02",
            occurrence_id="occ-02",
            reminder_id="rem-02",
        ))
        # The managed Runtime owns reconnect; this adapter stays attached to that same transport.
        context._transport.emit({"type": "notification.status", "data": {"notification_id": "occ-01", "status": "expired"}})
        context._transport.emit({"type": "notification.status", "data": {"notification_id": "occ-02", "status": "unknown"}})
        self.assertEqual([item["status"] for item in events], ["expired", "unknown"])
        self.assertEqual(len(context._transport.listeners), 1)
        adapter.close()

    def test_restored_outbox_context_accepts_late_correlated_event_before_redelivery(self) -> None:
        context = FakeContext()
        events = []
        adapter = ManagedNotificationAdapter(
            context,
            events.append,
            sdk_version="0.1.11",
            lock_path=LOCK,
            known_notifications={"occ-01": "2026-10-23T09:00:00Z"},
        )
        context._transport.emit({
            "type": "notification.acknowledged",
            "data": {"notification_id": "occ-01", "source": "button"},
        })
        self.assertEqual(events, [{
            "type": "notification.acknowledged", "notification_id": "occ-01", "source": "button",
        }])
        adapter.close()


if __name__ == "__main__":
    unittest.main()
