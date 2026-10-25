"""
Desk Agent R1 LAN Camera Viewer Service.

Provides secure local network JPEG frame viewing with:
        - Mutual exclusion single-viewer lease arbitration (RESOURCE_BUSY for concurrent viewers).
- Automatic lease expiration and renewal support.
- Strict freshness tracking that makes stale frames unavailable.
- Strict bounded latest-frame buffer (no disk writes, no video recordings, no audio streams).
- No synthetic frame generation when hardware camera is disconnected.
- Teardown callbacks to stop hardware sensor DMA upon lease release or timeout.
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections.abc import Iterator
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_CAMERA_JPEG_BYTES = 128 * 1024
MAX_LIVE_CAMERA_FPS = 15.0
LIVE_CAMERA_STALE_THRESHOLD_MS = 1000
MAX_CAMERA_STREAMS = 4
MAX_CAMERA_STREAMS_PER_OWNER = 1

LeaseReleaseCallback = Callable[[str, str, int], None]


class CameraStreamError(RuntimeError):
    """Pre-stream failure that an HTTP adapter can map before sending headers."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class CameraStreamLeaseError(CameraStreamError):
    pass


class CameraStreamFrameUnavailable(CameraStreamError):
    pass


class CameraStreamCapacityError(CameraStreamError):
    pass


class _ReservedCameraStream(Iterator[bytes]):
    """Iterator wrapper that releases its reservation on every terminal path."""

    def __init__(self, iterator: Iterator[bytes], on_close: Callable[[], None]) -> None:
        self._iterator = iterator
        self._on_close = on_close
        self._closed = False

    def __iter__(self) -> "_ReservedCameraStream":
        return self

    def __next__(self) -> bytes:
        if self._closed:
            raise StopIteration
        try:
            return next(self._iterator)
        except BaseException:
            self.close()
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            close = getattr(self._iterator, "close", None)
            if close:
                close()
        finally:
            self._on_close()


