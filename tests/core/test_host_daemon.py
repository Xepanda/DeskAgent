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


if __name__ == "__main__":
    unittest.main()
