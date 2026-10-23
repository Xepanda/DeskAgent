"""
Desk Agent R1 Windows Codex Companion.
Runs on Windows host alongside Codex CLI / app-server.

Features:
- Workspace alias protection and directory escape validation.
- Subprocess management driving `codex app-server` over stdio JSON-RPC.
- Thread and Turn lifecycle management (initialize, thread/create, turn/start, interrupt, append).
- Strict human-in-the-loop approval correlation and safety gating.
- Handles server-initiated approval requests (with server_req_id matching).
- Solves race conditions where approvals arrive before task/turn mapping completes.
- Injectable JSON-RPC transport for test isolation and offline verification.
- HTTP RPC daemon serving /health and /rpc for cross-machine host communication with Bearer auth.
  Startup is fail-closed (interfaces.md §6.5 / WP5): the default listener is loopback, and a
  non-loopback host requires an explicitly configured strong shared token, otherwise the
  Companion refuses to start. The Codex app-server itself is never exposed over the network.
- RemoteCodexCompanionClient for client-side consumption over LAN.
"""

from __future__ import annotations

import abc
import hmac
import ipaddress
import json
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Any, Callable, Mapping, Optional, Union

logger = logging.getLogger(__name__)


def _safe_task_summary(candidate: Any, prompt: Any, fallback: str) -> str:
    """Normalize a task summary and redact any echoed original prompt."""
    if isinstance(candidate, dict):
        candidate = candidate.get("summary") or candidate.get("text") or candidate.get("message")
    summary = str(candidate or "").strip()
    prompt_text = str(prompt or "").strip()
    if prompt_text and prompt_text in summary:
        summary = summary.replace(prompt_text, "[user request omitted]").strip()
    return summary or fallback

KNOWN_DECISION_KEYWORDS = {
    "accept", "acceptforsession", "decline", "cancel", "approved", "denied", "abort", "approve", "reject"
}


def normalize_decision_for_schema(action_type: str, decision: Any, reason: str = "") -> Any:
    """Aligns decision with codex-cli 0.161.0 schema specification.

    - CommandExecutionRequestApprovalResponse: "accept" | "acceptForSession" | "decline" | "cancel"
    - ApplyPatchApprovalResponse: "approved" | {"denied": {"rejection": ...}} | "abort"
    """
    is_patch = action_type in ("file_change", "apply_patch", "patch", "file")

    if is_patch:
        if isinstance(decision, dict):
            return decision
        dec_str = str(decision).strip()
        if dec_str in ("approved", "approve", "accept"):
            return "approved"
        elif dec_str in ("abort", "cancel"):
            return "abort"
        elif dec_str in ("denied", "reject", "decline"):
            return {"denied": {"rejection": reason or ""}}
        else:
            if "approve" in dec_str.lower() or "accept" in dec_str.lower():
                return "approved"
            return {"denied": {"rejection": reason or ""}}
    else:
        if isinstance(decision, dict):
            decision = decision.get("decision", "accept")
        dec_str = str(decision).strip()
        if dec_str in ("accept", "acceptForSession", "cancel", "decline"):
            return dec_str
        elif dec_str == "approve":
            return "accept"
        elif dec_str in ("reject", "denied"):
            return "decline"
        elif dec_str == "abort":
            return "cancel"
        else:
            if "approve" in dec_str.lower() or "accept" in dec_str.lower():
                return "accept"
            return "decline"


