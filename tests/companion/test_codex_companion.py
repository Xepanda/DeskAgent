"""
Unit tests for Windows Codex Companion.
"""

from __future__ import annotations

import unittest
from companion.src.codex_companion import CodexCompanion


class TestCodexCompanion(unittest.TestCase):
    def setUp(self) -> None:
        self.companion = CodexCompanion()

    def test_workspace_alias_resolution(self) -> None:
        # Default alias
        res = self.companion.submit_task(
            task_id="tsk-001",
            workspace_alias="epanda-desk-agent",
            prompt="git status",
        )
        self.assertEqual(res["task_id"], "tsk-001")
        self.assertEqual(res["state"], "running")

        # Unknown alias rejected
        with self.assertRaises(ValueError) as ctx:
            self.companion.submit_task(
                task_id="tsk-002",
                workspace_alias="unknown-alias",
                prompt="test",
            )
        self.assertIn("not registered on this Windows host", str(ctx.exception))

    def test_approval_lifecycle(self) -> None:
        self.companion.submit_task("tsk-003", "epanda-desk-agent", "make build")
        req = self.companion.request_approval(
            approval_id="appr-01",
            task_id="tsk-003",
            action_type="command",
            command_or_path="npm run deploy",
            description="Deploy to staging",
        )
        self.assertEqual(req["status"], "pending")

        # Reject approval
        resp = self.companion.respond_approval(
            approval_id="appr-01",
            task_id="tsk-003",
            decision="reject",
            reason="Blocked by policy",
        )
        self.assertEqual(resp["decision"], "reject")
        self.assertEqual(resp["status"], "rejected")

    def test_task_status_query(self) -> None:
        self.companion.submit_task("tsk-004", "epanda-desk-agent", "echo hi")
        st = self.companion.get_task_status("tsk-004")
        self.assertIsNotNone(st)
        self.assertEqual(st["task_id"], "tsk-004")


if __name__ == "__main__":
    unittest.main()
