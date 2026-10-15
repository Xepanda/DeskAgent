"""
End-to-end integration and automated verification suite for Desk Agent R1.
Tests cross-module contracts, router dispatching, Life service persistence,
HA execution with state confirmation, Codex task lifecycle, and Camera lease arbitration.
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from host.desk_agent.core.models import RequestEnvelope, ResponseEnvelope, ResponseStatus
from host.desk_agent.core.router import Router
from host.desk_agent.services.life_service import LifeService
from host.desk_agent.connectors.ha.ha_connector import HAConnector
from companion.src.codex_companion import CodexCompanion, JsonRpcTransport
from host.desk_agent.camera.camera_service import CameraViewerService


class E2EMockCodexTransport(JsonRpcTransport):
    def send_request(self, method: str, params: dict) -> dict:
        if method == "initialize":
            return {"serverInfo": {"name": "MockCodexServer", "version": "1.0.0"}}
        elif method == "thread/start":
            return {"thread_id": "thr_e2e_001"}
        elif method == "turn/start":
            return {"turn_id": "turn_e2e_001", "state": "running"}
        elif method == "turn/approval/respond":
            return {"status": "applied"}
        return {}


class TestDeskAgentE2E(unittest.TestCase):
    def setUp(self) -> None:
        self.router = Router()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_desk_agent.db"
        self.life_service = LifeService(self.db_path)
        self.ha_connector = HAConnector()
        self.codex_companion = CodexCompanion(
            allowed_workspaces={"epanda-desk-agent": os.path.realpath(self.temp_dir.name)},
            transport=E2EMockCodexTransport(),
        )
        self.camera_service = CameraViewerService()

        # Register router handlers
        self.router.register("ha.get_state", self._handle_ha_get_state)
        self.router.register("ha.execute", self._handle_ha_execute)
        self.router.register("reminder.create", self._handle_reminder_create)
        self.router.register("codex.submit", self._handle_codex_submit)
        self.router.register("codex.respond_approval", self._handle_codex_approval)

    def tearDown(self) -> None:
        self.life_service.close()
        self.temp_dir.cleanup()

    def _handle_ha_get_state(self, req: RequestEnvelope) -> ResponseEnvelope:
        state = self.ha_connector.get_state(req.arguments["entity_id"])
        return ResponseEnvelope.create_success(req.request_id, state)

    def _handle_ha_execute(self, req: RequestEnvelope) -> ResponseEnvelope:
        res = self.ha_connector.execute(
            entity_id=req.arguments["entity_id"],
            action=req.arguments["action"],
            parameters=req.arguments.get("parameters"),
            wait_for_state=req.arguments.get("wait_for_state"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_reminder_create(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        res = self.life_service.create_reminder(
            reminder_id=args.get("reminder_id", "rem-test-01"),
            title=args["title"],
            due_at=args["due_at"],
            timezone_str=args.get("timezone", "Asia/Shanghai"),
            repeat_rule=args.get("repeat_rule"),
            priority=args.get("priority", "normal"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_codex_submit(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        res = self.codex_companion.submit_task(
            task_id=args.get("task_id", req.request_id),
            workspace_alias=args["workspace_alias"],
            prompt=args["prompt"],
            model=args.get("model"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_codex_approval(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        res = self.codex_companion.respond_approval(
            approval_id=args["approval_id"],
            task_id=args["task_id"],
            decision=args["decision"],
            reason=args.get("reason"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def test_router_idempotency_and_expiration(self) -> None:
        # Test expired request rejection
        expired_req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-exp-01",
            operation="ha.get_state",
            arguments={"entity_id": "light.desk"},
            expires_at="2020-01-01T00:00:00Z",
        )
        resp = self.router.dispatch(expired_req)
        self.assertEqual(resp.status, ResponseStatus.FAILED)
        self.assertEqual(resp.error["code"], "EXPIRED")

        # Test valid request and deduplication caching
        valid_req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-valid-01",
            operation="ha.get_state",
            arguments={"entity_id": "light.desk"},
            expires_at="2030-01-01T00:00:00Z",
        )
        resp1 = self.router.dispatch(valid_req)
        self.assertEqual(resp1.status, ResponseStatus.SUCCEEDED)

        # Repeated dispatch should return cached response
        resp2 = self.router.dispatch(valid_req)
        self.assertEqual(resp2.status, ResponseStatus.SUCCEEDED)
        self.assertEqual(resp1.timestamp, resp2.timestamp)

    def test_ha_execution_and_confirmation(self) -> None:
        exec_req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-ha-turnon-01",
            operation="ha.execute",
            arguments={
                "entity_id": "light.living_room_ceiling",
                "action": "turn_on",
                "wait_for_state": {"expected_state": "on", "timeout_ms": 1000},
            },
            expires_at="2030-01-01T00:00:00Z",
        )
        resp = self.router.dispatch(exec_req)
        self.assertEqual(resp.status, ResponseStatus.SUCCEEDED)
        self.assertTrue(resp.result["state_confirmed"])
        self.assertEqual(resp.result["observed_state"], "on")

    def test_life_service_persistence_and_recovery(self) -> None:
        # Create reminder
        self.life_service.create_reminder(
            reminder_id="rem-standup-01",
            title="Standup meeting",
            due_at="2026-10-12T09:30:00Z",
        )

        # Simulate service restart by connecting with new instance to same SQLite DB
        restarted_service = LifeService(self.db_path)
        item = restarted_service.get_reminder("rem-standup-01")
        self.assertIsNotNone(item)
        self.assertEqual(item["title"], "Standup meeting")
        self.assertEqual(item["status"], "scheduled")

        # Snooze reminder (verifying snooze does not complete the reminder)
        snooze_res = restarted_service.snooze_reminder("rem-standup-01", snooze_minutes=10)
        self.assertEqual(snooze_res["status"], "snoozed")
        item_snoozed = restarted_service.get_reminder("rem-standup-01")
        self.assertEqual(item_snoozed["status"], "snoozed")

        # Complete reminder
        restarted_service.complete_reminder("rem-standup-01")
        item_done = restarted_service.get_reminder("rem-standup-01")
        self.assertEqual(item_done["status"], "completed")
        restarted_service.close()

    def test_codex_companion_task_and_approval_flow(self) -> None:
        # Submit task
        sub_req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-codex-01",
            operation="codex.submit",
            arguments={
                "workspace_alias": "epanda-desk-agent",
                "prompt": "Run integration tests",
                "task_id": "tsk-test-88",
            },
            expires_at="2030-01-01T00:00:00Z",
        )
        resp = self.router.dispatch(sub_req)
        self.assertEqual(resp.status, ResponseStatus.SUCCEEDED)
        self.assertEqual(resp.result["state"], "running")

        # Create approval requirement
        appr_obj = self.codex_companion.request_approval(
            approval_id="appr-88",
            task_id="tsk-test-88",
            action_type="command",
            command_or_path="pytest",
            description="Run test suite",
        )
        self.assertEqual(appr_obj["status"], "pending")

        # Respond approval
        appr_req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-codex-appr-01",
            operation="codex.respond_approval",
            arguments={
                "approval_id": "appr-88",
                "task_id": "tsk-test-88",
                "decision": "approve",
            },
            expires_at="2030-01-01T00:00:00Z",
        )
        resp_appr = self.router.dispatch(appr_req)
        self.assertEqual(resp_appr.status, ResponseStatus.SUCCEEDED)
        self.assertEqual(resp_appr.result["status"], "applied")

    def test_camera_service_lease_and_stale_detection(self) -> None:
        # Acquire lease
        lease1 = self.camera_service.acquire_lease("client-web-01", lease_ms=5000)
        self.assertTrue(lease1["granted"])

        # Second client attempting to acquire should be denied (RESOURCE_BUSY)
        lease2 = self.camera_service.acquire_lease("client-web-02", lease_ms=5000)
        self.assertFalse(lease2["granted"])
        self.assertEqual(lease2["reason"], "RESOURCE_BUSY")

        # Push frame
        test_frame = b"\xff\xd8\xff\xe0...test_jpeg...\xff\xd9"
        self.camera_service.push_frame(test_frame)

        # Get frame
        frame = self.camera_service.get_latest_frame("client-web-01")
        self.assertIsNotNone(frame)
        self.assertEqual(frame["jpeg_data"], test_frame)
        self.assertFalse(frame["is_stale"])

        # Release lease
        self.assertTrue(self.camera_service.release_lease("client-web-01"))
        self.assertFalse(self.camera_service.is_lease_valid())

    def test_firmware_to_host_tool_call_flow(self) -> None:
        """
        Verifies end-to-end tool call dispatch flow:
        Simulated firmware envelope arrives -> Host Router routes -> Service processes -> Dynamic response returned.
        Also verifies reminder creation followed by due occurrence evaluation and ringing outbox dispatch.
        """
        # 1. Firmware requests reminder creation via Router
        req_rem = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-fw-rem-01",
            operation="reminder.create",
            arguments={
                "reminder_id": "rem-fw-01",
                "title": "Drink warm water",
                "due_at": "2026-10-15T09:00:00Z",
                "priority": "high",
            },
            expires_at="2030-01-01T00:00:00Z",
        )
        resp_rem = self.router.dispatch(req_rem)
        self.assertEqual(resp_rem.status, ResponseStatus.SUCCEEDED)
        self.assertEqual(resp_rem.result["reminder_id"], "rem-fw-01")

        # 2. Verify reminder is retrieved and occurrence is generated
        due_items = self.life_service.get_due_occurrences("2026-10-15T09:30:00Z")
        self.assertEqual(len(due_items), 1)
        occ = due_items[0]
        self.assertEqual(occ["reminder_id"], "rem-fw-01")

        # 3. Mark occurrence ringing and queue proactive notification outbox message
        self.life_service.mark_occurrence_ringing(occ["occurrence_id"])
        self.life_service.queue_outbox_message(
            topic="reminder.alert",
            payload={"occurrence_id": occ["occurrence_id"], "reminder_id": "rem-fw-01", "title": "Drink warm water"},
        )
        outbox = self.life_service.get_pending_outbox_messages(limit=5)
        self.assertEqual(len(outbox), 1)
        self.assertEqual(outbox[0]["topic"], "reminder.alert")
        self.assertEqual(outbox[0]["payload"]["reminder_id"], "rem-fw-01")


if __name__ == "__main__":
    unittest.main()
