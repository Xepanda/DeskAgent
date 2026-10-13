"""
Desk Agent R1 LAN Camera Service & Web Viewer.
Provides local network authenticated JPEG frame viewing,
lease duration management, frame freshness tracking, and automatic stop upon lease expiration.
"""

from __future__ import annotations

import time
import threading
from typing import Any, Optional


class CameraViewerService:
    def __init__(self, default_lease_ms: int = 10000) -> None:
        self.default_lease_ms = default_lease_ms
        self._lock = threading.RLock()
        self._active_lease_owner: Optional[str] = None
        self._lease_expires_at: float = 0.0
        self._latest_jpeg: bytes = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00\x48\x00\x48\x00\x00\xff\xd9"
        self._latest_frame_time: float = 0.0
        self._frame_seq: int = 0

    def acquire_lease(self, client_id: str, lease_ms: Optional[int] = None) -> dict[str, Any]:
        duration = (lease_ms or self.default_lease_ms) / 1000.0
        now = time.time()
        with self._lock:
            if self._active_lease_owner and self._active_lease_owner != client_id and now < self._lease_expires_at:
                return {
                    "granted": False,
                    "reason": "RESOURCE_BUSY",
                    "current_owner": self._active_lease_owner,
                    "remaining_ms": int((self._lease_expires_at - now) * 1000),
                }

            self._active_lease_owner = client_id
            self._lease_expires_at = now + duration
            return {
                "granted": True,
                "client_id": client_id,
                "lease_duration_ms": int(duration * 1000),
                "expires_at": self._lease_expires_at,
            }

    def release_lease(self, client_id: str) -> bool:
        with self._lock:
            if self._active_lease_owner == client_id:
                self._active_lease_owner = None
                self._lease_expires_at = 0.0
                return True
            return False

    def is_lease_valid(self) -> bool:
        with self._lock:
            if not self._active_lease_owner:
                return False
            if time.time() >= self._lease_expires_at:
                self._active_lease_owner = None
                return False
            return True

    def push_frame(self, jpeg_data: bytes) -> None:
        with self._lock:
            if self.is_lease_valid():
                self._latest_jpeg = jpeg_data
                self._latest_frame_time = time.time()
                self._frame_seq += 1

    def get_latest_frame(self, client_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            if not self.is_lease_valid() or self._active_lease_owner != client_id:
                return None

            age_ms = int((time.time() - self._latest_frame_time) * 1000)
            return {
                "frame_seq": self._frame_seq,
                "jpeg_data": self._latest_jpeg,
                "frame_age_ms": age_ms,
                "is_stale": (age_ms > 2000),
            }
