"""
Comprehensive test suite for Home Assistant Connector.
Covers:
- Reproduction tests for unreachable servers
- Strict domain, entity, and service allowlist and denylist enforcement
- Mock transport simulating Home Assistant REST API
- WebSocket authentication handshake, subscription, and event streaming
- WebSocket auth failure handling
- Two-stage verification: service acceptance and state confirmation (polling and WebSocket event)
- HTTP error statuses: 401, 404, 429 rate limiting, 500
- Full snapshot reconciliation (get_all_states & reconcile_states)
"""

from __future__ import annotations

import json
import threading
import time
import unittest
from typing import Any, Mapping, Optional
from host.desk_agent.connectors.ha.ha_connector import HAConnector


class MockWebSocketClient:
    """Mock WebSocket client for testing Home Assistant WebSocket protocol."""

    def __init__(self, script: Optional[list[str]] = None) -> None:
        self.incoming: list[str] = list(script) if script else []
        self.sent: list[str] = []
        self.closed = False
        self._cond = threading.Condition()

    def recv(self, timeout: Optional[float] = None) -> str:
        with self._cond:
            deadline = time.time() + (timeout if timeout is not None else 1.0)
            while not self.incoming and not self.closed:
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise TimeoutError("Mock WebSocket recv timeout")
                self._cond.wait(timeout=remaining)
            if self.incoming:
                return self.incoming.pop(0)
            raise ConnectionError("Mock WebSocket closed")

    def send(self, data: str) -> None:
        self.sent.append(data)

    def push_message(self, data: str) -> None:
        with self._cond:
            self.incoming.append(data)
            self._cond.notify_all()

    def close(self) -> None:
        self.closed = True
        with self._cond:
            self._cond.notify_all()


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
    # 2. Domain & Entity Allowlist / Denylist Tests
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

    def test_denied_entities_enforcement(self) -> None:
        ha = HAConnector(
            base_url="http://localhost:8123",
            token="token",
            allowed_domains=["light"],
            denied_entities=["light.master_safe"],
        )
        self.assertTrue(ha.is_entity_allowed("light.living_room"))
        self.assertFalse(ha.is_entity_allowed("light.master_safe"))
        with self.assertRaises(PermissionError):
            ha.get_state("light.master_safe")

    def test_denied_services_enforcement(self) -> None:
        ha = HAConnector(
            base_url="http://localhost:8123",
            token="token",
            denied_services=["light.turn_off"],
        )
        self.assertTrue(ha.is_service_allowed("light", "turn_on"))
        self.assertFalse(ha.is_service_allowed("light", "turn_off"))
        # Default denied service
        self.assertFalse(ha.is_service_allowed("homeassistant", "restart"))
        with self.assertRaises(PermissionError):
            ha.execute("light.desk", "turn_off")

    def test_default_service_allowlist_is_explicit_and_domain_scoped(self) -> None:
        """Unconfigured deployments expose only declared low-risk controls, never action-wide grants."""
        ha = HAConnector(base_url="http://ha.test:8123", token="token")
        self.assertTrue(ha.is_service_allowed("light", "turn_on"))
        self.assertTrue(ha.is_service_allowed("climate", "set_temperature"))
        self.assertFalse(ha.is_service_allowed("light", "reload"))
        self.assertFalse(ha.is_service_allowed("media_player", "join"))
        self.assertFalse(ha.is_service_allowed("sensor", "turn_on"))
        self.assertFalse(ha.is_service_allowed("homeassistant", "restart"))

    def test_allowed_service_requires_domain_qualified_entry(self) -> None:
        """A configured bare action cannot accidentally authorize the same action across domains."""
        ha = HAConnector(allowed_services=["turn_on"])
        self.assertFalse(ha.is_service_allowed("light", "turn_on"))
        with self.assertRaises(PermissionError):
            ha.execute("light.desk", "turn_on")

    def test_parameters_cannot_replace_allowlisted_target_entity(self) -> None:
        """A caller cannot pass entity authorization for one entity and execute against another."""
        posted: list[Mapping[str, Any]] = []

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            if method == "POST":
                posted.append(payload or {})
            return 200, {"entity_id": "light.desk", "state": "on"}

        ha = HAConnector(
            base_url="http://ha.test:8123",
            token="token",
            allowed_entities=["light.desk"],
            transport=mock_transport,
        )
        with self.assertRaises(ValueError):
            ha.execute("light.desk", "turn_on", parameters={"entity_id": "light.restricted"})
        self.assertEqual(posted, [])

    def test_invalid_state_confirmation_timeout_rejected_before_service_call(self) -> None:
        """Malformed or negative confirmation waits must not trigger a service call."""
        recorded_methods: list[str] = []

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            recorded_methods.append(method)
            return 200, []

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        for invalid_timeout in (-1, float("inf"), "1000", True):
            with self.subTest(timeout_ms=invalid_timeout):
                with self.assertRaises(ValueError):
                    ha.execute("light.desk", "turn_on", wait_for_state={
                        "expected_state": "on", "timeout_ms": invalid_timeout,
                    })

        self.assertEqual(recorded_methods, [])

    def test_state_confirmation_timeout_is_capped(self) -> None:
        """Untrusted callers cannot extend synchronous state confirmation beyond 30 seconds."""
        from host.desk_agent.connectors.ha.ha_connector import _normalize_confirmation_timeout_ms

        self.assertEqual(_normalize_confirmation_timeout_ms(10**12), 30_000)
        self.assertEqual(_normalize_confirmation_timeout_ms(1250), 1250)

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

    def test_rate_limit_429_backoff(self) -> None:
        call_count = 0

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            nonlocal call_count
            call_count += 1
            return 429, {"message": "Too Many Requests"}

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        with self.assertRaises(ConnectionError) as ctx1:
            ha.get_state("light.desk")
        self.assertIn("429", str(ctx1.exception))
        self.assertEqual(call_count, 1)

        # Immediate follow-up call is prevented by rate limit cooldown before touching transport
        with self.assertRaises(ConnectionError) as ctx2:
            ha.get_state("light.desk")
        self.assertIn("rate limit active", str(ctx2.exception))
        self.assertEqual(call_count, 1)

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

    def test_reconcile_states_discrepancies(self) -> None:
        state_data = [{"entity_id": "light.desk", "state": "off"}]

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            return 200, list(state_data)

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        rep1 = ha.reconcile_states()
        self.assertEqual(len(rep1["reconciled_changes"]), 1)
        self.assertEqual(rep1["reconciled_changes"][0]["new_state"], "off")

        # Second reconciliation with same state yields 0 changes
        rep2 = ha.reconcile_states()
        self.assertEqual(len(rep2["reconciled_changes"]), 0)

        # External state changed in HA
        state_data[0] = {"entity_id": "light.desk", "state": "on"}
        rep3 = ha.reconcile_states()
        self.assertEqual(len(rep3["reconciled_changes"]), 1)
        self.assertEqual(rep3["reconciled_changes"][0]["old_state"], "off")
        self.assertEqual(rep3["reconciled_changes"][0]["new_state"], "on")

    def test_polling_subscriber_exception_does_not_drop_other_snapshot_changes(self) -> None:
        """One consumer exception must not permanently lose later changes in the same snapshot."""
        ha = HAConnector(mock_mode=True)
        ha._mock_states = {
            "light.desk": {"entity_id": "light.desk", "state": "on", "attributes": {}},
            "switch.heater": {"entity_id": "switch.heater", "state": "on", "attributes": {}},
        }
        delivered: list[str] = []
        heater_event = threading.Event()

        def subscriber(entity_id: str, old_state: Optional[str], new_state: Optional[str]) -> None:
            delivered.append(entity_id)
            if entity_id == "light.desk":
                raise RuntimeError("simulated consumer bug")
            if entity_id == "switch.heater":
                heater_event.set()

        stop = ha.subscribe_state_changes(subscriber, interval_sec=0.02)
        try:
            self.assertTrue(heater_event.wait(timeout=1.0))
            self.assertEqual(delivered, ["light.desk", "switch.heater"])
        finally:
            stop()
            ha.close()

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

    def test_execute_distinguishes_observed_state_transition_from_target_confirmation(self) -> None:
        """A matching final HA state may confirm the target without proving it changed or acted physically."""
        observed = {"state": "on"}
        baseline = {"state": "off"}
        get_count = 0

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            nonlocal get_count
            if method == "POST":
                return 200, []
            if method == "GET":
                get_count += 1
                state = baseline["state"] if get_count == 1 else observed["state"]
                return 200, {"entity_id": "light.desk", "state": state, "attributes": {}}
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        res = ha.execute(
            "light.desk", "turn_on",
            wait_for_state={"expected_state": "on", "timeout_ms": 100},
        )

        self.assertTrue(res["accepted"])
        self.assertTrue(res["state_confirmed"])
        self.assertTrue(res["state_changed"])
        self.assertIsNone(res["physical_action_confirmed"])

        # If the observed target state already existed before service execution,
        # target confirmation is true while the observed transition is false.
        get_count = 0
        observed["state"] = "on"
        baseline["state"] = "on"
        same_state_ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        same_state_res = same_state_ha.execute(
            "light.desk", "turn_on",
            wait_for_state={"expected_state": "on", "timeout_ms": 100},
        )
        self.assertTrue(same_state_res["state_confirmed"])
        self.assertFalse(same_state_res["state_changed"])
        self.assertIsNone(same_state_res["physical_action_confirmed"])

    def test_execute_continues_when_pre_call_state_snapshot_is_unavailable(self) -> None:
        """A failed observational read does not block an accepted command and cannot claim a transition."""
        get_count = 0

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            nonlocal get_count
            if method == "POST":
                return 200, []
            if method == "GET":
                get_count += 1
                if get_count == 1:
                    return 503, {"message": "temporary state endpoint failure"}
                return 200, {"entity_id": "light.desk", "state": "on", "attributes": {}}
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        res = ha.execute(
            "light.desk", "turn_on",
            wait_for_state={"expected_state": "on", "timeout_ms": 100},
        )

        self.assertTrue(res["accepted"])
        self.assertTrue(res["state_confirmed"])
        self.assertIsNone(res["state_changed"])
        self.assertIsNone(res["physical_action_confirmed"])

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

    # -------------------------------------------------------------------------
    # 5. WebSocket Integration & Real-time Event Streaming Tests
    # -------------------------------------------------------------------------
    def test_websocket_handshake_and_event_streaming(self) -> None:
        """Tests Home Assistant WebSocket auth handshake and event dispatching to subscribers."""
        script = [
            json.dumps({"type": "auth_required", "ha_version": "2026.10"}),
            json.dumps({"type": "auth_ok", "ha_version": "2026.10"}),
            json.dumps({"id": 1, "type": "result", "success": True, "result": None}),
        ]
        mock_ws = MockWebSocketClient(script)
        received_events: list[tuple[str, Optional[str], Optional[str]]] = []
        event_received = threading.Event()

        def on_change(eid: str, old_st: Optional[str], new_st: Optional[str]) -> None:
            received_events.append((eid, old_st, new_st))
            event_received.set()

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            return 200, [{"entity_id": "light.desk", "state": "off"}]

        ha = HAConnector(
            base_url="http://ha.test:8123",
            token="secret-access-token",
            transport=mock_transport,
            ws_factory=lambda url: mock_ws,
        )

        stop_sub = ha.subscribe_state_changes(on_change)
        try:
            # Wait for handshake completion
            deadline = time.time() + 1.0
            while not ha.is_ws_connected and time.time() < deadline:
                time.sleep(0.02)

            self.assertTrue(ha.is_ws_connected)
            self.assertGreaterEqual(len(mock_ws.sent), 2)
            auth_sent = json.loads(mock_ws.sent[0])
            self.assertEqual(auth_sent["type"], "auth")
            self.assertEqual(auth_sent["access_token"], "secret-access-token")

            # Simulate incoming state_changed event from Home Assistant
            mock_ws.push_message(json.dumps({
                "id": 1,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "light.desk",
                        "old_state": {"entity_id": "light.desk", "state": "off"},
                        "new_state": {"entity_id": "light.desk", "state": "on"},
                    },
                },
            }))

            self.assertTrue(event_received.wait(timeout=2.0))
            self.assertEqual(len(received_events), 1)
            self.assertEqual(received_events[0], ("light.desk", "off", "on"))
        finally:
            stop_sub()
            ha.close()

    def test_websocket_auth_failure(self) -> None:
        """Tests that invalid auth message from HA keeps is_ws_connected False."""
        script = [
            json.dumps({"type": "auth_required", "ha_version": "2026.10"}),
            json.dumps({"type": "auth_invalid", "message": "Invalid password"}),
        ]
        mock_ws = MockWebSocketClient(script)

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            return 200, []

        ha = HAConnector(
            base_url="http://ha.test:8123",
            token="bad-token",
            transport=mock_transport,
            ws_factory=lambda url: mock_ws,
        )

        stop_sub = ha.subscribe_state_changes(lambda *args: None)
        try:
            time.sleep(0.1)
            self.assertFalse(ha.is_ws_connected)
        finally:
            stop_sub()
            ha.close()

    def test_websocket_reconnects_after_transient_unavailability(self) -> None:
        """A transient HA outage must recover the authenticated event channel without restarting the host."""
        connected_ws = MockWebSocketClient([
            json.dumps({"type": "auth_required", "ha_version": "2026.10"}),
            json.dumps({"type": "auth_ok", "ha_version": "2026.10"}),
            json.dumps({"id": 1, "type": "result", "success": True, "result": None}),
        ])
        attempts = 0

        def ws_factory(url: str) -> MockWebSocketClient:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise ConnectionError("simulated temporary HA outage")
            return connected_ws

        ha = HAConnector(
            base_url="http://ha.test:8123",
            token="token",
            transport=lambda *args: (200, []),
            ws_factory=ws_factory,
        )
        stop_sub = ha.subscribe_state_changes(lambda *args: None, interval_sec=0.1)
        try:
            deadline = time.monotonic() + 3.5
            while not ha.is_ws_connected and time.monotonic() < deadline:
                time.sleep(0.02)

            self.assertGreaterEqual(attempts, 2)
            self.assertTrue(ha.is_ws_connected)
            sent = [json.loads(message) for message in connected_ws.sent]
            self.assertEqual(sent[0]["type"], "auth")
            self.assertEqual(sent[-1]["type"], "subscribe_events")
        finally:
            stop_sub()
            ha.close()

    def test_websocket_rejected_subscription_is_not_reported_connected(self) -> None:
        """HA must acknowledge the exact subscription before the connector claims event coverage."""
        script = [
            json.dumps({"type": "auth_required", "ha_version": "2026.10"}),
            json.dumps({"type": "auth_ok", "ha_version": "2026.10"}),
            json.dumps({"id": 999, "type": "result", "success": True, "result": None}),
        ]
        mock_ws = MockWebSocketClient(script)
        ha = HAConnector(
            base_url="http://ha.test:8123",
            token="token",
            transport=lambda *args: (200, []),
            ws_factory=lambda url: mock_ws,
        )

        stop_sub = ha.subscribe_state_changes(lambda *args: None)
        try:
            time.sleep(0.1)
            self.assertFalse(ha.is_ws_connected)
        finally:
            stop_sub()
            ha.close()

    def test_websocket_unexpected_initial_message_is_not_authenticated(self) -> None:
        """An unexpected greeting cannot proceed to event subscription as an authenticated session."""
        mock_ws = MockWebSocketClient([json.dumps({"type": "event", "event": {}})])
        ha = HAConnector(
            base_url="http://ha.test:8123",
            token="token",
            transport=lambda *args: (200, []),
            ws_factory=lambda url: mock_ws,
        )

        stop_sub = ha.subscribe_state_changes(lambda *args: None)
        try:
            time.sleep(0.1)
            self.assertFalse(ha.is_ws_connected)
            self.assertEqual(mock_ws.sent, [])
        finally:
            stop_sub()
            ha.close()

    def test_execute_fast_confirmation_via_websocket_event(self) -> None:
        """Tests that execute(..., wait_for_state=...) wakes up immediately when WebSocket event arrives."""
        script = [
            json.dumps({"type": "auth_required", "ha_version": "2026.10"}),
            json.dumps({"type": "auth_ok", "ha_version": "2026.10"}),
            json.dumps({"id": 1, "type": "result", "success": True, "result": None}),
        ]
        mock_ws = MockWebSocketClient(script)
        current_state = {"state": "off"}

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            if method == "POST":
                return 200, []
            if method == "GET":
                return 200, {"entity_id": "light.desk", "state": current_state["state"]}
            return 500, None

        ha = HAConnector(
            base_url="http://ha.test:8123",
            token="token",
            transport=mock_transport,
            ws_factory=lambda url: mock_ws,
        )
        stop_sub = ha.subscribe_state_changes(lambda *args: None)

        def push_event_later() -> None:
            time.sleep(0.06)
            current_state["state"] = "on"
            mock_ws.push_message(json.dumps({
                "id": 1,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "light.desk",
                        "old_state": {"entity_id": "light.desk", "state": "off"},
                        "new_state": {"entity_id": "light.desk", "state": "on"},
                    },
                },
            }))

        threading.Thread(target=push_event_later, daemon=True).start()

        start_t = time.time()
        res = ha.execute("light.desk", "turn_on", wait_for_state={"expected_state": "on", "timeout_ms": 3000})
        elapsed = time.time() - start_t

        self.assertTrue(res["accepted"])
        self.assertTrue(res["state_confirmed"])
        self.assertEqual(res["observed_state"], "on")
        self.assertLess(elapsed, 1.5)  # Resolved promptly upon event rather than waiting 3s

        stop_sub()
        ha.close()

    # -------------------------------------------------------------------------
    # 6. Target Attributes Verification & Security Permissions Injection Tests
    # -------------------------------------------------------------------------
    def test_execute_service_attribute_mismatch_unconfirmed(self) -> None:
        """When state matches expected_state but attribute does not match, state_confirmed must be False."""
        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            if method == "POST":
                return 200, []
            if method == "GET":
                return 200, {
                    "entity_id": "light.desk",
                    "state": "on",
                    "attributes": {"brightness": 100},
                }
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        # Calling via execute_service with parameters indicating target brightness
        res = ha.execute_service(
            entity_id="light.desk",
            action="turn_on",
            parameters={"brightness": 200},
            wait_for_state={"expected_state": "on", "timeout_ms": 100},
        )

        self.assertTrue(res["accepted"])
        self.assertFalse(res["state_confirmed"])
        self.assertEqual(res["observed_state"], "on")

    def test_execute_service_attribute_transition_confirmed(self) -> None:
        """When state and attributes transition to target during polling, state_confirmed becomes True."""
        call_count = 0

        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            nonlocal call_count
            if method == "POST":
                return 200, []
            if method == "GET":
                call_count += 1
                if call_count == 1:
                    # Initial state: on, but brightness transitioning (100)
                    return 200, {"entity_id": "light.desk", "state": "on", "attributes": {"brightness": 100}}
                else:
                    # Polled state: reached target brightness (200)
                    return 200, {"entity_id": "light.desk", "state": "on", "attributes": {"brightness": 200}}
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        res = ha.execute_service(
            entity_id="light.desk",
            action="turn_on",
            parameters={"brightness": 200},
            wait_for_state={"expected_state": "on", "timeout_ms": 1000},
        )

        self.assertTrue(res["accepted"])
        self.assertTrue(res["state_confirmed"])
        self.assertEqual(res["observed_state"], "on")

    def test_execute_service_explicit_expected_attributes(self) -> None:
        """Explicit expected_attributes in wait_for_state overrides check_parameters inference."""
        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            if method == "POST":
                return 200, []
            if method == "GET":
                return 200, {
                    "entity_id": "climate.ac",
                    "state": "cool",
                    "attributes": {"temperature": 24.0, "fan_mode": "auto"},
                }
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        # Target temperature matches (24.0), fan_mode in parameters ignored because expected_attributes specified
        res = ha.execute_service(
            entity_id="climate.ac",
            action="set_temperature",
            parameters={"temperature": 24.0, "fan_mode": "high"},
            wait_for_state={
                "expected_state": "cool",
                "expected_attributes": {"temperature": 24.0},
                "timeout_ms": 200,
            },
        )
        self.assertTrue(res["accepted"])
        self.assertTrue(res["state_confirmed"])

    def test_execute_service_numeric_tolerance(self) -> None:
        """Numeric attributes support small tolerance (e.g. brightness +-2, temperature +-0.5)."""
        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            if method == "POST":
                return 200, []
            if method == "GET":
                return 200, {
                    "entity_id": "light.desk",
                    "state": "on",
                    "attributes": {"brightness": 201},  # 201 is within +-2 of target 200
                }
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        res = ha.execute_service(
            entity_id="light.desk",
            action="turn_on",
            parameters={"brightness": 200},
            wait_for_state={"expected_state": "on", "timeout_ms": 100},
        )
        self.assertTrue(res["accepted"])
        self.assertTrue(res["state_confirmed"])

    def test_execute_service_check_parameters_disabled(self) -> None:
        """When check_parameters is False, attribute differences do not block confirmation."""
        def mock_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            if method == "POST":
                return 200, []
            if method == "GET":
                return 200, {
                    "entity_id": "light.desk",
                    "state": "on",
                    "attributes": {"brightness": 50},
                }
            return 500, None

        ha = HAConnector(base_url="http://ha.test:8123", token="token", transport=mock_transport)
        res = ha.execute_service(
            entity_id="light.desk",
            action="turn_on",
            parameters={"brightness": 200},
            wait_for_state={"expected_state": "on", "check_parameters": False, "timeout_ms": 100},
        )
        self.assertTrue(res["accepted"])
        self.assertTrue(res["state_confirmed"])

    def test_execute_mock_mode_attribute_check(self) -> None:
        """In mock mode, expected_attributes mismatch reports state_confirmed=False."""
        ha = HAConnector(mock_mode=True)
        # Expected brightness 255 but parameters set brightness to 200
        res = ha.execute_service(
            entity_id="light.desk",
            action="turn_on",
            parameters={"brightness": 200},
            wait_for_state={"expected_state": "on", "expected_attributes": {"brightness": 255}},
        )
        self.assertTrue(res["accepted"])
        self.assertFalse(res["state_confirmed"])

        # Expected brightness 200 matches parameters
        res_ok = ha.execute_service(
            entity_id="light.desk",
            action="turn_on",
            parameters={"brightness": 200},
            wait_for_state={"expected_state": "on", "expected_attributes": {"brightness": 200}},
        )
        self.assertTrue(res_ok["accepted"])
        self.assertTrue(res_ok["state_confirmed"])

    def test_ha_security_boundaries_from_config(self) -> None:
        """HAConnector initialized with allowed/denied services and entities enforces boundaries."""
        ha = HAConnector(
            base_url="http://ha.test:8123",
            token="token",
            allowed_services=["light.turn_on", "light.turn_off"],
            denied_services=["switch.turn_on"],
            denied_entities=["light.restricted_desk"],
            mock_mode=True,
        )

        # Denied entity
        self.assertFalse(ha.is_entity_allowed("light.restricted_desk"))
        with self.assertRaises(PermissionError):
            ha.execute_service("light.restricted_desk", "turn_on")

        # Denied service
        self.assertFalse(ha.is_service_allowed("switch", "turn_on"))
        with self.assertRaises(PermissionError):
            ha.execute_service("switch.heater", "turn_on")

        # Service not in allowed_services
        self.assertFalse(ha.is_service_allowed("fan", "turn_on"))
        with self.assertRaises(PermissionError):
            ha.execute_service("fan.ceiling", "turn_on")

        # Allowed service & entity
        self.assertTrue(ha.is_entity_allowed("light.desk"))
        self.assertTrue(ha.is_service_allowed("light", "turn_on"))
        res = ha.execute_service("light.desk", "turn_on")
        self.assertTrue(res["accepted"])

    def test_desk_agent_host_app_config_injection(self) -> None:
        """DeskAgentHostApp wires allowed_services, denied_services, and denied_entities into HAConnector."""
        import tempfile
        from pathlib import Path
        from host.desk_agent.main import DeskAgentHostApp

        with tempfile.TemporaryDirectory() as tmp_dir:
            cfg_file = Path(tmp_dir) / "config.json"
            db_path = Path(tmp_dir) / "test.db"
            cfg_data = {
                "storage": {"db_path": str(db_path)},
                "robot": {"enabled": False},
                "home_assistant": {
                    "base_url": "mock://ha.local:8123",
                    "access_token": "test-token",
                    "allowed_domains": ["light", "switch"],
                    "allowed_services": ["light.turn_on", "switch.turn_on"],
                    "denied_services": ["switch.turn_off"],
                    "denied_entities": ["light.restricted_vault"],
                },
            }
            cfg_file.write_text(json.dumps(cfg_data))

            app = DeskAgentHostApp(config_path=str(cfg_file))
            try:
                self.assertEqual(app.ha_connector.allowed_services, {"light.turn_on", "switch.turn_on"})
                self.assertEqual(app.ha_connector.denied_services, {"switch.turn_off"})
                self.assertEqual(app.ha_connector.denied_entities, {"light.restricted_vault"})

                # Verify permissions are enforced through app's ha_connector
                self.assertFalse(app.ha_connector.is_service_allowed("switch", "turn_off"))
                self.assertFalse(app.ha_connector.is_entity_allowed("light.restricted_vault"))
                self.assertTrue(app.ha_connector.is_service_allowed("light", "turn_on"))
            finally:
                app.close()


if __name__ == "__main__":
    unittest.main()

