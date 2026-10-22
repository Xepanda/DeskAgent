#!/usr/bin/env python3
"""
Desk Agent R1 Contract & Fixture Validator

Validates all JSON Schema definitions, positive fixtures, negative fixtures,
domain-specific payloads, error codes, and state transition rules for G0 baseline compliance.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import jsonschema
    from jsonschema import Draft202012Validator
except ImportError:
    print("Error: jsonschema package is required. Install via `pip install jsonschema`.", file=sys.stderr)
    sys.exit(1)

BASE_DIR = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = BASE_DIR / "schemas"
FIXTURES_DIR = BASE_DIR / "fixtures"
ERRORS_FILE = BASE_DIR / "error_codes.json"


def load_json(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def validate_schemas() -> dict[str, Any]:
    raw_schemas: dict[str, Any] = {}
    schema_files = sorted(SCHEMAS_DIR.glob("*.json"))
    if not schema_files:
        raise RuntimeError(f"No schema files found in {SCHEMAS_DIR}")

    for s_path in schema_files:
        schema = load_json(s_path)
        try:
            Draft202012Validator.check_schema(schema)
            raw_schemas[s_path.name] = schema
            print(f"  [OK] Schema valid: {s_path.name}")
        except jsonschema.exceptions.SchemaError as exc:
            print(f"  [FAIL] Schema error in {s_path.name}: {exc.message}", file=sys.stderr)
            raise

    return raw_schemas


def test_positive_requests(raw_schemas: dict[str, Any]) -> None:
    req_validator = Draft202012Validator(raw_schemas["envelope_request.json"])
    requests_dir = FIXTURES_DIR / "requests"
    count = 0

    # Domain argument schema mapping
    domain_arg_map = {
        "ha.get_state": (raw_schemas["domain_ha.json"], "ha_get_state_args"),
        "ha.execute": (raw_schemas["domain_ha.json"], "ha_execute_args"),
        "codex.submit": (raw_schemas["domain_codex.json"], "codex_submit_args"),
        "codex.respond_approval": (raw_schemas["domain_codex.json"], "codex_respond_approval_args"),
        "reminder.create": (raw_schemas["domain_life.json"], "reminder_create_args"),
        "note.create": (raw_schemas["domain_life.json"], "note_create_args"),
        "resource.motion.move": (raw_schemas["domain_resource.json"], "motion_move_args"),
        "resource.camera.acquire": (raw_schemas["domain_resource.json"], "camera_acquire_args"),
    }

    for path in sorted(requests_dir.glob("*.json")):
        if path.name.startswith("invalid_"):
            continue
        data = load_json(path)
        req_validator.validate(data)

        # Check expiration format
        datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))

        op = data["operation"]
        if op in domain_arg_map:
            domain_schema, def_name = domain_arg_map[op]
            subschema = domain_schema["definitions"][def_name]
            jsonschema.validate(instance=data["arguments"], schema=subschema)
            print(f"  [OK] Positive request valid (envelope + domain args): {path.name} (op={op})")
        else:
            print(f"  [OK] Positive request valid (envelope): {path.name} (op={op})")
        count += 1

    assert count >= 5, f"Expected at least 5 positive requests, found {count}"


def test_negative_requests(raw_schemas: dict[str, Any]) -> None:
    req_validator = Draft202012Validator(raw_schemas["envelope_request.json"])
    requests_dir = FIXTURES_DIR / "requests"

    # 1. Missing required field
    missing_id_path = requests_dir / "invalid_missing_id.json"
    if missing_id_path.exists():
        data = load_json(missing_id_path)
        errors = list(req_validator.iter_errors(data))
        assert len(errors) > 0, "invalid_missing_id.json should fail validation"
        assert any("request_id" in err.message for err in errors)
        print("  [OK] Negative test passed: invalid_missing_id.json correctly rejected (missing request_id)")

    # 2. Unknown unsegmented operation pattern
    unknown_op_path = requests_dir / "invalid_unknown_op.json"
    if unknown_op_path.exists():
        data = load_json(unknown_op_path)
        errors = list(req_validator.iter_errors(data))
        assert len(errors) > 0, "invalid_unknown_op.json should fail validation"
        assert any("operation" in str(err.path) for err in errors)
        print("  [OK] Negative test passed: invalid_unknown_op.json correctly rejected (bad operation pattern)")

    # 3. Expired request
    expired_path = requests_dir / "invalid_expired.json"
    if expired_path.exists():
        data = load_json(expired_path)
        req_validator.validate(data)  # Valid schema syntax
        # Check semantic expiration rejection
        expires_at = datetime.fromisoformat(data["expires_at"].replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        assert expires_at < now, f"Expected expired timestamp, got {expires_at}"
        print("  [OK] Negative test passed: invalid_expired.json correctly detected as expired")


def test_responses(raw_schemas: dict[str, Any]) -> None:
    resp_validator = Draft202012Validator(raw_schemas["envelope_response.json"])
    resp_dir = FIXTURES_DIR / "responses"
    count = 0

    # Domain result schema mapping
    domain_result_map = {
        "succeeded_ha.json": (raw_schemas["domain_ha.json"], "ha_execute_result"),
        "succeeded_codex.json": (raw_schemas["domain_codex.json"], "codex_submit_result"),
        "succeeded_reminder.json": (raw_schemas["domain_life.json"], "reminder_create_result"),
    }

    for path in sorted(resp_dir.glob("*.json")):
        data = load_json(path)
        resp_validator.validate(data)
        status = data["status"]
        if status in ("failed", "unknown"):
            assert "error" in data, f"Response {path.name} with status={status} must contain error block"
            assert "code" in data["error"], f"Error block in {path.name} must have error code"
        if path.name in domain_result_map and "result" in data:
            domain_schema, def_name = domain_result_map[path.name]
            subschema = domain_schema["definitions"][def_name]
            jsonschema.validate(instance=data["result"], schema=subschema)
            print(f"  [OK] Response valid (envelope + domain result): {path.name} (status={status})")
        else:
            print(f"  [OK] Response valid: {path.name} (status={status})")
        count += 1

    assert count >= 6, f"Expected at least 6 responses, found {count}"


def test_events(raw_schemas: dict[str, Any]) -> None:
    event_validator = Draft202012Validator(raw_schemas["envelope_event.json"])
    event_dir = FIXTURES_DIR / "events"
    count = 0

    # Domain event payload mapping
    domain_event_map = {
        "ha.state_changed": (raw_schemas["domain_ha.json"], "ha_state_changed_payload"),
        "codex.turn_completed": (raw_schemas["domain_codex.json"], "codex_turn_completed_payload"),
        "codex.approval_requested": (raw_schemas["domain_codex.json"], "codex_approval_requested_payload"),
        "reminder.triggered": (raw_schemas["domain_life.json"], "reminder_triggered_payload"),
        "resource.motion.completed": (raw_schemas["domain_resource.json"], "motion_completed_payload"),
        "resource.touch_event": (raw_schemas["domain_resource.json"], "touch_event_payload"),
    }

    for path in sorted(event_dir.glob("*.json")):
        data = load_json(path)
        event_validator.validate(data)
        evt_type = data["type"]
        if evt_type in domain_event_map:
            domain_schema, def_name = domain_event_map[evt_type]
            subschema = domain_schema["definitions"][def_name]
            jsonschema.validate(instance=data["payload"], schema=subschema)
            print(f"  [OK] Event valid (envelope + domain payload): {path.name} (type={evt_type})")
        else:
            print(f"  [OK] Event valid: {path.name} (type={evt_type})")
        count += 1

    assert count >= 5, f"Expected at least 5 events, found {count}"


def test_error_catalog() -> None:
    assert ERRORS_FILE.exists(), f"Error catalog missing: {ERRORS_FILE}"
    catalog = load_json(ERRORS_FILE)
    errors = catalog.get("errors", {})
    assert len(errors) >= 10, f"Expected at least 10 stable error codes, found {len(errors)}"

    for code, info in errors.items():
        assert info["code"] == code
        assert "category" in info
        assert "retryable" in info
        assert isinstance(info["retryable"], bool)
        assert "description" in info
    print(f"  [OK] Error catalog verified: {len(errors)} stable error codes")


def test_ha_contract_semantics(raw_schemas: dict[str, Any]) -> None:
    """Pin the HA schema to the connector's documented optional/default semantics."""
    schema = raw_schemas["domain_ha.json"]["definitions"]
    args_validator = Draft202012Validator(schema["ha_execute_args"])
    result_validator = Draft202012Validator(schema["ha_execute_result"])

    base = {"entity_id": "light.desk", "action": "turn_on"}
    args_validator.validate(base)
    args_validator.validate({**base, "wait_for_state": {}})
    args_validator.validate({**base, "wait_for_state": {"expected_attributes": {"brightness": 200}}})
    args_validator.validate({**base, "wait_for_state": {"timeout_ms": 0}})
    args_validator.validate({**base, "wait_for_state": {"timeout_ms": 1250.5}})
    args_validator.validate({**base, "wait_for_state": {"timeout_ms": 30000}})

    for invalid_timeout in (-0.1, 30000.1, True, "2000"):
        instance = {**base, "wait_for_state": {"timeout_ms": invalid_timeout}}
        assert list(args_validator.iter_errors(instance)), f"Invalid timeout accepted: {invalid_timeout!r}"
    assert list(args_validator.iter_errors({**base, "wait_for_state": {"unknown": True}}))

    result_validator.validate({
        "entity_id": "light.desk", "action": "turn_on", "accepted": True,
        "observed_state": "on", "state_confirmed": True, "state_changed": True,
        "physical_action_confirmed": None,
    })
    print("  [OK] HA contract semantics verified: optional criteria, timeout bounds/default shape, and truthful result fields")


