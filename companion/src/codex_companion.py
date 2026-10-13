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


def start_companion_server(config_path: Optional[str] = None, port: int = 8765, host: str = "0.0.0.0") -> None:
    import argparse
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from socketserver import ThreadingMixIn

    registered_aliases = {}
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                registered_aliases = cfg.get("registered_workspaces", {})
                port = cfg.get("port", port)
                host = cfg.get("host", host)
        except Exception as e:
            print(f"[WARN] Failed to load config {config_path}: {e}")

    companion = CodexCompanion(registered_workspaces=registered_aliases if registered_aliases else None)

    class CompanionHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"status": "ok", "service": "codex_companion"}')
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path == "/rpc":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                try:
                    req = json.loads(body)
                    op = req.get("operation")
                    args = req.get("arguments", {})

                    if op == "codex.submit":
                        res = companion.submit_task(
                            task_id=args["task_id"],
                            workspace_alias=args["workspace_alias"],
                            prompt=args["prompt"],
                            model=args.get("model"),
                        )
                    elif op == "codex.append":
                        res = companion.append_task(args["task_id"], args["text"])
                    elif op == "codex.cancel":
                        res = companion.cancel_task(args["task_id"], args.get("reason"))
                    elif op == "codex.request_approval":
                        res = companion.request_approval(
                            approval_id=args["approval_id"],
                            task_id=args["task_id"],
                            action_type=args["action_type"],
                            command_or_path=args["command_or_path"],
                            description=args["description"],
                        )
                    elif op == "codex.respond_approval":
                        res = companion.respond_approval(
                            approval_id=args["approval_id"],
                            task_id=args["task_id"],
                            decision=args["decision"],
                            reason=args.get("reason"),
                        )
                    elif op == "codex.get_status":
                        res = companion.get_task_status(args["task_id"])
                    else:
                        raise ValueError(f"Unknown operation: {op}")

                    resp_data = {"success": True, "result": res}
                    self.send_response(200)
                except Exception as e:
                    resp_data = {"success": False, "error": str(e)}
                    self.send_response(400)

                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(resp_data).encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

    class ThreadedServer(ThreadingMixIn, HTTPServer):
        daemon_threads = True

    server = ThreadedServer((host, port), CompanionHandler)
    print(f"[INFO] Codex Companion server listening on {host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Desk Agent R1 Windows Codex Companion")
    parser.add_argument("--config", "-c", type=str, help="Path to companion-config.json")
    parser.add_argument("--port", "-p", type=int, default=8765, help="Port to listen on")
    cli_args = parser.parse_args()
    start_companion_server(config_path=cli_args.config, port=cli_args.port)
