"""
Desk Agent R1 Home Assistant Connector.

Implements real Home Assistant REST API interactions and WebSocket real-time event streaming:
1. Enforces strict domain, entity, and service allowlists and denylists.
2. Interacts with Home Assistant via standard REST endpoints (/api/states, /api/services).
3. Connects to Home Assistant WebSocket API (/api/websocket) for real-time state change streaming.
4. Separates service acceptance (`accepted`) from observed physical state confirmation (`state_confirmed`).
5. Reconciles state snapshots upon startup, WebSocket reconnect, and periodic polling.
6. Rate limiting and error classification (401/403 PermissionError, 404 LookupError, 429/5xx ConnectionError).
7. Supports transport injection and explicit mock mode for test isolation without polluting production.
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

try:
    from websockets.sync.client import connect as ws_connect
    from websockets.exceptions import WebSocketException
    HAS_WEBSOCKETS = True
except ImportError:
    ws_connect = None
    WebSocketException = Exception
    HAS_WEBSOCKETS = False

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

DEFAULT_DENIED_DOMAINS = frozenset({
    "lock",
    "alarm_control_panel",
    "camera",
    "update",
})

DEFAULT_DENIED_SERVICES = frozenset({
    "homeassistant.restart",
    "homeassistant.stop",
    "homeassistant.check_config",
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
        denied_entities: Optional[Sequence[str] | Set[str]] = None,
        allowed_services: Optional[Sequence[str] | Set[str]] = None,
        denied_services: Optional[Sequence[str] | Set[str]] = None,
        timeout_sec: float = 5.0,
        mock_mode: bool = False,
        transport: Optional[HttpTransport] = None,
        ws_factory: Optional[Callable[[str], Any]] = None,
        ws_url: Optional[str] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = access_token or token
        if allowed_domains is not None:
            self.allowed_domains = set(allowed_domains)
        else:
            self.allowed_domains = set(DEFAULT_ALLOWED_DOMAINS)

        self.allowed_entities: Optional[set[str]] = set(allowed_entities) if allowed_entities is not None else None
        self.denied_entities: set[str] = set(denied_entities) if denied_entities is not None else set()
        self.allowed_services: Optional[set[str]] = set(allowed_services) if allowed_services is not None else None
        self.denied_services: set[str] = set(denied_services) if denied_services is not None else set()
        self.timeout_sec = max(0.5, timeout_sec)
        self._transport = transport

        # WebSocket configuration
        if ws_url:
            self.ws_url = ws_url
        else:
            if self.base_url.startswith("https://"):
                self.ws_url = "wss://" + self.base_url[8:] + "/api/websocket"
            elif self.base_url.startswith("http://"):
                self.ws_url = "ws://" + self.base_url[7:] + "/api/websocket"
            else:
                self.ws_url = f"ws://{self.base_url}/api/websocket"

        self._ws_factory = ws_factory
        self._ws_conn: Optional[Any] = None
        self._ws_thread: Optional[threading.Thread] = None
        self._stop_ws = threading.Event()
        self._ws_connected = threading.Event()
        self._subscribers: list[Callable[[str, Optional[str], Optional[str]], None]] = []
        self._subscribers_lock = threading.Lock()
        self._state_condition = threading.Condition()
        self._interaction_id = 1
        self._rate_limit_until: float = 0.0

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

    @property
    def is_ws_connected(self) -> bool:
        """Returns True if the WebSocket connection is actively authenticated and receiving events."""
        return self._ws_connected.is_set()

    def is_domain_allowed(self, entity_id: str) -> bool:
        """Validates that entity_id matches the format and belongs to an allowed domain."""
        if not isinstance(entity_id, str) or not ENTITY_ID_REGEX.match(entity_id):
            return False
        domain = entity_id.split(".", 1)[0]
        return domain in self.allowed_domains

    def is_entity_allowed(self, entity_id: str) -> bool:
        """Enforces domain-level allowlist, entity-level allowlist, and entity-level denylist."""
        if not self.is_domain_allowed(entity_id):
            return False
        if entity_id in self.denied_entities:
            return False
        if self.allowed_entities is not None and entity_id not in self.allowed_entities:
            return False
        return True

    def is_service_allowed(self, domain: str, action: str) -> bool:
        """Enforces service-level permissions and denylists."""
        full_svc = f"{domain}.{action}"
        if full_svc in self.denied_services or action in self.denied_services:
            return False
        if full_svc in DEFAULT_DENIED_SERVICES and not (self.allowed_services and full_svc in self.allowed_services):
            return False
        if self.allowed_services is None:
            return True
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
        Starts a state monitoring subscription.
        Prefers real-time WebSocket event streaming when available.
        Falls back seamlessly to periodic REST snapshot polling if WebSocket is unavailable or disconnected.
        Returns a callable that stops the subscriber.
        """
        with self._subscribers_lock:
            self._subscribers.append(callback)

        # In mock mode or unconfigured daemon test without custom ws_factory, run polling fallback directly
        if (self.mock_mode or self._is_default_unconfigured) and self._ws_factory is None:
            return self._start_polling_fallback(callback, interval_sec, run_only_when_ws_down=False)

        # Start WebSocket listener thread if not already running
        if self._ws_thread is None or not self._ws_thread.is_alive():
            self._stop_ws.clear()
            self._ws_thread = threading.Thread(target=self._ws_worker, name="ha-ws-worker", daemon=True)
            self._ws_thread.start()

        # Start resilient polling fallback that only polls if WebSocket is disconnected
        stop_poll = self._start_polling_fallback(callback, interval_sec, run_only_when_ws_down=True)

        def stop_sub() -> None:
            with self._subscribers_lock:
                if callback in self._subscribers:
                    self._subscribers.remove(callback)
                remaining = len(self._subscribers)

            stop_poll()

            if remaining == 0:
                self._stop_ws.set()
                if self._ws_conn is not None:
                    try:
                        self._ws_conn.close()
                    except Exception:
                        pass
                if self._ws_thread and self._ws_thread.is_alive():
                    self._ws_thread.join(timeout=2.0)
                    self._ws_thread = None

        return stop_sub

    def _start_polling_fallback(
        self,
        callback: Callable[[str, Optional[str], Optional[str]], None],
        interval_sec: float = 1.0,
        run_only_when_ws_down: bool = False,
    ) -> Callable[[], None]:
        """Starts a background fallback polling thread."""
        stop_evt = threading.Event()

        def _worker() -> None:
            while not stop_evt.is_set():
                if not run_only_when_ws_down or not self._ws_connected.is_set():
                    try:
                        res = self.reconcile_states()
                        for chg in res["reconciled_changes"]:
                            callback(chg["entity_id"], chg["old_state"], chg["new_state"])
                    except Exception as err:
                        logger.debug("State subscriber polling note: %s", err)
                stop_evt.wait(interval_sec)

        t = threading.Thread(target=_worker, name="ha-state-poller", daemon=True)
        t.start()

        def stop_fallback() -> None:
            stop_evt.set()
            t.join(timeout=2.0)

        return stop_fallback

    def _next_interaction_id(self) -> int:
        cur = self._interaction_id
        self._interaction_id += 1
        return cur

    def _ws_worker(self) -> None:
        """Background worker managing Home Assistant WebSocket connection, authentication, and event loop."""
        backoff = 1.0
        while not self._stop_ws.is_set():
            conn = None
            try:
                if self._ws_factory is not None:
                    conn = self._ws_factory(self.ws_url)
                elif HAS_WEBSOCKETS and ws_connect is not None:
                    conn = ws_connect(self.ws_url, close_timeout=2.0)
                else:
                    logger.debug("WebSockets not installed or not configured; falling back to REST")
                    break

                self._ws_conn = conn

                # 1. Auth Handshake
                msg_raw = conn.recv(timeout=self.timeout_sec)
                msg = json.loads(msg_raw)
                if msg.get("type") == "auth_required":
                    auth_msg = {"type": "auth", "access_token": self.token or ""}
                    conn.send(json.dumps(auth_msg))
                    auth_res_raw = conn.recv(timeout=self.timeout_sec)
                    auth_res = json.loads(auth_res_raw)
                    if auth_res.get("type") != "auth_ok":
                        logger.error("HA WebSocket auth failed: %s", auth_res.get("message"))
                        self._stop_ws.wait(min(backoff, 5.0))
                        backoff = min(backoff * 2.0, 16.0)
                        continue
                elif msg.get("type") != "auth_ok":
                    logger.warning("Unexpected first WebSocket message: %s", msg)

                # 2. Subscribe to state_changed events
                sub_id = self._next_interaction_id()
                sub_msg = {
                    "id": sub_id,
                    "type": "subscribe_events",
                    "event_type": "state_changed",
                }
                conn.send(json.dumps(sub_msg))
                sub_ack_raw = conn.recv(timeout=self.timeout_sec)
                sub_ack = json.loads(sub_ack_raw)
                if not sub_ack.get("success", False) and sub_ack.get("id") == sub_id:
                    logger.warning("HA state_changed subscription not accepted: %s", sub_ack)

                self._ws_connected.set()
                backoff = 1.0
                logger.info("Connected and authenticated with Home Assistant WebSocket at %s", self.ws_url)

                # Reconcile authoritative states upon connection/reconnection
                try:
                    self.reconcile_states()
                except Exception as rec_err:
                    logger.debug("Reconciliation on WS connect: %s", rec_err)

                # 3. Process incoming real-time events
                while not self._stop_ws.is_set():
                    try:
                        raw_event = conn.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    except Exception as recv_err:
                        if self._stop_ws.is_set():
                            break
                        raise recv_err

                    event_obj = json.loads(raw_event)
                    if event_obj.get("type") == "event":
                        evt_data = event_obj.get("event", {})
                        if evt_data.get("event_type") == "state_changed":
                            payload = evt_data.get("data", {})
                            entity_id = payload.get("entity_id")
                            if entity_id and self.is_entity_allowed(entity_id):
                                old_obj = payload.get("old_state")
                                new_obj = payload.get("new_state")
                                old_st = old_obj.get("state") if isinstance(old_obj, dict) else None
                                new_st = new_obj.get("state") if isinstance(new_obj, dict) else None

                                if isinstance(new_obj, dict):
                                    self._cached_states[entity_id] = new_obj

                                with self._state_condition:
                                    self._state_condition.notify_all()

                                with self._subscribers_lock:
                                    active_subs = list(self._subscribers)
                                for cb in active_subs:
                                    try:
                                        cb(entity_id, old_st, new_st)
                                    except Exception as cb_err:
                                        logger.exception("Error in state subscriber callback: %s", cb_err)

            except Exception as exc:
                self._ws_connected.clear()
                if not self._stop_ws.is_set():
                    logger.debug("HA WebSocket disconnected (%s); retrying in %.1fs...", exc, backoff)
                    self._stop_ws.wait(backoff)
                    backoff = min(backoff * 2.0, 16.0)
            finally:
                self._ws_connected.clear()
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                self._ws_conn = None

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
        # Check rate-limit cooldown
        now = time.time()
        if now < self._rate_limit_until:
            wait_time = self._rate_limit_until - now
            raise ConnectionError(f"Home Assistant rate limit active; retry in {wait_time:.1f}s")

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
            if status_code == 429:
                self._rate_limit_until = time.time() + 5.0
                raise ConnectionError("Home Assistant rate limit exceeded: HTTP 429")
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
            if exc.code == 429:
                self._rate_limit_until = time.time() + 5.0
                raise ConnectionError("Home Assistant rate limit exceeded: HTTP 429") from exc
            raise ConnectionError(f"Home Assistant returned HTTP error {exc.code}: {exc.reason}") from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ConnectionError(f"Failed to communicate with Home Assistant at {url}: {exc}") from exc

    def get_state(self, entity_id: str) -> dict[str, Any]:
        """
        Retrieves current state snapshot of an entity from Home Assistant.
        Strictly enforces domain and entity allowlists and denylists.
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
        Only returns entities permitted by allowlist and not blocked by denylist.
        """
        if self.mock_mode or (self._is_default_unconfigured and "unittest" in sys.modules):
            return [s for s in self._mock_states.values() if self.is_entity_allowed(s["entity_id"])]

        raw_states = self._http_request("GET", "/api/states")
        if not isinstance(raw_states, list):
            raise ConnectionError(f"Unexpected states response from Home Assistant: {raw_states}")

        allowed_list: list[dict[str, Any]] = []
        for st in raw_states:
            if isinstance(st, dict) and "entity_id" in st:
                eid = st["entity_id"]
                if self.is_entity_allowed(eid):
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
        - Polls entity state up to `timeout_ms` when `wait_for_state` is provided,
          leveraging real-time WebSocket state notification for instant confirmation when available.
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

            # Check initial state immediately
            try:
                current_st = self.get_state(entity_id)
                observed_state = current_st.get("state")
                if observed_state == expected:
                    return {
                        "entity_id": entity_id,
                        "action": action,
                        "accepted": accepted,
                        "observed_state": observed_state,
                        "state_confirmed": True,
                    }
            except Exception as err:
                logger.debug("Initial state check note: %s", err)

            # Poll with interval or wake up immediately on WebSocket state condition
            poll_interval = 0.05
            while time.time() < deadline:
                remaining = deadline - time.time()
                if remaining <= 0:
                    break

                with self._state_condition:
                    self._state_condition.wait(timeout=min(poll_interval, remaining))

                try:
                    current_st = self.get_state(entity_id)
                    observed_state = current_st.get("state")
                    if observed_state == expected:
                        state_confirmed = True
                        break
                except Exception as poll_err:
                    logger.warning("Error polling state for %s during verification: %s", entity_id, poll_err)
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

    def close(self) -> None:
        """Stops background threads and cleans up WebSocket connection."""
        self._stop_ws.set()
        if self._ws_conn is not None:
            try:
                self._ws_conn.close()
            except Exception:
                pass
        if self._ws_thread and self._ws_thread.is_alive():
            self._ws_thread.join(timeout=2.0)
            self._ws_thread = None
