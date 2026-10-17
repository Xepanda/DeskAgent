"""
P1 remediation evidence (A3, host side): hardware-fault propagation to the
OUTERMOST response envelope.

Background
----------
A1 is fixing the firmware hardware layer
(`firmware/components/desk_hardware/src/desk_hardware.c`) so that
`desk_hardware_motion_move` / `desk_hardware_motion_stop` propagate driver
errors unconditionally instead of swallowing them unless `strict_hardware_mode`
was on. Before that fix a real driver failure could return success, the SDK
bridge replied `accepted`, and the host reported success.

Scope of THIS file (important)
------------------------------
These tests drive the host with a **pure-Python fake device** speaking the
wire protocol over a `socketpair` (the same pattern as
`tests/core/test_motion_ack_semantics.py`). They prove the HOST end of the
chain: *given* that the device reports a hardware fault on the wire, the host
emits the correct contract envelope and never fabricates success.

They are NOT evidence that the real firmware propagates driver errors. That
claim lives in A1's firmware unit tests and A2's firmware/bridge contract
tests; a fake device here cannot and must not stand in for it.

What is asserted is the outermost response envelope (the contract surface,
`ResponseEnvelope.to_dict()` / its JSON serialisation) and the final observable
state -- never an internal "confirmed"/"accepted" flag.

Covers:
  1. sys.nack + error.code=HARDWARE_FAULT (move)  -> failed, no result/executed
  2. sys.nack + error.code=HARDWARE_FAULT (stop)  -> failed, no result/executed
  3. request_id preservation: envelope.request_id == the request that caused it
  4. firmware sys.ack -> host `accepted` + state_confirmed:false (never
     succeeded/completed/executed)  -- CR-002 boundary
  5. resource re-acquirable: a later request after a failed motion still works
"""

from __future__ import annotations

import json
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any, Callable, Optional

from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter
from host.desk_agent.core.models import RequestEnvelope
from host.desk_agent.main import DeskAgentHostApp


class FakeFirmware:
    """Minimal pure-Python fake device driving one end of a socketpair.

    Deliberately dumb: it echoes back whatever `policy` returns, correlated by
    the wire `request_id` it received. It is a test double, NOT the real
    firmware -- it cannot prove anything about real driver-error propagation.
    """

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


def _nack_hardware_fault(msg: dict) -> dict:
    """Wire shape A1's fixed firmware is expected to emit for a driver fault."""
    return {
        "type": "sys.nack",
        "code": 500,
        "request_id": msg.get("request_id"),
        "error": {"code": "HARDWARE_FAULT", "message": "gimbal driver reported a stall"},
    }


def _ack_accepted(msg: dict) -> dict:
    """A plain success ACK. Per interfaces.md 6.1 this means accepted only."""
    return {
        "type": "sys.ack",
        "code": 0,
        "request_id": msg.get("request_id"),
        "data": {"status": "accepted", "accepted": True},
    }


class HardwareFaultEnvelopeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        cfg_file = Path(self.temp_dir.name) / "config.json"
        cfg_file.write_text(json.dumps({
            "storage": {"db_path": str(Path(self.temp_dir.name) / "hwfault.db")},
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
    def _attach_fake_device(self, policy: Callable[[dict], Optional[dict]]) -> FakeFirmware:
        client_sock, device_sock = socket.socketpair()
        adapter = RobotAdapter(auto_pair=False)
        adapter._sock = client_sock
        adapter._connected = True
        firmware = FakeFirmware(device_sock, policy)
        firmware.start()
        self._closers.append(firmware.stop)
        self._closers.append(client_sock.close)
        self.app._robot_client = adapter
        return firmware

    def _dispatch(self, request_id: str, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """Dispatches through the app router and returns the OUTERMOST envelope dict."""
        return self.app.router.dispatch(RequestEnvelope(
            schema_version="1.0.0",
            request_id=request_id,
            operation=operation,
            arguments=arguments,
            expires_at="2030-01-01T00:00:00Z",
        )).to_dict()

    @staticmethod
    def _move_args() -> dict[str, Any]:
        return {"pan_deg": 30.0, "tilt_deg": 120.0, "duration_ms": 200}

    def _assert_failed_hardware_fault(self, resp: dict[str, Any]) -> None:
        """Shared assertions for requirements 1 & 2, run against the envelope."""
        # Envelope status: failed, and never any success-ish synonym.
        self.assertEqual(resp["status"], "failed")
        self.assertNotIn(resp["status"], ("accepted", "succeeded", "completed", "ok"))
        # Error code is the frozen contract vocabulary value.
        self.assertEqual(resp["error"]["code"], "HARDWARE_FAULT")
        self.assertFalse(resp["error"]["retryable"], "HARDWARE_FAULT is not retryable per contracts/error_codes.json")
        # A failure must carry NO result payload.
        self.assertNotIn("result", resp)
        # The host-invented `executed` field must not reappear anywhere.
        self.assertNotIn("executed", resp)
        self.assertNotIn("executed", resp.get("error", {}))
        self.assertNotIn("executed", resp.get("error", {}).get("details", {}))
        # Serialised form must be equally clean (proves it is not a to_dict artefact).
        raw = json.dumps(resp)
        self.assertNotIn('"executed"', raw)
        self.assertNotIn('"result"', raw)
        self.assertNotIn("succeeded", raw)
        self.assertNotIn("completed", raw)

    # -- 1. motion.move hardware fault -----------------------------------
    def test_motion_move_hardware_fault_envelope(self) -> None:
        """Req 1: sys.nack/HARDWARE_FAULT -> failed envelope, no result, no executed."""
        firmware = self._attach_fake_device(_nack_hardware_fault)
        resp = self._dispatch("req-hwfault-move-01", "resource.motion.move", self._move_args())

        self._assert_failed_hardware_fault(resp)
        self.assertEqual(resp["error"]["details"]["hardware_link"], "connected")
        # The fake device really did receive a dispatch (the fault is not a
        # local "no hardware" short-circuit).
        self.assertTrue(firmware.received, "no command reached the fake device")

    # -- 2. motion.stop hardware fault -----------------------------------
    def test_motion_stop_hardware_fault_envelope(self) -> None:
        """Req 2: same guarantee for resource.motion.stop."""
        firmware = self._attach_fake_device(_nack_hardware_fault)
        resp = self._dispatch("req-hwfault-stop-01", "resource.motion.stop", {"emergency": True})

        self._assert_failed_hardware_fault(resp)
        self.assertEqual(resp["error"]["details"]["hardware_link"], "connected")
        self.assertTrue(firmware.received, "no command reached the fake device")

    # -- 3. request_id preservation --------------------------------------
    def test_request_id_is_preserved_on_failure(self) -> None:
        """Req 3: the envelope carries the request_id of the request that caused it."""
        self._attach_fake_device(_nack_hardware_fault)

        move_resp = self._dispatch("req-hwfault-rid-move", "resource.motion.move", self._move_args())
        self.assertEqual(move_resp["request_id"], "req-hwfault-rid-move")

        stop_resp = self._dispatch("req-hwfault-rid-stop", "resource.motion.stop", {"emergency": False})
        self.assertEqual(stop_resp["request_id"], "req-hwfault-rid-stop")

    def test_request_id_is_preserved_on_accepted(self) -> None:
        """Req 3 (positive path): an accepted envelope also keeps the caller's id."""
        self._attach_fake_device(_ack_accepted)
        resp = self._dispatch("req-hwfault-rid-ok", "resource.motion.move", self._move_args())
        self.assertEqual(resp["request_id"], "req-hwfault-rid-ok")

    def test_fault_nack_with_foreign_request_id_is_not_reported_as_fault(self) -> None:
        """Req 3 negative control: an uncorrelated nack must not become this caller's fault.

        If the device nacks with a request_id that does not belong to the live
        waiter, the host must not adopt that verdict. It has no correlated
        answer, so the honest outcome is a timeout, not a fabricated
        HARDWARE_FAULT and definitely not success.
        """
        def policy(msg: dict) -> dict:
            reply = _nack_hardware_fault(msg)
            reply["request_id"] = "req-someone-elses-0002"
            return reply

        self._attach_fake_device(policy)
        resp = self._dispatch("req-hwfault-foreign", "resource.motion.move", self._move_args())

        self.assertNotEqual(resp["status"], "succeeded")
        self.assertEqual(resp["status"], "unknown")
        self.assertEqual(resp["error"]["code"], "OPERATION_TIMEOUT")
        self.assertNotIn("result", resp)

    # -- 4. success ACK is only ever `accepted` (CR-002) -----------------
    def test_move_ack_is_accepted_not_succeeded(self) -> None:
        """Req 4: a firmware sys.ack yields accepted + state_confirmed:false."""
        self._attach_fake_device(_ack_accepted)
        resp = self._dispatch("req-hwfault-ack-move", "resource.motion.move", self._move_args())

        self.assertEqual(resp["status"], "accepted")
        self.assertNotIn(resp["status"], ("succeeded", "completed"))
        self.assertFalse(resp["result"]["state_confirmed"])
        self.assertNotEqual(resp["result"].get("executed"), True)
        self.assertNotIn("executed", resp["result"])
        self.assertNotIn("executed", resp)

    def test_stop_ack_is_accepted_not_succeeded(self) -> None:
        """Req 4 for stop: same CR-002 boundary."""
        self._attach_fake_device(_ack_accepted)
        resp = self._dispatch("req-hwfault-ack-stop", "resource.motion.stop", {"emergency": True})

        self.assertEqual(resp["status"], "accepted")
        self.assertNotIn(resp["status"], ("succeeded", "completed"))
        self.assertFalse(resp["result"]["state_confirmed"])
        self.assertNotIn("executed", resp["result"])
        self.assertNotIn("executed", resp)

    # -- 5. resource re-acquirable after a fault -------------------------
    def test_motion_is_reacquirable_after_a_hardware_fault(self) -> None:
        """Req 5: a failed motion must not permanently wedge the resource.

        The fake device faults the first dispatch and then accepts the second;
        the later request (with its own request_id) must still be `accepted`.
        """
        replies = iter(["fault", "ack"])

        def policy(msg: dict) -> dict:
            return _nack_hardware_fault(msg) if next(replies) == "fault" else _ack_accepted(msg)

        firmware = self._attach_fake_device(policy)

        first = self._dispatch("req-hwfault-reacq-01", "resource.motion.move", self._move_args())
        self.assertEqual(first["status"], "failed")
        self.assertEqual(first["error"]["code"], "HARDWARE_FAULT")

        second = self._dispatch("req-hwfault-reacq-02", "resource.motion.move", self._move_args())
        self.assertEqual(second["status"], "accepted")
        self.assertFalse(second["result"]["state_confirmed"])
        self.assertEqual(len(firmware.received), 2, "the retry never reached the device")


if __name__ == "__main__":
    unittest.main()
