from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass
import os
import sys
from pathlib import Path

from host.desk_agent.connectors.robot.official_camera_adapter import OfficialRobotCameraAdapter


JPEG = b"\xff\xd8local-jpeg\xff\xd9"


@dataclass
class ImageFrame:
    data: bytes
    sequence: int
    timestamp: float
    content_type: str = "image/jpeg"
    session_id: int = 1


class Camera:
    def __init__(self, frame: ImageFrame | None = None) -> None:
        self.frame = frame or ImageFrame(JPEG, 4, time.time(), session_id=27)
        self.calls = 0
        self.entered = threading.Event()
        self.release = threading.Event()

    def capture(self, *, timeout: float) -> ImageFrame:
        self.calls += 1
        self.entered.set()
        self.release.wait(timeout)
        return self.frame


class Robot:
    def __init__(self, camera: Camera) -> None:
        self.camera = camera


class OfficialRobotCameraAdapterTests(unittest.TestCase):
    def test_device_lease_capture_and_release_use_sdk_camera_api(self) -> None:
        camera = Camera()
        commands: list[tuple[str, dict, float | None]] = []
        frames: list[bytes] = []

        def send_command(name: str, data: dict, timeout: float | None = None) -> dict:
            commands.append((name, data, timeout))
            return {"type": "sys.ack", "code": 0, "data": {"session_id": 27}}

        adapter = OfficialRobotCameraAdapter(
            Robot(camera), send_command=send_command, ingest_frame=frames.append,
            max_jpeg_bytes=65536,
        )
        try:
            self.assertTrue(adapter.acquire("viewer-a", 3000, target_fps=20)["granted"])
            self.assertTrue(camera.entered.wait(1))
            camera.release.set()
            deadline = time.time() + 1
            while not frames and time.time() < deadline:
                time.sleep(0.01)
            self.assertEqual(frames, [JPEG])
            self.assertTrue(adapter.release("viewer-a"))
            self.assertEqual([cmd[0] for cmd in commands], ["ctrl.camera.acquire", "ctrl.camera.release"])
            self.assertEqual(commands[0][1], {"client_id": "viewer-a", "lease_duration_ms": 3000})
        finally:
            camera.release.set()
            adapter.close()

    def test_capture_is_single_in_flight_and_rejects_bad_or_oversized_jpeg(self) -> None:
        camera = Camera(ImageFrame(b"\xff\xd8too-large\xff\xd9", 1, time.time(), session_id=2))
        camera.release.clear()
        adapter = OfficialRobotCameraAdapter(
            Robot(camera), send_command=lambda *_a, **_k: {"type": "sys.ack", "code": 0},
            ingest_frame=lambda _frame: self.fail("invalid frame must not be delivered"),
            max_jpeg_bytes=8,
        )
        try:
            adapter.acquire("viewer-a", 3000, target_fps=30)
            self.assertTrue(camera.entered.wait(1))
            time.sleep(0.08)
            self.assertEqual(camera.calls, 1)
            camera.release.set()
            deadline = time.time() + 1
            while adapter.last_error is None and time.time() < deadline:
                time.sleep(0.01)
            self.assertIn("size", adapter.last_error or "")
        finally:
            camera.release.set()
            adapter.close()

    def test_failed_device_acquire_does_not_start_capture(self) -> None:
        camera = Camera()
        adapter = OfficialRobotCameraAdapter(
            Robot(camera), send_command=lambda *_a, **_k: {"type": "sys.ack", "code": 1},
            ingest_frame=lambda _frame: None,
        )
        try:
            result = adapter.acquire("viewer-a", 3000)
            self.assertFalse(result["granted"])
            self.assertEqual(camera.calls, 0)
        finally:
            adapter.close()

    def test_lease_deadline_stops_capture_and_clears_last_frame(self) -> None:
        camera = Camera()
        camera.release.set()
        commands: list[str] = []
        cleared: list[str] = []
        adapter = OfficialRobotCameraAdapter(
            Robot(camera),
            send_command=lambda name, *_a, **_k: commands.append(name) or {"type": "sys.ack", "code": 0},
            ingest_frame=lambda _frame: None,
            clear_frame=cleared.append,
        )
        try:
            self.assertTrue(adapter.acquire("viewer-a", 100, target_fps=30)["granted"])
            deadline = time.time() + 1
            while adapter.is_connected and "ctrl.camera.release" not in commands and time.time() < deadline:
                time.sleep(0.01)
            self.assertIn("ctrl.camera.release", commands)
            self.assertEqual(cleared, ["viewer-a"])
            self.assertFalse(adapter.release("viewer-a"))
        finally:
            camera.release.set()
            adapter.close()

    def test_release_rejection_is_reported(self) -> None:
        adapter = OfficialRobotCameraAdapter(
            Robot(Camera()),
            send_command=lambda name, *_a, **_k: (
                {"type": "sys.ack", "code": 0}
                if name == "ctrl.camera.acquire"
                else {"type": "sys.ack", "code": 7}
            ),
            ingest_frame=lambda _frame: None,
        )
        try:
            self.assertTrue(adapter.acquire("viewer-a", 10000)["granted"])
            self.assertTrue(adapter.release("viewer-a"))
            self.assertIn("device rejected camera release", adapter.last_error or "")
        finally:
            adapter.close()


