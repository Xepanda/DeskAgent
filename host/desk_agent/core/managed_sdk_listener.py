"""Version-pinned adapter for observing managed SDK device messages."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any


MessageCallback = Callable[[dict[str, Any]], None]


class ManagedSdkMessageListener:
    """Expose the pinned SDK transport's observer API without replacing callbacks.

    ``ApplicationContext`` does not yet expose a public listener facade. Keep its
    private transport access here so SDK compatibility changes have one boundary.
    """

    def __init__(self, context: object) -> None:
        transport = getattr(context, "_transport", None)
        add_listener = getattr(transport, "add_message_listener", None)
        remove_listener = getattr(transport, "remove_message_listener", None)
        if not callable(add_listener) or not callable(remove_listener):
            raise TypeError(
                "Managed SDK transport must provide add/remove_message_listener"
            )
        self._transport = transport

    def add(self, callback: MessageCallback) -> None:
        """Observe parsed Device-channel JSON messages."""

        self._transport.add_message_listener(callback)

    def remove(self, callback: MessageCallback) -> None:
        """Stop observing parsed Device-channel JSON messages."""

        self._transport.remove_message_listener(callback)
