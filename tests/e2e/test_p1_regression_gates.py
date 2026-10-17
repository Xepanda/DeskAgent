"""
A7 cross-module P1 regression gates (interfaces.md §6.1-§6.5, PR #11 remediation).

These gates exercise the REAL `DeskAgentHostApp` (real `RobotAdapter`, real
`Router`, real `LifeService` SQLite store, real background reminder scheduler and
outbox delivery worker) over a REAL TCP socket against a faithful fake of the
`desk_sdk_bridge` firmware side. They assert on the OUTBOUND response envelope
and on the final observable state -- never on an internal `confirmed=False` flag.

Covered gates (mapped to the task's items 1-8):

  1. Driver failure never returns success         -> failed + HARDWARE_FAULT
  2. No-ACK timeout never reports completion      -> unknown + OPERATION_TIMEOUT
  3. Reverse-order concurrent ACKs do not cross   -> each waiter gets its own result
  4. Late ACK after timeout does not leak         -> new request gets its own answer
  5. TCP stream framing over real sockets         -> split / coalesced / over-long
  6. Pairing fail-closed (real C bridge)          -> no token for wrong/missing/unprovisioned
  7. Companion LAN startup refused                -> non-loopback + weak token exits non-zero
  8. No regression                                -> tool round-trip, reminder delivery, remote RPC

Evidence layering (AGENTS.md §4): every assertion is **Level 2** (software
protocol / simulated PoC). No ESP32-S3 board, no Tuya cloud, no LAN Home
Assistant, no Windows Codex host is present.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any, Callable, Optional

from host.desk_agent.main import DeskAgentHostApp, RemoteCodexCompanionClient

REPO_ROOT = Path(__file__).resolve().parents[2]

# Firmware capabilities reported in the sys.client.hello handshake ACK.
CAPABILITIES = ["inputs", "motion", "camera", "audio_stream", "expression"]
# Far-future expiry so envelopes survive the Router's expiration gate.
FUTURE_EXPIRY = "2030-01-01T00:00:00Z"


def _read_max_frame_len() -> int:
    """Reads DESK_SDK_MAX_FRAME_LEN from the frozen firmware header (no hardcoding)."""
    header = REPO_ROOT / "firmware" / "components" / "desk_sdk_bridge" / "include" / "desk_sdk_bridge.h"
    match = re.search(r"#define\s+DESK_SDK_MAX_FRAME_LEN\s+(\d+)", header.read_text(encoding="utf-8"))
    if not match:
        raise RuntimeError("DESK_SDK_MAX_FRAME_LEN is not defined in the firmware header")
    return int(match.group(1))


DESK_SDK_MAX_FRAME_LEN = _read_max_frame_len()


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


# ---------------------------------------------------------------------------
# Faithful fake of the desk_sdk_bridge TCP socket server (device side).
# ---------------------------------------------------------------------------
class ScriptedFirmware:
    """
    Serves the `desk_sdk_bridge` TCP server role over a real socket and lets each
    test script the exact wire reply (or silence) per incoming frame.

    Wire contract reproduced from firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c:
      Host -> Firmware : {"type":..., "request_id":...} + "\\n"
      Firmware -> Host : {"type":"sys.ack","code":0,"request_id":...,"data":{...}} + "\\n"
    """

    def __init__(self, capabilities: Optional[list[str]] = None) -> None:
        self.capabilities = list(capabilities or CAPABILITIES)

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]

        self._lock = threading.Lock()
        self._conn: Optional[socket.socket] = None
        self._running = True

        self.connected_event = threading.Event()
        self.handshake_count = 0
        self.received: list[dict[str, Any]] = []
        self.tool_responses: list[dict[str, Any]] = []
        self.announcements: list[dict[str, Any]] = []
        self.response_event = threading.Event()

        # Reply scripting:
        #  * suppress_types        -- command `type`s the firmware never answers
        #  * suppress_request_ids  -- specific request_ids the firmware never answers
        #  * handler               -- full override; returns a dict, a list of
        #                             dicts, raw bytes, or None (no reply)
        self.suppress_types: set[str] = set()
        self.suppress_request_ids: set[str] = set()
        self.handler: Optional[Callable[[dict[str, Any]], Any]] = None

        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="scripted-fw-accept", daemon=True
        )
        self._accept_thread.start()
        self._threads: list[threading.Thread] = [self._accept_thread]

    # -- lifecycle ---------------------------------------------------------
    def _accept_loop(self) -> None:
        while self._running:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                break
            with self._lock:
                self._conn = conn
            self.connected_event.set()
            reader = threading.Thread(
                target=self._read_loop, args=(conn,), name="scripted-fw-reader", daemon=True
            )
            reader.start()
            self._threads.append(reader)

    def close(self) -> None:
        self._running = False
        with self._lock:
            conn = self._conn
            self._conn = None
        for sock in (conn, self._srv):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                sock.close()
            except Exception:
                pass
        for th in self._threads:
            th.join(timeout=1.0)

    # -- wire helpers ------------------------------------------------------
    @staticmethod
    def _encode(obj: dict[str, Any]) -> bytes:
        return (json.dumps(obj) + "\n").encode("utf-8")

    def ack_bytes(self, request_id: Optional[str], data: Optional[dict[str, Any]] = None) -> bytes:
        ack: dict[str, Any] = {"type": "sys.ack", "code": 0, "data": data or {"status": "accepted", "accepted": True}}
        if request_id:
            ack["request_id"] = request_id
        return self._encode(ack)

    def nack_bytes(self, request_id: Optional[str], code: int = 500,
                   error_code: str = "HARDWARE_FAULT", message: str = "driver fault") -> bytes:
        nack: dict[str, Any] = {"type": "sys.nack", "code": code,
                                "error": {"code": error_code, "message": message}}
        if request_id:
            nack["request_id"] = request_id
        return self._encode(nack)

    def send_raw(self, data: bytes) -> None:
        """Writes raw bytes to the connected Host (used for framing/negative paths)."""
        with self._lock:
            conn = self._conn
        if conn is None:
            raise AssertionError("Scripted firmware has no connected Host client")
        conn.sendall(data)

    # -- device -> host ----------------------------------------------------
    def dispatch_tool_request(self, envelope: dict[str, Any]) -> None:
        self.send_raw(self._encode(envelope))

    # -- host -> device ----------------------------------------------------
    def _read_loop(self, conn: socket.socket) -> None:
        conn.settimeout(0.2)
        buf = ""
        while self._running:
            try:
                data = conn.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            buf += data.decode("utf-8", errors="replace")
            while "\n" in buf:
                line, buf = buf.split("\n", 1)
                line = line.strip()
                if line:
                    self._handle_line(conn, line)

    def _handle_line(self, conn: socket.socket, line: str) -> None:
        try:
            msg = json.loads(line)
        except Exception:
            return
        if not isinstance(msg, dict):
            return
        self.received.append(msg)

        msg_type = msg.get("type", "")
        req_id = msg.get("request_id")

        if msg_type == "sys.client.hello":
            self.handshake_count += 1
            conn.sendall(self.ack_bytes(None, {"role": "hardware", "capabilities": self.capabilities}))
            return

        if msg_type == "tool.response":
            env = msg.get("data", {})
            if isinstance(env, dict):
                self.tool_responses.append(env)
            self.response_event.set()
            conn.sendall(self.ack_bytes(req_id or (env.get("request_id") if isinstance(env, dict) else None),
                                        {"status": "received"}))
            return

        if msg_type == "ctrl.expression.play":
            self.announcements.append(msg.get("data", {}))
            self.response_event.set()

        if req_id in self.suppress_request_ids or msg_type in self.suppress_types:
            return

        if self.handler is not None:
            out = self.handler(msg)
            if out is None:
                return
            if isinstance(out, (bytes, bytearray)):
                conn.sendall(bytes(out))
            elif isinstance(out, list):
                for item in out:
                    conn.sendall(item if isinstance(item, (bytes, bytearray)) else self._encode(item))
            else:
                conn.sendall(self._encode(out))
            return

        conn.sendall(self.ack_bytes(req_id))


# ---------------------------------------------------------------------------
# Base: real DeskAgentHostApp + real TCP + scripted fake firmware.
# ---------------------------------------------------------------------------
class RealChainGateCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "regression_gate.db"

        self.firmware = ScriptedFirmware()

        cfg_file = Path(self.temp_dir.name) / "config.json"
        cfg_file.write_text(json.dumps({
            "storage": {"db_path": str(self.db_path)},
            "server": {"host": "127.0.0.1", "port": 0},
            "home_assistant": {"subscribe_events": False},
            "camera": {"default_lease_ms": 5000},
            "robot": {
                "enabled": True,
                "host": "127.0.0.1",
                "tcp_port": self.firmware.port,
                "udp_port": self.firmware.port + 1,
                "auto_pair": False,
                "reconnect_interval": 0.1,
            },
        }), encoding="utf-8")

        self.app = DeskAgentHostApp(config_path=str(cfg_file))
        self.adapter = self.app.robot_adapter
        self.assertIsNotNone(self.adapter, "Host must instantiate a RobotAdapter")
        self.adapter.start()
        self.assertTrue(
            wait_until(lambda: self.adapter.is_connected, timeout=5.0),
            "RobotAdapter failed to establish the TCP link to the fake firmware",
        )
        self.assertTrue(
            wait_until(lambda: self.firmware.connected_event.is_set(), timeout=5.0),
            "Fake firmware never accepted the Host connection",
        )

    def tearDown(self) -> None:
        try:
            self.app.close()
        finally:
            self.firmware.close()
            self.temp_dir.cleanup()

    # -- helpers -----------------------------------------------------------
    def make_envelope(self, request_id: str, operation: str,
                      arguments: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "1.0.0",
            "request_id": request_id,
            "operation": operation,
            "arguments": arguments,
            "expires_at": FUTURE_EXPIRY,
        }

    def await_tool_response(self, request_id: str, timeout: float = 5.0) -> dict[str, Any]:
        self.assertTrue(
            wait_until(
                lambda: any(r.get("request_id") == request_id for r in self.firmware.tool_responses),
                timeout=timeout,
            ),
            f"No tool.response carrying request_id={request_id!r} came back over TCP",
        )
        for resp in self.firmware.tool_responses:
            if resp.get("request_id") == request_id:
                return resp
        self.fail(f"tool.response for {request_id!r} vanished")

    def tool_roundtrip(self, request_id: str, operation: str,
                       arguments: dict[str, Any], timeout: float = 5.0) -> dict[str, Any]:
        self.firmware.dispatch_tool_request(self.make_envelope(request_id, operation, arguments))
        return self.await_tool_response(request_id, timeout=timeout)


# ---------------------------------------------------------------------------
# Gates 1-5, 8: outbound envelope + final state over the real chain.
# ---------------------------------------------------------------------------
class OutboundEnvelopeGates(RealChainGateCase):

    # -- Gate 1: driver failure never returns success ---------------------
    def test_driver_failure_never_returns_success(self) -> None:
        """Firmware driver returns non-DESK_HW_OK -> sys.nack + HARDWARE_FAULT.

        The Host must surface `failed` / `HARDWARE_FAULT` and must never report
        `succeeded`, `completed`, or the removed host-invented `executed` field.
        """

        def handler(msg: dict[str, Any]) -> Any:
            if msg.get("type") in ("ctrl.motion.move", "ctrl.motion.stop"):
                return self.firmware.nack_bytes(msg.get("request_id"), 500, "HARDWARE_FAULT",
                                                "Gimbal driver failed to accept motion command")
            return None  # fall through to the default ACK

        self.firmware.handler = handler

        move = self.tool_roundtrip("req-gate-fault-move", "resource.motion.move",
                                   {"pan_deg": 90.0, "tilt_deg": 120.0})
        self.assertEqual(move.get("status"), "failed")
        self.assertEqual(move.get("error", {}).get("code"), "HARDWARE_FAULT")
        self.assertNotIn("result", move)
        self.assertNotIn("executed", move)
        self.assertNotIn("succeeded", json.dumps(move))
        self.assertNotIn("completed", json.dumps(move))

        stop = self.tool_roundtrip("req-gate-fault-stop", "resource.motion.stop",
                                   {"emergency": True})
        self.assertEqual(stop.get("status"), "failed")
        self.assertEqual(stop.get("error", {}).get("code"), "HARDWARE_FAULT")
        self.assertNotIn("result", stop)
        self.assertNotIn("executed", stop)

    # -- Gate 2: no-ACK timeout never reports completion ------------------
    def test_no_ack_timeout_never_reports_completion(self) -> None:
        """Firmware silent -> `unknown` + OPERATION_TIMEOUT (retryable), no result."""
        self.firmware.suppress_types.add("ctrl.motion.move")

        resp = self.tool_roundtrip("req-gate-timeout", "resource.motion.move",
                                   {"pan_deg": 30.0, "tilt_deg": 120.0}, timeout=8.0)

        self.assertEqual(resp.get("status"), "unknown")
        self.assertNotIn(resp.get("status"), ("succeeded", "completed", "accepted"))
        self.assertEqual(resp.get("error", {}).get("code"), "OPERATION_TIMEOUT")
        self.assertTrue(resp["error"]["retryable"])
        self.assertNotIn("result", resp)

    # -- Gate 3: reverse-order concurrent ACKs do not cross wires ---------
    def test_reverse_order_acks_reach_their_own_waiters(self) -> None:
        """Two in-flight requests answered in reverse order; no cross-talk."""
        results: dict[int, dict[str, Any]] = {}
        pending: list[dict[str, Any]] = []
        lock = threading.Lock()

        def handler(msg: dict[str, Any]) -> Any:
            if msg.get("type") != "ctrl.motion.move":
                return None
            with lock:
                pending.append(msg)
                if len(pending) == 2:
                    # Answer in REVERSE arrival order, echoing each request's own seq.
                    return [
                        self.firmware.ack_bytes(m.get("request_id"),
                                                {"status": "accepted", "accepted": True,
                                                 "seq": m["data"]["seq"]})
                        for m in reversed(pending)
                    ]
            return None

        self.firmware.handler = handler

        def call(seq: int) -> None:
            results[seq] = self.adapter.send_command(
                {"type": "ctrl.motion.move", "data": {"seq": seq}}, timeout=5.0
            )

        t1 = threading.Thread(target=call, args=(1,))
        t2 = threading.Thread(target=call, args=(2,))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join(timeout=8.0)
        t2.join(timeout=8.0)

        self.assertEqual(results[1]["data"]["seq"], 1, "waiter 1 received the wrong response")
        self.assertEqual(results[2]["data"]["seq"], 2, "waiter 2 received the wrong response")
        self.assertNotEqual(results[1]["request_id"], results[2]["request_id"])

    # -- Gate 4: late ACK after timeout does not affect a new request -----
    def test_late_ack_after_timeout_does_not_satisfy_a_new_request(self) -> None:
        """A stale ACK must be discarded; a new request is answered only by its own ACK."""
        self.firmware.suppress_request_ids.update({"req-gate-late-A", "req-gate-late-B"})

        first = self.adapter.send_command(
            {"type": "ctrl.motion.move", "request_id": "req-gate-late-A", "data": {"seq": "A"}},
            timeout=0.4,
        )
        self.assertEqual(first["code"], 408, "the silent request must time out")
        self.assertNotIn("req-gate-late-A", self.adapter._pending_responses)

        # The late ACK for request A now arrives, after its waiter has exited.
        self.firmware.send_raw(self.firmware.ack_bytes(
            "req-gate-late-A", {"status": "accepted", "accepted": True, "seq": "STALE"}))
        time.sleep(0.3)
        self.assertNotIn(
            "req-gate-late-A", self.adapter._pending_responses,
            "a late ACK must be discarded, not buffered for a future waiter",
        )

        # A brand-new request (also silent on the wire) must NOT be satisfied by it.
        second = self.adapter.send_command(
            {"type": "ctrl.motion.move", "request_id": "req-gate-late-B", "data": {"seq": "B"}},
            timeout=0.5,
        )
        self.assertEqual(second["code"], 408, "the stale ACK must not satisfy a new request")
        self.assertEqual(second["request_id"], "req-gate-late-B")
        self.assertNotEqual(second.get("data", {}).get("seq"), "STALE")

        # A live request is answered by its OWN response.
        self.firmware.handler = lambda msg: (
            self.firmware.ack_bytes(msg.get("request_id"),
                                    {"status": "accepted", "accepted": True,
                                     "seq": msg.get("data", {}).get("seq")})
            if msg.get("type") == "ctrl.motion.move" else None
        )
        third = self.adapter.send_command(
            {"type": "ctrl.motion.move", "request_id": "req-gate-live-C", "data": {"seq": "C"}},
            timeout=3.0,
        )
        self.assertEqual(third["request_id"], "req-gate-live-C")
        self.assertEqual(third["data"]["seq"], "C")

    # -- Gate 5: TCP stream framing over real sockets ---------------------
    def test_framing_split_across_multiple_sends(self) -> None:
        """A frame split across two send() calls must still be reassembled."""
        env = self.make_envelope("req-frame-split", "note.create",
                                 {"note_id": "note-split", "title": "Split", "content": "ok"})
        raw = self.firmware._encode(env)
        mid = len(raw) // 2

        self.firmware.send_raw(raw[:mid])
        time.sleep(0.2)
        self.assertFalse(
            any(r.get("request_id") == "req-frame-split" for r in self.firmware.tool_responses),
            "a half frame must not be treated as a complete message",
        )

        self.firmware.send_raw(raw[mid:])
        resp = self.await_tool_response("req-frame-split", timeout=5.0)
        self.assertEqual(resp.get("status"), "succeeded")
        self.assertIsNotNone(self.app.life_service.get_note("note-split"))

    def test_framing_several_messages_coalesced_in_one_send(self) -> None:
        """Several messages in a single send() must each be processed."""
        env1 = self.make_envelope("req-frame-coal-1", "note.create",
                                  {"note_id": "note-coal-1", "title": "One", "content": "1"})
        env2 = self.make_envelope("req-frame-coal-2", "note.create",
                                  {"note_id": "note-coal-2", "title": "Two", "content": "2"})
        self.firmware.send_raw(self.firmware._encode(env1) + self.firmware._encode(env2))

        r1 = self.await_tool_response("req-frame-coal-1", timeout=5.0)
        r2 = self.await_tool_response("req-frame-coal-2", timeout=5.0)
        self.assertEqual(r1.get("status"), "succeeded")
        self.assertEqual(r2.get("status"), "succeeded")
        self.assertIsNotNone(self.app.life_service.get_note("note-coal-1"))
        self.assertIsNotNone(self.app.life_service.get_note("note-coal-2"))

    def test_framing_overlong_frame_then_valid_frame_still_processed(self) -> None:
        """An over-long frame (beyond DESK_SDK_MAX_FRAME_LEN) must not poison the next message.

        The firmware-side enforcement of DESK_SDK_MAX_FRAME_LEN is covered by
        tests/sdk_contract/test_sdk_bridge_protocol.py; here we assert the Host's
        own RX framing keeps serving valid frames after a garbage over-long line.
        """
        junk = b"{" + b"x" * (DESK_SDK_MAX_FRAME_LEN + 64) + b"\n"
        self.firmware.send_raw(junk)
        time.sleep(0.3)

        valid = self.make_envelope("req-frame-after-junk", "note.create",
                                   {"note_id": "note-after-junk", "title": "Alive", "content": "ok"})
        self.firmware.send_raw(self.firmware._encode(valid))

        resp = self.await_tool_response("req-frame-after-junk", timeout=5.0)
        self.assertEqual(resp.get("status"), "succeeded")
        self.assertIsNotNone(self.app.life_service.get_note("note-after-junk"))

    # -- Gate 8: no regression in existing paths --------------------------
    def test_tool_request_roundtrip_still_works(self) -> None:
        resp = self.tool_roundtrip(
            "req-gate-reg-rem", "reminder.create",
            {"reminder_id": "rem-gate-reg", "title": "Regression", "due_at": "2030-06-01T09:00:00Z"},
        )
        self.assertEqual(resp.get("status"), "succeeded")
        stored = self.app.life_service.get_reminder("rem-gate-reg")
        self.assertIsNotNone(stored)
        self.assertEqual(stored["title"], "Regression")

    def test_reminder_delivery_still_works_end_to_end(self) -> None:
        """A due reminder is delivered to the robot channel and the outbox row dispatched."""
        resp = self.tool_roundtrip(
            "req-gate-reg-due", "reminder.create",
            {"reminder_id": "rem-gate-due", "title": "Stretch", "due_at": "2026-10-16T08:00:00Z"},
        )
        self.assertEqual(resp.get("status"), "succeeded")

        self.assertTrue(
            wait_until(lambda: len(self.firmware.announcements) > 0, timeout=15.0),
            "Due reminder was never announced to the robot channel",
        )
        announcement = self.firmware.announcements[-1]
        self.assertEqual(announcement.get("event_type"), "reminder.alert")
        self.assertEqual(announcement.get("text"), "Stretch")
        self.assertEqual(announcement.get("payload", {}).get("reminder_id"), "rem-gate-due")

        self.assertTrue(
            wait_until(lambda: any(a.get("reminder_id") == "rem-gate-due"
                                   for a in self.app._delivered_alerts), timeout=5.0),
            "Host did not record the announcement as delivered",
        )


# ---------------------------------------------------------------------------
# Gate 8 (remote RPC): RemoteCodexCompanionClient against a mock companion.
# ---------------------------------------------------------------------------
class MockCompanionHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        op = body.get("operation")
        req_id = body.get("request_id", "res-default")
        if op == "codex.submit_turn":
            result = {"task_id": "task-gate-1", "status": "running"}
        elif op in ("codex.decide_approval", "codex.respond_approval"):
            result = {"task_id": body.get("arguments", {}).get("task_id"), "resolved": True}
        else:
            result = {"ok": True}
        payload = json.dumps({"status": "succeeded", "request_id": req_id, "result": result}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, fmt: str, *args: Any) -> None:  # noqa: A003
        pass


class RemoteRpcGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = HTTPServer(("127.0.0.1", 0), MockCompanionHandler)
        cls.port = cls.server.server_port
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_remote_codex_rpc_still_works(self) -> None:
        client = RemoteCodexCompanionClient(
            endpoint_url=f"http://127.0.0.1:{self.port}", auth_token="gate-token",
        )
        res = client.submit_task("task-gate-1", "ws", "do the thing")
        self.assertEqual(res.get("task_id"), "task-gate-1")
        appr = client.respond_approval("appr-1", "task-gate-1", decision="accept")
        self.assertTrue(appr.get("resolved"))


# ---------------------------------------------------------------------------
# Gate 6: pairing fail-closed, exercised against the REAL compiled C bridge.
# ---------------------------------------------------------------------------
class PairingFailClosedGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_sdk_bridge_gate.so")
        cmd = [
            "gcc", "-shared", "-fPIC", "-O2", "-Wall",
            f"-I{REPO_ROOT}/firmware/components/desk_hardware/include",
            f"-I{REPO_ROOT}/firmware/components/desk_tclaw/include",
            f"-I{REPO_ROOT}/firmware/components/desk_sdk_bridge/include",
            f"{REPO_ROOT}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{REPO_ROOT}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{REPO_ROOT}/firmware/components/desk_tclaw/src/desk_tclaw_bridge.c",
            f"{REPO_ROOT}/firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c",
            "-lpthread", "-o", cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile desk_sdk_bridge gate library: {res.stderr}")

        cls.lib = ctypes.CDLL(cls.so_path)
        cls.lib.desk_sdk_bridge_init.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_init.argtypes = []
        cls.lib.desk_sdk_bridge_reset.restype = None
        cls.lib.desk_sdk_bridge_reset.argtypes = []
        cls.lib.desk_sdk_bridge_set_pairing_code.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_set_pairing_code.argtypes = [ctypes.c_char_p]
        cls.lib.desk_sdk_bridge_is_pairing_code_configured.restype = ctypes.c_bool
        cls.lib.desk_sdk_bridge_is_pairing_code_configured.argtypes = []
        cls.lib.desk_sdk_bridge_process_pairing_udp.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_pairing_udp.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        cls.lib.desk_sdk_pairing_server_start.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_start.argtypes = [ctypes.c_uint16]
        cls.lib.desk_sdk_pairing_server_get_port.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_get_port.argtypes = []
        cls.lib.desk_sdk_pairing_server_stop.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_stop.argtypes = []

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_sdk_bridge_reset()
        self.lib.desk_sdk_bridge_init()

    def tearDown(self) -> None:
        self.lib.desk_sdk_pairing_server_stop()
        self.lib.desk_sdk_bridge_reset()

    @staticmethod
    def _pair_request(pairing_code: Optional[str]) -> bytes:
        req: dict[str, Any] = {
            "protocol": "watcher_lan_pairing_v1",
            "request_id": "pair-gate-001",
            "daemon_instance_id": "sdk-daemon-gate",
            "target_mode": "python_sdk",
        }
        if pairing_code is not None:
            req["pairing_code"] = pairing_code
        return json.dumps(req).encode("utf-8")

    def _process(self, pairing_code: Optional[str]) -> tuple[int, str]:
        out = ctypes.create_string_buffer(4096)
        rc = self.lib.desk_sdk_bridge_process_pairing_udp(
            self._pair_request(pairing_code), out, len(out)
        )
        return rc, out.value.decode("utf-8")

    def test_unprovisioned_code_yields_no_token(self) -> None:
        self.lib.desk_sdk_bridge_set_pairing_code(None)
        self.assertFalse(self.lib.desk_sdk_bridge_is_pairing_code_configured())
        rc, reply = self._process("123456")
        self.assertNotEqual(rc, 0, "unprovisioned pairing must be rejected")
        self.assertEqual(reply, "", "no pair.accept / session token may be emitted")
        self.assertNotIn("session_token", reply)

    def test_wrong_code_yields_no_token(self) -> None:
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        rc, reply = self._process("654321")
        self.assertNotEqual(rc, 0)
        self.assertEqual(reply, "")
        self.assertNotIn("token", reply)

    def test_missing_code_yields_no_token(self) -> None:
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        rc, reply = self._process(None)
        self.assertNotEqual(rc, 0)
        self.assertEqual(reply, "")

    def test_malformed_code_yields_no_token(self) -> None:
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        rc, reply = self._process("12ab56")
        self.assertNotEqual(rc, 0)
        self.assertEqual(reply, "")

    def test_valid_code_is_accepted_control(self) -> None:
        """Control: with the exact code provisioned, pairing DOES succeed."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        rc, reply = self._process("123456")
        self.assertEqual(rc, 0)
        payload = json.loads(reply)
        self.assertEqual(payload.get("type"), "pair.accept")
        token = payload.get("token") or payload.get("session_token")
        self.assertIsNotNone(token)
        self.assertEqual(len(token), 64)

    def test_real_udp_pairing_with_wrong_code_gets_no_reply(self) -> None:
        """Over a real UDP socket a rejected pairing produces NO wire reply at all."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        rc = self.lib.desk_sdk_pairing_server_start(0)
        self.assertEqual(rc, 0)
        udp_port = self.lib.desk_sdk_pairing_server_get_port()
        self.assertGreater(udp_port, 1024)

        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(1.0)
        try:
            sock.sendto(self._pair_request("000000"), ("127.0.0.1", udp_port))
            with self.assertRaises(socket.timeout, msg="a wrong pairing code must get no reply"):
                sock.recvfrom(4096)
        finally:
            sock.close()


# ---------------------------------------------------------------------------
# Gate 7: Companion LAN startup refused (integration level, real entry point).
# ---------------------------------------------------------------------------
class CompanionStartupGate(unittest.TestCase):
    REPO_PLACEHOLDER_TOKEN = "REPLACE_WITH_STRONG_SHARED_TOKEN_MATCHING_TAISHANPI_CONFIG"
    STRONG_TOKEN = "gate-strong-shared-token-9f3c1d7a4b2e"

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def _run_check_config(self, cfg: dict[str, Any]) -> subprocess.CompletedProcess:
        cfg_path = Path(self.temp_dir.name) / "companion-config.json"
        cfg_path.write_text(json.dumps(cfg), encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "companion.src.codex_companion",
             "--config", str(cfg_path), "--check-config"],
            cwd=str(REPO_ROOT), env=env, capture_output=True, text=True, timeout=30,
        )

    def test_non_loopback_without_token_refuses_to_start(self) -> None:
        proc = self._run_check_config({"host": "0.0.0.0", "port": 8765, "auth_token": ""})
        self.assertEqual(proc.returncode, 2, f"must refuse to start; stderr={proc.stderr}")
        self.assertIn("REFUSING TO START", proc.stderr)

    def test_non_loopback_with_placeholder_token_refuses_to_start(self) -> None:
        proc = self._run_check_config(
            {"host": "0.0.0.0", "port": 8765, "auth_token": self.REPO_PLACEHOLDER_TOKEN}
        )
        self.assertEqual(proc.returncode, 2, f"must refuse a placeholder token; stderr={proc.stderr}")
        self.assertIn("REFUSING TO START", proc.stderr)

    def test_non_loopback_with_strong_token_starts(self) -> None:
        """Control: a genuinely strong token on a LAN binding is accepted."""
        proc = self._run_check_config(
            {"host": "0.0.0.0", "port": 8765, "auth_token": self.STRONG_TOKEN}
        )
        self.assertEqual(proc.returncode, 0, f"strong token must be accepted; stderr={proc.stderr}")
        self.assertEqual(json.loads(proc.stdout).get("loopback_only"), False)

    def test_loopback_default_needs_no_token(self) -> None:
        """Control: the default loopback binding is allowed without a token."""
        proc = self._run_check_config({"host": "127.0.0.1", "port": 8765})
        self.assertEqual(proc.returncode, 0, f"loopback default must start; stderr={proc.stderr}")
        self.assertEqual(json.loads(proc.stdout).get("loopback_only"), True)


if __name__ == "__main__":
    unittest.main()
