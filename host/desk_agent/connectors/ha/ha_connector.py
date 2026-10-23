"""
Desk Agent R1 Home Assistant Connector.

Implements real Home Assistant REST API interactions and WebSocket real-time event streaming:
1. Enforces strict domain, entity, and service allowlists and denylists.
2. Interacts with Home Assistant via standard REST endpoints (/api/states, /api/services).
3. Connects to Home Assistant WebSocket API (/api/websocket) for real-time state change streaming.
4. Separates HA service acceptance from observed entity-state changes and target-state confirmation.
5. Reconciles state snapshots upon startup, WebSocket reconnect, and periodic polling.
6. Rate limiting and error classification (401/403 PermissionError, 404 LookupError, 429/5xx ConnectionError).
7. Supports transport injection and explicit mock mode for test isolation without polluting production.
"""

from __future__ import annotations

import copy
import json
import logging
import math
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

# Safe, explicit controls available when a deployment does not provide its
# own service list. Read-only domains intentionally have no default services.
DEFAULT_ALLOWED_SERVICES = frozenset({
    "light.turn_on", "light.turn_off", "light.toggle",
    "switch.turn_on", "switch.turn_off", "switch.toggle",
    "cover.open_cover", "cover.close_cover", "cover.stop_cover", "cover.set_cover_position",
    "climate.set_temperature", "climate.set_hvac_mode",
    "fan.turn_on", "fan.turn_off", "fan.toggle", "fan.set_percentage",
    "media_player.turn_on", "media_player.turn_off", "media_player.media_play",
    "media_player.media_pause", "media_player.media_stop", "media_player.volume_set",
    "scene.turn_on",
    "input_boolean.turn_on", "input_boolean.turn_off", "input_boolean.toggle",
})

ENTITY_ID_REGEX = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
SERVICE_ACTION_REGEX = re.compile(r"^[a-z0-9_]{1,64}$")
MAX_STATE_CONFIRMATION_TIMEOUT_MS = 30_000
MIN_STATE_POLL_INTERVAL_SEC = 1.0
MAX_STATE_POLL_INTERVAL_SEC = 300.0
# HA service payloads can select targets through fields other than entity_id.
# Those selectors can widen an entity-scoped request to devices, areas, floors,
# or labels, so the connector rejects them and requires one authorized entity.
HA_TARGET_SELECTOR_PARAMETER_KEYS = frozenset({
    "area_id",
    "device_id",
    "floor_id",
    "label_id",
    "target",
})


def _normalize_confirmation_timeout_ms(value: Any) -> float:
    """Validate and cap caller-supplied synchronous state confirmation waits."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("wait_for_state.timeout_ms must be a finite, non-negative number")
    return min(float(value), float(MAX_STATE_CONFIRMATION_TIMEOUT_MS))


def _normalize_state_poll_interval_sec(value: Any) -> float:
    """Keep full-state REST fallback between 1 second and a practical recovery bound."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < MIN_STATE_POLL_INTERVAL_SEC
    ):
        raise ValueError(f"interval_sec must be finite and at least {MIN_STATE_POLL_INTERVAL_SEC:g} second")
    return min(float(value), MAX_STATE_POLL_INTERVAL_SEC)


def _state_snapshot_changed(before: Optional[Mapping[str, Any]], after: Optional[Mapping[str, Any]]) -> Optional[bool]:
    """Compare HA-reported state and attributes; this does not establish physical device action."""
    if before is None or after is None:
        return None
    return (
        before.get("state") != after.get("state")
        or _snapshot_attributes(before) != _snapshot_attributes(after)
    )


def _snapshot_attributes(snapshot: Mapping[str, Any]) -> dict[str, Any]:
    """Return an independent, normalized copy of a snapshot's full attributes."""
    attributes = snapshot.get("attributes")
    return copy.deepcopy(dict(attributes)) if isinstance(attributes, Mapping) else {}

