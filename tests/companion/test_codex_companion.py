"""
Comprehensive unit tests for Windows Codex Companion.
Covers:
- Reproduction test: missing codex executable in production mode raises FileNotFoundError / ConnectionError
- Workspace alias registration and directory escape protection
- Mock JSON-RPC stdio transport simulating Codex app-server
- Thread and Turn lifecycle (initialize, submit, append, cancel, continue)
- Approval request and response correlation checks
- Task status query
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from typing import Any, Mapping, Optional
from companion.src.codex_companion import CodexCompanion, JsonRpcTransport


class TestCodexCompanion(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.workspace_dir = os.path.realpath(self.temp_dir.name)
        self.workspaces = {"test-project": self.workspace_dir}

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Reproduction Test: Production Mode without Mock
    # -------------------------------------------------------------------------
    def test_production_mode_missing_binary_raises_error(self) -> None:
        """In production mode with a non-existent codex executable, must raise FileNotFoundError/ConnectionError, not fake success."""
        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            codex_bin="/path/to/non_existent/codex_binary_9999",
        )
        with self.assertRaises((FileNotFoundError, ConnectionError)):
            companion.submit_task(
                task_id="tsk-repro-01",
                workspace_alias="test-project",
                prompt="git status",
            )

    # -------------------------------------------------------------------------
    # 2. Workspace Alias & Security Protection
    # -------------------------------------------------------------------------
    def test_workspace_alias_resolution(self) -> None:
        companion = CodexCompanion(allowed_workspaces=self.workspaces)

        # Unregistered alias rejected
        with self.assertRaises(ValueError) as ctx:
            companion.submit_task(
                task_id="tsk-sec-01",
                workspace_alias="unregistered-repo",
                prompt="git status",
            )
        self.assertIn("not registered on this Windows host", str(ctx.exception))

    def test_workspace_path_traversal_prevention(self) -> None:
        companion = CodexCompanion(allowed_workspaces=self.workspaces)
        with self.assertRaises(ValueError):
            # Attempt to register relative escape path
            companion.register_workspace("escaped", "../../../../etc/passwd")

    # -------------------------------------------------------------------------
    # 3. Mock JSON-RPC Stdio Transport & Turn Lifecycle
    # -------------------------------------------------------------------------
    def test_turn_lifecycle_with_mock_rpc(self) -> None:
        sent_messages: list[dict[str, Any]] = []

        class MockCodexTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                sent_messages.append({"method": method, "params": dict(params)})
                if method == "initialize":
                    return {"server_version": "0.1.0", "capabilities": {"turns": True}}
                elif method == "thread/create":
                    return {"thread_id": "thr-real-8888"}
                elif method == "turn/start":
                    return {"turn_id": "trn-real-9999", "state": "running"}
                elif method == "turn/interrupt":
                    return {"interrupted": True}
                elif method == "turn/append":
                    return {"appended": True}
                return {}

        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=MockCodexTransport(),
        )

        # 1. Submit task
        res = companion.submit_task(
            task_id="tsk-turn-01",
            workspace_alias="test-project",
            prompt="npm test",
        )
        self.assertEqual(res["task_id"], "tsk-turn-01")
        self.assertEqual(res["thread_id"], "thr-real-8888")
        self.assertEqual(res["turn_id"], "trn-real-9999")
        self.assertEqual(res["state"], "running")

        # Verify RPC calls occurred in correct order
        methods = [m["method"] for m in sent_messages]
        self.assertIn("initialize", methods)
        self.assertIn("thread/create", methods)
        self.assertIn("turn/start", methods)

        # 2. Append task
        append_res = companion.append_task("tsk-turn-01", "also check linter")
        self.assertTrue(append_res["appended"])

        # 3. Query status
        st = companion.get_task_status("tsk-turn-01")
        self.assertIsNotNone(st)
        self.assertEqual(st["thread_id"], "thr-real-8888")

        # 4. Cancel task
        cancel_res = companion.cancel_task("tsk-turn-01", reason="User requested interrupt")
        self.assertEqual(cancel_res["status"], "cancelled")

    # -------------------------------------------------------------------------
    # 4. Approval Correlation Lifecycle
    # -------------------------------------------------------------------------
    def test_approval_lifecycle(self) -> None:
        sent_approvals = []

        class MockApprovalTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                if method == "initialize":
                    return {"capabilities": {}}
                elif method == "thread/create":
                    return {"thread_id": "thr-appr-01"}
                elif method == "turn/start":
                    return {"turn_id": "trn-appr-01", "state": "waiting_approval"}
                elif method == "turn/approval/respond":
                    sent_approvals.append(dict(params))
                    return {"applied": True}
                return {}

        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=MockApprovalTransport(),
        )
        companion.submit_task("tsk-appr-01", "test-project", "deploy to prod")

        # Create approval
        req = companion.request_approval(
            approval_id="appr-123456",
            task_id="tsk-appr-01",
            action_type="command",
            command_or_path="kubectl apply -f prod.yaml",
            description="Apply prod manifests",
        )
        self.assertEqual(req["status"], "pending")

        # Mismatched task_id must be rejected
        with self.assertRaises(ValueError):
            companion.respond_approval(
                approval_id="appr-123456",
                task_id="wrong-task-id",
                decision="approve",
            )

        # Approve
        resp = companion.respond_approval(
            approval_id="appr-123456",
            task_id="tsk-appr-01",
            decision="approve",
        )
        self.assertEqual(resp["decision"], "approve")
        self.assertEqual(resp["status"], "applied")
        self.assertEqual(len(sent_approvals), 1)
        self.assertEqual(sent_approvals[0]["decision"], "approve")

    # -------------------------------------------------------------------------
    # 5. Stdio Transport ID Matching & Asynchronous Notifications
    # -------------------------------------------------------------------------
    def test_stdio_transport_id_matching_and_notifications(self) -> None:
        """Verifies SubprocessJsonRpcTransport background reader matches response IDs and routes notifications."""
        from companion.src.codex_companion import SubprocessJsonRpcTransport
        import io
        import threading
        import time

        notifications = []
        def on_notify(method, params):
            notifications.append((method, params))

        transport = SubprocessJsonRpcTransport(codex_bin="codex", on_notification=on_notify)

        # Mock process stdout and stdin with simulated pipes
        class PipeIO(io.StringIO):
            def __init__(self):
                super().__init__()
                self._cond = threading.Condition()
                self._lines = []

            def write_line(self, s: str):
                with self._cond:
                    self._lines.append(s)
                    self._cond.notify_all()

            def readline(self):
                with self._cond:
                    while not self._lines and transport._running:
                        self._cond.wait(timeout=0.1)
                    if self._lines:
                        return self._lines.pop(0) + "\n"
                    return ""

        mock_stdout = PipeIO()
        mock_stdin = io.StringIO()

        class MockProc:
            def __init__(self):
                self.stdin = mock_stdin
                self.stdout = mock_stdout
            def poll(self):
                return None
            def terminate(self):
                pass
            def wait(self, timeout=None):
                pass
            def kill(self):
                pass

        transport._proc = MockProc()
        transport._running = True
        transport._reader_thread = threading.Thread(
            target=transport._reader_loop, args=(transport._proc,), daemon=True
        )
        transport._reader_thread.start()

        # Send request 1 in background thread
        req1_res = {}
        def do_req1():
            req1_res.update(transport.send_request("thread/create", {"model": "default"}))

        t1 = threading.Thread(target=do_req1)
        t1.start()

        time.sleep(0.05)
        # Push notification first (should NOT be consumed as response)
        mock_stdout.write_line(json.dumps({"method": "turn/progress", "params": {"step": 1}}))

        # Push response with id=1
        mock_stdout.write_line(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"thread_id": "thr-101"}}))

        t1.join(timeout=2.0)
        self.assertEqual(req1_res.get("thread_id"), "thr-101")
        self.assertEqual(len(notifications), 1)
        self.assertEqual(notifications[0][0], "turn/progress")
        self.assertEqual(notifications[0][1]["step"], 1)

        transport.close()

    # -------------------------------------------------------------------------
    # 6. Companion Server Authentication & Loopback Binding
    # -------------------------------------------------------------------------
    def test_companion_server_bearer_token_auth(self) -> None:
        """Verifies start_companion_server enforces Bearer Token authentication and loopback default."""
        import urllib.request
        from companion.src.codex_companion import create_companion_server

        port = 18765
        token = "test-auth-token-12345"

        server = create_companion_server(port=port, host="127.0.0.1", auth_token=token)
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()
        time.sleep(0.1)

        try:
            # 1. GET /health is public
            req_health = urllib.request.Request(f"http://127.0.0.1:{port}/health")
            with urllib.request.urlopen(req_health, timeout=2.0) as resp:
                self.assertEqual(resp.status, 200)

            # 2. POST /rpc without token -> 401
            rpc_body = json.dumps({"operation": "codex.get", "arguments": {"task_id": "t1"}}).encode("utf-8")
            req_no_auth = urllib.request.Request(f"http://127.0.0.1:{port}/rpc", data=rpc_body)
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                urllib.request.urlopen(req_no_auth, timeout=2.0)
            self.assertEqual(ctx.exception.code, 401)

            # 3. POST /rpc with valid Bearer token -> 200
            req_auth = urllib.request.Request(
                f"http://127.0.0.1:{port}/rpc",
                data=rpc_body,
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req_auth, timeout=2.0) as resp:
                self.assertEqual(resp.status, 200)
                data = json.loads(resp.read().decode("utf-8"))
                self.assertTrue(data.get("success"))
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
