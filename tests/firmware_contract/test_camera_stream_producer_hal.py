from __future__ import annotations

import ctypes
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest


INIT_CB = ctypes.CFUNCTYPE(ctypes.c_int)
DEINIT_CB = ctypes.CFUNCTYPE(ctypes.c_int)
CAPTURE_CB = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_uint8),
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
    ctypes.c_uint32,
)
FRAME_CB = ctypes.CFUNCTYPE(
    None, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_uint32, ctypes.c_void_p
)
START_GATE_CB = ctypes.CFUNCTYPE(None)


class BoardDriverOps(ctypes.Structure):
    _fields_ = [
        ("driver_name", ctypes.c_char_p),
        ("init", INIT_CB),
        ("audio_record_read", ctypes.c_void_p),
        ("audio_play_pcm", ctypes.c_void_p),
        ("motion_move", ctypes.c_void_p),
        ("motion_stop", ctypes.c_void_p),
        ("camera_capture", CAPTURE_CB),
        ("display_expression", ctypes.c_void_p),
        ("deinit", DEINIT_CB),
    ]


class CameraStreamProducerHalTests(unittest.TestCase):
    _drivers = []
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_camera_test.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
        command = [
            "gcc", "-shared", "-fPIC", "-O2", "-Wall", "-DDESK_HARDWARE_TEST",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            "-o", cls.so_path,
        ]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.lib = ctypes.CDLL(cls.so_path)
        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_register_driver_ops.argtypes = [ctypes.POINTER(BoardDriverOps)]
        cls.lib.desk_hardware_register_driver_ops.restype = ctypes.c_int
        cls.lib.desk_arbiter_camera_acquire_lease.argtypes = [ctypes.c_char_p, ctypes.c_uint32]
        cls.lib.desk_arbiter_camera_acquire_lease.restype = ctypes.c_int
        cls.lib.desk_arbiter_camera_release_lease.argtypes = [ctypes.c_char_p]
        cls.lib.desk_arbiter_camera_lease_matches.argtypes = [ctypes.c_char_p]
        cls.lib.desk_arbiter_camera_lease_matches.restype = ctypes.c_bool
        cls.lib.desk_hardware_camera_stream_start.argtypes = [ctypes.c_char_p, ctypes.c_float, FRAME_CB, ctypes.c_void_p]
        cls.lib.desk_hardware_camera_stream_start.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_stream_pump.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_stream_is_ready.restype = ctypes.c_bool
        cls.lib.desk_hardware_camera_stream_is_available.restype = ctypes.c_bool
        cls.lib.desk_hardware_camera_stream_stop.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_inject_frame.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]
        cls.lib.desk_hardware_camera_inject_frame.restype = ctypes.c_int
        cls.lib.desk_hardware_test_set_camera_start_gate.argtypes = [START_GATE_CB]
        cls.lib.desk_hardware_test_set_camera_start_gate.restype = None

    def setUp(self) -> None:
        self.lib.desk_hardware_init()
        self.lib.desk_arbiter_camera_release_lease(b"viewer-a")
        self.lib.desk_arbiter_camera_release_lease(b"viewer-b")

    def tearDown(self) -> None:
        self.lib.desk_hardware_test_set_camera_start_gate(START_GATE_CB())

    def _install_capture_driver(self, capture):
        init = INIT_CB(lambda: 0)
        deinit = DEINIT_CB(lambda: 0)
        capture_cb = CAPTURE_CB(capture)
        self.driver = BoardDriverOps(b"native-camera-test", init, None, None, None, None, capture_cb, None, deinit)
        self._drivers.append(self.driver)
        self.assertEqual(self.lib.desk_hardware_register_driver_ops(ctypes.byref(self.driver)), 0)
        return capture_cb

    def test_native_mock_and_injected_frame_never_advertise_device_capability(self) -> None:
        self.assertFalse(self.lib.desk_hardware_camera_stream_is_available())
        self._install_capture_driver(lambda *args: -2)
        self.assertFalse(self.lib.desk_hardware_camera_stream_is_available())
        fixture = b"\xff\xd8native-only-fixture\xff\xd9"
        self.assertEqual(self.lib.desk_hardware_camera_inject_frame(fixture, len(fixture), 1234), 0)
        self.assertFalse(self.lib.desk_hardware_camera_stream_is_available())

    def test_esp_capability_probe_requires_real_watcher_source_and_buffer_capacity(self) -> None:
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
        source = Path(repo_root, "firmware/components/desk_hardware/src/desk_hardware.c").read_text(
            encoding="utf-8"
        )
        probe = source.split("bool desk_hardware_camera_stream_is_available(void)", 1)[1].split(
            "int desk_hardware_camera_stream_start(", 1
        )[0]
        self.assertIn("defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)", probe)
        self.assertIn("s_board_driver_ops != watcher_ops", probe)
        self.assertIn("watcher_ops->camera_capture != desk_watcher_camera_capture", probe)
        self.assertIn("!desk_watcher_camera_is_ready()", probe)
        self.assertIn("heap_caps_get_largest_free_block(MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)", probe)
        self.assertIn("return false;", probe)

    def test_lease_match_requires_active_unexpired_exact_owner(self) -> None:
        self.assertEqual(self.lib.desk_arbiter_camera_acquire_lease(b"viewer-a", 1000), 0)
        self.assertTrue(self.lib.desk_arbiter_camera_lease_matches(b"viewer-a"))
        self.assertFalse(self.lib.desk_arbiter_camera_lease_matches(b"viewer"))
        self.assertFalse(self.lib.desk_arbiter_camera_lease_matches(b"viewer-b"))
        self.lib.desk_arbiter_camera_release_lease(b"viewer-a")
        self.assertFalse(self.lib.desk_arbiter_camera_lease_matches(b"viewer-a"))

        self.assertEqual(self.lib.desk_arbiter_camera_acquire_lease(b"viewer-b", 1), 0)
        time.sleep(0.01)
        self.assertFalse(self.lib.desk_arbiter_camera_lease_matches(b"viewer-b"))

    def test_stream_uses_only_fresh_board_capture_and_readiness_needs_valid_jpeg(self) -> None:
        captures = []
        callbacks = []

        def capture(out_buf, max_len, out_len, timeout_ms):
            frame = (b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
                     b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00" +
                     bytes([len(captures)]) + b"\xff\xd9")
            captures.append(frame)
            ctypes.memmove(out_buf, frame, len(frame))
            out_len[0] = len(frame)
            return 0

        self._install_capture_driver(capture)
        self.assertEqual(self.lib.desk_arbiter_camera_acquire_lease(b"viewer-a", 5000), 0)

        def on_frame(buf, length, timestamp_ms, user_data):
            callbacks.append((bytes(buf[:length]), timestamp_ms))

        cb = FRAME_CB(on_frame)
        self.assertEqual(self.lib.desk_hardware_camera_stream_start(b"viewer-b", 15.0, cb, None), -5)
        self.assertEqual(self.lib.desk_hardware_camera_stream_start(b"viewer-a", 15.0, cb, None), 0)
        self.assertFalse(self.lib.desk_hardware_camera_stream_is_ready())
        self.assertEqual(self.lib.desk_hardware_camera_stream_pump(), 0)
        self.assertTrue(self.lib.desk_hardware_camera_stream_is_ready())
        self.assertEqual(self.lib.desk_hardware_camera_stream_pump(), 0)
        self.assertEqual(captures, [item[0] for item in callbacks])
        self.assertNotEqual(callbacks[0][0], callbacks[1][0])
        self.assertGreater(callbacks[1][1], callbacks[0][1])

        self.assertEqual(self.lib.desk_hardware_camera_stream_stop(), 0)
        self.assertFalse(self.lib.desk_hardware_camera_stream_is_ready())
        self.assertTrue(self.lib.desk_arbiter_camera_lease_matches(b"viewer-a"))
        self.assertNotEqual(self.lib.desk_hardware_camera_stream_pump(), 0)
        self.assertEqual(len(callbacks), 2)
        self.lib.desk_arbiter_camera_release_lease(b"viewer-a")

    def test_lease_reassignment_during_capture_suppresses_old_owner_callback(self) -> None:
        callbacks = []
        reassigned = []

        def capture(out_buf, max_len, out_len, timeout_ms):
            frame = (b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
                     b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00\xff\xd9")
            ctypes.memmove(out_buf, frame, len(frame))
            out_len[0] = len(frame)
            self.lib.desk_arbiter_camera_release_lease(b"viewer-a")
            reassigned.append(self.lib.desk_arbiter_camera_acquire_lease(b"viewer-b", 5000))
            return 0

        self._install_capture_driver(capture)
        self.assertEqual(self.lib.desk_arbiter_camera_acquire_lease(b"viewer-a", 5000), 0)
        cb = FRAME_CB(lambda *args: callbacks.append(args))
        self.assertEqual(self.lib.desk_hardware_camera_stream_start(b"viewer-a", 15.0, cb, None), 0)
        self.assertEqual(self.lib.desk_hardware_camera_stream_pump(), -5)
        self.assertEqual(reassigned, [0])
        self.assertFalse(self.lib.desk_hardware_camera_stream_is_ready())
        self.assertEqual(callbacks, [])
        self.lib.desk_hardware_camera_stream_stop()
        self.lib.desk_arbiter_camera_release_lease(b"viewer-b")

    def test_invalid_capture_never_sets_ready_or_calls_callback(self) -> None:
        callbacks = []

        def capture(out_buf, max_len, out_len, timeout_ms):
            frame = b"not-a-jpeg"
            ctypes.memmove(out_buf, frame, len(frame))
            out_len[0] = len(frame)
            return 0

        self._install_capture_driver(capture)
        self.assertEqual(self.lib.desk_arbiter_camera_acquire_lease(b"viewer-a", 5000), 0)
        cb = FRAME_CB(lambda *args: callbacks.append(args))
        self.assertEqual(self.lib.desk_hardware_camera_stream_start(b"viewer-a", 15.0, cb, None), 0)
        self.assertNotEqual(self.lib.desk_hardware_camera_stream_pump(), 0)
        self.assertFalse(self.lib.desk_hardware_camera_stream_is_ready())
        self.assertEqual(callbacks, [])
        self.lib.desk_hardware_camera_stream_stop()

    def test_stop_waits_for_active_callback_and_prevents_later_callback(self) -> None:
        entered = threading.Event()
        release = threading.Event()
        stopped = threading.Event()

        def capture(out_buf, max_len, out_len, timeout_ms):
            frame = (b"\xff\xd8\xff\xc0\x00\x0b\x08\x00\x01\x00\x01\x01\x01\x11\x00"
                     b"\xff\xda\x00\x08\x01\x01\x00\x00\x3f\x00\x00\xff\xd9")
            ctypes.memmove(out_buf, frame, len(frame))
            out_len[0] = len(frame)
            return 0

        self._install_capture_driver(capture)
        self.assertEqual(self.lib.desk_arbiter_camera_acquire_lease(b"viewer-a", 5000), 0)

        def on_frame(*args):
            entered.set()
            release.wait(2)

        cb = FRAME_CB(on_frame)
        self.assertEqual(self.lib.desk_hardware_camera_stream_start(b"viewer-a", 15.0, cb, None), 0)
        pump_thread = threading.Thread(target=self.lib.desk_hardware_camera_stream_pump)
        pump_thread.start()
        self.assertTrue(entered.wait(1))

        def stop_stream():
            self.lib.desk_hardware_camera_stream_stop()
            stopped.set()

        stop_thread = threading.Thread(target=stop_stream)
        stop_thread.start()
        second_stop_result = []
        second_stop_done = threading.Event()

        def stop_stream_again():
            second_stop_result.append(self.lib.desk_hardware_camera_stream_stop())
            second_stop_done.set()

        second_stop_thread = threading.Thread(target=stop_stream_again)
        second_stop_thread.start()
        time.sleep(0.03)
        self.assertFalse(stopped.is_set())
        self.assertFalse(second_stop_done.is_set())
        release.set()
        pump_thread.join(1)
        stop_thread.join(1)
        second_stop_thread.join(1)
        self.assertTrue(stopped.is_set())
        self.assertEqual(second_stop_result, [0])
        self.assertFalse(self.lib.desk_hardware_camera_stream_is_ready())
        self.assertNotEqual(self.lib.desk_hardware_camera_stream_pump(), 0)

    def test_stop_cancels_start_in_progress_and_waits_for_it(self) -> None:
        self._install_capture_driver(lambda *args: -2)
        self.assertEqual(self.lib.desk_arbiter_camera_acquire_lease(b"viewer-a", 5000), 0)
        start_entered = threading.Event()
        allow_start_to_continue = threading.Event()
        stop_done = threading.Event()
        start_result = []

        def start_gate():
            start_entered.set()
            allow_start_to_continue.wait(2)

        gate = START_GATE_CB(start_gate)
        self.lib.desk_hardware_test_set_camera_start_gate(gate)
        cb = FRAME_CB(lambda *args: None)
        start_thread = threading.Thread(
            target=lambda: start_result.append(
                self.lib.desk_hardware_camera_stream_start(b"viewer-a", 15.0, cb, None)
            )
        )
        start_thread.start()
        self.assertTrue(start_entered.wait(1))

        stop_result = []

        def stop_stream():
            stop_result.append(self.lib.desk_hardware_camera_stream_stop())
            stop_done.set()

        stop_thread = threading.Thread(target=stop_stream)
        stop_thread.start()
        time.sleep(0.03)
        self.assertFalse(stop_done.is_set())
        allow_start_to_continue.set()
        start_thread.join(1)
        stop_thread.join(1)
        self.lib.desk_hardware_test_set_camera_start_gate(START_GATE_CB())
        self.assertEqual(start_result, [-5])
        self.assertEqual(stop_result, [0])
        self.assertFalse(self.lib.desk_hardware_camera_stream_is_ready())
        self.assertEqual(self.lib.desk_hardware_camera_stream_pump(), -2)
        self.assertTrue(self.lib.desk_arbiter_camera_lease_matches(b"viewer-a"))
        self.lib.desk_arbiter_camera_release_lease(b"viewer-a")


if __name__ == "__main__":
    unittest.main()