class FrameCapturePipeline:
    """Frame capture pipeline for an attached physical camera source."""

    def __init__(self, fps: float = 10.0, allow_synthetic: bool = False) -> None:
        self.fps = max(1.0, min(30.0, fps))
        # Kept as an ignored compatibility argument. A synthetic frame must
        # never be presented as a camera result, even in local configuration.
        self.allow_synthetic = False
        self._interval = 1.0 / self.fps
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._generation = 0
        self._current_pan = 90.0
        self._current_tilt = 120.0
        self._consumer: Optional[Callable[[bytes, bool], None]] = None
        self._frame_source: Optional[Callable[[], Optional[bytes]]] = None
        self._last_source_frame: Optional[bytes] = None

    def set_frame_source(self, source: Optional[Callable[[], Optional[bytes]]]) -> None:
        """Sets external hardware frame capture callback (e.g. V4L2 device or robot frame stream)."""
        with self._lock:
            self._frame_source = source
            self._last_source_frame = None

    def start(
        self,
        consumer: Callable[[bytes, bool], None],
        *,
        advance_generation_if_running: bool = False,
    ) -> int:
        with self._lock:
            self._consumer = consumer
            if self._running:
                if advance_generation_if_running:
                    self._generation += 1
                return self._generation
            self._running = True
            self._generation += 1
            generation = self._generation
            # A disconnect can stop the pipeline while the source callback is
            # blocked in transport I/O. If the lease returns before that call
            # unwinds, resume the existing worker instead of creating a second
            # concurrent caller for a non-reentrant camera source.
            if self._thread and self._thread.is_alive():
                return generation
            self._thread = threading.Thread(target=self._capture_loop, name="camera-capture-pipeline", daemon=True)
            self._thread.start()
            return generation

    def stop(self) -> None:
        self.request_stop()
        self._join_stopped_worker()

    def stop_if_generation(self, generation: Optional[int]) -> bool:
        """Stops only the pipeline run owned by the supplied lifecycle token."""
        if generation is None or not self.request_stop_if_generation(generation):
            return False
        self._join_stopped_worker()
        return True

    def request_stop_if_generation(self, generation: Optional[int]) -> bool:
        """Non-blocking conditional stop used by upstream disconnect cleanup."""
        if generation is None:
            return False
        with self._lock:
            if generation != self._generation or not self._running:
                return False
            self._running = False
            self._consumer = None
            self._generation += 1
            return True

    def _join_stopped_worker(self) -> None:
        with self._lock:
            thread = self._thread
        if thread and thread.is_alive() and thread != threading.current_thread():
            try:
                thread.join(timeout=0.2)
            except Exception:
                pass
        # Keep the reference while a blocked source callback is still
        # running. A rapid lease reacquire can then resume this worker
        # instead of starting a second concurrent caller for the source.
        with self._lock:
            if thread and not thread.is_alive():
                self._thread = None

    def request_stop(self) -> None:
        """Requests capture shutdown without waiting for the capture worker.

        Use this from upstream disconnect callbacks, where the frame source may
        currently be blocked in transport I/O. Call ``stop`` later when joining
        the worker is safe and desired.
        """
        # Invalidate the current capture generation without waiting for an
        # in-flight source callback. stop() joins only after releasing this
        # lock, so a disconnect can promptly revoke the old callback result.
        with self._lock:
            self._running = False
            self._consumer = None
            self._generation += 1

    def update_coordinates(self, pan: float, tilt: float) -> None:
        with self._lock:
            self._current_pan = max(0.0, min(180.0, pan))
            self._current_tilt = max(100.0, min(140.0, tilt))

    def _capture_loop(self) -> None:
        while True:
            with self._lock:
                if not self._running:
                    return
                generation = self._generation
                frame_source = self._frame_source
                consumer = self._consumer
            try:
                frame = None
                is_hw = False
                if frame_source:
                    try:
                        frame = frame_source()
                        if frame:
                            is_hw = True
                            # The callback contract carries bytes only, with no
                            # capture timestamp or frame sequence. Repeated
                            # byte-identical data therefore cannot prove that
                            # a new physical exposure occurred; keep its
                            # original freshness deadline instead of
                            # re-publishing it as a new frame.
                            if frame == self._last_source_frame:
                                frame = None
                            else:
                                self._last_source_frame = frame
                    except Exception as src_err:
                        logger.debug("Hardware frame source error: %s", src_err)

                with self._lock:
                    generation_is_current = self._running and generation == self._generation
                if frame is not None and consumer and generation_is_current:
                    try:
                        consumer(frame, is_hw)
                    except TypeError:
                        consumer(frame)
            except Exception as e:
                logger.warning("Frame capture error: %s", e)
            time.sleep(self._interval)


