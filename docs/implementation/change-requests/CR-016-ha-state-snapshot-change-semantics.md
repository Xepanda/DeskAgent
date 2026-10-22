# CR-016: Define HA Attribute Changes and Entity Removal Events

**Status:** APPROVED — A0 compatibility-minimal contract decision
**Owner:** A0 owns this contract decision; A4 owns connector implementation
**Scope:** HA state-change event semantics and connector subscriber/reconciliation behavior
**Evidence level:** Level 2 (source, schema, fixture, and test review only)

## Finding

The frozen `ha.state_changed` payload does not fully describe the behavior already present in A4:

- `contracts/schemas/domain_ha.json` lines 90–101 require `new_state` to be a string, while allowing nullable `old_state`. The connector emits `new_state: null` for an entity removed from the authoritative `/api/states` snapshot (`host/desk_agent/connectors/ha/ha_connector.py` lines 281–291), and removes the cached entity on a WebSocket event whose `new_state` is absent (lines 472–480).
- The schema's optional `attributes` object (lines 94–100) has no defined old/new meaning. The sample event in `contracts/fixtures/events/ha_state_changed.json` lines 8–17 includes it, but does not state whether it is the new snapshot or a delta.
- `reconcile_states()` compares only `state` when deciding whether to report an existing entity's change (connector lines 271–280), even though it replaces the cached full state object. Attribute-only differences therefore update the cache silently.
- WebSocket dispatch extracts only old/new state strings and invokes a three-argument callback (connector lines 468–489). The Host stores only those state strings and a timestamp (`host/desk_agent/main.py` lines 220–229). An HA `state_changed` event caused only by attributes can consequently be delivered as an indistinguishable `old_state == new_state` event without the changed attributes.
- Existing tests cover state transitions and entity deletion in reconciliation (`tests/connectors/test_ha_connector.py` lines 298–331), but not an attribute-only change. WebSocket event tests likewise cover state transitions only (lines 598–615).

CR-013 covers `ha.execute` request/result semantics only (`docs/implementation/CR-013-ha-contract-reconciliation.md` lines 1–23); it does not adjudicate event attributes, reconciliation, or entity deletion.

## Approved contract decision

Keep the event type `ha.state_changed`. Define `payload.attributes` as the complete post-change attribute snapshot for a present entity, not a delta. Permit `new_state: null` only to signal entity removal; in that case `attributes` is omitted. `old_state: null` continues to mean no prior cached state was available, not removal.

An event is due when either the state string or the complete attributes snapshot changes, when a previously unseen entity appears, or when an entity disappears. For removal, emit the last known old state and `new_state: null`. For attribute-only changes, emit equal old/new state values plus the new full `attributes` snapshot. Preserve current event envelope fields and avoid adding a second event type.

No `old_attributes` or `new_attributes` fields are added. This preserves the existing payload shape and gives the existing `attributes` field one precise meaning. The schema and fixtures encode the approved semantics.

## A4 implementation and test requirements

1. Compare complete normalized attributes as well as state during WebSocket delivery and snapshot reconciliation; do not emit events for identical snapshots.
2. Deliver removal as `new_state: null`, clear the cached entity, and avoid repeating the same removal on subsequent polls.
3. Ensure Host event records preserve the agreed attributes and removal semantics.
4. Add connector tests for attributes-only WebSocket change, attributes-only polling/reconnect reconciliation, unchanged attributes, entity add/remove, and WebSocket removal. The shared schema validator must accept both the attributes-only and removal fixtures and reject invalid attribute payload types.
5. Run the focused HA connector, HA event schema/fixture validator, and Host event query tests. These are software-only checks and do not constitute live HA or physical-device verification.

## Limits

No production code, tests, real Home Assistant instance, credentials, or hardware were accessed or changed for this CR. Current findings establish a source-level contract mismatch only; runtime delivery and physical outcomes remain unverified.
