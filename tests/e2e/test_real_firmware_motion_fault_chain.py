"""
A7 cross-layer chain evidence: real compiled C firmware -> real `RobotAdapter`
-> real `DeskAgentHostApp` -> OUTERMOST response envelope.

Requirement under test
----------------------
    "驱动错误经固件协议、Adapter、Host 后，外层响应也正确失败"

A gimbal driver fault is injected at the *bottom* of the stack -- inside the real
compiled C hardware layer -- and the test asserts on the *outermost* contract
surface the Host produces (`ResponseEnvelope.to_dict()`), after the fault has
travelled through every hop:

    [test] desk_hardware_register_driver_ops(failing)      <- real C driver
      -> desk_hardware_motion_move_to() / _stop()          <- real C HAL (A1)
      -> desk_sdk_bridge_process_command() (sys.nack)      <- real C protocol (A2)
      -> TCP socket (real socket, newline framing)
      -> RobotAdapter.send_command()/_classify_response()  <- real host adapter
      -> DeskAgentHostApp Router -> _handle_motion_move()  <- real host router
      -> tool.response envelope written back over the wire <- OUTERMOST envelope

Nothing on this path is a Python fake. The device side is the actual compiled
`desk_sdk_bridge` shared library, driven through ctypes exactly the way
`tests/e2e/test_p1_regression_gates.py::PairingFailClosedGate` drives it
(`desk_sdk_socket_server_start(0)` + `desk_sdk_bridge_process_command`).

===========================================================================
EVIDENCE LAYERING (AGENTS.md §4) -- the three layers must not be conflated
===========================================================================
This final-P1 fix is covered at four distinct layers. They are NOT
interchangeable; each proves something the others cannot:

  Layer 1 -- DIRECT C FUNCTION TESTS (A1)
      tests/firmware_contract/test_desk_hardware_motion_fault_propagation.py
      Calls `desk_hardware_motion_move_to()` / `_stop()` directly and asserts
      the returned error code. Proves the C HAL contract; says nothing about
      the wire protocol or the Host.

  Layer 2 -- REAL C PROTOCOL TESTS (A2)
      tests/sdk_contract/test_sdk_bridge_protocol.py (WP1b block)
      Drives the real compiled `desk_sdk_bridge_process_command()` at DEFAULT
      init (`strict_mode == false`) and asserts the `sys.nack` / `HARDWARE_FAULT`
      wire message. Proves the device-side protocol reply; says nothing about
      the Host.

  Layer 3 -- PYTHON FAKE-DEVICE TESTS (A3)
      tests/core/test_hardware_fault_envelope.py
      Drives the real Host against a *pure-Python* fake device. Proves the Host
      end of the chain; the fake device CANNOT prove that real firmware
      propagates driver errors.

  Layer 4 -- FULL CROSS-LAYER CHAIN (A7) -- THIS FILE
      The only layer where a fault originating in the real C driver is carried
      through the real C protocol, the real Adapter and the real Host, and is
      asserted on the outermost envelope. It is the join of Layers 1-3; it does
      not replace any of them.

Evidence level (AGENTS.md §4): every assertion here is **Level 2** (software
protocol / simulated PoC). No ESP32-S3 board, no Tuya cloud, no LAN Home
Assistant and no Windows Codex host is present, so this is NOT G1 acceptance
and NOT Level 3/4. See docs/acceptance/acceptance-matrix.md.

CR-002 boundary (docs/implementation/change-requests/CR-002-...)
---------------------------------------------------------------
A successful motion ACK means `accepted` + `state_confirmed: false` -- never
`succeeded`/`completed`. No physical-completion semantics are asserted anywhere
in this module (the ACK->completion correlation does not exist yet).

Covered requirements (task items 1-6):
  1. move driver fault  -> outer envelope `failed` + `HARDWARE_FAULT`, no
     `result`, no `executed`, not accepted/succeeded/completed
  2. stop driver fault  -> same
  3. `request_id` preserved end to end (outer envelope carries the request's id)
  4. the run is at DEFAULT init / `strict_mode == false` (the broken config)
  5. resource re-acquirable: a later motion after the fault is `accepted`
     (no leaked gimbal lock)
  6. CR-002 boundary: a successful ACK is `accepted` + `state_confirmed: false`
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Optional

from host.desk_agent.main import DeskAgentHostApp

REPO_ROOT = Path(__file__).resolve().parents[2]

# Far-future expiry so the envelope survives the Router's expiration gate.
FUTURE_EXPIRY = "2030-01-01T00:00:00Z"

# --- C constants (desk_hardware.h / desk_arbiter.h) -------------------------
DESK_HW_OK = 0
DESK_HW_ERR_STALL = -6

DESK_OWNER_NONE = 0
DESK_RES_MOTION_GIMBAL = 3
DESK_ARBITER_OK = 0

# --- ctypes signatures ------------------------------------------------------
DRIVER_MOTION_MOVE_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_uint32)
DRIVER_MOTION_STOP_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_bool)
MCU_TX_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p)
TOOL_RESP_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)


class DeskBoardDriverOps(ctypes.Structure):
    """Mirror of `desk_board_driver_ops_t` (desk_hardware.h)."""

    _fields_ = [
        ("driver_name", ctypes.c_char_p),
        ("init", ctypes.c_void_p),
        ("audio_record_read", ctypes.c_void_p),
        ("audio_play_pcm", ctypes.c_void_p),
        ("motion_move", DRIVER_MOTION_MOVE_CB),
        ("motion_stop", DRIVER_MOTION_STOP_CB),
        ("camera_capture", ctypes.c_void_p),
        ("display_expression", ctypes.c_void_p),
        ("deinit", ctypes.c_void_p),
    ]


def wait_until(predicate: Callable[[], bool], timeout: float = 5.0, interval: float = 0.02) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


class RealFirmwareMotionFaultChain(unittest.TestCase):
    """Chains the real compiled C bridge into the real Host and asserts the
    outermost envelope for an injected driver fault."""

    # -- one-time C library build -----------------------------------------
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_sdk_bridge_chain.so")
        cmd = [
            "gcc", "-shared", "-fPIC", "-O2", "-Wall",
            f"-I{REPO_ROOT}/firmware/components/desk_hardware/include",
            f"-I{REPO_ROOT}/firmware/components/desk_tclaw/include",
            f"-I{REPO_ROOT}/firmware/components/desk_sdk_bridge/include",
            f"{REPO_ROOT}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{REPO_ROOT}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{REPO_ROOT}/firmware/components/desk_hardware/src/desk_notification.c",
            f"{REPO_ROOT}/firmware/components/desk_hardware/src/desk_watcher_log_source.c",
            f"{REPO_ROOT}/firmware/components/desk_tclaw/src/desk_tclaw_bridge.c",
            f"{REPO_ROOT}/firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c",
            f"{REPO_ROOT}/firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c",
            "-lpthread", "-o", cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile the desk_sdk_bridge chain library: {res.stderr}")

        cls.lib = ctypes.CDLL(cls.so_path)
        lib = cls.lib

        def sig(name: str, restype, argtypes) -> None:
            fn = getattr(lib, name)
            fn.restype = restype
            fn.argtypes = argtypes

        sig("desk_hardware_init", ctypes.c_int, [])
        sig("desk_hardware_register_driver_ops", ctypes.c_int, [ctypes.POINTER(DeskBoardDriverOps)])
        sig("desk_hardware_register_mcu_tx_cb", ctypes.c_int, [MCU_TX_CB, ctypes.c_void_p])
        sig("desk_hardware_is_strict_mode", ctypes.c_bool, [])
        sig("desk_hardware_set_strict_mode", None, [ctypes.c_bool])
        sig("desk_arbiter_get_owner", ctypes.c_int, [ctypes.c_int])

        sig("desk_sdk_bridge_init", ctypes.c_int, [])
        sig("desk_sdk_bridge_reset", None, [])
        sig("desk_sdk_bridge_is_connected", ctypes.c_bool, [])
        sig("desk_sdk_bridge_register_tool_response_cb", ctypes.c_int, [TOOL_RESP_CB, ctypes.c_void_p])
        sig("desk_sdk_bridge_dispatch_tool_request", ctypes.c_int, [ctypes.c_char_p])
        sig("desk_sdk_bridge_process_command", ctypes.c_int,
            [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t])

        sig("desk_sdk_socket_server_start", ctypes.c_int, [ctypes.c_uint16])
        sig("desk_sdk_socket_server_get_port", ctypes.c_int, [])
        sig("desk_sdk_socket_server_poll", ctypes.c_int, [ctypes.c_int])
        sig("desk_sdk_socket_server_stop", ctypes.c_int, [])

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    # -- per-test fixture --------------------------------------------------
    def setUp(self) -> None:
        self.lib.desk_sdk_socket_server_stop()   # drop any leaked listener first
        self.lib.desk_hardware_init()
        self.lib.desk_sdk_bridge_reset()
        self.lib.desk_sdk_bridge_init()

        # Requirement 4: this must run at DEFAULT initialisation. The final P1
        # defect only reproduced here (strict_mode == false); a test that flips
        # the flag would not cover the shipped configuration.
        self.assertFalse(
            self.lib.desk_hardware_is_strict_mode(),
            "this suite must run at default init (strict_mode == false)",
        )

        # Capture the OUTERMOST envelope: the real C bridge hands every
        # `tool.response` body it receives to this callback.
        self.envelopes: list[dict[str, Any]] = []
        self._captured_raw: list[str] = []

        @TOOL_RESP_CB
        def _on_tool_response(ptr, _user_data):
            raw = ctypes.string_at(ptr).decode("utf-8", errors="replace")
            self._captured_raw.append(raw)
            try:
                wrapper = json.loads(raw)
            except Exception:
                return 0
            body = wrapper.get("data") if isinstance(wrapper, dict) else None
            if isinstance(body, dict):
                self.envelopes.append(body)
            return 0

        self._tool_resp_cb = _on_tool_response
        self.assertEqual(self.lib.desk_sdk_bridge_register_tool_response_cb(_on_tool_response, None), 0)

        # Real C bridge TCP server (the firmware side is the TCP *server*).
        self.assertEqual(self.lib.desk_sdk_socket_server_start(0), 0)
        self.fw_port = self.lib.desk_sdk_socket_server_get_port()
        self.assertGreater(self.fw_port, 1024)

        # Injected driver bookkeeping: the counters prove the fault really
        # originated in the C driver callback, not somewhere above it.
        self.driver_calls = {"move": 0, "stop": 0}
        self._keepalive: list[Any] = []

        # Real Host (real RobotAdapter -> real Router -> real LifeService).
        self.host_tmp = tempfile.TemporaryDirectory()
        cfg_file = Path(self.host_tmp.name) / "config.json"
        cfg_file.write_text(json.dumps({
            "storage": {"db_path": str(Path(self.host_tmp.name) / "chain.db")},
            "server": {"host": "127.0.0.1", "port": 0},
            "home_assistant": {"subscribe_events": False},
            "camera": {"default_lease_ms": 5000},
            "robot": {
                "enabled": True,
                "host": "127.0.0.1",
                "tcp_port": self.fw_port,
                "udp_port": self.fw_port + 1,
                "auto_pair": False,
                "reconnect_interval": 0.1,
            },
        }), encoding="utf-8")

        self.app = DeskAgentHostApp(config_path=str(cfg_file))
        self.adapter = self.app.robot_adapter
        self.assertIsNotNone(self.adapter, "Host must instantiate a RobotAdapter")
        self.adapter.start()

        # The C bridge only accepts/answers while it is polled, so the accept +
        # hello handshake has to be pumped from this thread.
        self.assertTrue(
            self._pump_until(lambda: self.adapter.is_connected, timeout=8.0),
            "RobotAdapter failed to complete the hello handshake with the real C bridge",
        )
        self.assertTrue(self.lib.desk_sdk_bridge_is_connected())

    def tearDown(self) -> None:
        try:
            if getattr(self, "app", None) is not None:
                self.app.close()
        finally:
            self.lib.desk_sdk_socket_server_stop()
            if getattr(self, "host_tmp", None) is not None:
                self.host_tmp.cleanup()

    # -- C pump / driver helpers ------------------------------------------
    def _pump_until(self, predicate: Callable[[], bool], timeout: float = 8.0,
                    poll_ms: int = 20) -> bool:
        """Polls the real C bridge socket server until `predicate` holds.

        The C bridge is single-threaded, so every accept / read / reply in the
        chain happens inside these poll calls. Polling is confined to the test
        thread, so the C library is never entered concurrently.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.lib.desk_sdk_socket_server_poll(poll_ms)
            if predicate():
                return True
        return predicate()

    def _register_driver(self, move_rc: int, stop_rc: int) -> None:
        """Installs a board driver whose motion primitives return the given codes."""

        @DRIVER_MOTION_MOVE_CB
        def on_move(pan, tilt, duration):
            self.driver_calls["move"] += 1
            return move_rc

        @DRIVER_MOTION_STOP_CB
        def on_stop(emergency):
            self.driver_calls["stop"] += 1
            return stop_rc

        @MCU_TX_CB
        def on_mcu_tx(_data, _length, _user_data):
            return None

        ops = DeskBoardDriverOps()
        ops.driver_name = b"e2e_chain_driver"
        ops.motion_move = on_move
        ops.motion_stop = on_stop
        # Keep the ctypes callbacks (and the ops struct) alive for this test.
        self._keepalive.extend([on_move, on_stop, on_mcu_tx, ops])
        self.assertEqual(self.lib.desk_hardware_register_mcu_tx_cb(on_mcu_tx, None), 0)
        self.assertEqual(self.lib.desk_hardware_register_driver_ops(ctypes.byref(ops)), 0)

    # -- chain helpers -----------------------------------------------------
    def _dispatch_tool_request(self, request_id: str, operation: str,
                               arguments: dict[str, Any]) -> dict[str, Any]:
        envelope = {
            "schema_version": "1.0.0",
            "request_id": request_id,
            "operation": operation,
            "arguments": arguments,
            "expires_at": FUTURE_EXPIRY,
        }
        rc = self.lib.desk_sdk_bridge_dispatch_tool_request(json.dumps(envelope).encode("utf-8"))
        self.assertEqual(rc, 0, "the real C bridge failed to dispatch the tool request")
        return envelope

    def _await_outer_envelope(self, request_id: str, timeout: float = 10.0) -> dict[str, Any]:
        self.assertTrue(
            self._pump_until(
                lambda: any(e.get("request_id") == request_id for e in self.envelopes),
                timeout=timeout,
            ),
            f"no outermost tool.response envelope for request_id={request_id!r} "
            f"crossed the chain (captured={self.envelopes!r})",
        )
        for env in self.envelopes:
            if env.get("request_id") == request_id:
                return env
        self.fail(f"envelope for {request_id!r} vanished")

    def _roundtrip(self, request_id: str, operation: str,
                   arguments: dict[str, Any], timeout: float = 10.0) -> dict[str, Any]:
        self._dispatch_tool_request(request_id, operation, arguments)
        return self._await_outer_envelope(request_id, timeout=timeout)

    # =====================================================================
    # Requirement 1: move driver fault -> outer envelope fails
    # =====================================================================
    def test_move_driver_fault_fails_outer_envelope(self) -> None:
        """Req 1/3/4: a move-driver fault injected in the real C driver must
        surface as a `failed` / `HARDWARE_FAULT` OUTERMOST envelope carrying the
        request's `request_id`, with no `result` and no `executed`."""
        self.assertFalse(self.lib.desk_hardware_is_strict_mode())
        self._register_driver(DESK_HW_ERR_STALL, DESK_HW_OK)

        env = self._roundtrip(
            "req-chain-fault-move", "resource.motion.move",
            {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 300},
        )

        # The fault really came from the C driver callback (not from above it).
        self.assertGreater(self.driver_calls["move"], 0,
                           "the injected C motion_move driver was never invoked")

        # Req 1: outermost envelope must fail with HARDWARE_FAULT.
        self.assertEqual(env.get("status"), "failed")
        self.assertEqual(env.get("error", {}).get("code"), "HARDWARE_FAULT")
        self.assertNotIn("result", env, "a driver fault must not carry a result payload")
        self.assertNotIn("executed", env)
        self.assertNotIn("executed", json.dumps(env))
        # Req 1: it must not be any success-ish terminal state.
        self.assertNotIn(env.get("status"), ("accepted", "succeeded", "completed"))
        raw = json.dumps(env)
        self.assertNotIn("succeeded", raw)
        self.assertNotIn("completed", raw)

        # Req 3: the request_id survived every hop.
        self.assertEqual(env.get("request_id"), "req-chain-fault-move")

    # =====================================================================
    # Requirement 2: stop driver fault -> outer envelope fails
    # =====================================================================
    def test_stop_driver_fault_fails_outer_envelope(self) -> None:
        """Req 2/3/4: a stop-driver fault injected in the real C driver must
        surface as a `failed` / `HARDWARE_FAULT` OUTERMOST envelope carrying the
        request's `request_id`, with no `result` and no `executed`."""
        self.assertFalse(self.lib.desk_hardware_is_strict_mode())
        self._register_driver(DESK_HW_OK, DESK_HW_ERR_STALL)

        env = self._roundtrip(
            "req-chain-fault-stop", "resource.motion.stop",
            {"emergency": True},
        )

        self.assertGreater(self.driver_calls["stop"], 0,
                           "the injected C motion_stop driver was never invoked")

        self.assertEqual(env.get("status"), "failed")
        self.assertEqual(env.get("error", {}).get("code"), "HARDWARE_FAULT")
        self.assertNotIn("result", env)
        self.assertNotIn("executed", env)
        self.assertNotIn(env.get("status"), ("accepted", "succeeded", "completed"))
        self.assertNotIn("succeeded", json.dumps(env))
        self.assertNotIn("completed", json.dumps(env))

        self.assertEqual(env.get("request_id"), "req-chain-fault-stop")

    # =====================================================================
    # Requirement 5: resource re-acquirable after the fault
    # =====================================================================
    def test_gimbal_is_reacquirable_after_chain_fault(self) -> None:
        """Req 5: after a driver fault the gimbal lock is released (arbiter owner
        back to NONE) and a subsequent motion through the same chain succeeds
        (`accepted`) -- proving no leaked lock survives the fault."""
        self._register_driver(DESK_HW_ERR_STALL, DESK_HW_OK)

        fault = self._roundtrip(
            "req-chain-leak-fault", "resource.motion.move",
            {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 300},
        )
        self.assertEqual(fault.get("status"), "failed")
        self.assertEqual(fault.get("error", {}).get("code"), "HARDWARE_FAULT")

        # Direct read of the real C arbiter: the fault must not leak the lock.
        self.assertEqual(
            self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL), DESK_OWNER_NONE,
            "the gimbal lock was leaked after a driver fault",
        )

        # Re-arm a healthy driver and drive the same chain again: the resource
        # is usable, not wedged.
        self._register_driver(DESK_HW_OK, DESK_HW_OK)
        recovered = self._roundtrip(
            "req-chain-leak-recovered", "resource.motion.move",
            {"pan_deg": 45.0, "tilt_deg": 115.0, "duration_ms": 200},
        )
        self.assertEqual(recovered.get("status"), "accepted")
        self.assertEqual(recovered.get("request_id"), "req-chain-leak-recovered")

    # =====================================================================
    # Requirement 6: CR-002 boundary -- success ACK is accepted, not completed
    # =====================================================================
    def test_successful_motion_ack_is_accepted_with_unconfirmed_state(self) -> None:
        """Req 6 (CR-002 boundary): a *succeeding* motion yields only `accepted`
        + `state_confirmed: false` on the outermost envelope -- never
        `succeeded`/`completed`. No physical-completion semantics are asserted,
        because the ACK->completion correlation does not exist yet (CR-002)."""
        self._register_driver(DESK_HW_OK, DESK_HW_OK)

        env = self._roundtrip(
            "req-chain-accepted", "resource.motion.move",
            {"pan_deg": 60.0, "tilt_deg": 110.0, "duration_ms": 200},
        )

        self.assertGreater(self.driver_calls["move"], 0)
        self.assertEqual(env.get("status"), "accepted")
        self.assertEqual(env.get("request_id"), "req-chain-accepted")

        result = env.get("result")
        self.assertIsInstance(result, dict, "an accepted motion must carry a result payload")
        self.assertTrue(result.get("accepted"))
        self.assertIs(result.get("state_confirmed"), False,
                      "an ACK is acceptance, never physical confirmation (CR-002 / interfaces.md 6.1)")
        self.assertNotIn("executed", result)

        raw = json.dumps(env)
        self.assertNotIn("succeeded", raw)
        self.assertNotIn("completed", raw)

    # =====================================================================
    # Requirement 4 (explicit): the mode really is the broken default
    # =====================================================================
    def test_chain_runs_at_default_non_strict_initialisation(self) -> None:
        """Req 4: the whole suite must run at `strict_mode == false` -- the exact
        configuration in which the pre-926c82a firmware swallowed driver errors.
        This asserts the mode before *and* after a fault to catch any accidental
        mode switch inside the chain."""
        self.assertFalse(self.lib.desk_hardware_is_strict_mode())
        self._register_driver(DESK_HW_ERR_STALL, DESK_HW_OK)

        self._roundtrip(
            "req-chain-mode-check", "resource.motion.move",
            {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 300},
        )
        self.assertFalse(
            self.lib.desk_hardware_is_strict_mode(),
            "the chain must not have flipped strict mode",
        )


if __name__ == "__main__":
    unittest.main()