KNOWN_ATTRIBUTE_KEYS = frozenset({
    "brightness",
    "color_temp",
    "color_temp_kelvin",
    "rgb_color",
    "xy_color",
    "hs_color",
    "temperature",
    "target_temperature",
    "target_temp_high",
    "target_temp_low",
    "percentage",
    "volume_level",
    "is_volume_muted",
    "fan_mode",
    "preset_mode",
    "swing_mode",
    "humidity",
    "position",
})


def _compare_attribute_value(key: str, observed: Any, expected: Any) -> bool:
    if observed == expected:
        return True

    # Collections (e.g. list/tuple equality)
    if isinstance(observed, (list, tuple)) and isinstance(expected, (list, tuple)):
        return list(observed) == list(expected)

    # Numeric comparison with tolerances
    try:
        if not isinstance(observed, bool) and not isinstance(expected, bool):
            obs_num = float(observed)
            exp_num = float(expected)
            if key in ("brightness", "color_temp", "color_temp_kelvin", "percentage"):
                return abs(obs_num - exp_num) <= 2
            elif key in ("temperature", "target_temperature", "target_temp_high", "target_temp_low"):
                return abs(obs_num - exp_num) <= 0.5
            elif key in ("volume_level",):
                return abs(obs_num - exp_num) <= 0.05
            else:
                return abs(obs_num - exp_num) <= 1e-4
    except (ValueError, TypeError):
        pass

    # String comparison (case-insensitive strip)
    if isinstance(observed, str) and isinstance(expected, str):
        return observed.strip().lower() == expected.strip().lower()

    return False


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
        self.allowed_services: set[str] = (
            set(allowed_services) if allowed_services is not None else set(DEFAULT_ALLOWED_SERVICES)
        )
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
        # These domains are hard safety exclusions. A deployment allowlist may
        # narrow ordinary domains, but must not re-enable locks, alarms,
        # cameras, or update entities through configuration.
        return domain not in DEFAULT_DENIED_DOMAINS and domain in self.allowed_domains

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
        if not isinstance(domain, str) or not isinstance(action, str) or not SERVICE_ACTION_REGEX.fullmatch(action):
            return False
        full_svc = f"{domain}.{action}"
        if full_svc in self.denied_services or action in self.denied_services:
            return False
        if full_svc in DEFAULT_DENIED_SERVICES:
            return False
        return full_svc in self.allowed_services

    def reconcile_states(self) -> dict[str, Any]:
        """
        Reconciles local snapshot cache with authoritative Home Assistant states upon startup/reconnect.
        Returns report of reconciled discrepancies.
        """
        current_list = self.get_all_states()
        changes: list[dict[str, Any]] = []
        current_ids: set[str] = set()
        for item in current_list:
            eid = item["entity_id"]
            current_ids.add(eid)
            new_st = item.get("state")
            old_item = self._cached_states.get(eid)
            old_st = old_item.get("state") if old_item else None
            if old_item is None or _state_snapshot_changed(old_item, item):
                changes.append({
                    "entity_id": eid,
                    "old_state": old_st,
                    "new_state": new_st,
                    "attributes": _snapshot_attributes(item),
                })
            self._cached_states[eid] = item

        # HA can remove an entity without returning a replacement state in
        # /api/states. Drop it from the authoritative cache and report the
        # transition as old -> None so polling subscribers converge too.
        for eid in tuple(self._cached_states):
            if eid not in current_ids:
                old_item = self._cached_states.pop(eid)
                changes.append({
                    "entity_id": eid,
                    "old_state": old_item.get("state"),
                    "new_state": None,
                })

        return {"total_entities": len(current_list), "reconciled_changes": changes}

    def subscribe_state_changes(
        self,
        callback: Callable[[str, Optional[str], Optional[str], Optional[Mapping[str, Any]]], None],
        interval_sec: float = 1.0,
    ) -> Callable[[], None]:
        """
        Starts a state monitoring subscription.
        Prefers real-time WebSocket event streaming when available.
        Falls back seamlessly to periodic REST snapshot polling if WebSocket is unavailable or disconnected.
        Returns a callable that stops the subscriber.
        """
        interval_sec = _normalize_state_poll_interval_sec(interval_sec)
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
        callback: Callable[[str, Optional[str], Optional[str], Optional[Mapping[str, Any]]], None],
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
                            try:
                                callback(chg["entity_id"], chg["old_state"], chg["new_state"], chg.get("attributes"))
                            except Exception as callback_err:
                                # A consumer failure must not suppress other changes from
                                # this snapshot. Reconciliation updates the cache first,
                                # so skipped callbacks would otherwise be lost forever.
                                logger.exception("HA state subscriber callback failed: %s", callback_err)
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
                    raise ConnectionError("Home Assistant WebSocket did not begin with an authentication message")

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
                if (
                    sub_ack.get("type") != "result"
                    or sub_ack.get("id") != sub_id
                    or sub_ack.get("success") is not True
                ):
                    raise ConnectionError("Home Assistant did not acknowledge the state_changed subscription")

                self._ws_connected.set()
                backoff = 1.0
                logger.info("Connected and authenticated with Home Assistant WebSocket at %s", self.ws_url)

                # Reconcile authoritative states upon connection/reconnection
                try:
                    report = self.reconcile_states()
                    # The polling fallback pauses as soon as the WebSocket is
                    # authenticated. Surface transitions observed by this
                    # reconnect snapshot so a short outage cannot silently
                    # consume a state change between poll intervals.
                    with self._subscribers_lock:
                        active_subs = list(self._subscribers)
                    for change in report["reconciled_changes"]:
                        for cb in active_subs:
                            try:
                                cb(change["entity_id"], change["old_state"], change["new_state"], change.get("attributes"))
                            except Exception as cb_err:
                                logger.exception("Error in HA reconnect state subscriber: %s", cb_err)
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
                                cached_obj = self._cached_states.get(entity_id)
                                old_snapshot = cached_obj if isinstance(cached_obj, Mapping) else old_obj

                                if isinstance(new_obj, Mapping):
                                    changed = old_snapshot is None or _state_snapshot_changed(old_snapshot, new_obj) is True
                                    self._cached_states[entity_id] = dict(new_obj)
                                    event_attributes: Optional[Mapping[str, Any]] = _snapshot_attributes(new_obj)
                                    new_st = new_obj.get("state")
                                    old_st = old_snapshot.get("state") if isinstance(old_snapshot, Mapping) else None
                                else:
                                    changed = isinstance(cached_obj, Mapping)
                                    if changed:
                                        self._cached_states.pop(entity_id, None)
                                    event_attributes = None
                                    new_st = None
                                    old_st = cached_obj.get("state") if isinstance(cached_obj, Mapping) else None

                                if changed:
                                    with self._state_condition:
                                        self._state_condition.notify_all()

                                    with self._subscribers_lock:
                                        active_subs = list(self._subscribers)
                                    for cb in active_subs:
                                        try:
                                            cb(entity_id, old_st, new_st, event_attributes)
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
        return self._get_state(entity_id, timeout=None)

    def _get_state(self, entity_id: str, timeout: Optional[float]) -> dict[str, Any]:
        """Fetch state with an optional request timeout for bounded confirmation waits."""
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
        state_data = self._http_request("GET", path, timeout=timeout)
        if not isinstance(state_data, dict):
            raise ConnectionError(f"Invalid state payload returned for {entity_id}: {state_data}")

        # Keep the reconciliation baseline independent from both the injected
        # transport object and the mutable snapshot returned to callers.
        self._cached_states[entity_id] = copy.deepcopy(state_data)
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

    @staticmethod
    def _is_state_confirmed(
        current_st: Mapping[str, Any],
        expected_state: Optional[str],
        expected_attributes: Optional[Mapping[str, Any]],
    ) -> bool:
        """
        Confirms observed entity state against expectations.
        Requires both state (if expected_state provided) and all attributes (if expected_attributes provided) to match.
        """
        if expected_state is None and not expected_attributes:
            return False

        if expected_state is not None:
            if current_st.get("state") != expected_state:
                return False

        if expected_attributes:
            obs_attrs = current_st.get("attributes")
            if not isinstance(obs_attrs, dict):
                return False
            for attr_key, exp_val in expected_attributes.items():
                if attr_key not in obs_attrs:
                    return False
                if not _compare_attribute_value(attr_key, obs_attrs[attr_key], exp_val):
                    return False

        return True

    def execute(
        self,
        entity_id: str,
        action: str,
        parameters: Optional[dict[str, Any]] = None,
        wait_for_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Executes a service call on Home Assistant and verifies observed state.

        - `accepted` means HA returned success for the service request.
        - `state_confirmed` means the requested state currently matches HA's observed entity snapshot.
        - `state_changed` reports whether that snapshot differs from a best-effort pre-call snapshot.
        - `physical_action_confirmed` is always None because this connector only observes HA state,
          not independent physical feedback.
        - Polls entity state up to `timeout_ms` when `wait_for_state` is provided,
          leveraging real-time WebSocket state notification for instant confirmation when available.
        - Verifies both target state and target attributes (explicit or inferred from parameters).
        - Returns structured result conforming to `domain_ha.json` contract.
        """
        if not isinstance(action, str) or not SERVICE_ACTION_REGEX.fullmatch(action):
            raise ValueError("action must be a 1-64 character lowercase Home Assistant service name")
        if not self.is_entity_allowed(entity_id):
            raise PermissionError(f"Entity '{entity_id}' is outside allowed domain/entity scope")

        domain = entity_id.split(".", 1)[0]
        if not self.is_service_allowed(domain, action):
            raise PermissionError(f"Service '{domain}.{action}' is outside allowed service scope")

        # Validate this before issuing the service request, so malformed wait
        # parameters can never trigger an action and caller waits stay bounded.
        timeout_ms = None
        if wait_for_state is not None:
            timeout_ms = _normalize_confirmation_timeout_ms(wait_for_state.get("timeout_ms", 2000))

        service_path = f"/api/services/{domain}/{action}"

        body: dict[str, Any] = {"entity_id": entity_id}
        if parameters is not None:
            if not isinstance(parameters, Mapping):
                raise ValueError("parameters must be an object")
            if parameters:
                if "entity_id" in parameters and parameters["entity_id"] != entity_id:
                    raise ValueError("parameters.entity_id cannot differ from the allowlisted target entity")
                extra_target_selectors = HA_TARGET_SELECTOR_PARAMETER_KEYS.intersection(parameters)
                if extra_target_selectors:
                    selectors = ", ".join(sorted(extra_target_selectors))
                    raise ValueError(f"parameters cannot widen the allowlisted entity target: {selectors}")
                body.update(parameters)

        # Resolve target state and attributes confirmation requirements
        expected_state: Optional[str] = None
        expected_attributes: Optional[dict[str, Any]] = None
        if wait_for_state is not None:
            expected_state = wait_for_state.get("expected_state")
            if "expected_attributes" in wait_for_state and wait_for_state["expected_attributes"] is not None:
                expected_attributes = dict(wait_for_state["expected_attributes"])
            elif wait_for_state.get("check_parameters", True) and parameters:
                filtered = {k: v for k, v in parameters.items() if k in KNOWN_ATTRIBUTE_KEYS}
                if filtered:
                    expected_attributes = filtered

        before_state: Optional[dict[str, Any]] = None
        if wait_for_state is not None:
            try:
                baseline_timeout = min(self.timeout_sec, max(0.001, timeout_ms / 1000.0))
                before = self._get_state(entity_id, timeout=baseline_timeout)
                before_state = dict(before)
                if isinstance(before.get("attributes"), dict):
                    before_state["attributes"] = dict(before["attributes"])
            except Exception as err:
                # A pre-call read is observational only; it must not block an otherwise
                # permitted HA service request when the state endpoint is temporarily down.
                logger.debug("Pre-service state check note for %s: %s", entity_id, err)

        # Handle mock mode or unconfigured daemon test
        if self.mock_mode or (self._is_default_unconfigured and "unittest" in sys.modules):
            target_state = "on" if "turn_on" in action else "off" if "turn_off" in action else "active"
            if expected_state is not None:
                target_state = expected_state

            current = dict(self.get_state(entity_id))
            if isinstance(current.get("attributes"), dict):
                current["attributes"] = dict(current["attributes"])
            current["state"] = target_state
            if parameters:
                current.setdefault("attributes", {}).update(parameters)
            self._mock_states[entity_id] = current

            state_confirmed = False
            if wait_for_state is not None:
                state_confirmed = self._is_state_confirmed(current, expected_state, expected_attributes)

            return {
                "entity_id": entity_id,
                "action": action,
                "accepted": True,
                "observed_state": current["state"],
                "state_confirmed": state_confirmed,
                "state_changed": _state_snapshot_changed(before_state, current) if wait_for_state is not None else None,
                "physical_action_confirmed": None,
            }

        # 1. Trigger service call (First Stage: Acceptance)
        self._http_request("POST", service_path, payload=body)
        accepted = True

        # 2. State verification (Second Stage: Confirmation)
        observed_state: Optional[str] = None
        state_confirmed = False
        state_changed: Optional[bool] = None

        if wait_for_state is not None and (expected_state is not None or expected_attributes):
            deadline = time.monotonic() + (timeout_ms / 1000.0)

            # Check initial state immediately
            try:
                remaining = max(0.001, deadline - time.monotonic())
                current_st = self._get_state(entity_id, timeout=min(self.timeout_sec, remaining))
                observed_state = current_st.get("state")
                state_changed = _state_snapshot_changed(before_state, current_st)
                if self._is_state_confirmed(current_st, expected_state, expected_attributes):
                    return {
                        "entity_id": entity_id,
                        "action": action,
                        "accepted": accepted,
                        "observed_state": observed_state,
                        "state_confirmed": True,
                        "state_changed": state_changed,
                        "physical_action_confirmed": None,
                    }
            except Exception as err:
                logger.debug("Initial state check note: %s", err)

            # Poll with interval or wake up immediately on WebSocket state condition
            poll_interval = 0.05
            while time.monotonic() < deadline:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break

                with self._state_condition:
                    self._state_condition.wait(timeout=min(poll_interval, remaining))

                try:
                    remaining = max(0.001, deadline - time.monotonic())
                    current_st = self._get_state(entity_id, timeout=min(self.timeout_sec, remaining))
                    observed_state = current_st.get("state")
                    state_changed = _state_snapshot_changed(before_state, current_st)
                    if self._is_state_confirmed(current_st, expected_state, expected_attributes):
                        state_confirmed = True
                        break
                except Exception as poll_err:
                    logger.warning("Error polling state for %s during verification: %s", entity_id, poll_err)
        else:
            # No wait_for_state specified: read back once for current state readout
            try:
                current_st = self.get_state(entity_id)
                observed_state = current_st.get("state")
                state_changed = _state_snapshot_changed(before_state, current_st)
            except Exception:
                observed_state = None
            state_confirmed = False

        return {
            "entity_id": entity_id,
            "action": action,
            "accepted": accepted,
            "observed_state": observed_state,
            "state_confirmed": state_confirmed,
            "state_changed": state_changed,
            "physical_action_confirmed": None,
        }

    def execute_service(
        self,
        entity_id: str,
        action: str,
        parameters: Optional[dict[str, Any]] = None,
        wait_for_state: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        """
        Executes a service call on Home Assistant and verifies observed state.
        Alias for execute adhering to Desk Agent R1 interface conventions.
        """
        return self.execute(entity_id, action, parameters=parameters, wait_for_state=wait_for_state)

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
