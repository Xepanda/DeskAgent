from __future__ import annotations

import contextlib
import io
import json
import socket
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from host.desk_agent.core import hardware_cli


class _Server(ThreadingHTTPServer):
    def __init__(self, handler):
        super().__init__(("127.0.0.1", 0), handler)
        self.health = {"status": "healthy", "robot_connected": True}
        self.rpc_response = {"status": "accepted", "request_id": "req-test"}
        self.rpc_requests = []
        self.auth_headers = []
        self.delay = 0
        self.http_status = 200


def _handler_class():
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.server.auth_headers.append(self.headers.get("Authorization"))
            time.sleep(self.server.delay)
            body = json.dumps(self.server.health).encode()
            self.send_response(self.server.http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except OSError:
                pass  # Timeout test intentionally closes the client socket first.

        def do_POST(self):
            self.server.auth_headers.append(self.headers.get("Authorization"))
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            self.server.rpc_requests.append(payload)
            time.sleep(self.server.delay)
            body = json.dumps(self.server.rpc_response).encode()
            self.send_response(self.server.http_status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    return Handler


class TestHardwareCLI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = _Server(_handler_class())
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=2)

    def setUp(self):
        self.server.health = {"status": "healthy", "robot_connected": True}
        self.server.rpc_response = {"status": "accepted", "request_id": "req-test"}
        self.server.rpc_requests.clear()
        self.server.auth_headers.clear()
        self.server.delay = 0
        self.server.http_status = 200

    def invoke(self, *args, env=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = hardware_cli.main(["--host", self.base_url, *args], environ=env or {})
        return rc, stdout.getvalue(), stderr.getvalue()

    def test_status_reports_online_and_offline_without_failing(self):
        rc, out, _ = self.invoke("status")
        self.assertEqual(rc, 0)
        self.assertIn("robot_connected=true", out)
        self.server.health["robot_connected"] = False
        rc, out, _ = self.invoke("status")
        self.assertEqual(rc, 0)
        self.assertIn("robot_connected=false", out)

    def test_move_sends_standard_envelope_and_accepted_is_not_completion(self):
        rc, out, _ = self.invoke("move", "--pan", "42", "--tilt", "110", "--duration-ms", "700")
        self.assertEqual(rc, 0)
        self.assertIn("status=accepted", out)
        self.assertIn("physical completion not confirmed", out)
        request = self.server.rpc_requests[-1]
        self.assertEqual(request["schema_version"], "1.0.0")
        self.assertEqual(request["operation"], "resource.motion.move")
        self.assertEqual(request["arguments"], {"pan_deg": 42.0, "tilt_deg": 110.0, "duration_ms": 700})
        self.assertIn("request_id", request)
        self.assertIn("expires_at", request)

    def test_stop_defaults_to_emergency_and_can_select_normal_stop(self):
        rc, _, _ = self.invoke("stop")
        self.assertEqual(rc, 0)
        self.assertEqual(self.server.rpc_requests[-1]["operation"], "resource.motion.stop")
        self.assertEqual(self.server.rpc_requests[-1]["arguments"], {"emergency": True})
        rc, _, _ = self.invoke("stop", "--normal")
        self.assertEqual(rc, 0)
        self.assertEqual(self.server.rpc_requests[-1]["arguments"], {"emergency": False})

    def test_bearer_auth_is_sent_without_printing_token(self):
        token = "sensitive-cli-token"
        rc, out, err = self.invoke("move", "--pan", "90", "--tilt", "120", "--duration-ms", "500", "--token", token)
        self.assertEqual(rc, 0)
        self.assertEqual(self.server.auth_headers[-1], f"Bearer {token}")
        self.assertNotIn(token, out + err)

    def test_environment_token_is_used(self):
        rc, _, _ = self.invoke("status", env={"DESK_AGENT_AUTH_TOKEN": "env-secret"})
        self.assertEqual(rc, 0)
        self.assertEqual(self.server.auth_headers[-1], "Bearer env-secret")

    def test_failed_and_unknown_envelopes_are_nonzero_and_status_is_preserved(self):
        for status in ("failed", "unknown"):
            with self.subTest(status=status):
                self.server.rpc_response = {
                    "status": status,
                    "request_id": "req-test",
                    "error": {"code": "UPSTREAM_UNAVAILABLE", "message": "robot offline"},
                }
                rc, out, err = self.invoke("stop")
                self.assertNotEqual(rc, 0)
                self.assertIn(f"status={status}", out)
                self.assertIn("UPSTREAM_UNAVAILABLE", err)

    def test_succeeded_envelope_is_reported(self):
        self.server.rpc_response = {"status": "succeeded", "request_id": "req-test", "result": {"ok": True}}
        rc, out, _ = self.invoke("stop")
        self.assertEqual(rc, 0)
        self.assertIn("status=succeeded", out)

    def test_http_error_and_timeout_are_nonzero_and_diagnostic(self):
        self.server.http_status = 503
        rc, _, err = self.invoke("status")
        self.assertNotEqual(rc, 0)
        self.assertIn("HTTP 503", err)
        self.server.http_status = 200
        self.server.delay = 0.2
        rc, _, err = self.invoke("status", "--timeout", "0.02")
        self.assertNotEqual(rc, 0)
        self.assertIn("timed out", err.lower())

    def test_disconnected_host_is_nonzero_and_diagnostic(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            unused_port = listener.getsockname()[1]
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            rc = hardware_cli.main(
                ["--host", f"http://127.0.0.1:{unused_port}", "status"], environ={}
            )
        self.assertNotEqual(rc, 0)
        self.assertIn("Host request failed", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
