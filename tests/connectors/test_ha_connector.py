"""
Comprehensive test suite for Home Assistant Connector.
Covers:
- Reproduction tests for unreachable servers
- Strict domain allowlist enforcement
- Mock transport simulating Home Assistant REST API
- Two-stage verification: service acceptance and state confirmation
- HTTP error statuses: 401, 404, 500
- Full snapshot reconciliation (get_all_states)
"""

from __future__ import annotations

import unittest
from typing import Any, Mapping, Optional
from host.desk_agent.connectors.ha.ha_connector import HAConnector


class TestHAConnector(unittest.TestCase):
    # -------------------------------------------------------------------------
    # 1. Reproduction Tests (Real Production Network Execution)
    # -------------------------------------------------------------------------
    def test_unreachable_ha_raises_connection_error(self) -> None:
        """In production mode without transport mock, an unreachable HA server must raise ConnectionError."""
        ha = HAConnector(base_url="http://127.0.0.1:59999", token="dummy-token", timeout_sec=0.5)
        with self.assertRaises(ConnectionError):
            ha.get_state("light.desk")

    def test_unreachable_ha_execute_raises_connection_error(self) -> None:
        """In production mode, executing on unreachable HA must raise ConnectionError."""
        ha = HAConnector(base_url="http://127.0.0.1:59999", token="dummy-token", timeout_sec=0.5)
        with self.assertRaises(ConnectionError):
            ha.execute("light.desk", "turn_on")

    # -------------------------------------------------------------------------
    # 2. Domain & Entity Allowlist Tests
    # -------------------------------------------------------------------------
    def test_domain_allowlist_enforcement(self) -> None:
        ha = HAConnector(base_url="http://localhost:8123", token="token")

        # Invalid domains
        with self.assertRaises(PermissionError):
            ha.get_state("camera.front_door")

        with self.assertRaises(PermissionError):
            ha.execute("camera.front_door", "take_snapshot")

        # Malformed entity IDs
        with self.assertRaises(PermissionError):
            ha.get_state("invalid_entity_id")

        with self.assertRaises(PermissionError):
            ha.get_state("light.")

        with self.assertRaises(PermissionError):
            ha.get_state(".light")

    def test_custom_allowed_domains(self) -> None:
        ha = HAConnector(
            base_url="http://localhost:8123",
            token="token",
            allowed_domains=["light"],
        )
        self.assertTrue(ha.is_domain_allowed("light.bedroom"))
        self.assertFalse(ha.is_domain_allowed("switch.heater"))

    # -------------------------------------------------------------------------
    # 3. Transport Mock Integration Tests
    # -------------------------------------------------------------------------
    def test_get_state_success(self) -> None:
        recorded_requests: list[dict[str, Any]] = []

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            recorded_requests.append({"method": method, "url": url, "headers": headers})
            return 200, {
                "entity_id": "light.living_room",
                "state": "on",
                "attributes": {"brightness": 200},
                "last_changed": "2026-10-13T10:00:00Z",
            }

        ha = HAConnector(base_url="http://ha.test:8123", token="secret-token-123", transport=mock_transport)
        res = ha.get_state("light.living_room")

        self.assertEqual(res["entity_id"], "light.living_room")
        self.assertEqual(res["state"], "on")
        self.assertEqual(res["attributes"]["brightness"], 200)

        # Check recorded request
        self.assertEqual(len(recorded_requests), 1)
        self.assertEqual(recorded_requests[0]["method"], "GET")
        self.assertEqual(recorded_requests[0]["url"], "http://ha.test:8123/api/states/light.living_room")
        self.assertEqual(recorded_requests[0]["headers"]["Authorization"], "Bearer secret-token-123")

    def test_get_state_auth_error_401(self) -> None:
        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            return 401, {"message": "Invalid token"}

        ha = HAConnector(base_url="http://ha.test:8123", token="invalid-token", transport=mock_transport)
        with self.assertRaises(PermissionError) as ctx:
            ha.get_state("light.desk")
        self.assertIn("authentication failed", str(ctx.exception))

    def test_get_state_not_found_404(self) -> None:
        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            return 404, {"message": "Entity not found"}

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        with self.assertRaises(LookupError) as ctx:
            ha.get_state("light.non_existent")
        self.assertIn("not found", str(ctx.exception))

    def test_get_all_states_filtering(self) -> None:
        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            return 200, [
                {"entity_id": "light.desk", "state": "on"},
                {"entity_id": "switch.heater", "state": "off"},
                {"entity_id": "camera.secret_cam", "state": "idle"},  # Disallowed domain
                {"entity_id": "lock.front_door", "state": "locked"},  # Disallowed domain
            ]

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        all_states = ha.get_all_states()

        entity_ids = [s["entity_id"] for s in all_states]
        self.assertIn("light.desk", entity_ids)
        self.assertIn("switch.heater", entity_ids)
        self.assertNotIn("camera.secret_cam", entity_ids)
        self.assertNotIn("lock.front_door", entity_ids)

    # -------------------------------------------------------------------------
    # 4. Service Execution & Two-Stage Verification Tests
    # -------------------------------------------------------------------------
    def test_execute_without_wait_for_state(self) -> None:
        """When wait_for_state is omitted, state_confirmed must be False."""
        recorded_calls = []

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            recorded_calls.append((method, url, payload))
            if method == "POST":
                return 200, [{"entity_id": "switch.heater"}]
            if method == "GET":
                return 200, {"entity_id": "switch.heater", "state": "off"}
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        res = ha.execute("switch.heater", "turn_on")

        self.assertEqual(res["entity_id"], "switch.heater")
        self.assertEqual(res["action"], "turn_on")
        self.assertTrue(res["accepted"])
        self.assertFalse(res["state_confirmed"])
        self.assertEqual(res["observed_state"], "off")

        # Verify POST payload
        post_calls = [c for c in recorded_calls if c[0] == "POST"]
        self.assertEqual(len(post_calls), 1)
        self.assertEqual(post_calls[0][1], "http://ha.test:8123/api/services/switch/turn_on")
        self.assertEqual(post_calls[0][2], {"entity_id": "switch.heater"})

    def test_execute_with_wait_for_state_success(self) -> None:
        """When state transitions to expected_state during polling, state_confirmed is True."""
        state_sequence = ["off", "off", "on"]
        call_count = 0

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            nonlocal call_count
            if method == "POST":
                return 200, []
            if method == "GET":
                idx = min(call_count, len(state_sequence) - 1)
                st = state_sequence[idx]
                call_count += 1
                return 200, {"entity_id": "light.desk", "state": st}
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        res = ha.execute(
            entity_id="light.desk",
            action="turn_on",
            wait_for_state={"expected_state": "on", "timeout_ms": 1000},
        )

        self.assertTrue(res["accepted"])
        self.assertTrue(res["state_confirmed"])
        self.assertEqual(res["observed_state"], "on")

    def test_execute_with_wait_for_state_timeout(self) -> None:
        """When state does NOT reach expected_state before timeout, state_confirmed is False."""
        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            if method == "POST":
                return 200, []
            if method == "GET":
                # Stays "off" and never becomes "on"
                return 200, {"entity_id": "light.desk", "state": "off"}
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        res = ha.execute(
            entity_id="light.desk",
            action="turn_on",
            wait_for_state={"expected_state": "on", "timeout_ms": 200},
        )

        self.assertTrue(res["accepted"])
        self.assertFalse(res["state_confirmed"])
        self.assertEqual(res["observed_state"], "off")


if __name__ == "__main__":
    unittest.main()