class PinnedOfficialSdkCameraIntegrationTests(unittest.TestCase):
    """Optional local SDK integration; set WATCHEROBOT_SDK_SRC to the pinned SDK src directory."""

    @classmethod
    def setUpClass(cls) -> None:
        sdk_src = os.environ.get("WATCHEROBOT_SDK_SRC")
        if not sdk_src or not (Path(sdk_src) / "watcherobot" / "robot.py").is_file():
            raise unittest.SkipTest("set WATCHEROBOT_SDK_SRC to run pinned SDK integration")
        sys.path.insert(0, sdk_src)
        from watcherobot.protocol import BinaryFrame, FRAME_IMAGE
        from watcherobot.robot import WatcheRobot

        cls.BinaryFrame = BinaryFrame
        cls.FRAME_IMAGE = FRAME_IMAGE
        cls.WatcheRobot = WatcheRobot

    def test_official_camera_capture_result_is_ingested_as_complete_jpeg(self) -> None:
        frame_received = []

        class FakeSdkTransport:
            capabilities = ()
            device_info = {}

            def set_callbacks(self, message_cb, binary_cb, disconnect_cb):
                self.message_cb = message_cb
                self.binary_cb = binary_cb
                self.disconnect_cb = disconnect_cb

            def send_command(self, message_type, data, timeout=None):
                if message_type == "ctrl.camera.acquire":
                    return {"type": "sys.ack", "code": 0, "data": {}}
                if message_type == "ctrl.camera.release":
                    return {"type": "sys.ack", "code": 0, "data": {}}
                if message_type == "ctrl.camera.capture":
                    self.binary_cb(self.BinaryFrame(self.FRAME_IMAGE, 0, 41, 7, JPEG))
                    return {"type": "sys.ack", "code": 0, "data": {"session_id": 41}}
                raise AssertionError(message_type)

            def close(self):
                pass

        transport = FakeSdkTransport()
        transport.BinaryFrame = self.BinaryFrame
        transport.FRAME_IMAGE = self.FRAME_IMAGE
        robot = self.WatcheRobot._from_transport(transport)
        adapter = OfficialRobotCameraAdapter(
            robot,
            send_command=transport.send_command,
            ingest_frame=frame_received.append,
        )
        try:
            self.assertTrue(adapter.acquire("sdk-viewer", 3000, target_fps=10)["granted"])
            deadline = time.monotonic() + 1
            while not frame_received and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(frame_received, [JPEG])
        finally:
            adapter.release("sdk-viewer")
            adapter.close()
            robot.close()


if __name__ == "__main__":
    unittest.main()
