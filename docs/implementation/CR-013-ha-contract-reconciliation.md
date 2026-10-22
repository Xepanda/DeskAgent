# CR-013: Reconcile HA Execute Schema with Connector Semantics

**Status:** A0 adjudicated; contract schema aligned with existing A4 behavior  
**Scope:** `ha.execute` request and result schemas only  
**Evidence level:** Level 2 (source review and schema/fixture validation)

## Finding

The `domain_ha.json` schema rejected request forms that the A4 connector already supports: omitted `expected_state`, omitted `timeout_ms`, `expected_attributes`, and `check_parameters`. It also required integer timeouts of at least 500 ms, while the connector accepts finite non-negative numeric values and caps values at 30000 ms. The connector defaults an omitted timeout to 2000 ms.

The result schema used `additionalProperties: false` but omitted `state_changed` and `physical_action_confirmed`, both of which the connector always returns. The latter is always `null`: an HA entity snapshot is not independent physical feedback.

## Adjudication

Align the schema with the already implemented connector contract. This is backward-compatible for valid callers: it admits additional forms the connector already handles and does not remove any accepted form. `expected_state` is optional because a caller may confirm attributes only. `expected_attributes` takes precedence over inference from recognized service parameters; `check_parameters` defaults to true. Omitting both state and attribute criteria means no positive confirmation criterion is requested, so the connector reports `state_confirmed: false` after its readback.

The timeout has inclusive bounds `0..30000` milliseconds and permits fractional values. Omission means 2000 ms. Zero permits an immediate observation attempt. The cap and finite-number check remain enforced by A4 at runtime; non-finite values are not valid JSON numbers.

`state_changed` is a best-effort comparison between pre-call and observed HA snapshots and may be null when unavailable or when no wait was requested. It does not imply that the requested state was confirmed. `physical_action_confirmed` is nullable and remains null until an independent physical feedback source exists.

## Validation

The contract validator now checks representative optional and boundary request forms, rejects out-of-range or wrongly typed timeout values and unknown fields, and validates a complete result containing the two previously omitted fields. No A4 production code or live Home Assistant instance was changed or exercised.
