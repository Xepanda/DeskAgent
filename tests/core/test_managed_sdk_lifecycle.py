from __future__ import annotations

import unittest

from host.desk_agent.core.managed_sdk_lifecycle import ManagedSdkLifecycleObservers
from host.desk_agent.services.network_log_consumer import NetworkLogConsumer


def camera_event(sequence: int = 1) -> dict:
    return {
        "type": "sys.log.event",
        "data": {
            "log_schema_version": "1.0.0", "stream_id": "stream1", "boot_id": "boot1",
            "sequence": sequence, "uptime_ms": 10, "level": "WARN", "component": "camera",
            "event_id": "CAMERA_CAPTURE_EMPTY", "fields": {},
        },
    }


class FakeTransport:
    def __init__(self, callback=None) -> None:
        self._disconnect_callback = callback or (lambda: None)


class ManagedSdkLifecycleTests(unittest.TestCase):
    def test_upstream_runs_before_all_observers_and_close_is_idempotent(self) -> None:
        order: list[str] = []
        transport = FakeTransport(lambda: order.append("sdk"))
        original = transport._disconnect_callback
        registry = ManagedSdkLifecycleObservers(transport)
        registry.add(lambda: order.append("camera"))
        registry.add(lambda: order.append("logs"))

        transport._disconnect_callback()
        registry.close()
        registry.close()

        self.assertEqual(order, ["sdk", "camera", "logs"])
        self.assertIs(transport._disconnect_callback, original)

    def test_normal_shutdown_notifies_host_without_faking_sdk_disconnect(self) -> None:
        order: list[str] = []
        transport = FakeTransport(lambda: order.append("sdk"))
        original = transport._disconnect_callback
        registry = ManagedSdkLifecycleObservers(transport)
        registry.add(lambda: order.append("host-cleanup"))
        registry.close()
        registry.close()
        self.assertEqual(order, ["host-cleanup"])
        self.assertIs(transport._disconnect_callback, original)

    def test_upstream_error_is_rethrown_after_observers_and_observer_errors_are_isolated(self) -> None:
        seen: list[str] = []

        def upstream() -> None:
            seen.append("sdk")
            raise RuntimeError("private callback details")

        registry = ManagedSdkLifecycleObservers(FakeTransport(upstream))

        def broken() -> None:
            raise ValueError("secret-token-and-payload")

        registry.add(broken)
        registry.add(lambda: seen.append("remaining"))
        with self.assertLogs("host.desk_agent.core.managed_sdk_lifecycle", level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "private callback details"):
                registry._transport._disconnect_callback()
        self.assertEqual(seen, ["sdk", "remaining"])
        rendered = " ".join(logs.output)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("payload", rendered)
        self.assertNotIn("private callback details", rendered)

    def test_replaced_callback_is_not_overwritten_during_close(self) -> None:
        transport = FakeTransport()
        registry = ManagedSdkLifecycleObservers(transport)
        replacement = lambda: None
        transport._disconnect_callback = replacement
        with self.assertLogs("host.desk_agent.core.managed_sdk_lifecycle", level="ERROR"):
            registry.close()
        self.assertIs(transport._disconnect_callback, replacement)

    def test_missing_upstream_callback_fails_closed(self) -> None:
        transport = FakeTransport()
        transport._disconnect_callback = None
        with self.assertRaisesRegex(RuntimeError, "no callable disconnect callback"):
            ManagedSdkLifecycleObservers(transport)

    def test_disconnect_clears_consumer_state_and_requires_new_session(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        consumer.ingest(camera_event(), authorized=True)
        consumer._active_session_id = "old-session"
        consumer._subscription_pending = True
        consumer.on_disconnect()
        snapshot = consumer.snapshot()
        self.assertEqual(snapshot["queued"], 0)
        self.assertIsNone(consumer._active_stream_id)
        self.assertIsNone(consumer._active_boot_id)
        self.assertIsNone(consumer._active_session_id)
        self.assertFalse(consumer._subscription_pending)
        self.assertFalse(consumer.ingest(camera_event(2), authorized=True))
        self.assertEqual(consumer.snapshot()["queued"], 0)
        consumer.begin_session("new-session")
        self.assertTrue(consumer.ingest(camera_event(3), authorized=True))
        self.assertEqual(len(consumer.drain()), 1)
        consumer.close()


if __name__ == "__main__":
    unittest.main()
