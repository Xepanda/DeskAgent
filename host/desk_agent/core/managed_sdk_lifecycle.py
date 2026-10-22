"""Host-owned disconnect observers for the pinned managed SDK transport."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any


logger = logging.getLogger(__name__)
DisconnectObserver = Callable[[], None]


class ManagedSdkLifecycleObservers:
    """Preserve the SDK cleanup callback and fan out bounded Host cleanup."""

    def __init__(self, transport: object) -> None:
        callback = getattr(transport, "_disconnect_callback", None)
        if not callable(callback):
            raise RuntimeError("pinned SDK transport has no callable disconnect callback")
        self._transport = transport
        self._upstream = callback
        self._observers: list[DisconnectObserver] = []
        self._closed = False
        self._notified = False

        def wrapper() -> None:
            self._notify(upstream=True)

        self._wrapper = wrapper
        transport._disconnect_callback = wrapper

    def add(self, observer: DisconnectObserver) -> None:
        if not callable(observer):
            raise TypeError("disconnect observer must be callable")
        if self._closed:
            raise RuntimeError("managed SDK lifecycle observers are closed")
        if observer not in self._observers:
            self._observers.append(observer)

    def remove(self, observer: DisconnectObserver) -> None:
        try:
            self._observers.remove(observer)
        except ValueError:
            pass

    def close(self) -> None:
        """Run teardown cleanup once, unregister, and restore only our wrapper."""
        if self._closed:
            return
        try:
            self._notify(upstream=False)
        finally:
            self._closed = True
            self._observers.clear()
            if getattr(self._transport, "_disconnect_callback", None) is self._wrapper:
                self._transport._disconnect_callback = self._upstream
            else:
                logger.error("Managed SDK disconnect callback changed by another owner; left untouched")

    def _notify(self, *, upstream: bool) -> None:
        if self._notified:
            return
        self._notified = True
        upstream_error: BaseException | None = None
        if upstream:
            try:
                self._upstream()
            except BaseException as exc:  # Preserve SDK exception semantics after Host cleanup.
                upstream_error = exc
        for observer in tuple(self._observers):
            try:
                observer()
            except Exception as exc:
                # Never log callback text or event payloads: they may contain sensitive data.
                logger.warning("Managed SDK disconnect observer failed (%s)", type(exc).__name__)
        if upstream_error is not None:
            raise upstream_error
