# A4 implementation note: HA reconnect snapshot/event ordering

**Status:** BLOCKED — A0 decision; no ordering contract approved
**Owner:** A0 owns shared semantics; A4 owns connector implementation and protocol PoC
**Scope:** Reconnect cache freshness and event ordering only. CR-016 payload schema and event meanings remain unchanged.
**Evidence:** Level 1 (official HA WebSocket/REST documentation) + Level 2 (DeskAgent source review). No live HA instance was accessed.

## Decision

Do not approve timestamp-based ordering or a rule that treats a REST snapshot as globally ordered with WebSocket events. Do not add a private sequence number or change the CR-016 payload.

The current connector subscribes to WebSocket `state_changed`, marks the socket connected, then calls REST `reconcile_states()` before it drains queued WebSocket frames (`host/desk_agent/connectors/ha/ha_connector.py`, `_ws_worker`). A queued event is applied to `_cached_states` without comparing its `old_state` with the cached snapshot. Therefore a stale queued transition or removal can regress/remove a newer snapshot. The existing `is_ws_connected` flag also does not represent whether reconnect reconciliation has completed.

The official WebSocket API documents `time_fired` on event frames, but does not document a cross-transport ordering guarantee against a separate REST `/api/states` request. The event and state timestamps are not approved as a cross-channel sequence. Comparing `old_state` with the cache can detect some duplicates or conflicts, but cannot determine the correct order for every valid intervening transition. On conflict, another REST read still does not order a later WebSocket event against that read. Consequently no safe, complete rule for the current `subscribe_events` + REST design can be approved while preserving CR-016 semantics.

Home Assistant documents `subscribe_entities` as sending all states the client may read first, then only changes, in a compact format. This is a promising single-WebSocket baseline-and-update path and does not require changing the CR-016 payload. However, the public documentation does not specify an explicit completion marker for the initial-state portion. A4 must establish from the pinned/supported HA protocol implementation or a versioned official guarantee how the client knows the baseline is complete, including empty initial state sets, authorization filtering, and entity add/remove behavior. “First message”, “quiet period”, or REST response completion must not be treated as a completion marker without evidence.

## Gate to reopen approval

A4 should provide a source-backed protocol PoC proposal, without production edits, showing that a supported HA subscription can supply an unambiguous initial baseline and subsequent ordered changes on one WebSocket connection. It must account for the configured readable/allowed entity set and preserve existing CR-016 callback semantics. If no completion marker exists, A0 requires a different documented mechanism or explicit product acceptance of weaker semantics (for example, cache may remain stale/unknown and ambiguous events are withheld); A0 must approve that trade-off before implementation.

Once the protocol gate is met, A0 can approve the minimal rule: initialize cache only from the subscription's documented initial baseline; apply later updates in that same subscription's delivered order; mark freshness true only after a documented baseline-complete signal; mark stale on disconnect; do not compare REST and WebSocket timestamps or infer cross-transport order. If REST reconciliation remains necessary, it must not overwrite the live subscription cache unless a documented comparable version/order token exists.

## Required software acceptance tests after approval

- Reconnect receives an unambiguous initial baseline; freshness remains false until the documented completion signal and becomes true only afterward.
- Initial baseline followed by a state change for the same entity yields the final state and one CR-016-compatible transition.
- Initial baseline followed by entity removal, and initial absence followed by entity addition, produce the CR-016 removal/addition semantics exactly once.
- Multiple updates for one entity are applied in WebSocket delivery order, including attribute-only changes and removed attributes.
- Empty baseline, authorization-filtered entities, malformed frames, disconnect during baseline, and reconnect during queued updates fail closed and never claim fresh state prematurely.
- No REST/WebSocket timestamp comparison, inferred sequence, or private wire field is used.
- Retain existing unchanged-snapshot, attribute-only, add/remove, callback-failure, and reconnect tests.

These are proposed Level 2 tests only. They do not establish behavior against a live HA instance or prove physical device actions (Level 3/4).

## References

- [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/) — `subscribe_events` documents event `time_fired`; `subscribe_entities` documents initial readable states followed by changes, but no explicit initial-stream completion marker.
- [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/) — documents `/api/states`; no cross-transport ordering token is specified.
- [CR-016: Define HA Attribute Changes and Entity Removal Events](change-requests/CR-016-ha-state-snapshot-change-semantics.md) — approved payload and transition semantics; unchanged by this decision.
