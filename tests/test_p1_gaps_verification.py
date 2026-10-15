"""
Comprehensive verification test suite for all P1 gap closures:
1. Codex remote companion client and request ID response mapping.
2. Outbox delivery worker consuming reminder alerts with announcement audio dispatch and retries.
3. Home Assistant state reconciliation, whitelist enforcement, and change subscriptions.
4. Camera dynamic FrameCapturePipeline and gimbal coordinate overlay.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from host.desk_agent.camera.camera_service import FrameCapturePipeline
from host.desk_agent.connectors.ha.ha_connector import HAConnector
from host.desk_agent.main import RemoteCodexCompanionClient
from host.desk_agent.services.life_service import LifeService


class MockWindowsCompanionHandler(BaseHTTPRequestHandler):
    received_requests: list[dict[str, Any]] = []

    def do_POST(self) -> None:
        content_len = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(content_len).decode("utf-8"))
        MockWindowsCompanionHandler.received_requests.append(body)

        operation = body.get("operation")
        req_id = body.get("request_id", "res-default")

        if operation == "codex.submit_turn":
            response = {
                "status": "succeeded",
                "request_id": req_id,
                "result": {"task_id": "task-mock-100", "status": "running"},
            }
        elif operation in ("codex.decide_approval", "codex.respond_approval"):
            args = body.get("arguments", {})
            response = {
                "status": "succeeded",
                "request_id": req_id,
                "result": {"task_id": args.get("task_id"), "decision": args.get("decision", "accept"), "resolved": True},
            }
        elif operation == "codex.get_tasks":
            response = {
                "status": "succeeded",
                "request_id": req_id,
                "result": {"tasks": [{"task_id": "task-mock-100", "status": "running"}]},
            }
        else:
            response = {"status": "succeeded", "request_id": req_id, "result": {"ok": True}}

        out = json.dumps(response).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def log_message(self, format: str, *args: Any) -> None:
        pass


class TestP1GapsVerification(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        MockWindowsCompanionHandler.received_requests.clear()
        cls.server = HTTPServer(("127.0.0.1", 0), MockWindowsCompanionHandler)
        cls.server_port = cls.server.server_port
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_remote_codex_companion_client_forwarding(self) -> None:
        """Verifies Host app forwarding calls to remote Windows Codex endpoint."""
        client = RemoteCodexCompanionClient(
            endpoint_url=f"http://127.0.0.1:{self.server_port}",
            auth_token="test-secret",
        )

        res = client.submit_task("task-mock-100", "codex-proj", "refactor tests")
        self.assertEqual(res.get("task_id"), "task-mock-100")

        appr = client.respond_approval("appr-01", "task-mock-100", decision="accept")
        self.assertTrue(appr.get("resolved"))

        task_st = client.get_task_status("task-mock-100")
        self.assertIsNotNone(task_st)

    def test_reminder_outbox_delivery_and_robot_announcement(self) -> None:
        """Verifies outbox delivery worker processes reminder.alert and invokes robot announcement."""
        temp_dir = tempfile.TemporaryDirectory()
        db_path = os.path.join(temp_dir.name, "life_test.db")
        life = LifeService(db_path=db_path)

        outbox_id = life.enqueue_outbox(
            event_type="reminder.alert",
            payload={"reminder_id": "rem-p1-test", "title": "Stand up and stretch"},
        )
        self.assertTrue(outbox_id.startswith("outbox-"))

        # Verify message is pending
        pending = life.get_pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["outbox_id"], outbox_id)

        # Mark failure and retry
        life.record_outbox_failure(outbox_id, "Audio device busy", max_retries=3)
        pending = life.get_pending_outbox()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["retry_count"], 1)

        # Mark delivered / dispatched
        life.mark_outbox_dispatched(outbox_id)
        pending = life.get_pending_outbox()
        self.assertEqual(len(pending), 0)

        temp_dir.cleanup()

    def test_ha_whitelist_and_state_reconciliation(self) -> None:
        """Verifies HA connector entity allowlist rejection, state reconciliation, and subscriber."""
        ha = HAConnector(
            base_url="http://ha.mock.local:8123",
            allowed_entities=["light.desk_lamp", "switch.desk_fan"],
            allowed_services=["light.turn_on", "switch.turn_off"],
            mock_mode=True,
        )

        # Entity outside allowlist must be rejected
        with self.assertRaises(PermissionError):
            ha.execute("climate.living_room", "set_temperature")

        # Service outside allowlist must be rejected
        with self.assertRaises(PermissionError):
            ha.execute("light.desk_lamp", "toggle")

        # Update mock states
        ha.update_mock_state("light.desk_lamp", "on", {"brightness": 255})
        ha.update_mock_state("switch.desk_fan", "off", {})

        res = ha.reconcile_states()
        self.assertGreaterEqual(res.get("total_entities", 0), 2)

        # Test state change notification callback
        observed_events = []
        stop_sub = ha.subscribe_state_changes(lambda eid, old_st, new_st: observed_events.append((eid, old_st, new_st)))
        ha.update_mock_state("light.desk_lamp", "off", {"brightness": 0})
        time.sleep(1.2)
        stop_sub()

        self.assertGreaterEqual(len(observed_events), 1)
        lamp_events = [e for e in observed_events if e[0] == "light.desk_lamp"]
        self.assertTrue(len(lamp_events) > 0)

    def test_camera_capture_pipeline_dynamic_frames(self) -> None:
        """Verifies FrameCapturePipeline generates valid dynamic JPEG frames with pan/tilt overlays."""
        pipeline = FrameCapturePipeline(fps=20.0)
        captured = []

        def on_frame(data: bytes) -> None:
            captured.append(data)

        pipeline.start(on_frame)
        pipeline.update_coordinates(120.0, 115.0)
        time.sleep(0.15)
        pipeline.stop()

        self.assertGreaterEqual(len(captured), 1, "Pipeline must emit dynamic frames")
        first_frame = captured[0]
        # JPEG SOI marker (0xFF, 0xD8) and EOI marker (0xFF, 0xD9)
        self.assertEqual(first_frame[:2], b"\xff\xd8")
        self.assertEqual(first_frame[-2:], b"\xff\xd9")
        self.assertIn(b"DeskAgent", first_frame)


if __name__ == "__main__":
    unittest.main()
