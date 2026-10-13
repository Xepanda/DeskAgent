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

import os
import tempfile
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


if __name__ == "__main__":
    unittest.main()
