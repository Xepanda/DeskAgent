"""Narrow managed-SDK transport adapter for CR-017 reminder notifications.

This adapter deliberately reports command acknowledgement as transport-level
acceptance only. Device stages and user acknowledgement are separate events.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from host.desk_agent.core.managed_sdk_listener import ManagedSdkMessageListener


_PINNED_COMMIT = "8bc13967a06ef073459be47e930ff43ef37ee8fb"
_PINNED_TAG = "v0.1.11"
_STATUSES = frozenset({"accepted", "rendered", "played", "failed", "expired", "unknown"})
_DEVICE_EVENTS = frozenset({"notification.status", "notification.acknowledged"})

# Mirrors the notification error policy in contracts/error_codes.json and the
# SDK sys.nack schema. Keep this local so the runtime adapter does not depend
# on the repository's contract files being present in an installed Host.
_NACK_POLICY: dict[str, tuple[frozenset[int], bool, str]] = {
    "AUTH_REQUIRED": (frozenset({401}), False, "Notification delivery requires an authenticated device session."),
    "STORAGE_FULL": (frozenset({507}), False, "Notification storage is full."),
    "EXPIRED": (frozenset({410}), False, "Notification has expired."),
    "INVALID_ARGUMENT": (frozenset({400}), False, "Invalid notification request."),
    "STORAGE_UNAVAILABLE": (
        frozenset({503}), True,
        "Notification storage provider is unavailable. Retry the same notification_id when the provider becomes available.",
    ),
    "STORAGE_ERROR": (
        frozenset({500}), True,
        "Notification storage operation failed. Keep delivery unknown and retry the same notification_id.",
    ),
    "NOTIFICATION_DELIVERY_FAILED": (
        frozenset({500, 503}), True,
        "Notification delivery failed. Fail closed and retry only with the same notification_id.",
    ),
}


class ManagedNotificationNackError(RuntimeError):
    """A recognized notification ``sys.nack`` with its wire context intact."""

    def __init__(self, *, code: int, error: str, command_id: str, operation: str) -> None:
        allowed_codes, retryable, message = _NACK_POLICY[error]
        if code not in allowed_codes:
            raise ValueError("notification NACK code does not match its registered error")
        super().__init__(message)
        self.code = code
        self.error = error
        self.retryable = retryable
        self.command_id = command_id
        self.operation = operation


class ManagedNotificationUnknownError(RuntimeError):
    """An unrecognized or malformed response; retryable without invented codes."""

    retryable = True


def _check_sdk_pin(sdk_version: str | None, lock_path: Path | None = None) -> None:
    path = lock_path or Path(__file__).resolve().parents[3] / "contracts" / "dependency_lock.json"
    lock = json.loads(path.read_text(encoding="utf-8"))
    sdk_lock = lock.get("repositories", {}).get("watcherobot_python_sdk", {})
    if sdk_lock.get("commit") != _PINNED_COMMIT or sdk_lock.get("tag") != _PINNED_TAG:
        raise RuntimeError("managed notification adapter requires the audited SDK lock pin")
    if sdk_version != _PINNED_TAG.removeprefix("v"):
        raise RuntimeError("managed notification adapter requires Watcherobot SDK 0.1.11")


class ManagedNotificationAdapter:
    """Send CR-017 notifications and observe correlated device event messages.

    ``on_event`` must persist any event it accepts. This class only performs
    process-local event de-duplication; it is not a durable receipt store.
    """

    def __init__(
        self,
        context: object,
        on_event: Callable[[dict[str, Any]], None],
        *,
        sdk_version: str | None = None,
        lock_path: Path | None = None,
        now_utc: Callable[[], datetime] | None = None,
        known_notifications: Mapping[str, str] | None = None,
        known_summaries: Mapping[str, str] | None = None,
    ) -> None:
        if sdk_version is None:
            try:
                import watcherobot
            except ImportError as exc:
                raise RuntimeError("pinned Watcherobot SDK is unavailable") from exc
            sdk_version = getattr(watcherobot, "__version__", None)
        _check_sdk_pin(sdk_version, lock_path)

        transport = getattr(context, "_transport", None)
        send_command = getattr(transport, "send_command", None)
        if not callable(send_command):
            raise TypeError("managed SDK transport must provide send_command")
        self._send_command = send_command
        self._listener = ManagedSdkMessageListener(context)
        self._on_event = on_event
        self._closed = False
        self._lock = threading.Lock()
        self._seen: set[str] = set()
        self._notifications: dict[str, dict[str, Any]] = {}
        self._summaries: dict[str, dict[str, Any]] = {}
        self._expires_at: dict[str, datetime] = {}
        self._now_utc = now_utc or (lambda: datetime.now(timezone.utc))
        for notification_id, expires_at in (known_notifications or {}).items():
            if not isinstance(notification_id, str) or not notification_id:
                continue
            self._expires_at[notification_id] = self._parse_utc(expires_at)
        for summary_id, expires_at in (known_summaries or {}).items():
            if not isinstance(summary_id, str) or not summary_id:
                continue
            self._expires_at[summary_id] = self._parse_utc(expires_at)
        self._listener.add(self._receive)

    def deliver(self, notification: Mapping[str, Any], *, timeout: float = 3.0) -> dict[str, Any]:
        """Send one validated notification; return only transport ACK evidence."""
        if self._closed:
            raise RuntimeError("managed notification adapter is closed")
        payload = self._validate_notification(notification)
        expiry = self._parse_utc(payload["expires_at"])
        scheduled = self._parse_utc(payload["scheduled_at"])
        if expiry <= scheduled:
            raise ValueError("expires_at must be later than scheduled_at")
        notification_id = payload["notification_id"]
        with self._lock:
            previous = self._notifications.get(notification_id)
            if previous is not None and previous != payload:
                raise ValueError("a notification_id cannot be reused with different payload")
            known_expiry = self._expires_at.get(notification_id)
            if known_expiry is not None and known_expiry != expiry:
                raise ValueError("notification expiry conflicts with restored outbox context")
            self._notifications[notification_id] = payload
            self._expires_at[notification_id] = expiry
        if self._now_utc().astimezone(timezone.utc) > expiry:
            return {
                "notification_id": notification_id,
                "transport_acknowledged": False,
                "unverified_device_acceptance_observed": False,
                "durable_acceptance_confirmed": False,
                "delivery_state": "expired",
                "rendered": False,
                "played": False,
                "user_acknowledged": False,
            }
        ack = self._send_command("notification.deliver", payload, timeout)
        if isinstance(ack, dict) and ack.get("type") == "sys.nack":
            self._raise_for_nack(ack, "notification.deliver")
        if not isinstance(ack, dict) or ack.get("type") != "sys.ack" or ack.get("code") != 0:
            raise ManagedNotificationUnknownError("managed notification response was malformed or unrecognized")
        data = ack.get("data")
        accepted = isinstance(data, dict) and data.get("status") == "accepted" and data.get("accepted") is True
        return {
            "notification_id": notification_id,
            "transport_acknowledged": True,
            "unverified_device_acceptance_observed": accepted,
            "durable_acceptance_confirmed": False,
            "delivery_state": "unknown",
            "rendered": False,
            "played": False,
            "user_acknowledged": False,
        }

    def deliver_summary(self, summary: Mapping[str, Any], *, timeout: float = 3.0) -> dict[str, Any]:
        """Send a CR-028 summary on its own SDK command and retain unknown semantics."""
        if self._closed:
            raise RuntimeError("managed notification adapter is closed")
        payload = self._validate_summary(summary)
        summary_id = payload["summary_id"]
        expiry = self._parse_utc(payload["expires_at"])
        with self._lock:
            previous = self._summaries.get(summary_id)
            if previous is not None and previous != payload:
                raise ValueError("a summary_id cannot be reused with different payload")
            known_expiry = self._expires_at.get(summary_id)
            if known_expiry is not None and known_expiry != expiry:
                raise ValueError("summary expiry conflicts with restored outbox context")
            self._summaries[summary_id] = payload
            self._expires_at[summary_id] = expiry
        if self._now_utc().astimezone(timezone.utc) >= expiry:
            return {
                "summary_id": summary_id,
                "transport_acknowledged": False,
                "unverified_device_acceptance_observed": False,
                "durable_acceptance_confirmed": False,
                "delivery_state": "expired",
            }
        ack = self._send_command("notification.summary.deliver", payload, timeout)
        if isinstance(ack, dict) and ack.get("type") == "sys.nack":
            self._raise_for_nack(ack, "notification.summary.deliver")
        if not isinstance(ack, dict) or ack.get("type") != "sys.ack" or ack.get("code") != 0:
            raise ManagedNotificationUnknownError("managed summary response was malformed or unrecognized")
        data = ack.get("data")
        accepted = isinstance(data, dict) and data.get("status") == "accepted" and data.get("accepted") is True
        return {
            "summary_id": summary_id,
            "transport_acknowledged": True,
            "unverified_device_acceptance_observed": accepted,
            "durable_acceptance_confirmed": False,
            "delivery_state": "unknown",
        }

    @staticmethod
    def _raise_for_nack(nack: dict[str, Any], expected_operation: str = "notification.deliver") -> None:
        data = nack.get("data")
        if not isinstance(data, dict):
            raise ManagedNotificationUnknownError("managed notification NACK was malformed")
        code = nack.get("code")
        error = data.get("error")
        operation = data.get("type")
        command_id = data.get("command_id")
        if (
            isinstance(code, bool)
            or not isinstance(code, int)
            or operation != expected_operation
            or not isinstance(command_id, str)
            or not command_id
            or not isinstance(error, str)
            or error not in _NACK_POLICY
        ):
            raise ManagedNotificationUnknownError("managed notification NACK was unrecognized")
        allowed_codes, _retryable, _message = _NACK_POLICY[error]
        if code not in allowed_codes:
            raise ManagedNotificationUnknownError("managed notification NACK code and error did not match")
        raise ManagedNotificationNackError(
            code=code,
            error=error,
            command_id=command_id,
            operation=operation,
        )

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._listener.remove(self._receive)

    def _receive(self, message: dict[str, Any]) -> None:
        if self._closed or not isinstance(message, dict) or message.get("type") not in _DEVICE_EVENTS:
            return
        data = message.get("data")
        if not isinstance(data, dict):
            return
        is_summary = isinstance(data.get("summary_id"), str) and bool(data.get("summary_id"))
        identity_key = "summary_id" if is_summary else "notification_id"
        identity = data.get(identity_key)
        if not isinstance(identity, str) or not identity:
            return
        with self._lock:
            expiry = self._expires_at.get(identity)
        if expiry is None or self._now_utc().astimezone(timezone.utc) > expiry:
            return
        event_type = message["type"]
        event: dict[str, Any] = {"type": event_type, identity_key: identity}
        if event_type == "notification.status":
            status = data.get("status")
            if status not in _STATUSES:
                return
            if status == "accepted":
                # CR-018 has no approved durable store. Preserve the device report
                # for diagnostics without elevating it to Host delivery state.
                event["status"] = "unknown"
                event["unverified_device_status"] = "accepted"
            else:
                event["status"] = status
        else:
            source = data.get("source")
            if not isinstance(source, str) or not source:
                return
            event["source"] = source
        fingerprint = json.dumps(event, sort_keys=True, separators=(",", ":"))
        with self._lock:
            if fingerprint in self._seen:
                return
        self._on_event(event)
        with self._lock:
            self._seen.add(fingerprint)

    @staticmethod
    def _validate_notification(notification: Mapping[str, Any]) -> dict[str, Any]:
        required = ("notification_id", "reminder_id", "occurrence_id", "title", "scheduled_at", "expires_at")
        if any(not isinstance(notification.get(key), str) or not notification[key] for key in required):
            raise ValueError("notification is missing a required CR-017 string field")
        if notification["notification_id"] != notification["occurrence_id"]:
            raise ValueError("notification_id must equal occurrence_id")
        if not notification["title"].strip():
            raise ValueError("notification title must not be blank")
        payload = {key: notification[key] for key in required}
        if "is_catchup" in notification:
            if not isinstance(notification["is_catchup"], bool):
                raise ValueError("is_catchup must be boolean")
            payload["is_catchup"] = notification["is_catchup"]
        return payload

    @classmethod
    def _validate_summary(cls, summary: Mapping[str, Any]) -> dict[str, Any]:
        required = ("summary_id", "batch_id", "created_at", "expires_at", "items", "omitted_count")
        if any(key not in summary for key in required) or set(summary) != set(required):
            raise ValueError("summary does not match the CR-028 payload fields")
        for key in ("summary_id", "batch_id"):
            value = summary[key]
            if not isinstance(value, str) or not 1 <= len(value) <= 128 or not all(
                char.isascii() and (char.isalnum() or char in "._:-") for char in value
            ):
                raise ValueError(f"{key} is not a valid CR-028 identifier")
        created = cls._parse_utc(summary["created_at"])
        expiry = cls._parse_utc(summary["expires_at"])
        if expiry - created != timedelta(hours=24):
            raise ValueError("summary expiry must be exactly 24 hours after creation")
        items = summary["items"]
        if not isinstance(items, list) or not 1 <= len(items) <= 20:
            raise ValueError("summary items must contain between 1 and 20 entries")
        if isinstance(summary["omitted_count"], bool) or not isinstance(summary["omitted_count"], int) or summary["omitted_count"] < 0:
            raise ValueError("omitted_count must be a non-negative integer")
        normalized_items = []
        previous_time: datetime | None = None
        item_keys = {"reminder_id", "occurrence_id", "title", "scheduled_at", "timezone"}
        for item in items:
            if not isinstance(item, Mapping) or set(item) != item_keys:
                raise ValueError("summary item does not match the CR-028 item fields")
            if any(not isinstance(item[key], str) or not item[key] for key in item_keys):
                raise ValueError("summary item contains an empty or non-string field")
            scheduled = cls._parse_utc(item["scheduled_at"])
            if previous_time is not None and scheduled < previous_time:
                raise ValueError("summary items must be ordered by scheduled_at")
            previous_time = scheduled
            normalized_items.append({key: item[key] for key in ("reminder_id", "occurrence_id", "title", "scheduled_at", "timezone")})
        return {
            "summary_id": summary["summary_id"],
            "batch_id": summary["batch_id"],
            "created_at": summary["created_at"],
            "expires_at": summary["expires_at"],
            "items": normalized_items,
            "omitted_count": summary["omitted_count"],
        }

    @staticmethod
    def _parse_utc(value: str) -> datetime:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("notification timestamps must use RFC3339 format") from exc
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("notification timestamps must include a timezone")
        return parsed.astimezone(timezone.utc)
