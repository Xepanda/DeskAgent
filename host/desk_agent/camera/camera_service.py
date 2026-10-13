"""
Desk Agent R1 LAN Camera Viewer Service.

Provides secure local network JPEG frame viewing with:
- Mutual exclusion single-viewer lease arbitration (RESOURCE_BUSY for concurrent viewers).
- Automatic lease expiration and renewal support.
- Hardened freshness tracking with stale frame blanking/masking (prevents old frames masquerading as live video).
- Strict bounded latest-frame buffer (no disk writes, no video recordings, no audio streams).
- Teardown callbacks to stop hardware sensor DMA upon lease release or timeout.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Standard black frame JPEG placeholder (JFIF 1x1 black pixel) used strictly when stream goes stale
STALE_MASK_JPEG = (
    b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xdb\x00C\x00\x08\x06\x06"
    b"\x07\x06\x05\x08\x07\x07\x07\t\t\x08\n\x0c\x14\r\x0c\x0b\x0b\x0c\x19\x12\x13\x0f\x14\x1d\x1a"
    b"\x1f\x1e\x1d\x1a\x1c\x1c $.' \",#\x1c\x1c(7),01444\x1f'9=82<.342\xff\xc0\x00\x0b\x08\x00\x01"
    b"\x00\x01\x01\x01\x11\x00\xff\xc4\x00\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00"
    b"\x00\x00\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00\x08\x01\x01\x00\x00"
    b"?\x00\xbf\x80\xff\xd9"
)

LeaseReleaseCallback = Callable[[str, str], None]


class CameraViewerService:
    def __init__(
        self,
        default_lease_ms: int = 10000,
        stale_threshold_ms: int = 2000,
        on_lease_released: Optional[LeaseReleaseCallback] = None,
    ) -> None:
        self.default_lease_ms = default_lease_ms
        self.stale_threshold_ms = stale_threshold_ms
        self.on_lease_released = on_lease_released

        self._lock = threading.RLock()
        self._active_lease_owner: Optional[str] = None
        self._lease_expires_at: float = 0.0

        # Bounded single latest frame buffer (initially None, no fake frames)
        self._latest_jpeg: Optional[bytes] = None
        self._latest_frame_time: float = 0.0
        self._frame_seq: int = 0

    def acquire_lease(self, client_id: str, lease_ms: Optional[int] = None) -> dict[str, Any]:
        """
        Acquires or renews single-viewer lease for camera streaming.
        Rejects concurrent clients with RESOURCE_BUSY.
        """
        duration = (lease_ms or self.default_lease_ms) / 1000.0
        now = time.time()

        with self._lock:
            # Check if active lease held by another client
            if self._active_lease_owner and self._active_lease_owner != client_id:
                if now < self._lease_expires_at:
                    return {
                        "granted": False,
                        "reason": "RESOURCE_BUSY",
                        "current_owner": self._active_lease_owner,
                        "remaining_ms": int((self._lease_expires_at - now) * 1000),
                    }
                else:
                    # Previous lease expired, trigger teardown callback for previous owner
                    prev_owner = self._active_lease_owner
                    self._active_lease_owner = None
                    self._trigger_teardown(prev_owner, "expired")

            # Grant lease or renew
            self._active_lease_owner = client_id
            self._lease_expires_at = now + duration

            return {
                "granted": True,
                "client_id": client_id,
                "lease_duration_ms": int(duration * 1000),
                "expires_at": self._lease_expires_at,
            }

    def release_lease(self, client_id: str) -> bool:
        """Explicitly releases camera viewing lease."""
        with self._lock:
            if self._active_lease_owner == client_id:
                owner = self._active_lease_owner
                self._active_lease_owner = None
                self._lease_expires_at = 0.0
                self._latest_jpeg = None
                self._trigger_teardown(owner, "explicit_release")
                return True
            return False

    def is_lease_valid(self) -> bool:
        """Checks if current camera lease is valid and active."""
        with self._lock:
            if not self._active_lease_owner:
                return False
            now = time.time()
            if now >= self._lease_expires_at:
                owner = self._active_lease_owner
                self._active_lease_owner = None
                self._lease_expires_at = 0.0
                self._latest_jpeg = None
                self._trigger_teardown(owner, "expired")
                return False
            return True

    def _trigger_teardown(self, owner: str, reason: str) -> None:
        if self.on_lease_released:
            try:
                self.on_lease_released(owner, reason)
            except Exception as exc:
                logger.error("Error executing camera teardown callback: %s", exc)

    def push_frame(self, jpeg_data: bytes) -> None:
        """
        Receives captured JPEG frame from device hardware.
        Only stores the latest frame if an active lease exists.
        """
        with self._lock:
            if self.is_lease_valid():
                self._latest_jpeg = jpeg_data
                self._latest_frame_time = time.time()
                self._frame_seq += 1

    def get_latest_frame(self, client_id: str) -> Optional[dict[str, Any]]:
        """
        Retrieves latest frame for authenticated lease owner.
        Applies stale frame masking/blanking if frame age exceeds threshold.
        """
        with self._lock:
            if not self.is_lease_valid() or self._active_lease_owner != client_id:
                return None

            if self._latest_jpeg is None:
                return None

            now = time.time()
            age_ms = int((now - self._latest_frame_time) * 1000)
            is_stale = age_ms > self.stale_threshold_ms

            if is_stale:
                return {
                    "frame_seq": self._frame_seq,
                    "jpeg_data": STALE_MASK_JPEG,
                    "frame_age_ms": age_ms,
                    "is_stale": True,
                    "is_masked": True,
                }

            return {
                "frame_seq": self._frame_seq,
                "jpeg_data": self._latest_jpeg,
                "frame_age_ms": age_ms,
                "is_stale": False,
                "is_masked": False,
            }
