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


class OfficialRobotCameraAdapter:
    """Capture bounded JPEGs through the official SDK while a viewer lease is active."""

    def __init__(
        self,
        robot: Any,
        *,
        send_command: Callable[[str, dict[str, Any], float | None], dict[str, Any]],
        ingest_frame: Callable[[bytes], None],
        max_jpeg_bytes: int = DEFAULT_MAX_JPEG_BYTES,
        capture_timeout: float = 3.0,
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
        self._owner: str | None = None
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

    def acquire(self, client_id: str, lease_ms: int, target_fps: float = 2.0) -> dict[str, Any]:
        if not client_id or isinstance(lease_ms, bool) or lease_ms <= 0:
            raise ValueError("a client id and positive lease duration are required")
        if not 0.1 <= target_fps <= 30:
            raise ValueError("target_fps must be between 0.1 and 30")
        with self._lock:
            if not self.is_connected:
                return {"granted": False, "reason": "UPSTREAM_UNAVAILABLE"}
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
                return {"granted": True, "client_id": client_id, "lease_duration_ms": lease_ms}

            ack = self._send_command(
                "ctrl.camera.acquire",
                {"client_id": client_id, "lease_duration_ms": lease_ms},
                1.0,
            )
            if not self._ack_succeeded(ack):
                self._last_error = f"camera acquire rejected: {ack!r}"
                return {"granted": False, "reason": self._error_code(ack), "device_ack": ack}

            self._owner = client_id
            self._lease_ms = lease_ms
            self._lease_deadline = time.monotonic() + lease_ms / 1000
            self._interval = 1.0 / target_fps
            stop_event = threading.Event()
            self._stop_event = stop_event
            self._thread = threading.Thread(
                target=self._capture_loop,
                args=(client_id, stop_event),
                name="official-sdk-camera-capture",
                daemon=True,
            )
            self._thread.start()
            self._last_error = None
            return {"granted": True, "client_id": client_id, "lease_duration_ms": lease_ms}

    def release(self, client_id: str) -> bool:
        with self._lock:
            if self._owner != client_id:
                return False
            self._owner = None
            self._lease_deadline = 0.0
            stop_event, thread = self._stop_event, self._thread
            self._stop_event = None
            self._thread = None
            if stop_event:
                stop_event.set()
        if thread and thread is not threading.current_thread():
            thread.join(timeout=self._capture_timeout + 0.25)
        try:
            ack = self._send_command("ctrl.camera.release", {"client_id": client_id}, 1.0)
            if not self._ack_succeeded(ack):
                raise RuntimeError(f"device rejected camera release: {ack!r}")
        except Exception as error:
            with self._lock:
                self._last_error = f"camera release unconfirmed: {error}"
            logger.warning("Official SDK camera release unconfirmed: %s", error)
        if self._clear_frame:
            self._clear_frame(client_id)
        return True

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            owner = self._owner
            self._owner = None
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
            self._clear_frame(owner)

    def close(self) -> None:
        with self._lock:
            owner = self._owner
            self._closed = True
        if owner:
            self.release(owner)
        self.disconnect()

    def _capture_loop(self, owner: str, stop_event: threading.Event) -> None:
        while not stop_event.is_set():
            started = time.monotonic()
            with self._lock:
                deadline = self._lease_deadline
            remaining_lease = deadline - started
            if remaining_lease <= 0:
                self._expire_owner(owner, stop_event)
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
                    if self._owner != owner or not self._connected or self._closed:
                        return
                    if time.monotonic() >= self._lease_deadline:
                        self._expire_owner(owner, stop_event)
                        return
                    self._last_error = None
                    interval = self._interval
                self._ingest_frame(data)
            except Exception as error:
                with self._lock:
                    self._last_error = str(error)
                logger.warning("Official SDK camera capture failed: %s", error)
                # Do not reuse a cached image after a failed capture.
                if self._clear_frame:
                    self._clear_frame(owner)
                interval = self._interval
            stop_event.wait(max(0.0, interval - (time.monotonic() - started)))

    def _expire_owner(self, owner: str, stop_event: threading.Event) -> None:
        with self._lock:
            if self._owner != owner:
                return
            self._owner = None
            self._lease_deadline = 0.0
            if self._stop_event is stop_event:
                self._stop_event = None
                self._thread = None
            stop_event.set()
        try:
            ack = self._send_command("ctrl.camera.release", {"client_id": owner}, 1.0)
            if not self._ack_succeeded(ack):
                raise RuntimeError(f"device rejected camera release: {ack!r}")
        except Exception as error:
            with self._lock:
                self._last_error = f"expired camera release unconfirmed: {error}"
            logger.warning("Expired official SDK camera lease release unconfirmed: %s", error)
        if self._clear_frame:
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