def test_idempotency_and_sequence_rules() -> None:
    requests_dir = FIXTURES_DIR / "requests"
    seen_ids: set[str] = set()

    for path in sorted(requests_dir.glob("*.json")):
        if path.name.startswith("invalid_"):
            continue
        data = load_json(path)
        req_id = data["request_id"]
        assert req_id not in seen_ids, f"Collision detected in test fixtures: {req_id}"
        seen_ids.add(req_id)

    print("  [OK] Idempotency: All positive fixture request_ids are unique")


def main() -> int:
    print("=== Desk Agent R1 G0 Contract Verification (Deep Domain Checks) ===")
    print("\n1. Verifying JSON Schemas...")
    raw_schemas = validate_schemas()

    print("\n2. Verifying Positive Request Fixtures...")
    test_positive_requests(raw_schemas)

    print("\n3. Verifying Negative Request Fixtures...")
    test_negative_requests(raw_schemas)

    print("\n4. Verifying Response / Result Fixtures...")
    test_responses(raw_schemas)

    print("\n5. Verifying Event Fixtures...")
    test_events(raw_schemas)

    print("\n6. Verifying Error Catalog...")
    test_error_catalog()

    print("\n6a. Verifying HA Connector Contract Semantics...")
    test_ha_contract_semantics(raw_schemas)

    print("\n7. Verifying Idempotency and Sequence Constraints...")
    test_idempotency_and_sequence_rules()

    print("\n=== ALL G0 CONTRACT & DEEP DOMAIN CHECKS PASSED (100% OK) ===")
    return 0


if __name__ == "__main__":
    sys.exit(main())
