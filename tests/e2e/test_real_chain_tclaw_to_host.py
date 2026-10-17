"""
A7 End-to-End Real Chain Verification: TClaw -> SDK Bridge TCP -> RobotAdapter
-> Host Router -> Life/HA execution -> result returned over TCP -> TClaw.

This suite boots a REAL `DeskAgentHostApp` (with its real `RobotAdapter`, real
`Router`, real `LifeService` SQLite store, real background reminder scheduler and
outbox delivery worker) and drives it from a faithful fake of the ESP32-S3
firmware side: the `desk_sdk_bridge` TCP socket server plus the TClaw dialogue
engine.

Wire contract reproduced exactly from the C firmware
(`firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c`):

  Firmware -> Host  : raw RequestEnvelope JSON + "\\n"
                      (written by `desk_sdk_bridge_dispatch_tool_request` to s_client_fd)
  Host -> Firmware  : {"type":"tool.response","data":<ResponseEnvelope>,"request_id":...} + "\\n"
                      (`RobotAdapter.send_tool_response`)
  Firmware ACK      : {"type":"sys.ack","code":0,"request_id":...,"data":{"status":"received"}}
                      (`desk_sdk_bridge_process_line` tool.response branch)

Evidence layering (AGENTS.md Section 4): every assertion below is **Level 2**
(software protocol / simulated PoC). No ESP32-S3 board, no Tuya cloud, no LAN
Home Assistant, no Windows Codex host is present. This suite therefore proves the
software chain is genuinely wired and free of faked ACKs; it does NOT prove
Level 3/4 physical delivery. See docs/acceptance/acceptance-matrix.md.
"""

from __future__ import annotations

import json
import socket
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Optional

from host.desk_agent.main import DeskAgentHostApp

# Firmware capabilities reported in the sys.client.hello handshake ACK.
FIRMWARE_CAPABILITIES = ["inputs", "motion", "camera", "audio_stream", "expression"]

# Far-future expiry so envelopes survive the Router's expiration gate.
FUTURE_EXPIRY = "2030-01-01T00:00:00Z"


