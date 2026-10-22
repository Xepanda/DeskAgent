# A4 implementation note: HA reconnect snapshot/event ordering

**Status:** BLOCKED — A0 decision; target HA Core version is not locked/verified
**Owner:** A0 owns shared semantics; A4 owns connector implementation and protocol PoC
**Scope:** Reconnect cache freshness and event ordering only. CR-016 payload schema and event meanings remain unchanged.
**Evidence:** Level 1 (official HA WebSocket/REST documentation and A4 review of HA Core development source) + Level 2 (DeskAgent source review). No live HA instance was accessed.

## Decision

Do not approve timestamp-based ordering or a rule that treats a REST snapshot as globally ordered with WebSocket events. Do not add a private sequence number or change the CR-016 payload.

The current connector subscribes to WebSocket `state_changed`, marks the socket connected, then calls REST `reconcile_states()` before it drains queued WebSocket frames (`host/desk_agent/connectors/ha/ha_connector.py`, `_ws_worker`). A queued event is applied to `_cached_states` without comparing its `old_state` with the cached snapshot. Therefore a stale queued transition or removal can regress/remove a newer snapshot. The existing `is_ws_connected` flag also does not represent whether reconnect reconciliation has completed.

The official WebSocket API documents `time_fired` on event frames, but does not document a cross-transport ordering guarantee against a separate REST `/api/states` request. The event and state timestamps are not approved as a cross-channel sequence. Comparing `old_state` with the cache can detect some duplicates or conflicts, but cannot determine the correct order for every valid intervening transition. On conflict, another REST read still does not order a later WebSocket event against that read. Consequently no safe, complete rule for the current `subscribe_events` + REST design can be approved while preserving CR-016 semantics.

### A4 Level 1 source review

A4 reviewed the Home Assistant Core development source for `subscribe_entities` and reported this sequence: register the state listener, send the subscription result, then emit one event on that subscription containing the complete initial `a` baseline; an empty baseline is represented by `a: {}`. Subsequent `a`/`c`/`r` updates are delivered for that same subscription ID in WebSocket queue order. This provides a candidate same-stream baseline boundary without changing CR-016 payload semantics.

This is a source-level finding for the reviewed development revision, not a guarantee for the user's deployed HA Core. The deployment version is not currently recorded or verified. Therefore A0 does not approve treating “first event after result” as a baseline-complete marker in production until A4 identifies the target Core version and confirms the same behavior in that exact version (including empty initial states and access-filtered entities). The implementation must also validate that messages belong to the active subscription ID and fail closed if the expected baseline event is absent or malformed.

## Gate to reopen approval

A4 must record the target HA Core version (or deployment-supported version range) and source commit/tag, then verify the reported initial-event boundary against that version. A4 should provide a software PoC proposal covering the target version's actual frames, configured readable/allowed entity set, subscription ID validation, and CR-016 callback semantics. Until that evidence exists, the contract remains blocked and the current mixed `subscribe_events` + REST behavior must not be represented as ordered.

If the exact-version gate passes, the candidate minimal rule for A0 approval is: initialize cache only from the validated first full-baseline event for the active `subscribe_entities` ID; apply subsequent `a`/`c`/`r` updates from that same subscription in delivery order; mark freshness true only after that baseline event; mark stale on disconnect; do not compare REST and WebSocket timestamps or infer cross-transport order. REST reconciliation must not overwrite the live subscription cache unless a documented comparable version/order token exists. This rule is not approved yet.

## Required software acceptance tests after approval

- Reconnect receives an unambiguous initial baseline; freshness remains false until the documented completion signal and becomes true only afterward.
- Verify against the pinned target HA Core revision that `result` precedes exactly one initial baseline event on the active subscription ID, including `a: {}` for an empty visible state set; do not infer completion from a quiet interval.
- Frames on another subscription ID, a missing initial baseline, malformed baseline, or disconnect before baseline completion must leave cache stale and readiness false.
- Initial baseline followed by a state change for the same entity yields the final state and one CR-016-compatible transition.
- Initial baseline followed by entity removal, and initial absence followed by entity addition, produce the CR-016 removal/addition semantics exactly once.
- Multiple updates for one entity are applied in WebSocket delivery order, including attribute-only changes and removed attributes.
- Empty baseline, authorization-filtered entities, malformed frames, disconnect during baseline, and reconnect during queued updates fail closed and never claim fresh state prematurely.
- No REST/WebSocket timestamp comparison, inferred sequence, or private wire field is used.
- Retain existing unchanged-snapshot, attribute-only, add/remove, callback-failure, and reconnect tests.

These are proposed Level 2 tests only. They do not establish behavior against a live HA instance or prove physical device actions (Level 3/4).

## References

- [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/) — documents `subscribe_entities` sending initial readable states followed by changes; it does not specify the initial event boundary or its availability across Core versions.
- [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/) — documents `/api/states`; no cross-transport ordering token is specified.
- [CR-016: Define HA Attribute Changes and Entity Removal Events](change-requests/CR-016-ha-state-snapshot-change-semantics.md) — approved payload and transition semantics; unchanged by this decision.
