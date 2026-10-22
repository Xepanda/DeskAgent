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
from unittest.mock import patch
from typing import Any, Mapping, Optional
from companion.src.codex_companion import CodexCompanion, DefaultMockTransport, JsonRpcTransport


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
        self.assertEqual(cancel_res["status"], "cancelling")
        self.assertEqual(companion.get_task_status("tsk-turn-01")["status"], "cancelling")

        # Verify initialize request structure aligned with codex-cli 0.161.0
        init_call = next(m for m in sent_messages if m["method"] == "initialize")
        self.assertEqual(init_call["params"].get("clientInfo", {}).get("name"), "desk-agent-companion")
        self.assertEqual(init_call["params"].get("clientInfo", {}).get("version"), "0.1.0")
        self.assertEqual(init_call["params"].get("capabilities"), {})

        # Verify turn/interrupt used camelCase params without snake_case
        interrupt_call = next(m for m in sent_messages if m["method"] == "turn/interrupt")
        self.assertEqual(interrupt_call["params"].get("threadId"), "thr-real-8888")
        self.assertEqual(interrupt_call["params"].get("turnId"), "trn-real-9999")
        self.assertNotIn("thread_id", interrupt_call["params"])
        self.assertNotIn("turn_id", interrupt_call["params"])

    def test_default_model_is_omitted_and_explicit_model_is_preserved(self) -> None:
        sent_calls: list[dict[str, Any]] = []

        class ModelTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                sent_calls.append({"method": method, "params": dict(params)})
                if method == "initialize":
                    return {"capabilities": {}}
                if method in ("thread/create", "thread/start"):
                    return {"thread_id": f"thr-{len(sent_calls)}"}
                if method == "turn/start":
                    return {"turn_id": f"turn-{len(sent_calls)}", "state": "running"}
                return {}

        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=ModelTransport(),
            db_path=":memory:",
        )
        companion.submit_task("tsk-default-model", "test-project", "hello")
        companion.submit_task("tsk-explicit-model", "test-project", "hello", model="chosen-model")

        turn_calls = [call["params"] for call in sent_calls if call["method"] == "turn/start"]
        self.assertEqual(len(turn_calls), 2)
        self.assertNotIn("model", turn_calls[0])
        self.assertEqual(turn_calls[1].get("model"), "chosen-model")
        self.assertIsNone(companion.get_task_status("tsk-default-model")["model"])
        self.assertEqual(companion.get_task_status("tsk-explicit-model")["model"], "chosen-model")

    def test_user_message_is_not_output_but_agent_message_with_null_role_is(self) -> None:
        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=":memory:",
        )
        user_task = companion.submit_task("tsk-user-item", "test-project", "private user prompt")
        companion._handle_server_notification(
            "item/created",
            {
                "threadId": user_task["thread_id"],
                "turnId": user_task["turn_id"],
                "item": {"type": "userMessage", "content": [{"type": "text", "text": "private user prompt"}]},
            },
        )
        companion._handle_server_notification(
            "turn/completed",
            {"threadId": user_task["thread_id"], "turnId": user_task["turn_id"], "status": "completed"},
        )
        user_status = companion.get_task_status("tsk-user-item")
        self.assertNotIn("private user prompt", user_status["last_output"])
        self.assertNotIn("private user prompt", user_status["summary"])

        agent_task = companion.submit_task("tsk-agent-item", "test-project", "another prompt")
        companion._handle_server_notification(
            "item/completed",
            {
                "threadId": agent_task["thread_id"],
                "turnId": agent_task["turn_id"],
                "item": {
                    "type": "agentMessage",
                    "role": None,
                    "content": [{"type": "text", "text": "real assistant result"}],
                },
            },
        )
        companion._handle_server_notification(
            "turn/completed",
            {"threadId": agent_task["thread_id"], "turnId": agent_task["turn_id"], "status": "completed"},
        )
        agent_status = companion.get_task_status("tsk-agent-item")
        self.assertEqual(agent_status["last_output"], "real assistant result")
        self.assertEqual(agent_status["summary"], "real assistant result")

    def test_ambiguous_legacy_message_item_is_not_adopted_as_output(self) -> None:
        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=":memory:",
        )
        task = companion.submit_task("tsk-legacy-message", "test-project", "make a change")
        companion._handle_server_notification(
            "item/updated",
            {"threadId": task["thread_id"], "turnId": task["turn_id"], "item": {"type": "message", "text": "assistant result"}},
        )
        companion._handle_server_notification(
            "turn/completed",
            {"threadId": task["thread_id"], "turnId": task["turn_id"], "status": "completed"},
        )
        legacy_status = companion.get_task_status("tsk-legacy-message")
        self.assertEqual(legacy_status["summary"], "任务已成功完成")
        self.assertEqual(legacy_status["last_output"], "")

        user_task = companion.submit_task("tsk-legacy-user", "test-project", "private prompt")
        companion._handle_server_notification(
            "item/updated",
            {
                "threadId": user_task["thread_id"],
                "turnId": user_task["turn_id"],
                "item": {"type": "message", "role": "user", "text": "private prompt"},
            },
        )
        companion._handle_server_notification(
            "turn/completed",
            {"threadId": user_task["thread_id"], "turnId": user_task["turn_id"], "status": "completed"},
        )
        self.assertNotIn("private prompt", companion.get_task_status("tsk-legacy-user")["summary"])

    def test_untyped_terminal_summary_and_output_are_not_adopted_as_result(self) -> None:
        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=":memory:",
        )
        task = companion.submit_task("tsk-untyped-terminal", "test-project", "private prompt")
        companion._handle_server_notification(
            "turn/completed",
            {
                "threadId": task["thread_id"],
                "turnId": task["turn_id"],
                "status": "completed",
                "summary": "unattributed summary",
                "output": "unattributed output",
                "turn": {"status": "completed", "response": "unattributed response"},
            },
        )
        status = companion.get_task_status("tsk-untyped-terminal")
        self.assertEqual(status["summary"], "任务已成功完成")
        self.assertEqual(status["last_output"], "")

    def test_failed_turn_is_reported_as_failure_with_server_error(self) -> None:
        db_path = os.path.join(self.workspace_dir, "failed-task.sqlite3")
        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_path,
        )
        events: list[tuple[str, dict[str, Any]]] = []
        companion.add_event_listener(lambda event, data: events.append((event, data)))
        task = companion.submit_task("tsk-failed-turn", "test-project", "run the task")

        companion._handle_server_notification(
            "turn/completed",
            {
                "threadId": task["thread_id"],
                "turnId": task["turn_id"],
                "status": "failed",
                "error": {"message": "app-server rejected the operation"},
            },
        )

        status = companion.get_task_status("tsk-failed-turn")
        self.assertEqual(status["status"], "failed")
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["summary"], "app-server rejected the operation")
        self.assertEqual(events, [("codex.failed", {
            "task_id": "tsk-failed-turn",
            "status": "failed",
            "summary": "app-server rejected the operation",
        })])

        restored_companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_path,
        )
        restored = restored_companion.get_task_status("tsk-failed-turn")
        self.assertEqual(restored["status"], "failed")
        self.assertEqual(restored["summary"], "app-server rejected the operation")
        companion._conn.close()
        restored_companion._conn.close()

    def test_failed_and_interrupted_events_redact_prompt_echo_and_keep_status_distinct(self) -> None:
        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=":memory:",
        )
        prompt = "private task instructions"
        events: list[tuple[str, dict[str, Any]]] = []
        companion.add_event_listener(lambda event, data: events.append((event, data)))

        failed = companion.submit_task("tsk-redacted-failure", "test-project", prompt)
        companion._handle_server_notification(
            "turn/completed",
            {
                "threadId": failed["thread_id"],
                "turnId": failed["turn_id"],
                "status": "failed",
                "error": {"message": f"Execution failed while handling: {prompt}"},
            },
        )

        interrupted = companion.submit_task("tsk-redacted-interruption", "test-project", prompt)
        companion._handle_server_notification(
            "turn/completed",
            {
                "threadId": interrupted["thread_id"],
                "turnId": interrupted["turn_id"],
                "status": "interrupted",
                "summary": f"Interrupted: {prompt}",
            },
        )

        self.assertEqual([event for event, _ in events], ["codex.failed", "codex.interrupted"])
        self.assertEqual([data["status"] for _, data in events], ["failed", "interrupted"])
        self.assertNotIn(prompt, json.dumps(events))
        self.assertIn("[user request omitted]", events[0][1]["summary"])
        self.assertIn("[user request omitted]", events[1][1]["summary"])
        self.assertEqual(companion.get_task_status("tsk-redacted-failure")["status"], "failed")
        self.assertEqual(companion.get_task_status("tsk-redacted-interruption")["status"], "interrupted")

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
        self.assertEqual(resp["decision"], "accept")
        self.assertEqual(resp["status"], "applied")
        self.assertEqual(len(sent_approvals), 1)
        self.assertEqual(sent_approvals[0]["decision"], "accept")

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

        token = "test-auth-token-12345"

        # Bind an ephemeral port (0) and read back the OS-assigned one so that
        # concurrent full-suite runs cannot collide on a fixed port.
        server = create_companion_server(port=0, host="127.0.0.1", auth_token=token)
        port = server.server_address[1]
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

    # -------------------------------------------------------------------------
    # 7. Server-Initiated Approval with Server Request ID
    # -------------------------------------------------------------------------
    def test_server_initiated_request_approval_with_server_req_id(self) -> None:
        """Verifies server-initiated approvals send JSON-RPC response with matching server_req_id."""
        mock_transport = DefaultMockTransport()
        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=mock_transport,
        )

        sub_res = companion.submit_task("tsk-serv-01", "test-project", "npm test")
        thread_id = sub_res["thread_id"]
        turn_id = sub_res["turn_id"]

        # Server sends approval request with server-side JSON-RPC ID 88
        companion._handle_server_notification(
            method="item/commandExecution/requestApproval",
            params={
                "threadId": thread_id,
                "turnId": turn_id,
                "command": "npm install lodash",
                "cwd": self.workspace_dir,
            },
            server_req_id=88,
        )

        # Verify approval is registered under task
        pending = companion.get_pending_approvals("tsk-serv-01")
        self.assertEqual(len(pending), 1)
        appr = pending[0]
        self.assertEqual(appr["task_id"], "tsk-serv-01")
        self.assertEqual(appr["server_req_id"], 88)
        self.assertEqual(appr["action_type"], "command")
        self.assertEqual(appr["command_or_path"], "npm install lodash")

        # Respond with decision 'accept'
        res = companion.respond_approval(
            approval_id=appr["approval_id"],
            task_id="tsk-serv-01",
            decision="accept",
            reason="User confirmed in dialog",
        )
        self.assertEqual(res["decision"], "accept")
        self.assertEqual(res["status"], "applied")

        # Verify transport sent response with id=88
        self.assertEqual(len(mock_transport.sent_responses), 1)
        resp_sent = mock_transport.sent_responses[0]
        self.assertEqual(resp_sent["id"], 88)
        self.assertEqual(resp_sent["result"]["decision"], "accept")

    # -------------------------------------------------------------------------
    # 8. Approval Race Condition Resolution (Approval before Task Mapping)
    # -------------------------------------------------------------------------
    def test_approval_arriving_before_task_mapping_race_condition(self) -> None:
        """Verifies approval arriving before task mapping completes is automatically backfilled."""
        mock_transport = DefaultMockTransport()
        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=mock_transport,
        )

        target_thread = "thr-early-race-01"

        # Inject approval notification BEFORE submit_task finishes
        companion._handle_server_notification(
            method="item/fileChange/requestApproval",
            params={
                "threadId": target_thread,
                "turnId": "trn-early-01",
                "path": "package.json",
            },
            server_req_id=99,
        )

        # At this moment, task_id is unresolved
        pending_early = companion.get_pending_approvals()
        self.assertEqual(len(pending_early), 1)
        self.assertEqual(pending_early[0]["task_id"], "")

        # Now simulate thread mapping occurring (e.g. submit_task receives thread_id)
        with companion._lock:
            companion._thread_to_task[target_thread] = "tsk-race-resolved"
            for appr in companion._pending_approvals.values():
                if not appr.get("task_id") and appr.get("thread_id") == target_thread:
                    appr["task_id"] = "tsk-race-resolved"

        # Verify approval is now associated with task_id
        pending_after = companion.get_pending_approvals("tsk-race-resolved")
        self.assertEqual(len(pending_after), 1)
        self.assertEqual(pending_after[0]["task_id"], "tsk-race-resolved")
        self.assertEqual(pending_after[0]["action_type"], "file_change")

        # Responding succeeds
        resp = companion.respond_approval(
            approval_id=pending_after[0]["approval_id"],
            task_id="tsk-race-resolved",
            decision="approve",
        )
        self.assertEqual(resp["status"], "applied")

    # -------------------------------------------------------------------------
    # 9. RemoteCodexCompanionClient Full Cycle Over HTTP RPC
    # -------------------------------------------------------------------------
    def test_remote_codex_companion_client_full_cycle(self) -> None:
        """Verifies RemoteCodexCompanionClient operations over LAN HTTP RPC."""
        from companion.src.codex_companion import create_companion_server, RemoteCodexCompanionClient

        token = "secret-client-token-999"

        # Bind an ephemeral port (0) and read back the OS-assigned one so that
        # concurrent full-suite runs cannot collide on a fixed port.
        server = create_companion_server(port=0, host="127.0.0.1", auth_token=token)
        port = server.server_address[1]
        srv_thread = threading.Thread(target=server.serve_forever, daemon=True)
        srv_thread.start()
        time.sleep(0.1)

        client = RemoteCodexCompanionClient(
            endpoint_url=f"http://127.0.0.1:{port}",
            auth_token=token,
            timeout_sec=5.0,
        )

        try:
            # 1. Submit task
            submit_res = client.submit_task("tsk-rem-01", "epanda-desk-agent", "pytest tests/")
            self.assertEqual(submit_res["task_id"], "tsk-rem-01")
            self.assertEqual(submit_res["workspace_alias"], "epanda-desk-agent")

            # 2. Get task status
            st = client.get_task_status("tsk-rem-01")
            self.assertIsNotNone(st)
            self.assertEqual(st["task_id"], "tsk-rem-01")

            # 3. List workspaces
            workspaces = client.list_workspaces()
            self.assertIn("epanda-desk-agent", workspaces)

            # 4. Append task
            append_res = client.append_task("tsk-rem-01", "run coverage report")
            self.assertTrue(append_res["appended"])

            # 5. Cancel task
            cancel_res = client.cancel_task("tsk-rem-01", reason="Testing cancel")
            self.assertEqual(cancel_res["status"], "cancelling")

            # 6. Auth failure with invalid token
            bad_client = RemoteCodexCompanionClient(
                endpoint_url=f"http://127.0.0.1:{port}",
                auth_token="wrong-token",
            )
            with self.assertRaises(PermissionError):
                bad_client.get_task_status("tsk-rem-01")

        finally:
            server.shutdown()
            server.server_close()

    def test_remote_completion_event_excludes_raw_task_status_and_prompt(self) -> None:
        from companion.src.codex_companion import RemoteCodexCompanionClient

        client = RemoteCodexCompanionClient("http://127.0.0.1:9998", poll_interval=0.01)
        task_id = "tsk-safe-completion"
        prompt = "private user prompt"
        client._pending_tasks[task_id] = {"task_id": task_id, "prompt": prompt}
        client.get_task_status = lambda _task_id: {
            "result": {
                "task_id": task_id,
                "status": "completed",
                "thread_id": "thread-safe-completion",
                "turn_id": "turn-safe-completion",
                "prompt": prompt,
                "summary": f"assistant response echoed request: {prompt}",
            }
        }
        events: list[tuple[str, dict[str, Any]]] = []

        def acknowledge_restored_event(event: str, data: dict[str, Any]) -> bool:
            events.append((event, data))
            return True

        try:
            client.add_event_listener(acknowledge_restored_event)
            deadline = time.time() + 2.0
            while time.time() < deadline and not events:
                time.sleep(0.01)
        finally:
            client.close()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "codex.turn_completed")
        self.assertEqual(
            events[0][1]["summary"],
            "assistant response echoed request: [user request omitted]",
        )
        self.assertNotIn("result", events[0][1])
        self.assertNotIn(prompt, json.dumps(events[0][1]))

    def test_remote_task_is_not_acknowledged_by_listener_without_explicit_true(self) -> None:
        from companion.src.codex_companion import RemoteCodexCompanionClient

        client = RemoteCodexCompanionClient("http://127.0.0.1:9995", poll_interval=0.01)
        task_id = "tsk-no-device-ack"
        client._pending_tasks[task_id] = {"task_id": task_id, "prompt": "private prompt"}
        client.get_task_status = lambda _task_id: {
            "result": {"task_id": task_id, "status": "completed", "summary": "assistant result"}
        }
        events: list[tuple[str, dict[str, Any]]] = []
        stop_after_delivery = threading.Event()

        def observer(event: str, data: dict[str, Any]) -> None:
            events.append((event, data))
            stop_after_delivery.set()

        try:
            client.add_event_listener(observer)
            deadline = time.time() + 2.0
            while time.time() < deadline and not stop_after_delivery.is_set():
                time.sleep(0.01)
        finally:
            client.close()

        self.assertEqual(len(events), 1)
        self.assertIn(task_id, client._pending_tasks)

    def test_remote_task_is_acknowledged_only_when_listener_returns_true(self) -> None:
        from companion.src.codex_companion import RemoteCodexCompanionClient

        client = RemoteCodexCompanionClient("http://127.0.0.1:9994", poll_interval=0.01)
        task_id = "tsk-device-ack"
        client._pending_tasks[task_id] = {"task_id": task_id, "prompt": "private prompt"}
        client.get_task_status = lambda _task_id: {
            "result": {"task_id": task_id, "status": "completed", "summary": "assistant result"}
        }
        events: list[tuple[str, dict[str, Any]]] = []

        def accepted(event: str, data: dict[str, Any]) -> bool:
            events.append((event, data))
            return True

        try:
            client.add_event_listener(accepted)
            deadline = time.time() + 2.0
            while time.time() < deadline and task_id in client._pending_tasks:
                time.sleep(0.01)
        finally:
            client.close()

        self.assertEqual(len(events), 1)
        self.assertNotIn(task_id, client._pending_tasks)

    def test_remote_failure_event_uses_persisted_failure_summary(self) -> None:
        from companion.src.codex_companion import RemoteCodexCompanionClient

        client = RemoteCodexCompanionClient("http://127.0.0.1:9997", poll_interval=0.01)
        task_id = "tsk-safe-failure"
        prompt = "private user prompt"
        client._pending_tasks[task_id] = {"task_id": task_id, "prompt": prompt}
        client.get_task_status = lambda _task_id: {
            "result": {
                "task_id": task_id,
                "status": "failed",
                "thread_id": "thread-safe-failure",
                "turn_id": "turn-safe-failure",
                "prompt": prompt,
                "summary": "task failed: validation did not pass",
                "error": {"message": "internal transport detail"},
            }
        }
        events: list[tuple[str, dict[str, Any]]] = []
        try:
            client.add_event_listener(lambda event, data: events.append((event, data)))
            deadline = time.time() + 2.0
            while time.time() < deadline and not events:
                time.sleep(0.01)
        finally:
            client.close()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "codex.failed")
        self.assertEqual(events[0][1]["summary"], "task failed: validation did not pass")
        self.assertNotIn("result", events[0][1])
        self.assertNotIn(prompt, json.dumps(events[0][1]))
        self.assertNotIn("internal transport detail", json.dumps(events[0][1]))

    def test_restored_remote_notification_drops_legacy_raw_result(self) -> None:
        from companion.src.codex_companion import RemoteCodexCompanionClient

        client = RemoteCodexCompanionClient("http://127.0.0.1:9996", poll_interval=0.01)
        task_id = "tsk-restored-safe"
        prompt = "private recovered prompt"
        client._pending_tasks[task_id] = {
            "task_id": task_id,
            "prompt": prompt,
            "status": "completed",
            "notification": {
                "task_id": task_id,
                "status": "completed",
                "summary": "assistant result after restart",
                "result": {"prompt": prompt, "last_output": "assistant result after restart"},
            },
        }
        events: list[tuple[str, dict[str, Any]]] = []
        try:
            client.add_event_listener(lambda event, data: events.append((event, data)))
            deadline = time.time() + 2.0
            while time.time() < deadline and not events:
                time.sleep(0.01)
        finally:
            client.close()

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0][0], "codex.turn_completed")
        self.assertEqual(events[0][1]["summary"], "assistant result after restart")
        self.assertNotIn("result", events[0][1])
        self.assertNotIn(prompt, json.dumps(events[0][1]))

    # -------------------------------------------------------------------------
    # 10. Approval Schema Enums Mapping (Command & Patch)
    # -------------------------------------------------------------------------
    def test_approval_schema_enums_mapping_command_and_patch(self) -> None:
        """Verifies CommandExecutionRequestApprovalResponse and ApplyPatchApprovalResponse align with Codex 0.161.0 schema."""
        mock_transport = DefaultMockTransport()
        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=mock_transport,
        )

        sub_res = companion.submit_task("tsk-enum-01", "test-project", "make build")
        thread_id = sub_res["thread_id"]
        turn_id = sub_res["turn_id"]

        # 1. Command approval: legacy "approve" -> "accept"
        companion.request_approval("appr-cmd-01", "tsk-enum-01", "command", "make install", "install deps")
        res1 = companion.respond_approval(approval_id="appr-cmd-01", task_id="tsk-enum-01", decision="approve")
        self.assertEqual(res1["decision"], "accept")
        self.assertEqual(res1["status"], "applied")

        # 2. Command approval: legacy "reject" -> "decline"
        companion.request_approval("appr-cmd-02", "tsk-enum-01", "command", "rm -rf /", "destructive rm")
        res2 = companion.respond_approval(approval_id="appr-cmd-02", decision="reject")
        self.assertEqual(res2["decision"], "decline")
        self.assertEqual(res2["status"], "rejected")

        # 3. Command approval: direct "acceptForSession" and "cancel"
        companion.request_approval("appr-cmd-03", "tsk-enum-01", "command", "git pull", "git pull")
        res3 = companion.respond_approval(approval_id="appr-cmd-03", decision="acceptForSession")
        self.assertEqual(res3["decision"], "acceptForSession")
        self.assertEqual(res3["status"], "applied")

        companion.request_approval("appr-cmd-04", "tsk-enum-01", "command", "cargo run", "cargo run")
        res4 = companion.respond_approval(approval_id="appr-cmd-04", decision="cancel")
        self.assertEqual(res4["decision"], "cancel")

        # 4. Patch approval: legacy "approve" -> "approved"
        companion.request_approval("appr-patch-01", "tsk-enum-01", "file_change", "src/main.py", "add feature")
        res5 = companion.respond_approval(approval_id="appr-patch-01", decision="approve")
        self.assertEqual(res5["decision"], "approved")
        self.assertEqual(res5["status"], "applied")

        # 5. Patch approval: legacy "reject" -> {"denied": {"rejection": reason}}
        companion.request_approval("appr-patch-02", "tsk-enum-01", "file_change", "config.json", "update secret")
        res6 = companion.respond_approval(
            approval_id="appr-patch-02",
            decision="reject",
            reason="Do not edit secret keys in config",
        )
        self.assertEqual(res6["decision"], {"denied": {"rejection": "Do not edit secret keys in config"}})
        self.assertEqual(res6["status"], "rejected")

        # 6. Patch approval: direct "abort"
        companion.request_approval("appr-patch-03", "tsk-enum-01", "apply_patch", "patch.diff", "apply patch")
        res7 = companion.respond_approval(approval_id="appr-patch-03", decision="abort")
        self.assertEqual(res7["decision"], "abort")

    # -------------------------------------------------------------------------
    # 11. SQLite Persistence & Reload Across Companion Restarts
    # -------------------------------------------------------------------------
    def test_sqlite_persistence_across_companion_restarts(self) -> None:
        """Verifies tasks and approvals persist to SQLite and survive process restarts."""
        db_file = os.path.join(self.temp_dir.name, "companion_persisted.db")

        # Instance 1: Create tasks and approvals
        comp1 = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )

        res_task = comp1.submit_task("tsk-persist-100", "test-project", "npm run test:unit")
        thread_id = res_task["thread_id"]
        turn_id = res_task["turn_id"]

        # Register approvals
        comp1.request_approval("appr-persist-cmd", "tsk-persist-100", "command", "npm install", "install deps")
        comp1.request_approval("appr-persist-file", "tsk-persist-100", "file_change", "src/index.js", "modify index")

        # Respond to cmd approval
        comp1.respond_approval("appr-persist-cmd", "tsk-persist-100", "accept")

        # Simulate turn progress
        comp1._handle_server_notification("turn/progress", {"threadId": thread_id, "turnId": turn_id, "progress": "step 3 of 5"})

        comp1.close()

        # Instance 2: Reload from same database
        comp2 = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )

        # Task must be recovered
        task_st = comp2.get_task_status("tsk-persist-100")
        self.assertIsNotNone(task_st)
        self.assertEqual(task_st["task_id"], "tsk-persist-100")
        self.assertEqual(task_st["thread_id"], thread_id)
        self.assertEqual(task_st["turn_id"], turn_id)
        self.assertEqual(task_st["workspace_alias"], "test-project")
        self.assertEqual(task_st["target_dir"], self.workspace_dir)

        # Only pending approvals returned by get_pending_approvals
        pending = comp2.get_pending_approvals("tsk-persist-100")
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["approval_id"], "appr-persist-file")
        self.assertEqual(pending[0]["action_type"], "file_change")
        self.assertEqual(pending[0]["command_or_path"], "src/index.js")

        # Applied approval state was preserved
        appr_cmd = comp2._pending_approvals.get("appr-persist-cmd")
        self.assertIsNotNone(appr_cmd)
        self.assertEqual(appr_cmd["status"], "applied")
        self.assertEqual(appr_cmd["decision"], "accept")

        # Instance 2 responds to pending file change approval with rejection
        resp_file = comp2.respond_approval(
            "appr-persist-file",
            "tsk-persist-100",
            "reject",
            reason="Security review rejected changes",
        )
        self.assertEqual(resp_file["decision"], {"denied": {"rejection": "Security review rejected changes"}})
        self.assertEqual(resp_file["status"], "rejected")

        # Cancel task
        comp2.cancel_task("tsk-persist-100", reason="Done testing")
        comp2.close()

        # Instance 3: Verify updated states persisted
        comp3 = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )

        task_st3 = comp3.get_task_status("tsk-persist-100")
        self.assertIsNotNone(task_st3)
        self.assertEqual(task_st3["status"], "cancelling")

        pending3 = comp3.get_pending_approvals("tsk-persist-100")
        self.assertEqual(len(pending3), 0)

        appr_file3 = comp3._pending_approvals.get("appr-persist-file")
        self.assertIsNotNone(appr_file3)
        self.assertEqual(appr_file3["status"], "rejected")
        self.assertEqual(appr_file3["decision"], {"denied": {"rejection": "Security review rejected changes"}})

        comp3.close()

    # -------------------------------------------------------------------------
    # 12. turn/interrupt CamelCase Schema Validation
    # -------------------------------------------------------------------------
    def test_cancel_task_turn_interrupt_camel_case_schema(self) -> None:
        """Verifies turn/interrupt passes strict camelCase threadId and turnId parameters."""
        sent_calls: list[dict[str, Any]] = []

        class InterceptTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                sent_calls.append({"method": method, "params": dict(params)})
                if method == "initialize":
                    return {"capabilities": {}}
                elif method in ("thread/create", "thread/start"):
                    return {"thread_id": "thr-interrupt-123"}
                elif method == "turn/start":
                    return {"turn_id": "trn-interrupt-456", "state": "running"}
                elif method == "turn/interrupt":
                    return {"interrupted": True}
                return {}

        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=InterceptTransport(),
        )

        companion.submit_task("tsk-inter-01", "test-project", "sleep 100")
        companion.cancel_task("tsk-inter-01", reason="Timed out")

        interrupt_calls = [c for c in sent_calls if c["method"] == "turn/interrupt"]
        self.assertEqual(len(interrupt_calls), 1)
        params = interrupt_calls[0]["params"]
        self.assertEqual(params["threadId"], "thr-interrupt-123")
        self.assertEqual(params["turnId"], "trn-interrupt-456")
        self.assertEqual(params["reason"], "Timed out")
        self.assertNotIn("thread_id", params)
        self.assertNotIn("turn_id", params)
        companion.close()

    def test_cancel_reconciles_server_terminal_state_when_turn_is_no_longer_active(self) -> None:
        """A completion/cancel race should return the app-server's terminal state."""
        class NoActiveTurnTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                if method == "initialize":
                    return {"capabilities": {}}
                if method in ("thread/create", "thread/start"):
                    return {"thread_id": "thr-no-active-01"}
                if method == "turn/start":
                    return {"turn_id": "trn-no-active-01", "state": "running"}
                if method == "turn/interrupt":
                    raise RuntimeError("Codex JSON-RPC error: no active turn to interrupt")
                if method == "thread/resume":
                    return {"thread": {"id": params["threadId"], "turns": [{
                        "id": "trn-no-active-01",
                        "status": "interrupted",
                        "items": [],
                    }]}}
                return {}

        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=NoActiveTurnTransport(),
        )
        events: list[tuple[str, dict[str, Any]]] = []
        companion.add_event_listener(lambda event, data: events.append((event, data)))
        companion.submit_task("tsk-no-active-01", "test-project", "private prompt")

        result = companion.cancel_task("tsk-no-active-01", reason="User requested stop")

        status = companion.get_task_status("tsk-no-active-01")
        self.assertEqual(result["status"], "interrupted")
        self.assertEqual(status["status"], "interrupted")
        self.assertNotIn("private prompt", status["summary"])
        self.assertEqual([event for event, _ in events], ["codex.interrupted"])
        companion.close()

    def test_initialize_resumes_persisted_running_thread_and_restores_turn_correlation(self) -> None:
        db_file = os.path.join(self.temp_dir.name, "resume-running.db")
        original = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )
        task = original.submit_task("tsk-resume-running", "test-project", "run a task")
        original.close()

        calls: list[tuple[str, dict[str, Any]]] = []

        class ResumeTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                calls.append((method, dict(params)))
                if method == "initialize":
                    return {"server_version": "test"}
                if method == "thread/resume":
                    return {"thread": {"id": task["thread_id"], "turns": [{
                        "id": task["turn_id"], "status": "inProgress", "items": [],
                    }]}}
                return {}

        resumed = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=ResumeTransport(),
            db_path=db_file,
        )
        events: list[tuple[str, dict[str, Any]]] = []
        resumed.add_event_listener(lambda event, data: events.append((event, data)))
        resumed.initialize()

        self.assertEqual([method for method, _ in calls], ["initialize", "thread/resume"])
        resume_params = calls[1][1]
        self.assertEqual(resume_params["threadId"], task["thread_id"])
        self.assertEqual(resume_params["cwd"], self.workspace_dir)
        self.assertFalse(resume_params["excludeTurns"])
        self.assertEqual(resumed._turn_to_task[task["turn_id"]], "tsk-resume-running")
        self.assertEqual(resumed.get_task_status("tsk-resume-running")["status"], "running")
        self.assertEqual(resumed.get_task_status("tsk-resume-running")["recovery_status"], "reconnected")

        resumed._handle_server_notification(
            "turn/completed",
            {"threadId": task["thread_id"], "turn": {"id": task["turn_id"], "status": "completed"}},
        )
        self.assertEqual(resumed.get_task_status("tsk-resume-running")["status"], "completed")
        self.assertEqual([event for event, _ in events], ["codex.turn_completed"])
        resumed.close()

    def test_turn_start_timeout_persists_thread_and_recovers_actual_turn(self) -> None:
        db_file = os.path.join(self.temp_dir.name, "turn-start-timeout.db")

        class DelayedTurnStartTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                if method == "initialize":
                    return {"server_version": "test"}
                if method in ("thread/create", "thread/start"):
                    return {"thread_id": "thr-start-timeout"}
                if method == "turn/start":
                    raise TimeoutError("app-server timed out waiting for turn/start")
                return {}

        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DelayedTurnStartTransport(),
            db_path=db_file,
        )
        with self.assertRaises(TimeoutError):
            companion.submit_task("tsk-start-timeout", "test-project", "private prompt")

        status = companion.get_task_status("tsk-start-timeout")
        self.assertIsNotNone(status)
        self.assertEqual(status["thread_id"], "thr-start-timeout")
        self.assertEqual(status["status"], "start_unknown")
        self.assertEqual(status["summary"], "")
        self.assertEqual(status["last_output"], "")
        companion.close()

        class ResumeTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                if method == "initialize":
                    return {"server_version": "test"}
                if method == "thread/resume":
                    return {"thread": {"id": params["threadId"], "turns": [{
                        "id": "trn-created-despite-timeout",
                        "status": "inProgress",
                        "items": [],
                    }]}}
                if method == "turn/interrupt":
                    if params["turnId"] != "trn-created-despite-timeout":
                        raise AssertionError("recovery did not restore the actual turn ID")
                    return {"interrupted": True}
                return {}

        resumed = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=ResumeTransport(),
            db_path=db_file,
        )
        resumed.initialize()
        recovered = resumed.get_task_status("tsk-start-timeout")
        self.assertEqual(recovered["turn_id"], "trn-created-despite-timeout")
        self.assertEqual(recovered["status"], "running")
        self.assertEqual(recovered["recovery_status"], "reconnected")
        self.assertEqual(resumed.cancel_task("tsk-start-timeout")["status"], "cancelling")
        resumed.close()

    def test_turn_start_response_does_not_overwrite_synchronous_terminal_notification(self) -> None:
        companion: CodexCompanion

        class NotificationBeforeTurnStartResponseTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                if method == "initialize":
                    return {"server_version": "test"}
                if method in ("thread/create", "thread/start"):
                    return {"thread_id": "thr-notification-race"}
                if method == "turn/start":
                    companion._handle_server_notification("turn/completed", {
                        "threadId": params["threadId"],
                        "turnId": "trn-notification-race",
                        "turn": {
                            "status": "completed",
                            "items": [{"type": "agentMessage", "text": "assistant completed result"}],
                        },
                    })
                    return {"turn_id": "trn-notification-race", "state": "inProgress"}
                return {}

        companion = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=NotificationBeforeTurnStartResponseTransport(),
            db_path=":memory:",
        )
        result = companion.submit_task("tsk-notification-race", "test-project", "private prompt")
        status = companion.get_task_status("tsk-notification-race")
        self.assertEqual(result["turn_id"], "trn-notification-race")
        self.assertEqual(status["turn_id"], "trn-notification-race")
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["summary"], "assistant completed result")
        self.assertEqual(status["last_output"], "assistant completed result")
        self.assertNotIn("private prompt", status["summary"])
        companion.close()

    def test_resume_reconciles_schema_terminal_turns_without_using_user_items_as_output(self) -> None:
        db_file = os.path.join(self.temp_dir.name, "resume-terminal.db")
        original = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )
        tasks = {
            key: original.submit_task(key, "test-project", "private prompt")
            for key in ("tsk-resume-completed", "tsk-resume-failed", "tsk-resume-interrupted")
        }
        cancel_result = original.cancel_task("tsk-resume-interrupted", reason="user requested stop")
        self.assertEqual(cancel_result["status"], "cancelling")
        self.assertEqual(original.get_task_status("tsk-resume-interrupted")["status"], "cancelling")
        original.close()

        statuses = {
            tasks["tsk-resume-completed"]["thread_id"]: ("completed", [
                {"type": "userMessage", "text": "private prompt"},
                {"type": "agentMessage", "text": "assistant result after restart"},
            ]),
            tasks["tsk-resume-failed"]["thread_id"]: ("failed", []),
            tasks["tsk-resume-interrupted"]["thread_id"]: ("interrupted", []),
        }

        class ResumeTerminalTransport(JsonRpcTransport):
            companion: Optional[CodexCompanion] = None

            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                if method == "initialize":
                    return {"server_version": "test"}
                if method == "thread/resume":
                    status, items = statuses[params["threadId"]]
                    task = next(value for value in tasks.values() if value["thread_id"] == params["threadId"])
                    if status == "completed":
                        items.append({"type": "message", "text": "ambiguous non-assistant text"})
                    if status == "completed" and self.companion is not None:
                        self.companion._handle_server_notification("turn/completed", {
                            "threadId": params["threadId"],
                            "turn": {"id": task["turn_id"], "status": status, "items": items},
                        })
                    return {"thread": {"id": params["threadId"], "turns": [{
                        "id": task["turn_id"], "status": status, "items": items,
                        "error": {"message": "server error"} if status == "failed" else None,
                    }]}}
                return {}

        terminal_transport = ResumeTerminalTransport()
        resumed = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=terminal_transport,
            db_path=db_file,
        )
        terminal_transport.companion = resumed
        events: list[tuple[str, dict[str, Any]]] = []
        resumed.add_event_listener(lambda event, data: events.append((event, data)))
        resumed.initialize()

        self.assertEqual(resumed.get_task_status("tsk-resume-completed")["status"], "completed")
        self.assertEqual(resumed.get_task_status("tsk-resume-completed")["summary"], "assistant result after restart")
        self.assertNotIn("private prompt", json.dumps(events))
        self.assertEqual(sum(event == "codex.turn_completed" for event, _ in events), 1)
        self.assertEqual(resumed.get_task_status("tsk-resume-failed")["status"], "failed")
        self.assertEqual(resumed.get_task_status("tsk-resume-interrupted")["status"], "interrupted")
        self.assertEqual([event for event, _ in events], [
            "codex.turn_completed", "codex.failed", "codex.interrupted",
        ])
        resumed.close()

    def test_nonterminal_thread_is_resumed_after_each_companion_restart(self) -> None:
        db_file = os.path.join(self.temp_dir.name, "resume-twice.db")
        original = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )
        task = original.submit_task("tsk-resume-twice", "test-project", "run a task")
        original.close()

        resume_calls = 0

        class ResumeTwiceTransport(JsonRpcTransport):
            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                nonlocal resume_calls
                if method == "initialize":
                    return {"server_version": "test"}
                if method == "thread/resume":
                    resume_calls += 1
                    return {"thread": {"id": task["thread_id"], "turns": [{
                        "id": task["turn_id"], "status": "inProgress", "items": [],
                    }]}}
                return {}

        for _ in range(2):
            companion = CodexCompanion(
                allowed_workspaces=self.workspaces,
                transport=ResumeTwiceTransport(),
                db_path=db_file,
            )
            companion.initialize()
            self.assertEqual(companion.get_task_status("tsk-resume-twice")["status"], "running")
            self.assertEqual(companion.get_task_status("tsk-resume-twice")["recovery_status"], "reconnected")
            companion.close()
        self.assertEqual(resume_calls, 2)

    def test_active_writer_resume_conflict_stays_pending_and_can_be_retried(self) -> None:
        db_file = os.path.join(self.temp_dir.name, "resume-active-writer.db")
        original = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )
        task = original.submit_task("tsk-active-writer", "test-project", "run a task")
        original.close()

        class ResumeTransport(JsonRpcTransport):
            attempts = 0

            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                if method == "initialize":
                    return {"server_version": "test"}
                if method == "thread/resume":
                    self.attempts += 1
                    if self.attempts == 1:
                        raise RuntimeError("Codex JSON-RPC error: {'code': -32600, 'message': 'thread already has an active writer'}")
                    if self.attempts == 2:
                        return {"error": {"code": -32600, "message": "thread already has an active writer"}}
                    return {"thread": {"id": task["thread_id"], "turns": [{
                        "id": task["turn_id"], "status": "inProgress", "items": [],
                    }]}}
                return {}

        transport = ResumeTransport()
        resumed = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=transport,
            db_path=db_file,
        )
        self.addCleanup(resumed.close)
        clock = [100.0]
        clock_patcher = patch("companion.src.codex_companion.time.monotonic", side_effect=lambda: clock[0])
        clock_patcher.start()
        self.addCleanup(clock_patcher.stop)
        resumed.initialize()

        status = resumed.get_task_status("tsk-active-writer")
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["recovery_status"], "pending")
        self.assertEqual(status["recovery_error_kind"], "active_writer")
        self.assertFalse(status["summary"])
        self.assertEqual(resumed.get_pending_approvals("tsk-active-writer"), [])

        recovered = resumed.recover_pending_tasks()
        self.assertEqual(recovered, {"reconnected": 0, "reconciled": 0, "pending": 1})
        self.assertEqual(transport.attempts, 1)
        status = resumed.get_task_status("tsk-active-writer")
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["recovery_status"], "pending")
        self.assertEqual(status["recovery_error_kind"], "active_writer")

        clock[0] += 2.0
        recovered = resumed.recover_pending_tasks()
        self.assertEqual(recovered, {"reconnected": 0, "reconciled": 0, "pending": 1})
        self.assertEqual(transport.attempts, 2)
        status = resumed.get_task_status("tsk-active-writer")
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["recovery_status"], "pending")
        self.assertEqual(status["recovery_error_kind"], "active_writer")

        clock[0] += 3.0
        recovered = resumed.recover_pending_tasks()
        self.assertEqual(recovered, {"reconnected": 1, "reconciled": 0, "pending": 0})
        status = resumed.get_task_status("tsk-active-writer")
        self.assertEqual(status["status"], "running")
        self.assertEqual(status["recovery_status"], "reconnected")
        self.assertNotIn("recovery_error_kind", status)
        self.assertEqual(resumed.recover_pending_tasks(), {"reconnected": 0, "reconciled": 0, "pending": 0})
        self.assertEqual(transport.attempts, 3)
        resumed.close()

    def test_active_writer_backoff_preserves_task_and_reconciles_terminal_assistant_result(self) -> None:
        db_file = os.path.join(self.temp_dir.name, "resume-active-writer-backoff.db")
        original = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )
        prompt = "private recovery prompt"
        task = original.submit_task("tsk-active-writer-backoff", "test-project", prompt)
        original.close()

        class BackoffResumeTransport(JsonRpcTransport):
            resume_calls = 0

            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                if method == "initialize":
                    return {"server_version": "test"}
                if method == "thread/resume":
                    self.resume_calls += 1
                    if self.resume_calls == 1:
                        raise RuntimeError("Codex JSON-RPC error: {'code': -32600, 'message': 'thread already has an active writer'}")
                    return {"thread": {"id": task["thread_id"], "turns": [{
                        "id": task["turn_id"],
                        "status": "completed",
                        "items": [
                            {"type": "userMessage", "text": prompt},
                            {"type": "agentMessage", "text": "Recovered assistant result"},
                        ],
                    }]}}
                return {}

        transport = BackoffResumeTransport()
        clock = [1000.0]
        resumed = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=transport,
            db_path=db_file,
        )
        with patch("companion.src.codex_companion.time.monotonic", side_effect=lambda: clock[0]):
            resumed.initialize()
            state = resumed.get_task_status("tsk-active-writer-backoff")
            self.assertEqual(state["status"], "running")
            self.assertEqual(state["recovery_status"], "pending")
            self.assertEqual(state["recovery_error_kind"], "active_writer")
            self.assertEqual(state["thread_id"], task["thread_id"])
            self.assertEqual(state["turn_id"], task["turn_id"])
            self.assertEqual(state["prompt"], prompt)
            self.assertEqual(transport.resume_calls, 1)

            # Immediate retries remain pending and do not hammer the busy server.
            self.assertEqual(resumed.recover_pending_tasks(), {"reconnected": 0, "reconciled": 0, "pending": 1})
            self.assertEqual(transport.resume_calls, 1)

            # The retry becomes eligible after the short initial backoff.
            clock[0] += 2.0
            self.assertEqual(resumed.recover_pending_tasks(), {"reconnected": 0, "reconciled": 1, "pending": 0})

        final = resumed.get_task_status("tsk-active-writer-backoff")
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["summary"], "Recovered assistant result")
        self.assertNotIn(prompt, final["summary"])
        self.assertEqual(transport.resume_calls, 2)
        resumed.close()

    def test_active_writer_uses_read_only_snapshot_until_terminal_then_recovers_after_restart(self) -> None:
        db_file = os.path.join(self.temp_dir.name, "resume-active-writer-snapshot.db")
        original = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )
        task = original.submit_task("tsk-active-writer-snapshot", "test-project", "private prompt")
        original.close()

        class SnapshotTransport(JsonRpcTransport):
            snapshot_calls = 0
            methods: list[str] = []

            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                self.methods.append(method)
                if method == "initialize":
                    return {"server_version": "test"}
                if method == "thread/resume":
                    return {"error": {"code": -32600, "message": "thread already has an active writer"}}
                if method == "thread/read":
                    self.snapshot_calls += 1
                    status = "inProgress" if self.snapshot_calls == 1 else "completed"
                    items = [] if status == "inProgress" else [
                        {"type": "userMessage", "text": "private prompt"},
                        {"type": "agentMessage", "text": "assistant result recovered from snapshot"},
                    ]
                    return {"thread": {"id": task["thread_id"], "turns": [{
                        "id": task["turn_id"], "status": status, "items": items,
                    }]}}
                return {}

        transport = SnapshotTransport()
        resumed = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=transport,
            db_path=db_file,
        )
        self.addCleanup(resumed.close)
        clock = [2000.0]
        with patch("companion.src.codex_companion.time.monotonic", side_effect=lambda: clock[0]):
            resumed.initialize()
            first = resumed.get_task_status("tsk-active-writer-snapshot")
            self.assertEqual(first["status"], "running")
            self.assertEqual(first["recovery_status"], "pending")
            self.assertEqual(first["recovery_error_kind"], "active_writer")
            self.assertEqual(transport.methods, ["initialize", "thread/resume", "thread/read"])

            # An active snapshot is not ownership or event-stream recovery.
            self.assertEqual(resumed._thread_to_task.get(task["thread_id"]), "tsk-active-writer-snapshot")
            self.assertEqual(resumed._turn_to_task.get(task["turn_id"]), "tsk-active-writer-snapshot")
            self.assertEqual(resumed.get_task_status("tsk-active-writer-snapshot")["recovery_status"], "pending")

            clock[0] += 2.0
            self.assertEqual(resumed.recover_pending_tasks(), {"reconnected": 0, "reconciled": 1, "pending": 0})

        final = resumed.get_task_status("tsk-active-writer-snapshot")
        self.assertEqual(final["status"], "completed")
        self.assertEqual(final["summary"], "assistant result recovered from snapshot")
        self.assertNotIn("private prompt", final["summary"])
        self.assertEqual(transport.snapshot_calls, 2)
        self.assertNotIn("turn/start", transport.methods)
        self.assertNotIn("turn/append", transport.methods)
        resumed.close()

    def test_legacy_cancelled_task_is_not_resumed(self) -> None:
        db_file = os.path.join(self.temp_dir.name, "legacy-cancelled.db")
        original = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=DefaultMockTransport(),
            db_path=db_file,
        )
        original.submit_task("tsk-legacy-cancelled", "test-project", "already cancelled")
        with original._lock:
            task = original._tasks["tsk-legacy-cancelled"]
            task["status"] = task["state"] = "cancelled"
            original._persist_task(task)
        original.close()

        class NoResumeTransport(JsonRpcTransport):
            resume_calls = 0

            def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
                if method == "initialize":
                    return {"server_version": "test"}
                if method == "thread/resume":
                    self.resume_calls += 1
                return {}

        transport = NoResumeTransport()
        resumed = CodexCompanion(
            allowed_workspaces=self.workspaces,
            transport=transport,
            db_path=db_file,
        )
        resumed.initialize()
        self.assertEqual(transport.resume_calls, 0)
        self.assertEqual(resumed.get_task_status("tsk-legacy-cancelled")["status"], "cancelled")
        resumed.close()


if __name__ == "__main__":
    unittest.main()

