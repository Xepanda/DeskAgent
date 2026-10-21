from __future__ import annotations

import asyncio
import sys
import types
import unittest
from unittest.mock import patch

from host.desk_agent import main


class ManagedSdkStartupTests(unittest.TestCase):
    def test_single_managed_context_owns_robot_and_host_cleanup(self) -> None:
        state: dict[str, object] = {"context_count": 0, "host_closed": False, "life_closed": False}

        class FakeTransport:
            def __init__(self) -> None:
                self._disconnect_callback = lambda: None

            def send_command(self, *_args, **_kwargs):
                return {"type": "sys.ack", "code": 0}

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

            def ingest_robot_frame(self, _frame):
                pass

            def release_lease(self, _owner):
                return False

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
            app.close()
            state["host_closed"] = True

        config = {"storage": {"db_path": ":memory:"}, "home_assistant": {"subscribe_events": False}}
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
        self.assertIs(transport._disconnect_callback, state["disconnect_callback"])
        old_tcp_adapter.assert_not_called()


if __name__ == "__main__":
    unittest.main()
