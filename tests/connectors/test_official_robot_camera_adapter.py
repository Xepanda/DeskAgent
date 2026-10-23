from __future__ import annotations

import threading
import time
import unittest
from dataclasses import dataclass
import os
import sys
from pathlib import Path

from host.desk_agent.connectors.robot.official_camera_adapter import OfficialRobotCameraAdapter
from host.desk_agent.camera.camera_service import CameraViewerService


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
    def test_capture_cadence_is_capped_to_host_mjpeg_rate_on_acquire_and_renewal(self) -> None:
        camera = Camera()
        camera.release.clear()
        adapter = OfficialRobotCameraAdapter(
            Robot(camera),
            send_command=lambda *_a, **_k: {"type": "sys.ack", "code": 0},
            ingest_frame=lambda _frame: None,
        )
        try:
            self.assertTrue(adapter.acquire("viewer-a", 5000, target_fps=20)["granted"])
            self.assertEqual(adapter._interval, 0.5, "capture cadence must not exceed Host's 2 FPS MJPEG output cap")

            # Lower consumer rates remain effective when the same lease is renewed.
            self.assertTrue(adapter.acquire("viewer-a", 5000, target_fps=1)["granted"])
            self.assertEqual(adapter._interval, 1.0)

            # Renewing with a faster request must restore the consumer cap, not the raw request.
            self.assertTrue(adapter.acquire("viewer-a", 5000, target_fps=30)["granted"])
            self.assertEqual(adapter._interval, 0.5)
        finally:
            camera.release.set()
            adapter.close()

    def test_sdk_disconnect_invalidates_camera_lease_without_release_command(self) -> None:
        camera = Camera()
        camera.release.clear()
        commands: list[str] = []
        teardown_events: list[tuple[str, str]] = []
        viewer = CameraViewerService(
            on_lease_released=lambda owner, reason: teardown_events.append((owner, reason))
        )
        self.assertTrue(viewer.acquire_lease("viewer-x", lease_ms=5000)["granted"])
        viewer.ingest_robot_frame(JPEG)
        adapter = OfficialRobotCameraAdapter(
            Robot(camera),
            send_command=lambda name, *_a, **_k: commands.append(name) or {"type": "sys.ack", "code": 0},
            ingest_frame=viewer.ingest_robot_frame,
            clear_frame=viewer.invalidate_owner_on_upstream_disconnect,
            capture_timeout=5.0,
        )
        capture_thread = None
        try:
            self.assertTrue(adapter.acquire("viewer-x", 5000)["granted"])
            self.assertTrue(camera.entered.wait(1))
            capture_thread = adapter._thread
            adapter.on_sdk_disconnect()

            self.assertFalse(viewer.is_lease_valid())
            self.assertIsNone(viewer.get_latest_frame("viewer-x"))
            self.assertEqual(commands, ["ctrl.camera.acquire"])
            self.assertEqual(teardown_events, [])
            self.assertTrue(capture_thread.is_alive(), "disconnect path must not join in-flight camera capture")
        finally:
            camera.release.set()
            if capture_thread and capture_thread.is_alive():
                capture_thread.join(timeout=1)
            adapter.close()

    def test_sdk_disconnect_observer_does_not_wait_for_capture_thread(self) -> None:
        camera = Camera()
        camera.release.clear()
        cleared: list[tuple[str, int | None]] = []
        adapter = OfficialRobotCameraAdapter(
            Robot(camera),
            send_command=lambda *_a, **_k: {"type": "sys.ack", "code": 0},
            ingest_frame=lambda _frame: None,
            clear_frame=lambda owner, *, expected_generation=None: cleared.append((owner, expected_generation)),
            capture_timeout=5.0,
        )
        capture_thread = None
        try:
            self.assertTrue(adapter.acquire("viewer-fast", 10000)["granted"])
            self.assertTrue(camera.entered.wait(1))
            capture_thread = adapter._thread
            stop_event = adapter._stop_event
            self.assertIsNotNone(capture_thread)
            self.assertIsNotNone(stop_event)
            started = time.monotonic()
            adapter.on_sdk_disconnect()
            elapsed = time.monotonic() - started

            self.assertLess(elapsed, 0.1)
            self.assertTrue(capture_thread.is_alive(), "disconnect observer must not join capture thread")
            self.assertTrue(stop_event.is_set(), "disconnect observer signals capture worker to stop")
            self.assertFalse(adapter.is_connected)
            self.assertIsNone(adapter._owner)
            self.assertIsNone(adapter._thread)
            self.assertEqual(cleared, [("viewer-fast", 1)])
        finally:
            camera.release.set()
            if capture_thread and capture_thread.is_alive():
                capture_thread.join(timeout=1)
            # Explicit close remains the blocking owner cleanup path.
            adapter.close()

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

    def test_stale_generation_release_and_worker_cleanup_cannot_stop_reacquired_owner(self) -> None:
        camera = Camera()
        camera.release.clear()
        commands: list[str] = []
        cleared: list[tuple[str, int | None]] = []
        adapter = OfficialRobotCameraAdapter(
            Robot(camera),
            send_command=lambda name, *_a, **_k: commands.append(name) or {"type": "sys.ack", "code": 0},
            ingest_frame=lambda _frame: None,
            clear_frame=lambda owner, *, expected_generation=None: cleared.append((owner, expected_generation)),
            capture_timeout=0.01,
        )
        try:
            first = adapter.acquire("same-owner", 5000, lease_generation=11)
            self.assertTrue(first["granted"])
            self.assertTrue(camera.entered.wait(1))
            old_event = adapter._stop_event
            old_thread = adapter._thread

            self.assertTrue(adapter.release("same-owner", expected_generation=11))
            self.assertEqual(cleared, [("same-owner", 11)])
            second = adapter.acquire("same-owner", 5000, lease_generation=12)
            self.assertTrue(second["granted"])
            self.assertEqual(adapter._lease_generation, 12)
            command_count = len(commands)

            adapter._stop_owner("same-owner", old_event, "late old worker", lease_generation=11)

            self.assertEqual(len(commands), command_count, "stale worker must not release the new device lease")
            self.assertEqual(cleared, [("same-owner", 11)], "stale worker must not clear a newer generation's frame")
            self.assertEqual(adapter._owner, "same-owner")
            self.assertIsNot(adapter._stop_event, old_event)
            self.assertFalse(adapter.release("same-owner", expected_generation=11))
            self.assertEqual(len(commands), command_count)
        finally:
            camera.release.set()
            if adapter._thread and adapter._thread is not threading.current_thread():
                adapter._thread.join(timeout=1)
            adapter.close()

    def test_acquire_waits_for_current_release_device_command(self) -> None:
        release_entered = threading.Event()
        allow_release = threading.Event()
        commands: list[str] = []

        def send_command(name: str, *_args, **_kwargs):
            commands.append(name)
            if name == "ctrl.camera.release":
                release_entered.set()
                allow_release.wait(1)
            return {"type": "sys.ack", "code": 0}

        camera = Camera()
        camera.release.set()
        adapter = OfficialRobotCameraAdapter(
            Robot(camera), send_command=send_command, ingest_frame=lambda _frame: None,
            capture_timeout=0.01,
        )
        release_result: list[bool] = []
        acquire_result: list[dict] = []
        try:
            self.assertTrue(adapter.acquire("same-owner", 5000, lease_generation=21)["granted"])
            releasing = threading.Thread(
                target=lambda: release_result.append(adapter.release("same-owner", expected_generation=21))
            )
            releasing.start()
            self.assertTrue(release_entered.wait(1))

            acquiring = threading.Thread(
                target=lambda: acquire_result.append(adapter.acquire("same-owner", 5000, lease_generation=22))
            )
            acquiring.start()
            time.sleep(0.03)
            self.assertEqual(commands.count("ctrl.camera.acquire"), 1)

            allow_release.set()
            releasing.join(timeout=1)
            acquiring.join(timeout=1)
            self.assertEqual(release_result, [True])
            self.assertEqual(len(acquire_result), 1)
            self.assertTrue(acquire_result[0]["granted"])
            self.assertLess(commands.index("ctrl.camera.release"), len(commands) - 1)
            self.assertEqual(commands[-1], "ctrl.camera.acquire")
        finally:
            allow_release.set()
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

    def test_capture_failure_clears_frame_releases_device_and_stops_retrying(self) -> None:
        class FailingCamera(Camera):
            def capture(self, *, timeout: float) -> ImageFrame:
                self.calls += 1
                self.entered.set()
                raise TimeoutError("no image before timeout")

        camera = FailingCamera()
        commands: list[str] = []
        cleared: list[str] = []
        adapter = OfficialRobotCameraAdapter(
            Robot(camera),
            send_command=lambda name, *_a, **_k: commands.append(name) or {"type": "sys.ack", "code": 0},
            ingest_frame=lambda _frame: self.fail("failed capture must not deliver a frame"),
            clear_frame=cleared.append,
            capture_timeout=0.1,
        )
        try:
            self.assertTrue(adapter.acquire("viewer-a", 5000, target_fps=30)["granted"])
            self.assertTrue(camera.entered.wait(1))
            deadline = time.monotonic() + 1
            while adapter._owner is not None and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.05)
            self.assertIsNone(adapter._owner)
            self.assertEqual(camera.calls, 1)
            self.assertEqual(commands, ["ctrl.camera.acquire", "ctrl.camera.release"])
            self.assertEqual(cleared, ["viewer-a"])
            self.assertIn("no image before timeout", adapter.last_error or "")
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
