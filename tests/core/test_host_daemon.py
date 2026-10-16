"""
Unit test for Host Daemon App and HTTP endpoints.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
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
        cfg_file.write_text(json.dumps({"storage": {"db_path": str(cls.db_path)}}))

        # Initialize App
        cls.app = DeskAgentHostApp(config_path=str(cfg_file))

        # Start HTTP server on ephemeral port (8099)
        cls.port = 8099
        handler = make_request_handler(cls.app)
        cls.server = ThreadedHTTPServer(("127.0.0.1", cls.port), handler)
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

        # 2. Fetch live frame
        frame_req = Request(f"http://127.0.0.1:{self.port}/api/camera/frame?client_id=web-viewer-01")
        with urlopen(frame_req) as frame_resp:
            self.assertEqual(frame_resp.status, 200)
            self.assertEqual(frame_resp.headers.get("Content-Type"), "image/jpeg")
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

    def test_reminder_automatic_scheduler_delivery(self) -> None:
        """Verifies background ReminderScheduler automatically triggers ringing and outbox alert for due items."""
        # Create a reminder due 1 minute ago
        past_time = "2020-01-01T12:00:00Z"
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


if __name__ == "__main__":
    unittest.main()
