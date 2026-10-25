"""Integration tests for Camera Vision and Patrol RPC and HTTP endpoints."""

from __future__ import annotations

import base64
import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from host.desk_agent.core.models import RequestEnvelope, ResponseStatus
from host.desk_agent.main import (
    DeskAgentHostApp,
    ThreadedHTTPServer,
    make_request_handler,
)


class TestCameraVisionIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test_vision_integration.db"
        cfg_file = Path(cls.temp_dir.name) / "config.json"
        cfg_file.write_text(
            json.dumps({
                "storage": {"db_path": str(cls.db_path)},
                "camera": {"default_lease_ms": 5000, "patrol_settle_delay_s": 0.05},
                "auth_token": "test-secret-token",
            })
        )

        cls.app = DeskAgentHostApp(config_path=str(cfg_file))
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

    def setUp(self) -> None:
        if self.app.patrol_service.is_running():
            self.app.patrol_service.stop_patrol(timeout=1.0)
        self.auth_headers = {
            "Authorization": "Bearer test-secret-token",
            "Content-Type": "application/json",
        }
        self.valid_jpeg = b"\xff\xd8\x00\x01\x02\x03\xff\xd9"
        self.valid_b64 = base64.b64encode(self.valid_jpeg).decode("ascii")
        self.app.patrol_service.snapshot_fn = lambda: self.valid_jpeg

    def tearDown(self) -> None:
        if self.app.patrol_service.is_running():
            self.app.patrol_service.stop_patrol(timeout=1.0)

    def test_rpc_analyze_snapshot_with_base64(self) -> None:
        req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-vision-analyze-01",
            client_id="test-client",
            operation="camera.analyze_snapshot",
            arguments={"jpeg_base64": self.valid_b64, "prompt": "Identify objects"},
            expires_at="2030-01-01T00:00:00Z",
        )
        resp = self.app.router.dispatch(req)
        self.assertEqual(resp.status.value, "succeeded")
        self.assertTrue(resp.result["success"])
        self.assertEqual(resp.result["scene"], "desk_workspace")
        self.assertGreater(len(resp.result["objects"]), 0)

    def test_rpc_analyze_snapshot_invalid_base64(self) -> None:
        req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-vision-analyze-02",
            client_id="test-client",
            operation="camera.analyze_snapshot",
            arguments={"jpeg_base64": "!!!not_valid_b64@@@"},
            expires_at="2030-01-01T00:00:00Z",
        )
        resp = self.app.router.dispatch(req)
        self.assertEqual(resp.status.value, "failed")
        self.assertEqual(resp.error["code"], "INVALID_ARGUMENTS")

    def test_rpc_patrol_lifecycle(self) -> None:
        # 1. Check initial status
        req_status = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-patrol-status-01",
            client_id="test-client",
            operation="camera.patrol.status",
            arguments={},
            expires_at="2030-01-01T00:00:00Z",
        )
        resp_status = self.app.router.dispatch(req_status)
        self.assertEqual(resp_status.status.value, "succeeded")
        self.assertFalse(resp_status.result["is_running"])

        # 2. Start patrol with 1 waypoint for quick run
        req_start = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-patrol-start-01",
            client_id="test-client",
            operation="camera.patrol.start",
            arguments={
                "waypoints": [{"id": "wp_center", "name": "中位", "pan": 90.0, "tilt": 120.0}],
                "duration_ms": 50,
            },
            expires_at="2030-01-01T00:00:00Z",
        )
        resp_start = self.app.router.dispatch(req_start)
        self.assertEqual(resp_start.status.value, "succeeded")
        self.assertEqual(resp_start.result["status"], "started")

        # Wait for completion
        for _ in range(50):
            if not self.app.patrol_service.is_running():
                break
            time.sleep(0.05)

        # 3. Check history
        req_hist = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-patrol-hist-01",
            client_id="test-client",
            operation="camera.patrol.history",
            arguments={"limit": 10},
            expires_at="2030-01-01T00:00:00Z",
        )
        resp_hist = self.app.router.dispatch(req_hist)
        self.assertEqual(resp_hist.status.value, "succeeded")
        reports = resp_hist.result["reports"]
        self.assertGreater(len(reports), 0)
        self.assertEqual(reports[-1]["status"], "completed")

    def test_http_analyze_endpoint(self) -> None:
        url = f"http://127.0.0.1:{self.port}/api/camera/analyze"
        body = json.dumps({"jpeg_base64": self.valid_b64}).encode("utf-8")
        req = Request(url, data=body, headers=self.auth_headers, method="POST")

        with urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "succeeded")
            self.assertTrue(data["result"]["success"])

    def test_http_patrol_endpoints(self) -> None:
        # GET /api/camera/patrol/status
        status_url = f"http://127.0.0.1:{self.port}/api/camera/patrol/status"
        req_get = Request(status_url, headers=self.auth_headers, method="GET")
        with urlopen(req_get) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("is_running", data)

        # GET /api/camera/patrol/history
        hist_url = f"http://127.0.0.1:{self.port}/api/camera/patrol/history"
        req_hist = Request(hist_url, headers=self.auth_headers, method="GET")
        with urlopen(req_hist) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertIn("reports", data)

        # POST /api/camera/patrol/start
        start_url = f"http://127.0.0.1:{self.port}/api/camera/patrol/start"
        start_body = json.dumps({
            "waypoints": [{"id": "wp1", "name": "W1", "pan": 90.0, "tilt": 120.0}],
            "duration_ms": 20,
        }).encode("utf-8")
        req_start = Request(start_url, data=start_body, headers=self.auth_headers, method="POST")
        with urlopen(req_start) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(data["status"], "succeeded")

        # Stop patrol
        self.app.patrol_service.stop_patrol(timeout=1.0)


if __name__ == "__main__":
    unittest.main()
