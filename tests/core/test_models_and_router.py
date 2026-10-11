"""
Unit tests for Desk Agent R1 Core models and router.
"""

from __future__ import annotations

import unittest
from host.desk_agent.core.models import RequestEnvelope, ResponseEnvelope, EventEnvelope, ResponseStatus
from host.desk_agent.core.router import Router


class TestCoreModelsAndRouter(unittest.TestCase):
    def test_request_envelope_valid(self) -> None:
        req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-unit-01",
            operation="ha.get_state",
            arguments={"entity_id": "light.desk"},
            expires_at="2030-01-01T00:00:00Z",
        )
        self.assertEqual(req.operation, "ha.get_state")
        d = req.to_dict()
        self.assertEqual(d["request_id"], "req-unit-01")
        req2 = RequestEnvelope.from_dict(d)
        self.assertEqual(req2.request_id, req.request_id)

    def test_response_envelope_helpers(self) -> None:
        succ = ResponseEnvelope.create_success("req-unit-02", {"status": "ok"})
        self.assertEqual(succ.status, ResponseStatus.SUCCEEDED)
        self.assertIsNone(succ.error)

        fail = ResponseEnvelope.create_error("req-unit-03", "INVALID_ARGUMENT", "Invalid param", retryable=False)
        self.assertEqual(fail.status, ResponseStatus.FAILED)
        self.assertEqual(fail.error["code"], "INVALID_ARGUMENT")
        self.assertFalse(fail.error["retryable"])

    def test_event_envelope(self) -> None:
        evt = EventEnvelope(
            schema_version="1.0.0",
            event_id="evt-unit-01",
            sequence=1,
            sequence_scope="reminder",
            type="reminder.triggered",
            timestamp="2026-10-12T00:00:00Z",
            payload={"reminder_id": "rem-01", "title": "Meeting"},
        )
        d = evt.to_dict()
        self.assertEqual(d["type"], "reminder.triggered")

    def test_router_unknown_operation(self) -> None:
        router = Router()
        req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-unit-04",
            operation="unknown.operation",
            arguments={},
            expires_at="2030-01-01T00:00:00Z",
        )
        resp = router.dispatch(req)
        self.assertEqual(resp.status, ResponseStatus.FAILED)
        self.assertEqual(resp.error["code"], "UNKNOWN_OPERATION")


if __name__ == "__main__":
    unittest.main()