class FakeFirmwareTClaw:
    """
    Faithful fake of the device side of the real chain.

    Serves the `desk_sdk_bridge` TCP socket server role (the firmware is the TCP
    *server*; the Host's RobotAdapter is the TCP client) and records every frame
    the Host sends back, so tests can assert on what TClaw actually received.
    """

    def __init__(self, capabilities: Optional[list[str]] = None) -> None:
        self.capabilities = list(capabilities or FIRMWARE_CAPABILITIES)

        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(4)
        self.port = self._srv.getsockname()[1]

        self._lock = threading.Lock()
        self._conn: Optional[socket.socket] = None
        self._running = True

        # Observed traffic (what the Host sent back to the device).
        self.hello_received = threading.Event()
        self.connected_event = threading.Event()
        self.handshake_count = 0
        self.tool_requests_sent: list[dict[str, Any]] = []
        self.tool_responses: list[dict[str, Any]] = []
        self.announcements: list[dict[str, Any]] = []
        self.motion_commands: list[dict[str, Any]] = []
        self.response_event = threading.Event()
        self.announcement_event = threading.Event()

        # Fault injection: command `type`s for which the firmware withholds its ACK.
        self.suppress_ack_for: set[str] = set()

        self._threads: list[threading.Thread] = []
        self._accept_thread = threading.Thread(
            target=self._accept_loop, name="fake-tclaw-accept", daemon=True
        )
        self._accept_thread.start()

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
                target=self._read_loop, args=(conn,), name="fake-tclaw-reader", daemon=True
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
        for th in self._threads + [self._accept_thread]:
            th.join(timeout=1.0)

    # -- device -> host ----------------------------------------------------
    def dispatch_tool_request(self, envelope: dict[str, Any]) -> None:
        """
        Mirrors `desk_sdk_bridge_dispatch_tool_request`: writes the raw
        RequestEnvelope JSON followed by a newline onto the client socket.
        """
        self.tool_requests_sent.append(envelope)
        self._send_raw(json.dumps(envelope))

    def dispatch_malformed_line(self, raw: str) -> None:
        """Pushes an arbitrary raw line, for negative-path coverage."""
        self._send_raw(raw)

    def drop_connection(self) -> None:
        """Abruptly drops the device-side TCP link, simulating firmware/Wi-Fi loss."""
        with self._lock:
            conn = self._conn
            self._conn = None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    # -- host -> device ----------------------------------------------------
    def _send_raw(self, line: str) -> None:
        with self._lock:
            conn = self._conn
        if conn is None:
            raise AssertionError("Fake firmware has no connected Host client")
        conn.sendall((line if line.endswith("\n") else line + "\n").encode("utf-8"))

    def _send_ack(self, conn: socket.socket, request_id: Optional[str] = None,
                  data: Optional[dict[str, Any]] = None) -> None:
        ack: dict[str, Any] = {"type": "sys.ack", "code": 0, "data": data or {"status": "ok"}}
        if request_id:
            ack["request_id"] = request_id
        try:
            conn.sendall((json.dumps(ack) + "\n").encode("utf-8"))
        except OSError:
            pass

    def _read_loop(self, conn: socket.socket) -> None:
        conn.settimeout(0.2)
        buf = ""
        while self._running:
            try:
                data = conn.recv(4096)
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

        msg_type = msg.get("type", "")
        req_id = msg.get("request_id")

        if msg_type == "sys.client.hello":
            with self._lock:
                self.handshake_count += 1
            self.hello_received.set()
            self._send_ack(conn, data={
                "role": "hardware",
                "capabilities": self.capabilities,
            })
            return

        if msg_type == "tool.response":
            env = msg.get("data", {})
            with self._lock:
                self.tool_responses.append(env)
            self.response_event.set()
            # Firmware ACKs the tool response (tool.response branch of process_line).
            self._send_ack(conn, request_id=req_id or env.get("request_id"),
                           data={"status": "received"})
            return

        if msg_type == "ctrl.expression.play":
            with self._lock:
                self.announcements.append(msg.get("data", {}))
            self.announcement_event.set()
            if msg_type not in self.suppress_ack_for:
                # interfaces.md 6.2 rule 1: the ACK must echo the original
                # request_id, otherwise the Host's waiter cannot correlate it.
                self._send_ack(conn, request_id=req_id,
                               data={"status": "accepted", "accepted": True})
            return

        if msg_type in ("ctrl.motion.move", "ctrl.motion.stop"):
            with self._lock:
                self.motion_commands.append(msg)
            if msg_type not in self.suppress_ack_for:
                # interfaces.md 6.1: a firmware ACK means "accepted" (dispatched
                # to the driver), never "succeeded"/"completed".
                self._send_ack(conn, request_id=req_id,
                               data={"status": "accepted", "accepted": True})
            return

        if msg_type not in self.suppress_ack_for:
            self._send_ack(conn, request_id=req_id,
                           data={"status": "accepted", "accepted": True})


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class RealChainTestCase(unittest.TestCase):
    """Boots a real DeskAgentHostApp wired to a fake firmware TClaw over real TCP."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "real_chain.db"

        self.firmware = FakeFirmwareTClaw()

        cfg_file = Path(self.temp_dir.name) / "config.json"
        cfg_file.write_text(json.dumps({
            "storage": {"db_path": str(self.db_path)},
            "server": {"host": "127.0.0.1", "port": 0},
            # No LAN Home Assistant in this environment: keep the HA websocket
            # subscriber from spinning against homeassistant.local.
            "home_assistant": {"subscribe_events": False},
            "camera": {"default_lease_ms": 5000},
            "robot": {
                "enabled": True,
                "host": "127.0.0.1",
                "tcp_port": self.firmware.port,
                "udp_port": self.firmware.port + 1,
                "auto_pair": False,          # UDP pairing is exercised in sdk_contract tests
                "reconnect_interval": 0.2,
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

    def tool_request_roundtrip(self, request_id: str, operation: str,
                               arguments: dict[str, Any],
                               timeout: float = 5.0) -> dict[str, Any]:
        """Sends one tool request from the device and returns the matching tool.response."""
        before = len(self.firmware.tool_responses)
        self.firmware.dispatch_tool_request(
            self.make_envelope(request_id, operation, arguments)
        )
        self.assertTrue(
            wait_until(lambda: len(self.firmware.tool_responses) > before, timeout=timeout),
            f"No tool.response came back over TCP for {operation!r} ({request_id})",
        )
        for resp in self.firmware.tool_responses[before:]:
            if resp.get("request_id") == request_id:
                return resp
        self.fail(f"No tool.response carried request_id={request_id!r}")

    # ---------------------------------------------------------------------
    # 1. Handshake / capability negotiation
    # ---------------------------------------------------------------------
    def test_firmware_handshake_and_capabilities_negotiated(self) -> None:
        """The Host performs the real sys.client.hello handshake and learns capabilities."""
        self.assertTrue(self.firmware.hello_received.is_set())
        self.assertEqual(sorted(self.adapter.capabilities), sorted(FIRMWARE_CAPABILITIES))

    # ---------------------------------------------------------------------
    # 2. Core chain: tool request -> router -> Life execution -> response back
    # ---------------------------------------------------------------------
    def test_tclaw_tool_request_reaches_router_and_life_service_executes(self) -> None:
        """
        Full chain: TClaw emits reminder.create -> SDK Bridge TCP -> RobotAdapter
        -> Host Router -> LifeService SQLite write -> result returned to TClaw.
        """
        resp = self.tool_request_roundtrip(
            "req-chain-rem-01", "reminder.create",
            {"reminder_id": "rem-chain-01", "title": "Drink warm water",
             "due_at": "2030-06-01T09:00:00Z", "priority": "high"},
        )

        self.assertEqual(resp.get("status"), "succeeded")
        self.assertEqual(resp.get("request_id"), "req-chain-rem-01")
        self.assertEqual(resp.get("result", {}).get("reminder_id"), "rem-chain-01")

        # The Host really persisted it — not an echoed stub.
        stored = self.app.life_service.get_reminder("rem-chain-01")
        self.assertIsNotNone(stored, "reminder.create must really write to the Host SQLite store")
        self.assertEqual(stored["title"], "Drink warm water")
        self.assertEqual(stored["priority"], "high")

    # ---------------------------------------------------------------------
    # 3. Hardware leg: motion command confirmed by real firmware ACK
    # ---------------------------------------------------------------------
    def test_motion_move_requires_real_firmware_ack_for_confirmation(self) -> None:
        """
        resource.motion.move travels Host -> RobotAdapter -> firmware. A firmware
        ACK only proves the command was *accepted* by the driver (interfaces.md
        6.1); physical completion is a separate `resource.motion.completed` event
        and is never asserted here.
        """
        resp = self.tool_request_roundtrip(
            "req-chain-motion-01", "resource.motion.move",
            {"pan_deg": 45.0, "tilt_deg": 110.0, "duration_ms": 300},
        )

        self.assertEqual(resp.get("status"), "accepted")
        self.assertNotIn(resp.get("status"), ("succeeded", "completed"))
        result = resp["result"]
        self.assertTrue(result["accepted"], "Firmware ACKed, so the command was accepted")
        self.assertFalse(
            result["state_confirmed"],
            "An ACK is acceptance, never physical confirmation (interfaces.md 6.1)",
        )
        self.assertEqual(result["hardware_link"], "connected")
        self.assertEqual(result["pan"], 45.0)
        self.assertEqual(result["tilt"], 110.0)
        self.assertNotIn("executed", result)

        # The firmware genuinely received the framed motion command.
        self.assertTrue(
            wait_until(lambda: len(self.firmware.motion_commands) >= 1, timeout=2.0),
            "Firmware never received ctrl.motion.move",
        )
        move_cmd = self.firmware.motion_commands[-1]
        self.assertEqual(move_cmd["type"], "ctrl.motion.move")
        self.assertEqual(move_cmd["data"]["pan_deg"], 45.0)
        self.assertEqual(move_cmd["data"]["tilt_deg"], 110.0)

    def test_motion_move_mechanical_limits_clamped_before_dispatch(self) -> None:
        """Out-of-range gimbal angles are clamped to the physical envelope (Pan 0..180, Tilt 100..140)."""
        resp = self.tool_request_roundtrip(
            "req-chain-motion-clamp", "resource.motion.move",
            {"pan_deg": 999.0, "tilt_deg": -50.0},
        )
        self.assertEqual(resp.get("status"), "accepted")
        result = resp["result"]
        self.assertEqual(result["pan"], 180.0)
        self.assertEqual(result["tilt"], 100.0)

    # ---------------------------------------------------------------------
    # 4. Anti-false-success: withheld ACK must NOT be reported as success
    # ---------------------------------------------------------------------
    def test_withheld_firmware_ack_is_not_reported_as_success(self) -> None:
        """
        With the firmware deliberately silent, the Host must not claim the motion
        succeeded. This is the chain-level guard against faked ACKs: the timeout
        surfaces as an `unknown` error envelope with no `result` at all.
        """
        self.firmware.suppress_ack_for.add("ctrl.motion.move")

        resp = self.tool_request_roundtrip(
            "req-chain-motion-noack", "resource.motion.move",
            {"pan_deg": 10.0, "tilt_deg": 120.0},
            timeout=8.0,
        )

        self.assertEqual(resp.get("status"), "unknown")
        self.assertNotIn(resp.get("status"), ("succeeded", "completed", "accepted"))
        self.assertEqual(resp.get("error", {}).get("code"), "OPERATION_TIMEOUT")
        self.assertTrue(resp["error"]["retryable"], "A timeout must be flagged retryable")
        self.assertNotIn(
            "result", resp,
            "An unconfirmed motion must not carry a result claiming any physical state",
        )

    # ---------------------------------------------------------------------
    # 5. Reminder firing -> outbox -> announcement pushed back to TClaw
    # ---------------------------------------------------------------------
    def test_due_reminder_is_announced_back_to_tclaw(self) -> None:
        """
        Proactive leg: a due reminder created over the chain is picked up by the
        real background scheduler, committed atomically to the outbox, delivered by
        the outbox worker, and announced on the robot's expression/audio channel.

        This must be a *genuine* delivery: the announcement really reaches the
        fake firmware (which ACKs it with the correct request_id), and the outbox
        row is marked `dispatched` because delivery succeeded -- not because the
        worker backed off after a rejected send.
        """
        # Already-due timestamp so the scheduler fires on its next poll.
        resp = self.tool_request_roundtrip(
            "req-chain-rem-due", "reminder.create",
            {"reminder_id": "rem-chain-due", "title": "Stand up and stretch",
             "due_at": "2026-10-16T08:00:00Z"},
        )
        self.assertEqual(resp.get("status"), "succeeded")

        self.assertTrue(
            wait_until(lambda: self.firmware.announcement_event.is_set(), timeout=15.0),
            "Due reminder was never announced back to the robot channel",
        )
        announcement = self.firmware.announcements[-1]
        self.assertEqual(announcement.get("event_type"), "reminder.alert")
        self.assertEqual(announcement.get("text"), "Stand up and stretch")
        self.assertEqual(announcement.get("payload", {}).get("reminder_id"), "rem-chain-due")

        # The Host only records a delivered alert when the announcement channel
        # returned True -- i.e. the firmware genuinely ACKed the ctrl.expression.play.
        self.assertTrue(
            wait_until(
                lambda: any(a.get("reminder_id") == "rem-chain-due"
                            for a in self.app._delivered_alerts),
                timeout=5.0,
            ),
            "Announcement was not confirmed as delivered by the Host (send_announcement returned False)",
        )

        # Reminder state really advanced.
        reminder = self.app.life_service.get_reminder("rem-chain-due")
        self.assertEqual(reminder["status"], "ringing")

        # The outbox row must end up `dispatched` with no retry recorded. A row
        # that merely backed off would still be `pending` (with a future
        # next_retry_at) -- asserting only "no *ready* pending row" would pass
        # vacuously, so query the row state directly.
        def outbox_row() -> Optional[dict[str, Any]]:
            conn = sqlite3.connect(str(self.db_path))
            try:
                conn.row_factory = sqlite3.Row
                cur = conn.execute(
                    "SELECT state, retry_count, dispatched_at FROM outbox "
                    "WHERE event_type = 'reminder.alert'"
                )
                rows = [dict(r) for r in cur.fetchall()]
                return rows[0] if rows else None
            finally:
                conn.close()

        self.assertTrue(
            wait_until(lambda: (outbox_row() or {}).get("state") == "dispatched", timeout=5.0),
            f"Outbox row must be dispatched after a successful delivery, got {outbox_row()}",
        )
        row = outbox_row()
        self.assertEqual(row["state"], "dispatched")
        self.assertEqual(row["retry_count"], 0, "A successful delivery must not record any retry")
        self.assertIsNotNone(row["dispatched_at"])

    # ---------------------------------------------------------------------
    # 6. Error propagation back over the wire
    # ---------------------------------------------------------------------
    def test_unknown_operation_error_propagates_back_to_tclaw(self) -> None:
        """An unregistered operation yields a failed envelope delivered back over TCP."""
        resp = self.tool_request_roundtrip(
            "req-chain-unknown-01", "totally.bogus.operation", {"x": 1},
        )
        self.assertEqual(resp.get("status"), "failed")
        self.assertEqual(resp.get("request_id"), "req-chain-unknown-01")
        self.assertIn("error", resp)

    def test_expired_envelope_is_rejected_not_executed(self) -> None:
        """A stale expires_at is rejected by the Router and never reaches the Life store."""
        before = len(self.firmware.tool_responses)
        self.firmware.dispatch_tool_request({
            "schema_version": "1.0.0",
            "request_id": "req-chain-expired-01",
            "operation": "reminder.create",
            "arguments": {"reminder_id": "rem-chain-expired", "title": "Stale",
                          "due_at": "2030-06-01T09:00:00Z"},
            "expires_at": "2020-01-01T00:00:00Z",
        })
        self.assertTrue(
            wait_until(lambda: len(self.firmware.tool_responses) > before, timeout=5.0),
            "Expired envelope must still be answered with a failure response",
        )
        resp = self.firmware.tool_responses[-1]
        self.assertEqual(resp.get("status"), "failed")
        self.assertEqual(resp.get("error", {}).get("code"), "EXPIRED")
        self.assertIsNone(self.app.life_service.get_reminder("rem-chain-expired"))

    def test_malformed_line_does_not_break_the_chain(self) -> None:
        """Garbage on the wire is dropped, and the link keeps serving valid requests."""
        self.firmware.dispatch_malformed_line("this is not json at all")

        resp = self.tool_request_roundtrip(
            "req-chain-after-garbage", "note.create",
            {"note_id": "note-after-garbage", "title": "Still alive", "content": "ok"},
        )
        self.assertEqual(resp.get("status"), "succeeded")
        self.assertIsNotNone(self.app.life_service.get_note("note-after-garbage"))

    # ---------------------------------------------------------------------
    # 7. Resilience: reconnect after the device link drops
    # ---------------------------------------------------------------------
    def test_adapter_reconnects_and_chain_resumes_after_link_drop(self) -> None:
        """After the TCP link drops, the adapter re-handshakes and the chain resumes."""
        self.assertEqual(self.firmware.handshake_count, 1, "Expected exactly one initial handshake")

        self.firmware.drop_connection()  # Abrupt device-side disconnect

        # Assert on the deterministic signal (a second handshake) rather than on an
        # observable disconnected window: with a 0.2s reconnect interval the adapter
        # can re-establish the link between two polls.
        self.assertTrue(
            wait_until(lambda: self.firmware.handshake_count >= 2, timeout=10.0),
            "Adapter must reconnect and re-run sys.client.hello after the link drop",
        )
        self.assertTrue(
            wait_until(lambda: self.adapter.is_connected, timeout=10.0),
            "Adapter must report a live link after reconnecting",
        )

        resp = self.tool_request_roundtrip(
            "req-chain-after-reconnect", "reminder.create",
            {"reminder_id": "rem-after-reconnect", "title": "Recovered",
             "due_at": "2030-06-01T09:00:00Z"},
        )
        self.assertEqual(resp.get("status"), "succeeded")
        self.assertIsNotNone(self.app.life_service.get_reminder("rem-after-reconnect"))


if __name__ == "__main__":
    unittest.main()
