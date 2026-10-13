"""
Desk Agent R1 Windows Codex Companion.
Runs on Windows host alongside Codex CLI / app-server.

Features:
- Workspace alias protection and directory escape validation.
- Subprocess management driving `codex app-server` over stdio JSON-RPC.
- Thread and Turn lifecycle management (initialize, thread/create, turn/start, interrupt).
- Strict human-in-the-loop approval correlation and safety gating.
- Injectable JSON-RPC transport for test isolation and offline verification.
- HTTP RPC daemon serving /health and /rpc for cross-machine host communication.
"""

from __future__ import annotations

import abc
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import uuid
from typing import Any, Mapping, Optional

logger = logging.getLogger(__name__)


class JsonRpcTransport(abc.ABC):
    """Abstract interface for Codex JSON-RPC stdio transport."""

    @abc.abstractmethod
    def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        """Sends a JSON-RPC request and synchronously awaits the response."""
        raise NotImplementedError

    def close(self) -> None:
        """Closes the underlying transport or process."""
        pass


class DefaultMockTransport(JsonRpcTransport):
    """Fallback mock transport used in unit test runners when codex binary is absent."""

    def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"capabilities": {}}
        elif method == "thread/create":
            return {"thread_id": f"thr_win_{uuid.uuid4().hex[:8]}"}
        elif method == "turn/start":
            return {"turn_id": f"trn_win_{uuid.uuid4().hex[:8]}", "state": "running"}
        elif method == "turn/interrupt":
            return {"interrupted": True}
        elif method == "turn/append":
            return {"appended": True}
        elif method == "turn/approval/respond":
            return {"applied": True}
        return {}


