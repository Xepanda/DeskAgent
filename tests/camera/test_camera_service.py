"""
Unit tests for Camera Service.
"""

from __future__ import annotations

import unittest
import time
from host.desk_agent.camera.camera_service import CameraViewerService


class TestCameraService(unittest.TestCase):
    def setUp(self) -> None:
        self.camera = CameraViewerService(default_lease_ms=100)

    def test_lease_acquisition_and_concurrency(self) -> None:
        res1 = self.camera.acquire_lease("user-A", lease_ms=1000)
        self.assertTrue(res1["granted"])

        # user-B denied
        res2 = self.camera.acquire_lease("user-B", lease_ms=1000)
        self.assertFalse(res2["granted"])
        self.assertEqual(res2["reason"], "RESOURCE_BUSY")

        # user-A re-acquires (renews)
        res3 = self.camera.acquire_lease("user-A", lease_ms=2000)
        self.assertTrue(res3["granted"])

    def test_lease_expiration(self) -> None:
        self.camera.acquire_lease("user-short", lease_ms=50)
        self.assertTrue(self.camera.is_lease_valid())
        time.sleep(0.06)
        self.assertFalse(self.camera.is_lease_valid())
        self.assertIsNone(self.camera.get_latest_frame("user-short"))

    def test_frame_streaming_and_stale(self) -> None:
        self.camera.acquire_lease("streamer", lease_ms=5000)
        self.camera.push_frame(b"dummy_jpeg_data")
        frame = self.camera.get_latest_frame("streamer")
        self.assertIsNotNone(frame)
        self.assertEqual(frame["jpeg_data"], b"dummy_jpeg_data")
        self.assertEqual(frame["frame_seq"], 1)


if __name__ == "__main__":
    unittest.main()
