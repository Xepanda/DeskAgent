# Home Assistant live read-only validation

This runbook validates the existing A4 connector against the configured Home Assistant instance without invoking any control service or changing an automation. Run it on the TaishanPi host where the DeskAgent production config and network route exist. Do not copy or print the access token.

**Status (2026-10-22):** The read-only procedure has been reviewed but not run against the live HA instance. The Windows development host has no production config and no reachable HA endpoint; execute it on TaishanPi using its existing config. The probe is limited to REST state discovery and WebSocket state-event observation and does not validate control-service execution.

## Preconditions

- Use the active DeskAgent config path (the systemd unit defaults to `/etc/desk-agent/config.json`).
- Confirm the HA base URL is reachable from TaishanPi and `home_assistant.access_token` is configured. Check only whether a token is present; never include its value in command output, logs, screenshots, or evidence.
- Confirm `home_assistant.allowed_domains` and any `allowed_entities` scope include only entities approved for observation.
- Keep the normal Host process stopped while running the probe, or use the already-running process and its existing status/log interfaces. Do not start a second Host against the production SQLite database.

## Read-only probe

Run from the deployed DeskAgent source root with the deployed virtualenv. The probe reads config in-process, creates the normal connector with the configured allowlists, fetches the allowed state snapshot, and listens for state events. It never calls `execute()` or an HA service endpoint. Output entity IDs and state values only for entities explicitly approved for this validation; suppress HTTP headers, config objects, and exception representations that may contain credentials.

```python
import json
import logging
import threading
from pathlib import Path

from host.desk_agent.connectors.ha.ha_connector import HAConnector

logging.basicConfig(level=logging.WARNING)
cfg = json.loads(Path("/etc/desk-agent/config.json").read_text(encoding="utf-8"))
ha_cfg = cfg.get("home_assistant", {})
if not ha_cfg.get("access_token") or ha_cfg["access_token"].startswith("REPLACE_"):
    raise SystemExit("BLOCKED: HA access token is not configured")

connector = HAConnector(
    base_url=ha_cfg.get("base_url", "http://homeassistant.local:8123"),
    token=ha_cfg["access_token"],
    allowed_domains=ha_cfg.get("allowed_domains"),
    allowed_entities=ha_cfg.get("allowed_entities"),
    denied_entities=ha_cfg.get("denied_entities"),
    timeout_sec=5,
)
observed = threading.Event()

def on_state(entity_id, old_state, new_state):
    print(f"STATE_CHANGED {entity_id} {old_state!r}->{new_state!r}")
    observed.set()

try:
    states = connector.get_all_states()
    print(f"DISCOVERY_OK allowed_entity_count={len(states)}")
    stop_subscription = connector.subscribe_state_changes(on_state, interval_sec=2)
    if not connector._ws_connected.wait(timeout=15):
        raise SystemExit("BLOCKED: authenticated WebSocket subscription not established")
    print("EVENT_SUBSCRIPTION_OK authenticated=true state_changed_ack=true")
    print("READY: change one approved low-risk entity manually in the HA UI to verify event delivery")
    if not observed.wait(timeout=60):
        raise SystemExit("EVENT_NOT_OBSERVED: no approved entity changed during the observation window")
finally:
    if "stop_subscription" in locals():
        stop_subscription()
```

The script uses the connector's internal event flag only to observe subscription readiness; no service calls are made. If the production runtime already owns the WebSocket listener, do not run this standalone probe: use runtime logs/status and arrange the event observation through that existing instance instead.

## Evidence and pass criteria

- **REST discovery (L3 node-level):** `DISCOVERY_OK` with a nonzero count and a redacted sample of only approved entities/states. A zero count is not a pass; investigate HA permissions and allowlists.
- **WebSocket event channel (L3 node-level):** authenticated connection and positive `state_changed` subscription acknowledgement, followed by one callback matching a manual HA UI state change for an approved entity.
- Record timestamp, TaishanPi/DeskAgent build revision, redacted HA URL (host only if sensitive), counts, event entity ID, old/new state, and whether the Host remained healthy. Never record the token or full config.
- This validates entity discovery and observation only. It does not prove service-call safety, physical device actuation, offline/timeout behavior, or end-to-end voice control; those remain separate acceptance gates.

## Current blocker

No production config or HA token is present in the development workspace, and `homeassistant.local:8123` did not resolve/reach from the Windows development host during the 2026-10-22 check. Run this probe on TaishanPi after confirming its already-provisioned config; no credential needs to be sent to the development workspace.
