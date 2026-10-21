"""Host-side camera lease, freshness, and real-frame handling contracts."""

from __future__ import annotations

import time
import unittest

from host.desk_agent.camera.camera_service import (
    CameraStreamCapacityError,
    CameraStreamFrameUnavailable,
    CameraStreamLeaseError,
    CameraViewerService,
)


TEST_JPEG = b"\xff\xd8test-image-payload\xff\xd9"


class TestCameraService(unittest.TestCase):
    def test_no_frame_is_available_without_a_physical_source_even_if_legacy_flag_is_true(self) -> None:
        cam = CameraViewerService(auto_start_pipeline=True, allow_synthetic=True)
        cam.acquire_lease("viewer", lease_ms=1000)
        try:
            time.sleep(0.08)
            self.assertFalse(cam.allow_synthetic)
            self.assertIsNone(cam.get_latest_frame("viewer"))
        finally:
            cam.release_lease("viewer")

    def test_hardware_jpeg_is_available_until_stale_then_becomes_unavailable(self) -> None:
        cam = CameraViewerService(stale_threshold_ms=40)
        cam.acquire_lease("viewer", lease_ms=1000)
        cam.ingest_robot_frame(TEST_JPEG)
        frame = cam.get_latest_frame("viewer")
        self.assertIsNotNone(frame)
        self.assertEqual(frame["jpeg_data"], TEST_JPEG)
        self.assertTrue(frame["is_hardware"])
        self.assertEqual(frame["source"], "hardware_sensor")

        time.sleep(0.06)
        self.assertIsNone(cam.get_latest_frame("viewer"), "A stale frame must not be returned or masked as a usable image")
        cam.release_lease("viewer")

    def test_invalid_or_oversized_jpeg_is_not_published(self) -> None:
        cam = CameraViewerService()
        cam.acquire_lease("viewer", lease_ms=1000)
        cam.ingest_robot_frame(b"not-jpeg")
        cam.ingest_robot_frame(b"\xff\xd8" + b"x" * (128 * 1024) + b"\xff\xd9")
        self.assertIsNone(cam.get_latest_frame("viewer"))
        cam.release_lease("viewer")

    def test_single_owner_arbitration_and_lease_renewal(self) -> None:
        cam = CameraViewerService()
        first = cam.acquire_lease("client-A", lease_ms=1000)
        second = cam.acquire_lease("client-B", lease_ms=1000)
        renewed = cam.acquire_lease("client-A", lease_ms=2000)
        self.assertTrue(first["granted"])
        self.assertFalse(second["granted"])
        self.assertEqual(second["reason"], "RESOURCE_BUSY")
        self.assertTrue(renewed["granted"])
        self.assertIsNone(cam.get_latest_frame("client-B"))
        cam.release_lease("client-A")

    def test_release_clears_frame_and_calls_teardown_once(self) -> None:
        events = []
        cam = CameraViewerService(on_lease_released=lambda owner, reason: events.append((owner, reason)))
        cam.acquire_lease("viewer", lease_ms=1000)
        cam.ingest_robot_frame(TEST_JPEG)
        self.assertTrue(cam.release_lease("viewer"))
        self.assertFalse(cam.release_lease("viewer"))
        self.assertIsNone(cam.get_latest_frame("viewer"))
        self.assertEqual(events, [("viewer", "explicit_release")])

    def test_expiry_clears_data_and_reacquiring_same_client_does_not_reuse_it(self) -> None:
        events = []
        cam = CameraViewerService(on_lease_released=lambda owner, reason: events.append((owner, reason)))
        cam.acquire_lease("viewer", lease_ms=35)
        cam.ingest_robot_frame(TEST_JPEG)
        time.sleep(0.05)

        renewed = cam.acquire_lease("viewer", lease_ms=1000)
        self.assertTrue(renewed["granted"])
        self.assertIsNone(cam.get_latest_frame("viewer"), "An expired lease must not carry its previous frame into renewal")
        self.assertEqual(events, [("viewer", "expired")])
        cam.release_lease("viewer")

    def test_ephemeral_snapshot_lease_is_released_even_without_a_frame(self) -> None:
        events = []
        cam = CameraViewerService(on_lease_released=lambda owner, reason: events.append((owner, reason)))
        self.assertIsNone(cam.get_snapshot())
        self.assertFalse(cam.is_lease_valid())
        self.assertEqual(events, [("snapshot-viewer", "explicit_release")])

    def test_snapshot_uses_real_source_and_releases_ephemeral_lease(self) -> None:
        cam = CameraViewerService(auto_start_pipeline=True, allow_synthetic=True)
        cam.set_hardware_frame_source(lambda: TEST_JPEG)
        try:
            frame = cam.get_snapshot()
            self.assertIsNotNone(frame)
            self.assertEqual(frame["jpeg_data"], TEST_JPEG)
            self.assertTrue(frame["is_hardware"])
            self.assertFalse(cam.is_lease_valid(), "The short-lived snapshot lease must be released after returning bytes")
        finally:
            cam.stop_capture()

    def test_snapshot_cannot_borrow_another_clients_lease(self) -> None:
        cam = CameraViewerService()
        cam.acquire_lease("owner-A", lease_ms=1000)
        cam.ingest_robot_frame(TEST_JPEG)
        self.assertIsNone(cam.get_snapshot("viewer-B"))
        self.assertIsNone(cam.get_snapshot())
        self.assertEqual(cam._active_lease_owner, "owner-A")
        self.assertTrue(cam.release_lease("owner-A"))

    def test_motion_coordinates_are_clamped(self) -> None:
        cam = CameraViewerService()
        cam.update_motion_coordinates(-30.0, 190.0)
        self.assertEqual(cam.pipeline._current_pan, 0.0)
        self.assertEqual(cam.pipeline._current_tilt, 140.0)
        cam.update_motion_coordinates(200.0, 80.0)
        self.assertEqual(cam.pipeline._current_pan, 180.0)
        self.assertEqual(cam.pipeline._current_tilt, 100.0)

    def test_mjpeg_requires_current_owner_and_initial_fresh_hardware_frame(self) -> None:
        cam = CameraViewerService()
        with self.assertRaises(CameraStreamLeaseError) as no_lease:
            cam.iter_mjpeg("viewer")
        self.assertEqual(no_lease.exception.code, "NO_ACTIVE_LEASE")
        cam.acquire_lease("owner", lease_ms=1000)
        with self.assertRaises(CameraStreamLeaseError) as wrong_owner:
            cam.iter_mjpeg("viewer")
        self.assertEqual(wrong_owner.exception.code, "RESOURCE_BUSY")
        stream = cam.iter_mjpeg("owner", wait_timeout=0.05)
        with self.assertRaises(CameraStreamFrameUnavailable) as no_frame:
            next(stream)
        self.assertEqual(no_frame.exception.code, "NO_FRESH_FRAME")
        cam.release_lease("owner")

    def test_mjpeg_yields_only_new_physical_jpegs_and_stops_when_lease_released(self) -> None:
        cam = CameraViewerService(stale_threshold_ms=300)
        cam.acquire_lease("viewer", lease_ms=1000)
        cam.ingest_robot_frame(TEST_JPEG)
        stream = cam.iter_mjpeg("viewer", wait_timeout=0.2)

        def publish_new_frame() -> None:
            time.sleep(0.02)
            cam.ingest_robot_frame(TEST_JPEG)

        import threading
        producer = threading.Thread(target=publish_new_frame)
        producer.start()
        part = next(stream)
        producer.join()
        self.assertIn(b"Content-Type: image/jpeg", part)
        self.assertIn(b"X-Frame-Seq: 2", part)
        self.assertTrue(part.endswith(TEST_JPEG + b"\r\n"))
        cam.release_lease("viewer")
        self.assertRaises(StopIteration, next, stream)

    def test_mjpeg_rejects_external_or_stale_frame(self) -> None:
        cam = CameraViewerService(stale_threshold_ms=20)
        cam.acquire_lease("viewer", lease_ms=1000)
        cam.push_frame(TEST_JPEG, is_hardware=False)
        stream = cam.iter_mjpeg("viewer", wait_timeout=0.05)
        with self.assertRaises(CameraStreamFrameUnavailable):
            next(stream)
        self.assertNotIn("viewer", cam._active_stream_owners, "first-frame errors must release the reservation")
        cam.release_lease("viewer")

    def test_mjpeg_wait_expiry_clears_lease_frame_and_stream_resources(self) -> None:
        events = []
        cam = CameraViewerService(
            stale_threshold_ms=1000,
            on_lease_released=lambda owner, reason: events.append((owner, reason)),
        )
        cam.acquire_lease("viewer", lease_ms=80)
        stream = cam.iter_mjpeg("viewer", wait_timeout=0.5)

        import threading
        producer = threading.Thread(target=lambda: (time.sleep(0.01), cam.ingest_robot_frame(TEST_JPEG)))
        producer.start()
        self.assertIn(b"X-Frame-Seq: 1", next(stream))
        producer.join()

        with self.assertRaises(StopIteration):
            next(stream)

        self.assertIsNone(cam._active_lease_owner)
        self.assertIsNone(cam._latest_jpeg)
        self.assertNotIn("viewer", cam._active_stream_owners)
        self.assertEqual(events, [("viewer", "expired")])

    def test_mjpeg_stream_admission_limits_owner_and_global_subscribers(self) -> None:
        cam = CameraViewerService()
        cam.acquire_lease("viewer", lease_ms=1000)
        stream = cam.iter_mjpeg("viewer")
        with self.assertRaises(CameraStreamCapacityError) as duplicate:
            cam.iter_mjpeg("viewer")
        self.assertEqual(duplicate.exception.code, "STREAM_ALREADY_ACTIVE")
        stream.close()  # close before first next must release its reservation
        self.assertNotIn("viewer", cam._active_stream_owners)

        cam._active_stream_owners.update({"reserved-a", "reserved-b", "reserved-c", "reserved-d"})
        with self.assertRaises(CameraStreamCapacityError) as full:
            cam.iter_mjpeg("viewer")
        self.assertEqual(full.exception.code, "STREAM_CAPACITY")
        cam._active_stream_owners.clear()
        cam.release_lease("viewer")

    def test_mjpeg_wire_part_uses_exact_crlf_and_content_length(self) -> None:
        cam = CameraViewerService()
        cam.acquire_lease("viewer", lease_ms=1000)
        stream = cam.iter_mjpeg("viewer", wait_timeout=0.2)
        import threading
        producer = threading.Thread(target=lambda: (time.sleep(0.02), cam.ingest_robot_frame(TEST_JPEG)))
        producer.start()
        part = next(stream)
        producer.join()
        expected = (
            b"--deskagent-frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(TEST_JPEG)}\r\n".encode()
            + b"X-Frame-Seq: 1\r\nX-Frame-Age-Ms: "
        )
        self.assertTrue(part.startswith(expected))
        self.assertIn(b"\r\n\r\n" + TEST_JPEG + b"\r\n", part)
        stream.close()
        self.assertNotIn("viewer", cam._active_stream_owners)
        cam.release_lease("viewer")


    def test_get_snapshot_auto_acquire(self) -> None:
        """Confirms get_snapshot auto-acquires a lease and waits for a real frame source."""
        cam = CameraViewerService(default_lease_ms=500, auto_start_pipeline=True, allow_synthetic=True)
        cam.set_hardware_frame_source(lambda: TEST_JPEG)
        try:
            self.assertFalse(cam.is_lease_valid())
            frame = cam.get_snapshot()
            self.assertIsNotNone(frame)
            self.assertFalse(cam.is_lease_valid(), "The short-lived snapshot lease must be released after returning bytes")
            self.assertEqual(frame["jpeg_data"], TEST_JPEG)
            self.assertTrue(frame["is_hardware"])
        finally:
            cam.stop_capture()

    def test_get_snapshot_with_active_lease(self) -> None:
        """Confirms get_snapshot returns a real frame for the active lease owner."""
        cam = CameraViewerService(default_lease_ms=1000, auto_start_pipeline=True, allow_synthetic=True)
        cam.set_hardware_frame_source(lambda: TEST_JPEG)
        try:
            cam.acquire_lease("active-user", lease_ms=2000)
            time.sleep(0.12)
            frame = cam.get_snapshot("active-user")
            self.assertIsNotNone(frame)
            self.assertEqual(cam._active_lease_owner, "active-user")
            self.assertEqual(frame["jpeg_data"], TEST_JPEG)
            self.assertTrue(frame["is_hardware"])
        finally:
            cam.release_lease("active-user")
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
