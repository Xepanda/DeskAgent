# A4 implementation note: HA reconnect snapshot/event ordering

**Status:** PROPOSED — A0 review only; not an approved contract or implementation instruction
**Owner:** A4 implementation; A0 must approve any shared event freshness semantics
**Evidence:** Level 1 (official HA WebSocket documentation) + Level 2 (source/test review)

## Finding

Approved [CR-016](change-requests/CR-016-ha-state-snapshot-change-semantics.md) defines *what changes* produce `ha.state_changed`: state or complete attributes change, entity addition/removal, and the meaning of `attributes`/`new_state: null`. It does **not** define ordering or freshness between a WebSocket event and a REST snapshot, nor does it require a generation, sequence, or timestamp comparison.

The risk is real in the current reconnect path (`host/desk_agent/connectors/ha/ha_connector.py`, `_ws_worker`): A4 subscribes to `state_changed`, marks the socket connected, then calls REST `reconcile_states()` before it begins receiving queued WebSocket frames. A change event can therefore be waiting on the socket while the REST snapshot already reflects a later state. The snapshot updates `_cached_states`; the queued event is subsequently applied without comparing its version to the cache and can regress or remove that newer cached state. The polling worker also pauses as soon as `_ws_connected` is set. Current reconnect test covers an outage change observed by the reconnect snapshot, but not a queued pre-snapshot event that conflicts with the snapshot.

The official HA WebSocket API documents event subscriptions and their `time_fired` field, and documents `subscribe_entities` as sending initial states followed by changes on that subscription. It does not document a cross-transport ordering/freshness guarantee between a WebSocket event and a separate REST `/api/states` response. Thus this risk is not closed by relying on WebSocket frame order alone.

## Proposed minimum safe behavior (requires A0 approval)

1. Never let an event known to be older than the cached HA state overwrite or remove that state. Use HA-provided state/event timestamps only where both are present and comparable; do not invent local sequence numbers or assume REST/WS response ordering.
2. If ordering cannot be established (including missing/malformed timestamps), treat the event as an invalidation and reconcile from an authoritative current-state snapshot before publishing a state transition or declaring the cache current. Preserve the existing CR-016 payload shape.
3. Keep connection health separate from state freshness: authenticated + subscribed may report WebSocket-connected, but do not claim the entity cache is reconciled until the reconnect baseline is complete.
4. A4 should assess whether HA's documented `subscribe_entities` initial-state-plus-changes stream can provide the needed baseline/order without adding a private protocol. Any change to the shared callback/payload contract requires a separate A0 decision.

## Required software acceptance tests if approved

- Queue an older state-change frame while the reconnect snapshot returns a newer state; after the frame is drained, cache and subscriber-visible state must remain at the newer value.
- Queue an old removal while the snapshot contains the re-created entity; the entity must remain present.
- Queue a change newer than the snapshot; it must be applied once and delivered with the CR-016 full-attributes semantics.
- Missing/invalid timestamps must cause a fresh authoritative reconciliation before a transition is published; no guessed ordering.
- Assert cache-freshness/readiness is false during reconnect reconciliation and true only after baseline completion; disconnect returns it to stale.
- Retain the existing unchanged-snapshot, attribute-only, add/remove, callback failure, and reconnect tests.

These are proposed L2 tests only. They do not establish behavior against a live HA instance or prove physical device actions (L3/L4).

## References

- [Home Assistant WebSocket API: event subscriptions and `subscribe_entities`](https://contributors.home-assistant.io/core/internals/websocket-api/)
- [Home Assistant REST API](https://contributors.home-assistant.io/core/internals/rest-api/)
