"""Bounded, in-memory consumer for the frozen CR-009 structured log events.

The consumer intentionally has no persistence or raw-frame retention. Callers
must pass the per-session authorization decision when delivering each message.
"""

from __future__ import annotations

import re
import threading
from collections import deque
from datetime import datetime
from typing import Any, Mapping, Optional


_TOKEN = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_LEVELS = frozenset({"ERROR", "WARN", "INFO", "DEBUG"})
_EVENT_ID = "CAMERA_CAPTURE_EMPTY"
_GAP_REASON = "QUEUE_FULL"
_SCHEMA_VERSION = "1.0.0"
_QUEUE_CAPACITY = 16


class NetworkLogConsumer:
    """Validate and queue low-sensitivity CR-009 events in bounded memory."""

    def __init__(
        self,
        *,
        enabled: bool = False,
    ) -> None:
        self.enabled = bool(enabled)
        self._queue: deque[dict[str, Any]] = deque()
        self._lock = threading.Lock()
        self._closed = False
        self._accepted = 0
        self._rejected = 0
        self._unauthorized = 0
        self._dropped = 0

    def ingest(self, message: object, *, authorized: bool) -> bool:
        """Accept one message only when explicitly enabled and session-authorized."""

        with self._lock:
            if self._closed or not self.enabled:
                self._rejected += 1
                return False
            if authorized is not True:
                self._unauthorized += 1
                return False
            safe_message = self._validate(message)
            if safe_message is None:
                self._rejected += 1
                return False
            if len(self._queue) >= _QUEUE_CAPACITY:
                # CR-009 uses drop-new to preserve the earliest queued evidence.
                self._dropped += 1
                return False
            self._queue.append(safe_message)
            self._accepted += 1
            return True

    def drain(self, limit: Optional[int] = None) -> list[dict[str, Any]]:
        """Remove and return up to ``limit`` validated events."""

        with self._lock:
            count = len(self._queue) if limit is None else max(0, min(int(limit), len(self._queue)))
            return [self._queue.popleft() for _ in range(count)]

    def reset_session(self) -> None:
        """Discard queued events when the authorized Device session changes."""

        with self._lock:
            self._queue.clear()

    def close(self) -> None:
        """Stop accepting events and discard buffered content."""

        with self._lock:
            self._closed = True
            self._queue.clear()

    def snapshot(self) -> dict[str, int | bool]:
        """Return counters and queue depth without exposing event contents."""

        with self._lock:
            return {
                "enabled": self.enabled,
                "closed": self._closed,
                "queued": len(self._queue),
                "capacity": _QUEUE_CAPACITY,
                "accepted": self._accepted,
                "rejected": self._rejected,
                "unauthorized": self._unauthorized,
                "dropped": self._dropped,
            }

    def _validate(self, message: object) -> Optional[dict[str, Any]]:
        if not isinstance(message, Mapping) or set(message) != {"type", "data"}:
            return None
        message_type = message.get("type")
        data = message.get("data")
        if not isinstance(data, Mapping):
            return None
        if message_type == "sys.log.event":
            return NetworkLogConsumer._validate_event(data)
        if message_type == "sys.log.gap":
            return self._validate_gap(data)
        return None

    @staticmethod
    def _validate_event(data: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        required = {
            "log_schema_version", "stream_id", "boot_id", "sequence", "uptime_ms",
            "level", "component", "event_id", "fields",
        }
        if not required.issubset(data) or set(data) - required - {"timestamp_utc"}:
            return None
        if data.get("log_schema_version") != _SCHEMA_VERSION:
            return None
        if not _valid_token(data.get("stream_id")) or not _valid_token(data.get("boot_id")):
            return None
        if not _is_nonnegative_int(data.get("sequence"), allow_zero=False):
            return None
        if not _is_nonnegative_int(data.get("uptime_ms")):
            return None
        if data.get("level") not in _LEVELS or data.get("component") != "camera":
            return None
        if data.get("event_id") != _EVENT_ID or data.get("fields") != {}:
            return None

        event = {
            "type": "sys.log.event",
            "data": {
                "log_schema_version": _SCHEMA_VERSION,
                "stream_id": data["stream_id"],
                "boot_id": data["boot_id"],
                "sequence": data["sequence"],
                "uptime_ms": data["uptime_ms"],
                "level": data["level"],
                "component": "camera",
                "event_id": _EVENT_ID,
                "fields": {},
            },
        }
        if "timestamp_utc" in data:
            timestamp = data["timestamp_utc"]
            if not isinstance(timestamp, str) or len(timestamp) > 40:
                return None
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError:
                return None
            if parsed.tzinfo is None:
                return None
            event["data"]["timestamp_utc"] = timestamp
        return event

    def _validate_gap(self, data: Mapping[str, Any]) -> Optional[dict[str, Any]]:
        required = {
            "log_schema_version", "stream_id", "boot_id", "first_dropped_sequence",
            "last_dropped_sequence", "dropped_count", "reason",
        }
        if set(data) != required or data.get("log_schema_version") != _SCHEMA_VERSION:
            return None
        if not _valid_token(data.get("stream_id")) or not _valid_token(data.get("boot_id")):
            return None
        first = data.get("first_dropped_sequence")
        last = data.get("last_dropped_sequence")
        count = data.get("dropped_count")
        reason = data.get("reason")
        if not all(_is_nonnegative_int(value, allow_zero=False) for value in (first, last, count)):
            return None
        if last < first or count != last - first + 1:
            return None
        if reason != _GAP_REASON:
            return None
        return {
            "type": "sys.log.gap",
            "data": {
                "log_schema_version": _SCHEMA_VERSION,
                "stream_id": data["stream_id"],
                "boot_id": data["boot_id"],
                "first_dropped_sequence": first,
                "last_dropped_sequence": last,
                "dropped_count": count,
                "reason": _GAP_REASON,
            },
        }


def _valid_token(value: object) -> bool:
    return isinstance(value, str) and _TOKEN.fullmatch(value) is not None


def _is_nonnegative_int(value: object, *, allow_zero: bool = True) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and (value >= 0 if allow_zero else value > 0)
