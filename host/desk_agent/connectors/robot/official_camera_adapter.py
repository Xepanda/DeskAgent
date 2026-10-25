"""Bridge an already-connected official WatcheRobot SDK session to Host camera leases.

The adapter owns no SDK Runtime or connection. A Daemon-managed Application (or
another authorized SDK owner) supplies its connected ``WatcheRobot`` and the
same session's command sender.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_MAX_JPEG_BYTES = 64 * 1024
MAX_WSPK_FRAME_BYTES = 131088  # 16-byte header + 128 KiB payload
MAX_HOST_MJPEG_FPS = 2.0


class OfficialRobotCameraAdapter:
    """Capture bounded JPEGs through the official SDK while a viewer lease is active."""

    def __init__(
        self,
        robot: Any,
        *,
        send_command: Callable[[str, dict[str, Any], float | None], dict[str, Any]],
        ingest_frame: Callable[[bytes], None],
        max_jpeg_bytes: int = DEFAULT_MAX_JPEG_BYTES,
        capture_timeout: float = 7.0,
        clear_frame: Callable[[str], None] | None = None,
    ) -> None:
        if not 4 <= max_jpeg_bytes <= 128 * 1024:
            raise ValueError("max_jpeg_bytes must be between 4 and 131072")
        if capture_timeout <= 0:
            raise ValueError("capture_timeout must be positive")
        self._robot = robot
        self._send_command = send_command
        self._ingest_frame = ingest_frame
        self._clear_frame = clear_frame
        self._max_jpeg_bytes = max_jpeg_bytes
        self._capture_timeout = capture_timeout
        self._lock = threading.RLock()
        # Device acquire/release commands are ordered independently from the
        # capture worker lock. Never join a worker while holding this lock.
        self._lifecycle_lock = threading.RLock()
        self._owner: str | None = None
        self._lease_generation: int | None = None
        self._local_generation = 0
        self._blocked_thread: threading.Thread | None = None
        self._lease_ms = 0
        self._lease_deadline = 0.0
        self._interval = 0.5
        self._stop_event: threading.Event | None = None
        self._thread: threading.Thread | None = None
        self._connected = True
        self._closed = False
        self._last_error: str | None = None

    @property
    def is_connected(self) -> bool:
        with self._lock:
            return self._connected and not self._closed

    @property
    def last_error(self) -> str | None:
        with self._lock:
            return self._last_error

    def acquire(
        self, client_id: str, lease_ms: int, target_fps: float = 2.0,
        *, lease_generation: int | None = None,
    ) -> dict[str, Any]:
        if not client_id or isinstance(lease_ms, bool) or lease_ms <= 0:
            raise ValueError("a client id and positive lease duration are required")
        if not 0.1 <= target_fps <= 30:
            raise ValueError("target_fps must be between 0.1 and 30")
        replaced_thread = None
        with self._lifecycle_lock:
            with self._lock:
                replacing_generation = (
                    self._owner == client_id and lease_generation is not None
                    and lease_generation != self._lease_generation
                )
                if replacing_generation:
                    replaced_thread = self._thread
                    if replaced_thread is not None and replaced_thread.is_alive():
                        self._blocked_thread = replaced_thread
                    self._release_locked(client_id, self._lease_generation)
        if replaced_thread and replaced_thread is not threading.current_thread():
            replaced_thread.join(timeout=self._capture_timeout + 0.25)
            if replaced_thread.is_alive():
                return {"granted": False, "reason": "CAPTURE_STOP_TIMEOUT"}
        with self._lifecycle_lock, self._lock:
            if not self.is_connected:
                return {"granted": False, "reason": "UPSTREAM_UNAVAILABLE"}
            if self._blocked_thread is not None:
                if self._blocked_thread.is_alive():
                    return {"granted": False, "reason": "CAPTURE_STOP_TIMEOUT"}
                self._blocked_thread = None
            if self._owner and self._owner != client_id:
                return {"granted": False, "reason": "RESOURCE_BUSY", "current_owner": self._owner}
            if self._owner == client_id:
                ack = self._send_command(
                    "ctrl.camera.acquire",
                    {"client_id": client_id, "lease_duration_ms": lease_ms},
                    1.0,
                )
                if not self._ack_succeeded(ack):
                    self._last_error = f"camera lease renewal rejected: {ack!r}"
                    return {"granted": False, "reason": self._error_code(ack), "device_ack": ack}
                self._lease_ms = lease_ms
                self._lease_deadline = time.monotonic() + lease_ms / 1000
                self._interval = 1.0 / min(target_fps, MAX_HOST_MJPEG_FPS)
                return {"granted": True, "client_id": client_id, "lease_duration_ms": lease_ms,
                        "lease_generation": self._lease_generation}

            ack = self._send_command(
                "ctrl.camera.acquire",
                {"client_id": client_id, "lease_duration_ms": lease_ms},
                1.0,
            )
            if not self._ack_succeeded(ack):
                self._last_error = f"camera acquire rejected: {ack!r}"
                return {"granted": False, "reason": self._error_code(ack), "device_ack": ack}

            self._owner = client_id
            if lease_generation is None:
                self._local_generation += 1
                lease_generation = self._local_generation
            self._lease_generation = lease_generation
            self._lease_ms = lease_ms
            self._lease_deadline = time.monotonic() + lease_ms / 1000
            self._interval = 1.0 / min(target_fps, MAX_HOST_MJPEG_FPS)
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._thread = threading.Thread(
                target=self._capture_loop,
                args=(client_id, stop_event, lease_generation),
                name="official-sdk-camera-capture",
                daemon=True,
            )
            self._thread.start()
            self._last_error = None
            return {"granted": True, "client_id": client_id, "lease_duration_ms": lease_ms,
                    "lease_generation": lease_generation}

    def release(self, client_id: str, *, expected_generation: int | None = None) -> bool:
        with self._lifecycle_lock:
            with self._lock:
                if (self._owner != client_id
                        or (expected_generation is not None and expected_generation != self._lease_generation)):
                    return False
                thread = self._thread
                released_generation = self._lease_generation
            if thread is not None and thread.is_alive():
                self._blocked_thread = thread
            if not self._release_locked(client_id, released_generation):
                return False
        if thread and thread is not threading.current_thread():
            thread.join(timeout=self._capture_timeout + 0.25)
            if thread.is_alive():
                with self._lock:
                    self._blocked_thread = thread
        return True

    def _release_locked(self, client_id: str, expected_generation: int | None) -> bool:
        """Release the current device generation; caller holds lifecycle lock."""
        with self._lock:
            if (self._owner != client_id
                    or (expected_generation is not None and expected_generation != self._lease_generation)):
                return False
            self._owner = None
            self._lease_generation = None
            self._lease_deadline = 0.0
            stop_event, thread = self._stop_event, self._thread
            self._stop_event = None
            self._thread = None
            if stop_event:
                stop_event.set()
        try:
            ack = self._send_command("ctrl.camera.release", {"client_id": client_id}, 1.0)
            if not self._ack_succeeded(ack):
                raise RuntimeError(f"device rejected camera release: {ack!r}")
        except Exception as error:
            with self._lock:
                self._last_error = f"camera release unconfirmed: {error}"
            logger.warning("Official SDK camera release unconfirmed: %s", error)
        if self._clear_frame:
            self._clear_frame_for_generation(client_id, expected_generation)
        return True

    def disconnect(self) -> None:
        """Explicit cleanup path; may wait for an in-flight capture to finish."""
        with self._lock:
            self._connected = False
            owner = self._owner
            generation = self._lease_generation
            self._owner = None
            self._lease_generation = None
            self._lease_deadline = 0.0
            stop_event, thread = self._stop_event, self._thread
            self._stop_event = None
            self._thread = None
            self._last_error = "official SDK device session disconnected"
            if stop_event:
                stop_event.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=self._capture_timeout + 0.25)
        if owner and self._clear_frame:
            self._clear_frame_for_generation(owner, generation)

    def on_sdk_disconnect(self) -> None:
        """Non-blocking SDK callback path: signal capture stop and clear stale frame."""
        with self._lock:
            self._connected = False
            owner = self._owner
            generation = self._lease_generation
            self._owner = None
            self._lease_generation = None
            self._lease_ms = 0
            self._lease_deadline = 0.0
            stop_event = self._stop_event
            self._stop_event = None
            self._thread = None
            self._last_error = "official SDK device session disconnected"
            if stop_event:
                stop_event.set()
        if owner and self._clear_frame:
            self._clear_frame_for_generation(owner, generation)

    def close(self) -> None:
        with self._lock:
            owner = self._owner
            self._closed = True
        if owner:
            self.release(owner)
        self.disconnect()

    def _capture_loop(self, owner: str, stop_event: threading.Event, lease_generation: int) -> None:
        while not stop_event.is_set():
            started = time.monotonic()
            with self._lock:
                deadline = self._lease_deadline
            remaining_lease = deadline - started
            if remaining_lease <= 0:
                self._stop_owner(owner, stop_event, "lease expired", lease_generation=lease_generation)
                return
            try:
                image = self._robot.camera.capture(timeout=min(self._capture_timeout, remaining_lease))
                data = getattr(image, "data", None)
                size = len(data) if isinstance(data, bytes) else 0
                if not isinstance(data, bytes) or not 4 <= size <= self._max_jpeg_bytes:
                    raise ValueError(f"camera JPEG size outside 4..{self._max_jpeg_bytes} bytes")
                if data[:2] != b"\xff\xd8" or data[-2:] != b"\xff\xd9":
                    raise ValueError("camera result is not a complete JPEG")
                if getattr(image, "content_type", "image/jpeg") != "image/jpeg":
                    raise ValueError("camera result content type is not image/jpeg")
                if not isinstance(getattr(image, "session_id", None), int) or image.session_id <= 0:
                    raise ValueError("camera result has no correlated capture session")
                with self._lock:
                    if (self._owner != owner or self._lease_generation != lease_generation
                            or self._stop_event is not stop_event or not self._connected or self._closed):
                        return
                    expired = time.monotonic() >= self._lease_deadline
                    self._last_error = None
                    interval = self._interval
                if expired:
                    self._stop_owner(owner, stop_event, "lease expired", lease_generation=lease_generation)
                    return
                self._ingest_frame(data)
            except Exception as error:
                with self._lock:
                    self._last_error = str(error)
                logger.warning("Official SDK camera capture failed: %s", error)
                # A failed request has no trustworthy fresh image. Stop this
                # lease instead of retrying forever or leaving the prior JPEG
                # available as if it were the result of the failed capture.
                self._stop_owner(owner, stop_event, "capture failed", lease_generation=lease_generation)
                return
            stop_event.wait(max(0.0, interval - (time.monotonic() - started)))

    def _stop_owner(
        self, owner: str, stop_event: threading.Event, reason: str,
        *, lease_generation: int | None = None,
    ) -> None:
        with self._lifecycle_lock:
            with self._lock:
                if (self._owner != owner or self._stop_event is not stop_event
                        or (lease_generation is not None and self._lease_generation != lease_generation)):
                    return
                generation = self._lease_generation
            self._release_locked(owner, generation)

    def _clear_frame_for_generation(self, owner: str, generation: int | None) -> None:
        if not self._clear_frame:
            return
        try:
            self._clear_frame(owner, expected_generation=generation)
        except TypeError:
            # Keep the adapter usable with legacy one-argument callbacks.
            self._clear_frame(owner)

    @staticmethod
    def _ack_succeeded(ack: Any) -> bool:
        return isinstance(ack, dict) and ack.get("type") == "sys.ack" and ack.get("code") == 0

    @staticmethod
    def _error_code(ack: Any) -> str:
        if isinstance(ack, dict):
            error = ack.get("error")
            if isinstance(error, dict) and isinstance(error.get("code"), str):
                return error["code"]
        return "DEVICE_ERROR"
