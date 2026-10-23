"""Host-side camera lease, freshness, and real-frame handling contracts."""

from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import patch

from host.desk_agent.camera.camera_service import (
    CameraStreamCapacityError,
    CameraStreamFrameUnavailable,
    CameraStreamLeaseError,
    CameraViewerService,
)


TEST_JPEG = b"\xff\xd8test-image-payload\xff\xd9"


class TestCameraService(unittest.TestCase):
    def test_upstream_disconnect_invalidates_lease_without_callback_or_thread_join(self) -> None:
        entered_source = threading.Event()
        unblock_source = threading.Event()
        teardown_events = []

        def blocked_source():
            entered_source.set()
            unblock_source.wait(2.0)
            return TEST_JPEG

        cam = CameraViewerService(
            auto_start_pipeline=True,
            on_lease_released=lambda owner, reason: teardown_events.append((owner, reason)),
        )
        cam.set_hardware_frame_source(blocked_source)
        cam.acquire_lease("sdk-owner", lease_ms=5000)
        cam.ingest_robot_frame(TEST_JPEG)
        self.assertTrue(entered_source.wait(1.0))

        started = time.monotonic()
        self.assertTrue(cam.invalidate_owner_on_upstream_disconnect("sdk-owner"))
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.1, "disconnect invalidation must not wait for the capture worker")
        self.assertIsNone(cam._active_lease_owner)
        self.assertIsNone(cam._latest_jpeg)
        self.assertEqual(cam._latest_frame_time, 0.0)
        self.assertFalse(cam._is_hardware_frame)
        self.assertFalse(cam.pipeline._running)
        self.assertIsNone(cam.get_latest_frame("sdk-owner"))
        self.assertEqual(teardown_events, [], "upstream disconnect must not invoke lease teardown callbacks")

        unblock_source.set()
        cam.stop_capture()

    def test_reconnect_reuses_blocked_capture_worker_instead_of_starting_a_second_one(self) -> None:
        entered_source = threading.Event()
        unblock_source = threading.Event()
        active_calls = 0
        max_active_calls = 0
        source_calls = 0
        calls_lock = threading.Lock()

        def blocked_source():
            nonlocal active_calls, max_active_calls, source_calls
            with calls_lock:
                source_calls += 1
                call_number = source_calls
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            entered_source.set()
            if call_number == 1:
                unblock_source.wait(2.0)
            with calls_lock:
                active_calls -= 1
            return TEST_JPEG if call_number == 1 else b"\xff\xd8new-session-frame\xff\xd9"

        cam = CameraViewerService(auto_start_pipeline=True)
        cam.set_hardware_frame_source(blocked_source)
        cam.acquire_lease("sdk-owner", lease_ms=5000)
        self.assertTrue(entered_source.wait(1.0))
        self.assertTrue(cam.invalidate_owner_on_upstream_disconnect("sdk-owner"))

        cam.acquire_lease("sdk-owner", lease_ms=5000)
        # A reconnect while the previous source call is blocked must not start
        # another worker against the same non-reentrant hardware source.
        time.sleep(0.08)
        with calls_lock:
            self.assertEqual(max_active_calls, 1)
            self.assertEqual(active_calls, 1)

        unblock_source.set()
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and cam.get_latest_frame("sdk-owner") is None:
            time.sleep(0.01)
        self.assertIsNotNone(cam.get_latest_frame("sdk-owner"))
        cam.release_lease("sdk-owner")
        cam.stop_capture()

    def test_late_capture_from_disconnected_lease_is_not_published_after_reacquire(self) -> None:
        first_capture_entered = threading.Event()
        release_first_capture = threading.Event()
        second_capture_entered = threading.Event()
        release_second_capture = threading.Event()
        calls = 0
        stale_jpeg = b"\xff\xd8stale-before-disconnect\xff\xd9"
        fresh_jpeg = b"\xff\xd8fresh-after-reconnect\xff\xd9"

        def source():
            nonlocal calls
            calls += 1
            if calls == 1:
                first_capture_entered.set()
                release_first_capture.wait(2.0)
                return stale_jpeg
            second_capture_entered.set()
            release_second_capture.wait(2.0)
            return fresh_jpeg

        cam = CameraViewerService(auto_start_pipeline=True)
        cam.set_hardware_frame_source(source)
        cam.acquire_lease("sdk-owner", lease_ms=5000)
        self.assertTrue(first_capture_entered.wait(1.0))
        self.assertTrue(cam.invalidate_owner_on_upstream_disconnect("sdk-owner"))

        # The same viewer may reconnect while the non-reentrant source callback
        # from its old session is still unwinding.
        cam.acquire_lease("sdk-owner", lease_ms=5000)
        release_first_capture.set()
        try:
            self.assertTrue(second_capture_entered.wait(1.0))
            self.assertIsNone(
                cam.get_latest_frame("sdk-owner"),
                "a frame captured before disconnect must not become the new session's first frame",
            )
        finally:
            release_second_capture.set()

        deadline = time.monotonic() + 1.0
        frame = None
        while time.monotonic() < deadline:
            frame = cam.get_latest_frame("sdk-owner")
            if frame is not None:
                break
            time.sleep(0.01)
        self.assertIsNotNone(frame)
        self.assertEqual(frame["jpeg_data"], fresh_jpeg)
        cam.release_lease("sdk-owner")
        cam.stop_capture()

    def test_release_and_reacquire_does_not_start_second_blocked_capture_worker(self) -> None:
        entered_source = threading.Event()
        unblock_source = threading.Event()
        active_calls = 0
        max_active_calls = 0
        calls_lock = threading.Lock()

        def blocked_source():
            nonlocal active_calls, max_active_calls
            with calls_lock:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            entered_source.set()
            unblock_source.wait(2.0)
            with calls_lock:
                active_calls -= 1
            return TEST_JPEG

        cam = CameraViewerService(auto_start_pipeline=True)
        cam.set_hardware_frame_source(blocked_source)
        cam.acquire_lease("viewer", lease_ms=5000)
        self.assertTrue(entered_source.wait(1.0))
        self.assertTrue(cam.release_lease("viewer"))

        cam.acquire_lease("viewer", lease_ms=5000)
        time.sleep(0.08)
        with calls_lock:
            self.assertEqual(max_active_calls, 1)
            self.assertEqual(active_calls, 1)

        unblock_source.set()
        cam.release_lease("viewer")
        cam.stop_capture()

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

    def test_repeated_source_jpeg_does_not_refresh_stale_frame(self) -> None:
        source_calls = 0
        source_called_twice = threading.Event()

        def stale_source():
            nonlocal source_calls
            source_calls += 1
            if source_calls >= 2:
                source_called_twice.set()
            return TEST_JPEG

        cam = CameraViewerService(stale_threshold_ms=50, auto_start_pipeline=True)
        cam.set_hardware_frame_source(stale_source)
        cam.acquire_lease("viewer", lease_ms=1000, target_fps=30)
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and cam.get_latest_frame("viewer") is None:
                time.sleep(0.005)
            first = cam.get_latest_frame("viewer")
            self.assertIsNotNone(first)
            self.assertTrue(source_called_twice.wait(0.5))
            time.sleep(0.06)
            self.assertIsNone(
                cam.get_latest_frame("viewer"),
                "re-reading byte-identical source data must not renew the frame freshness timestamp",
            )
        finally:
            cam.release_lease("viewer")
            cam.stop_capture()

    def test_capture_timeout_does_not_refresh_last_frame(self) -> None:
        source_calls = 0

        def timeout_after_first_frame():
            nonlocal source_calls
            source_calls += 1
            return TEST_JPEG if source_calls == 1 else None

        cam = CameraViewerService(stale_threshold_ms=50, auto_start_pipeline=True)
        cam.set_hardware_frame_source(timeout_after_first_frame)
        cam.acquire_lease("viewer", lease_ms=1000, target_fps=30)
        try:
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and cam.get_latest_frame("viewer") is None:
                time.sleep(0.005)
            self.assertIsNotNone(cam.get_latest_frame("viewer"))
            deadline = time.monotonic() + 0.5
            while time.monotonic() < deadline and cam.get_latest_frame("viewer") is not None:
                time.sleep(0.005)
            self.assertIsNone(cam.get_latest_frame("viewer"))
        finally:
            cam.release_lease("viewer")
            cam.stop_capture()

    def test_frame_fractionally_over_freshness_limit_is_rejected(self) -> None:
        cam = CameraViewerService(stale_threshold_ms=2000)
        cam.acquire_lease("viewer", lease_ms=10000)
        cam.ingest_robot_frame(TEST_JPEG)
        frame_time = cam._latest_frame_time

        # The previous int-before-compare implementation truncated 2000.5 ms
        # to 2000 and incorrectly exposed the frame as fresh.
        with patch("host.desk_agent.camera.camera_service.time.time", return_value=frame_time + 2.0005):
            self.assertIsNone(cam.get_latest_frame("viewer"))

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

    def test_snapshot_waits_for_async_sdk_frame_without_local_capture_worker(self) -> None:
        cam = CameraViewerService(auto_start_pipeline=False)
        result = []

        def request_snapshot() -> None:
            result.append(cam.get_snapshot(wait_timeout_ms=500))

        requester = threading.Thread(target=request_snapshot)
        requester.start()
        deadline = time.monotonic() + 0.5
        while not cam.is_lease_valid() and time.monotonic() < deadline:
            time.sleep(0.005)
        time.sleep(0.03)
        cam.ingest_robot_frame(TEST_JPEG)
        requester.join(1.0)

        self.assertFalse(requester.is_alive(), "snapshot wait must be bounded")
        self.assertEqual(len(result), 1)
        self.assertIsNotNone(result[0], "a frame pushed by the independent SDK ingress must wake the snapshot request")
        self.assertEqual(result[0]["jpeg_data"], TEST_JPEG)
        self.assertTrue(result[0]["is_hardware"])
        self.assertFalse(cam.is_lease_valid(), "ephemeral lease must be released after the snapshot")

    def test_snapshot_wait_returns_unavailable_when_upstream_disconnects(self) -> None:
        cam = CameraViewerService(auto_start_pipeline=False)
        cam.acquire_lease("sdk-owner", lease_ms=2000)
        result = []
        requester = threading.Thread(
            target=lambda: result.append(cam.get_snapshot("sdk-owner", wait_timeout_ms=1000))
        )
        requester.start()
        time.sleep(0.03)

        started = time.monotonic()
        self.assertTrue(cam.invalidate_owner_on_upstream_disconnect("sdk-owner"))
        requester.join(0.5)

        self.assertFalse(requester.is_alive(), "disconnect notification should wake the single-frame wait")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(result, [None], "a disconnected source must produce unavailable, never a cached image")

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

    def test_live_mjpeg_carries_atomic_stream_metadata_and_uses_15fps_ceiling(self) -> None:
        cam = CameraViewerService(stale_threshold_ms=2000)
        cam.acquire_lease("viewer", lease_ms=2000)
        stream_meta = {
            "sequence": 7,
            "capture_timestamp_ms": 1234,
            "session_generation": "session-a",
            "host_receive_monotonic_ms": 2000,
            "clock_offset_ms": 100.0,
            "clock_uncertainty_ms": 20.0,
            "latency_ms": 766.0,
        }
        cam.ingest_robot_frame(TEST_JPEG, metadata=stream_meta)
        stored = cam.get_latest_frame("viewer")
        self.assertEqual(stored["metadata"], stream_meta)

        stream = cam.iter_mjpeg("viewer", target_fps=30, wait_timeout=0.05)
        cam.ingest_robot_frame(b"\xff\xd8new-frame\xff\xd9", metadata={**stream_meta, "sequence": 8})
        part = next(stream)
        self.assertIn(b"X-Device-Capture-Monotonic-Ms: 1234\r\n", part)
        self.assertIn(b"X-Host-Receive-Monotonic-Ms: 2000\r\n", part)
        self.assertIn(b"X-Clock-Offset-Ms: 100.0\r\n", part)
        self.assertIn(b"X-Clock-Uncertainty-Ms: 20.0\r\n", part)
        self.assertIn(b"X-Session-Generation: session-a\r\n", part)
        self.assertIn(b"X-Frame-Seq: 2\r\n", part)
        self.assertIn(b"X-Device-Sequence: 8\r\n", part)
        stream.close()
        cam.release_lease("viewer")

    def test_live_mjpeg_caps_delivery_at_fifteen_fps(self) -> None:
        cam = CameraViewerService()
        cam.acquire_lease("viewer", lease_ms=2000)
        stream = cam.iter_mjpeg("viewer", wait_timeout=0.2)
        cam.ingest_robot_frame(b"\xff\xd8frame-one\xff\xd9")
        next(stream)
        cam.ingest_robot_frame(b"\xff\xd8frame-two\xff\xd9")
        started = time.monotonic()
        next(stream)
        self.assertGreaterEqual(time.monotonic() - started, 0.045)
        stream.close()
        cam.release_lease("viewer")

    def test_live_stream_rejects_stale_session_and_non_forward_sequence(self) -> None:
        cam = CameraViewerService()
        cam.acquire_lease("viewer", lease_ms=2000)
        metadata = {"sequence": 10, "session_generation": "session-a"}
        cam.ingest_robot_frame(TEST_JPEG, metadata)
        original = cam.get_latest_frame("viewer")
        cam.ingest_robot_frame(b"\xff\xd8duplicate\xff\xd9", {**metadata, "sequence": 10})
        cam.ingest_robot_frame(b"\xff\xd8old\xff\xd9", {**metadata, "sequence": 9})
        cam.ingest_robot_frame(b"\xff\xd8foreign\xff\xd9", {**metadata, "sequence": 11, "session_generation": "session-b"})
        current = cam.get_latest_frame("viewer")
        self.assertEqual(current["frame_seq"], original["frame_seq"])
        self.assertEqual(current["metadata"], metadata)
        cam.release_lease("viewer")

    def test_live_stream_clears_frame_after_one_second_even_when_snapshot_threshold_is_longer(self) -> None:
        cam = CameraViewerService(stale_threshold_ms=2000)
        cam.acquire_lease("viewer", lease_ms=3000)
        stream = cam.iter_mjpeg("viewer", wait_timeout=0.05)
        producer = threading.Thread(target=lambda: (time.sleep(0.01), cam.ingest_robot_frame(TEST_JPEG)))
        producer.start()
        next(stream)
        producer.join()
        with cam._lock:
            cam._latest_frame_monotonic = time.monotonic() - 1.01
        with self.assertRaises(StopIteration):
            next(stream)
        self.assertIsNone(cam._latest_jpeg)
        self.assertIsNone(cam.get_latest_frame("viewer"))
        stream.close()
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
