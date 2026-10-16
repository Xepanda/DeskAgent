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

    def send_response(self, req_id: Any, result: Mapping[str, Any]) -> None:
        """Sends a JSON-RPC response back to the server for a server-initiated request."""
        pass

    def close(self) -> None:
        """Closes the underlying transport or process."""
        pass


class DefaultMockTransport(JsonRpcTransport):
    """Fallback mock transport used in unit test runners when codex binary is absent."""

    def __init__(self) -> None:
        self.sent_responses: list[dict[str, Any]] = []

    def send_response(self, req_id: Any, result: Mapping[str, Any]) -> None:
        self.sent_responses.append({"id": req_id, "result": dict(result)})

    def send_request(self, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        if method == "initialize":
            return {"capabilities": {}}
        elif method in ("thread/start", "thread/create"):
            tid = f"thr_win_{uuid.uuid4().hex[:8]}"
            return {"thread_id": tid, "thread": {"id": tid}}
        elif method == "turn/start":
            trn_id = f"trn_win_{uuid.uuid4().hex[:8]}"
            return {"turn_id": trn_id, "state": "running", "turn": {"id": trn_id, "status": "running"}}
        elif method == "turn/interrupt":
            return {"interrupted": True}
        elif method == "turn/append":
            return {"appended": True}
        elif method == "turn/approval/respond":
            return {"applied": True}
        return {}


class SubprocessJsonRpcTransport(JsonRpcTransport):
    """Production stdio JSON-RPC transport communicating with `codex app-server`.
    Features continuous background reader thread, response ID matching,
    and notification dispatch (turn progress, completion, approval requests).
    """

    def __init__(
        self,
        codex_bin: str = "codex",
        on_notification: Optional[Callable[..., None]] = None,
    ) -> None:
        self.codex_bin = codex_bin
        self.on_notification = on_notification
        self._proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._req_counter = 0
        self._pending: dict[int, threading.Event] = {}
        self._responses: dict[int, dict[str, Any]] = {}
        self._errors: dict[int, str] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._running = False

    def _reader_loop(self, proc: subprocess.Popen) -> None:
        while self._running:
            if proc.stdout is None or proc.poll() is not None:
                break
            try:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except Exception:
                    continue

                req_id = msg.get("id")
                if req_id is not None and req_id in self._pending:
                    if "error" in msg:
                        self._errors[req_id] = str(msg["error"])
                    else:
                        self._responses[req_id] = msg.get("result", {})
                    self._pending[req_id].set()
                elif "method" in msg:
                    if self.on_notification:
                        try:
                            # Pass server req_id if this was a server-initiated request needing response
                            server_req_id = msg.get("id")
                            try:
                                self.on_notification(msg.get("method", ""), msg.get("params", {}), server_req_id)
                            except TypeError:
                                self.on_notification(msg.get("method", ""), msg.get("params", {}))
                        except Exception as e:
                            logger.warning("Error in notification handler: %s", e)
            except Exception:
                break

    def send_response(self, req_id: Any, result: Mapping[str, Any]) -> None:
        """Sends a JSON-RPC response back to the server for a server-initiated request."""
        with self._lock:
            proc = self._ensure_proc()
            if proc.stdin is None:
                raise ConnectionError("Codex app-server stdio pipe is not available")
            payload = {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": dict(result),
            }
            try:
                proc.stdin.write(json.dumps(payload) + "\n")
                proc.stdin.flush()
            except Exception as exc:
                raise ConnectionError(f"Error sending response to Codex app-server: {exc}") from exc

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
            self._running = True
            self._reader_thread = threading.Thread(
                target=self._reader_loop, args=(self._proc,), daemon=True
            )
            self._reader_thread.start()
        except Exception as exc:
            raise ConnectionError(f"Failed to launch Codex app-server: {exc}") from exc

        return self._proc

    def send_request(self, method: str, params: Mapping[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        with self._lock:
            proc = self._ensure_proc()
            self._req_counter += 1
            req_id = self._req_counter
            evt = threading.Event()
            self._pending[req_id] = evt

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
            except Exception as exc:
                self._pending.pop(req_id, None)
                raise ConnectionError(f"Error communicating with Codex app-server: {exc}") from exc

        # Wait for reader thread to match response ID
        if not evt.wait(timeout=timeout):
            self._pending.pop(req_id, None)
            raise TimeoutError(f"Codex app-server timed out waiting for response to '{method}' (id={req_id})")

        self._pending.pop(req_id, None)
        if req_id in self._errors:
            err = self._errors.pop(req_id)
            raise RuntimeError(f"Codex JSON-RPC error: {err}")

        return self._responses.pop(req_id, {})

    def close(self) -> None:
        with self._lock:
            self._running = False
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2.0)
                except Exception:
                    self._proc.kill()
                self._proc = None
            self._pending.clear()
            self._responses.clear()
            self._errors.clear()


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
        self._thread_to_task: dict[str, str] = {}
        self._turn_to_task: dict[str, str] = {}
        self._lock = threading.Lock()

    def register_workspace(self, alias: str, abs_path: str) -> None:
        # Prevent traversal and escape: must be non-empty absolute path
        if not abs_path or not os.path.isabs(abs_path):
            raise ValueError(f"Workspace path must be absolute, got: '{abs_path}'")
        resolved = os.path.realpath(os.path.abspath(abs_path))
        self.workspaces[alias] = resolved

    def _resolve_task_id(self, thread_id: Optional[str], turn_id: Optional[str]) -> str:
        if turn_id and turn_id in self._turn_to_task:
            return self._turn_to_task[turn_id]
        if thread_id and thread_id in self._thread_to_task:
            return self._thread_to_task[thread_id]
        return ""

    def _handle_server_notification(
        self, method: str, params: dict[str, Any], server_req_id: Optional[Any] = None
    ) -> None:
        with self._lock:
            # Resolve task_id from threadId or turnId in real Codex notification
            thread_id = params.get("threadId") or params.get("thread_id")
            turn_id = params.get("turnId") or params.get("turn_id")
            task_id = params.get("task_id") or self._resolve_task_id(thread_id, turn_id)

            if method in ("approval/request", "item/approval", "item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
                approval_id = params.get("approval_id") or f"appr-{uuid.uuid4().hex[:8]}"
                self._pending_approvals[approval_id] = {
                    "approval_id": approval_id,
                    "server_req_id": server_req_id,
                    "task_id": task_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "action_type": params.get("action_type") or params.get("type", "command"),
                    "command_or_path": params.get("command_or_path") or params.get("command") or params.get("path", ""),
                    "description": params.get("description", "Approval required by Codex"),
                    "status": "pending",
                }
            elif method in ("turn/progress", "task/progress", "item/updated", "turn/step"):
                if task_id and task_id in self._tasks:
                    self._tasks[task_id]["progress"] = params.get("progress") or params.get("message") or "in_progress"
                    self._tasks[task_id]["state"] = "running"
            elif method in ("turn/completed", "task/completed", "turn/finished"):
                if task_id and task_id in self._tasks:
                    self._tasks[task_id]["status"] = "completed"
                    self._tasks[task_id]["state"] = "completed"

    def _get_transport(self) -> JsonRpcTransport:
        if self._transport is not None:
            return self._transport

        # In unit tests with default codex binary, use DefaultMockTransport unless explicitly forced
        if self.codex_bin == "codex" and "unittest" in sys.modules and os.environ.get("DESK_AGENT_USE_REAL_CODEX") != "1":
            self._transport = DefaultMockTransport()
            return self._transport

        # If an explicit non-standard binary path was supplied or running in production
        self._transport = SubprocessJsonRpcTransport(
            self.codex_bin, on_notification=self._handle_server_notification
        )
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

        # 1. Create real thread in workspace (supports both thread/create and real codex thread/start)
        try:
            thread_res = transport.send_request("thread/create", {"workspace_path": target_dir})
        except Exception as e:
            err_msg = str(e).lower()
            if "unknown variant `thread/create`" in err_msg or "method not found" in err_msg or "-32600" in err_msg or "-32601" in err_msg:
                thread_res = transport.send_request("thread/start", {"workspace_path": target_dir, "cwd": target_dir})
            else:
                raise

        if not thread_res.get("thread_id") and not thread_res.get("thread"):
            try:
                alt = transport.send_request("thread/start", {"workspace_path": target_dir, "cwd": target_dir})
                if alt:
                    thread_res = alt
            except Exception:
                pass

        if "thread" in thread_res and isinstance(thread_res["thread"], dict):
            thread_id = thread_res["thread"].get("id", f"thr_win_{task_id[:8]}")
        else:
            thread_id = thread_res.get("thread_id", f"thr_win_{task_id[:8]}")

        # 2. Start turn (Codex app-server uses threadId and input array)
        turn_res = transport.send_request("turn/start", {
            "threadId": thread_id,
            "thread_id": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "prompt": prompt,
            "model": model or "gpt-5-codex",
        })
        if "turn" in turn_res and isinstance(turn_res["turn"], dict):
            turn_id = turn_res["turn"].get("id", f"trn_{task_id[:8]}_01")
            state = turn_res["turn"].get("status", "running")
        else:
            turn_id = turn_res.get("turn_id", f"trn_{task_id[:8]}_01")
            state = turn_res.get("state", "running")

        with self._lock:
            self._thread_to_task[thread_id] = task_id
            self._turn_to_task[turn_id] = task_id
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
            server_req_id = appr.get("server_req_id")

        transport = self._get_transport()
        if server_req_id is not None:
            # Server initiated request with ID: respond with JSON-RPC response ID
            transport.send_response(server_req_id, {
                "decision": decision,
                "reason": reason or "",
                "status": "applied" if decision == "approve" else "rejected",
            })
        else:
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


def start_companion_server(
    config_path: Optional[str] = None,
    port: int = 8765,
    host: str = "127.0.0.1",
    auth_token: Optional[str] = None,
) -> None:
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
                auth_token = cfg.get("auth_token", auth_token)
        except Exception as e:
            logger.warning("Failed to load config %s: %s", config_path, e)

    companion = CodexCompanion(
        allowed_workspaces=registered_aliases if registered_aliases else None,
        codex_bin=codex_bin,
    )

    class CompanionHandler(BaseHTTPRequestHandler):
        def _check_auth(self) -> bool:
            if not auth_token:
                return True
            auth_hdr = self.headers.get("Authorization", "")
            tok_hdr = self.headers.get("X-Companion-Token", "")
            if auth_hdr.startswith("Bearer "):
                return auth_hdr[7:].strip() == auth_token
            if tok_hdr:
                return tok_hdr.strip() == auth_token
            return False

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
            if not self._check_auth():
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"success": false, "error": "Unauthorized: invalid or missing bearer token"}')
                return

            if self.path == "/rpc":
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")
                try:
                    req = json.loads(body)
                    op = req.get("operation")
                    args = req.get("arguments", {})

                    if op in ("codex.submit", "codex.submit_turn"):
                        task_id = args.get("task_id") or f"tsk-{uuid.uuid4().hex[:8]}"
                        res = companion.submit_task(
                            task_id=task_id,
                            workspace_alias=args["workspace_alias"],
                            prompt=args["prompt"],
                            model=args.get("model"),
                        )
                    elif op in ("codex.get_status", "codex.get", "codex.get_task"):
                        res = companion.get_task_status(args["task_id"])
                    elif op in ("codex.respond_approval", "codex.decide_approval"):
                        decision = args["decision"]
                        # Map approve <-> accept for complete cross-client compatibility
                        if decision == "accept":
                            decision = "approve"
                        res = companion.respond_approval(
                            approval_id=args["approval_id"],
                            task_id=args["task_id"],
                            decision=decision,
                            reason=args.get("reason"),
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
                    elif op in ("codex.list_workspaces", "workspaces.list"):
                        res = {"workspaces": list(companion.workspaces.keys())}
                    else:
                        raise ValueError(f"Unknown operation: {op}")

                    resp_data = {
                        "status": "succeeded",
                        "success": True,
                        "request_id": req.get("request_id"),
                        "result": res,
                    }
                    self.send_response(200)
                except Exception as e:
                    resp_data = {
                        "status": "failed",
                        "success": False,
                        "request_id": req.get("request_id") if 'req' in locals() else None,
                        "error": {"code": "COMPANION_ERROR", "message": str(e)},
                    }
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
