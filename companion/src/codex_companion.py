"""
Desk Agent R1 Windows Codex Companion.
Runs on Windows host alongside Codex CLI / app-server.
Manages workspace aliases, stdio JSON-RPC subprocess lifecycle,
controlled thread/turn creation, and human-in-the-loop approval bindings.
"""

from __future__ import annotations

import logging
import os
import subprocess
import threading
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


class CodexCompanion:
    def __init__(self, allowed_workspaces: Optional[dict[str, str]] = None) -> None:
        """
        :param allowed_workspaces: Mapping of workspace_alias -> absolute directory path.
        """
        self.workspaces = allowed_workspaces or {
            "epanda-desk-agent": os.path.abspath("."),
            "desk-agent-core": os.path.abspath("."),
        }
        # In-memory session tracking: task_id -> task details
        self._tasks: dict[str, dict[str, Any]] = {}
        # Pending approvals: approval_id -> approval details
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def register_workspace(self, alias: str, abs_path: str) -> None:
        self.workspaces[alias] = os.path.abspath(abs_path)

    def submit_task(
        self,
        task_id: str,
        workspace_alias: str,
        prompt: str,
        model: Optional[str] = None,
    ) -> dict[str, Any]:
        """Submits a new task under a controlled thread within the registered workspace."""
        if workspace_alias not in self.workspaces:
            raise ValueError(f"Workspace alias '{workspace_alias}' is not registered on this Windows host")

        target_dir = self.workspaces[workspace_alias]
        thread_id = f"thr_win_{task_id[:8]}"
        turn_id = f"trn_{task_id[:8]}_01"

        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "workspace_alias": workspace_alias,
                "target_dir": target_dir,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "prompt": prompt,
                "model": model or "gpt-5-codex",
                "state": "running",
            }

        return {
            "task_id": task_id,
            "workspace_alias": workspace_alias,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "state": "running",
        }

    def append_task(self, task_id: str, text: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(f"Task '{task_id}' not found")
            task["state"] = "running"
            return {"task_id": task_id, "appended": True}

    def cancel_task(self, task_id: str, reason: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(f"Task '{task_id}' not found")
            task["state"] = "cancelled"
            return {"task_id": task_id, "turn_id": task["turn_id"], "status": "cancelled", "reason": reason}

    def request_approval(
        self,
        approval_id: str,
        task_id: str,
        action_type: str,
        command_or_path: str,
        description: str,
    ) -> dict[str, Any]:
        """Registers a pending approval request required before command execution."""
        with self._lock:
            self._pending_approvals[approval_id] = {
                "approval_id": approval_id,
                "task_id": task_id,
                "action_type": action_type,
                "command_or_path": command_or_path,
                "description": description,
                "status": "pending",
            }
        return self._pending_approvals[approval_id]

    def respond_approval(
        self,
        approval_id: str,
        task_id: str,
        decision: str,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        with self._lock:
            appr = self._pending_approvals.get(approval_id)
            if not appr:
                raise KeyError(f"Approval '{approval_id}' not found")
            if appr["task_id"] != task_id:
                raise ValueError("Approval ID does not match specified task ID")

            appr["status"] = "applied" if decision == "approve" else "rejected"
            appr["decision"] = decision
            appr["reason"] = reason

        return {
            "approval_id": approval_id,
            "task_id": task_id,
            "decision": decision,
            "status": appr["status"],
        }

    def get_task_status(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            return dict(task) if task else None
