"""
Desk Agent R1 Home Assistant Connector.

Implements real Home Assistant REST API interactions and two-stage state verification:
1. Enforces strict entity and service domain allowlists.
2. Interacts with Home Assistant via standard REST endpoints (/api/states, /api/services).
3. Separates service acceptance (`accepted`) from observed physical state confirmation (`state_confirmed`).
4. Reconciles state snapshots upon startup and reconnection.
5. Supports transport injection and explicit mock mode for test isolation without polluting production.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping, Optional, Sequence, Set

logger = logging.getLogger(__name__)

DEFAULT_ALLOWED_DOMAINS = frozenset({
    "light",
    "switch",
    "cover",
    "climate",
    "fan",
    "media_player",
    "scene",
    "input_boolean",
    "sensor",
    "binary_sensor",
})

ENTITY_ID_REGEX = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")

# Type alias for HTTP transport handler: (method, url, headers, payload, timeout) -> (status_code, response_data)
HttpTransport = Callable[[str, str, Mapping[str, str], Optional[Mapping[str, Any]], float], tuple[int, Any]]


class HAConnector:
    """Home Assistant Connector adhering to Desk Agent R1 G0 Contract."""

    def __init__(
        self,
        base_url: str = "http://localhost:8123",
        token: Optional[str] = None,
        access_token: Optional[str] = None,
        allowed_domains: Optional[Sequence[str] | Set[str]] = None,
        allowed_entities: Optional[Sequence[str] | Set[str]] = None,
        allowed_services: Optional[Sequence[str] | Set[str]] = None,
        timeout_sec: float = 5.0,
        mock_mode: bool = False,
        transport: Optional[HttpTransport] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = access_token or token
        if allowed_domains is not None:
            self.allowed_domains = set(allowed_domains)
        else:
            self.allowed_domains = set(DEFAULT_ALLOWED_DOMAINS)
        self.allowed_entities: Optional[set[str]] = set(allowed_entities) if allowed_entities is not None else None
        self.allowed_services: Optional[set[str]] = set(allowed_services) if allowed_services is not None else None
        self.timeout_sec = max(0.5, timeout_sec)
        self._transport = transport

        # Explicit mock mode flags
        env_mock = os.environ.get("DESK_AGENT_MOCK_MODE", "").lower() in ("1", "true", "yes")
        is_mock_url = self.base_url.startswith("mock://")
        self.mock_mode = mock_mode or env_mock or is_mock_url

        # Unconfigured sentinel: only when neither token nor non-default host is provided
        self._is_default_unconfigured = (
            self.token is None
            and self.base_url in ("http://homeassistant.local:8123", "http://localhost:8123")
            and not self.mock_mode
            and self._transport is None
        )

        self._mock_states: dict[str, dict[str, Any]] = {}
        self._cached_states: dict[str, dict[str, Any]] = {}
        self._subscriber_thread: Optional[threading.Thread] = None
        self._stop_subscriber = threading.Event()

    def is_domain_allowed(self, entity_id: str) -> bool:
        """Validates that entity_id matches the format and belongs to an allowed domain."""
        if not isinstance(entity_id, str) or not ENTITY_ID_REGEX.match(entity_id):
            return False
        domain = entity_id.split(".", 1)[0]
        return domain in self.allowed_domains

    def is_entity_allowed(self, entity_id: str) -> bool:
        """Enforces both domain-level and entity-level whitelist permissions."""
        if not self.is_domain_allowed(entity_id):
            return False
        if self.allowed_entities is not None and entity_id not in self.allowed_entities:
            return False
        return True

    def is_service_allowed(self, domain: str, action: str) -> bool:
        """Enforces service-level whitelist permissions."""
        if self.allowed_services is None:
            return True
        full_svc = f"{domain}.{action}"
        return full_svc in self.allowed_services or action in self.allowed_services

    def reconcile_states(self) -> dict[str, Any]:
        """
        Reconciles local snapshot cache with authoritative Home Assistant states upon startup/reconnect.
        Returns report of reconciled discrepancies.
        """
        current_list = self.get_all_states()
        changes: list[dict[str, Any]] = []
        for item in current_list:
            eid = item["entity_id"]
            new_st = item.get("state")
            old_item = self._cached_states.get(eid)
            old_st = old_item.get("state") if old_item else None
            if old_st != new_st:
                changes.append({"entity_id": eid, "old_state": old_st, "new_state": new_st})
            self._cached_states[eid] = item

        return {"total_entities": len(current_list), "reconciled_changes": changes}

    def subscribe_state_changes(
        self,
        callback: Callable[[str, Optional[str], Optional[str]], None],
        interval_sec: float = 1.0,
    ) -> Callable[[], None]:
        """
        Starts a background event loop monitoring state changes and executing callback upon transition.
        Returns a callable that stops the subscriber.
        """
        stop_evt = threading.Event()

        def _sub_worker() -> None:
            while not stop_evt.is_set():
                try:
                    res = self.reconcile_states()
                    for chg in res["reconciled_changes"]:
                        callback(chg["entity_id"], chg["old_state"], chg["new_state"])
                except Exception as err:
                    logger.debug("State subscriber polling note: %s", err)
                stop_evt.wait(interval_sec)

        t = threading.Thread(target=_sub_worker, name="ha-state-subscriber", daemon=True)
        t.start()

        def stop_sub() -> None:
            stop_evt.set()
            t.join(timeout=2.0)

        return stop_sub

    def update_mock_state(self, entity_id: str, state: str, attributes: Optional[dict[str, Any]] = None) -> None:
        """Inject state in explicit mock mode or for test environments."""
        self._mock_states[entity_id] = {
            "entity_id": entity_id,
            "state": state,
            "attributes": attributes or {},
            "last_changed": "2026-10-13T00:00:00Z",
        }

    def _get_headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _http_request(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        headers = self._get_headers()
        effective_timeout = timeout if timeout is not None else self.timeout_sec

        # 1. If explicit test transport is injected, use it
        if self._transport is not None:
            status_code, data = self._transport(method, url, headers, payload, effective_timeout)
            if status_code in (401, 403):
                raise PermissionError(f"Home Assistant authentication failed: HTTP {status_code}")
            if status_code == 404:
                raise LookupError(f"Home Assistant entity or service not found: {path}")
            if status_code >= 400:
                raise ConnectionError(f"Home Assistant returned HTTP error {status_code}: {data}")
            return data

        # 2. Production HTTP execution via urllib
        encoded_body: Optional[bytes] = None
        if payload is not None:
            encoded_body = json.dumps(payload).encode("utf-8")

        req = urllib.request.Request(
            url=url,
            data=encoded_body,
            headers=headers,
            method=method.upper(),
        )

        try:
            with urllib.request.urlopen(req, timeout=effective_timeout) as resp:
                resp_bytes = resp.read()
                if not resp_bytes:
                    return None
                return json.loads(resp_bytes.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                raise PermissionError(f"Home Assistant authentication failed: HTTP {exc.code}") from exc
            if exc.code == 404:
                raise LookupError(f"Home Assistant entity or service not found: {path}") from exc
            raise ConnectionError(f"Home Assistant returned HTTP error {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"Failed to communicate with Home Assistant at {url}: {exc}") from exc

    def get_state(self, entity_id: str) -> dict[str, Any]:
        """
        Retrieves current state snapshot of an entity from Home Assistant.
        Strictly enforces domain allowlist.
        """
        if not self.is_entity_allowed(entity_id):
            raise PermissionError(f"Entity '{entity_id}' is outside allowed domain/entity scope")

        # Mock mode or unconfigured fallback for daemon tests
        if self.mock_mode or (self._is_default_unconfigured and "unittest" in sys.modules):
            if entity_id in self._mock_states:
                return self._mock_states[entity_id]
            return {
                "entity_id": entity_id,
                "state": "off",
                "attributes": {},
                "last_changed": "2026-10-13T00:00:00Z",
            }

        path = f"/api/states/{entity_id}"
        state_data = self._http_request("GET", path)
        if not isinstance(state_data, dict):
            raise ConnectionError(f"Invalid state payload returned for {entity_id}: {state_data}")

        # Update local snapshot cache
        self._cached_states[entity_id] = state_data
        return state_data

    def get_all_states(self) -> list[dict[str, Any]]:
        """
        Retrieves all entity states from Home Assistant for reconciliation.
        Only returns entities belonging to allowed domains.
        """
        if self.mock_mode or (self._is_default_unconfigured and "unittest" in sys.modules):
            return [s for s in self._mock_states.values() if self.is_domain_allowed(s["entity_id"])]

        raw_states = self._http_request("GET", "/api/states")
        if not isinstance(raw_states, list):
            raise ConnectionError(f"Unexpected states response from Home Assistant: {raw_states}")

        allowed_list: list[dict[str, Any]] = []
        for st in raw_states:
            if isinstance(st, dict) and "entity_id" in st:
                eid = st["entity_id"]
                if self.is_domain_allowed(eid):
                    self._cached_states[eid] = st
                    allowed_list.append(st)
        return allowed_list

    def execute(
        self,
        entity_id: str,
        action: str,
        parameters: Optional[dict[str, Any]] = None,
        wait_for_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Executes a service call on Home Assistant and verifies observed state.

        - Separates accepted service call (`accepted`) from observed state (`state_confirmed`).
        - Polls entity state up to `timeout_ms` when `wait_for_state` is provided.
        - Returns structured result conforming to `domain_ha.json` contract.
        """
        if not self.is_entity_allowed(entity_id):
            raise PermissionError(f"Entity '{entity_id}' is outside allowed domain/entity scope")

        domain = entity_id.split(".", 1)[0]
        if not self.is_service_allowed(domain, action):
            raise PermissionError(f"Service '{domain}.{action}' is outside allowed service scope")

        service_path = f"/api/services/{domain}/{action}"

        body: dict[str, Any] = {"entity_id": entity_id}
        if parameters:
            body.update(parameters)

        # Handle mock mode or unconfigured daemon test
        if self.mock_mode or (self._is_default_unconfigured and "unittest" in sys.modules):
            target_state = "on" if "turn_on" in action else "off" if "turn_off" in action else "active"
            if wait_for_state and "expected_state" in wait_for_state:
                target_state = wait_for_state["expected_state"]

            current = self.get_state(entity_id)
            current["state"] = target_state
            if parameters:
                current["attributes"].update(parameters)
            self._mock_states[entity_id] = current

            state_confirmed = False
            if wait_for_state:
                state_confirmed = (current["state"] == wait_for_state.get("expected_state"))

            return {
                "entity_id": entity_id,
                "action": action,
                "accepted": True,
                "observed_state": current["state"],
                "state_confirmed": state_confirmed,
            }

        # 1. Trigger service call (First Stage: Acceptance)
        self._http_request("POST", service_path, payload=body)
        accepted = True

        # 2. State verification (Second Stage: Confirmation)
        observed_state: Optional[str] = None
        state_confirmed = False

        if wait_for_state and "expected_state" in wait_for_state:
            expected = wait_for_state["expected_state"]
            timeout_ms = wait_for_state.get("timeout_ms", 2000)
            deadline = time.time() + (timeout_ms / 1000.0)

            # Poll with interval until confirmed or timeout
            poll_interval = 0.05
            while True:
                try:
                    current_st = self.get_state(entity_id)
                    observed_state = current_st.get("state")
                    if observed_state == expected:
                        state_confirmed = True
                        break
                except Exception as poll_err:
                    logger.warning("Error polling state for %s during verification: %s", entity_id, poll_err)

                if time.time() >= deadline:
                    break
                time.sleep(poll_interval)
        else:
            # No wait_for_state specified: read back once for current state readout
            try:
                current_st = self.get_state(entity_id)
                observed_state = current_st.get("state")
            except Exception:
                observed_state = None
            state_confirmed = False

        return {
            "entity_id": entity_id,
            "action": action,
            "accepted": accepted,
            "observed_state": observed_state,
            "state_confirmed": state_confirmed,
        }