class CameraViewerService:
    def __init__(
        self,
        default_lease_ms: int = 10000,
        stale_threshold_ms: int = 2000,
        on_lease_released: Optional[LeaseReleaseCallback] = None,
        auto_start_pipeline: bool = False,
        allow_synthetic: bool = False,
    ) -> None:
        self.default_lease_ms = default_lease_ms
        self.stale_threshold_ms = stale_threshold_ms
        self.on_lease_released = on_lease_released
        self.auto_start_pipeline = auto_start_pipeline
        # Keep the old config attribute for callers, but synthetic output is
        # intentionally disabled regardless of configuration.
        self.allow_synthetic = False

        self._lock = threading.RLock()
        self._frame_changed = threading.Condition(self._lock)
        self._active_lease_owner: Optional[str] = None
        self._lease_generation_counter = 0
        self._active_lease_generation: Optional[int] = None
        self._lease_expires_at: float = 0.0

        # Bounded single latest frame buffer
        self._latest_jpeg: Optional[bytes] = None
        self._latest_frame_time: float = 0.0
        self._latest_frame_monotonic: float = 0.0
        self._frame_seq: int = 0
        self._is_hardware_frame: bool = False
        self._latest_frame_metadata: Optional[dict[str, Any]] = None
        self._camera_session_generation: Optional[str] = None
        self._last_device_sequence: Optional[int] = None
        self._capture_generation = 0
        self._pipeline_generation: Optional[int] = None
        self._active_stream_owners: set[str] = set()
        self.pipeline = FrameCapturePipeline(fps=10.0, allow_synthetic=False)

    def update_motion_coordinates(self, pan: float, tilt: float) -> None:
        """Updates gimbal pan/tilt overlay on the active camera feed."""
        self.pipeline.update_coordinates(pan, tilt)

    def start_capture(self) -> None:
        """Starts the underlying capture pipeline."""
        with self._lock:
            capture_generation = self._capture_generation
        pipeline_generation = self.pipeline.start(
            lambda frame, is_hardware: self.push_frame(
                frame,
                is_hardware,
                capture_generation=capture_generation,
            )
        )
        with self._lock:
            if capture_generation == self._capture_generation:
                self._pipeline_generation = pipeline_generation
                return
        self.pipeline.stop_if_generation(pipeline_generation)

    def stop_capture(self) -> None:
        """Stops the underlying capture pipeline."""
        self.pipeline.stop()

    def set_hardware_frame_source(self, source_fn: Optional[Callable[[], Optional[bytes]]]) -> None:
        """Sets external hardware frame capture callback (e.g. V4L2 device or robot frame stream)."""
        self.pipeline.set_frame_source(source_fn)

    def ingest_robot_frame(self, frame_bytes: bytes, metadata: Optional[dict[str, Any]] = None) -> None:
        """Atomically ingest a live JPEG and the metadata received with that frame."""
        self.push_frame(frame_bytes, is_hardware=True, metadata=metadata)

    def acquire_lease(
        self,
        client_id: str,
        lease_ms: Optional[int] = None,
        target_fps: Optional[float] = None,
    ) -> dict[str, Any]:
        """
        Acquires or renews single-viewer lease for camera streaming.
        Rejects concurrent clients with RESOURCE_BUSY.
        """
        duration = (lease_ms or self.default_lease_ms) / 1000.0
        now = time.time()

        expired_lease = None
        expired_pipeline_generation = None
        new_lease = False
        with self._lock:
            previous_pipeline_generation = self._pipeline_generation
            expired_lease = self._expire_lease_locked(now)
            if expired_lease:
                expired_pipeline_generation = previous_pipeline_generation
            if self._active_lease_owner and self._active_lease_owner != client_id:
                return {
                    "granted": False,
                    "reason": "RESOURCE_BUSY",
                    "current_owner": self._active_lease_owner,
                    "remaining_ms": int((self._lease_expires_at - now) * 1000),
                }

            if self._active_lease_owner is None:
                self._capture_generation += 1
                self._lease_generation_counter += 1
                self._active_lease_generation = self._lease_generation_counter
                new_lease = True
            self._active_lease_owner = client_id
            self._lease_expires_at = now + duration
            capture_generation = self._capture_generation
            lease_generation = self._active_lease_generation
            assert lease_generation is not None

            if target_fps is not None:
                self.pipeline.fps = max(0.5, min(30.0, float(target_fps)))
                self.pipeline._interval = 1.0 / self.pipeline.fps

            result = {
                "granted": True,
                "client_id": client_id,
                "lease_id": f"lease-{client_id}",
                "lease_duration_ms": int(duration * 1000),
                "expires_at": self._lease_expires_at,
                "lease_generation": lease_generation,
                "target_fps": self.pipeline.fps,
            }

        if expired_lease:
            expired_owner, expired_lease_generation = expired_lease
            self.pipeline.stop_if_generation(expired_pipeline_generation)
            self._trigger_teardown(expired_owner, "expired", expired_lease_generation)
        if self.auto_start_pipeline:
            pipeline_generation = self.pipeline.start(
                lambda frame, is_hardware: self.push_frame(
                    frame,
                    is_hardware,
                    capture_generation=capture_generation,
                ),
                advance_generation_if_running=new_lease,
            )
            with self._lock:
                if (
                    self._active_lease_owner == client_id
                    and self._capture_generation == capture_generation
                ):
                    self._pipeline_generation = pipeline_generation
                    keep_pipeline_running = True
                else:
                    keep_pipeline_running = False
            if not keep_pipeline_running:
                self.pipeline.stop_if_generation(pipeline_generation)
        return result

    def release_lease(self, client_id: str, expected_generation: Optional[int] = None) -> bool:
        """Explicitly releases camera viewing lease."""
        with self._lock:
            if (
                self._active_lease_owner == client_id
                and (
                    expected_generation is None
                    or self._active_lease_generation == expected_generation
                )
            ):
                owner = self._active_lease_owner
                lease_generation = self._active_lease_generation
                assert lease_generation is not None
                pipeline_generation = self._pipeline_generation
                self._pipeline_generation = None
                self._capture_generation += 1
                self._active_lease_owner = None
                self._active_lease_generation = None
                self._lease_expires_at = 0.0
                self._latest_jpeg = None
                self._latest_frame_time = 0.0
                self._latest_frame_monotonic = 0.0
                self._latest_frame_metadata = None
                self._camera_session_generation = None
                self._last_device_sequence = None
                self._is_hardware_frame = False
                self._frame_changed.notify_all()
            else:
                return False
        self.pipeline.stop_if_generation(pipeline_generation)
        self._trigger_teardown(owner, "explicit_release", lease_generation)
        return True

    def invalidate_owner_on_upstream_disconnect(
        self,
        client_id: str,
        expected_generation: Optional[int] = None,
    ) -> bool:
        """Invalidates camera state after the owning SDK transport disconnects.

        This is deliberately a non-blocking invalidation path: it clears the
        owner's lease and buffered frame, wakes stream waiters, and requests
        capture shutdown without invoking ``on_lease_released`` or joining the
        capture worker. The normal ``release_lease`` path remains responsible
        for teardown callbacks and synchronous pipeline stopping.
        """
        with self._lock:
            if (
                self._active_lease_owner != client_id
                or (
                    expected_generation is not None
                    and self._active_lease_generation != expected_generation
                )
            ):
                return False
            pipeline_generation = self._pipeline_generation
            self._pipeline_generation = None
            self._capture_generation += 1
            self._active_lease_owner = None
            self._active_lease_generation = None
            self._lease_expires_at = 0.0
            self._latest_jpeg = None
            self._latest_frame_time = 0.0
            self._latest_frame_monotonic = 0.0
            self._latest_frame_metadata = None
            self._camera_session_generation = None
            self._last_device_sequence = None
            self._is_hardware_frame = False
            self._frame_changed.notify_all()
        self.pipeline.request_stop_if_generation(pipeline_generation)
        return True

    def is_lease_valid(self) -> bool:
        """Checks if current camera lease is valid and active."""
        with self._lock:
            pipeline_generation = self._pipeline_generation
            expired_lease = self._expire_lease_locked(time.time())
            valid = self._active_lease_owner is not None
        if expired_lease:
            owner, lease_generation = expired_lease
            self.pipeline.stop_if_generation(pipeline_generation)
            self._trigger_teardown(owner, "expired", lease_generation)
        return valid

    def _expire_lease_locked(self, now: float) -> Optional[tuple[str, int]]:
        """Clears expired owner/frame state; caller performs teardown after unlock."""
        if self._active_lease_owner and now >= self._lease_expires_at:
            owner = self._active_lease_owner
            lease_generation = self._active_lease_generation
            assert lease_generation is not None
            self._pipeline_generation = None
            self._capture_generation += 1
            self._active_lease_owner = None
            self._active_lease_generation = None
            self._lease_expires_at = 0.0
            self._latest_jpeg = None
            self._latest_frame_time = 0.0
            self._latest_frame_monotonic = 0.0
            self._latest_frame_metadata = None
            self._camera_session_generation = None
            self._last_device_sequence = None
            self._is_hardware_frame = False
            self._frame_changed.notify_all()
            return owner, lease_generation
        return None

    def _trigger_teardown(self, owner: str, reason: str, lease_generation: int) -> None:
        if self.on_lease_released:
            try:
                self.on_lease_released(owner, reason, lease_generation)
            except Exception as exc:
                logger.error("Error executing camera teardown callback: %s", exc)

    def push_frame(
        self,
        jpeg_data: bytes,
        is_hardware: bool = False,
        metadata: Optional[dict[str, Any]] = None,
        *,
        capture_generation: Optional[int] = None,
    ) -> None:
        """
        Receives captured JPEG frame from device hardware or fallback pipeline.
        Only stores the latest frame if an active lease exists.
        """
        if not isinstance(jpeg_data, bytes) or not (4 <= len(jpeg_data) <= MAX_CAMERA_JPEG_BYTES):
            logger.warning("Rejected camera payload with invalid size/type")
            return
        if jpeg_data[:2] != b"\xff\xd8" or jpeg_data[-2:] != b"\xff\xd9":
            logger.warning("Rejected camera payload without JPEG SOI/EOI markers")
            return

        if not self.is_lease_valid():
            return
        with self._lock:
            if capture_generation is not None and capture_generation != self._capture_generation:
                return
            if self._active_lease_owner and time.time() < self._lease_expires_at:
                incoming_metadata = dict(metadata) if isinstance(metadata, dict) else None
                incoming_session = incoming_metadata.get("session_generation") if incoming_metadata else None
                incoming_sequence = incoming_metadata.get("sequence") if incoming_metadata else None
                if incoming_session is not None:
                    if not isinstance(incoming_session, str) or not incoming_session:
                        return
                    if self._camera_session_generation is not None and incoming_session != self._camera_session_generation:
                        logger.debug("Rejected frame from a stale or foreign camera session")
                        return
                if incoming_sequence is not None:
                    if isinstance(incoming_sequence, bool) or not isinstance(incoming_sequence, int) or not 0 <= incoming_sequence <= 0xFFFFFFFF:
                        return
                    if self._last_device_sequence is not None:
                        delta = (incoming_sequence - self._last_device_sequence) & 0xFFFFFFFF
                        if not 0 < delta < 0x80000000:
                            logger.debug("Rejected repeated or out-of-order camera sequence")
                            return
                # Never let a synthetic frame overwrite a non-stale live hardware frame
                if not is_hardware and getattr(self, "_is_hardware_frame", False) and self._latest_jpeg is not None:
                    now = time.time()
                    if (now - self._latest_frame_time) * 1000 < self.stale_threshold_ms:
                        return
                self._latest_jpeg = jpeg_data
                self._latest_frame_time = time.time()
                self._latest_frame_monotonic = time.monotonic()
                self._is_hardware_frame = is_hardware
                self._latest_frame_metadata = incoming_metadata
                if incoming_session is not None:
                    self._camera_session_generation = incoming_session
                if incoming_sequence is not None:
                    self._last_device_sequence = incoming_sequence
                self._frame_seq += 1
                self._frame_changed.notify_all()

    def iter_mjpeg(
        self,
        client_id: str,
        *,
        boundary: str = "deskagent-frame",
        target_fps: float = MAX_LIVE_CAMERA_FPS,
        wait_timeout: float = 0.5,
    ) -> Iterator[bytes]:
        """Yield fresh physical JPEG parts while the caller owns the camera lease.

        Admission is synchronous: one stream per owner, at most four globally.
        Duplicate owners raise STREAM_ALREADY_ACTIVE; global exhaustion raises
        STREAM_CAPACITY. The iterator starts after the currently buffered frame, so it never replays
        a snapshot as the first stream frame. Each wait is bounded, allowing lease
        expiry and generator close to be observed promptly without a background task.
        """
        fps = max(0.1, min(MAX_LIVE_CAMERA_FPS, float(target_fps)))
        interval = 1.0 / fps
        with self._lock:
            owner = None
            pipeline_generation = self._pipeline_generation
            if self._active_lease_owner != client_id or time.time() >= self._lease_expires_at:
                expired_lease = self._expire_lease_locked(time.time())
                error = "NO_ACTIVE_LEASE" if expired_lease or self._active_lease_owner is None else "RESOURCE_BUSY"
            else:
                expired_lease = None
                error = None
            if error is None and client_id in self._active_stream_owners:
                raise CameraStreamCapacityError("STREAM_ALREADY_ACTIVE")
            if error is None and len(self._active_stream_owners) >= MAX_CAMERA_STREAMS:
                raise CameraStreamCapacityError("STREAM_CAPACITY")
            if error is None:
                self._active_stream_owners.add(client_id)
                lease_generation = self._capture_generation
            last_seq = self._frame_seq
        if expired_lease:
            owner, lease_generation = expired_lease
            self.pipeline.stop_if_generation(pipeline_generation)
            self._trigger_teardown(owner, "expired", lease_generation)
        if error:
            raise CameraStreamLeaseError(error)

        def release_reservation() -> None:
            with self._lock:
                self._active_stream_owners.discard(client_id)

        def stream() -> Iterator[bytes]:
            nonlocal last_seq
            last_emit = 0.0
            first_frame = True
            last_valid_frame_at = time.monotonic()
            while True:
                if not self.is_lease_valid():
                    return
                with self._frame_changed:
                    if (
                        self._active_lease_owner != client_id
                        or self._capture_generation != lease_generation
                    ):
                        return
                    deadline = time.monotonic() + max(0.05, min(1.0, wait_timeout))
                    frame = None
                    lease_expired = False
                    while True:
                        if (
                            self._active_lease_owner != client_id
                            or self._capture_generation != lease_generation
                        ):
                            return
                        if time.time() >= self._lease_expires_at:
                            lease_expired = True
                            break
                        now = time.monotonic()
                        if self._latest_jpeg is not None and self._frame_seq > last_seq:
                            age_ms = (time.monotonic() - self._latest_frame_monotonic) * 1000
                            if age_ms <= LIVE_CAMERA_STALE_THRESHOLD_MS and self._is_hardware_frame:
                                candidate = {
                                    "jpeg_data": self._latest_jpeg,
                                    "frame_seq": self._frame_seq,
                                    "frame_age_ms": max(0, int(age_ms)),
                                    "metadata": dict(self._latest_frame_metadata or {}),
                                }
                                if now - last_emit >= interval:
                                    frame = candidate
                                    break
                        remaining = deadline - now
                        if self._latest_jpeg is not None and self._latest_frame_monotonic > 0:
                            until_live_expiry = (
                                self._latest_frame_monotonic
                                + LIVE_CAMERA_STALE_THRESHOLD_MS / 1000.0
                                - now
                            )
                            remaining = min(remaining, max(0.0, until_live_expiry))
                        if remaining <= 0:
                            break
                        self._frame_changed.wait(min(remaining, interval))
                    if not lease_expired and frame is None and self._latest_jpeg is not None:
                        age_ms = (time.monotonic() - self._latest_frame_monotonic) * 1000
                        if age_ms > LIVE_CAMERA_STALE_THRESHOLD_MS:
                            self._clear_latest_frame_locked()
                            self._frame_changed.notify_all()
                    if not lease_expired and frame is None and first_frame:
                        raise CameraStreamFrameUnavailable("NO_FRESH_FRAME")
                    if not lease_expired and frame is None and not first_frame and time.monotonic() - last_valid_frame_at > LIVE_CAMERA_STALE_THRESHOLD_MS / 1000.0:
                        return
                if lease_expired:
                    # Re-enter the standard expiry path outside the condition lock
                    # so it clears the latest frame and invokes the teardown callback.
                    self.is_lease_valid()
                    return
                if frame is None:
                    continue
                first_frame = False
                last_valid_frame_at = time.monotonic()
                last_seq = frame["frame_seq"]
                last_emit = time.monotonic()
                jpeg = frame["jpeg_data"]
                part = (
                    f"--{boundary}\r\n"
                    "Content-Type: image/jpeg\r\n"
                    f"Content-Length: {len(jpeg)}\r\n"
                    f"X-Frame-Seq: {frame['frame_seq']}\r\n"
                    f"X-Frame-Age-Ms: {frame['frame_age_ms']}\r\n"
                    + self._metadata_headers(frame["metadata"])
                    + "\r\n"
                ).encode("ascii") + jpeg + b"\r\n"
                yield part

        return _ReservedCameraStream(stream(), release_reservation)

    @staticmethod
    def _metadata_headers(metadata: dict[str, Any]) -> str:
        """Encode only bounded, validated CR-022 timing/session metadata headers."""
        names = {
            "sequence": "X-Device-Sequence",
            "capture_timestamp_ms": "X-Device-Capture-Monotonic-Ms",
            "host_receive_monotonic_ms": "X-Host-Receive-Monotonic-Ms",
            "clock_offset_ms": "X-Clock-Offset-Ms",
            "clock_uncertainty_ms": "X-Clock-Uncertainty-Ms",
            "latency_ms": "X-Source-To-Host-Receive-Ms",
        }
        lines = []
        uint32_fields = {"sequence", "capture_timestamp_ms", "host_receive_monotonic_ms"}
        for key, name in names.items():
            value = metadata.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                continue
            if key in uint32_fields:
                if not isinstance(value, int) or not 0 <= value <= 0xFFFFFFFF:
                    continue
            elif abs(value) > 0x7FFFFFFF or not math.isfinite(value):
                continue
            lines.append(f"{name}: {value}\r\n")
        generation = metadata.get("session_generation")
        if isinstance(generation, str) and generation.isascii() and generation and len(generation) <= 128 and all(ch.isalnum() or ch in "-_" for ch in generation):
            lines.append(f"X-Session-Generation: {generation}\r\n")
        return "".join(lines)

    def _clear_latest_frame_locked(self) -> None:
        self._latest_jpeg = None
        self._latest_frame_time = 0.0
        self._latest_frame_monotonic = 0.0
        self._latest_frame_metadata = None
        self._is_hardware_frame = False

    def get_latest_frame(self, client_id: str) -> Optional[dict[str, Any]]:
        """
        Retrieves latest frame for authenticated lease owner.
        Applies stale frame masking/blanking if frame age exceeds threshold.
        """
        if not self.is_lease_valid():
            return None
        with self._lock:
            if self._active_lease_owner != client_id or time.time() >= self._lease_expires_at:
                return None

            if self._latest_jpeg is None:
                return None

            now = time.time()
            age_ms_exact = max(0.0, (now - self._latest_frame_time) * 1000)
            # Compare the unrounded age so a frame at (for example) 2000.5 ms
            # cannot pass a 2000 ms freshness limit after integer truncation.
            is_stale = age_ms_exact > self.stale_threshold_ms

            if is_stale:
                # A stale image is unavailable, not a usable snapshot. This
                # prevents consumers from mistaking an old scene for live data.
                return None

            is_hw = getattr(self, "_is_hardware_frame", False)
            return {
                "frame_seq": self._frame_seq,
                "jpeg_data": self._latest_jpeg,
                "frame_age_ms": int(age_ms_exact),
                "is_stale": False,
                "is_masked": False,
                "source": "hardware_sensor" if is_hw else "external_source",
                "is_hardware": is_hw,
                "metadata": dict(self._latest_frame_metadata or {}),
            }

    def get_snapshot(
        self,
        client_id: Optional[str] = None,
        auto_acquire_ms: int = 5000,
        wait_timeout_ms: int = 250,
    ) -> Optional[dict[str, Any]]:
        """
        Retrieves a fresh snapshot JPEG frame.
        If client_id is provided and holds an active lease, returns the latest frame.
        If no active lease is held, auto-acquires a short ephemeral lease. If the
        latest frame is absent or stale, waits briefly for any producer (local
        capture worker or asynchronous SDK ingress) to publish a fresh JPEG.
        """
        if isinstance(wait_timeout_ms, bool) or not isinstance(wait_timeout_ms, int) or not 0 <= wait_timeout_ms <= 5000:
            raise ValueError("wait_timeout_ms must be an integer between 0 and 5000")
        acquired_ephemeral = False
        ephemeral_lease_generation: Optional[int] = None
        if self.is_lease_valid():
            with self._lock:
                # An active lease is held: caller must explicitly match the lease owner.
                if not client_id or client_id != self._active_lease_owner:
                    return None
                effective_client = client_id
        else:
            effective_client = client_id if client_id else "snapshot-viewer"
            acq = self.acquire_lease(effective_client, lease_ms=auto_acquire_ms)
            if not acq.get("granted"):
                return None
            acquired_ephemeral = True
            ephemeral_lease_generation = acq["lease_generation"]

        try:
            # SDK ingress is independent from pipeline._running. Wait on the
            # same condition used by all producers instead of assuming that
            # the local capture worker is responsible for delivering a frame.
            frame = self.get_latest_frame(effective_client)
            if frame is None and wait_timeout_ms:
                frame = self._wait_for_fresh_frame(effective_client, wait_timeout_ms / 1000.0)
            return frame
        finally:
            if acquired_ephemeral:
                self.release_lease(
                    effective_client,
                    expected_generation=ephemeral_lease_generation,
                )

    def _wait_for_fresh_frame(self, client_id: str, timeout_s: float) -> Optional[dict[str, Any]]:
        """Wait for a fresh JPEG from any producer while this client owns the lease."""
        deadline = time.monotonic() + timeout_s
        expired_lease: Optional[tuple[str, int]] = None
        expired_pipeline_generation: Optional[int] = None
        frame: Optional[dict[str, Any]] = None
        with self._frame_changed:
            while True:
                now_wall = time.time()
                if self._active_lease_owner != client_id:
                    break
                if now_wall >= self._lease_expires_at:
                    expired_pipeline_generation = self._pipeline_generation
                    expired_lease = self._expire_lease_locked(now_wall)
                    break
                age_ms = (time.monotonic() - self._latest_frame_monotonic) * 1000
                if (
                    self._latest_jpeg is not None
                    and self._latest_frame_monotonic > 0
                    and age_ms <= self.stale_threshold_ms
                ):
                    is_hw = self._is_hardware_frame
                    frame = {
                        "frame_seq": self._frame_seq,
                        "jpeg_data": self._latest_jpeg,
                        "frame_age_ms": max(0, int(age_ms)),
                        "is_stale": False,
                        "is_masked": False,
                        "source": "hardware_sensor" if is_hw else "external_source",
                        "is_hardware": is_hw,
                        "metadata": dict(self._latest_frame_metadata or {}),
                    }
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._frame_changed.wait(remaining)
        if expired_lease:
            expired_owner, lease_generation = expired_lease
            self.pipeline.stop_if_generation(expired_pipeline_generation)
            self._trigger_teardown(expired_owner, "expired", lease_generation)
        return frame
