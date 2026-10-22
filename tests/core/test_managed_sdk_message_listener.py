from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import unittest
from pathlib import Path

from host.desk_agent.core.managed_sdk_listener import ManagedSdkMessageListener
from host.desk_agent.services.network_log_consumer import NetworkLogConsumer


ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / ".target_logging" / "sdk-acceptance" / "vendor" / "WatcheRobot_python_sdk"
SDK_SHA = "8bc13967a06ef073459be47e930ff43ef37ee8fb"


class ManagedSdkMessageListenerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not (SDK_ROOT / "src" / "watcherobot" / "application" / "transport.py").is_file():
            raise AssertionError("Pinned Watcherobot SDK source checkout is unavailable")
        sha = subprocess.check_output(
            ["git", "-C", str(SDK_ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        if sha != SDK_SHA:
            raise AssertionError(f"Expected pinned SDK SHA {SDK_SHA}, found {sha}")
        sys.path.insert(0, str(SDK_ROOT / "src"))
        from watcherobot.application.context import ApplicationContext
        from watcherobot.application.transport import DaemonApplicationTransport
        from watcherobot.runtime.daemon.application.session import ApplicationChannel

        cls.ApplicationContext = ApplicationContext
        cls.DaemonApplicationTransport = DaemonApplicationTransport
        cls.ApplicationChannel = ApplicationChannel

    def test_listener_adapter_binds_to_exact_sdk_and_adds_removes_observer(self) -> None:
        transport = self.DaemonApplicationTransport()
        context = self.ApplicationContext(app_id="listener-test", transport=transport)
        seen: list[dict] = []
        adapter = ManagedSdkMessageListener(context)

        adapter.add(seen.append)
        message = {"type": "evt.test", "code": 0, "data": {"value": 1}}
        asyncio.run(
            transport._on_frame(
                self.ApplicationChannel.DEVICE,
                json.dumps(message),
            )
        )
        self.assertEqual(seen, [message])

        adapter.remove(seen.append)
        asyncio.run(
            transport._on_frame(
                self.ApplicationChannel.DEVICE,
                json.dumps({"type": "evt.test", "code": 0, "data": {"value": 2}}),
            )
        )
        self.assertEqual(seen, [message])
        transport.close()

    def test_listener_exception_does_not_break_sdk_message_routing_or_replace_callbacks(self) -> None:
        transport = self.DaemonApplicationTransport()
        context = self.ApplicationContext(app_id="listener-test", transport=transport)
        routed: list[dict] = []
        binary_callback = lambda _frame: None
        disconnect_callback = lambda: None
        transport.set_callbacks(routed.append, binary_callback, disconnect_callback)
        original_callbacks = (
            transport._message_callback,
            transport._binary_callback,
            transport._disconnect_callback,
        )

        def broken_listener(_message: dict) -> None:
            raise RuntimeError("observer failure")

        adapter = ManagedSdkMessageListener(context)
        adapter.add(broken_listener)
        message = {"type": "evt.test", "code": 0, "data": {}}
        asyncio.run(
            transport._on_frame(
                self.ApplicationChannel.DEVICE,
                json.dumps(message),
            )
        )

        self.assertEqual(routed, [message])
        self.assertEqual(
            (
                transport._message_callback,
                transport._binary_callback,
                transport._disconnect_callback,
            ),
            original_callbacks,
        )
        adapter.remove(broken_listener)
        transport.close()

    def test_consumer_observes_device_channel_only_through_pinned_listener(self) -> None:
        transport = self.DaemonApplicationTransport()
        context = self.ApplicationContext(app_id="network-log-test", transport=transport)
        adapter = ManagedSdkMessageListener(context)
        consumer = NetworkLogConsumer(enabled=True)
        message = {
            "type": "sys.log.event",
            "data": {
                "log_schema_version": "1.0.0",
                "stream_id": "logstream_01",
                "boot_id": "boot-opaque-id",
                "sequence": 1,
                "uptime_ms": 123456,
                "level": "WARN",
                "component": "camera",
                "event_id": "CAMERA_CAPTURE_EMPTY",
                "fields": {},
            },
        }

        def receive(event: dict) -> None:
            consumer.ingest(event, authorized=True)

        adapter.add(receive)
        asyncio.run(transport._on_frame(self.ApplicationChannel.DESKTOP, json.dumps(message)))
        self.assertEqual(consumer.drain(), [])
        asyncio.run(transport._on_frame(self.ApplicationChannel.DEVICE, json.dumps(message)))
        self.assertEqual(consumer.drain(), [message])

        adapter.remove(receive)
        consumer.close()
        transport.close()


if __name__ == "__main__":
    unittest.main()
