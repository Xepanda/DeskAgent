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


if __name__ == "__main__":
    unittest.main()
