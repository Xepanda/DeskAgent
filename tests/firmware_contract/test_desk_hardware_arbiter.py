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

# Touch enums matching desk_hardware.h
DESK_TOUCH_TARGET_BACKTOUCH = 0
DESK_TOUCH_TARGET_SCREENTOUCH = 1
DESK_TOUCH_TARGET_ROLLER = 2

DESK_TOUCH_ACTION_SHORT_PRESS = 0
DESK_TOUCH_ACTION_LONG_PRESS = 1
DESK_TOUCH_ACTION_SCROLL_UP = 2
DESK_TOUCH_ACTION_SCROLL_DOWN = 3


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

        cls.lib.desk_hardware_get_last_mcu_frame.restype = ctypes.c_int
        cls.lib.desk_hardware_get_last_mcu_frame.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

        cls.lib.desk_hardware_audio_record_start.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_record_start.argtypes = [ctypes.c_uint32]

        cls.lib.desk_hardware_audio_record_stop.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_record_stop.argtypes = []

        cls.lib.desk_hardware_audio_record_inject.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_record_inject.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t]

        cls.lib.desk_hardware_audio_record_read.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_record_read.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)]

        AUDIO_PLAY_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.c_uint32, ctypes.c_void_p)
        cls.AUDIO_PLAY_CB = AUDIO_PLAY_CB
        cls.lib.desk_hardware_register_audio_play_cb.restype = ctypes.c_int
        cls.lib.desk_hardware_register_audio_play_cb.argtypes = [AUDIO_PLAY_CB, ctypes.c_void_p]

        cls.lib.desk_hardware_audio_play_pcm.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_play_pcm.argtypes = [ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.c_uint32]

        cls.lib.desk_hardware_audio_play_stop.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_play_stop.argtypes = []

        CAM_FRAME_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32, ctypes.c_void_p)
        cls.CAM_FRAME_CB = CAM_FRAME_CB
        cls.lib.desk_hardware_camera_stream_start.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_stream_start.argtypes = [ctypes.c_float, CAM_FRAME_CB, ctypes.c_void_p]

        cls.lib.desk_hardware_camera_stream_pump.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_stream_pump.argtypes = []

        cls.lib.desk_hardware_camera_stream_stop.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_stream_stop.argtypes = []

        cls.lib.desk_hardware_camera_inject_frame.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_inject_frame.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]

        cls.lib.desk_hardware_camera_capture_jpeg.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_capture_jpeg.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_uint32]

        TOUCH_CB = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_int, ctypes.c_void_p)
        cls.TOUCH_CB = TOUCH_CB
        cls.lib.desk_hardware_register_input_cb.restype = ctypes.c_int
        cls.lib.desk_hardware_register_input_cb.argtypes = [TOUCH_CB, ctypes.c_void_p]

        cls.lib.desk_hardware_inject_touch_event.restype = ctypes.c_int
        cls.lib.desk_hardware_inject_touch_event.argtypes = [ctypes.c_int, ctypes.c_int]

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

    def test_stm32_mcu_frame_protocol_servo_move_and_stop(self) -> None:
        """Verifies real STM32 MCU binary link packet generation (magic 0xA5 0x5A, CRC16, LE payload)."""
        # Execute motion move
        rc = self.lib.desk_hardware_motion_move_to(ctypes.c_float(120.0), ctypes.c_float(110.0), 450)
        self.assertEqual(rc, 0)

        buf = ctypes.create_string_buffer(64)
        out_len = ctypes.c_size_t()
        rc = self.lib.desk_hardware_get_last_mcu_frame(buf, 64, ctypes.byref(out_len))
        self.assertEqual(rc, 0)

        # 12 header + 9 payload + 2 CRC16 = 23 bytes
        self.assertEqual(out_len.value, 23)
        raw = bytes(buf.raw[:23])

        # Header check
        self.assertEqual(raw[0], 0xA5)
        self.assertEqual(raw[1], 0x5A)
        self.assertEqual(raw[2], 0x01) # proto_version
        self.assertEqual(raw[3], 0x02) # MCU_FRAME_CLASS_MOTION
        self.assertEqual(raw[4], 0x01) # MCU_MOTION_MSG_SERVO_MOVE
        self.assertEqual(raw[5], 0x01) # ACK_REQ

        # Payload check (9 bytes)
        payload = raw[12:21]
        self.assertEqual(payload[0], 0x03) # axis_mask
        import struct
        x_x10, y_x10, duration, profile, source = struct.unpack("<hhHBB", payload[1:])
        self.assertEqual(x_x10, 1200) # 120.0 deg * 10
        self.assertEqual(y_x10, 1100) # 110.0 deg * 10
        self.assertEqual(duration, 450)
        self.assertEqual(source, 1)

        # Stop command check
        self.lib.desk_hardware_motion_stop(False)
        rc = self.lib.desk_hardware_get_last_mcu_frame(buf, 64, ctypes.byref(out_len))
        self.assertEqual(rc, 0)
        self.assertEqual(out_len.value, 16) # 12 header + 2 payload + 2 CRC = 16
        raw_stop = bytes(buf.raw[:16])
        self.assertEqual(raw_stop[4], 0x02) # MCU_MOTION_MSG_SERVO_STOP
        self.assertEqual(raw_stop[12], 0x01) # all_pending = 1

    def test_audio_record_ringbuffer_and_playback_callback(self) -> None:
        """Verifies PCM ring buffer injection, continuous acoustic reading, and playback callback."""
        # 1. Start audio recording
        rc = self.lib.desk_hardware_audio_record_start(16000)
        self.assertEqual(rc, 0)

        # 2. Inject 4 distinct PCM samples
        test_samples = (ctypes.c_int16 * 4)(100, -200, 300, -400)
        rc = self.lib.desk_hardware_audio_record_inject(test_samples, 4)
        self.assertEqual(rc, 0)

        # 3. Read back 4 samples
        read_buf = (ctypes.c_int16 * 4)()
        read_count = ctypes.c_size_t()
        rc = self.lib.desk_hardware_audio_record_read(read_buf, 4, ctypes.byref(read_count))
        self.assertEqual(rc, 0)
        self.assertEqual(read_count.value, 4)
        self.assertEqual(list(read_buf), [100, -200, 300, -400])

        # 4. Read additional samples when buffer empty -> must return 0 read without fake 440Hz wave synthesis
        empty_buf = (ctypes.c_int16 * 8)()
        self.lib.desk_hardware_audio_record_read(empty_buf, 8, ctypes.byref(read_count))
        self.assertEqual(read_count.value, 0, "Empty mic buffer must return 0 read samples without synthetic wave")

        # Inject real stream samples and verify reading them
        real_samples = (ctypes.c_int16 * 8)(10, 20, 30, 40, 50, 60, 70, 80)
        self.lib.desk_hardware_audio_record_inject(real_samples, 8)
        self.lib.desk_hardware_audio_record_read(empty_buf, 8, ctypes.byref(read_count))
        self.assertEqual(read_count.value, 8)
        self.assertEqual(list(empty_buf), [10, 20, 30, 40, 50, 60, 70, 80])

        self.lib.desk_hardware_audio_record_stop()

        # 5. Playback callback test
        played_samples = []
        def on_play(pcm_ptr, samples, rate, user_data):
            for i in range(samples):
                played_samples.append(pcm_ptr[i])

        cb_holder = self.AUDIO_PLAY_CB(on_play)
        self.lib.desk_hardware_register_audio_play_cb(cb_holder, None)

        play_data = (ctypes.c_int16 * 3)(500, 1000, 1500)
        rc = self.lib.desk_hardware_audio_play_pcm(play_data, 3, 16000)
        self.assertEqual(rc, 0)
        self.assertEqual(played_samples, [500, 1000, 1500])
        self.lib.desk_hardware_audio_play_stop()

    def test_camera_frame_injection_and_streaming(self) -> None:
        """Verifies camera frame injection, stream dispatch callback, and JPEG capture."""
        stream_frames = []
        def on_frame(jpeg_ptr, jpeg_len, ts_ms, user_data):
            stream_frames.append((bytes(jpeg_ptr[:jpeg_len]), ts_ms))

        cb_holder = self.CAM_FRAME_CB(on_frame)
        self.lib.desk_hardware_camera_stream_start(ctypes.c_float(15.0), cb_holder, None)

        # Inject custom JPEG frame
        fake_jpeg = b"\xFF\xD8\xFF\xE0TEST_JPEG_DATA\xFF\xD9"
        rc = self.lib.desk_hardware_camera_inject_frame(fake_jpeg, len(fake_jpeg), 5000)
        self.assertEqual(rc, 0)

        # Stream callback was called on injection
        self.assertEqual(len(stream_frames), 1)
        self.assertEqual(stream_frames[0][0], fake_jpeg)
        self.assertEqual(stream_frames[0][1], 5000)

        # Stream pump advances timestamp and dispatches
        rc = self.lib.desk_hardware_camera_stream_pump()
        self.assertEqual(rc, 0)
        self.assertEqual(len(stream_frames), 2)
        self.assertGreater(stream_frames[1][1], 5000)

        # Capture JPEG returns latest frame
        cap_buf = ctypes.create_string_buffer(128)
        cap_len = ctypes.c_size_t()
        rc = self.lib.desk_hardware_camera_capture_jpeg(cap_buf, 128, ctypes.byref(cap_len), 100)
        self.assertEqual(rc, 0)
        self.assertEqual(bytes(cap_buf.raw[:cap_len.value]), fake_jpeg)

        self.lib.desk_hardware_camera_stream_stop()

    def test_touch_event_subscription_and_injection(self) -> None:
        """Verifies touch inputs callback registration and dispatch."""
        received_events = []
        def on_touch(target, action, user_data):
            received_events.append((target, action))

        cb_holder = self.TOUCH_CB(on_touch)
        self.lib.desk_hardware_register_input_cb(cb_holder, None)

        self.lib.desk_hardware_inject_touch_event(DESK_TOUCH_TARGET_BACKTOUCH, DESK_TOUCH_ACTION_SHORT_PRESS)
        self.lib.desk_hardware_inject_touch_event(DESK_TOUCH_TARGET_ROLLER, DESK_TOUCH_ACTION_SCROLL_UP)

        self.assertEqual(len(received_events), 2)
        self.assertEqual(received_events[0], (DESK_TOUCH_TARGET_BACKTOUCH, DESK_TOUCH_ACTION_SHORT_PRESS))
        self.assertEqual(received_events[1], (DESK_TOUCH_TARGET_ROLLER, DESK_TOUCH_ACTION_SCROLL_UP))
