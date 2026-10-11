"""
Desk Agent R1 Central Operation and Event Router.
Routes incoming tool requests and manages idempotency and deduplication.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping, Optional
from host.desk_agent.core.models import RequestEnvelope, ResponseEnvelope, ResponseStatus

logger = logging.getLogger(__name__)

HandlerType = Callable[[RequestEnvelope], ResponseEnvelope]


class Router:
    def __init__(self) -> None:
        self._handlers: dict[str, HandlerType] = {}
        # Idempotency deduplication cache: request_id -> ResponseEnvelope
        self._idempotency_cache: dict[str, ResponseEnvelope] = {}

    def register(self, operation: str, handler: HandlerType) -> None:
        self._handlers[operation] = handler

    def dispatch(self, envelope: RequestEnvelope) -> ResponseEnvelope:
        start_time = time.monotonic()

        # 1. Check expiration
        if envelope.is_expired():
            return ResponseEnvelope.create_error(
                request_id=envelope.request_id,
                code="EXPIRED",
                message=f"Request {envelope.request_id} expired at {envelope.expires_at}",
                retryable=False,
            )

        # 2. Check idempotency deduplication cache
        if envelope.request_id in self._idempotency_cache:
            logger.info("Serving deduplicated response for request %s", envelope.request_id)
            return self._idempotency_cache[envelope.request_id]

        # 3. Find registered handler
        handler = self._handlers.get(envelope.operation)
        if not handler:
            return ResponseEnvelope.create_error(
                request_id=envelope.request_id,
                code="UNKNOWN_OPERATION",
                message=f"Operation '{envelope.operation}' is not supported",
                retryable=False,
            )

        # 4. Execute handler
        try:
            response = handler(envelope)
        except Exception as exc:
            logger.exception("Error executing operation %s: %s", envelope.operation, exc)
            response = ResponseEnvelope.create_error(
                request_id=envelope.request_id,
                code="OPERATION_TIMEOUT" if "timeout" in str(exc).lower() else "INTERNAL_ERROR",
                message=str(exc),
                retryable=True,
            )

        # 5. Cache response for idempotency
        self._idempotency_cache[envelope.request_id] = response
        return response
