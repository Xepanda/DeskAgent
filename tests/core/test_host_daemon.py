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
        cls.app.life_service.close()
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


if __name__ == "__main__":
    unittest.main()
