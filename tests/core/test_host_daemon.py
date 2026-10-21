"""
Unit test for Host Daemon App and HTTP endpoints.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
import json

from host.desk_agent.main import DeskAgentHostApp, make_request_handler, ThreadedHTTPServer
from host.desk_agent.core.models import RequestEnvelope


class TestHostDaemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test_host_daemon.db"
        cfg_file = Path(cls.temp_dir.name) / "config.json"
        cfg_file.write_text(json.dumps({"storage": {"db_path": str(cls.db_path)}, "camera": {"allow_synthetic": True, "auto_start_pipeline": True}}))

        # Initialize App
        cls.app = DeskAgentHostApp(config_path=str(cfg_file))

        # Start HTTP server on an ephemeral port so concurrent suite runs
        # (and any other process) can never collide on a fixed port.
        handler = make_request_handler(cls.app)
        cls.server = ThreadedHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()
        cls.temp_dir.cleanup()

    def test_health_endpoint(self) -> None:
        with urlopen(f"http://127.0.0.1:{self.port}/health") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "healthy")

    def test_rpc_endpoint(self) -> None:
        req_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-test-01",
            "operation": "ha.get_state",
            "arguments": {"entity_id": "light.desk"},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(req_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["request_id"], "req-daemon-test-01")

    def test_rpc_reminder_flow(self) -> None:
        create_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-rem-01",
            "operation": "reminder.create",
            "arguments": {
                "reminder_id": "rem-daemon-01",
                "title": "Edge check",
                "due_at": "2026-10-15T12:00:00Z",
            },
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(create_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["result"]["reminder_id"], "rem-daemon-01")

        list_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-rem-02",
            "operation": "reminder.list",
            "arguments": {},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req2 = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(list_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req2) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertTrue(len(data["result"]["items"]) >= 1)

    def test_rpc_note_flow(self) -> None:
        note_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-note-01",
            "operation": "note.create",
            "arguments": {
                "note_id": "note-daemon-01",
                "title": "Meeting memo",
                "content": "Discuss architecture baseline",
                "tags": ["work"],
            },
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(note_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["result"]["note_id"], "note-daemon-01")

        get_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-note-02",
            "operation": "note.get",
            "arguments": {"note_id": "note-daemon-01"},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req2 = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(get_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req2) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["result"]["title"], "Meeting memo")

    def test_rpc_codex_routes(self) -> None:
        """Verifies codex.submit_turn and codex.get_task routes registered on Host."""
        submit_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-codex-01",
            "operation": "codex.submit_turn",
            "arguments": {
                "task_id": "tsk-host-codex-01",
                "workspace_alias": "epanda-desk-agent",
                "prompt": "run tests",
            },
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(submit_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["result"]["task_id"], "tsk-host-codex-01")

        # Query task status via Host
        get_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-codex-02",
            "operation": "codex.get_task",
            "arguments": {"task_id": "tsk-host-codex-01"},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req2 = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(get_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req2) as resp2:
            data2 = json.loads(resp2.read().decode())
            self.assertEqual(data2["status"], "succeeded")
            self.assertEqual(data2["result"]["task_id"], "tsk-host-codex-01")

    def test_camera_acquire_and_frame_endpoint(self) -> None:
        """Verifies acquiring camera lease enables /api/camera/frame JPEG stream retrieval."""
        # 1. Acquire lease
        acq_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-cam-acq-01",
            "operation": "resource.camera.acquire",
            "arguments": {"client_id": "web-viewer-01", "lease_duration_ms": 5000},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(acq_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertTrue(data["result"]["granted"])

        # Wait briefly for capture pipeline to produce first frame
        time.sleep(0.15)

        # 2. Fetch live frame
        frame_req = Request(f"http://127.0.0.1:{self.port}/api/camera/frame?client_id=web-viewer-01")
        with urlopen(frame_req) as frame_resp:
            self.assertEqual(frame_resp.status, 200)
            self.assertEqual(frame_resp.headers.get("Content-Type"), "image/jpeg")
            self.assertIsNotNone(frame_resp.headers.get("X-Frame-Source"))
            self.assertIn(frame_resp.headers.get("X-Frame-Hardware"), ("0", "1"))
            raw_jpeg = frame_resp.read()
            self.assertTrue(raw_jpeg.startswith(b"\xff\xd8"))

        # 3. Release lease
        rel_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-cam-rel-01",
            "operation": "resource.camera.release",
            "arguments": {"client_id": "web-viewer-01"},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req_rel = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(rel_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req_rel) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertTrue(data["result"]["released"])

    def test_snapshot_endpoint_retrieval(self) -> None:
        """Verifies GET /snapshot returns fresh JPEG with anti-caching and frame headers."""
        snap_req = Request(f"http://127.0.0.1:{self.port}/snapshot")
        with urlopen(snap_req) as snap_resp:
            self.assertEqual(snap_resp.status, 200)
            self.assertEqual(snap_resp.headers.get("Content-Type"), "image/jpeg")
            self.assertIn("no-cache", snap_resp.headers.get("Cache-Control", ""))
            self.assertIsNotNone(snap_resp.headers.get("X-Frame-Seq"))
            raw_jpeg = snap_resp.read()
            self.assertTrue(raw_jpeg.startswith(b"\xff\xd8"))

    def test_reminder_automatic_scheduler_delivery(self) -> None:
        """Verifies background ReminderScheduler automatically triggers ringing and outbox alert for due items."""
        # Create a reminder due 1 minute ago
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        create_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-sched-rem-01",
            "operation": "reminder.create",
            "arguments": {
                "reminder_id": "rem-auto-deliver-01",
                "title": "Immediate Alert",
                "due_at": past_time,
            },
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(create_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")

        # Allow background scheduler loop (polls every 1.0s) to detect and deliver
        time.sleep(1.2)

        # Verify alert was either dispatched by outbox delivery worker or pending in outbox (including with backoff)
        delivered_ids = [a.get("reminder_id") for a in self.app._delivered_alerts]
        pending_msgs = self.app.life_service.get_pending_outbox_messages(limit=10, include_delayed=True)
        pending_topics = [m["topic"] for m in pending_msgs]
        self.assertTrue(
            "rem-auto-deliver-01" in delivered_ids or "reminder.alert" in pending_topics,
            f"Alert must be processed: delivered={delivered_ids}, pending={pending_topics}",
        )

    def test_scheduler_loop_uses_atomic_trigger(self) -> None:
        """Verifies scheduler loop triggers due reminders via atomic trigger_occurrence_to_outbox."""
        called_occs = []
        original_trigger = getattr(self.app.life_service, "trigger_occurrence_to_outbox", None)

        def spy_trigger(occurrence_id, topic="reminder.alert", is_catchup=False):
            called_occs.append(occurrence_id)
            if original_trigger:
                return original_trigger(occurrence_id, topic=topic, is_catchup=is_catchup)
            return None

        self.app.life_service.trigger_occurrence_to_outbox = spy_trigger
        try:
            self.app.life_service.create_reminder(
                reminder_id="rem-atomic-sched-01",
                title="Atomic Sched Alert",
                due_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            )
            # Wait for scheduler loop to pick it up
            time.sleep(1.2)
            self.assertTrue(len(called_occs) >= 1, "Scheduler loop must call trigger_occurrence_to_outbox")
        finally:
            if original_trigger:
                self.app.life_service.trigger_occurrence_to_outbox = original_trigger
            elif hasattr(self.app.life_service, "trigger_occurrence_to_outbox"):
                delattr(self.app.life_service, "trigger_occurrence_to_outbox")

    def test_robot_tool_request_dispatched_to_router(self) -> None:
        """Verifies Host processes tool request from robot dialogue engine and sends response back."""
        # 1. Wrapped format
        wrapped_req = {
            "type": "tool.request",
            "data": {
                "schema_version": "1.0.0",
                "request_id": "req-tclaw-router-01",
                "operation": "reminder.create",
                "arguments": {
                    "reminder_id": "rem-tclaw-bridge-01",
                    "title": "Drink Coffee",
                    "due_at": "2026-10-16T16:00:00Z",
                },
                "expires_at": "2030-01-01T00:00:00Z",
            },
        }

        sent_responses: list[dict] = []
        if self.app.robot_adapter:
            original_send = self.app.robot_adapter.send_tool_response
            self.app.robot_adapter.send_tool_response = lambda resp: sent_responses.append(resp) or True
        else:
            class DummyAdapter:
                def send_tool_response(self, resp):
                    sent_responses.append(resp)
                    return True
            self.app.robot_adapter = DummyAdapter()
            original_send = None

        try:
            self.app._on_robot_tool_request(wrapped_req)
            self.assertEqual(len(sent_responses), 1)
            resp = sent_responses[0]
            self.assertEqual(resp["request_id"], "req-tclaw-router-01")
            self.assertEqual(resp["status"], "succeeded")
            self.assertEqual(resp["result"]["reminder_id"], "rem-tclaw-bridge-01")

            # Check reminder actually exists in life service
            rem = self.app.life_service.get_reminder("rem-tclaw-bridge-01")
            self.assertIsNotNone(rem)
            self.assertEqual(rem["title"], "Drink Coffee")

            # 2. Direct envelope format
            direct_req = {
                "schema_version": "1.0.0",
                "request_id": "req-tclaw-router-02",
                "operation": "ha.get_state",
                "arguments": {"entity_id": "light.living_room"},
                "expires_at": "2030-01-01T00:00:00Z",
            }
            self.app._on_robot_tool_request(direct_req)
            self.assertEqual(len(sent_responses), 2)
            resp2 = sent_responses[1]
            self.assertEqual(resp2["request_id"], "req-tclaw-router-02")
            self.assertEqual(resp2["status"], "succeeded")
        finally:
            if original_send and self.app.robot_adapter:
                self.app.robot_adapter.send_tool_response = original_send

    def test_motion_move_with_pan_deg_and_tilt_deg(self) -> None:
        """Verifies resource.motion.move accepts pan_deg/tilt_deg as well as pan/tilt and updates coordinates.

        A firmware ACK only proves the command was accepted, so the envelope must
        report ``accepted`` (never ``succeeded``) and must not carry the removed
        host-invented ``executed`` field (interfaces.md 6.1).
        """
        dispatched: list[tuple[float, float, int]] = []

        class MockRobotClient:
            is_connected = True

            def send_motion_move(self, pan: float, tilt: float, duration_ms: int = 500):
                dispatched.append((pan, tilt, duration_ms))
                return {
                    "status": "accepted",
                    "accepted": True,
                    "state_confirmed": False,
                    "error_code": None,
                    "retryable": False,
                    "pan": pan,
                    "tilt": tilt,
                    "duration_ms": duration_ms,
                }

        original_client = self.app._robot_client
        self.app._robot_client = MockRobotClient()
        try:
            # 1. Using pan_deg and tilt_deg
            move_payload = {
                "schema_version": "1.0.0",
                "request_id": "req-motion-deg-01",
                "operation": "resource.motion.move",
                "arguments": {
                    "pan_deg": 45.0,
                    "tilt_deg": 115.0,
                    "duration_ms": 300,
                },
                "expires_at": "2030-01-01T00:00:00Z",
            }
            req = Request(
                f"http://127.0.0.1:{self.port}/api/rpc",
                data=json.dumps(move_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                self.assertEqual(data["status"], "accepted")
                res = data["result"]
                self.assertNotIn("executed", res, "`executed` is not a contract field and must be removed")
                self.assertTrue(res["accepted"])
                self.assertFalse(res["state_confirmed"])
                self.assertEqual(res["pan"], 45.0)
                self.assertEqual(res["tilt"], 115.0)
                self.assertEqual(self.app.camera_service.pipeline._current_pan, 45.0)
                self.assertEqual(self.app.camera_service.pipeline._current_tilt, 115.0)

            # 2. Using fallback pan and tilt keys
            move_fallback = {
                "schema_version": "1.0.0",
                "request_id": "req-motion-fallback-01",
                "operation": "resource.motion.move",
                "arguments": {
                    "pan": 75.0,
                    "tilt": 125.0,
                    "duration_ms": 250,
                },
                "expires_at": "2030-01-01T00:00:00Z",
            }
            req2 = Request(
                f"http://127.0.0.1:{self.port}/api/rpc",
                data=json.dumps(move_fallback).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req2) as resp2:
                data2 = json.loads(resp2.read().decode())
                self.assertEqual(data2["status"], "accepted")
                res2 = data2["result"]
                self.assertEqual(res2["pan"], 75.0)
                self.assertEqual(res2["tilt"], 125.0)
                self.assertEqual(self.app.camera_service.pipeline._current_pan, 75.0)
                self.assertEqual(self.app.camera_service.pipeline._current_tilt, 125.0)
        finally:
            self.app._robot_client = original_client

    def test_motion_move_route_disconnected_is_not_reported_as_success(self) -> None:
        """Without a hardware link, resource.motion.move must not fabricate a success."""
        move_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-motion-move-nolink-01",
            "operation": "resource.motion.move",
            "arguments": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 200},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(move_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertNotEqual(data["status"], "succeeded")
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["error"]["code"], "UPSTREAM_UNAVAILABLE")
            self.assertTrue(data["error"]["retryable"])
            self.assertNotIn("result", data)
            self.assertEqual(data["error"]["details"]["reason"], "NO_PHYSICAL_ROBOT_CONNECTED")

    def test_motion_stop_route_disconnected(self) -> None:
        """Verifies resource.motion.stop reports UPSTREAM_UNAVAILABLE instead of a simulated success."""
        stop_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-motion-stop-01",
            "operation": "resource.motion.stop",
            "arguments": {"emergency": True},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(stop_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertNotEqual(data["status"], "succeeded")
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["error"]["code"], "UPSTREAM_UNAVAILABLE")
            self.assertTrue(data["error"]["retryable"])
            self.assertNotIn("result", data)
            self.assertEqual(data["error"]["details"]["reason"], "NO_PHYSICAL_ROBOT_CONNECTED")
            self.assertEqual(data["error"]["details"]["hardware_link"], "disconnected")

    def test_motion_stop_route_connected(self) -> None:
        """Verifies resource.motion.stop reports `accepted` (not a fabricated completion) when connected."""
        called_args = []

        class MockRobotClient:
            is_connected = True

            def send_motion_stop(self, emergency: bool = False):
                called_args.append(emergency)
                return {
                    "status": "accepted",
                    "accepted": True,
                    "state_confirmed": False,
                    "error_code": None,
                    "retryable": False,
                    "emergency": emergency,
                }

        original_client = self.app._robot_client
        self.app._robot_client = MockRobotClient()
        try:
            stop_payload = {
                "schema_version": "1.0.0",
                "request_id": "req-motion-stop-02",
                "operation": "resource.motion.stop",
                "arguments": {"emergency": False},
                "expires_at": "2030-01-01T00:00:00Z",
            }
            req = Request(
                f"http://127.0.0.1:{self.port}/api/rpc",
                data=json.dumps(stop_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                self.assertEqual(data["status"], "accepted")
                res = data["result"]
                self.assertTrue(res["accepted"])
                self.assertFalse(res["state_confirmed"])
                self.assertEqual(res["hardware_link"], "connected")
                self.assertNotIn("executed", res)
                self.assertEqual(called_args, [False])
        finally:
            self.app._robot_client = original_client

    def test_robot_adapter_send_motion_stop(self) -> None:
        """Verifies RobotAdapter.send_motion_stop sends ctrl.motion.stop and reports an accepted outcome."""
        from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter

        adapter = RobotAdapter(auto_pair=False)
        sent_cmds = []

        def mock_send(cmd, timeout=2.0):
            sent_cmds.append(cmd)
            return {"type": "sys.ack", "code": 0, "data": {"status": "accepted", "accepted": True}}

        adapter.send_command = mock_send
        res = adapter.send_motion_stop(emergency=True)

        self.assertEqual(len(sent_cmds), 1)
        self.assertEqual(sent_cmds[0]["type"], "ctrl.motion.stop")
        self.assertEqual(sent_cmds[0]["data"]["emergency"], True)
        self.assertEqual(res["status"], "accepted")
        self.assertTrue(res["accepted"])
        self.assertFalse(res["state_confirmed"])
        self.assertIsNone(res["error_code"])
        self.assertTrue(res["emergency"])


if __name__ == "__main__":
    unittest.main()

