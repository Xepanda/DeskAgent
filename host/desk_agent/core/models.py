"""
Desk Agent R1 Protocol Envelopes and Data Models.
Standardized according to contracts/schemas/
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping, Optional


class ResponseStatus(str, Enum):
    ACCEPTED = "accepted"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class RequestEnvelope:
    schema_version: str
    request_id: str
    operation: str
    arguments: Mapping[str, Any]
    expires_at: str
    task_id: Optional[str] = None
    session_id: Optional[str] = None
    client_id: Optional[str] = None

    def is_expired(self) -> bool:
        dt = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        return datetime.now(timezone.utc) > dt

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RequestEnvelope:
        return cls(
            schema_version=data.get("schema_version", "1.0.0"),
            request_id=data["request_id"],
            operation=data["operation"],
            arguments=data.get("arguments", {}),
            expires_at=data["expires_at"],
            task_id=data.get("task_id"),
            session_id=data.get("session_id"),
            client_id=data.get("client_id"),
        )


@dataclass(frozen=True)
class ResponseEnvelope:
    schema_version: str
    request_id: str
    status: ResponseStatus
    timestamp: str
    result: Optional[Mapping[str, Any]] = None
    error: Optional[Mapping[str, Any]] = None
    execution_duration_ms: Optional[int] = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "request_id": self.request_id,
            "status": self.status.value if isinstance(self.status, ResponseStatus) else str(self.status),
            "timestamp": self.timestamp,
        }
        if self.result is not None:
            d["result"] = dict(self.result)
        if self.error is not None:
            d["error"] = dict(self.error)
        if self.execution_duration_ms is not None:
            d["execution_duration_ms"] = self.execution_duration_ms
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def create_success(
        cls,
        request_id: str,
        result: Mapping[str, Any],
        duration_ms: int = 0
    ) -> ResponseEnvelope:
        return cls(
            schema_version="1.0.0",
            request_id=request_id,
            status=ResponseStatus.SUCCEEDED,
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            result=result,
            execution_duration_ms=duration_ms,
        )

    @classmethod
    def create_error(
        cls,
        request_id: str,
        code: str,
        message: str,
        retryable: bool = False,
        details: Optional[Mapping[str, Any]] = None,
        duration_ms: int = 0
    ) -> ResponseEnvelope:
        err = {"code": code, "message": message, "retryable": retryable}
        if details:
            err["details"] = dict(details)
        return cls(
            schema_version="1.0.0",
            request_id=request_id,
            status=ResponseStatus.FAILED,
            timestamp=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            error=err,
            execution_duration_ms=duration_ms,
        )


@dataclass(frozen=True)
class EventEnvelope:
    schema_version: str
    event_id: str
    sequence: int
    sequence_scope: str
    type: str
    timestamp: str
    payload: Mapping[str, Any]
    request_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        d = {
            "schema_version": self.schema_version,
            "event_id": self.event_id,
            "sequence": self.sequence,
            "sequence_scope": self.sequence_scope,
            "type": self.type,
            "timestamp": self.timestamp,
            "payload": dict(self.payload),
        }
        if self.request_id is not None:
            d["request_id"] = self.request_id
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)