def _resolve_approval_args(
    task_id: Optional[str],
    decision: Optional[Union[str, dict[str, Any]]],
    reason: Optional[str],
) -> tuple[Optional[str], Any, Optional[str]]:
    """Flexibly handles (approval_id, task_id, decision, reason) and (approval_id, decision, reason)."""
    if decision is None:
        return None, task_id, reason
    if isinstance(task_id, (str, dict)):
        if isinstance(task_id, dict) or (isinstance(task_id, str) and task_id.lower() in KNOWN_DECISION_KEYWORDS):
            if isinstance(decision, str) and decision.lower() not in KNOWN_DECISION_KEYWORDS:
                return None, task_id, decision
    return task_id, decision, reason


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
        self._lock = threading.RLock()
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
                encoding="utf-8",
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
    _RECOVERY_RETRY_BASE_SEC = 1.0
    _RECOVERY_RETRY_MAX_SEC = 30.0

    def __init__(
        self,
        allowed_workspaces: Optional[dict[str, str]] = None,
        registered_workspaces: Optional[dict[str, str]] = None,
        codex_bin: str = "codex",
        transport: Optional[JsonRpcTransport] = None,
        db_path: Optional[str] = None,
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
        self._last_recovery_counts = {"reconnected": 0, "reconciled": 0, "pending": 0}
        self._recovery_retry_deadlines: dict[str, float] = {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._pending_approvals: dict[str, dict[str, Any]] = {}
        self._thread_to_task: dict[str, str] = {}
        self._turn_to_task: dict[str, str] = {}
        self._lock = threading.RLock()
        self._initialize_lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._event_listeners: list[Callable[[str, dict[str, Any]], None]] = []

        if db_path is None:
            if "unittest" in sys.modules and os.environ.get("DESK_AGENT_USE_REAL_CODEX") != "1":
                self.db_path = ":memory:"
            else:
                base_dir = os.path.expanduser("~/.deskagent")
                os.makedirs(base_dir, exist_ok=True)
                self.db_path = os.path.join(base_dir, "codex_companion.db")
        else:
            if db_path != ":memory:":
                parent = os.path.dirname(os.path.abspath(db_path))
                if parent:
                    os.makedirs(parent, exist_ok=True)
            self.db_path = db_path

        self._conn: Optional[sqlite3.Connection] = None
        self._init_db()
        self._load_from_db()

    def add_event_listener(self, cb: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            if cb not in self._event_listeners:
                self._event_listeners.append(cb)

    @staticmethod
    def _safe_terminal_summary(task: dict[str, Any], candidate: Any, fallback: str) -> str:
        """Return terminal text without echoing the user's original prompt."""
        return _safe_task_summary(candidate, task.get("prompt"), fallback)

    def _dispatch_event(self, event_type: str, data: dict[str, Any]) -> None:
        with self._lock:
            listeners = list(self._event_listeners)
        for cb in listeners:
            try:
                cb(event_type, data)
            except Exception as err:
                logger.warning("Codex event listener error: %s", err)

    def _get_connection(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, check_same_thread=False)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA foreign_keys = ON;")
            if self.db_path != ":memory:":
                self._conn.execute("PRAGMA journal_mode = WAL;")
        return self._conn

    def _init_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS tasks (
                task_id TEXT PRIMARY KEY,
                thread_id TEXT,
                turn_id TEXT,
                status TEXT NOT NULL DEFAULT 'running',
                alias TEXT NOT NULL,
                cwd TEXT NOT NULL,
                prompt TEXT,
                model TEXT,
                summary TEXT,
                last_output TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS approvals (
                approval_id TEXT PRIMARY KEY,
                task_id TEXT,
                thread_id TEXT,
                turn_id TEXT,
                request_data TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                decision TEXT,
                created_at TEXT NOT NULL
            );
            """)
            conn.commit()
            for col in ("summary", "last_output"):
                try:
                    conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
                except sqlite3.OperationalError:
                    pass
            conn.commit()

    def _load_from_db(self) -> None:
        with self._lock:
            conn = self._get_connection()
            cur = conn.cursor()

            # Load tasks
            cur.execute("SELECT task_id, thread_id, turn_id, status, alias, cwd, prompt, model, summary, last_output, created_at, updated_at FROM tasks")
            for row in cur.fetchall():
                tid = row["task_id"]
                th_id = row["thread_id"] or ""
                tu_id = row["turn_id"] or ""
                self._tasks[tid] = {
                    "task_id": tid,
                    "workspace_alias": row["alias"],
                    "target_dir": row["cwd"],
                    "thread_id": th_id,
                    "turn_id": tu_id,
                    "prompt": row["prompt"] or "",
                    "model": row["model"] or None,
                    "summary": row["summary"] or "",
                    "last_output": row["last_output"] or "",
                    "state": row["status"],
                    "status": row["status"],
                    "created_at": row["created_at"],
                    "updated_at": row["updated_at"],
                }
                if th_id:
                    self._thread_to_task[th_id] = tid
                if tu_id:
                    self._turn_to_task[tu_id] = tid

            # Load approvals
            cur.execute("SELECT approval_id, task_id, thread_id, turn_id, request_data, status, decision, created_at FROM approvals")
            for row in cur.fetchall():
                aid = row["approval_id"]
                req_data: dict[str, Any] = {}
                if row["request_data"]:
                    try:
                        req_data = json.loads(row["request_data"])
                    except Exception:
                        pass

                dec_val = row["decision"]
                if dec_val and isinstance(dec_val, str) and dec_val.startswith("{"):
                    try:
                        dec_val = json.loads(dec_val)
                    except Exception:
                        pass

                task_id = row["task_id"] or req_data.get("task_id", "")
                thread_id = row["thread_id"] or req_data.get("thread_id", "")
                turn_id = row["turn_id"] or req_data.get("turn_id", "")

                self._pending_approvals[aid] = {
                    "approval_id": aid,
                    "server_req_id": req_data.get("server_req_id"),
                    "task_id": task_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "action_type": req_data.get("action_type", "command"),
                    "command_or_path": req_data.get("command_or_path", ""),
                    "description": req_data.get("description", ""),
                    "status": row["status"],
                    "decision": dec_val,
                    "reason": req_data.get("reason", ""),
                    "created_at": row["created_at"],
                }

    def _persist_task(self, task: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        created_at = task.get("created_at") or now
        task["created_at"] = created_at
        task["updated_at"] = now
        with self._lock:
            conn = self._get_connection()
            conn.execute(
                """
                INSERT INTO tasks (task_id, thread_id, turn_id, status, alias, cwd, prompt, model, summary, last_output, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET
                    thread_id=excluded.thread_id,
                    turn_id=excluded.turn_id,
                    status=excluded.status,
                    alias=excluded.alias,
                    cwd=excluded.cwd,
                    prompt=excluded.prompt,
                    model=excluded.model,
                    summary=excluded.summary,
                    last_output=excluded.last_output,
                    updated_at=excluded.updated_at
                """,
                (
                    task["task_id"],
                    task.get("thread_id", ""),
                    task.get("turn_id", ""),
                    task.get("status") or task.get("state", "running"),
                    task.get("workspace_alias", ""),
                    task.get("target_dir", ""),
                    task.get("prompt", ""),
                    task.get("model", ""),
                    task.get("summary", ""),
                    task.get("last_output", ""),
                    created_at,
                    now,
                ),
            )
            conn.commit()

    def _persist_approval(self, appr: dict[str, Any]) -> None:
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        created_at = appr.get("created_at") or now
        appr["created_at"] = created_at
        req_data = {
            "action_type": appr.get("action_type", ""),
            "command_or_path": appr.get("command_or_path", ""),
            "description": appr.get("description", ""),
            "server_req_id": appr.get("server_req_id"),
            "reason": appr.get("reason", ""),
        }
        decision_val = appr.get("decision")
        if isinstance(decision_val, dict):
            decision_str = json.dumps(decision_val)
        else:
            decision_str = str(decision_val) if decision_val is not None else None

        with self._lock:
            conn = self._get_connection()
            conn.execute(
                """
                INSERT INTO approvals (approval_id, task_id, thread_id, turn_id, request_data, status, decision, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(approval_id) DO UPDATE SET
                    task_id=excluded.task_id,
                    thread_id=excluded.thread_id,
                    turn_id=excluded.turn_id,
                    request_data=excluded.request_data,
                    status=excluded.status,
                    decision=excluded.decision
                """,
                (
                    appr["approval_id"],
                    appr.get("task_id", ""),
                    appr.get("thread_id", ""),
                    appr.get("turn_id", ""),
                    json.dumps(req_data),
                    appr.get("status", "pending"),
                    decision_str,
                    created_at,
                ),
            )
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._transport is not None:
                try:
                    self._transport.close()
                except Exception:
                    pass
            if self._conn is not None:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None

    def register_workspace(self, alias: str, abs_path: str) -> None:
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
            thread_id = params.get("threadId") or params.get("thread_id")
            turn_id = params.get("turnId") or params.get("turn_id")
            task_id = params.get("task_id") or self._resolve_task_id(thread_id, turn_id)
            if task_id and task_id in self._tasks:
                current_task = self._tasks[task_id]
                if thread_id and not current_task.get("thread_id"):
                    current_task["thread_id"] = thread_id
                if turn_id:
                    current_task["turn_id"] = turn_id
                    self._turn_to_task[turn_id] = task_id
                self._persist_task(current_task)

            if method in (
                "approval/request",
                "item/approval",
                "item/commandExecution/requestApproval",
                "item/fileChange/requestApproval",
            ):
                approval_id = params.get("approval_id") or params.get("itemId") or f"appr-{uuid.uuid4().hex[:8]}"
                if "commandExecution" in method:
                    default_type = "command"
                elif "fileChange" in method:
                    default_type = "file_change"
                else:
                    default_type = "command"
                action_type = params.get("action_type") or params.get("type") or default_type
                command_or_path = params.get("command_or_path") or params.get("command") or params.get("path") or ""
                desc = params.get("description")
                if not desc:
                    if action_type == "command":
                        desc = f"Execute command: {command_or_path}" if command_or_path else "Approval required for command execution"
                    else:
                        desc = f"File change: {command_or_path}" if command_or_path else "Approval required for file modification"

                appr_dict = {
                    "approval_id": approval_id,
                    "server_req_id": server_req_id,
                    "task_id": task_id,
                    "thread_id": thread_id,
                    "turn_id": turn_id,
                    "action_type": action_type,
                    "command_or_path": command_or_path,
                    "description": desc,
                    "status": "pending",
                }
                self._pending_approvals[approval_id] = appr_dict
                self._persist_approval(appr_dict)
                if task_id and task_id in self._tasks:
                    self._tasks[task_id]["status"] = "approval_pending"
                    self._tasks[task_id]["state"] = "approval_pending"
                    self._persist_task(self._tasks[task_id])
            elif method in ("turn/progress", "task/progress", "item/updated", "turn/step", "item/created", "item/completed"):
                if task_id and task_id in self._tasks:
                    self._tasks[task_id]["progress"] = params.get("progress") or params.get("message") or "in_progress"
                    if not self.get_pending_approvals(task_id):
                        self._tasks[task_id]["state"] = "running"
                        self._tasks[task_id]["status"] = "running"
                    item = params.get("item") if isinstance(params.get("item"), dict) else {}
                    item_type = item.get("type")
                    item_role = item.get("role") or item.get("author")
                    is_user_message = item_type == "userMessage" or item_role == "user"
                    is_assistant_message = (
                        item_type in ("agentMessage", "assistantMessage")
                        or item_role in ("assistant", "agent")
                    )
                    output_text = None
                    if not is_user_message and is_assistant_message:
                        output_text = item.get("text") or item.get("formatted")
                        if not output_text:
                            content = item.get("content")
                            if isinstance(content, str):
                                output_text = content
                            elif isinstance(content, list):
                                text_parts = [
                                    part.get("text", "")
                                    for part in content
                                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                                ]
                                output_text = "".join(text_parts).strip() or None
                    if output_text:
                        self._tasks[task_id]["last_output"] = str(output_text).strip()
                    self._persist_task(self._tasks[task_id])
            elif method in ("turn/completed", "task/completed", "turn/finished"):
                if task_id and task_id in self._tasks:
                    turn = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                    raw_status = str(turn.get("status") or params.get("status") or "").lower()
                    error_info = params.get("error") or turn.get("error")
                    if raw_status in ("failed", "error") or error_info is not None:
                        self._tasks[task_id]["status"] = "failed"
                        self._tasks[task_id]["state"] = "failed"
                        self._persist_task(self._tasks[task_id])
                        summary = params.get("summary") or turn.get("summary")
                        if not summary:
                            if isinstance(error_info, dict):
                                summary = error_info.get("message")
                            elif error_info:
                                summary = str(error_info)
                            else:
                                summary = f"Task {task_id} failed"
                        summary = self._safe_terminal_summary(
                            self._tasks[task_id], summary, f"Task {task_id} failed"
                        )
                        self._tasks[task_id]["summary"] = summary
                        self._tasks[task_id]["last_output"] = summary
                        self._dispatch_event("codex.failed", {
                            "task_id": task_id,
                            "thread_id": self._tasks[task_id].get("thread_id", ""),
                            "turn_id": self._tasks[task_id].get("turn_id", ""),
                            "status": "failed",
                            "summary": summary,
                        })
                        self._persist_task(self._tasks[task_id])
                    elif raw_status in ("interrupted", "cancelled"):
                        terminal_status = "cancelled" if raw_status == "cancelled" else "interrupted"
                        self._tasks[task_id]["status"] = terminal_status
                        self._tasks[task_id]["state"] = terminal_status
                        summary = self._safe_terminal_summary(
                            self._tasks[task_id],
                            params.get("summary") or turn.get("summary"),
                            f"Task {task_id} {terminal_status}",
                        )
                        self._tasks[task_id]["summary"] = summary
                        self._tasks[task_id]["last_output"] = summary
                        self._persist_task(self._tasks[task_id])
                        self._dispatch_event("codex.interrupted", {
                            "task_id": task_id,
                            "thread_id": self._tasks[task_id].get("thread_id", ""),
                            "turn_id": self._tasks[task_id].get("turn_id", ""),
                            "status": terminal_status,
                            "summary": summary,
                        })
                    elif raw_status not in ("completed", "succeeded"):
                        # A terminal notification without a recognized success
                        # status is not proof that the turn completed. Match the
                        # restart-recovery path: preserve the current task state
                        # and reconcile against the app-server before reporting.
                        self._set_recovery_pending(task_id, "unknown_turn_status")
                        logger.warning(
                            "Codex task %s received unrecognized terminal status %r; keeping it pending",
                            task_id,
                            raw_status or None,
                        )
                    else:
                        assistant_output = (
                            self._assistant_text_from_items(turn.get("items"))
                            or self._tasks[task_id].get("last_output")
                        )
                        summary = assistant_output or "任务已成功完成"
                        if isinstance(summary, dict):
                            summary = summary.get("summary") or summary.get("text") or summary.get("message") or "任务已成功完成"
                        summary = self._safe_terminal_summary(
                            self._tasks[task_id],
                            summary,
                            self._tasks[task_id].get("last_output") or "任务已成功完成",
                        )
                        self._tasks[task_id]["status"] = "completed"
                        self._tasks[task_id]["state"] = "completed"
                        self._tasks[task_id]["summary"] = summary
                        self._tasks[task_id]["last_output"] = assistant_output or ""
                        self._persist_task(self._tasks[task_id])
                        self._dispatch_event("codex.turn_completed", {
                            "task_id": task_id,
                            "thread_id": self._tasks[task_id].get("thread_id", ""),
                            "turn_id": self._tasks[task_id].get("turn_id", ""),
                            "status": "completed",
                            "summary": summary,
                        })

    def _get_transport(self) -> JsonRpcTransport:
        if self._transport is not None:
            return self._transport

        if self.codex_bin == "codex" and "unittest" in sys.modules and os.environ.get("DESK_AGENT_USE_REAL_CODEX") != "1":
            self._transport = DefaultMockTransport()
            return self._transport

        self._transport = SubprocessJsonRpcTransport(
            self.codex_bin, on_notification=self._handle_server_notification
        )
        return self._transport

    def initialize(self) -> dict[str, Any]:
        """Performs JSON-RPC initialize handshake with Codex app-server."""
        with self._initialize_lock:
            with self._lock:
                initialized = self._initialized
            if initialized:
                return {"capabilities": {}}

            transport = self._get_transport()
            resp = transport.send_request(
                "initialize",
                {
                    "clientInfo": {"name": "desk-agent-companion", "version": "0.1.0"},
                    "capabilities": {},
                },
            )
            if resp is None or (isinstance(resp, dict) and "error" in resp):
                raise ConnectionError(f"Codex app-server initialization failed: {resp}")
            with self._lock:
                self._initialized = True

        # Do not hold the state lock across RPCs: thread/resume may emit
        # notifications before its response, and the transport reader must be
        # able to correlate those notifications while this request is pending.
        self._last_recovery_counts = self._recover_pending_tasks()
        return resp

    def recover_pending_tasks(self) -> dict[str, int]:
        """Reconnects persisted non-terminal tasks to their app-server threads.

        Resume errors, including active-writer conflicts, leave the original
        task status untouched and mark recovery pending so callers can retry.
        """
        if not self._initialized:
            self.initialize()
            return dict(self._last_recovery_counts)

        self._last_recovery_counts = self._recover_pending_tasks()
        return dict(self._last_recovery_counts)

    def _recover_pending_tasks(self) -> dict[str, int]:
        counts = {"reconnected": 0, "reconciled": 0, "pending": 0}
        # Keep legacy persisted ``cancelled`` tasks terminal for compatibility.
        # New cancellation requests use ``cancelling`` until the server reports
        # an authoritative interrupted terminal turn.
        terminal_statuses = {"completed", "failed", "interrupted", "cancelled", "succeeded", "error"}
        with self._recovery_lock:
            with self._lock:
                now = time.monotonic()
                active_tasks = [
                    task for task in self._tasks.values()
                    if str(task.get("status") or task.get("state") or "").lower() not in terminal_statuses
                ]
                candidates = [
                    dict(task)
                    for task in active_tasks
                    if task.get("recovery_status") in (None, "pending")
                    and self._recovery_retry_deadlines.get(task["task_id"], 0.0) <= now
                ]
                counts["pending"] = sum(
                    1 for task in active_tasks
                    if task.get("recovery_status") == "pending"
                    and self._recovery_retry_deadlines.get(task["task_id"], 0.0) > now
                )

            transport = self._get_transport()
            for task_snapshot in candidates:
                task_id = task_snapshot["task_id"]
                thread_id = task_snapshot.get("thread_id")
                turn_id = task_snapshot.get("turn_id")
                with self._lock:
                    task = self._tasks.get(task_id)
                    if task is None:
                        continue
                    task["recovery_status"] = "pending"
                    task["recovery_attempts"] = int(task.get("recovery_attempts", 0)) + 1
                    task.pop("recovery_error_kind", None)

                if not thread_id:
                    self._set_recovery_pending(task_id, "missing_thread_id")
                    counts["pending"] += 1
                    continue

                active_writer_snapshot = False

                try:
                    response = transport.send_request("thread/resume", {
                        "threadId": thread_id,
                        "cwd": task_snapshot.get("target_dir") or None,
                        "excludeTurns": False,
                    })
                except Exception as exc:
                    error_text = str(exc).lower()
                    error_kind = "active_writer" if "already has an active writer" in error_text else "resume_error"
                    if error_kind == "active_writer":
                        response = self._read_thread_snapshot(transport, thread_id)
                        active_writer_snapshot = response is not None
                    else:
                        response = None
                    if response is None:
                        self._set_recovery_pending(task_id, error_kind)
                        counts["pending"] += 1
                        logger.warning("Codex task %s thread recovery remains pending (%s)", task_id, error_kind)
                        continue

                if isinstance(response, dict) and isinstance(response.get("error"), dict):
                    error_text = str(response["error"].get("message") or "").lower()
                    error_kind = "active_writer" if "already has an active writer" in error_text else "resume_error"
                    if error_kind == "active_writer":
                        response = self._read_thread_snapshot(transport, thread_id)
                        active_writer_snapshot = response is not None
                    else:
                        response = None
                    if response is None:
                        self._set_recovery_pending(task_id, error_kind)
                        counts["pending"] += 1
                        logger.warning("Codex task %s thread recovery remains pending (%s)", task_id, error_kind)
                        continue

                thread = response.get("thread") if isinstance(response, dict) else None
                if not isinstance(thread, dict):
                    self._set_recovery_pending(task_id, "missing_thread")
                    counts["pending"] += 1
                    continue

                resumed_thread_id = thread.get("id") or thread_id
                if resumed_thread_id != thread_id:
                    self._set_recovery_pending(task_id, "thread_mismatch")
                    counts["pending"] += 1
                    continue

                with self._lock:
                    current = self._tasks.get(task_id)
                    if current is None:
                        continue
                    self._thread_to_task[thread_id] = task_id

                turns = thread.get("turns")
                if not isinstance(turns, list):
                    self._set_recovery_pending(task_id, "missing_turns")
                    counts["pending"] += 1
                    continue

                matching_turn = next((
                    turn for turn in turns
                    if isinstance(turn, dict) and turn_id and turn.get("id") == turn_id
                ), None)
                if matching_turn is None and not turn_id and str(task_snapshot.get("status")).lower() == "start_unknown":
                    # A timed-out turn/start has no returned turn ID. Threads
                    # are created per Companion task, so their newest turn is
                    # the authoritative candidate for this task.
                    matching_turn = next((turn for turn in reversed(turns) if isinstance(turn, dict) and turn.get("id")), None)
                if matching_turn is None:
                    self._set_recovery_pending(task_id, "turn_not_found")
                    counts["pending"] += 1
                    continue

                resolved_turn_id = str(matching_turn.get("id") or turn_id)
                with self._lock:
                    current = self._tasks.get(task_id)
                    if current is not None and not current.get("turn_id"):
                        current["turn_id"] = resolved_turn_id
                        self._persist_task(current)
                    self._turn_to_task[resolved_turn_id] = task_id
                turn_status = str(matching_turn.get("status") or "").lower()

                with self._lock:
                    current = self._tasks.get(task_id)
                    current_status = str((current or {}).get("status") or "").lower()
                if current_status in terminal_statuses:
                    # A notification may arrive before the resume RPC response.
                    # Keep its authoritative result and avoid duplicate events.
                    with self._lock:
                        current = self._tasks.get(task_id)
                        if current is not None:
                            self._recovery_retry_deadlines.pop(task_id, None)
                            current["recovery_status"] = "reconciled"
                            current.pop("recovery_error_kind", None)
                            self._persist_task(current)
                    counts["reconciled"] += 1
                    continue

                if turn_status == "inprogress":
                    if active_writer_snapshot:
                        # thread/read is observational only. It cannot subscribe
                        # this Companion to the other writer's notifications or
                        # transfer thread ownership, so keep polling snapshots
                        # with backoff until an authoritative terminal turn is
                        # visible (or thread/resume becomes available).
                        self._set_recovery_pending(task_id, "active_writer")
                        counts["pending"] += 1
                        continue
                    with self._lock:
                        current = self._tasks.get(task_id)
                        if current is not None:
                            self._recovery_retry_deadlines.pop(task_id, None)
                            has_pending_approval = any(
                                appr.get("task_id") == task_id and appr.get("status") == "pending"
                                for appr in self._pending_approvals.values()
                            )
                            current["state"] = "approval_pending" if has_pending_approval else "running"
                            current["status"] = current["state"]
                            current["recovery_status"] = "reconnected"
                            current.pop("recovery_error_kind", None)
                            self._persist_task(current)
                    counts["reconnected"] += 1
                    continue

                if turn_status not in {"completed", "failed", "interrupted", "cancelled"}:
                    self._set_recovery_pending(task_id, "unknown_turn_status")
                    counts["pending"] += 1
                    continue

                terminal_turn = dict(matching_turn)
                assistant_text = self._assistant_text_from_items(terminal_turn.get("items"))
                params: dict[str, Any] = {"threadId": thread_id, "turnId": resolved_turn_id, "turn": terminal_turn}
                if assistant_text:
                    params["summary"] = assistant_text
                self._handle_server_notification("turn/completed", params)
                with self._lock:
                    current = self._tasks.get(task_id)
                    if current is not None:
                        self._recovery_retry_deadlines.pop(task_id, None)
                        current["recovery_status"] = "reconciled"
                        current.pop("recovery_error_kind", None)
                        self._persist_task(current)
                counts["reconciled"] += 1

        return counts

    @staticmethod
    def _read_thread_snapshot(transport: JsonRpcTransport, thread_id: str) -> Optional[dict[str, Any]]:
        """Reads persisted thread state without claiming its active writer lease."""
        try:
            response = transport.send_request("thread/read", {"threadId": thread_id, "includeTurns": True})
        except Exception:
            return None
        if not isinstance(response, dict) or isinstance(response.get("error"), dict):
            return None
        thread = response.get("thread")
        if not isinstance(thread, dict) or not isinstance(thread.get("turns"), list):
            return None
        return response

    def _set_recovery_pending(self, task_id: str, error_kind: str) -> None:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return
            task["recovery_status"] = "pending"
            task["recovery_error_kind"] = error_kind
            attempts = max(1, int(task.get("recovery_attempts", 1)))
            delay = min(
                self._RECOVERY_RETRY_BASE_SEC * (2 ** min(attempts - 1, 10)),
                self._RECOVERY_RETRY_MAX_SEC,
            )
            self._recovery_retry_deadlines[task_id] = time.monotonic() + delay
            # Persist the task's existing status and output without translating
            # a reconnect problem into a task failure or completion.
            self._persist_task(task)

    @staticmethod
    def _assistant_text_from_items(items: Any) -> Optional[str]:
        if not isinstance(items, list):
            return None
        for item in reversed(items):
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            role = item.get("role") or item.get("author")
            if item_type == "userMessage" or role == "user":
                continue
            if not (
                item_type in ("agentMessage", "assistantMessage")
                or role in ("assistant", "agent")
            ):
                continue
            text = item.get("text") or item.get("formatted")
            if not text:
                content = item.get("content")
                if isinstance(content, str):
                    text = content
                elif isinstance(content, list):
                    parts = [
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict) and isinstance(part.get("text"), str)
                    ]
                    text = "".join(parts).strip()
            if text:
                return str(text).strip()
        return None

    def _ensure_initialized(self) -> None:
        if not self._initialized:
            self.initialize()

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

        # Bind thread_id immediately before turn/start so approvals during turn/start resolve task_id
        with self._lock:
            self._thread_to_task[thread_id] = task_id
            for appr in self._pending_approvals.values():
                if not appr.get("task_id") and appr.get("thread_id") == thread_id:
                    appr["task_id"] = task_id

            # Persist the thread association before turn/start. The app-server
            # may create a turn and then time out before returning its ID; the
            # durable thread ID lets startup recovery reconcile that outcome.
            task_dict = {
                "task_id": task_id,
                "workspace_alias": workspace_alias,
                "target_dir": target_dir,
                "thread_id": thread_id,
                "turn_id": "",
                "prompt": prompt,
                "model": model,
                "state": "starting",
                "status": "starting",
            }
            self._tasks[task_id] = task_dict
            pending_approvals = [
                appr for appr in self._pending_approvals.values()
                if appr.get("task_id") == task_id and appr.get("status") == "pending"
            ]
            if pending_approvals:
                task_dict["state"] = "approval_pending"
                task_dict["status"] = "approval_pending"
                for appr in pending_approvals:
                    self._persist_approval(appr)
            self._persist_task(task_dict)

        # 2. Start turn (Codex app-server uses threadId and input array)
        turn_params = {
            "threadId": thread_id,
            "thread_id": thread_id,
            "input": [{"type": "text", "text": prompt}],
            "prompt": prompt,
        }
        if model is not None:
            turn_params["model"] = model
        try:
            turn_res = transport.send_request("turn/start", turn_params)
        except Exception:
            # The request may have reached app-server even when its response
            # did not. Preserve the task and thread so resume can discover the
            # actual turn; do not report a false terminal failure.
            with self._lock:
                # A progress/terminal notification can beat the timed-out
                # response. Preserve its authoritative state and turn ID.
                if task_dict.get("status") == "starting":
                    task_dict["state"] = "start_unknown"
                    task_dict["status"] = "start_unknown"
                    task_dict["recovery_status"] = "pending"
                    task_dict["recovery_error_kind"] = "turn_start_outcome_unknown"
                self._persist_task(task_dict)
            raise
        if "turn" in turn_res and isinstance(turn_res["turn"], dict):
            turn_id = turn_res["turn"].get("id", f"trn_{task_id[:8]}_01")
            state = turn_res["turn"].get("status", "running")
        else:
            turn_id = turn_res.get("turn_id", f"trn_{task_id[:8]}_01")
            state = turn_res.get("state", "running")

        with self._lock:
            self._turn_to_task[turn_id] = task_id
            for appr in self._pending_approvals.values():
                if not appr.get("task_id") and (appr.get("turn_id") == turn_id or appr.get("thread_id") == thread_id):
                    appr["task_id"] = task_id
                    self._persist_approval(appr)
            task_dict["turn_id"] = turn_id
            # Notifications can be delivered synchronously before turn/start
            # returns. Keep their newer running or terminal state and output;
            # use the RPC state only while the task is still awaiting its
            # first notification.
            if task_dict.get("status") == "starting":
                task_dict["state"] = state
                task_dict["status"] = state
            task_dict.pop("recovery_error_kind", None)
            task_dict.pop("recovery_status", None)
            self._persist_task(task_dict)

        return {
            "task_id": task_id,
            "workspace_alias": workspace_alias,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "state": state,
        }

    def append_task(self, task_id: str, text: str) -> dict[str, Any]:
        self._ensure_initialized()
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(f"Task '{task_id}' not found")
            thread_id = task["thread_id"]

        transport = self._get_transport()
        transport.send_request("turn/append", {"thread_id": thread_id, "text": text})

        with self._lock:
            task["state"] = "running"
            task["status"] = "running"
            self._persist_task(task)
        return {"task_id": task_id, "appended": True}

    def cancel_task(self, task_id: str, reason: Optional[str] = None) -> dict[str, Any]:
        self._ensure_initialized()
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                raise KeyError(f"Task '{task_id}' not found")
            thread_id = task["thread_id"]
            turn_id = task["turn_id"]

        if not thread_id or not turn_id:
            self.recover_pending_tasks()
            with self._lock:
                task = self._tasks.get(task_id)
                thread_id = (task or {}).get("thread_id")
                turn_id = (task or {}).get("turn_id")
            if not thread_id or not turn_id:
                raise RuntimeError("Task startup is not reconciled yet; cancellation is unavailable")

        transport = self._get_transport()
        params: dict[str, Any] = {"threadId": thread_id, "turnId": turn_id}
        if reason:
            params["reason"] = reason
        try:
            transport.send_request("turn/interrupt", params)
        except RuntimeError as exc:
            # Cancellation can race with a turn that has already reached a
            # terminal state. Reconcile the authoritative thread state so the
            # caller receives completed/interrupted instead of a stale RPC
            # error and a task left marked as running.
            if "no active turn to interrupt" not in str(exc).lower():
                raise
            self.recover_pending_tasks()
            with self._lock:
                task = self._tasks.get(task_id)
                terminal_statuses = {"completed", "failed", "interrupted", "cancelled", "succeeded", "error"}
                if task is not None and str(task.get("status") or "").lower() in terminal_statuses:
                    return {
                        "task_id": task_id,
                        "turn_id": turn_id,
                        "status": task["status"],
                        "summary": task.get("summary") or task.get("last_output") or "",
                        "reason": reason,
                    }
            raise

        with self._lock:
            terminal_statuses = {"completed", "failed", "interrupted", "cancelled", "succeeded", "error"}
            current_status = str(task.get("status") or "").lower()
            if current_status not in terminal_statuses:
                task["state"] = "cancelling"
                task["status"] = "cancelling"
                self._persist_task(task)
            result = {
                "task_id": task_id,
                "turn_id": turn_id,
                "status": task["status"],
                "reason": reason,
            }
            if current_status in terminal_statuses:
                result["summary"] = task.get("summary") or task.get("last_output") or ""
        return result

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

            appr_dict = {
                "approval_id": approval_id,
                "task_id": task_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "action_type": action_type,
                "command_or_path": command_or_path,
                "description": description,
                "status": "pending",
            }
            self._pending_approvals[approval_id] = appr_dict
            self._persist_approval(appr_dict)
            if task is not None:
                task["status"] = "approval_pending"
                task["state"] = "approval_pending"
                self._persist_task(task)
        return self._pending_approvals[approval_id]

    def respond_approval(
        self,
        approval_id: str,
        task_id: Optional[str] = None,
        decision: Optional[Union[str, dict[str, Any]]] = None,
        reason: Optional[str] = None,
    ) -> dict[str, Any]:
        resolved_task_id, resolved_decision, resolved_reason = _resolve_approval_args(task_id, decision, reason)

        with self._lock:
            appr = self._pending_approvals.get(approval_id)
            if not appr:
                raise KeyError(f"Approval '{approval_id}' not found")
            if resolved_task_id:
                if appr.get("task_id") and appr["task_id"] != resolved_task_id:
                    raise ValueError(f"Approval ID '{approval_id}' is for task '{appr['task_id']}', does not match '{resolved_task_id}'")
                if not appr.get("task_id"):
                    appr["task_id"] = resolved_task_id

            actual_task_id = appr.get("task_id") or ""
            action_type = appr.get("action_type", "command")
            thread_id = appr.get("thread_id")
            turn_id = appr.get("turn_id")
            server_req_id = appr.get("server_req_id")

        target_decision = normalize_decision_for_schema(action_type, resolved_decision, resolved_reason or "")

        if target_decision in ("accept", "acceptForSession", "approved"):
            status_val = "applied"
        elif target_decision in ("cancel", "abort"):
            status_val = "cancelled"
        else:
            status_val = "rejected"

        transport = self._get_transport()
        if server_req_id is not None:
            transport.send_response(server_req_id, {
                "decision": target_decision,
            })
        else:
            transport.send_request("turn/approval/respond", {
                "approval_id": approval_id,
                "thread_id": thread_id,
                "turn_id": turn_id,
                "threadId": thread_id,
                "turnId": turn_id,
                "decision": target_decision,
                "reason": resolved_reason or "",
            })

        with self._lock:
            appr["status"] = status_val
            appr["decision"] = target_decision
            appr["reason"] = resolved_reason
            self._persist_approval(appr)
            task = self._tasks.get(actual_task_id)
            if task is not None and task.get("status") == "approval_pending" and not any(
                pending.get("task_id") == actual_task_id and pending.get("status") == "pending"
                for pending in self._pending_approvals.values()
            ):
                task["status"] = "running"
                task["state"] = "running"
                self._persist_task(task)

        return {
            "approval_id": approval_id,
            "task_id": actual_task_id,
            "decision": target_decision,
            "status": status_val,
        }

    def get_pending_approvals(self, task_id: Optional[str] = None) -> list[dict[str, Any]]:
        """Returns active pending approvals, optionally filtered by task_id."""
        with self._lock:
            approvals = []
            for appr in self._pending_approvals.values():
                if appr.get("status") != "pending":
                    continue
                if task_id and appr.get("task_id") != task_id:
                    continue
                approvals.append(dict(appr))
            return approvals

    def get_task_status(self, task_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            if not task:
                return None
            res = dict(task)
            res["summary"] = task.get("summary") or task.get("last_output") or ""
            res["last_output"] = task.get("last_output") or ""
            res["output"] = task.get("last_output") or task.get("summary") or ""
            res["pending_approvals"] = self.get_pending_approvals(task_id)
            return res


class RemoteCodexCompanionClient:
    """HTTP client connecting Host (on 泰山派 Linux) to Windows Codex Companion."""

    def __init__(
        self,
        endpoint_url: str,
        auth_token: Optional[str] = None,
        timeout_sec: float = 10.0,
        poll_interval: float = 1.0,
        state_file_path: Optional[str] = None,
    ) -> None:
        self.endpoint_url = endpoint_url.rstrip("/")
        if self.endpoint_url.endswith("/rpc"):
            self.endpoint_url = self.endpoint_url[:-4]
        self.auth_token = auth_token
        self.timeout_sec = timeout_sec
        self.poll_interval = poll_interval
        self.state_file_path = state_file_path
        self.workspaces: dict[str, str] = {}
        self._lock = threading.RLock()
        self._event_listeners: list[Callable[[str, dict[str, Any]], bool]] = []
        self._pending_tasks: dict[str, dict[str, Any]] = {}
        self._stop_event = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None

        if self.state_file_path:
            self._load_persisted_tasks()

    def _load_persisted_tasks(self) -> None:
        if not self.state_file_path:
            return
        p = Path(self.state_file_path)
        if not p.is_file():
            return
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                with self._lock:
                    self._pending_tasks.update(data)
                if self._pending_tasks:
                    logger.info("Restored %d in-flight Codex tasks from %s (polling deferred until listeners registered)", len(self._pending_tasks), p)
        except Exception as exc:
            logger.warning("Failed to load persisted remote Codex tasks from %s: %s", p, exc)

    def _save_persisted_tasks(self) -> None:
        if not self.state_file_path:
            return
        p = Path(self.state_file_path)
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp_p = p.with_suffix(".tmp")
            with open(tmp_p, "w", encoding="utf-8") as f:
                json.dump(self._pending_tasks, f, indent=2)
            tmp_p.replace(p)
        except Exception as exc:
            logger.warning("Failed to save remote Codex tasks to %s: %s", p, exc)

    def start(self) -> None:
        """Starts background poller if there are pending in-flight tasks and listeners registered."""
        with self._lock:
            if self._pending_tasks and self._event_listeners:
                self._ensure_polling_thread()

    def add_event_listener(self, cb: Callable[[str, dict[str, Any]], bool]) -> None:
        with self._lock:
            if cb not in self._event_listeners:
                self._event_listeners.append(cb)
            if self._pending_tasks:
                self._ensure_polling_thread()

    def remove_event_listener(self, cb: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            if cb in self._event_listeners:
                self._event_listeners.remove(cb)

    def acknowledge_task(self, task_id: str) -> None:
        """Acknowledges successful delivery of a task notification and removes it from pending list and disk."""
        with self._lock:
            if task_id in self._pending_tasks:
                self._pending_tasks.pop(task_id, None)
                self._save_persisted_tasks()
                logger.info("Remote Codex task %s acknowledged and cleared from pending queue", task_id)

    def _dispatch_event(self, event_type: str, data: dict[str, Any]) -> bool:
        with self._lock:
            listeners = list(self._event_listeners)
        if not listeners:
            return False
        all_acked = True
        for cb in listeners:
            try:
                res = cb(event_type, data)
                if res is not True:
                    all_acked = False
            except Exception as err:
                logger.warning("Remote Codex event listener error: %s", err)
                all_acked = False
        return all_acked

    def _ensure_polling_thread(self) -> None:
        with self._lock:
            if self._poll_thread is None or not self._poll_thread.is_alive():
                self._stop_event.clear()
                self._poll_thread = threading.Thread(
                    target=self._poll_worker,
                    name="RemoteCodexPoller",
                    daemon=True,
                )
                self._poll_thread.start()

    def _poll_worker(self) -> None:
        while not self._stop_event.is_set():
            with self._lock:
                task_items = list(self._pending_tasks.items())
            if not task_items:
                self._stop_event.wait(self.poll_interval)
                continue

            for task_id, task_meta in task_items:
                if self._stop_event.is_set():
                    break

                existing_notification = task_meta.get("notification")
                if existing_notification:
                    event_type = "codex.turn_completed"
                    if task_meta.get("status") in ("interrupted", "cancelled"):
                        event_type = "codex.interrupted"
                    elif task_meta.get("status") in ("failed", "error"):
                        event_type = "codex.failed"
                    event_payload = {
                        key: existing_notification[key]
                        for key in ("status", "summary", "thread_id", "turn_id", "resolution")
                        if key in existing_notification
                    }
                    event_payload["task_id"] = task_id
                    summary = event_payload.get("summary")
                    if event_type == "codex.failed":
                        fallback = f"Task {task_id} failed"
                    elif event_type == "codex.interrupted":
                        fallback = f"Task {task_id} interrupted"
                    else:
                        fallback = "任务已成功完成"
                    summary = _safe_task_summary(summary, task_meta.get("prompt"), fallback)
                    event_payload["summary"] = summary
                    with self._lock:
                        task_meta["notification"] = event_payload
                        task_meta["summary"] = summary
                        self._save_persisted_tasks()
                    acked = self._dispatch_event(event_type, event_payload)
                    if acked:
                        with self._lock:
                            self._pending_tasks.pop(task_id, None)
                            self._save_persisted_tasks()
                    continue

                try:
                    res = self.get_task_status(task_id)
                except Exception as exc:
                    logger.debug("Failed to poll status for task %s: %s", task_id, exc)
                    continue

                if not isinstance(res, dict):
                    continue

                res_dict = res.get("result") if isinstance(res.get("result"), dict) else {}
                task_status = str(
                    res_dict.get("status")
                    or res_dict.get("state")
                    or res.get("status")
                    or res.get("state")
                    or ""
                ).lower()

                if task_status in ("completed", "succeeded"):
                    summary = (
                        res_dict.get("summary")
                        or res_dict.get("last_output")
                        or res_dict.get("output")
                        or res_dict.get("response")
                        or res.get("summary")
                        or res.get("last_output")
                        or res.get("output")
                        or res.get("response")
                        or res.get("message")
                        or "任务已成功完成"
                    )
                    summary = _safe_task_summary(summary, task_meta.get("prompt"), "任务已成功完成")

                    event_payload = {
                        "task_id": task_id,
                        "thread_id": (
                            res_dict.get("thread_id")
                            or res.get("thread_id")
                            or task_meta.get("thread_id", "")
                        ),
                        "turn_id": (
                            res_dict.get("turn_id")
                            or res.get("turn_id")
                            or task_meta.get("turn_id", "")
                        ),
                        "status": "completed",
                        "summary": summary,
                    }
                    with self._lock:
                        self._pending_tasks[task_id]["notification"] = event_payload
                        self._pending_tasks[task_id]["status"] = "completed"
                        self._pending_tasks[task_id]["summary"] = summary
                        self._save_persisted_tasks()

                    acked = self._dispatch_event("codex.turn_completed", event_payload)
                    if acked:
                        with self._lock:
                            self._pending_tasks.pop(task_id, None)
                            self._save_persisted_tasks()

                elif task_status in ("interrupted", "cancelled"):
                    terminal_status = "cancelled" if task_status == "cancelled" else "interrupted"
                    summary = res_dict.get("summary") or res.get("summary") or f"Task {task_id} {terminal_status}"
                    summary = _safe_task_summary(summary, task_meta.get("prompt"), f"Task {task_id} {terminal_status}")
                    event_payload = {
                        "task_id": task_id,
                        "thread_id": (
                            res_dict.get("thread_id")
                            or res.get("thread_id")
                            or task_meta.get("thread_id", "")
                        ),
                        "turn_id": (
                            res_dict.get("turn_id")
                            or res.get("turn_id")
                            or task_meta.get("turn_id", "")
                        ),
                        "status": terminal_status,
                        "summary": summary,
                    }
                    with self._lock:
                        self._pending_tasks[task_id]["notification"] = event_payload
                        self._pending_tasks[task_id]["status"] = terminal_status
                        self._pending_tasks[task_id]["summary"] = summary
                        self._save_persisted_tasks()

                    acked = self._dispatch_event("codex.interrupted", event_payload)
                    if acked:
                        with self._lock:
                            self._pending_tasks.pop(task_id, None)
                            self._save_persisted_tasks()

                elif task_status in ("failed", "error"):
                    err_msg = (
                        res_dict.get("summary")
                        or res_dict.get("last_output")
                        or res_dict.get("output")
                        or res_dict.get("error")
                        or res.get("summary")
                        or res.get("last_output")
                        or res.get("output")
                        or res.get("error")
                        or f"Task {task_id} failed"
                    )
                    err_msg = _safe_task_summary(err_msg, task_meta.get("prompt"), f"Task {task_id} failed")
                    event_payload = {
                        "task_id": task_id,
                        "thread_id": (
                            res_dict.get("thread_id")
                            or res.get("thread_id")
                            or task_meta.get("thread_id", "")
                        ),
                        "turn_id": (
                            res_dict.get("turn_id")
                            or res.get("turn_id")
                            or task_meta.get("turn_id", "")
                        ),
                        "status": "failed",
                        "summary": err_msg,
                    }
                    with self._lock:
                        self._pending_tasks[task_id]["notification"] = event_payload
                        self._pending_tasks[task_id]["status"] = "failed"
                        self._pending_tasks[task_id]["summary"] = err_msg
                        self._save_persisted_tasks()

                    acked = self._dispatch_event("codex.failed", event_payload)
                    if acked:
                        with self._lock:
                            self._pending_tasks.pop(task_id, None)
                            self._save_persisted_tasks()

            self._stop_event.wait(self.poll_interval)

    def _rpc_call(self, operation: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        url = f"{self.endpoint_url}/rpc"
        req_id = f"req-rem-codex-{uuid.uuid4().hex[:8]}"
        payload = {
            "schema_version": "1.0.0",
            "request_id": req_id,
            "operation": operation,
            "arguments": dict(arguments),
            "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=int(self.timeout_sec))).isoformat().replace("+00:00", "Z"),
        }
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            headers["Authorization"] = f"Bearer {self.auth_token}"
            headers["X-Companion-Token"] = self.auth_token

        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url=url, data=data_bytes, headers=headers, method="POST")

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_sec) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                if data.get("status") == "succeeded" or data.get("success") is True:
                    return data.get("result", {})
                err = data.get("error", {})
                if isinstance(err, dict):
                    err_msg = err.get("message", "Unknown error")
                    err_code = err.get("code", "ERROR")
                else:
                    err_msg = str(err)
                    err_code = "ERROR"
                raise RuntimeError(f"Windows Companion error ({err_code}): {err_msg}")
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise PermissionError("Authentication failed with Windows Companion: HTTP 401") from exc
            raise ConnectionError(f"HTTP error connecting to Windows Companion at {url}: {exc}") from exc
        except Exception as exc:
            raise ConnectionError(f"Failed to connect to Windows Companion at {url}: {exc}") from exc

    def submit_task(self, task_id: str, workspace_alias: str, prompt: str, model: Optional[str] = None) -> dict[str, Any]:
        res = self._rpc_call("codex.submit_turn", {
            "task_id": task_id,
            "workspace_alias": workspace_alias,
            "prompt": prompt,
            "model": model,
        })
        with self._lock:
            self._pending_tasks[task_id] = {
                "task_id": task_id,
                "prompt": prompt,
                "workspace_alias": workspace_alias,
                "model": model,
            }
            self._save_persisted_tasks()
        self._ensure_polling_thread()
        return res

    def get_task_status(self, task_id: str) -> Optional[dict[str, Any]]:
        return self._rpc_call("codex.get_task", {"task_id": task_id})

    def respond_approval(self, approval_id: str, task_id: str, decision: str, reason: Optional[str] = None) -> dict[str, Any]:
        return self._rpc_call("codex.decide_approval", {
            "approval_id": approval_id,
            "task_id": task_id,
            "decision": decision,
            "reason": reason,
        })

    def get_pending_approvals(self, task_id: Optional[str] = None) -> list[dict[str, Any]]:
        args: dict[str, Any] = {}
        if task_id:
            args["task_id"] = task_id
        res = self._rpc_call("codex.get_pending_approvals", args)
        return res.get("approvals", [])

    def list_workspaces(self) -> list[str]:
        res = self._rpc_call("codex.list_workspaces", {})
        return res.get("workspaces", [])

    def append_task(self, task_id: str, text: str) -> dict[str, Any]:
        return self._rpc_call("codex.append", {"task_id": task_id, "text": text})

    def cancel_task(self, task_id: str, reason: Optional[str] = None) -> dict[str, Any]:
        return self._rpc_call("codex.cancel", {"task_id": task_id, "reason": reason})

    def close(self) -> None:
        self._stop_event.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=1.0)



# ---------------------------------------------------------------------------
# WP5 / interfaces.md §6.5 — Companion listener binding safety (Ruling R5)
#
# Ruling: the default listen host is loopback; a non-loopback listener (0.0.0.0,
# a LAN address, ...) MUST be protected by an explicitly configured, non-empty,
# non-placeholder auth_token, otherwise startup is refused (fail closed). The
# listener never silently downgrades to an unauthenticated public service.
#
# Loopback behaviour (explicitly documented decision):
#   - loopback + no token  -> allowed (local isolation, no auth), unchanged.
#   - loopback + placeholder token -> refused: a repository-committed
#     placeholder is never a real secret and must not become a live token.
# ---------------------------------------------------------------------------

DEFAULT_COMPANION_HOST = "127.0.0.1"

#: Minimum length required for an auth_token when the listener is non-loopback.
#: A "strong shared token" shorter than this is treated as misconfiguration.
MIN_LAN_AUTH_TOKEN_LENGTH = 16

#: Tokens that are published in this repository or are obvious "fill me in"
#: markers. They can never be treated as a real shared secret.
KNOWN_PLACEHOLDER_TOKENS = frozenset({
    "replace_with_strong_shared_token_matching_taishanpi_config",
    "replace_with_strong_shared_token_or_leave_empty_for_local_only",
    "replace_with_strong_shared_token",
    "replace_with_a_strong_shared_token",
    "replace_with_your_token",
    "replace_me",
    "changeme",
    "change_me",
    "change-me",
    "your_token_here",
    "your-token-here",
    "yourtoken",
    "placeholder",
    "placeholder_token",
    "example",
    "example_token",
    "sample_token",
    "test_token",
    "secret",
    "token",
    "password",
    "todo",
    "tbd",
    "xxx",
    "none",
    "null",
    "undefined",
})

#: Case-insensitive prefixes that mark a value as a placeholder to be replaced.
_PLACEHOLDER_PREFIXES = (
    "REPLACE_WITH",
    "REPLACE-",
    "REPLACEWITH",
    "REPLACE_ME",
    "CHANGEME",
    "CHANGE_ME",
    "CHANGE-ME",
    "YOUR_TOKEN",
    "YOURTOKEN",
    "PLACEHOLDER",
    "EXAMPLE",
    "SAMPLE",
    "TODO",
    "<",
)

#: Case-insensitive substrings anywhere in the value that mark it as a placeholder.
_PLACEHOLDER_SUBSTRINGS = (
    "REPLACE_WITH",
    "REPLACE-ME",
    "REPLACEME",
    "CHANGEME",
    "CHANGE_ME",
    "PLACEHOLDER",
)


class CompanionConfigError(ValueError):
    """Raised when the Companion listener configuration is unsafe to start.

    Raised by :func:`validate_companion_binding`, :func:`resolve_companion_settings`,
    :func:`create_companion_server` and :func:`start_companion_server` so that every
    startup entry point (library, CLI, PowerShell launchers) fails closed.
    """


def is_loopback_host(host: Optional[str]) -> bool:
    """Returns True only for hosts that cannot be reached from the LAN."""
    if not isinstance(host, str):
        return False
    candidate = host.strip()
    if not candidate:
        return False
    if candidate.startswith("[") and candidate.endswith("]"):
        candidate = candidate[1:-1]
    if candidate.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        # Any other hostname (machine name, DNS name) is not provably loopback.
        return False


def is_placeholder_token(token: Optional[str]) -> bool:
    """Returns True for empty/None tokens and for known or obvious placeholder tokens."""
    if not isinstance(token, str):
        return True
    candidate = token.strip()
    if not candidate:
        return True
    lowered = candidate.lower()
    if lowered in KNOWN_PLACEHOLDER_TOKENS:
        return True
    upper = candidate.upper()
    if upper.startswith(_PLACEHOLDER_PREFIXES):
        return True
    return any(marker in upper for marker in _PLACEHOLDER_SUBSTRINGS)


def validate_companion_binding(host: Optional[str], auth_token: Optional[str]) -> None:
    """Validates the effective (host, auth_token) pair for the Companion listener.

    Fail-closed rules (interfaces.md §6.5):
      * non-loopback host + empty/None/whitespace token -> CompanionConfigError;
      * non-loopback host + placeholder token (incl. the repo example token) -> CompanionConfigError;
      * non-loopback host + token shorter than MIN_LAN_AUTH_TOKEN_LENGTH -> CompanionConfigError;
      * loopback host + empty/None token -> allowed (local isolation);
      * loopback host + placeholder token -> CompanionConfigError (never a real secret);
      * loopback host + any other explicit token -> allowed and enforced by the handler.
    """
    if auth_token is not None and not isinstance(auth_token, str):
        raise CompanionConfigError(
            f"Refusing to start: auth_token must be a string, got {type(auth_token).__name__}."
        )

    if is_loopback_host(host):
        if auth_token is not None and auth_token.strip() and is_placeholder_token(auth_token):
            raise CompanionConfigError(
                "Refusing to start: auth_token is a known/obvious placeholder value "
                "(e.g. the example token committed in this repository). Set a real strong "
                "shared token, or leave auth_token empty to run loopback-only without auth."
            )
        return

    if auth_token is None or not auth_token.strip():
        raise CompanionConfigError(
            f"Refusing to start: host '{host}' is not a loopback address and no auth_token is configured. "
            "A non-loopback listener (e.g. 0.0.0.0) exposes the Codex Companion /rpc endpoint to the LAN, "
            "so an explicit strong shared token is mandatory (interfaces.md §6.5). Refusing to start an "
            "unauthenticated listener."
        )

    if is_placeholder_token(auth_token):
        raise CompanionConfigError(
            "Refusing to start: auth_token is a known/obvious placeholder value "
            "(e.g. 'REPLACE_WITH_STRONG_SHARED_TOKEN_MATCHING_TAISHANPI_CONFIG' from the shipped example). "
            "A publicly committed placeholder must never become a live LAN token. Configure a real strong "
            "shared token on both the Windows Companion and the TaishanPi host."
        )

    if len(auth_token.strip()) < MIN_LAN_AUTH_TOKEN_LENGTH:
        raise CompanionConfigError(
            f"Refusing to start: auth_token is too short for a non-loopback listener "
            f"(minimum {MIN_LAN_AUTH_TOKEN_LENGTH} characters). Generate a strong shared token, e.g. "
            "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`."
        )


def resolve_companion_settings(
    config_path: Optional[str] = None,
    port: int = 8765,
    host: str = DEFAULT_COMPANION_HOST,
    auth_token: Optional[str] = None,
    db_path: Optional[str] = None,
) -> dict[str, Any]:
    """Loads the config file (if any) and returns the validated effective settings.

    All startup entry points funnel through this function so that the loopback /
    strong-token rule of interfaces.md §6.5 cannot be bypassed by a caller.
    Raises CompanionConfigError when the resulting binding is unsafe.
    """
    settings: dict[str, Any] = {
        "port": port,
        "host": host,
        "auth_token": auth_token,
        "db_path": db_path,
        "registered_workspaces": {},
        "codex_bin": "codex",
    }

    if config_path:
        if not os.path.exists(config_path):
            raise CompanionConfigError(
                f"Refusing to start: companion config file not found: {config_path}. "
                "Copy deploy/windows/companion-config.example.json to companion-config.json, then configure "
                "host and auth_token explicitly."
            )
        with open(config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            raise CompanionConfigError(
                f"Refusing to start: companion config {config_path} must contain a JSON object."
            )
        settings["registered_workspaces"] = cfg.get("registered_workspaces", {}) or {}
        settings["port"] = cfg.get("port", settings["port"])
        settings["host"] = cfg.get("host", settings["host"])
        settings["codex_bin"] = cfg.get("codex_bin", settings["codex_bin"])
        settings["auth_token"] = cfg.get("auth_token", settings["auth_token"])
        settings["db_path"] = cfg.get("db_path", settings["db_path"])

    validate_companion_binding(settings["host"], settings["auth_token"])
    return settings


def create_companion_server(
    config_path: Optional[str] = None,
    port: int = 8765,
    host: str = DEFAULT_COMPANION_HOST,
    auth_token: Optional[str] = None,
    db_path: Optional[str] = None,
):
    from http.server import HTTPServer, BaseHTTPRequestHandler
    from socketserver import ThreadingMixIn

    # WP5 / §6.5: validate the effective binding before anything is constructed.
    # Raises CompanionConfigError (fail closed) instead of binding an exposed,
    # unauthenticated listener.
    settings = resolve_companion_settings(
        config_path=config_path, port=port, host=host, auth_token=auth_token, db_path=db_path
    )
    registered_aliases = settings["registered_workspaces"]
    port = settings["port"]
    host = settings["host"]
    codex_bin = settings["codex_bin"]
    auth_token = settings["auth_token"]
    db_path = settings["db_path"]

    companion = CodexCompanion(
        allowed_workspaces=registered_aliases if registered_aliases else None,
        codex_bin=codex_bin,
        db_path=db_path,
    )

    class CompanionHandler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.0"

        def end_headers(self) -> None:
            self.send_header("Connection", "close")
            super().end_headers()
            self.close_connection = True

        def _check_auth(self) -> bool:
            # WP5 / §6.5: a non-loopback listener can never be constructed without a
            # validated strong token (see validate_companion_binding), so an empty
            # token only ever means "loopback-only, local isolation". Never return
            # True for an unauthenticated non-loopback listener.
            if not auth_token:
                return is_loopback_host(host)
            auth_hdr = self.headers.get("Authorization", "")
            tok_hdr = self.headers.get("X-Companion-Token", "")
            if auth_hdr.startswith("Bearer "):
                return hmac.compare_digest(auth_hdr[7:].strip(), auth_token)
            if tok_hdr:
                return hmac.compare_digest(tok_hdr.strip(), auth_token)
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
                        res = companion.respond_approval(
                            approval_id=args["approval_id"],
                            task_id=args.get("task_id"),
                            decision=args["decision"],
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
                    elif op in ("codex.get_pending_approvals", "codex.list_approvals"):
                        task_id = args.get("task_id")
                        res = {"approvals": companion.get_pending_approvals(task_id)}
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
    return server


def start_companion_server(
    config_path: Optional[str] = None,
    port: int = 8765,
    host: str = DEFAULT_COMPANION_HOST,
    auth_token: Optional[str] = None,
) -> None:
    # WP5 / §6.5: same validation path as create_companion_server / the CLI, so a
    # library caller cannot bypass the loopback-or-strong-token rule either.
    settings = resolve_companion_settings(config_path=config_path, port=port, host=host, auth_token=auth_token)
    server = create_companion_server(config_path=config_path, port=port, host=host, auth_token=auth_token)
    logger.info(
        "Codex Companion server listening on %s:%s (auth %s)",
        settings["host"],
        settings["port"],
        "enabled" if (settings["auth_token"] or "").strip() else "disabled (loopback only)",
    )
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
    parser.add_argument(
        "--check-config",
        action="store_true",
        help="Validate the effective listen host / auth_token and exit without starting the server "
             "(interfaces.md §6.5). Exits 0 when the binding is safe, 2 when it is refused.",
    )
    cli_args = parser.parse_args()
    try:
        resolved = resolve_companion_settings(config_path=cli_args.config, port=cli_args.port)
    except CompanionConfigError as exc:
        print(f"[companion] REFUSING TO START: {exc}", file=sys.stderr)
        sys.exit(2)
    if cli_args.check_config:
        # Never echo the token itself, only whether one is enforced.
        print(json.dumps({
            "status": "ok",
            "host": resolved["host"],
            "port": resolved["port"],
            "loopback_only": is_loopback_host(resolved["host"]),
            "auth_required": bool((resolved["auth_token"] or "").strip()),
        }))
        sys.exit(0)
    try:
        start_companion_server(config_path=cli_args.config, port=cli_args.port)
    except CompanionConfigError as exc:
        print(f"[companion] REFUSING TO START: {exc}", file=sys.stderr)
        sys.exit(2)