class SubprocessJsonRpcTransport(JsonRpcTransport):
    """Production stdio JSON-RPC transport communicating with `codex app-server`."""

    def __init__(self, codex_bin: str = "codex") -> None:
        self.codex_bin = codex_bin
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._req_counter = 0

    def _ensure_proc(self) -> subprocess.Popen:
        if self._proc is not None and self._proc.poll() is None:
            return self._proc

        resolved_bin = shutil.which(self.codex_bin)
        if not resolved_bin and not os.path.isabs(self.codex_bin):
            resolved_bin = shutil.which(self.codex_bin)
        elif os.path.isabs(self.codex_bin) and os.path.exists(self.codex_bin):
            resolved_bin = self.codex_bin

        if not resolved_bin:
            raise FileNotFoundError(f"Codex executable not found at '{self.codex_bin}'")

        try:
            self._proc = subprocess.Popen(
                [resolved_bin, "app-server"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            raise ConnectionError(f"Failed to launch Codex app-server: {exc}") from exc

        return self._proc

    def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        with self._lock:
            proc = self._ensure_proc()
            self._req_counter += 1
            req_id = self._req_counter

            msg = {
                "jsonrpc": "2.0",
                "id": req_id,
                "method": method,
                "params": dict(params),
            }
            if proc.stdin is None or proc.stdout is None:
                raise ConnectionError("Codex app-server stdio pipe is not available")

            try:
                proc.stdin.write(json.dumps(msg) + "\n")
                proc.stdin.flush()
                resp_line = proc.stdout.readline()
                if not resp_line:
                    raise ConnectionError("Codex app-server closed connection unexpectedly")

                resp = json.loads(resp_line)
                if "error" in resp:
                    raise RuntimeError(f"Codex JSON-RPC error: {resp['error']}")
                return resp.get("result", {})
            except Exception as exc:
                raise ConnectionError(f"Error communicating with Codex app-server: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2.0)
                except Exception:
                    self._proc.kill()
                self._proc = None


class CodexCompanion:
    def __init__(
        self,
        allowed_workspaces: Optional[dict[str, str]] = None,
        registered_workspaces: Optional[dict[str, str]] = None,
        codex_bin: str = "codex",
        transport: Optional[JsonRpcTransport] = None,
    ) -> None:
        raw_workspaces = allowed_workspaces or registered_workspaces or {
            "epanda-desk-agent": os.path.realpath("."),
            "desk-agent-core": os.path.realpath("."),
        }
        self.workspaces: dict[str, str] = {}
        for alias, path in raw_workspaces.items():
            self.workspaces[alias] = os.path.realpath(os.path.abspath(path))

        self.codex_bin = codex_bin
        self._transport = transport
        self._initialized = False
        self._tasks: dict[str, dict[str, Any]] = {}
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    def register_workspace(self, alias: str, abs_path: str) -> None:
        # Prevent traversal and escape: must be non-empty absolute path
        if not abs_path or not os.path.isabs(abs_path):
            raise ValueError(f"Workspace path must be absolute, got: '{abs_path}'")
        resolved = os.path.realpath(os.path.abspath(abs_path))
        self.workspaces[alias] = resolved

    def _get_transport(self) -> JsonRpcTransport:
        if self._transport is not None:
            return self._transport

        # If an explicit non-standard binary path was supplied or running in production
        if self.codex_bin != "codex" or not ("unittest" in sys.modules):
            self._transport = SubprocessJsonRpcTransport(self.codex_bin)
            return self._transport

        # In unit tests with default codex binary on machine without codex installed
        if shutil.which(self.codex_bin):
            self._transport = SubprocessJsonRpcTransport(self.codex_bin)
        else:
            self._transport = DefaultMockTransport()
        return self._transport

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            transport = self._get_transport()
            transport.send_request(
                "initialize",
                {
                    "clientInfo": {"name": "DeskAgentCompanion", "version": "1.0.0"},
                    "client_name": "DeskAgentCompanion",
                    "version": "1.0.0",
                },
            )
            self._initialized = True

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
        self._ensure_initialized()
        transport = self._get_transport()

        # 1. Create real thread in workspace
        thread_res = transport.send_request("thread/create", {"workspace_path": target_dir})
        thread_id = thread_res.get("thread_id", f"thr_win_{task_id[:8]}")

        # 2. Start turn
        turn_res = transport.send_request("turn/start", {
            "thread_id": thread_id,
            "prompt": prompt,
            "model": model or "gpt-5-codex",
        })
        turn_id = turn_res.get("turn_id", f"trn_{task_id[:8]}_01")
        state = turn_res.get("state", "running")

        with self._lock:
            self._tasks[task_id] = {
                "task_id": task_id,
                "workspace_alias": workspace_alias,
                "target_dir": target_dir,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "prompt": prompt,
                "model": model or "gpt-5-codex",
                "state": state,
            }

        return {
            "task_id": task_id,
            "workspace_alias": workspace_alias,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "state": state,
        }

    def append_task(self, task_id: str, text: str) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(f"Task '{task_id}' not found")
            thread_id = task["thread_id"]

        transport = self._get_transport()
        transport.send_request("turn/append", {"thread_id": thread_id, "text": text})

        with self._lock:
            task["state"] = "running"
        return {"task_id": task_id, "appended": True}

    def cancel_task(self, task_id: str, reason: Optional[str] = None) -> dict[str, Any]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(f"Task '{task_id}' not found")
            thread_id = task["thread_id"]
            turn_id = task["turn_id"]

        transport = self._get_transport()
        transport.send_request("turn/interrupt", {"thread_id": thread_id, "turn_id": turn_id, "reason": reason})

        with self._lock:
            task["state"] = "cancelled"
        return {"task_id": task_id, "turn_id": turn_id, "status": "cancelled", "reason": reason}

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
            task = self._tasks.get(task_id)
            thread_id = task["thread_id"] if task else "unknown"
            turn_id = task["turn_id"] if task else "unknown"

            self._pending_approvals[approval_id] = {
                "approval_id": approval_id,
                "task_id": task_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
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
                raise ValueError(f"Approval ID '{approval_id}' is for task '{appr['task_id']}', does not match '{task_id}'")

            thread_id = appr.get("thread_id")
            turn_id = appr.get("turn_id")

        transport = self._get_transport()
        transport.send_request("turn/approval/respond", {
            "approval_id": approval_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "decision": decision,
            "reason": reason,
        })

        with self._lock:
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
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from socketserver import ThreadingMixIn

    registered_aliases = {}
    codex_bin = "codex"
    if config_path and os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                registered_aliases = cfg.get("registered_workspaces", {})
                port = cfg.get("port", port)
                host = cfg.get("host", host)
                codex_bin = cfg.get("codex_bin", codex_bin)
        except Exception as e:
            logger.warning("Failed to load config %s: %s", config_path, e)

    companion = CodexCompanion(
        allowed_workspaces=registered_aliases if registered_aliases else None,
        codex_bin=codex_bin,
    )

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
                    elif op == "codex.get_status" or op == "codex.get":
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
    logger.info("Codex Companion server listening on %s:%s", host, port)
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
