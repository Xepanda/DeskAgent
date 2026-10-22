from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import patch

from host.desk_agent import main


class ManagedSdkStartupTests(unittest.TestCase):
    def test_registration_failure_closes_lifecycle_registry_and_host(self) -> None:
        state: dict[str, object] = {"host_closed": False}

        class FakeTransport:
            def __init__(self) -> None:
                self._disconnect_callback = lambda: None
                self.original_callback = self._disconnect_callback
                self._binary_callback = lambda _frame: None

            def send_command(self, *_args, **_kwargs):
                return {"type": "sys.ack", "code": 0}

        class FakeContext:
            def __init__(self) -> None:
                self._transport = FakeTransport()
                self.robot = object()

            @classmethod
            def from_environment(cls):
                instance = cls()
                state["context"] = instance
                return instance

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

        class FakeHostApp:
            def __init__(self, **_kwargs):
                self.network_log_consumer = types.SimpleNamespace(on_disconnect=lambda: None)

            def register_official_sdk_camera_session(self, *_args, **_kwargs):
                raise RuntimeError("registration failed")

            def close(self):
                state["host_closed"] = True

        watcherobot_module = types.ModuleType("watcherobot")
        application_module = types.ModuleType("watcherobot.application")
        application_module.ApplicationContext = FakeContext
        watcherobot_module.application = application_module
        with (
            patch.dict(sys.modules, {"watcherobot": watcherobot_module, "watcherobot.application": application_module}),
            patch.object(main, "DeskAgentHostApp", FakeHostApp),
        ):
            with self.assertRaisesRegex(RuntimeError, "registration failed"):
                asyncio.run(main._run_managed_application(None, "127.0.0.1", 0))

        transport = state["context"]._transport
        self.assertIs(transport._disconnect_callback, transport.original_callback)
        self.assertTrue(state["host_closed"])

    def test_missing_disconnect_callback_closes_host(self) -> None:
        state: dict[str, object] = {"host_closed": False}

        class FakeTransport:
            _disconnect_callback = None
            _binary_callback = staticmethod(lambda _frame: None)

        class FakeContext:
            _transport = FakeTransport()
            robot = object()

            @classmethod
            def from_environment(cls):
                return cls()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                pass

        class FakeHostApp:
            def __init__(self, **_kwargs):
                pass

            def close(self):
                state["host_closed"] = True

        watcherobot_module = types.ModuleType("watcherobot")
        application_module = types.ModuleType("watcherobot.application")
        application_module.ApplicationContext = FakeContext
        watcherobot_module.application = application_module
        with (
            patch.dict(sys.modules, {"watcherobot": watcherobot_module, "watcherobot.application": application_module}),
            patch.object(main, "DeskAgentHostApp", FakeHostApp),
        ):
            with self.assertRaisesRegex(RuntimeError, "no callable disconnect callback"):
                asyncio.run(main._run_managed_application(None, "127.0.0.1", 0))

        self.assertTrue(state["host_closed"])

    def test_single_managed_context_owns_robot_and_host_cleanup(self) -> None:
        state: dict[str, object] = {"context_count": 0, "host_closed": False, "life_closed": False, "sdk_disconnect_called": False}

        class FakeTransport:
            def __init__(self) -> None:
                def sdk_disconnect() -> None:
                    state["sdk_disconnect_called"] = True

                self._disconnect_callback = sdk_disconnect
                self.binary_frames = []
                self._binary_callback = self.binary_frames.append
                self.commands = []
                self.message_listeners = []

            def add_message_listener(self, callback):
                if callback not in self.message_listeners:
                    self.message_listeners.append(callback)

            def remove_message_listener(self, callback):
                if callback in self.message_listeners:
                    self.message_listeners.remove(callback)

            def emit_device_message(self, message):
                for callback in tuple(self.message_listeners):
                    callback(message)

            def send_command(self, *args, **_kwargs):
                self.commands.append(args)
                return {"type": "sys.ack", "code": 0, "data": {"status": "accepted", "accepted": True}}

        class FakeCamera:
            def capture(self, **_kwargs):
                raise AssertionError("startup test must not capture")

        class FakeRobot:
            def __init__(self) -> None:
                self.camera = FakeCamera()

        class FakeContext:
            def __init__(self) -> None:
                state["context_count"] = int(state["context_count"]) + 1
                self.robot = FakeRobot()
                self._transport = FakeTransport()
                state["transport"] = self._transport
                state["disconnect_callback"] = self._transport._disconnect_callback
                self.shutdown_requested = True
                self.entered = False
                self.exited = False

            @classmethod
            def from_environment(cls):
                return cls()

            async def __aenter__(self):
                self.entered = True
                return self

            async def __aexit__(self, *_args):
                self.exited = True
                state["context_exited"] = True

        class FakeLifeService:
            def __init__(self, *_args):
                pass

            def get_due_occurrences(self):
                return []

            def get_pending_outbox_messages(self, **_kwargs):
                return []

            def close(self):
                state["life_closed"] = True

        class FakeHaConnector:
            def __init__(self, **_kwargs):
                pass

        class FakeCameraViewer:
            def __init__(self, **_kwargs):
                self.on_lease_released = None
                self.active_owner = None
                self.frame = None
                self.invalidated = []

            def ingest_robot_frame(self, _frame):
                pass

            def release_lease(self, _owner):
                return False

            def invalidate_owner_on_upstream_disconnect(self, owner):
                if self.active_owner != owner:
                    return False
                self.invalidated.append(owner)
                self.active_owner = None
                self.frame = None
                return True

            def stop_capture(self):
                pass

        class FakeCompanion:
            def __init__(self, **_kwargs):
                pass

            def close(self):
                state["companion_closed"] = True

        watcherobot_module = types.ModuleType("watcherobot")
        application_module = types.ModuleType("watcherobot.application")
        application_module.ApplicationContext = FakeContext
        watcherobot_module.application = application_module

        def fake_serve(app, _host, _port, *, shutdown_check=None):
            self.assertTrue(shutdown_check())
            self.assertIsNone(app.robot_adapter, "managed mode must not start the legacy TCP adapter")
            self.assertIsNotNone(app.official_camera_adapter)
            self.assertTrue(app.official_camera_adapter.is_connected)
            state["camera_adapter"] = app.official_camera_adapter
            state["camera_service"] = app.camera_service
            app.camera_service.active_owner = "viewer-a"
            app.camera_service.frame = b"stale-frame"
            app.official_camera_adapter._owner = "viewer-a"
            transport = state["transport"]
            transport._binary_callback(types.SimpleNamespace(payload=b"x" * 131072))
            transport._binary_callback(types.SimpleNamespace(payload=b"x" * 131073))
            self.assertEqual(len(transport.binary_frames), 1, "oversized WSPK must be dropped before SDK handling")
            self.assertIsNotNone(app.managed_codex_feedback_adapter)
            app.managed_codex_feedback_adapter.send_terminal("codex.completed", {
                "task_id": "startup-task", "resolution": "completed", "summary": "done",
            })
            transport = state["transport"]
            transport.emit_device_message({
                "type": "sys.log.event",
                "data": {
                    "log_schema_version": "1.0.0", "stream_id": "stream1", "boot_id": "boot1",
                    "sequence": 1, "uptime_ms": 10, "level": "WARN", "component": "camera",
                    "event_id": "CAMERA_CAPTURE_EMPTY", "fields": {},
                },
            })
            self.assertEqual(app.network_log_consumer.snapshot()["unauthorized"], 1)
            self.assertEqual(app.network_log_consumer.snapshot()["queued"], 0)
            transport._disconnect_callback()
            state["disconnect_observed_in_runtime"] = True
            app.close()
            state["host_closed"] = True

        config = {
            "storage": {"db_path": ":memory:"},
            "home_assistant": {"subscribe_events": False},
            "network_logs": {"enabled": True},
        }
        with (
            patch.dict(sys.modules, {"watcherobot": watcherobot_module, "watcherobot.application": application_module}),
            patch.object(main, "LifeService", FakeLifeService),
            patch.object(main, "HAConnector", FakeHaConnector),
            patch.object(main, "CameraViewerService", FakeCameraViewer),
            patch.object(main, "CodexCompanion", FakeCompanion),
            patch.object(main, "RobotAdapter") as old_tcp_adapter,
            patch.object(main.DeskAgentHostApp, "_load_config", return_value=config),
            patch.object(main, "_serve_host", side_effect=fake_serve),
        ):
            asyncio.run(main._run_managed_application(None, "127.0.0.1", 0))

        self.assertEqual(state["context_count"], 1)
        self.assertTrue(state["context_exited"])
        self.assertTrue(state["host_closed"])
        self.assertTrue(state["life_closed"])
        self.assertTrue(state["companion_closed"])
        transport = state["transport"]
        self.assertEqual(transport.commands[0][0], "ctrl.expression.set")
        self.assertEqual(transport.commands[0][1]["event_type"], "codex.completed")
        self.assertIs(transport._disconnect_callback, state["disconnect_callback"])
        self.assertTrue(state["disconnect_observed_in_runtime"])
        self.assertTrue(state["sdk_disconnect_called"], "SDK disconnect cleanup remains chained")
        self.assertFalse(state["camera_adapter"].is_connected, "disconnect clears camera session state")
        self.assertEqual(state["camera_service"].invalidated, ["viewer-a"])
        self.assertIsNone(state["camera_service"].frame)
        self.assertNotIn("ctrl.camera.release", [command[0] for command in transport.commands])
        old_tcp_adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
