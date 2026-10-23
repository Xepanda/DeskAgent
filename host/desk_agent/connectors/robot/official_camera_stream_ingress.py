"""Fail-closed ingress for CR-022 live camera frames on the managed SDK link.

The observer attaches to the already-running DaemonApplicationTransport's raw
ApplicationCommunicators callback. It never opens an ApplicationChannels or a
second Runtime connection. Snapshot capture remains in OfficialRobotCameraAdapter.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterable
from typing import Any

logger = logging.getLogger(__name__)

WSPK_HEADER_BYTES = 16
WJPG_HEADER_BYTES = 20
MAX_WSPK_PAYLOAD_BYTES = 128 * 1024
MAX_WSPK_FRAME_BYTES = WSPK_HEADER_BYTES + MAX_WSPK_PAYLOAD_BYTES
MAX_JPEG_BYTES = MAX_WSPK_PAYLOAD_BYTES - WJPG_HEADER_BYTES
LIVE_CAMERA_CAPABILITY = "camera.stream.mjpeg.wspk.v1"
UINT32_MASK = 0xFFFFFFFF
UINT32_HALF = 0x80000000


def install_raw_device_frame_observer(
    communicators: object,
    observer: Callable[[object, str | bytes], None],
) -> Callable[[], None]:
    """Observe raw frames on the existing SDK channel before its parser runs.

    The pinned SDK does not expose ApplicationChannels through ApplicationContext.
    Its managed transport owns one ApplicationCommunicators instance; wrapping its
    callback is the supported-by-version integration point and keeps that single
    paired connection intact.
    """
    prior = getattr(communicators, "_on_frame", None)
    if not callable(prior):
        raise TypeError("managed SDK communicators have no raw frame callback")

    def observed(channel: object, frame: str | bytes) -> Any:
        if _channel_value(channel) == "device":
            try:
                observer(channel, frame)
            except Exception:
                logger.exception("Managed SDK raw camera observer failed")
        return prior(channel, frame)

    setattr(communicators, "_on_frame", observed)

    def remove() -> None:
        if getattr(communicators, "_on_frame", None) is observed:
            setattr(communicators, "_on_frame", prior)

    return remove


def _channel_value(channel: object) -> str:
    value = getattr(channel, "value", channel)
    return str(value).lower()


def _signed_u32(value: int) -> int:
    value &= UINT32_MASK
    return value if value < UINT32_HALF else value - (UINT32_MASK + 1)


def _u32_forward_delta(new: int, old: int) -> int:
    return (new - old) & UINT32_MASK


def _estimate_host_minus_device_offset(
    host_send: int,
    device_receive: int,
    device_send: int,
    host_receive: int,
) -> tuple[float, float]:
    """Return host-minus-device offset and NTP-style uncertainty in ms."""
    device_processing = _u32_forward_delta(device_send, device_receive)
    round_trip = _u32_forward_delta(host_receive, host_send) - device_processing
    if round_trip < 0 or round_trip >= 60000:
        raise ValueError("clock ping RTT is invalid")
    device_minus_host = (
        _signed_u32(_u32_forward_delta(device_receive, host_send))
        + _signed_u32(_u32_forward_delta(device_send, host_receive))
    ) / 2.0
    return -device_minus_host, round_trip / 2.0


def _jpeg_dimensions_and_validate(data: bytes) -> tuple[int, int]:
    """Validate bounded JPEG marker structure and return its SOF dimensions.

    Runtime dependencies intentionally do not include an image codec. This checks
    SOI/EOI, marker lengths, frame header and scan placement; full pixel decoding is
    left to the browser/image consumer and remains a separate validation gate.
    """
    if len(data) < 16 or len(data) > MAX_JPEG_BYTES:
        raise ValueError("JPEG size outside the CR-022 WSPK limit")
    if not data.startswith(b"\xff\xd8") or not data.endswith(b"\xff\xd9"):
        raise ValueError("JPEG is missing complete SOI/EOI markers")

    offset = 2
    dimensions: tuple[int, int] | None = None
    saw_scan = False
    while offset < len(data):
        if data[offset] != 0xFF:
            raise ValueError("JPEG marker prefix is malformed")
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            raise ValueError("JPEG marker is truncated")
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            if offset != len(data) or dimensions is None or not saw_scan:
                raise ValueError("JPEG ended before a complete frame scan")
            return dimensions
        if marker == 0xD8 or marker == 0x00:
            raise ValueError("JPEG contains an invalid nested marker")
        if marker in (0x01, *range(0xD0, 0xD8)):
            continue
        if offset + 2 > len(data):
            raise ValueError("JPEG marker length is truncated")
        segment_len = int.from_bytes(data[offset:offset + 2], "big")
        if segment_len < 2 or offset + segment_len > len(data):
            raise ValueError("JPEG marker length is invalid")
        segment_start = offset + 2
        segment_end = offset + segment_len
        if marker in (0xC0, 0xC1, 0xC2, 0xC3):
            if segment_len < 8:
                raise ValueError("JPEG frame header is truncated")
            height = int.from_bytes(data[segment_start + 1:segment_start + 3], "big")
            width = int.from_bytes(data[segment_start + 3:segment_start + 5], "big")
            components = data[segment_start + 5]
            if not width or not height or not components:
                raise ValueError("JPEG frame dimensions are invalid")
            dimensions = (width, height)
        if marker == 0xDA:
            if dimensions is None:
                raise ValueError("JPEG scan precedes its frame header")
            saw_scan = True
            scan = segment_end
            while scan < len(data) - 1:
                if data[scan] != 0xFF:
                    scan += 1
                    continue
                next_byte = data[scan + 1]
                if next_byte == 0x00 or 0xD0 <= next_byte <= 0xD7:
                    scan += 2
                    continue
                if next_byte == 0xFF:
                    scan += 1
                    continue
                # EOI is the only marker accepted after the single scan. A second
                # scan would require a more complete entropy decoder than ingress.
                if next_byte == 0xD9 and scan + 2 == len(data):
                    return dimensions
                raise ValueError("JPEG scan has an unsupported or malformed marker")
            raise ValueError("JPEG entropy scan has no EOI marker")
        offset = segment_end
    raise ValueError("JPEG has no complete scan")


def _parse_live_frame(raw: bytes) -> dict[str, Any]:
    if not isinstance(raw, bytes) or len(raw) < WSPK_HEADER_BYTES + WJPG_HEADER_BYTES:
        raise ValueError("WSPK frame is truncated")
    if len(raw) > MAX_WSPK_FRAME_BYTES:
        raise ValueError("WSPK frame exceeds the 128 KiB payload ceiling")
    magic, frame_type, flags, stream_id, sequence, payload_len = struct.unpack(
        "<4sBBHII", raw[:WSPK_HEADER_BYTES]
    )
    if magic != b"WSPK" or frame_type != 3 or flags != 0:
        raise ValueError("WSPK frame type, flags, or magic is invalid")
    if payload_len > MAX_WSPK_PAYLOAD_BYTES or payload_len != len(raw) - WSPK_HEADER_BYTES:
        raise ValueError("WSPK payload length is invalid")
    payload = raw[WSPK_HEADER_BYTES:]
    wmagic, version, reserved, header_len, inner_sequence, timestamp, jpeg_len = struct.unpack(
        "<4sBBHIII", payload[:WJPG_HEADER_BYTES]
    )
    if wmagic != b"WJPG" or version != 1 or reserved != 0 or header_len != WJPG_HEADER_BYTES:
        raise ValueError("WJPG v1 header is invalid")
    jpeg = payload[WJPG_HEADER_BYTES:]
    if inner_sequence != sequence or jpeg_len != len(jpeg):
        raise ValueError("WJPG sequence or JPEG length does not match WSPK")
    width, height = _jpeg_dimensions_and_validate(jpeg)
    if (width, height) != (640, 480):
        raise ValueError("live stream JPEG is not the required 640x480 resolution")
    return {
        "stream_id": stream_id,
        "sequence": sequence,
        "capture_timestamp_ms": timestamp,
        "jpeg": jpeg,
        "width": width,
        "height": height,
    }


class OfficialCameraStreamIngress:
    """Lease/session-bound CR-022 stream receiver for one managed SDK session."""

    def __init__(
        self,
        *,
        send_command: Callable[[str, dict[str, Any], float | None], dict[str, Any]],
        ingest_frame: Callable[[bytes, dict[str, Any]], None],
        owner_is_valid: Callable[[str], bool],
        capabilities: Iterable[str],
        session_generation: str | None = None,
        clear_frame: Callable[[str], None] | None = None,
        clock_ping_interval: float = 10.0,
    ) -> None:
        self._send_command = send_command
        self._ingest_frame = ingest_frame
        self._owner_is_valid = owner_is_valid
        self._clear_frame = clear_frame
        self._capability_available = LIVE_CAMERA_CAPABILITY in set(capabilities)
        self.session_generation = session_generation or uuid.uuid4().hex
        self._clock_ping_interval = max(0.1, float(clock_ping_interval))
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._owner: str | None = None
        self._lease_generation: int | None = None
        self._stream_id: int | None = None
        self._active = True
        self._session_available = True
        self._closed = False
        self._last_sequence: int | None = None
        self._last_timestamp: int | None = None
        self._drop_count = 0
        self._latest_frame_metadata: dict[str, Any] | None = None
        self._clock_samples: deque[tuple[int, float, float]] = deque(maxlen=8)
        self._clock_offset_ms: float | None = None
        self._clock_uncertainty_ms: float | None = None
        self._last_ping_at = 0.0
        self._ping_stop = threading.Event()
        self._ping_thread: threading.Thread | None = None

    @property
    def drop_count(self) -> int:
        with self._lock:
            return self._drop_count

    @property
    def latest_frame_metadata(self) -> dict[str, Any] | None:
        with self._lock:
            return dict(self._latest_frame_metadata) if self._latest_frame_metadata else None

    @property
    def clock_offset_is_certain(self) -> bool:
        with self._lock:
            return self._clock_uncertainty_ms is not None and self._clock_uncertainty_ms <= 100.0

    def start(self, client_id: str, lease_ms: int, *, lease_generation: int | None = None) -> dict[str, Any]:
        with self._lifecycle_lock:
            with self._lock:
                current_generation = self._lease_generation
                replace_generation = self._owner == client_id and lease_generation is not None \
                    and lease_generation != current_generation
            if replace_generation:
                self.stop(expected_generation=current_generation)
            return self._start_locked(client_id, lease_ms, lease_generation=lease_generation)

    def _start_locked(
        self, client_id: str, lease_ms: int, *, lease_generation: int | None = None,
    ) -> dict[str, Any]:
        with self._lock:
            if self._closed or not self._session_available:
                return {"started": False, "reason": "SESSION_UNAVAILABLE"}
            if not self._capability_available:
                return {"started": False, "reason": "CAMERA_STREAM_UNAVAILABLE"}
            if not client_id or not isinstance(lease_ms, int) or isinstance(lease_ms, bool) or lease_ms <= 0:
                return {"started": False, "reason": "INVALID_LEASE"}
            active_owner = self._owner
            active_stream_id = self._stream_id
            if active_owner and active_owner != client_id:
                return {"started": False, "reason": "STREAM_ALREADY_ACTIVE"}
            if not self._owner_generation_is_valid(client_id, lease_generation):
                return {"started": False, "reason": "NO_ACTIVE_LEASE"}
        if active_owner == client_id:
            # Host lease renewals must renew the device lease too; they must not
            # accidentally issue a second stream.start for the active generation.
            try:
                ack = self._send_command(
                    "ctrl.camera.acquire",
                    {"client_id": client_id, "lease_duration_ms": lease_ms},
                    2.0,
                )
                if self._ack_succeeded(ack):
                    if lease_generation is not None:
                        with self._lock:
                            self._lease_generation = lease_generation
                    return {"started": True, "client_id": client_id, "stream_id": active_stream_id}
                return {"started": False, "reason": "CAMERA_LEASE_RENEWAL_REJECTED"}
            except Exception as error:
                logger.warning("Official SDK camera lease renewal failed: %s", error)
                return {"started": False, "reason": "UPSTREAM_UNAVAILABLE"}
        acquired = False
        try:
            lease_ack = self._send_command(
                "ctrl.camera.acquire",
                {"client_id": client_id, "lease_duration_ms": lease_ms},
                2.0,
            )
            if not self._ack_succeeded(lease_ack):
                return {"started": False, "reason": "CAMERA_LEASE_REJECTED"}
            acquired = True
            start_ack = self._send_command(
                "ctrl.camera.stream.start",
                {"client_id": client_id, "width": 640, "height": 480, "target_fps": 15},
                5.0,
            )
            data = start_ack.get("data", {}) if isinstance(start_ack, dict) else {}
            stream_id = data.get("stream_id") if isinstance(data, dict) else None
            device_ms = data.get("device_monotonic_ms") if isinstance(data, dict) else None
            if (not self._ack_succeeded(start_ack) or not isinstance(stream_id, int)
                    or isinstance(stream_id, bool) or not 1 <= stream_id <= 0xFFFF
                    or not isinstance(device_ms, int) or isinstance(device_ms, bool)
                    or not 0 <= device_ms <= UINT32_MASK):
                self._send_command("ctrl.camera.release", {"client_id": client_id}, 1.0)
                acquired = False
                return {"started": False, "reason": "CAMERA_STREAM_START_REJECTED"}
            with self._lock:
                if self._closed or not self._owner_generation_is_valid(client_id, lease_generation):
                    self._send_command("ctrl.camera.stream.stop", {"client_id": client_id, "stream_id": stream_id}, 1.0)
                    self._send_command("ctrl.camera.release", {"client_id": client_id}, 1.0)
                    acquired = False
                    return {"started": False, "reason": "LEASE_EXPIRED_DURING_START"}
                self._owner = client_id
                self._lease_generation = lease_generation
                self._stream_id = stream_id
                self._last_sequence = None
                self._last_timestamp = None
                self._latest_frame_metadata = None
                self._active = True
            self.ping_clock()
            self._ping_stop.clear()
            self._ping_thread = threading.Thread(
                target=self._clock_ping_loop,
                name="official-sdk-camera-clock",
                daemon=True,
            )
            self._ping_thread.start()
            return {"started": True, "client_id": client_id, "stream_id": stream_id}
        except Exception as error:
            if acquired:
                try:
                    self._send_command("ctrl.camera.release", {"client_id": client_id}, 1.0)
                except Exception:
                    pass
            logger.warning("Official SDK camera stream start failed: %s", error)
            return {"started": False, "reason": "UPSTREAM_UNAVAILABLE"}

    def acquire(
        self, client_id: str, lease_ms: int, target_fps: float = 15.0,
        *, lease_generation: int | None = None,
    ) -> dict[str, Any]:
        """Camera lease adapter shape used by the Host's existing lease handler."""
        if target_fps < 15.0:
            return {"granted": False, "reason": "LIVE_STREAM_REQUIRES_15_FPS"}
        result = self.start(client_id, lease_ms, lease_generation=lease_generation)
        if not result.get("started"):
            return {"granted": False, "reason": result.get("reason", "DEVICE_ERROR")}
        return {"granted": True, "client_id": client_id, "stream_id": result["stream_id"]}

    def release(self, client_id: str, *, expected_generation: int | None = None) -> bool:
        with self._lock:
            if (self._owner != client_id
                    or (expected_generation is not None and expected_generation != self._lease_generation)):
                return False
        return self.stop(expected_generation=expected_generation)

    def _owner_generation_is_valid(self, owner: str, generation: int | None) -> bool:
        try:
            return bool(self._owner_is_valid(owner, generation))
        except TypeError:
            return bool(self._owner_is_valid(owner))

    def on_application_frame(
        self,
        channel: object,
        frame: str | bytes,
        *,
        session_generation: str | None = None,
        host_receive_monotonic_ms: int | None = None,
    ) -> bool:
        if _channel_value(channel) != "device" or not isinstance(frame, bytes):
            return False
        with self._lock:
            owner = self._owner
            stream_id = self._stream_id
            if (not self._active or not owner or stream_id is None
                    or (session_generation is not None and session_generation != self.session_generation)
                    or not self._owner_generation_is_valid(owner, self._lease_generation)):
                self._drop_count += 1
                return False
        try:
            parsed = _parse_live_frame(frame)
            if parsed["stream_id"] != stream_id:
                raise ValueError("frame belongs to an old or foreign stream")
            with self._lock:
                if self._last_sequence is not None:
                    seq_delta = _u32_forward_delta(parsed["sequence"], self._last_sequence)
                    ts_delta = _u32_forward_delta(parsed["capture_timestamp_ms"], self._last_timestamp or 0)
                    if not 0 < seq_delta < UINT32_HALF:
                        raise ValueError("WSPK sequence repeated, rolled back, or jumped backwards")
                    if not 0 < ts_delta < UINT32_HALF:
                        raise ValueError("WJPG timestamp repeated or moved backwards")
                host_received = (int(time.monotonic() * 1000) & UINT32_MASK
                                 if host_receive_monotonic_ms is None
                                 else host_receive_monotonic_ms & UINT32_MASK)
                latency = self._latency_ms(parsed["capture_timestamp_ms"], host_received)
                metadata = {
                    key: parsed[key]
                    for key in ("stream_id", "sequence", "capture_timestamp_ms", "width", "height")
                }
                metadata.update({
                    "session_generation": self.session_generation,
                    "host_receive_monotonic_ms": host_received,
                    "clock_offset_ms": self._clock_offset_ms if self.clock_offset_is_certain else None,
                    "clock_uncertainty_ms": self._clock_uncertainty_ms,
                    "latency_ms": latency,
                })
                if (not self._active or owner != self._owner or stream_id != self._stream_id
                        or not self._owner_generation_is_valid(owner, self._lease_generation)):
                    self._drop_count += 1
                    return False
                self._last_sequence = parsed["sequence"]
                self._last_timestamp = parsed["capture_timestamp_ms"]
                self._latest_frame_metadata = metadata
                self._ingest_frame(parsed["jpeg"], dict(metadata))
                return True
        except (ValueError, struct.error) as error:
            with self._lock:
                self._drop_count += 1
            logger.debug("Dropped invalid CR-022 camera frame: %s", error)
            return False

    def ping_clock(self) -> bool:
        now_ms = int(time.monotonic() * 1000) & UINT32_MASK
        ping_id = uuid.uuid4().hex
        try:
            ack = self._send_command(
                "ctrl.camera.clock.ping",
                {"ping_id": ping_id, "host_send_monotonic_ms": now_ms},
                2.0,
            )
            host_recv = int(time.monotonic() * 1000) & UINT32_MASK
            data = ack.get("data", {}) if isinstance(ack, dict) else {}
            if (not self._ack_succeeded(ack) or data.get("ping_id") != ping_id):
                raise ValueError("clock ping ACK correlation failed")
            device_recv = data.get("device_receive_monotonic_ms")
            device_send = data.get("device_send_monotonic_ms")
            if any(not isinstance(v, int) or isinstance(v, bool) or not 0 <= v <= UINT32_MASK
                   for v in (device_recv, device_send)):
                raise ValueError("clock ping ACK has invalid device timestamps")
            host_minus_device, uncertainty = _estimate_host_minus_device_offset(
                now_ms, device_recv, device_send, host_recv
            )
            rtt = uncertainty * 2
            with self._lock:
                self._clock_samples.append((rtt, host_minus_device, uncertainty))
                best = min(self._clock_samples, key=lambda item: item[0])
                self._clock_offset_ms = best[1]
                self._clock_uncertainty_ms = best[2]
                self._last_ping_at = time.monotonic()
            return True
        except Exception as error:
            with self._lock:
                self._clock_offset_ms = None
                self._clock_uncertainty_ms = None
            logger.debug("Camera clock offset unavailable: %s", error)
            return False

    def _latency_ms(self, device_capture_ms: int, host_receive_ms: int) -> float | None:
        if not self.clock_offset_is_certain or self._clock_offset_ms is None:
            return None
        estimated_host_capture = int(round(device_capture_ms + self._clock_offset_ms)) & UINT32_MASK
        age = _signed_u32(host_receive_ms - int(estimated_host_capture))
        return float(age) if 0 <= age < UINT32_HALF else None

    def _clock_ping_loop(self) -> None:
        while not self._ping_stop.wait(self._clock_ping_interval):
            with self._lock:
                if not self._active or self._owner is None:
                    return
            self.ping_clock()

    def stop(self, *, expected_generation: int | None = None) -> bool:
        with self._lifecycle_lock:
            return self._stop_locked(expected_generation=expected_generation)

    def _stop_locked(self, *, expected_generation: int | None = None) -> bool:
        with self._lock:
            owner = self._owner
            generation = self._lease_generation
            stream_id = self._stream_id
            if (owner is None
                    or (expected_generation is not None and expected_generation != generation)):
                return False
            self._active = False
            self._owner = None
            self._lease_generation = None
            self._stream_id = None
            self._last_sequence = None
            self._last_timestamp = None
            self._latest_frame_metadata = None
            self._clock_offset_ms = None
            self._clock_uncertainty_ms = None
            self._ping_stop.set()
            ping_thread = self._ping_thread
            self._ping_thread = None
        if ping_thread and ping_thread is not threading.current_thread():
            ping_thread.join(timeout=2.25)
        stop_ok = False
        try:
            ack = self._send_command(
                "ctrl.camera.stream.stop", {"client_id": owner, "stream_id": stream_id}, 3.0
            )
            stop_ok = self._ack_succeeded(ack)
        except Exception as error:
            logger.warning("Camera stream stop unconfirmed; device lease TTL remains authoritative: %s", error)
        try:
            release_ack = self._send_command("ctrl.camera.release", {"client_id": owner}, 2.0)
            stop_ok = stop_ok and self._ack_succeeded(release_ack)
        except Exception as error:
            stop_ok = False
            logger.warning("Camera lease release unconfirmed: %s", error)
        if self._clear_frame:
            self._clear_frame_for_generation(owner, generation)
        return stop_ok

    def on_session_disconnect(self) -> None:
        with self._lock:
            owner = self._owner
            generation = self._lease_generation
            self._active = False
            self._session_available = False
            self._owner = None
            self._lease_generation = None
            self._stream_id = None
            self._latest_frame_metadata = None
            self._clock_offset_ms = None
            self._clock_uncertainty_ms = None
            self._ping_stop.set()
        if owner and self._clear_frame:
            self._clear_frame_for_generation(owner, generation)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
        self.stop()
        self.on_session_disconnect()

    @staticmethod
    def _ack_succeeded(ack: Any) -> bool:
        return isinstance(ack, dict) and ack.get("type") == "sys.ack" and ack.get("code") == 0

    def _clear_frame_for_generation(self, owner: str, generation: int | None) -> None:
        if not self._clear_frame:
            return
        try:
            self._clear_frame(owner, expected_generation=generation)
        except TypeError:
            self._clear_frame(owner)

    def set_clear_frame_callback(self, callback: Callable[[str], None] | None) -> None:
        self._clear_frame = callback
