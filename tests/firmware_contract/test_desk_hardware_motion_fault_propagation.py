"""
Level 2 (静态检查 / 模拟 PoC) contract tests for Desk Agent R1 gimbal driver
fault propagation.

Scope
-----
These are **direct function tests** (AGENTS.md §4, evidence Level 2). They drive
the *real compiled C library* (``desk_hardware.c`` + ``desk_arbiter.c``) through
ctypes and inject a board driver table whose ``motion_move`` / ``motion_stop``
primitives return a chosen error code. They prove the data/state-machine
contract of the hardware layer only -- they do **not** prove that any physical
gimbal moved, nor that the STM32 coprocessor accepted a frame.

Binding requirement
-------------------
``docs/implementation/interfaces.md`` §6.1 (裁决 R1):
    设备侧 sys.ack 仅表示"已受理"，绝不表示"物理已达成"
Therefore a driver that returns non-``DESK_HW_OK`` must surface as an error from
``desk_hardware_motion_move_to()`` / ``desk_hardware_motion_stop()``, and must
never be reported as success -- **regardless of** ``s_strict_hardware_mode``.

Covered regression requirements
-------------------------------
1. non-strict move driver failure propagates
2. non-strict stop driver failure propagates
3. strict mode: same correct propagation for both primitives
4. the requested target is not adopted as the confirmed position on failure
5. the gimbal resource is re-acquirable after a failed move (no leaked lock)
6. the success path is unchanged (returns DESK_HW_OK and updates position)
plus two boundary guards:
7. the documented strict-mode UART pre-check is preserved
8. a failed move does not release a lock this call never acquired
"""

from __future__ import annotations

import ctypes
import math
import os
from pathlib import Path
import subprocess
import tempfile
import unittest

# Owners matching desk_arbiter.h
DESK_OWNER_NONE = 0
DESK_OWNER_IDLE = 1
DESK_OWNER_TCLAW_VOICE = 2
DESK_OWNER_SDK_REMOTE = 3
DESK_OWNER_LAN_CAMERA = 4
DESK_OWNER_SYSTEM_ALERT = 5

# Resources matching desk_arbiter.h
DESK_RES_MOTION_GIMBAL = 3

# Arbiter status codes
DESK_ARBITER_OK = 0
DESK_ARBITER_ERR_BUSY = 1

# Hardware error codes matching desk_hardware.h
DESK_HW_OK = 0
DESK_HW_ERR_INVALID_ARG = -1
DESK_HW_ERR_UNAVAILABLE = -2
DESK_HW_ERR_TIMEOUT = -3
DESK_HW_ERR_IO = -4
DESK_HW_ERR_BUSY = -5
DESK_HW_ERR_STALL = -6

# Board driver primitives (desk_board_driver_ops_t)
DRIVER_MOTION_MOVE_CB = ctypes.CFUNCTYPE(
    ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_uint32
)
DRIVER_MOTION_STOP_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_bool)
MCU_TX_CB = ctypes.CFUNCTYPE(
    None, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_void_p
)


class DeskBoardDriverOps(ctypes.Structure):
    """ctypes mirror of ``desk_board_driver_ops_t`` (field order is load-bearing)."""

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


# Module-level keep-alive: the C library stores raw pointers to the driver table
# and to the callback trampolines, so they must outlive the test that made them.
_ALIVE: list = []


