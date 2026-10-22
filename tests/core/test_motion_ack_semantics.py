"""
WP1-A3 / WP2-A3 regression tests.

Covers the A0 rulings in docs/implementation/interfaces.md 6.1 (ACK confirmation
level semantics) and 6.2 (request correlation):

* A firmware `sys.ack` means "accepted", never "completed"/"succeeded".
* Driver failure -> failed + HARDWARE_FAULT; arbitration refusal -> RESOURCE_BUSY;
  disconnect -> UPSTREAM_UNAVAILABLE; silence -> unknown + OPERATION_TIMEOUT.
* The removed host-invented `executed` field must never reappear.
* Responses are only ever delivered to the waiter that owns the exact
  `request_id`; uncorrelated / late ACKs are discarded, never reused.
* `request_id` values are collision-free and well formed under concurrency.

These assert on the outbound response envelope (the contract surface) and on the
final observable state, not on an internal "confirmed" flag.
"""

from __future__ import annotations

import json
import re
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Optional

from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter
from host.desk_agent.core.models import RequestEnvelope
from host.desk_agent.main import DeskAgentHostApp

REQUEST_ID_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


class FakeFirmware:
    """Drives the far end of a socketpair as a minimal fake TClaw firmware."""

    def __init__(self, sock: socket.socket, policy: Callable[[dict], Optional[dict]]) -> None:
        self._sock = sock
        self._policy = policy
        self.received: list[dict] = []
        self._buf = ""
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        self._sock.settimeout(0.1)
        while not self._stop.is_set():
            try:
                data = self._sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                break
            if not data:
                break
            self._buf += data.decode("utf-8", "replace")
            while "\n" in self._buf:
                line, self._buf = self._buf.split("\n", 1)
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                self.received.append(msg)
                reply = self._policy(msg)
                if reply is not None:
                    try:
                        self._sock.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                    except OSError:
                        return


