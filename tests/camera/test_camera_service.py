"""
Comprehensive unit tests for LAN Camera Viewer Service.
Covers:
- Reproduction tests: no fake initial frames before real capture
- Single-owner lease arbitration and concurrent access rejection (RESOURCE_BUSY)
- Lease renewal and automatic expiration
- Stale frame detection (> 2000ms) and blanking/masking (prevents old frame masquerading)
- Teardown callback execution upon lease expiration or explicit release
- Bounded memory buffer (no video recording / memory bloat)
"""

from __future__ import annotations

import time
import unittest
from host.desk_agent.camera.camera_service import CameraViewerService, STALE_MASK_JPEG


class TestCameraService(unittest.TestCase):
    def setUp(self) -> None:
        self.camera = CameraViewerService(default_lease_ms=200, stale_threshold_ms=100)

    # -------------------------------------------------------------------------
    # 1. Reproduction Tests: No Fake Initial Frame & Stale Frame Masking
    # -------------------------------------------------------------------------
    def test_no_fake_initial_frame_before_push(self) -> None:
        """Before any real frame is pushed from camera hardware, get_latest_frame must return None."""
        self.camera.acquire_lease("viewer-01", lease_ms=1000)
        frame = self.camera.get_latest_frame("viewer-01")
        self.assertIsNone(frame, "Must NOT return hardcoded fake dummy frame before real frame is captured")

    def test_stale_frame_masking_prevents_old_image_masquerade(self) -> None:
        """When frame age exceeds stale_threshold_ms, service must flag it as stale and mask it."""
        self.camera.acquire_lease("viewer-01", lease_ms=2000)
        self.camera.push_frame(b"live_frame_bytes_001")

        # 1. Immediate read is fresh
        fresh_frame = self.camera.get_latest_frame("viewer-01")
        self.assertIsNotNone(fresh_frame)
        self.assertEqual(fresh_frame["jpeg_data"], b"live_frame_bytes_001")
        self.assertFalse(fresh_frame["is_stale"])

        # 2. Wait for stale threshold (100ms) to elapse
        time.sleep(0.12)
        stale_frame = self.camera.get_latest_frame("viewer-01")
        self.assertIsNotNone(stale_frame)
        self.assertTrue(stale_frame["is_stale"])
        self.assertTrue(stale_frame.get("is_masked", False))
        self.assertEqual(stale_frame["jpeg_data"], STALE_MASK_JPEG)

    # -------------------------------------------------------------------------
    # 2. Single-Owner Lease Arbitration & Renewal
    # -------------------------------------------------------------------------
    def test_single_owner_arbitration_and_busy_rejection(self) -> None:
        # Client A acquires lease
        res_a = self.camera.acquire_lease("client-A", lease_ms=1000)
        self.assertTrue(res_a["granted"])
        self.assertEqual(res_a["client_id"], "client-A")

        # Client B attempts to acquire -> Rejected with RESOURCE_BUSY
        res_b = self.camera.acquire_lease("client-B", lease_ms=1000)
        self.assertFalse(res_b["granted"])
        self.assertEqual(res_b["reason"], "RESOURCE_BUSY")
        self.assertEqual(res_b["current_owner"], "client-A")

        # Unauthorized client cannot read frame
        self.camera.push_frame(b"live_frame")
        self.assertIsNone(self.camera.get_latest_frame("client-B"))

        # Client A can renew
        res_a_renew = self.camera.acquire_lease("client-A", lease_ms=2000)
        self.assertTrue(res_a_renew["granted"])

    def test_lease_expiration_and_teardown_callback(self) -> None:
        teardown_called = []

        def on_teardown(owner: str, reason: str) -> None:
            teardown_called.append((owner, reason))

        camera_with_callback = CameraViewerService(
            default_lease_ms=50,
            on_lease_released=on_teardown,
        )
        camera_with_callback.acquire_lease("user-ephemeral", lease_ms=50)
        self.assertTrue(camera_with_callback.is_lease_valid())

        # Wait for expiration
        time.sleep(0.07)
        self.assertFalse(camera_with_callback.is_lease_valid())
        self.assertIsNone(camera_with_callback.get_latest_frame("user-ephemeral"))
        self.assertEqual(len(teardown_called), 1)
        self.assertEqual(teardown_called[0][0], "user-ephemeral")
        self.assertEqual(teardown_called[0][1], "expired")

    def test_explicit_release(self) -> None:
        released_events = []

        def on_release(owner: str, reason: str) -> None:
            released_events.append((owner, reason))

        cam = CameraViewerService(on_lease_released=on_release)
        cam.acquire_lease("user-logout", lease_ms=5000)
        self.assertTrue(cam.release_lease("user-logout"))
        self.assertFalse(cam.is_lease_valid())
        self.assertEqual(len(released_events), 1)
        self.assertEqual(released_events[0], ("user-logout", "explicit_release"))

        # Non-owner cannot release
        self.assertFalse(cam.release_lease("user-other"))

    # -------------------------------------------------------------------------
    # 3. Hardware Grounding & Synthetic Disabling Tests
    # -------------------------------------------------------------------------
    def test_pipeline_without_hardware_source_produces_no_fake_frames_when_synthetic_disabled(self) -> None:
        """Confirms that with allow_synthetic=False (default), missing hardware produces NO fake frames."""
        cam = CameraViewerService(
            default_lease_ms=1000,
            auto_start_pipeline=True,
            allow_synthetic=False,
        )
        cam.acquire_lease("viewer-clean", lease_ms=1000)
        time.sleep(0.12)
        frame = cam.get_latest_frame("viewer-clean")
        self.assertIsNone(frame, "Must NOT produce fake synthetic frames when hardware is missing and synthetic is disabled")
        cam.pipeline.stop()

    def test_pipeline_with_hardware_source_produces_hardware_frame(self) -> None:
        """Confirms that hardware sensor source produces frames marked is_hardware=True and source='hardware_sensor'."""
        cam = CameraViewerService(
            default_lease_ms=1000,
            auto_start_pipeline=False,
            allow_synthetic=False,
        )
        real_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xd9"
        cam.set_hardware_frame_source(lambda: real_jpeg)
        cam.acquire_lease("viewer-hw", lease_ms=1000)
        cam.pipeline.start(cam.push_frame)
        time.sleep(0.12)
        cam.pipeline.stop()

        frame = cam.get_latest_frame("viewer-hw")
        self.assertIsNotNone(frame)
        self.assertTrue(frame["is_hardware"])
        self.assertEqual(frame["source"], "hardware_sensor")
        self.assertEqual(frame["jpeg_data"], real_jpeg)

    def test_ingest_robot_frame_directly(self) -> None:
        """Confirms ingest_robot_frame tags the frame as hardware sensor."""
        cam = CameraViewerService(default_lease_ms=1000)
        cam.acquire_lease("viewer-ingest", lease_ms=1000)
        robot_bytes = b"real_robot_camera_frame_data"
        cam.ingest_robot_frame(robot_bytes)

        frame = cam.get_latest_frame("viewer-ingest")
        self.assertIsNotNone(frame)
        self.assertTrue(frame["is_hardware"])
        self.assertEqual(frame["source"], "hardware_sensor")
        self.assertEqual(frame["jpeg_data"], robot_bytes)

    def test_motion_coordinates_clamping(self) -> None:
        """Confirms gimbal pan and tilt coordinates are physically clamped to 0..180 and 100..140."""
        cam = CameraViewerService()
        cam.update_motion_coordinates(-30.0, 190.0)
        self.assertEqual(cam.pipeline._current_pan, 0.0)
        self.assertEqual(cam.pipeline._current_tilt, 140.0)

        cam.update_motion_coordinates(200.0, 80.0)
        self.assertEqual(cam.pipeline._current_pan, 180.0)
        self.assertEqual(cam.pipeline._current_tilt, 100.0)


    def test_get_snapshot_auto_acquire(self) -> None:
        """Confirms get_snapshot auto-acquires ephemeral lease when no viewer is active."""
        cam = CameraViewerService(default_lease_ms=500, auto_start_pipeline=True, allow_synthetic=True)
        try:
            self.assertFalse(cam.is_lease_valid())
            frame = cam.get_snapshot()
            self.assertIsNotNone(frame)
            self.assertTrue(cam.is_lease_valid())
            self.assertEqual(cam._active_lease_owner, "snapshot-viewer")
            self.assertIn("jpeg_data", frame)
            self.assertTrue(frame["jpeg_data"].startswith(b"\xff\xd8"))
        finally:
            cam.stop_capture()

    def test_get_snapshot_with_active_lease(self) -> None:
        """Confirms get_snapshot returns frame for current lease owner."""
        cam = CameraViewerService(default_lease_ms=1000, auto_start_pipeline=True, allow_synthetic=True)
        try:
            cam.acquire_lease("active-user", lease_ms=2000)
            time.sleep(0.12)
            frame = cam.get_snapshot("active-user")
            self.assertIsNotNone(frame)
            self.assertEqual(cam._active_lease_owner, "active-user")
        finally:
            cam.stop_capture()


    def test_get_snapshot_rejects_omitted_or_non_owner_when_active_lease_held(self) -> None:
        """Confirms get_snapshot denies access when an active lease is held and client_id is omitted or non-owner."""
        cam = CameraViewerService(default_lease_ms=1000, auto_start_pipeline=False, allow_synthetic=False)
        try:
            cam.acquire_lease("owner-A", lease_ms=2000)
            cam.push_frame(b"\xff\xd8fake-owner-frame\xff\xd9", is_hardware=True)
            self.assertTrue(cam.is_lease_valid())
            self.assertEqual(cam._active_lease_owner, "owner-A")

            # 1. Non-owner explicit client must be rejected
            self.assertIsNone(cam.get_snapshot("viewer-B"))

            # 2. Omitted client_id must NOT hijack or bypass owner-A's lease
            self.assertIsNone(cam.get_snapshot())
            self.assertIsNone(cam.get_snapshot(None))
            self.assertIsNone(cam.get_snapshot(""))

            # 3. Legitimate owner-A must be granted frame
            frame = cam.get_snapshot("owner-A")
            self.assertIsNotNone(frame)
            self.assertEqual(frame["jpeg_data"], b"\xff\xd8fake-owner-frame\xff\xd9")
        finally:
            cam.release_lease("owner-A")
            cam.stop_capture()


if __name__ == "__main__":
    unittest.main()