class TestMotionDriverFaultPropagation(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_hardware_fault_test.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wshadow",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            "-o",
            cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to compile desk_hardware shared library: {res.stderr}"
            )

        cls.lib = ctypes.CDLL(cls.so_path)

        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_init.argtypes = []

        cls.lib.desk_hardware_set_strict_mode.restype = None
        cls.lib.desk_hardware_set_strict_mode.argtypes = [ctypes.c_bool]

        cls.lib.desk_hardware_is_strict_mode.restype = ctypes.c_bool
        cls.lib.desk_hardware_is_strict_mode.argtypes = []

        cls.lib.desk_hardware_motion_move_to.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_move_to.argtypes = [
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_uint32,
        ]

        cls.lib.desk_hardware_motion_stop.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_stop.argtypes = [ctypes.c_bool]

        cls.lib.desk_hardware_motion_get_position.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_get_position.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]

        cls.lib.desk_hardware_get_last_mcu_frame.restype = ctypes.c_int
        cls.lib.desk_hardware_get_last_mcu_frame.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        cls.lib.desk_hardware_get_pan.restype = ctypes.c_float
        cls.lib.desk_hardware_get_pan.argtypes = []

        cls.lib.desk_hardware_get_tilt.restype = ctypes.c_float
        cls.lib.desk_hardware_get_tilt.argtypes = []

        cls.lib.desk_hardware_register_driver_ops.restype = ctypes.c_int
        cls.lib.desk_hardware_register_driver_ops.argtypes = [
            ctypes.POINTER(DeskBoardDriverOps)
        ]

        cls.lib.desk_hardware_register_mcu_tx_cb.restype = ctypes.c_int
        cls.lib.desk_hardware_register_mcu_tx_cb.argtypes = [MCU_TX_CB, ctypes.c_void_p]

        cls.lib.desk_arbiter_acquire.restype = ctypes.c_int
        cls.lib.desk_arbiter_acquire.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint32,
        ]

        cls.lib.desk_arbiter_release.restype = None
        cls.lib.desk_arbiter_release.argtypes = [ctypes.c_int, ctypes.c_int]

        cls.lib.desk_arbiter_get_owner.restype = ctypes.c_int
        cls.lib.desk_arbiter_get_owner.argtypes = [ctypes.c_int]

        # Layout sanity check: every field is a pointer-sized member.
        assert ctypes.sizeof(DeskBoardDriverOps) == 9 * ctypes.sizeof(
            ctypes.c_void_p
        ), "DeskBoardDriverOps layout drifted from desk_board_driver_ops_t"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        # desk_hardware_init() re-installs the default driver table, clears the
        # arbiter and resets strict mode + reported position (90.0 / 120.0).
        self.lib.desk_hardware_init()
        self.lib.desk_hardware_set_strict_mode(False)

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _install_driver(
        self, move_rc: int = DESK_HW_OK, stop_rc: int = DESK_HW_OK
    ) -> dict:
        """Install a fault-injecting board driver table.

        Returns a mutable state dict so a test can flip the injected return code
        mid-test (fail -> succeed) without reinstalling the table. ``move_calls``
        / ``stop_calls`` let a test prove the driver primitive was actually
        reached (and not short-circuited before the call).
        """
        state = {
            "move_rc": move_rc,
            "stop_rc": stop_rc,
            "move_calls": 0,
            "stop_calls": 0,
        }

        @DRIVER_MOTION_MOVE_CB
        def on_move(pan, tilt, duration):
            state["move_calls"] += 1
            return state["move_rc"]

        @DRIVER_MOTION_STOP_CB
        def on_stop(emergency):
            state["stop_calls"] += 1
            return state["stop_rc"]

        ops = DeskBoardDriverOps()
        ops.driver_name = b"fault_injection_test_bsp"
        ops.motion_move = on_move
        ops.motion_stop = on_stop

        _ALIVE.extend([on_move, on_stop, ops])
        self.assertEqual(
            self.lib.desk_hardware_register_driver_ops(ctypes.byref(ops)), 0
        )
        return state

    def _use_real_mcu_transport(self) -> None:
        """Register a non-default MCU TX callback.

        The strict-mode pre-check in ``desk_hardware_motion_move_to()`` returns
        ``DESK_HW_ERR_UNAVAILABLE`` when there is no UART fd *and* the MCU TX
        callback is still the built-in default. A real board always has a
        transport, so registering one lets strict-mode tests reach the driver
        call instead of short-circuiting before it.
        """

        @MCU_TX_CB
        def on_mcu_tx(frame, length, user_data):
            return None

        _ALIVE.append(on_mcu_tx)
        self.assertEqual(
            self.lib.desk_hardware_register_mcu_tx_cb(on_mcu_tx, None), 0
        )

    def _get_position(self):
        pan = ctypes.c_float()
        tilt = ctypes.c_float()
        rc = self.lib.desk_hardware_motion_get_position(
            ctypes.byref(pan), ctypes.byref(tilt)
        )
        self.assertEqual(rc, 0)
        return pan.value, tilt.value

    def _owner(self) -> int:
        return self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL)

    # ------------------------------------------------------------------
    # 1. non-strict move driver failure propagates
    # ------------------------------------------------------------------
    def test_01_non_strict_move_driver_failure_propagates(self) -> None:
        """Req 1 (Level 2, direct function test): with the default init and
        strict=false, a failing ``motion_move`` driver makes
        ``desk_hardware_motion_move_to`` return the driver's error, never 0."""
        self.assertFalse(self.lib.desk_hardware_is_strict_mode())
        state = self._install_driver()

        for injected in (
            DESK_HW_ERR_IO,
            DESK_HW_ERR_STALL,
            DESK_HW_ERR_BUSY,
            DESK_HW_ERR_TIMEOUT,
            DESK_HW_ERR_INVALID_ARG,
        ):
            with self.subTest(injected_rc=injected):
                state["move_rc"] = injected
                rc = self.lib.desk_hardware_motion_move_to(90.0, 120.0, 300)
                self.assertNotEqual(
                    rc, DESK_HW_OK, "a driver failure must never be reported as success"
                )
                self.assertEqual(rc, injected)

        self.assertEqual(state["move_calls"], 5, "the driver must be reached each call")

    # ------------------------------------------------------------------
    # 2. non-strict stop driver failure propagates
    # ------------------------------------------------------------------
    def test_02_non_strict_stop_driver_failure_propagates(self) -> None:
        """Req 2 (Level 2, direct function test): with the default init and
        strict=false, a failing ``motion_stop`` driver makes
        ``desk_hardware_motion_stop`` return the driver's error, never
        ``DESK_HW_OK``."""
        self.assertFalse(self.lib.desk_hardware_is_strict_mode())
        state = self._install_driver()

        for injected in (DESK_HW_ERR_IO, DESK_HW_ERR_STALL, DESK_HW_ERR_TIMEOUT):
            with self.subTest(injected_rc=injected):
                state["stop_rc"] = injected
                rc = self.lib.desk_hardware_motion_stop(True)
                self.assertNotEqual(rc, DESK_HW_OK)
                self.assertEqual(rc, injected)

        self.assertEqual(state["stop_calls"], 3)

        # The stop path still releases both motion owners even on failure.
        self.assertEqual(self._owner(), DESK_OWNER_NONE)

    # ------------------------------------------------------------------
    # 3. strict mode: same correct propagation
    # ------------------------------------------------------------------
    def test_03_strict_mode_propagates_move_and_stop_failure(self) -> None:
        """Req 3 (Level 2, direct function test): the strict-mode flag must not
        gate error propagation -- strict=true yields the same driver error for
        both ``motion_move`` and ``motion_stop``."""
        self._use_real_mcu_transport()
        state = self._install_driver()
        self.lib.desk_hardware_set_strict_mode(True)
        self.assertTrue(self.lib.desk_hardware_is_strict_mode())

        state["move_rc"] = DESK_HW_ERR_STALL
        rc_move = self.lib.desk_hardware_motion_move_to(90.0, 120.0, 300)
        self.assertEqual(
            state["move_calls"], 1, "the UART pre-check must not short-circuit here"
        )
        self.assertNotEqual(rc_move, DESK_HW_OK)
        self.assertEqual(rc_move, DESK_HW_ERR_STALL)

        state["stop_rc"] = DESK_HW_ERR_IO
        rc_stop = self.lib.desk_hardware_motion_stop(False)
        self.assertEqual(state["stop_calls"], 1)
        self.assertNotEqual(rc_stop, DESK_HW_OK)
        self.assertEqual(rc_stop, DESK_HW_ERR_IO)

    # ------------------------------------------------------------------
    # 4. position not adopted on failure
    # ------------------------------------------------------------------
    def test_04_position_only_updated_on_driver_success(self) -> None:
        """Req 4 (Level 2, direct function test): a failed move must leave the
        last successfully-confirmed pose intact; a succeeding move updates it."""
        state = self._install_driver()

        # Confirm a pose with a succeeding driver.
        self.assertEqual(
            self.lib.desk_hardware_motion_move_to(60.0, 130.0, 200), DESK_HW_OK
        )
        pan, tilt = self._get_position()
        self.assertAlmostEqual(pan, 60.0, places=3)
        self.assertAlmostEqual(tilt, 130.0, places=3)

        # Failed move to a different target: the target must NOT be adopted.
        state["move_rc"] = DESK_HW_ERR_STALL
        self.assertEqual(
            self.lib.desk_hardware_motion_move_to(150.0, 110.0, 200),
            DESK_HW_ERR_STALL,
        )
        pan, tilt = self._get_position()
        self.assertAlmostEqual(pan, 60.0, places=3, msg="failed move must not move pan")
        self.assertAlmostEqual(
            tilt, 130.0, places=3, msg="failed move must not move tilt"
        )
        self.assertNotAlmostEqual(pan, 150.0, places=3)
        self.assertNotAlmostEqual(tilt, 110.0, places=3)

        # The direct getters read the same confirmed state.
        self.assertAlmostEqual(self.lib.desk_hardware_get_pan(), 60.0, places=3)
        self.assertAlmostEqual(self.lib.desk_hardware_get_tilt(), 130.0, places=3)

        # A subsequent successful move does update the confirmed pose.
        state["move_rc"] = DESK_HW_OK
        self.assertEqual(
            self.lib.desk_hardware_motion_move_to(45.0, 125.0, 200), DESK_HW_OK
        )
        pan, tilt = self._get_position()
        self.assertAlmostEqual(pan, 45.0, places=3)
        self.assertAlmostEqual(tilt, 125.0, places=3)
        self.assertAlmostEqual(self.lib.desk_hardware_get_pan(), 45.0, places=3)
        self.assertAlmostEqual(self.lib.desk_hardware_get_tilt(), 125.0, places=3)

    # ------------------------------------------------------------------
    # 5. resource re-acquirable after failure (资源可被后续请求重新获取)
    # ------------------------------------------------------------------
    def test_05_gimbal_resource_reacquirable_after_failed_move(self) -> None:
        """Req 5 (Level 2, direct function test): a failed move must not leak the
        gimbal lock it auto-acquired; a later request can still take it."""
        state = self._install_driver(move_rc=DESK_HW_ERR_IO)
        self.assertEqual(self._owner(), DESK_OWNER_NONE)

        self.assertEqual(
            self.lib.desk_hardware_motion_move_to(90.0, 120.0, 300), DESK_HW_ERR_IO
        )
        self.assertEqual(
            self._owner(),
            DESK_OWNER_NONE,
            "the failure path must release the auto-acquired gimbal lock",
        )

        # A direct low-priority acquire must succeed: nothing is left locked.
        # (DESK_OWNER_IDLE has priority 10, so it would be rejected as BUSY if
        # the SDK_REMOTE priority-40 lock had leaked.)
        self.assertEqual(
            self.lib.desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_IDLE, 0),
            DESK_ARBITER_OK,
        )
        self.assertEqual(self._owner(), DESK_OWNER_IDLE)
        self.lib.desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_IDLE)

        # And a subsequent successful move re-acquires and then releases again.
        state["move_rc"] = DESK_HW_OK
        self.assertEqual(
            self.lib.desk_hardware_motion_move_to(100.0, 115.0, 200), DESK_HW_OK
        )
        self.assertEqual(self._owner(), DESK_OWNER_NONE)

    # ------------------------------------------------------------------
    # 6. success path unchanged
    # ------------------------------------------------------------------
    def test_06_success_path_unchanged(self) -> None:
        """Req 6 (Level 2, direct function test): a succeeding driver still
        returns ``DESK_HW_OK``, is reached exactly once, and updates position."""
        state = self._install_driver()

        self.assertEqual(
            self.lib.desk_hardware_motion_move_to(75.0, 115.0, 250), DESK_HW_OK
        )
        self.assertEqual(state["move_calls"], 1)
        pan, tilt = self._get_position()
        self.assertAlmostEqual(pan, 75.0, places=3)
        self.assertAlmostEqual(tilt, 115.0, places=3)
        self.assertEqual(self._owner(), DESK_OWNER_NONE)

        self.assertEqual(self.lib.desk_hardware_motion_stop(False), DESK_HW_OK)
        self.assertEqual(state["stop_calls"], 1)

        # Mechanical clamping is unaffected by the fix.
        self.assertEqual(
            self.lib.desk_hardware_motion_move_to(220.0, 80.0, 250), DESK_HW_OK
        )
        pan, tilt = self._get_position()
        self.assertAlmostEqual(pan, 180.0, places=3)
        self.assertAlmostEqual(tilt, 100.0, places=3)

    def test_nonfinite_targets_are_rejected_before_arbiter_serialization_or_driver(self) -> None:
        """NaN and +/-Inf on either axis must stop at the public HAL boundary."""
        state = self._install_driver()
        tx_state = {"calls": 0}

        @MCU_TX_CB
        def on_mcu_tx(frame, length, user_data):
            tx_state["calls"] += 1

        _ALIVE.append(on_mcu_tx)
        self.assertEqual(
            self.lib.desk_hardware_register_mcu_tx_cb(on_mcu_tx, None), 0
        )

        nonfinite_values = (math.nan, math.inf, -math.inf)
        for value in nonfinite_values:
            with self.subTest(axis="pan", value=value):
                rc = self.lib.desk_hardware_motion_move_to(value, 120.0, 300)
                self.assertEqual(rc, DESK_HW_ERR_INVALID_ARG)
            with self.subTest(axis="tilt", value=value):
                rc = self.lib.desk_hardware_motion_move_to(90.0, value, 300)
                self.assertEqual(rc, DESK_HW_ERR_INVALID_ARG)

        self.assertEqual(self._owner(), DESK_OWNER_NONE)
        self.assertEqual(state["move_calls"], 0)
        self.assertEqual(tx_state["calls"], 0)
        frame = (ctypes.c_uint8 * 64)()
        frame_len = ctypes.c_size_t(999)
        self.assertEqual(
            self.lib.desk_hardware_get_last_mcu_frame(frame, len(frame), ctypes.byref(frame_len)),
            0,
        )
        self.assertEqual(frame_len.value, 0, "invalid input must be rejected before serialization")

    def test_nonfinite_rejection_precedes_clamp_arbiter_serialization_and_driver_in_source(self) -> None:
        """Keep the public entry guard ahead of all stateful motion operations."""
        source_path = Path(__file__).resolve().parents[2] / "firmware/components/desk_hardware/src/desk_hardware.c"
        source = source_path.read_text(encoding="utf-8")
        move_to = source.split("int desk_hardware_motion_move_to(float pan_deg, float tilt_deg, uint32_t duration_ms)", 1)[1]
        move_to = move_to.split("int desk_hardware_motion_stop(bool emergency)", 1)[0]
        reject_index = move_to.index("if (!isfinite(pan_deg) || !isfinite(tilt_deg))")
        clamp_index = move_to.index("/* Mechanical clamping */")
        arbiter_index = move_to.index("desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL)")
        serialize_index = move_to.index("desk_hardware_format_stm32_motion_packet(pan_deg, tilt_deg")
        driver_index = move_to.index("s_board_driver_ops->motion_move(pan_deg, tilt_deg, duration_ms)")

        self.assertLess(reject_index, clamp_index)
        self.assertLess(reject_index, arbiter_index)
        self.assertLess(reject_index, serialize_index)
        self.assertLess(reject_index, driver_index)

    # ------------------------------------------------------------------
    # 7. boundary guard: documented strict pre-check preserved
    # ------------------------------------------------------------------
    @unittest.skipIf(
        os.environ.get("DESK_UART_DEVICE"),
        "DESK_UART_DEVICE is set; the no-UART strict pre-check cannot trigger",
    )
    def test_07_strict_uart_precheck_boundary_preserved(self) -> None:
        """Boundary guard (Level 2, direct function test): the documented
        strict-mode UART pre-check is deliberately NOT part of this fix. With no
        UART fd and the default MCU TX callback, strict mode still returns
        ``DESK_HW_ERR_UNAVAILABLE``; non-strict still returns ``DESK_HW_OK``."""
        self.lib.desk_hardware_set_strict_mode(True)
        rc = self.lib.desk_hardware_motion_move_to(90.0, 120.0, 300)
        self.assertEqual(rc, DESK_HW_ERR_UNAVAILABLE)

        self.lib.desk_hardware_set_strict_mode(False)
        rc2 = self.lib.desk_hardware_motion_move_to(90.0, 120.0, 300)
        self.assertEqual(rc2, DESK_HW_OK)

    # ------------------------------------------------------------------
    # 8. boundary guard: no over-release of a foreign lock
    # ------------------------------------------------------------------
    def test_08_failure_does_not_release_foreign_owner_lock(self) -> None:
        """Boundary guard (Level 2, direct function test): when the gimbal was
        already held by another owner, a failed move must not release a lock
        this call never acquired."""
        state = self._install_driver(move_rc=DESK_HW_ERR_IO)
        self.assertEqual(
            self.lib.desk_arbiter_acquire(
                DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE, 0
            ),
            DESK_ARBITER_OK,
        )
        self.assertEqual(self._owner(), DESK_OWNER_TCLAW_VOICE)

        self.assertEqual(
            self.lib.desk_hardware_motion_move_to(90.0, 120.0, 300), DESK_HW_ERR_IO
        )
        self.assertEqual(
            self._owner(),
            DESK_OWNER_TCLAW_VOICE,
            "a failed move must not release a lock owned by a different owner",
        )
        self.lib.desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE)
        self.assertEqual(self._owner(), DESK_OWNER_NONE)


if __name__ == "__main__":
    unittest.main()
