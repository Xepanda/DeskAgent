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
import threading
import time
from collections.abc import Iterator
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

MAX_CAMERA_JPEG_BYTES = 128 * 1024
MAX_CAMERA_STREAMS = 4
MAX_CAMERA_STREAMS_PER_OWNER = 1

LeaseReleaseCallback = Callable[[str, str], None]


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
        self._current_pan = 90.0
        self._current_tilt = 120.0
        self._consumer: Optional[Callable[[bytes, bool], None]] = None
        self._frame_source: Optional[Callable[[], Optional[bytes]]] = None

    def set_frame_source(self, source: Optional[Callable[[], Optional[bytes]]]) -> None:
        """Sets external hardware frame capture callback (e.g. V4L2 device or robot frame stream)."""
        with self._lock:
            self._frame_source = source

    def start(self, consumer: Callable[[bytes, bool], None]) -> None:
        with self._lock:
            self._consumer = consumer
            if self._running:
                return
            self._running = True
            # A disconnect can stop the pipeline while the source callback is
            # blocked in transport I/O. If the lease returns before that call
            # unwinds, resume the existing worker instead of creating a second
            # concurrent caller for a non-reentrant camera source.
            if self._thread and self._thread.is_alive():
                return
            self._thread = threading.Thread(target=self._capture_loop, name="camera-capture-pipeline", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self.request_stop()
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
            if thread and not thread.is_alive():
                self._thread = None

    def request_stop(self) -> None:
        """Requests capture shutdown without waiting for the capture worker.

        Use this from upstream disconnect callbacks, where the frame source may
        currently be blocked in transport I/O. Call ``stop`` later when joining
        the worker is safe and desired.
        """
        # These assignments intentionally do not acquire _lock: stop() may hold
        # it while joining the worker, so the disconnect callback must stay
        # independent of that join and any in-flight frame-source call.
        self._running = False
        self._consumer = None

    def update_coordinates(self, pan: float, tilt: float) -> None:
        with self._lock:
            self._current_pan = max(0.0, min(180.0, pan))
            self._current_tilt = max(100.0, min(140.0, tilt))

    def _capture_loop(self) -> None:
        while self._running:
            try:
                frame = None
                is_hw = False
                if self._frame_source:
                    try:
                        frame = self._frame_source()
                        if frame:
                            is_hw = True
                    except Exception as src_err:
                        logger.debug("Hardware frame source error: %s", src_err)

                if frame is not None and self._consumer and self._running:
                    try:
                        self._consumer(frame, is_hw)
                    except TypeError:
                        self._consumer(frame)
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
        self._lease_expires_at: float = 0.0

        # Bounded single latest frame buffer
        self._latest_jpeg: Optional[bytes] = None
        self._latest_frame_time: float = 0.0
        self._frame_seq: int = 0
        self._is_hardware_frame: bool = False
        self._active_stream_owners: set[str] = set()
        self.pipeline = FrameCapturePipeline(fps=10.0, allow_synthetic=False)

    def update_motion_coordinates(self, pan: float, tilt: float) -> None:
        """Updates gimbal pan/tilt overlay on the active camera feed."""
        self.pipeline.update_coordinates(pan, tilt)

    def start_capture(self) -> None:
        """Starts the underlying capture pipeline."""
        self.pipeline.start(self.push_frame)

    def stop_capture(self) -> None:
        """Stops the underlying capture pipeline."""
        self.pipeline.stop()

    def set_hardware_frame_source(self, source_fn: Optional[Callable[[], Optional[bytes]]]) -> None:
        """Sets external hardware frame capture callback (e.g. V4L2 device or robot frame stream)."""
        self.pipeline.set_frame_source(source_fn)

    def ingest_robot_frame(self, frame_bytes: bytes) -> None:
        """Ingests live raw JPEG frame from physical robot camera link."""
        self.push_frame(frame_bytes, is_hardware=True)

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

        expired_owner = None
        with self._lock:
            expired_owner = self._expire_lease_locked(now)
            if self._active_lease_owner and self._active_lease_owner != client_id:
                return {
                    "granted": False,
                    "reason": "RESOURCE_BUSY",
                    "current_owner": self._active_lease_owner,
                    "remaining_ms": int((self._lease_expires_at - now) * 1000),
                }

            self._active_lease_owner = client_id
            self._lease_expires_at = now + duration

            if target_fps is not None:
                self.pipeline.fps = max(0.5, min(30.0, float(target_fps)))
                self.pipeline._interval = 1.0 / self.pipeline.fps

            result = {
                "granted": True,
                "client_id": client_id,
                "lease_id": f"lease-{client_id}",
                "lease_duration_ms": int(duration * 1000),
                "expires_at": self._lease_expires_at,
                "target_fps": self.pipeline.fps,
            }

        if expired_owner:
            self.pipeline.stop()
            self._trigger_teardown(expired_owner, "expired")
        if self.auto_start_pipeline:
            self.pipeline.start(self.push_frame)
        return result

    def release_lease(self, client_id: str) -> bool:
        """Explicitly releases camera viewing lease."""
        with self._lock:
            if self._active_lease_owner == client_id:
                owner = self._active_lease_owner
                self._active_lease_owner = None
                self._lease_expires_at = 0.0
                self._latest_jpeg = None
                self._is_hardware_frame = False
                self._frame_changed.notify_all()
            else:
                return False
        self.pipeline.stop()
        self._trigger_teardown(owner, "explicit_release")
        return True

    def invalidate_owner_on_upstream_disconnect(self, client_id: str) -> bool:
        """Invalidates camera state after the owning SDK transport disconnects.

        This is deliberately a non-blocking invalidation path: it clears the
        owner's lease and buffered frame, wakes stream waiters, and requests
        capture shutdown without invoking ``on_lease_released`` or joining the
        capture worker. The normal ``release_lease`` path remains responsible
        for teardown callbacks and synchronous pipeline stopping.
        """
        with self._lock:
            if self._active_lease_owner != client_id:
                return False
            self._active_lease_owner = None
            self._lease_expires_at = 0.0
            self._latest_jpeg = None
            self._latest_frame_time = 0.0
            self._is_hardware_frame = False
            self._frame_changed.notify_all()
        self.pipeline.request_stop()
        return True

    def is_lease_valid(self) -> bool:
        """Checks if current camera lease is valid and active."""
        with self._lock:
            owner = self._expire_lease_locked(time.time())
            valid = self._active_lease_owner is not None
        if owner:
            self.pipeline.stop()
            self._trigger_teardown(owner, "expired")
        return valid

    def _expire_lease_locked(self, now: float) -> Optional[str]:
        """Clears expired owner/frame state; caller performs teardown after unlock."""
        if self._active_lease_owner and now >= self._lease_expires_at:
            owner = self._active_lease_owner
            self._active_lease_owner = None
            self._lease_expires_at = 0.0
            self._latest_jpeg = None
            self._is_hardware_frame = False
            self._frame_changed.notify_all()
            return owner
        return None

    def _trigger_teardown(self, owner: str, reason: str) -> None:
        if self.on_lease_released:
            try:
                self.on_lease_released(owner, reason)
            except Exception as exc:
                logger.error("Error executing camera teardown callback: %s", exc)

    def push_frame(self, jpeg_data: bytes, is_hardware: bool = False) -> None:
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
            if self._active_lease_owner and time.time() < self._lease_expires_at:
                # Never let a synthetic frame overwrite a non-stale live hardware frame
                if not is_hardware and getattr(self, "_is_hardware_frame", False) and self._latest_jpeg is not None:
                    now = time.time()
                    if (now - self._latest_frame_time) * 1000 < self.stale_threshold_ms:
                        return
                self._latest_jpeg = jpeg_data
                self._latest_frame_time = time.time()
                self._is_hardware_frame = is_hardware
                self._frame_seq += 1
                self._frame_changed.notify_all()

    def iter_mjpeg(
        self,
        client_id: str,
        *,
        boundary: str = "deskagent-frame",
        target_fps: float = 2.0,
        wait_timeout: float = 0.5,
    ) -> Iterator[bytes]:
        """Yield fresh physical JPEG parts while the caller owns the camera lease.

        Admission is synchronous: one stream per owner, at most four globally.
        Duplicate owners raise STREAM_ALREADY_ACTIVE; global exhaustion raises
        STREAM_CAPACITY. The iterator starts after the currently buffered frame, so it never replays
        a snapshot as the first stream frame. Each wait is bounded, allowing lease
        expiry and generator close to be observed promptly without a background task.
        """
        fps = max(0.1, min(2.0, float(target_fps)))
        interval = 1.0 / fps
        with self._lock:
            owner = None
            if self._active_lease_owner != client_id or time.time() >= self._lease_expires_at:
                owner = self._expire_lease_locked(time.time())
                error = "NO_ACTIVE_LEASE" if owner or self._active_lease_owner is None else "RESOURCE_BUSY"
            else:
                error = None
            if error is None and client_id in self._active_stream_owners:
                raise CameraStreamCapacityError("STREAM_ALREADY_ACTIVE")
            if error is None and len(self._active_stream_owners) >= MAX_CAMERA_STREAMS:
                raise CameraStreamCapacityError("STREAM_CAPACITY")
            if error is None:
                self._active_stream_owners.add(client_id)
            last_seq = self._frame_seq
        if owner:
            self.pipeline.stop()
            self._trigger_teardown(owner, "expired")
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
                    if self._active_lease_owner != client_id:
                        return
                    deadline = time.monotonic() + max(0.05, min(1.0, wait_timeout))
                    frame = None
                    lease_expired = False
                    while True:
                        if self._active_lease_owner != client_id:
                            return
                        if time.time() >= self._lease_expires_at:
                            lease_expired = True
                            break
                        now = time.monotonic()
                        if self._latest_jpeg is not None and self._frame_seq > last_seq:
                            age_ms = (time.time() - self._latest_frame_time) * 1000
                            if age_ms <= self.stale_threshold_ms and self._is_hardware_frame:
                                candidate = {
                                    "jpeg_data": self._latest_jpeg,
                                    "frame_seq": self._frame_seq,
                                    "frame_age_ms": max(0, int(age_ms)),
                                }
                                if now - last_emit >= interval:
                                    frame = candidate
                                    break
                        remaining = deadline - now
                        if remaining <= 0:
                            break
                        self._frame_changed.wait(min(remaining, interval))
                    if not lease_expired and frame is None and first_frame:
                        raise CameraStreamFrameUnavailable("NO_FRESH_FRAME")
                    if not lease_expired and frame is None and not first_frame and time.monotonic() - last_valid_frame_at > self.stale_threshold_ms / 1000.0:
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
                    f"X-Frame-Age-Ms: {frame['frame_age_ms']}\r\n\r\n"
                ).encode("ascii") + jpeg + b"\r\n"
                yield part

        return _ReservedCameraStream(stream(), release_reservation)

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
            }

    def get_snapshot(
        self,
        client_id: Optional[str] = None,
        auto_acquire_ms: int = 5000,
    ) -> Optional[dict[str, Any]]:
        """
        Retrieves a fresh snapshot JPEG frame.
        If client_id is provided and holds an active lease, returns the latest frame.
        If no active lease is held, auto-acquires a short ephemeral lease and awaits
        a fresh frame from the capture pipeline.
        """
        acquired_ephemeral = False
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

        try:
            # Check latest frame, waiting briefly if a physical source is starting.
            frame = self.get_latest_frame(effective_client)
            if frame is None and self.pipeline._running:
                deadline = time.time() + 0.25
                while time.time() < deadline:
                    time.sleep(0.02)
                    frame = self.get_latest_frame(effective_client)
                    if frame is not None:
                        break
            return frame
        finally:
            if acquired_ephemeral:
                self.release_lease(effective_client)
