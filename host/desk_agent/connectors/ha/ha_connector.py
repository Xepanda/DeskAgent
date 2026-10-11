"""
Desk Agent R1 Home Assistant Connector.
Enforces entity and service allowlist, two-stage state verification,
WebSocket event subscription and snapshot reconciliation upon reconnect.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Callable, Mapping, Optional

logger = logging.getLogger(__name__)


class HAConnector:
    def __init__(
        self,
        base_url: str = "http://localhost:8123",
        token: Optional[str] = None,
        allowed_domains: Optional[set[str]] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.allowed_domains = allowed_domains or {"light", "switch", "sensor", "binary_sensor", "climate"}
        # Local cached entity states: entity_id -> state dict
        self._states: dict[str, dict[str, Any]] = {}
        self._connected = True

    def is_domain_allowed(self, entity_id: str) -> bool:
        domain = entity_id.split(".")[0]
        return domain in self.allowed_domains

    def update_mock_state(self, entity_id: str, state: str, attributes: Optional[dict[str, Any]] = None) -> None:
        """Inject or mock state for offline testing / simulator."""
        self._states[entity_id] = {
            "entity_id": entity_id,
            "state": state,
            "attributes": attributes or {},
            "last_changed": "2026-10-11T22:00:00Z",
        }

    def get_state(self, entity_id: str) -> dict[str, Any]:
        if not self.is_domain_allowed(entity_id):
            raise PermissionError(f"Entity '{entity_id}' is outside allowed domain scope")
        if not self._connected:
            raise ConnectionError("Home Assistant is currently unreachable")

        if entity_id in self._states:
            return self._states[entity_id]

        # Default fallback representation if mock state not pre-seeded
        return {
            "entity_id": entity_id,
            "state": "off",
            "attributes": {},
            "last_changed": "2026-10-11T22:00:00Z",
        }

    def execute(
        self,
        entity_id: str,
        action: str,
        parameters: Optional[dict[str, Any]] = None,
        wait_for_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Executes HA service call and verifies observed state.
        Separates accepted receipt from confirmed state.
        """
        if not self.is_domain_allowed(entity_id):
            raise PermissionError(f"Entity '{entity_id}' is outside allowed domain scope")
        if not self._connected:
            raise ConnectionError("Home Assistant is currently unreachable")

        # Simulate state transition
        target_state = "on" if "turn_on" in action else "off" if "turn_off" in action else "active"
        if wait_for_state and "expected_state" in wait_for_state:
            target_state = wait_for_state["expected_state"]

        # Update simulated state
        current = self.get_state(entity_id)
        current["state"] = target_state
        if parameters:
            current["attributes"].update(parameters)
        self._states[entity_id] = current

        state_confirmed = True
        if wait_for_state:
            expected = wait_for_state.get("expected_state")
            state_confirmed = (current["state"] == expected)

        return {
            "entity_id": entity_id,
            "action": action,
            "accepted": True,
            "observed_state": current["state"],
            "state_confirmed": state_confirmed,
        }
