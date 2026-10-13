"""
Unit and contract tests for Desk Agent R1 Hardware HAL and Unified Resource Arbiter.
Compiles and loads the C shared library for desk_hardware and desk_arbiter,
verifying priority preemption, camera lease gimbal locking, audio exclusivity,
motion mechanical limits clamping, and expression/display management.
"""

from __future__ import annotations

import ctypes
import os
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
DESK_RES_AUDIO_PLAYBACK = 0
DESK_RES_AUDIO_RECORD = 1
DESK_RES_CAMERA_SENSOR = 2
DESK_RES_MOTION_GIMBAL = 3
DESK_RES_DISPLAY_UI = 4
DESK_RES_MAX = 5

# Status codes
DESK_ARBITER_OK = 0
DESK_ARBITER_ERR_BUSY = 1
DESK_ARBITER_ERR_INVALID = 2
DESK_ARBITER_ERR_TIMEOUT = 3


class TestDeskHardwareArbiter(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_hardware_test.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        # Compile desk_hardware + desk_arbiter into shared library
        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            "-o",
            cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile desk_hardware shared library: {res.stderr}")

        cls.lib = ctypes.CDLL(cls.so_path)

        # Function signatures
        cls.lib.desk_arbiter_init.restype = ctypes.c_int
        cls.lib.desk_arbiter_init.argtypes = []

        cls.lib.desk_arbiter_acquire.restype = ctypes.c_int
        cls.lib.desk_arbiter_acquire.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32]

        cls.lib.desk_arbiter_release.restype = None
        cls.lib.desk_arbiter_release.argtypes = [ctypes.c_int, ctypes.c_int]

        cls.lib.desk_arbiter_get_owner.restype = ctypes.c_int
        cls.lib.desk_arbiter_get_owner.argtypes = [ctypes.c_int]

        cls.lib.desk_arbiter_camera_acquire_lease.restype = ctypes.c_int
        cls.lib.desk_arbiter_camera_acquire_lease.argtypes = [ctypes.c_char_p, ctypes.c_uint32]

        cls.lib.desk_arbiter_camera_renew_lease.restype = ctypes.c_int
        cls.lib.desk_arbiter_camera_renew_lease.argtypes = [ctypes.c_char_p, ctypes.c_uint32]

        cls.lib.desk_arbiter_camera_release_lease.restype = None
        cls.lib.desk_arbiter_camera_release_lease.argtypes = [ctypes.c_char_p]

        cls.lib.desk_arbiter_camera_is_lease_active.restype = ctypes.c_bool
        cls.lib.desk_arbiter_camera_is_lease_active.argtypes = []

        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_init.argtypes = []

        cls.lib.desk_hardware_motion_move_to.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_move_to.argtypes = [ctypes.c_float, ctypes.c_float, ctypes.c_uint32]

        cls.lib.desk_hardware_motion_get_position.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_get_position.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]

        cls.lib.desk_hardware_motion_stop.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_stop.argtypes = [ctypes.c_bool]

        cls.lib.desk_hardware_display_set_expression.restype = ctypes.c_int
        cls.lib.desk_hardware_display_set_expression.argtypes = [ctypes.c_char_p, ctypes.c_char_p]

        cls.lib.desk_hardware_display_get_expression.restype = ctypes.c_int
        cls.lib.desk_hardware_display_get_expression.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_hardware_init()

    def test_arbiter_priority_preemption(self) -> None:
        """Verifies higher priority owner preempts lower priority, and lower cannot preempt higher."""
        # 1. SDK_REMOTE (priority 40) acquires audio playback
        rc = self.lib.desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE, 0)
        self.assertEqual(rc, DESK_ARBITER_OK)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_SDK_REMOTE)

        # 2. TCLAW_VOICE (priority 80) preempts SDK_REMOTE
        rc2 = self.lib.desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 0)
        self.assertEqual(rc2, DESK_ARBITER_OK)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_TCLAW_VOICE)

        # 3. SDK_REMOTE cannot preempt TCLAW_VOICE
        rc3 = self.lib.desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE, 0)
        self.assertEqual(rc3, DESK_ARBITER_ERR_BUSY)

        # 4. SYSTEM_ALERT (priority 100) preempts TCLAW_VOICE
        rc4 = self.lib.desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SYSTEM_ALERT, 0)
        self.assertEqual(rc4, DESK_ARBITER_OK)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_SYSTEM_ALERT)

        # Release
        self.lib.desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SYSTEM_ALERT)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_NONE)

    def test_camera_lease_locks_tclaw_motion_tracking(self) -> None:
        """Verifies active camera viewing lease locks out automatic TClaw gimbal tracking."""
        # Acquire camera lease
        rc = self.lib.desk_arbiter_camera_acquire_lease(b"client-viewer-1", 5000)
        self.assertEqual(rc, DESK_ARBITER_OK)
        self.assertTrue(self.lib.desk_arbiter_camera_is_lease_active())

        # TClaw voice dialogue attempts auto-tracking gimbal move -> must be locked (ERR_BUSY)
        rc_tclaw = self.lib.desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE, 0)
        self.assertEqual(rc_tclaw, DESK_ARBITER_ERR_BUSY)

        # Release camera lease
        self.lib.desk_arbiter_camera_release_lease(b"client-viewer-1")
        self.assertFalse(self.lib.desk_arbiter_camera_is_lease_active())

        # Now TClaw can acquire motion gimbal
        rc_tclaw_after = self.lib.desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE, 0)
        self.assertEqual(rc_tclaw_after, DESK_ARBITER_OK)
        self.lib.desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE)

    def test_motion_hardware_clamping(self) -> None:
        """Verifies mechanical angle limits (Pan: 0..180, Tilt: 100..140)."""
        # Test values outside limits
        self.lib.desk_hardware_motion_move_to(ctypes.c_float(220.0), ctypes.c_float(80.0), 300)
        pan = ctypes.c_float()
        tilt = ctypes.c_float()
        self.lib.desk_hardware_motion_get_position(ctypes.byref(pan), ctypes.byref(tilt))
        self.assertAlmostEqual(pan.value, 180.0, places=1)
        self.assertAlmostEqual(tilt.value, 100.0, places=1)

        # Test valid values within limits
        self.lib.desk_hardware_motion_move_to(ctypes.c_float(45.0), ctypes.c_float(125.0), 300)
        self.lib.desk_hardware_motion_get_position(ctypes.byref(pan), ctypes.byref(tilt))
        self.assertAlmostEqual(pan.value, 45.0, places=1)
        self.assertAlmostEqual(tilt.value, 125.0, places=1)

    def test_display_expression_and_status(self) -> None:
        """Verifies expression preset and status text storage and retrieval."""
        rc = self.lib.desk_hardware_display_set_expression(b"thinking", b"Processing request...")
        self.assertEqual(rc, 0)

        preset_buf = ctypes.create_string_buffer(32)
        status_buf = ctypes.create_string_buffer(64)
        self.lib.desk_hardware_display_get_expression(preset_buf, 32, status_buf, 64)
        self.assertEqual(preset_buf.value.decode("utf-8"), "thinking")
        self.assertEqual(status_buf.value.decode("utf-8"), "Processing request...")