class MotionEnvelopeSemanticsTestCase(unittest.TestCase):
    """WP1-A3: envelope-level mapping of hardware outcomes."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        cfg_file = Path(self.temp_dir.name) / "config.json"
        cfg_file.write_text(json.dumps({
            "storage": {"db_path": str(Path(self.temp_dir.name) / "semantics.db")},
            "robot": {"enabled": False},
            "home_assistant": {"subscribe_events": False},
        }))
        self.app = DeskAgentHostApp(config_path=str(cfg_file))
        self._closers: list[Callable[[], None]] = []

    def tearDown(self) -> None:
        for closer in self._closers:
            try:
                closer()
            except Exception:
                pass
        self.app.close()
        self.temp_dir.cleanup()

    # -- helpers ---------------------------------------------------------
    def _attach_real_adapter(self, policy: Callable[[dict], Optional[dict]]) -> RobotAdapter:
        client_sock, firmware_sock = socket.socketpair()
        adapter = RobotAdapter(auto_pair=False)
        adapter._sock = client_sock
        adapter._connected = True
        firmware = FakeFirmware(firmware_sock, policy)
        firmware.start()
        self._closers.append(firmware.stop)
        self._closers.append(client_sock.close)
        self.app._robot_client = adapter
        return adapter

    def _dispatch(self, request_id: str, operation: str, arguments: dict[str, Any]):
        return self.app.router.dispatch(RequestEnvelope(
            schema_version="1.0.0",
            request_id=request_id,
            operation=operation,
            arguments=arguments,
            expires_at="2030-01-01T00:00:00Z",
        ))

    @staticmethod
    def _move_args() -> dict[str, Any]:
        return {"pan_deg": 30.0, "tilt_deg": 120.0, "duration_ms": 200}

    # -- driver failure --------------------------------------------------
    def test_driver_failure_yields_failed_hardware_fault(self) -> None:
        def policy(msg: dict) -> dict:
            return {
                "type": "sys.nack",
                "code": 500,
                "request_id": msg.get("request_id"),
                "error": {"code": "HARDWARE_FAULT", "message": "servo stall detected"},
            }

        self._attach_real_adapter(policy)
        resp = self._dispatch("req-sem-fault-01", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "failed")
        self.assertEqual(resp["error"]["code"], "HARDWARE_FAULT")
        self.assertFalse(resp["error"]["retryable"])
        self.assertNotIn("result", resp)

    def test_bare_nack_without_error_code_yields_hardware_fault(self) -> None:
        self._attach_real_adapter(lambda msg: {
            "type": "sys.nack", "code": 500, "request_id": msg.get("request_id"),
        })
        resp = self._dispatch("req-sem-fault-02", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "failed")
        self.assertEqual(resp["error"]["code"], "HARDWARE_FAULT")
        self.assertNotIn("result", resp)

    # -- no ACK / timeout ------------------------------------------------
    def test_no_ack_yields_unknown_operation_timeout(self) -> None:
        # Firmware that never answers (link silently drops the command).
        self._attach_real_adapter(lambda msg: None)
        resp = self._dispatch("req-sem-timeout-01", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "unknown")
        self.assertNotEqual(resp["status"], "succeeded")
        self.assertEqual(resp["error"]["code"], "OPERATION_TIMEOUT")
        self.assertTrue(resp["error"]["retryable"])
        self.assertNotIn("result", resp)

    # -- accepted but unconfirmed ---------------------------------------
    def test_accepted_ack_is_not_reported_as_success(self) -> None:
        def policy(msg: dict) -> dict:
            return {
                "type": "sys.ack",
                "code": 0,
                "request_id": msg.get("request_id"),
                "data": {"status": "accepted", "accepted": True},
            }

        self._attach_real_adapter(policy)
        resp = self._dispatch("req-sem-accepted-01", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "accepted")
        self.assertNotIn(resp["status"], ("succeeded", "completed"))
        self.assertTrue(resp["result"]["accepted"])
        self.assertFalse(resp["result"]["state_confirmed"])
        self.assertNotIn("executed", resp["result"])
        self.assertNotIn("executed", resp)

    def test_legacy_completed_claim_is_downgraded_to_accepted(self) -> None:
        """A link that merely ACKs must never be able to produce a success envelope."""

        class LegacyOverclaimingClient:
            is_connected = True

            def send_motion_move(self, pan, tilt, duration_ms=500):
                return {"status": "completed", "confirmed": True, "pan": pan, "tilt": tilt}

        self.app._robot_client = LegacyOverclaimingClient()
        resp = self._dispatch("req-sem-legacy-01", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "accepted")
        self.assertNotIn(resp["status"], ("succeeded", "completed"))
        self.assertFalse(resp["result"]["state_confirmed"])
        self.assertNotIn("executed", resp["result"])

    # -- arbitration / disconnect propagation ----------------------------
    def test_resource_busy_refusal_propagates(self) -> None:
        def policy(msg: dict) -> dict:
            return {
                "type": "sys.nack",
                "code": 409,
                "request_id": msg.get("request_id"),
                "error": {"code": "RESOURCE_BUSY", "message": "camera lease holds the gimbal"},
            }

        self._attach_real_adapter(policy)
        resp = self._dispatch("req-sem-busy-01", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "failed")
        self.assertEqual(resp["error"]["code"], "RESOURCE_BUSY")
        self.assertTrue(resp["error"]["retryable"])
        self.assertNotIn("result", resp)

    def test_link_exception_propagates_as_upstream_unavailable(self) -> None:
        class DroppingClient:
            is_connected = True

            def send_motion_move(self, pan, tilt, duration_ms=500):
                raise ConnectionError("socket closed mid-dispatch")

        self.app._robot_client = DroppingClient()
        resp = self._dispatch("req-sem-drop-01", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "failed")
        self.assertEqual(resp["error"]["code"], "UPSTREAM_UNAVAILABLE")
        self.assertTrue(resp["error"]["retryable"])
        self.assertNotIn("result", resp)

    def test_disconnected_link_is_not_reported_as_success(self) -> None:
        self.app._robot_client = None
        resp = self._dispatch("req-sem-nolink-01", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "failed")
        self.assertNotEqual(resp["status"], "succeeded")
        self.assertEqual(resp["error"]["code"], "UPSTREAM_UNAVAILABLE")
        self.assertTrue(resp["error"]["retryable"])
        self.assertNotIn("result", resp)
        self.assertEqual(resp["error"]["details"]["reason"], "NO_PHYSICAL_ROBOT_CONNECTED")

    # -- motion.stop parity ----------------------------------------------
    def test_motion_stop_accepted_is_not_reported_as_success(self) -> None:
        self._attach_real_adapter(lambda msg: {
            "type": "sys.ack", "code": 0, "request_id": msg.get("request_id"),
            "data": {"status": "accepted", "accepted": True},
        })
        resp = self._dispatch(
            "req-sem-stop-01", "resource.motion.stop", {"emergency": True}
        ).to_dict()

        self.assertEqual(resp["status"], "accepted")
        self.assertNotIn(resp["status"], ("succeeded", "completed"))
        self.assertTrue(resp["result"]["accepted"])
        self.assertFalse(resp["result"]["state_confirmed"])
        self.assertNotIn("executed", resp["result"])

    def test_motion_stop_timeout_is_unknown_operation_timeout(self) -> None:
        self._attach_real_adapter(lambda msg: None)
        resp = self._dispatch(
            "req-sem-stop-02", "resource.motion.stop", {"emergency": False}
        ).to_dict()

        self.assertEqual(resp["status"], "unknown")
        self.assertEqual(resp["error"]["code"], "OPERATION_TIMEOUT")
        self.assertTrue(resp["error"]["retryable"])

    def test_state_unconfirmed_error_code_propagates(self) -> None:
        def policy(msg: dict) -> dict:
            return {
                "type": "sys.nack",
                "code": 500,
                "request_id": msg.get("request_id"),
                "error": {"code": "STATE_UNCONFIRMED", "message": "physical state confirmation timeout"},
            }

        self._attach_real_adapter(policy)
        resp = self._dispatch("req-sem-unconfirmed-01", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "unknown")
        self.assertEqual(resp["error"]["code"], "STATE_UNCONFIRMED")
        self.assertFalse(resp["error"]["retryable"])
        self.assertNotIn("result", resp)

    def test_overclaiming_client_state_confirmed_strictly_enforced_false(self) -> None:
        """CR-002: In open-loop PWM without encoder, state_confirmed is strictly False on Host."""
        class MaliciousOverclaimingClient:
            is_connected = True

            def send_motion_move(self, pan, tilt, duration_ms=500):
                return {
                    "status": "accepted",
                    "accepted": True,
                    "state_confirmed": True,
                    "pan": pan,
                    "tilt": tilt,
                }

        self.app._robot_client = MaliciousOverclaimingClient()
        resp = self._dispatch("req-sem-cr002-clamp", "resource.motion.move", self._move_args()).to_dict()

        self.assertEqual(resp["status"], "accepted")
        self.assertFalse(resp["result"]["state_confirmed"])
        self.assertTrue(resp["result"]["accepted"])


class RobotAdapterCorrelationTestCase(unittest.TestCase):
    """WP2-A3: strict request/response correlation in RobotAdapter."""

    def _make_adapter(self) -> tuple[RobotAdapter, socket.socket, socket.socket]:
        client_sock, firmware_sock = socket.socketpair()
        adapter = RobotAdapter(auto_pair=False)
        adapter._sock = client_sock
        adapter._connected = True
        # Let the adapter's own worker thread own recv(). Without it, concurrent
        # send_command callers fall back to a shared blocking recv() on the same
        # socket, and one waiter can drain a coalesced ACK segment that also
        # belonged to another waiter.
        adapter.start()
        self.addCleanup(adapter.stop)
        self.addCleanup(firmware_sock.close)
        self.addCleanup(client_sock.close)
        return adapter, client_sock, firmware_sock

    def test_reverse_order_acks_reach_their_own_waiters(self) -> None:
        adapter, _client, firmware_sock = self._make_adapter()
        results: dict[int, dict] = {}

        def firmware() -> None:
            buf = ""
            msgs: list[dict] = []
            firmware_sock.settimeout(5.0)
            while len(msgs) < 2:
                data = firmware_sock.recv(4096)
                if not data:
                    return
                buf += data.decode("utf-8", "replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if line.strip():
                        msgs.append(json.loads(line))
            # Reply in REVERSE arrival order to prove there is no cross-talk.
            for msg in reversed(msgs):
                ack = {
                    "type": "sys.ack",
                    "code": 0,
                    "request_id": msg["request_id"],
                    "data": {"status": "accepted", "seq": msg["data"]["seq"]},
                }
                firmware_sock.sendall((json.dumps(ack) + "\n").encode("utf-8"))

        ft = threading.Thread(target=firmware, daemon=True)
        ft.start()

        def call(seq: int) -> None:
            results[seq] = adapter.send_command(
                {"type": "ctrl.motion.move", "data": {"seq": seq}}, timeout=3.0
            )

        t1 = threading.Thread(target=call, args=(1,))
        t2 = threading.Thread(target=call, args=(2,))
        t1.start()
        time.sleep(0.05)
        t2.start()
        t1.join(timeout=6.0)
        t2.join(timeout=6.0)
        ft.join(timeout=2.0)

        self.assertEqual(results[1]["data"]["seq"], 1, "waiter 1 received the wrong response")
        self.assertEqual(results[2]["data"]["seq"], 2, "waiter 2 received the wrong response")
        self.assertNotEqual(results[1]["request_id"], results[2]["request_id"])

    def test_uncorrelated_ack_without_request_id_is_discarded(self) -> None:
        adapter, _client, _firmware = self._make_adapter()
        result: dict[str, dict] = {}

        def call() -> None:
            result["r"] = adapter.send_command(
                {"type": "ctrl.motion.move", "request_id": "req-uncorr-0001", "data": {}},
                timeout=0.4,
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.05)
        # Legacy firmware ACK that forgets to echo request_id.
        adapter._handle_incoming_line(json.dumps(
            {"type": "sys.ack", "code": 0, "data": {"status": "accepted"}}
        ))
        t.join(timeout=3.0)

        self.assertEqual(result["r"]["code"], 408, "an uncorrelated ACK must never satisfy a waiter")
        self.assertEqual(result["r"]["status"], "timeout")

    def test_foreign_request_id_ack_does_not_satisfy_waiter(self) -> None:
        adapter, _client, _firmware = self._make_adapter()
        result: dict[str, dict] = {}

        def call() -> None:
            result["r"] = adapter.send_command(
                {"type": "ctrl.motion.move", "request_id": "req-mine-0001", "data": {}},
                timeout=0.4,
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.05)
        adapter._handle_incoming_line(json.dumps(
            {"type": "sys.ack", "code": 0, "request_id": "req-someone-else-0002", "data": {}}
        ))
        t.join(timeout=3.0)

        self.assertEqual(result["r"]["code"], 408)
        self.assertNotIn("req-someone-else-0002", adapter._pending_responses)

    def test_late_ack_after_timeout_is_discarded(self) -> None:
        adapter, _client, _firmware = self._make_adapter()

        # 1. First request times out (firmware stays silent past the deadline).
        first = adapter.send_command(
            {"type": "ctrl.motion.move", "request_id": "req-late-0001", "data": {}}, timeout=0.15
        )
        self.assertEqual(first["code"], 408)
        self.assertNotIn("req-late-0001", adapter._pending_responses)

        # 2. The late ACK for request 1 now arrives.
        adapter._handle_incoming_line(json.dumps(
            {"type": "sys.ack", "code": 0, "request_id": "req-late-0001", "data": {"status": "accepted"}}
        ))
        self.assertNotIn(
            "req-late-0001", adapter._pending_responses,
            "a late ACK must be discarded, not buffered for a future waiter",
        )

        # 3. A brand-new request must not be satisfied by the stale ACK.
        second = adapter.send_command(
            {"type": "ctrl.motion.move", "request_id": "req-late-0002", "data": {}}, timeout=0.2
        )
        self.assertEqual(second["code"], 408, "stale ACK must not leak into a later request")

    def _make_workerless_adapter(self) -> tuple[RobotAdapter, socket.socket, socket.socket]:
        """Adapter with no receiver worker: forces send_command's fallback read path."""
        client_sock, firmware_sock = socket.socketpair()
        adapter = RobotAdapter(auto_pair=False)
        adapter._sock = client_sock
        adapter._connected = True
        self.addCleanup(firmware_sock.close)
        self.addCleanup(client_sock.close)
        return adapter, client_sock, firmware_sock

    def test_fallback_read_delivers_coalesced_acks_to_both_waiters(self) -> None:
        """Without a worker thread, one coalesced segment must still satisfy both waiters."""
        for iteration in range(25):
            adapter, _client, firmware_sock = self._make_workerless_adapter()
            results: dict[int, dict] = {}

            def call(seq: int, _adapter: RobotAdapter = adapter) -> None:
                results[seq] = _adapter.send_command(
                    {
                        "type": "ctrl.motion.move",
                        "request_id": f"req-coal-{seq}",
                        "data": {"seq": seq},
                    },
                    timeout=1.0,
                )

            t1 = threading.Thread(target=call, args=(1,))
            t2 = threading.Thread(target=call, args=(2,))
            t1.start()
            t2.start()
            wait_deadline = time.time() + 1.0
            while time.time() < wait_deadline:
                if "req-coal-1" in adapter._awaiting_ids and "req-coal-2" in adapter._awaiting_ids:
                    break
                time.sleep(0.005)
            time.sleep(0.02)  # let both waiters enter the fallback loop
            coalesced = "".join(
                json.dumps({
                    "type": "sys.ack",
                    "code": 0,
                    "request_id": f"req-coal-{seq}",
                    "data": {"status": "accepted", "seq": seq},
                }) + "\n"
                for seq in (1, 2)
            )
            firmware_sock.sendall(coalesced.encode("utf-8"))
            t1.join(timeout=3.0)
            t2.join(timeout=3.0)

            self.assertIn(1, results, f"iteration {iteration}: waiter 1 was starved by the fallback reader")
            self.assertIn(2, results, f"iteration {iteration}: waiter 2 was starved by the fallback reader")
            self.assertEqual(results[1]["data"]["seq"], 1)
            self.assertEqual(results[2]["data"]["seq"], 2)

    def test_fallback_read_respects_deadline(self) -> None:
        """The worker-less fallback must never block past the caller's deadline."""
        adapter, _client, _firmware = self._make_workerless_adapter()
        started = time.monotonic()
        res = adapter.send_command(
            {"type": "ctrl.motion.move", "request_id": "req-deadline-0001", "data": {}}, timeout=0.3
        )
        elapsed = time.monotonic() - started

        self.assertEqual(res["code"], 408)
        self.assertEqual(res["status"], "timeout")
        self.assertLess(elapsed, 1.5, "fallback read must respect the timeout budget")

    def test_late_ack_does_not_linger_in_pending_map(self) -> None:
        adapter, _client, _firmware = self._make_adapter()
        adapter.send_command(
            {"type": "ctrl.motion.move", "request_id": "req-leak-0001", "data": {}}, timeout=0.1
        )
        adapter._handle_incoming_line(json.dumps(
            {"type": "sys.ack", "code": 0, "request_id": "req-leak-0001", "data": {}}
        ))
        self.assertEqual(adapter._pending_responses, {})
        self.assertEqual(adapter._awaiting_ids, set())

    def test_request_ids_are_unique_and_well_formed_under_concurrency(self) -> None:
        adapter, _client, firmware_sock = self._make_adapter()
        seen: list[str] = []
        seen_lock = threading.Lock()
        stop = threading.Event()

        def drain() -> None:
            firmware_sock.settimeout(0.1)
            buf = ""
            while not stop.is_set():
                try:
                    data = firmware_sock.recv(65536)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not data:
                    break
                buf += data.decode("utf-8", "replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if line.strip():
                        with seen_lock:
                            seen.append(json.loads(line)["request_id"])

        drain_thread = threading.Thread(target=drain, daemon=True)
        drain_thread.start()

        def worker() -> None:
            for _ in range(20):
                adapter.send_command({"type": "ctrl.motion.move", "data": {}}, timeout=0.02)

        workers = [threading.Thread(target=worker) for _ in range(8)]
        for w in workers:
            w.start()
        for w in workers:
            w.join(timeout=15.0)
        stop.set()
        drain_thread.join(timeout=2.0)

        self.assertEqual(len(seen), 160, "expected one request_id per send_command call")
        self.assertEqual(len(set(seen)), 160, "request_id collided under concurrency")
        for rid in seen:
            self.assertRegex(rid, REQUEST_ID_RE)
            self.assertGreaterEqual(len(rid), 8)
            self.assertLessEqual(len(rid), 64)

    def test_generated_request_ids_are_unique_across_threads(self) -> None:
        adapter = RobotAdapter(auto_pair=False)
        ids: list[str] = []
        lock = threading.Lock()

        def worker() -> None:
            local = [adapter._new_request_id() for _ in range(250)]
            with lock:
                ids.extend(local)

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        self.assertEqual(len(ids), 2000)
        self.assertEqual(len(set(ids)), 2000)
        for rid in ids:
            self.assertRegex(rid, REQUEST_ID_RE)
            self.assertGreaterEqual(len(rid), 8)
            self.assertLessEqual(len(rid), 64)

    def test_disconnect_clears_residual_buffer(self) -> None:
        """interfaces.md 6.3 rule 4: Disconnection must clear residual buffer, never concatenate across reconnects."""
        adapter = RobotAdapter(auto_pair=False)
        adapter._recv_buf = '{"type":"sys.partial'
        adapter._close_socket()
        self.assertEqual(adapter._recv_buf, "", "residual buffer must be cleared upon socket close")

    def test_oversized_frame_discarded(self) -> None:
        """interfaces.md 6.3 rule 2: Oversized frame must be discarded and not corrupt subsequent messages."""
        adapter, _client, firmware_sock = self._make_adapter()

        # Send an oversized junk string without newline (> 65536 bytes) followed by a valid ACK
        large_junk = "X" * 70000 + "\n"
        valid_ack = json.dumps({
            "type": "sys.ack",
            "code": 0,
            "request_id": "req-oversize-001",
            "data": {"status": "accepted"},
        }) + "\n"

        result: dict[str, Any] = {}

        def call() -> None:
            result["res"] = adapter.send_command(
                {"type": "ctrl.motion.move", "request_id": "req-oversize-001", "data": {}},
                timeout=2.0,
            )

        t = threading.Thread(target=call)
        t.start()
        time.sleep(0.05)
        firmware_sock.sendall(large_junk.encode("utf-8") + valid_ack.encode("utf-8"))
        t.join(timeout=3.0)

        self.assertIn("res", result)
        self.assertEqual(result["res"].get("code"), 0)
        self.assertEqual(result["res"].get("request_id"), "req-oversize-001")


if __name__ == "__main__":
    unittest.main()
