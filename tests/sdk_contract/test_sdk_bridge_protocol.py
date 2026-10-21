"""
Contract and protocol tests for Desk Agent R1 WatcheRobot SDK Bridge (C shared library).
Verifies handshake negotiation, argument parsing, resource arbitration enforcement,
WSPK binary framing, input events, and bidirectional tool dispatch.
"""

from __future__ import annotations

import contextlib
import ctypes
import json
import os
import subprocess
import sys
import tempfile
import unittest

# Mirror of DESK_SDK_MAX_FRAME_LEN in desk_sdk_bridge.h (interfaces.md 6.3.2).
DESK_SDK_MAX_FRAME_LEN = 8192

# A conforming session_token: exactly ^[0-9a-f]{64}$ (official wire contract).
VALID_SESSION_TOKEN = "0123456789abcdef" * 4

# Hardware error codes (desk_hardware.h)
DESK_HW_OK = 0
DESK_HW_ERR_STALL = -6

# Resource enum definitions matching desk_arbiter.h
DESK_OWNER_NONE = 0
DESK_OWNER_IDLE = 1
DESK_OWNER_TCLAW_VOICE = 2
DESK_OWNER_SDK_REMOTE = 3
DESK_OWNER_LAN_CAMERA = 4
DESK_OWNER_SYSTEM_ALERT = 5

DESK_RES_AUDIO_PLAYBACK = 0
DESK_RES_AUDIO_RECORD = 1
DESK_RES_CAMERA_SENSOR = 2
DESK_RES_MOTION_GIMBAL = 3
DESK_RES_DISPLAY_UI = 4

DESK_ARBITER_OK = 0
DESK_ARBITER_ERR_BUSY = 1
DESK_ARBITER_ERR_INVALID = 2

# Touch enums matching desk_hardware.h
DESK_TOUCH_TARGET_BACKTOUCH = 0
DESK_TOUCH_TARGET_SCREENTOUCH = 1
DESK_TOUCH_TARGET_ROLLER = 2

DESK_TOUCH_ACTION_SHORT_PRESS = 0
DESK_TOUCH_ACTION_LONG_PRESS = 1
DESK_TOUCH_ACTION_SCROLL_UP = 2
DESK_TOUCH_ACTION_SCROLL_DOWN = 3


class DeskWspkHeader(ctypes.Structure):
    _pack_ = 1
    _layout_ = "ms"
    _fields_ = [
        ("magic", ctypes.c_char * 4),
        ("frame_type", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
        ("stream_id", ctypes.c_uint16),
        ("sequence", ctypes.c_uint32),
        ("payload_size", ctypes.c_uint32),
    ]


# Callback signatures
OUTBOUND_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p)
TOOL_RESP_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)

# Board driver ops table (desk_hardware.h desk_board_driver_ops_t) for fault injection
DRIVER_MOTION_MOVE_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_uint32)
DRIVER_MOTION_STOP_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_bool)
MCU_TX_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_void_p)


class DeskBoardDriverOps(ctypes.Structure):
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


@contextlib.contextmanager
def capture_c_stdout():
    """Capture C-level stdout (printf) by redirecting file descriptor 1."""
    sys.stdout.flush()
    libc = ctypes.CDLL(None)
    libc.fflush(None)
    saved_fd = os.dup(1)
    tmp = tempfile.TemporaryFile()
    os.dup2(tmp.fileno(), 1)
    try:
        yield tmp
    finally:
        # C stdio buffers to a non-tty, so flush before restoring fd 1.
        libc.fflush(None)
        sys.stdout.flush()
        os.dup2(saved_fd, 1)
        os.close(saved_fd)


def read_captured(tmp) -> str:
    tmp.flush()
    tmp.seek(0)
    return tmp.read().decode("utf-8", errors="replace")


class TestSdkBridgeProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_sdk_bridge_test.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        # Compile C shared library with gcc
        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            f"-I{repo_root}/firmware/components/desk_sdk_bridge/include",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c",
            "-o",
            cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile desk_sdk_bridge shared library: {res.stderr}")

        cls.lib = ctypes.CDLL(cls.so_path)

        # Setup ctypes signatures
        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_init.argtypes = []

        cls.lib.desk_arbiter_acquire.restype = ctypes.c_int
        cls.lib.desk_arbiter_acquire.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32]

        cls.lib.desk_arbiter_release.restype = None
        cls.lib.desk_arbiter_release.argtypes = [ctypes.c_int, ctypes.c_int]

        cls.lib.desk_hardware_motion_get_position.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_get_position.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]

        cls.lib.desk_hardware_display_get_expression.restype = ctypes.c_int
        cls.lib.desk_hardware_display_get_expression.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        AUDIO_PLAY_CB = ctypes.CFUNCTYPE(None, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.c_uint32, ctypes.c_void_p)
        cls.AUDIO_PLAY_CB = AUDIO_PLAY_CB
        cls.lib.desk_hardware_register_audio_play_cb.restype = ctypes.c_int
        cls.lib.desk_hardware_register_audio_play_cb.argtypes = [AUDIO_PLAY_CB, ctypes.c_void_p]

        cls.lib.desk_hardware_set_strict_mode.restype = None
        cls.lib.desk_hardware_set_strict_mode.argtypes = [ctypes.c_bool]

        cls.lib.desk_hardware_is_strict_mode.restype = ctypes.c_bool
        cls.lib.desk_hardware_is_strict_mode.argtypes = []

        cls.lib.desk_arbiter_get_owner.restype = ctypes.c_int
        cls.lib.desk_arbiter_get_owner.argtypes = [ctypes.c_int]

        cls.lib.desk_hardware_set_display_initialized.restype = None
        cls.lib.desk_hardware_set_display_initialized.argtypes = [ctypes.c_bool]

        cls.lib.desk_sdk_bridge_init.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_init.argtypes = []

        cls.lib.desk_sdk_bridge_process_command.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_command.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        cls.lib.desk_sdk_bridge_send_touch_event.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_send_touch_event.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        cls.lib.desk_wspk_pack_header.restype = ctypes.c_int
        cls.lib.desk_wspk_pack_header.argtypes = [
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.c_uint16,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
        ]

        cls.lib.desk_wspk_unpack_header.restype = ctypes.c_int
        cls.lib.desk_wspk_unpack_header.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(DeskWspkHeader),
        ]

        cls.lib.desk_sdk_bridge_register_outbound.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_register_outbound.argtypes = [OUTBOUND_CB, ctypes.c_void_p]

        cls.lib.desk_sdk_bridge_register_tool_response_cb.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_register_tool_response_cb.argtypes = [TOOL_RESP_CB, ctypes.c_void_p]

        cls.lib.desk_sdk_bridge_dispatch_tool_request.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_dispatch_tool_request.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_handle_tool_response.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_handle_tool_response.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_get_pending_request_count.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_get_pending_request_count.argtypes = []

        cls.lib.desk_sdk_bridge_has_pending_request.restype = ctypes.c_bool
        cls.lib.desk_sdk_bridge_has_pending_request.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_get_request_status.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_get_request_status.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_sdk_bridge_clear_pending_requests.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_clear_pending_requests.argtypes = []

        cls.lib.desk_sdk_bridge_cleanup_expired_requests.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_cleanup_expired_requests.argtypes = [ctypes.c_uint32]

        cls.lib.desk_sdk_bridge_get_last_heartbeat_time.restype = ctypes.c_uint32
        cls.lib.desk_sdk_bridge_get_last_heartbeat_time.argtypes = []

        cls.lib.desk_sdk_bridge_is_heartbeat_alive.restype = ctypes.c_bool
        cls.lib.desk_sdk_bridge_is_heartbeat_alive.argtypes = [ctypes.c_uint32]

        cls.lib.desk_sdk_bridge_process_pairing_udp.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_pairing_udp.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        cls.lib.desk_sdk_bridge_set_pairing_code.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_set_pairing_code.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_is_pairing_code_configured.restype = ctypes.c_bool
        cls.lib.desk_sdk_bridge_is_pairing_code_configured.argtypes = []

        cls.lib.desk_hardware_register_driver_ops.restype = ctypes.c_int
        cls.lib.desk_hardware_register_driver_ops.argtypes = [ctypes.POINTER(DeskBoardDriverOps)]

        cls.lib.desk_hardware_camera_inject_frame.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_inject_frame.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_uint32,
        ]

        cls.lib.desk_hardware_register_mcu_tx_cb.restype = ctypes.c_int
        cls.lib.desk_hardware_register_mcu_tx_cb.argtypes = [MCU_TX_CB, ctypes.c_void_p]

        cls.lib.desk_sdk_bridge_reset.restype = None
        cls.lib.desk_sdk_bridge_reset.argtypes = []

        cls.lib.desk_sdk_bridge_set_auth_required.restype = None
        cls.lib.desk_sdk_bridge_set_auth_required.argtypes = [ctypes.c_bool]

        cls.lib.desk_sdk_bridge_set_pairing_token.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_set_pairing_token.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_pack_media_frame.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_pack_media_frame.argtypes = [
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.c_uint16,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        cls.lib.desk_sdk_socket_server_start.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_start.argtypes = [ctypes.c_uint16]

        cls.lib.desk_sdk_socket_server_get_port.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_get_port.argtypes = []

        cls.lib.desk_sdk_socket_server_start_ex.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_start_ex.argtypes = [ctypes.c_char_p, ctypes.c_uint16]

        cls.lib.desk_sdk_socket_server_poll.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_poll.argtypes = [ctypes.c_int]

        cls.lib.desk_sdk_socket_server_stop.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_stop.argtypes = []

        cls.lib.desk_sdk_pairing_server_start.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_start.argtypes = [ctypes.c_uint16]

        cls.lib.desk_sdk_pairing_server_poll.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_poll.argtypes = [ctypes.c_int]

        cls.lib.desk_sdk_pairing_server_stop.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_stop.argtypes = []

        cls.lib.desk_sdk_pairing_server_get_port.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_get_port.argtypes = []

        cls.lib.desk_firmware_poll.restype = ctypes.c_int
        cls.lib.desk_firmware_poll.argtypes = [ctypes.c_int]

        cls.lib.json_parse_top_level_str.restype = ctypes.c_bool
        cls.lib.json_parse_top_level_str.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_hardware_init()
        self.lib.desk_sdk_bridge_reset()
        self.lib.desk_sdk_bridge_init()

    def tearDown(self) -> None:
        self.lib.desk_hardware_register_driver_ops(None)
        if hasattr(self, "_driver_ops_keepalive"):
            self._driver_ops_keepalive.clear()

    def _process_cmd(self, cmd_dict: dict) -> dict:
        cmd_str = json.dumps(cmd_dict).encode("utf-8")
        out_buf = ctypes.create_string_buffer(4096)
        rc = self.lib.desk_sdk_bridge_process_command(cmd_str, out_buf, 4096)
        self.assertEqual(rc, 0, f"desk_sdk_bridge_process_command returned error code {rc}")
        return json.loads(out_buf.value.decode("utf-8"))

    def _parse_top_level(self, json_str: str, key: str, max_len: int = 128) -> str | None:
        buf = ctypes.create_string_buffer(max_len)
        ok = self.lib.json_parse_top_level_str(
            json_str.encode("utf-8"),
            key.encode("utf-8"),
            buf,
            max_len,
        )
        if ok:
            return buf.value.decode("utf-8")
        return None

    # -- helpers -----------------------------------------------------------

    def _process_pairing(self, payload: bytes) -> tuple[int, str]:
        """Returns (return_code, raw_reply_text). Reply text is "" on rejection."""
        out_buf = ctypes.create_string_buffer(2048)
        rc = self.lib.desk_sdk_bridge_process_pairing_udp(payload, out_buf, 2048)
        return rc, out_buf.value.decode("utf-8")

    def _official_pair_request(self, pairing_code: str, **overrides) -> bytes:
        body = {
            "type": "pair.request",
            "protocol": "watcher-lan-pairing",
            "version": "1.0",
            "request_id": "21a9dbf05ea3443480e62076f79a3b12",
            "daemon_instance_id": "f730f29e670c49f7a3320c4314eb9805",
            "pairing_code": pairing_code,
            "target_mode": "python_sdk",
            "websocket_port": 8765,
        }
        body.update(overrides)
        return json.dumps(body).encode("utf-8")

    def _register_motion_driver(self, move_rc: int, stop_rc: int) -> None:
        """Install a board driver whose motion primitives return the given codes.

        An MCU TX callback is registered too, so the hardware layer does not
        short-circuit on the host (no real UART) before reaching the driver.
        """
        @DRIVER_MOTION_MOVE_CB
        def on_move(pan, tilt, duration):
            return move_rc

        @DRIVER_MOTION_STOP_CB
        def on_stop(emergency):
            return stop_rc

        @MCU_TX_CB
        def on_mcu_tx(data, length, user_data):
            return None

        ops = DeskBoardDriverOps()
        ops.driver_name = b"faulty_test_bsp"
        ops.init = None
        ops.audio_record_read = None
        ops.audio_play_pcm = None
        ops.motion_move = on_move
        ops.motion_stop = on_stop
        ops.camera_capture = None
        ops.display_expression = None
        ops.deinit = None

        if not hasattr(self, "_driver_ops_keepalive"):
            self._driver_ops_keepalive = []
        self._driver_ops_keepalive.append((ops, on_move, on_stop, on_mcu_tx))
        self.assertEqual(self.lib.desk_hardware_register_mcu_tx_cb(on_mcu_tx, None), 0)
        self.assertEqual(self.lib.desk_hardware_register_driver_ops(ctypes.byref(ops)), 0)

    def _register_failing_motion_driver(self, move_rc: int, stop_rc: int) -> None:
        """Install a board driver whose motion primitives always fail."""
        self._register_motion_driver(move_rc, stop_rc)

    def _open_tcp_session(self):
        import socket

        rc = self.lib.desk_sdk_socket_server_start(0)
        self.assertEqual(rc, 0)
        port = self.lib.desk_sdk_socket_server_get_port()
        self.assertGreater(port, 1024)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", port))
        sock.settimeout(0.5)
        self.lib.desk_sdk_socket_server_poll(100)
        self.addCleanup(sock.close)
        self.addCleanup(self.lib.desk_sdk_socket_server_stop)
        return sock

    def _pump(self, polls: int = 3, timeout_ms: int = 50) -> None:
        for _ in range(polls):
            self.lib.desk_sdk_socket_server_poll(timeout_ms)

    @staticmethod
    def _recv_lines(sock, timeout: float = 0.4) -> list[str]:
        import socket as _socket

        sock.settimeout(timeout)
        buf = b""
        try:
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        except _socket.timeout:
            pass
        return [line for line in buf.decode("utf-8", errors="replace").split("\n") if line.strip()]

    def test_handshake_hello_and_capabilities(self) -> None:
        """Verifies sys.client.hello negotiation and capabilities list."""
        resp = self._process_cmd({
            "type": "sys.client.hello",
            "data": {"role": "desktop", "version": "1.0"},
        })
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)
        data = resp.get("data", {})
        self.assertEqual(data.get("role"), "device")
        caps = data.get("capabilities", [])
        expected_caps = ["inputs", "motion", "camera", "audio.stream", "expression.audio_follow.v1", "mcp_tools"]
        for cap in expected_caps:
            self.assertIn(cap, caps, f"Missing expected capability: {cap}")

    def test_motion_move_with_custom_angles(self) -> None:
        """Verifies motion command parses JSON arguments and sets hardware angles."""
        resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "data": {"pan_deg": 45.0, "tilt_deg": 110.0, "duration_ms": 400},
        })
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)

        pan = ctypes.c_float()
        tilt = ctypes.c_float()
        self.lib.desk_hardware_motion_get_position(ctypes.byref(pan), ctypes.byref(tilt))
        self.assertAlmostEqual(pan.value, 45.0, places=1)
        self.assertAlmostEqual(tilt.value, 110.0, places=1)

    def test_motion_move_with_pan_tilt_fallback(self) -> None:
        """Verifies motion command accepts pan/tilt when pan_deg/tilt_deg are omitted."""
        resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "data": {"pan": 55.0, "tilt": 125.0, "duration_ms": 350},
        })
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)

        pan = ctypes.c_float()
        tilt = ctypes.c_float()
        self.lib.desk_hardware_motion_get_position(ctypes.byref(pan), ctypes.byref(tilt))
        self.assertAlmostEqual(pan.value, 55.0, places=1)
        self.assertAlmostEqual(tilt.value, 125.0, places=1)

    def test_motion_clamping_constraints(self) -> None:
        """Verifies angles outside mechanical bounds are clamped correctly."""
        # Pan max limit 180, tilt max limit 140
        resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "data": {"pan_deg": 250.0, "tilt_deg": 160.0, "duration_ms": 300},
        })
        self.assertEqual(resp.get("code"), 0)

        pan = ctypes.c_float()
        tilt = ctypes.c_float()
        self.lib.desk_hardware_motion_get_position(ctypes.byref(pan), ctypes.byref(tilt))
        self.assertAlmostEqual(pan.value, 180.0, places=1)
        self.assertAlmostEqual(tilt.value, 140.0, places=1)

    def test_motion_rejected_when_arbiter_busy(self) -> None:
        """Verifies motion command returns RESOURCE_BUSY when arbiter is busy."""
        # Acquire motion gimbal by TCLAW_VOICE (priority 80)
        rc = self.lib.desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE, 0)
        self.assertEqual(rc, DESK_ARBITER_OK)

        resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "data": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 200},
        })
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 409)
        self.assertIn("RESOURCE_BUSY", resp.get("error", {}).get("code", ""))

        # Release ownership
        self.lib.desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE)

        # Now motion should succeed
        resp2 = self._process_cmd({
            "type": "ctrl.motion.move",
            "data": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 200},
        })
        self.assertEqual(resp2.get("type"), "sys.ack")
        self.assertEqual(resp2.get("code"), 0)

    def test_camera_lease_acquire_and_release(self) -> None:
        """Verifies camera acquire lease, conflict rejection, and release."""
        # Client A acquires camera lease
        resp = self._process_cmd({
            "type": "ctrl.camera.acquire",
            "data": {"client_id": "client-desk-01", "lease_duration_ms": 5000},
        })
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)

        # Client B tries to acquire during active lease -> RESOURCE_BUSY
        resp_conflict = self._process_cmd({
            "type": "ctrl.camera.acquire",
            "data": {"client_id": "client-intruder-02", "lease_duration_ms": 5000},
        })
        self.assertEqual(resp_conflict.get("type"), "sys.nack")
        self.assertEqual(resp_conflict.get("code"), 409)

        # Client A releases camera lease
        resp_rel = self._process_cmd({
            "type": "ctrl.camera.release",
            "data": {"client_id": "client-desk-01"},
        })
        self.assertEqual(resp_rel.get("type"), "sys.ack")
        self.assertEqual(resp_rel.get("code"), 0)

        # Now Client B can acquire
        resp_b = self._process_cmd({
            "type": "ctrl.camera.acquire",
            "data": {"client_id": "client-intruder-02", "lease_duration_ms": 5000},
        })
        self.assertEqual(resp_b.get("type"), "sys.ack")
        self.assertEqual(resp_b.get("code"), 0)

    def test_reject_unknown_command(self) -> None:
        """Verifies unknown operations return sys.nack with UNKNOWN_OPERATION, not empty ack."""
        resp = self._process_cmd({
            "type": "arbitrary.unknown.operation",
            "data": {"foo": "bar"},
        })
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 404)
        self.assertEqual(resp.get("error", {}).get("code"), "UNKNOWN_OPERATION")

    def test_json_parse_top_level_str_direct(self) -> None:
        """Unit tests for json_parse_top_level_str: top-level extraction, nested key isolation,
        escaped quotes, key-prefix collisions, and buffer limits."""
        # 1. Normal top-level extraction
        self.assertEqual(self._parse_top_level('{"type": "sys.ping"}', "type"), "sys.ping")
        self.assertEqual(self._parse_top_level('{"foo": "bar", "type": "motion.stop"}', "type"), "motion.stop")
        self.assertEqual(self._parse_top_level('{\n  "type"\t:\r\n  "ctrl.camera.acquire"\n}', "type"), "ctrl.camera.acquire")

        # 2. Nested key isolation: child object keys must not be parsed
        self.assertIsNone(self._parse_top_level('{"result": {"type": "ctrl.motion.stop"}}', "type"))
        self.assertEqual(
            self._parse_top_level('{"result": {"type": "ctrl.motion.stop"}, "type": "tool.response"}', "type"),
            "tool.response",
        )

        # 3. Nested key isolation: array element keys must not be parsed
        self.assertIsNone(self._parse_top_level('{"items": [{"type": "camera.acquire"}]}', "type"))
        self.assertEqual(
            self._parse_top_level('{"items": [{"type": "camera.acquire"}], "type": "sys.ping"}', "type"),
            "sys.ping",
        )

        # 4. Escaped quotes in strings before target key
        self.assertEqual(
            self._parse_top_level('{"note": "User said \\\"motion.stop\\\" now", "type": "tool.response"}', "type"),
            "tool.response",
        )
        self.assertEqual(
            self._parse_top_level('{"path": "C:\\\\folder\\\\sub", "type": "sys.ping"}', "type"),
            "sys.ping",
        )

        # 5. Key prefix collision prevention: e.g. "typeface" vs "type"
        self.assertEqual(
            self._parse_top_level('{"typeface": "sans-serif", "type": "sys.pong"}', "type"),
            "sys.pong",
        )
        self.assertIsNone(self._parse_top_level('{"types": "many"}', "type"))
        self.assertIsNone(self._parse_top_level('{"my_type": "custom"}', "type"))

        # 6. Non-string values: numbers, booleans, null, objects, arrays must return False
        self.assertIsNone(self._parse_top_level('{"type": 12345}', "type"))
        self.assertIsNone(self._parse_top_level('{"type": true}', "type"))
        self.assertIsNone(self._parse_top_level('{"type": null}', "type"))
        self.assertIsNone(self._parse_top_level('{"type": {"nested": "obj"}}', "type"))
        self.assertIsNone(self._parse_top_level('{"type": ["arr"]}', "type"))

        # 7. Empty string value
        self.assertEqual(self._parse_top_level('{"type": ""}', "type"), "")

        # 8. Malformed / non-object JSON
        self.assertIsNone(self._parse_top_level('["type", "sys.ping"]', "type"))
        self.assertIsNone(self._parse_top_level('not json', "type"))

        # 9. Buffer length truncation safety
        truncated = self._parse_top_level('{"type": "very_long_type_string_value"}', "type", max_len=10)
        self.assertEqual(truncated, "very_long")

    def test_tool_response_note_motion_stop_is_not_dispatched_as_command(self) -> None:
        """Verifies tool.response containing 'motion.stop' in notes does NOT dispatch motion command."""
        stop_calls = []

        @DRIVER_MOTION_STOP_CB
        def on_stop(emergency):
            stop_calls.append(emergency)
            return 0

        @DRIVER_MOTION_MOVE_CB
        def on_move(pan, tilt, duration):
            return 0

        ops = DeskBoardDriverOps()
        ops.driver_name = b"spy_motion_bsp"
        ops.init = None
        ops.audio_record_read = None
        ops.audio_play_pcm = None
        ops.motion_move = on_move
        ops.motion_stop = on_stop
        ops.camera_capture = None
        ops.display_expression = None
        ops.deinit = None

        if not hasattr(self, "_driver_ops_keepalive"):
            self._driver_ops_keepalive = []
        self._driver_ops_keepalive.append((ops, on_move, on_stop))
        self.lib.desk_hardware_register_driver_ops(ctypes.byref(ops))

        received_responses = []

        @TOOL_RESP_CB
        def resp_cb(resp_ptr, user_data):
            received_responses.append(ctypes.string_at(resp_ptr).decode("utf-8"))
            return 0

        self.lib.desk_sdk_bridge_register_tool_response_cb(resp_cb, None)

        tool_resp_payload = {
            "type": "tool.response",
            "request_id": "req-tool-note-001",
            "status": "succeeded",
            "result": {
                "action": "note_taking",
                "note": "User asked to execute ctrl.motion.stop immediately",
                "content": "motion.stop",
            },
        }
        resp = self._process_cmd(tool_resp_payload)
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)
        self.assertEqual(resp.get("request_id"), "req-tool-note-001")

        self.assertEqual(len(stop_calls), 0, "tool.response must not invoke motion_stop driver")
        self.assertEqual(len(received_responses), 1, "tool.response must route to tool response callback")

    def test_nested_keywords_in_tool_response_do_not_trigger_commands(self) -> None:
        """Verifies tool.response containing nested command strings (sys.ping, motion.move, camera.acquire)
        does not invoke any hardware or command handlers."""
        move_calls = []
        stop_calls = []

        @DRIVER_MOTION_MOVE_CB
        def on_move(pan, tilt, duration):
            move_calls.append((pan, tilt, duration))
            return 0

        @DRIVER_MOTION_STOP_CB
        def on_stop(emergency):
            stop_calls.append(emergency)
            return 0

        ops = DeskBoardDriverOps()
        ops.driver_name = b"spy_keywords_bsp"
        ops.init = None
        ops.audio_record_read = None
        ops.audio_play_pcm = None
        ops.motion_move = on_move
        ops.motion_stop = on_stop
        ops.camera_capture = None
        ops.display_expression = None
        ops.deinit = None

        if not hasattr(self, "_driver_ops_keepalive"):
            self._driver_ops_keepalive = []
        self._driver_ops_keepalive.append((ops, on_move, on_stop))
        self.lib.desk_hardware_register_driver_ops(ctypes.byref(ops))

        received_responses = []

        @TOOL_RESP_CB
        def resp_cb(resp_ptr, user_data):
            received_responses.append(ctypes.string_at(resp_ptr).decode("utf-8"))
            return 0

        self.lib.desk_sdk_bridge_register_tool_response_cb(resp_cb, None)

        test_payloads = [
            ("sys.ping", {"note": "nested string with sys.ping test", "content": "ping sys.ping"}),
            ("ctrl.motion.move", {"command": "ctrl.motion.move", "args": {"pan_deg": 45}}),
            ("motion.move", {"note": "motion.move target pan 90"}),
            ("ctrl.motion.stop", {"content": "emergency ctrl.motion.stop command"}),
            ("motion.stop", {"note": "user requested motion.stop", "content": "motion.stop"}),
            ("ctrl.camera.acquire", {"text": "acquire camera lease ctrl.camera.acquire"}),
            ("camera.acquire", {"note": "camera.acquire background"}),
            ("camera.capture", {"note": "camera.capture snapshot"}),
            ("ctrl.expression.play", {"expression": "ctrl.expression.play happy"}),
        ]

        for i, (kw, res_content) in enumerate(test_payloads):
            req_id = f"req-tool-nested-{i}"
            resp = self._process_cmd({
                "type": "tool.response",
                "request_id": req_id,
                "status": "succeeded",
                "result": res_content,
            })
            self.assertEqual(resp.get("type"), "sys.ack")
            self.assertNotEqual(resp.get("type"), "sys.pong", f"tool.response with {kw} must not return sys.pong")
            self.assertEqual(resp.get("code"), 0)
            self.assertEqual(resp.get("request_id"), req_id)

        self.assertEqual(len(move_calls), 0, "No motion commands should have been executed")
        self.assertEqual(len(stop_calls), 0, "No stop commands should have been executed")
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_CAMERA_SENSOR), DESK_OWNER_NONE)
        self.assertEqual(len(received_responses), len(test_payloads))

    def test_non_tool_envelope_with_hardware_keywords_fail_closes(self) -> None:
        """Verifies envelopes with missing type or non-command type containing hardware keywords
        (motion.stop, sys.ping, camera.acquire) fail-close without executing any hardware commands."""
        move_calls = []
        stop_calls = []

        @DRIVER_MOTION_MOVE_CB
        def on_move(pan, tilt, duration):
            move_calls.append((pan, tilt, duration))
            return 0

        @DRIVER_MOTION_STOP_CB
        def on_stop(emergency):
            stop_calls.append(emergency)
            return 0

        ops = DeskBoardDriverOps()
        ops.driver_name = b"spy_fail_close_bsp"
        ops.init = None
        ops.audio_record_read = None
        ops.audio_play_pcm = None
        ops.motion_move = on_move
        ops.motion_stop = on_stop
        ops.camera_capture = None
        ops.display_expression = None
        ops.deinit = None

        if not hasattr(self, "_driver_ops_keepalive"):
            self._driver_ops_keepalive = []
        self._driver_ops_keepalive.append((ops, on_move, on_stop))
        self.lib.desk_hardware_register_driver_ops(ctypes.byref(ops))

        adversarial_cases = [
            ("missing_type_with_stop", {
                "request_id": "req-adv-1",
                "note": "ctrl.motion.stop motion.stop emergency",
                "content": "sys.ping camera.acquire",
            }),
            ("chat_type_with_ping_stop_camera", {
                "type": "chat.message",
                "request_id": "req-adv-2",
                "note": "Please execute ctrl.motion.stop immediately",
                "content": "camera.acquire sys.ping motion.stop",
            }),
            ("event_type_with_hardware_keywords", {
                "type": "custom.event",
                "request_id": "req-adv-3",
                "data": {
                    "note": "ctrl.camera.acquire ctrl.motion.move",
                    "text": "ctrl.expression.play",
                },
            }),
        ]

        for name, payload in adversarial_cases:
            resp = self._process_cmd(payload)
            self.assertEqual(resp.get("type"), "sys.nack", f"{name} must fail-close with sys.nack")
            self.assertEqual(resp.get("code"), 404, f"{name} must return 404")
            self.assertEqual(resp.get("error", {}).get("code"), "UNKNOWN_OPERATION")

        self.assertEqual(len(move_calls), 0, "Adversarial messages must never trigger motion_move")
        self.assertEqual(len(stop_calls), 0, "Adversarial messages must never trigger motion_stop")
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_CAMERA_SENSOR), DESK_OWNER_NONE)

    def test_missing_or_malformed_type_fail_closes(self) -> None:
        """Verifies missing, malformed, or nested type fields return 404 UNKNOWN_OPERATION fail-closed."""
        cases = [
            ("missing_type", {"data": {"foo": "bar"}}),
            ("integer_type", {"type": 12345, "data": {}}),
            ("nested_type_only", {"wrapper": {"type": "ctrl.motion.stop"}}),
            ("empty_type", {"type": "", "data": {}}),
            ("null_type", {"type": None, "data": {}}),
        ]

        for name, payload in cases:
            resp = self._process_cmd(payload)
            self.assertEqual(resp.get("type"), "sys.nack", f"{name} must fail-close with sys.nack")
            self.assertEqual(resp.get("code"), 404, f"{name} must return 404")
            self.assertEqual(resp.get("error", {}).get("code"), "UNKNOWN_OPERATION")

    def test_legitimate_commands_still_route_correctly(self) -> None:
        """Verifies legitimate commands route to their proper handlers."""
        # 1. Ping
        ping_resp = self._process_cmd({"type": "sys.ping", "request_id": "req-ping-exact"})
        self.assertEqual(ping_resp.get("type"), "sys.pong")
        self.assertEqual(ping_resp.get("request_id"), "req-ping-exact")

        # 2. Stop (ctrl.motion.stop and motion.stop)
        stop_resp = self._process_cmd({
            "type": "ctrl.motion.stop",
            "request_id": "req-stop-exact",
            "data": {"emergency": True},
        })
        self.assertEqual(stop_resp.get("type"), "sys.ack")
        self.assertEqual(stop_resp.get("request_id"), "req-stop-exact")
        self.assertEqual(stop_resp.get("data", {}).get("status"), "accepted")
        self.assertTrue(stop_resp.get("data", {}).get("accepted"))
        self.assertIs(stop_resp.get("data", {}).get("state_confirmed"), False)
        self.assertTrue(stop_resp.get("data", {}).get("stopped"))

        stop_resp2 = self._process_cmd({
            "type": "motion.stop",
            "request_id": "req-stop-legacy",
            "data": {"emergency": False},
        })
        self.assertEqual(stop_resp2.get("type"), "sys.ack")
        self.assertEqual(stop_resp2.get("request_id"), "req-stop-legacy")
        self.assertEqual(stop_resp2.get("data", {}).get("status"), "accepted")
        self.assertTrue(stop_resp2.get("data", {}).get("accepted"))
        self.assertIs(stop_resp2.get("data", {}).get("state_confirmed"), False)
        self.assertTrue(stop_resp2.get("data", {}).get("stopped"))

        # 3. Move (ctrl.motion.move and motion.move)
        move_resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-move-exact",
            "data": {"pan_deg": 30.0, "tilt_deg": 100.0, "duration_ms": 300},
        })
        self.assertEqual(move_resp.get("type"), "sys.ack")
        self.assertEqual(move_resp.get("request_id"), "req-move-exact")
        self.assertEqual(move_resp.get("data", {}).get("status"), "accepted")
        self.assertTrue(move_resp.get("data", {}).get("accepted"))
        self.assertIs(move_resp.get("data", {}).get("state_confirmed"), False)

        move_resp2 = self._process_cmd({
            "type": "motion.move",
            "request_id": "req-move-legacy",
            "data": {"pan_deg": 0.0, "tilt_deg": 90.0, "duration_ms": 200},
        })
        self.assertEqual(move_resp2.get("type"), "sys.ack")
        self.assertEqual(move_resp2.get("request_id"), "req-move-legacy")
        self.assertEqual(move_resp2.get("data", {}).get("status"), "accepted")
        self.assertTrue(move_resp2.get("data", {}).get("accepted"))
        self.assertIs(move_resp2.get("data", {}).get("state_confirmed"), False)

        # 4. Camera lease acquire & release
        acq_resp = self._process_cmd({
            "type": "ctrl.camera.acquire",
            "request_id": "req-cam-acq",
            "data": {"client_id": "test-client-1", "lease_duration_ms": 5000},
        })
        self.assertEqual(acq_resp.get("type"), "sys.ack")
        self.assertEqual(acq_resp.get("request_id"), "req-cam-acq")
        self.assertTrue(acq_resp.get("data", {}).get("accepted"))

        rel_resp = self._process_cmd({
            "type": "ctrl.camera.release",
            "request_id": "req-cam-rel",
            "data": {"client_id": "test-client-1"},
        })
        self.assertEqual(rel_resp.get("type"), "sys.ack")
        self.assertEqual(rel_resp.get("request_id"), "req-cam-rel")
        self.assertTrue(rel_resp.get("data", {}).get("accepted"))

        # 5. Expression play
        exp_resp = self._process_cmd({
            "type": "ctrl.expression.play",
            "request_id": "req-exp-play",
            "data": {"preset": "happy", "text": "Hello world"},
        })
        self.assertEqual(exp_resp.get("type"), "sys.ack")
        self.assertEqual(exp_resp.get("request_id"), "req-exp-play")
        self.assertTrue(exp_resp.get("data", {}).get("accepted"))
        self.assertEqual(exp_resp.get("data", {}).get("preset"), "happy")

        # 6. Expression audio follow
        follow_resp = self._process_cmd({
            "type": "ctrl.expression.audio_follow",
            "request_id": "req-exp-follow",
            "data": {"enabled": True},
        })
        self.assertEqual(follow_resp.get("type"), "sys.ack")
        self.assertEqual(follow_resp.get("request_id"), "req-exp-follow")
        self.assertTrue(follow_resp.get("data", {}).get("enabled"))

        # 7. Tool response
        tool_resp = self._process_cmd({
            "type": "tool.response",
            "request_id": "req-tool-resp-legit",
            "status": "succeeded",
            "result": {"status": "done"},
        })
        self.assertEqual(tool_resp.get("type"), "sys.ack")
        self.assertEqual(tool_resp.get("request_id"), "req-tool-resp-legit")
        self.assertEqual(tool_resp.get("data", {}).get("status"), "received")

    def test_touch_event_formatting(self) -> None:
        """Verifies hardware touch events are formatted according to contract envelope and schema."""
        out_buf = ctypes.create_string_buffer(2048)
        rc = self.lib.desk_sdk_bridge_send_touch_event(
            DESK_TOUCH_TARGET_BACKTOUCH,
            DESK_TOUCH_ACTION_SHORT_PRESS,
            out_buf,
            2048,
        )
        self.assertEqual(rc, 0)
        event_dict = json.loads(out_buf.value.decode("utf-8"))
        self.assertEqual(event_dict.get("schema_version"), "1.0.0")
        self.assertEqual(event_dict.get("sequence_scope"), "device:esp32s3")
        self.assertEqual(event_dict.get("type"), "resource.touch_event")
        self.assertTrue(event_dict.get("event_id", "").startswith("evt-"))
        payload = event_dict.get("payload", {})
        self.assertEqual(payload.get("target"), "backtouch")
        self.assertEqual(payload.get("action"), "short_press")

        # Validate with official JSON schemas
        import jsonschema
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
        with open(os.path.join(repo_root, "contracts", "schemas", "envelope_event.json"), "r", encoding="utf-8") as f:
            envelope_schema = json.load(f)
        with open(os.path.join(repo_root, "contracts", "schemas", "domain_resource.json"), "r", encoding="utf-8") as f:
            domain_schema = json.load(f)

        jsonschema.validate(instance=event_dict, schema=envelope_schema)
        touch_payload_schema = domain_schema["definitions"]["touch_event_payload"]
        jsonschema.validate(instance=payload, schema=touch_payload_schema)

    def test_wspk_native_c_pack_and_unpack(self) -> None:
        """Verifies native C WSPK header serialization and deserialization."""
        buf = (ctypes.c_uint8 * 16)()
        rc = self.lib.desk_wspk_pack_header(
            1,     # audio
            0x01,  # EOS flag
            55,    # stream_id
            1001,  # sequence
            1920,  # payload_size
            buf,
            16,
        )
        self.assertEqual(rc, 0)

        out_hdr = DeskWspkHeader()
        rc_unpack = self.lib.desk_wspk_unpack_header(buf, 16, ctypes.byref(out_hdr))
        self.assertEqual(rc_unpack, 0)
        self.assertEqual(out_hdr.magic, b"WSPK")
        self.assertEqual(out_hdr.frame_type, 1)
        self.assertEqual(out_hdr.flags, 0x01)
        self.assertEqual(out_hdr.stream_id, 55)
        self.assertEqual(out_hdr.sequence, 1001)
        self.assertEqual(out_hdr.payload_size, 1920)

    def test_bidirectional_tool_request_and_response(self) -> None:
        """Verifies TClaw tool request forwarding and response callback routing."""
        received_requests: list[str] = []
        received_responses: list[str] = []

        @OUTBOUND_CB
        def outbound_handler(data_ptr, length, user_data):
            raw = ctypes.string_at(data_ptr, length).decode("utf-8")
            received_requests.append(raw)
            return 0

        @TOOL_RESP_CB
        def response_handler(resp_ptr, user_data):
            raw = ctypes.string_at(resp_ptr).decode("utf-8")
            received_responses.append(raw)
            return 0

        self.lib.desk_sdk_bridge_register_outbound(outbound_handler, None)
        self.lib.desk_sdk_bridge_register_tool_response_cb(response_handler, None)

        # Dispatch tool request envelope
        req_env = json.dumps({
            "schema_version": "1.0.0",
            "request_id": "req-tclaw-9988",
            "operation": "ha.get_state",
            "arguments": {"entity_id": "light.desk_lamp"},
            "expires_at": "2026-10-13T12:00:00Z",
        })
        rc_req = self.lib.desk_sdk_bridge_dispatch_tool_request(req_env.encode("utf-8"))
        self.assertEqual(rc_req, 0)
        self.assertEqual(len(received_requests), 1)
        req_parsed = json.loads(received_requests[0])
        self.assertEqual(req_parsed["request_id"], "req-tclaw-9988")

        # Reply with tool response envelope
        resp_env = json.dumps({
            "schema_version": "1.0.0",
            "request_id": "req-tclaw-9988",
            "status": "succeeded",
            "timestamp": "2026-10-13T11:00:00Z",
            "result": {"state": "on"},
        })
        rc_resp = self.lib.desk_sdk_bridge_handle_tool_response(resp_env.encode("utf-8"))
        self.assertEqual(rc_resp, 0)
        self.assertEqual(len(received_responses), 1)
        resp_parsed = json.loads(received_responses[0])
        self.assertEqual(resp_parsed["request_id"], "req-tclaw-9988")

    def test_udp_pairing_protocol_exchange(self) -> None:
        """Verifies UDP pair.request handling and pair.accept generation per watcher_lan_pairing_v1.

        The expected pairing code must be provisioned explicitly (interfaces.md 6.4);
        nothing is granted before it validates.
        """
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        self.assertTrue(self.lib.desk_sdk_bridge_is_pairing_code_configured())

        rc, reply = self._process_pairing(self._official_pair_request("123456"))
        self.assertEqual(rc, 0)
        self.assertNotEqual(reply, "")

        rsp = json.loads(reply)
        self.assertEqual(rsp["type"], "pair.accept")
        self.assertEqual(rsp["protocol"], "watcher-lan-pairing")
        self.assertEqual(rsp["version"], "1.0")
        self.assertEqual(rsp["request_id"], "21a9dbf05ea3443480e62076f79a3b12")
        self.assertEqual(rsp["daemon_instance_id"], "f730f29e670c49f7a3320c4314eb9805")
        self.assertEqual(rsp["target_mode"], "python_sdk")
        self.assertEqual(len(rsp["session_token"]), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in rsp["session_token"]))

    def test_hardware_hello_handshake_with_audio_negotiation(self) -> None:
        """Verifies sys.client.hello with role=hardware negotiates Opus audio uplink per contract."""
        hardware_hello = json.dumps({
            "type": "sys.client.hello",
            "code": 0,
            "data": {
                "role": "hardware",
                "pairing_protocol": "watcher-lan-pairing",
                "pairing_version": "1.0",
                "pair_request_id": "21a9dbf05ea3443480e62076f79a3b12",
                "daemon_instance_id": "f730f29e670c49f7a3320c4314eb9805",
                "session_token": "f84a1e16ce6f35f14d167f227a93ea93d1a9c4d9eb5517112030f2839d57ae4b",
                "mode": "python_sdk",
            },
        })
        out_buf = ctypes.create_string_buffer(2048)
        rc = self.lib.desk_sdk_bridge_process_command(hardware_hello.encode("utf-8"), out_buf, 2048)
        self.assertEqual(rc, 0)

        rsp = json.loads(out_buf.value.decode("utf-8"))
        self.assertEqual(rsp["type"], "sys.ack")
        self.assertEqual(rsp["code"], 0)
        self.assertEqual(rsp["data"]["role"], "hardware")
        self.assertEqual(rsp["data"]["session_state"], "connected")
        audio_up = rsp["data"]["negotiated"]["audio_uplink"]
        self.assertEqual(audio_up["codec"], "opus")
        self.assertEqual(audio_up["sample_rate"], 16000)
        self.assertEqual(audio_up["channels"], 1)

    def test_media_frame_pack_and_unpack(self) -> None:
        """Verifies packaging complete WSPK binary media frames with Little-Endian headers."""
        payload = b"TEST_AUDIO_PAYLOAD_BYTES"
        out_buf = ctypes.create_string_buffer(128)
        out_len = ctypes.c_size_t()

        rc = self.lib.desk_sdk_bridge_pack_media_frame(
            1, 0, 42, 100, payload, len(payload), out_buf, 128, ctypes.byref(out_len)
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out_len.value, 16 + len(payload))

        # Check raw bytes
        raw = bytes(out_buf.raw[:out_len.value])
        self.assertEqual(raw[:4], b"WSPK")
        import struct
        magic, f_type, flags, s_id, seq, p_size = struct.unpack("<4sBBHII", raw[:16])
        self.assertEqual(f_type, 1)
        self.assertEqual(s_id, 42)
        self.assertEqual(seq, 100)
        self.assertEqual(p_size, len(payload))
        self.assertEqual(raw[16:], payload)

    def test_live_tcp_socket_communication(self) -> None:
        """Verifies live TCP socket server startup, connection, JSON command processing, and event dispatch."""
        import socket
        import time

        # Start socket server on ephemeral port (127.0.0.1)
        rc = self.lib.desk_sdk_socket_server_start(0)
        self.assertEqual(rc, 0)

        port = self.lib.desk_sdk_socket_server_get_port()
        self.assertGreater(port, 1024)

        # Connect a real TCP socket client
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_sock.connect(("127.0.0.1", port))
        client_sock.settimeout(2.0)

        # Poll server to accept client
        self.lib.desk_sdk_socket_server_poll(100)

        # Send sys.ping over TCP (newline-delimited framing, interfaces.md 6.3)
        ping_msg = json.dumps({"type": "sys.ping"}).encode("utf-8")
        client_sock.sendall(ping_msg + b"\n")

        # Poll server to process command
        self.lib.desk_sdk_socket_server_poll(100)

        # Read response from socket
        resp_data = client_sock.recv(1024).decode("utf-8").strip()
        resp_json = json.loads(resp_data)
        self.assertEqual(resp_json["type"], "sys.pong")

        # Send motion.move over TCP
        move_msg = json.dumps({
            "type": "motion.move",
            "request_id": "req-live-move-01",
            "pan_deg": 110.0,
            "tilt_deg": 115.0,
            "duration_ms": 300,
        }).encode("utf-8")
        client_sock.sendall(move_msg + b"\n")
        self.lib.desk_sdk_socket_server_poll(100)

        resp_move = client_sock.recv(1024).decode("utf-8").strip()
        resp_move_json = json.loads(resp_move)
        self.assertEqual(resp_move_json["type"], "sys.ack")
        # A device ACK means "accepted", never "succeeded" (interfaces.md 6.1).
        self.assertEqual(resp_move_json["data"]["status"], "accepted")
        self.assertTrue(resp_move_json["data"]["accepted"])
        self.assertEqual(resp_move_json["request_id"], "req-live-move-01")
        self.assertEqual(resp_move_json["data"]["pan_deg"], 110.0)

        # Trigger event and verify socket receives it
        self.lib.desk_sdk_bridge_send_event(b"device.status", b'{"battery": 95}')
        event_raw = client_sock.recv(1024).decode("utf-8").strip()
        event_json = json.loads(event_raw)
        self.assertEqual(event_json["type"], "device.status")
        self.assertEqual(event_json["payload"]["battery"], 95)

        client_sock.close()
        self.lib.desk_sdk_socket_server_stop()

    def test_udp_pairing_discovery_server(self) -> None:
        """Verifies UDP Pairing Discovery server (watcher_lan_pairing_v1) responds to pairing requests."""
        import socket

        # Provision the device-side expected code first (fail-closed otherwise).
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)

        # Start UDP pairing server on ephemeral port
        rc = self.lib.desk_sdk_pairing_server_start(0)
        self.assertEqual(rc, 0)

        udp_port = self.lib.desk_sdk_pairing_server_get_port()
        self.assertGreater(udp_port, 1024)

        # Send pair.request datagram from client
        client_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_udp.settimeout(2.0)

        pair_req = json.dumps({
            "type": "pair.request",
            "protocol": "watcher_lan_pairing_v1",
            "client_id": "test_py_sdk_runner",
            "pairing_code": "123456",
            "timestamp": 1792000000,
        }).encode("utf-8")

        client_udp.sendto(pair_req, ("127.0.0.1", udp_port))

        # Poll pairing server to process datagram and reply
        self.lib.desk_sdk_pairing_server_poll(100)

        # Read reply from server
        data, _ = client_udp.recvfrom(2048)
        reply = json.loads(data.decode("utf-8"))

        self.assertEqual(reply.get("type"), "pair.accept")
        self.assertEqual(reply.get("status"), "accepted")
        self.assertIn("device_id", reply)
        self.assertIn("token", reply)
        self.assertIn("port", reply)

        client_udp.close()
        self.lib.desk_sdk_pairing_server_stop()

    def test_udp_pairing_discovery_server_silent_when_code_unprovisioned(self) -> None:
        """Fail-closed: with no provisioned code the server must not answer a pairing request."""
        import socket

        # The provisioned code deliberately survives a bridge reset, so clear it here.
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(None), 0)
        self.assertFalse(self.lib.desk_sdk_bridge_is_pairing_code_configured())

        rc = self.lib.desk_sdk_pairing_server_start(0)
        self.assertEqual(rc, 0)
        udp_port = self.lib.desk_sdk_pairing_server_get_port()

        client_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        client_udp.settimeout(0.4)
        try:
            client_udp.sendto(
                json.dumps({
                    "type": "pair.request",
                    "protocol": "watcher_lan_pairing_v1",
                    "client_id": "test_py_sdk_runner",
                    "pairing_code": "123456",
                }).encode("utf-8"),
                ("127.0.0.1", udp_port),
            )
            self.lib.desk_sdk_pairing_server_poll(100)
            with self.assertRaises(socket.timeout, msg="unprovisioned device must not reply pair.accept"):
                client_udp.recvfrom(2048)
        finally:
            client_udp.close()
            self.lib.desk_sdk_pairing_server_stop()

    def test_socket_server_bind_any(self) -> None:
        """Verifies TCP socket server can bind to 0.0.0.0 via start_ex or start."""
        rc = self.lib.desk_sdk_socket_server_start_ex(b"0.0.0.0", 0)
        self.assertEqual(rc, 0)
        port = self.lib.desk_sdk_socket_server_get_port()
        self.assertGreater(port, 1024)

        # Connect over loopback to verify 0.0.0.0 accepts loopback connections
        import socket
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.connect(("127.0.0.1", port))
        self.lib.desk_sdk_socket_server_poll(50)
        s.close()
        self.lib.desk_sdk_socket_server_stop()

    def test_tcp_tool_request_forwarding_over_socket(self) -> None:
        """Verifies desk_sdk_bridge_dispatch_tool_request writes tool request envelope to TCP client."""
        import socket
        rc = self.lib.desk_sdk_socket_server_start(0)
        self.assertEqual(rc, 0)
        port = self.lib.desk_sdk_socket_server_get_port()

        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_sock.connect(("127.0.0.1", port))
        client_sock.settimeout(1.0)
        self.lib.desk_sdk_socket_server_poll(100)

        # Dispatch tool request
        tool_req = json.dumps({
            "schema_version": "1.0.0",
            "request_id": "req-tclaw-tcp-01",
            "operation": "reminder.create",
            "arguments": {"title": "Meeting in 5 mins", "due_at": "2026-10-16T15:00:00Z"},
            "expires_at": "2026-10-16T15:01:00Z",
        })
        rc_disp = self.lib.desk_sdk_bridge_dispatch_tool_request(tool_req.encode("utf-8"))
        self.assertEqual(rc_disp, 0)

        # Receive on client socket
        raw_received = client_sock.recv(4096).decode("utf-8")
        self.assertTrue(raw_received.endswith("\n"))
        data = json.loads(raw_received.strip())
        self.assertEqual(data["request_id"], "req-tclaw-tcp-01")
        self.assertEqual(data["operation"], "reminder.create")

        # Reply with tool response
        tool_resp = json.dumps({
            "type": "tool.response",
            "request_id": "req-tclaw-tcp-01",
            "data": {
                "schema_version": "1.0.0",
                "request_id": "req-tclaw-tcp-01",
                "status": "succeeded",
                "timestamp": "2026-10-16T15:00:01Z",
                "result": {"reminder_id": "rem-101"},
            },
        })
        client_sock.sendall((tool_resp + "\n").encode("utf-8"))
        self.lib.desk_sdk_socket_server_poll(100)

        ack_data = client_sock.recv(1024).decode("utf-8").strip()
        ack_json = json.loads(ack_data)
        self.assertEqual(ack_json.get("type"), "sys.ack")
        self.assertEqual(ack_json.get("code"), 0)

        client_sock.close()
        self.lib.desk_sdk_socket_server_stop()

    def test_robot_adapter_send_command_timeout_returns_408_nack(self) -> None:
        """Verifies RobotAdapter.send_command returns 408 NACK instead of 0 on timeout."""
        import socket
        import threading
        from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter

        srv_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv_sock.bind(("127.0.0.1", 0))
        srv_sock.listen(1)
        port = srv_sock.getsockname()[1]

        conn_box: list[socket.socket] = []

        def _srv():
            try:
                conn, _ = srv_sock.accept()
                conn_box.append(conn)
                # Receive sys.client.hello and send sys.ack
                hello_line = conn.recv(1024).decode("utf-8")
                if "hello" in hello_line:
                    ack = json.dumps({"type": "sys.ack", "code": 0, "data": {"capabilities": ["motion"]}}) + "\n"
                    conn.sendall(ack.encode("utf-8"))
                # For next commands, don't reply so client times out
                while True:
                    data = conn.recv(1024)
                    if not data:
                        break
            except Exception:
                pass
            finally:
                if conn_box:
                    try:
                        conn_box[0].close()
                    except Exception:
                        pass

        th = threading.Thread(target=_srv, daemon=True)
        th.start()

        adapter = RobotAdapter(host="127.0.0.1", tcp_port=port, auto_pair=False, token="dummy-token")
        try:
            self.assertTrue(adapter.connect_tcp(timeout=2.0))
            # Send a command with a very short timeout
            res = adapter.send_command({"type": "ctrl.motion.move", "data": {"pan": 0}}, timeout=0.1)
            self.assertEqual(res.get("type"), "sys.nack")
            self.assertEqual(res.get("code"), 408)
            self.assertEqual(res.get("status"), "timeout")
            self.assertIn("TIMEOUT", res.get("error", {}).get("code", ""))
        finally:
            adapter.stop()
            if conn_box:
                try:
                    conn_box[0].close()
                except Exception:
                    pass
            srv_sock.close()
            th.join(timeout=1.0)

    def test_e2e_real_bridge_tool_dispatch_and_response_loop(self) -> None:
        """Verifies real end-to-end tool request dispatch from firmware to RobotAdapter and tool response back."""
        import socket
        import threading
        import time
        from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter

        # 1. Start C bridge TCP socket server on ephemeral port
        rc = self.lib.desk_sdk_socket_server_start(0)
        self.assertEqual(rc, 0)
        port = self.lib.desk_sdk_socket_server_get_port()
        self.assertGreater(port, 1024)

        # 2. Register C bridge tool response callback
        received_responses: list[dict] = []
        @TOOL_RESP_CB
        def on_bridge_tool_response(resp_ptr, user_data):
            raw = ctypes.string_at(resp_ptr).decode("utf-8")
            received_responses.append(json.loads(raw))
            return 0

        self.lib.desk_sdk_bridge_register_tool_response_cb(on_bridge_tool_response, None)

        # 3. Create RobotAdapter and register tool request handler
        adapter = RobotAdapter(host="127.0.0.1", tcp_port=port, auto_pair=False, token="e2e-token")
        received_requests: list[dict] = []
        response_sent_event = threading.Event()
        adapter_send_result: list[bool] = []

        def on_adapter_tool_request(raw_msg: dict) -> None:
            received_requests.append(raw_msg)
            req_id = raw_msg.get("request_id") or raw_msg.get("data", {}).get("request_id")
            tool_resp = {
                "schema_version": "1.0.0",
                "request_id": req_id,
                "status": "succeeded",
                "timestamp": "2026-10-16T15:30:00Z",
                "result": {"status": "ok", "delivered": True},
            }
            res = adapter.send_tool_response(tool_resp)
            adapter_send_result.append(res)
            response_sent_event.set()

        adapter.register_tool_request_handler(on_adapter_tool_request)

        try:
            # 4. Start RobotAdapter and poll C bridge server until connected
            adapter.start()
            t0 = time.time()
            while time.time() - t0 < 2.0:
                self.lib.desk_sdk_socket_server_poll(20)
                if adapter.is_connected:
                    break
                time.sleep(0.01)
            self.assertTrue(adapter.is_connected, "RobotAdapter failed to connect to C bridge")

            # 5. Dispatch tool request from C bridge over TCP
            t_start = time.time()
            tool_req = json.dumps({
                "schema_version": "1.0.0",
                "request_id": "req-e2e-tclaw-99",
                "operation": "reminder.create",
                "arguments": {"title": "Team Standup", "due_at": "2026-10-16T18:00:00Z"},
                "expires_at": "2030-01-01T00:00:00Z",
            })
            rc_disp = self.lib.desk_sdk_bridge_dispatch_tool_request(tool_req.encode("utf-8"))
            self.assertEqual(rc_disp, 0)

            # 6. Poll C bridge until tool response is received and ACKed
            while time.time() - t_start < 2.0:
                self.lib.desk_sdk_socket_server_poll(20)
                if len(received_responses) > 0 and len(adapter_send_result) > 0:
                    break
                time.sleep(0.01)

            elapsed_ms = (time.time() - t_start) * 1000
            self.assertTrue(response_sent_event.wait(timeout=1.0), "Response event was not set")
            self.assertEqual(len(received_requests), 1)
            self.assertEqual(received_requests[0].get("request_id"), "req-e2e-tclaw-99")
            self.assertEqual(len(adapter_send_result), 1)
            self.assertTrue(adapter_send_result[0], "send_tool_response must return True (code 0)")
            self.assertEqual(len(received_responses), 1)
            self.assertEqual(received_responses[0].get("request_id"), "req-e2e-tclaw-99")
            self.assertEqual(received_responses[0].get("data", {}).get("status"), "succeeded")
        finally:
            adapter.stop()
            self.lib.desk_sdk_socket_server_stop()

    def test_ctrl_expression_play_and_set_success(self) -> None:
        """Verifies ctrl.expression.play and ctrl.expression.set update hardware expression and trigger audio alert."""
        audio_played = []
        def on_audio_play(pcm_ptr, samples, rate, user_data):
            for i in range(samples):
                audio_played.append(pcm_ptr[i])

        cb_holder = self.AUDIO_PLAY_CB(on_audio_play)
        self.lib.desk_hardware_register_audio_play_cb(cb_holder, None)

        # 1. Test ctrl.expression.play with reminder.alert event
        resp = self._process_cmd({
            "type": "ctrl.expression.play",
            "request_id": "req-play-alert-1",
            "data": {
                "preset": "alert",
                "text": "Meeting Reminder",
                "event_type": "reminder.alert",
            },
        })
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)
        self.assertEqual(resp.get("request_id"), "req-play-alert-1")
        self.assertEqual(resp.get("data", {}).get("status"), "accepted")
        self.assertTrue(resp.get("data", {}).get("accepted"))
        self.assertEqual(resp.get("data", {}).get("preset"), "alert")
        self.assertEqual(resp.get("data", {}).get("text"), "Meeting Reminder")

        preset_buf = ctypes.create_string_buffer(32)
        status_buf = ctypes.create_string_buffer(64)
        self.lib.desk_hardware_display_get_expression(preset_buf, 32, status_buf, 64)
        self.assertEqual(preset_buf.value.decode("utf-8"), "alert")
        self.assertEqual(status_buf.value.decode("utf-8"), "Meeting Reminder")
        self.assertGreater(len(audio_played), 0, "Alert audio PCM should be played when event_type is reminder.alert")

        # 2. Test ctrl.expression.set
        resp2 = self._process_cmd({
            "type": "ctrl.expression.set",
            "request_id": "req-set-happy-2",
            "data": {
                "preset": "happy",
                "text": "Standing By",
            },
        })
        self.assertEqual(resp2.get("type"), "sys.ack")
        self.assertEqual(resp2.get("code"), 0)
        self.assertEqual(resp2.get("request_id"), "req-set-happy-2")
        self.assertEqual(resp2.get("data", {}).get("preset"), "happy")
        self.assertEqual(resp2.get("data", {}).get("text"), "Standing By")

        self.lib.desk_hardware_display_get_expression(preset_buf, 32, status_buf, 64)
        self.assertEqual(preset_buf.value.decode("utf-8"), "happy")
        self.assertEqual(status_buf.value.decode("utf-8"), "Standing By")

    def test_ctrl_expression_play_resource_busy_preemption(self) -> None:
        """Verifies ctrl.expression.play returns 409 RESOURCE_BUSY when DESK_RES_DISPLAY is held by higher priority owner."""
        # 1. Acquire display by higher priority owner (TCLAW_VOICE)
        rc = self.lib.desk_arbiter_acquire(DESK_RES_DISPLAY_UI, DESK_OWNER_TCLAW_VOICE, 0)
        self.assertEqual(rc, DESK_ARBITER_OK)

        # 2. Attempt ctrl.expression.play from SDK client -> must return 409 RESOURCE_BUSY
        resp = self._process_cmd({
            "type": "ctrl.expression.play",
            "request_id": "req-busy-409",
            "data": {
                "preset": "alert",
                "text": "Blocked Announcement",
            },
        })
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 409)
        self.assertEqual(resp.get("request_id"), "req-busy-409")
        self.assertEqual(resp.get("error", {}).get("code"), "RESOURCE_BUSY")

        # 3. Release display and verify expression play succeeds
        self.lib.desk_arbiter_release(DESK_RES_DISPLAY_UI, DESK_OWNER_TCLAW_VOICE)
        resp2 = self._process_cmd({
            "type": "ctrl.expression.play",
            "request_id": "req-unlocked-200",
            "data": {
                "preset": "idle",
                "text": "Unlocked",
            },
        })
        self.assertEqual(resp2.get("type"), "sys.ack")
        self.assertEqual(resp2.get("code"), 0)

    def test_ctrl_motion_stop(self) -> None:
        """Verifies ctrl.motion.stop stops gimbal and returns sys.ack with stopped: True and state_confirmed: False."""
        resp = self._process_cmd({
            "type": "ctrl.motion.stop",
            "request_id": "req-motion-stop-1",
            "data": {"emergency": True},
        })
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)
        self.assertEqual(resp.get("request_id"), "req-motion-stop-1")
        self.assertEqual(resp.get("data", {}).get("status"), "accepted")
        self.assertTrue(resp.get("data", {}).get("accepted"))
        self.assertIs(resp.get("data", {}).get("state_confirmed"), False)
        self.assertTrue(resp.get("data", {}).get("stopped"))

    def test_ctrl_expression_play_hardware_error_in_strict_mode(self) -> None:
        """Verifies ctrl.expression.play returns 500 HARDWARE_ERROR when display hardware fails in strict mode."""
        self.lib.desk_hardware_set_strict_mode(True)
        self.lib.desk_hardware_set_display_initialized(False)

        preset_buf = ctypes.create_string_buffer(32)
        status_buf = ctypes.create_string_buffer(64)
        self.lib.desk_hardware_display_get_expression(preset_buf, 32, status_buf, 64)
        initial_preset = preset_buf.value.decode("utf-8")
        initial_status = status_buf.value.decode("utf-8")

        resp = self._process_cmd({
            "type": "ctrl.expression.play",
            "request_id": "req-hw-err-500",
            "data": {
                "preset": "dizzy",
                "text": "Failing Hardware",
            },
        })
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 500)
        self.assertEqual(resp.get("request_id"), "req-hw-err-500")
        self.assertEqual(resp.get("error", {}).get("code"), "HARDWARE_ERROR")

        # Verify dirty cache prevention: caches did NOT update on failure
        self.lib.desk_hardware_display_get_expression(preset_buf, 32, status_buf, 64)
        self.assertEqual(preset_buf.value.decode("utf-8"), initial_preset)
        self.assertEqual(status_buf.value.decode("utf-8"), initial_status)

        # Re-enabling display hardware in strict mode allows expression play to succeed
        self.lib.desk_hardware_set_display_initialized(True)
        resp2 = self._process_cmd({
            "type": "ctrl.expression.play",
            "request_id": "req-hw-ok-200",
            "data": {
                "preset": "happy",
                "text": "Hardware OK",
            },
        })
        self.assertEqual(resp2.get("type"), "sys.ack")
        self.assertEqual(resp2.get("code"), 0)
        self.assertEqual(resp2.get("data", {}).get("preset"), "happy")

        self.lib.desk_hardware_set_strict_mode(False)

    # ------------------------------------------------------------------
    # WP1 -- driver return values must never be reported as code:0
    # ------------------------------------------------------------------

    def test_motion_move_driver_failure_returns_hardware_fault_nack(self) -> None:
        """Strict-mode variant of the driver-failure contract; default-mode coverage
        is in test_motion_move_driver_failure_default_mode_returns_hardware_fault_nack.

        A failing gimbal driver must produce sys.nack HARDWARE_FAULT, never code:0.
        """
        self._register_failing_motion_driver(DESK_HW_ERR_STALL, DESK_HW_OK)
        self.lib.desk_hardware_set_strict_mode(True)

        resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-hw-fault-move",
            "data": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 300},
        })
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 500)
        self.assertEqual(resp.get("request_id"), "req-hw-fault-move")
        self.assertEqual(resp.get("error", {}).get("code"), "HARDWARE_FAULT")
        self.assertNotIn("data", resp, "a driver failure must not carry a success payload")

    def test_motion_stop_driver_failure_returns_hardware_fault_nack(self) -> None:
        """Strict-mode variant of the driver-failure contract; default-mode coverage
        is in test_motion_stop_driver_failure_default_mode_returns_hardware_fault_nack.

        A failing stop driver must produce sys.nack HARDWARE_FAULT, never stopped:true.
        """
        self._register_failing_motion_driver(DESK_HW_OK, DESK_HW_ERR_STALL)
        self.lib.desk_hardware_set_strict_mode(True)

        resp = self._process_cmd({
            "type": "ctrl.motion.stop",
            "request_id": "req-hw-fault-stop",
            "data": {"emergency": True},
        })
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 500)
        self.assertEqual(resp.get("request_id"), "req-hw-fault-stop")
        self.assertEqual(resp.get("error", {}).get("code"), "HARDWARE_FAULT")

    def test_motion_move_success_ack_is_accepted_not_succeeded(self) -> None:
        """interfaces.md 6.1 & CR-002: a successful motion ACK means accepted, not succeeded, state_confirmed: false."""
        resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-accepted-level",
            "data": {"pan_deg": 45.0, "tilt_deg": 115.0, "duration_ms": 200},
        })
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)
        self.assertEqual(resp.get("data", {}).get("status"), "accepted")
        self.assertTrue(resp.get("data", {}).get("accepted"))
        self.assertIs(resp.get("data", {}).get("state_confirmed"), False)
        self.assertNotEqual(resp.get("data", {}).get("status"), "succeeded")

    # ------------------------------------------------------------------
    # WP1b -- driver-failure contract at DEFAULT initialisation.
    #
    # Evidence type: real compiled C protocol tests. These drive the actual
    # desk_sdk_bridge_process_command() entry point of the C shared library
    # (not a direct C function test, and not a Python fake device).
    #
    # Why this block exists: the two WP1 tests above call
    # desk_hardware_set_strict_mode(True), so they passed even while
    # desk_hardware.c propagated a driver error ONLY under strict mode
    # (fixed in 926c82a). A test that only passes because it flips a mode
    # flag does not cover the shipped default configuration. These tests run
    # at the default initialisation, i.e. with strict_mode == false.
    # ------------------------------------------------------------------

    def _assert_default_mode(self) -> None:
        """Fail loudly if strict mode is not at its default (false)."""
        self.assertFalse(
            self.lib.desk_hardware_is_strict_mode(),
            "this test must run at default init (strict_mode == false)",
        )

    def test_motion_move_driver_failure_default_mode_returns_hardware_fault_nack(self) -> None:
        """Req 1/3/4: real C bridge test -- at default init (strict_mode == false),
        a failing gimbal driver yields sys.nack 500 HARDWARE_FAULT, echoes the
        original request_id, and carries no success data payload.

        Default mode is deliberately NOT switched to strict here.
        """
        self._assert_default_mode()
        self._register_failing_motion_driver(DESK_HW_ERR_STALL, DESK_HW_OK)

        resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-default-fault-move",
            "data": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 300},
        })

        # Req 3: never a success ack, never code:0, on a driver failure.
        self.assertNotEqual(resp.get("type"), "sys.ack")
        self.assertNotEqual(resp.get("code"), 0)
        # Req 1: sys.nack / 500 / HARDWARE_FAULT.
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 500)
        self.assertEqual(resp.get("error", {}).get("code"), "HARDWARE_FAULT")
        # Req 1: no success data payload.
        self.assertNotIn("data", resp, "a driver failure must not carry a success payload")
        # Req 4: original request_id echoed.
        self.assertEqual(resp.get("request_id"), "req-default-fault-move")

    def test_motion_stop_driver_failure_default_mode_returns_hardware_fault_nack(self) -> None:
        """Req 2/3/4: real C bridge test -- at default init (strict_mode == false),
        a failing stop driver yields sys.nack 500 HARDWARE_FAULT and must not
        report stopped:true. Default mode is NOT switched to strict here.
        """
        self._assert_default_mode()
        self._register_failing_motion_driver(DESK_HW_OK, DESK_HW_ERR_STALL)

        resp = self._process_cmd({
            "type": "ctrl.motion.stop",
            "request_id": "req-default-fault-stop",
            "data": {"emergency": True},
        })

        # Req 3: never a success ack, never code:0.
        self.assertNotEqual(resp.get("type"), "sys.ack")
        self.assertNotEqual(resp.get("code"), 0)
        # Req 2: sys.nack / 500 / HARDWARE_FAULT, no stopped:true.
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 500)
        self.assertEqual(resp.get("error", {}).get("code"), "HARDWARE_FAULT")
        self.assertIsNot(resp.get("data", {}).get("stopped"), True)
        self.assertNotIn("data", resp, "a driver failure must not carry a success payload")
        # Req 4: original request_id echoed.
        self.assertEqual(resp.get("request_id"), "req-default-fault-stop")

    def test_motion_driver_failure_default_mode_is_never_a_success_ack(self) -> None:
        """Req 3: real C bridge test -- neither fault path may return code:0 or a
        sys.ack in any form at default init (strict_mode == false). This is the
        exact shape the pre-926c82a default-mode defect produced (accepted ack).
        """
        self._assert_default_mode()
        self._register_failing_motion_driver(DESK_HW_ERR_STALL, DESK_HW_ERR_STALL)

        for request_id, cmd in (
            ("req-default-never-ack-move",
             {"type": "ctrl.motion.move", "data": {"pan_deg": 10.0, "tilt_deg": 100.0, "duration_ms": 200}}),
            ("req-default-never-ack-stop",
             {"type": "ctrl.motion.stop", "data": {"emergency": False}}),
        ):
            payload = dict(cmd)
            payload["request_id"] = request_id
            resp = self._process_cmd(payload)
            self.assertNotEqual(resp.get("type"), "sys.ack", f"{request_id}: must not be a success ack")
            self.assertNotEqual(resp.get("code"), 0, f"{request_id}: must never report code:0")
            self.assertIsNot(resp.get("data", {}).get("accepted"), True,
                             f"{request_id}: must not claim accepted")
            self.assertNotEqual(resp.get("data", {}).get("status"), "accepted",
                                f"{request_id}: must not claim accepted status")

    def test_motion_driver_failure_default_mode_preserves_request_id(self) -> None:
        """Req 4: real C bridge test -- the echoed request_id equals the one sent,
        across both default-mode fault paths and in the presence of an interleaved
        successful command (no cross-talk between responses).
        """
        self._assert_default_mode()
        self._register_failing_motion_driver(DESK_HW_ERR_STALL, DESK_HW_ERR_STALL)

        # A healthy command first: proves correlation is not simply "last id wins".
        ok = self._process_cmd({"type": "sys.sdk.ready.get", "request_id": "req-default-ok-0"})
        self.assertEqual(ok.get("request_id"), "req-default-ok-0")

        move_fault = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-default-correlate-move",
            "data": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 300},
        })
        self.assertEqual(move_fault.get("request_id"), "req-default-correlate-move")
        self.assertEqual(move_fault.get("type"), "sys.nack")

        stop_fault = self._process_cmd({
            "type": "ctrl.motion.stop",
            "request_id": "req-default-correlate-stop",
            "data": {"emergency": True},
        })
        self.assertEqual(stop_fault.get("request_id"), "req-default-correlate-stop")
        self.assertEqual(stop_fault.get("type"), "sys.nack")

    def test_motion_resource_reacquirable_after_default_mode_fault(self) -> None:
        """Req 5: real C bridge test -- after a driver fault at default init, the
        gimbal is released (arbiter owner back to NONE) and a subsequent motion
        command through the same bridge succeeds with sys.ack code:0.

        The arbiter-owner assertion is the discriminating check: a leaked
        SDK_REMOTE lock would still let a same-owner re-acquire succeed, so the
        bridge-level ack alone could not distinguish released from leaked.
        """
        self._assert_default_mode()
        self._register_failing_motion_driver(DESK_HW_ERR_STALL, DESK_HW_OK)

        fault = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-default-leak-fault",
            "data": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 300},
        })
        self.assertEqual(fault.get("type"), "sys.nack")
        self.assertEqual(fault.get("code"), 500)

        # Direct C read of the arbiter state: the fault must not leak the lock.
        self.assertEqual(
            self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL), DESK_OWNER_NONE,
            "the gimbal lock was leaked after a driver fault",
        )

        # Re-register the ops table with a healthy driver and drive the same
        # bridge again: the resource is usable, not wedged.
        self._register_motion_driver(DESK_HW_OK, DESK_HW_OK)
        recovered = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-default-leak-recovered",
            "data": {"pan_deg": 45.0, "tilt_deg": 115.0, "duration_ms": 200},
        })
        self.assertEqual(recovered.get("type"), "sys.ack")
        self.assertEqual(recovered.get("code"), 0)
        self.assertEqual(recovered.get("request_id"), "req-default-leak-recovered")
        self.assertEqual(recovered.get("data", {}).get("status"), "accepted")
        self.assertIs(recovered.get("data", {}).get("state_confirmed"), False)

    def test_motion_success_ack_is_accepted_not_succeeded_default_mode(self) -> None:
        """Req 6 (CR-002 boundary): real C bridge test -- a succeeding move at
        default init (strict_mode == false) yields sys.ack code:0 with
        data.status == "accepted", data.accepted == true, and data.state_confirmed == false,
        and must never claim succeeded/completed.
        """
        self._assert_default_mode()

        resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-default-accepted-only",
            "data": {"pan_deg": 60.0, "tilt_deg": 110.0, "duration_ms": 200},
        })
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)
        self.assertEqual(resp.get("data", {}).get("status"), "accepted")
        self.assertTrue(resp.get("data", {}).get("accepted"))
        self.assertIs(resp.get("data", {}).get("state_confirmed"), False)

        raw = json.dumps(resp)
        self.assertNotIn("succeeded", raw, "a device ack must not claim physical completion")
        self.assertNotIn("completed", raw, "a device ack must not claim physical completion")
        self.assertNotIn('"state_confirmed": true', raw, "a device ack must not claim state confirmed")
        self.assertIn('"state_confirmed": false', raw, "a device ack must explicitly mark state unconfirmed")

    def test_cr002_motion_ack_semantics_contract(self) -> None:
        """CR-002 & R3a adjudication: motion move and stop ACK-only semantics.
        sys.ack must contain status: "accepted", accepted: True, state_confirmed: False.
        Under no circumstance may it claim physical completion (succeeded/completed/state_confirmed: true).
        """
        # Test ctrl.motion.move
        move_resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-cr002-move-contract",
            "data": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 300},
        })
        self.assertEqual(move_resp.get("type"), "sys.ack")
        self.assertEqual(move_resp.get("code"), 0)
        self.assertEqual(move_resp.get("request_id"), "req-cr002-move-contract")
        move_data = move_resp.get("data", {})
        self.assertEqual(move_data.get("status"), "accepted")
        self.assertIs(move_data.get("accepted"), True)
        self.assertIs(move_data.get("state_confirmed"), False)
        self.assertAlmostEqual(move_data.get("pan_deg"), 90.0, places=1)
        self.assertAlmostEqual(move_data.get("tilt_deg"), 120.0, places=1)
        self.assertEqual(move_data.get("duration_ms"), 300)

        # Test ctrl.motion.stop
        stop_resp = self._process_cmd({
            "type": "ctrl.motion.stop",
            "request_id": "req-cr002-stop-contract",
            "data": {"emergency": True},
        })
        self.assertEqual(stop_resp.get("type"), "sys.ack")
        self.assertEqual(stop_resp.get("code"), 0)
        self.assertEqual(stop_resp.get("request_id"), "req-cr002-stop-contract")
        stop_data = stop_resp.get("data", {})
        self.assertEqual(stop_data.get("status"), "accepted")
        self.assertIs(stop_data.get("accepted"), True)
        self.assertIs(stop_data.get("state_confirmed"), False)
        self.assertIs(stop_data.get("stopped"), True)

        # Wire payload negative checks
        for resp in (move_resp, stop_resp):
            wire = json.dumps(resp)
            self.assertNotIn("succeeded", wire)
            self.assertNotIn("completed", wire)
            self.assertNotIn('"state_confirmed": true', wire)
            self.assertIn('"state_confirmed": false', wire)

    # ------------------------------------------------------------------
    # WP2 -- request_id correlation
    # ------------------------------------------------------------------

    def test_request_id_echoed_on_every_response(self) -> None:
        """interfaces.md 6.2: every response echoes the request_id of its request."""
        test_jpeg = b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00CORRELATE\xFF\xD9"
        self.lib.desk_hardware_camera_inject_frame(test_jpeg, len(test_jpeg), 12345)

        cases = [
            ("motion.move", "sys.ack", {"type": "ctrl.motion.move",
                                        "data": {"pan_deg": 30.0, "tilt_deg": 120.0, "duration_ms": 200}}),
            ("motion.stop", "sys.ack", {"type": "ctrl.motion.stop", "data": {"emergency": True}}),
            ("motion.action.play", "sys.ack", {"type": "ctrl.motion.action.play", "data": {}}),
            ("camera.acquire", "sys.ack", {"type": "ctrl.camera.acquire",
                                           "data": {"client_id": "corr-client", "lease_duration_ms": 2000}}),
            ("camera.capture", "sys.ack", {"type": "ctrl.camera.capture", "data": {}}),
            ("camera.release", "sys.ack", {"type": "ctrl.camera.release",
                                           "data": {"client_id": "corr-client"}}),
            ("microphone.open", "sys.ack", {"type": "ctrl.microphone.open",
                                            "data": {"sample_rate_hz": 16000}}),
            ("microphone.close", "sys.ack", {"type": "ctrl.microphone.close", "data": {}}),
            ("expression.play", "sys.ack", {"type": "ctrl.expression.play",
                                            "data": {"preset": "idle", "text": "correlate"}}),
            ("expression.audio_follow", "sys.ack", {"type": "ctrl.expression.audio_follow",
                                                    "data": {"enabled": True}}),
            ("sys.sdk.ready.get", "sys.ack", {"type": "sys.sdk.ready.get", "data": {}}),
            ("sys.ping", "sys.pong", {"type": "sys.ping", "data": {}}),
            ("unknown.operation", "sys.nack", {"type": "totally.unknown.operation", "data": {}}),
        ]

        for idx, (name, expected_type, cmd) in enumerate(cases):
            req_id = f"req-correlate-{idx:02d}"
            payload = dict(cmd)
            payload["request_id"] = req_id
            resp = self._process_cmd(payload)
            self.assertEqual(resp.get("type"), expected_type, f"{name}: unexpected response type")
            self.assertEqual(resp.get("request_id"), req_id, f"{name}: request_id was not echoed")

    def test_hello_auth_failure_nack_echoes_request_id(self) -> None:
        """The 401 nack must correlate to its request_id like any other response."""
        self.lib.desk_sdk_bridge_set_auth_required(True)
        self.addCleanup(self.lib.desk_sdk_bridge_set_auth_required, False)

        resp = self._process_cmd({
            "type": "sys.client.hello",
            "request_id": "req-hello-401",
            "data": {"role": "desktop", "token": "bogus"},
        })
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 401)
        self.assertEqual(resp.get("request_id"), "req-hello-401")

    def test_arbiter_busy_nack_echoes_request_id(self) -> None:
        """The 409 RESOURCE_BUSY nack must correlate to its request_id."""
        rc = self.lib.desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE, 0)
        self.assertEqual(rc, DESK_ARBITER_OK)
        self.addCleanup(self.lib.desk_arbiter_release, DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE)

        resp = self._process_cmd({
            "type": "ctrl.motion.move",
            "request_id": "req-busy-correlate",
            "data": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 200},
        })
        self.assertEqual(resp.get("type"), "sys.nack")
        self.assertEqual(resp.get("code"), 409)
        self.assertEqual(resp.get("error", {}).get("code"), "RESOURCE_BUSY")
        self.assertEqual(resp.get("request_id"), "req-busy-correlate")

    # ------------------------------------------------------------------
    # WP3 -- TCP stream framing
    # ------------------------------------------------------------------

    def test_tcp_half_packet_reassembly(self) -> None:
        """A frame split across two reads must be reassembled, not dropped."""
        sock = self._open_tcp_session()
        sock.sendall(b'{"type":"sys.pi')
        self._pump(2)
        self.assertEqual(self._recv_lines(sock, timeout=0.3), [],
                         "an incomplete frame must not be processed")

        sock.sendall(b'ng"}\n')
        self._pump(2)
        lines = self._recv_lines(sock)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["type"], "sys.pong")

    def test_tcp_message_split_across_many_reads(self) -> None:
        """A single message dribbled byte-group by byte-group must still parse."""
        import time

        sock = self._open_tcp_session()
        msg = json.dumps({"type": "sys.sdk.ready.get", "request_id": "req-split-01"}) + "\n"
        for i in range(0, len(msg), 7):
            sock.sendall(msg[i:i + 7].encode("utf-8"))
            time.sleep(0.005)
            self._pump(2, timeout_ms=10)

        lines = self._recv_lines(sock)
        self.assertEqual(len(lines), 1, f"expected exactly one response, got {lines}")
        parsed = json.loads(lines[0])
        self.assertEqual(parsed["type"], "sys.ack")
        self.assertEqual(parsed["request_id"], "req-split-01")

    def test_tcp_multiple_messages_in_single_read(self) -> None:
        """Two frames arriving in one TCP segment must both be processed."""
        sock = self._open_tcp_session()
        payload = (json.dumps({"type": "sys.ping"}) + "\n" +
                   json.dumps({"type": "sys.sdk.ready.get"}) + "\n")
        sock.sendall(payload.encode("utf-8"))
        self._pump(3)

        lines = self._recv_lines(sock)
        self.assertEqual(len(lines), 2, f"expected two responses, got {lines}")
        self.assertEqual(json.loads(lines[0])["type"], "sys.pong")
        self.assertEqual(json.loads(lines[1])["type"], "sys.ack")

    def test_tcp_oversized_frame_discarded_and_resynced(self) -> None:
        """An over-long frame is dropped without corrupting the following message."""
        sock = self._open_tcp_session()

        oversized = b"{" + b"x" * (DESK_SDK_MAX_FRAME_LEN + 64)
        sock.sendall(oversized + b"\n")
        self._pump(24, timeout_ms=20)  # drain the whole oversized frame
        self.assertEqual(self._recv_lines(sock, timeout=0.3), [],
                         "an oversized frame must not be answered")

        sock.sendall((json.dumps({"type": "sys.ping"}) + "\n").encode("utf-8"))
        self._pump(3)
        lines = self._recv_lines(sock)
        self.assertEqual(len(lines), 1, f"expected only the post-overflow response, got {lines}")
        self.assertEqual(json.loads(lines[0])["type"], "sys.pong")

    def test_tcp_max_length_frame_still_accepted(self) -> None:
        """A frame exactly at the limit is accepted (only longer frames are dropped)."""
        sock = self._open_tcp_session()
        # Pad a valid command with trailing spaces to reach exactly the limit.
        base = json.dumps({"type": "sys.ping"})
        padded = base + " " * (DESK_SDK_MAX_FRAME_LEN - len(base))
        self.assertEqual(len(padded), DESK_SDK_MAX_FRAME_LEN)
        sock.sendall(padded.encode("utf-8") + b"\n")
        self._pump(12, timeout_ms=20)

        lines = self._recv_lines(sock)
        self.assertEqual(len(lines), 1, f"max-length frame must be accepted, got {lines}")
        self.assertEqual(json.loads(lines[0])["type"], "sys.pong")

    def test_tcp_garbage_input_does_not_break_link(self) -> None:
        """Non-JSON lines are dropped and the connection stays usable."""
        sock = self._open_tcp_session()
        sock.sendall(b"this is definitely not json\n")
        self._pump(2)
        self.assertEqual(self._recv_lines(sock, timeout=0.3), [])

        sock.sendall((json.dumps({"type": "sys.ping"}) + "\n").encode("utf-8"))
        self._pump(2)
        lines = self._recv_lines(sock)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["type"], "sys.pong")

    def test_tcp_crlf_and_leading_whitespace_tolerated(self) -> None:
        """CRLF framing and leading whitespace do not break frame detection."""
        sock = self._open_tcp_session()
        sock.sendall(b"   \r\n" + b'{"type":"sys.ping"}\r\n')
        self._pump(3)

        lines = self._recv_lines(sock)
        self.assertEqual(len(lines), 1, f"expected exactly one response, got {lines}")
        self.assertEqual(json.loads(lines[0])["type"], "sys.pong")

    def test_tcp_residual_buffer_cleared_on_disconnect(self) -> None:
        """Bytes left over from a dropped connection must not leak into the next one."""
        import socket

        sock1 = self._open_tcp_session()
        sock1.sendall(b'{"type":"sys.pi')  # partial frame, never terminated
        self._pump(2)
        sock1.close()
        self._pump(3)  # server observes EOF and must discard the partial frame

        port = self.lib.desk_sdk_socket_server_get_port()
        sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock2.connect(("127.0.0.1", port))
        sock2.settimeout(0.4)
        self.addCleanup(sock2.close)
        self._pump(2)

        # The abandoned tail must NOT complete a frame on the new connection.
        sock2.sendall(b'ng"}\n')
        self._pump(2)
        self.assertEqual(self._recv_lines(sock2, timeout=0.3), [],
                         "residual bytes leaked across a reconnect")

        sock2.sendall((json.dumps({"type": "sys.ping"}) + "\n").encode("utf-8"))
        self._pump(2)
        lines = self._recv_lines(sock2)
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0])["type"], "sys.pong")

    # ------------------------------------------------------------------
    # WP4 -- pairing code validation (fail-closed)
    # ------------------------------------------------------------------

    def test_pairing_rejected_when_no_code_provisioned(self) -> None:
        """Fail-closed: without a provisioned code no pairing is ever granted."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(None), 0)
        self.assertFalse(self.lib.desk_sdk_bridge_is_pairing_code_configured())

        rc, reply = self._process_pairing(self._official_pair_request("123456"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(reply, "", "no pair.accept may be emitted when unprovisioned")

    def test_pairing_rejected_on_wrong_code(self) -> None:
        """A mismatching pairing code must not yield a session token."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"654321"), 0)
        rc, reply = self._process_pairing(self._official_pair_request("123456"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(reply, "")

    def test_pairing_rejected_on_missing_code(self) -> None:
        """A pair.request without a pairing_code must be rejected."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        req = json.loads(self._official_pair_request("123456").decode("utf-8"))
        del req["pairing_code"]
        rc, reply = self._process_pairing(json.dumps(req).encode("utf-8"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(reply, "")

    def test_pairing_rejected_on_malformed_code(self) -> None:
        """Only ^[0-9]{6}$ is acceptable, even when it is a prefix/suffix of the real code."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        for bad in ["12345", "1234567", "12345a", "abcdef", "", "123 56", "-12345"]:
            with self.subTest(code=bad):
                rc, reply = self._process_pairing(self._official_pair_request(bad))
                self.assertNotEqual(rc, 0, f"{bad!r} must be rejected")
                self.assertEqual(reply, "")

    def test_pairing_rejected_after_code_cleared(self) -> None:
        """Clearing the provisioned code returns the device to fail-closed."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        rc_ok, reply_ok = self._process_pairing(self._official_pair_request("123456"))
        self.assertEqual(rc_ok, 0)
        self.assertEqual(json.loads(reply_ok)["type"], "pair.accept")

        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(None), 0)
        self.assertFalse(self.lib.desk_sdk_bridge_is_pairing_code_configured())
        rc, reply = self._process_pairing(self._official_pair_request("123456"))
        self.assertNotEqual(rc, 0)
        self.assertEqual(reply, "")

    def test_set_pairing_code_rejects_malformed_provisioning_input(self) -> None:
        """Malformed provisioning input is refused and does not arm pairing."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(None), 0)
        for bad in [b"abc123", b"12345", b"1234567", b"", b"12 456"]:
            with self.subTest(code=bad):
                if bad == b"":
                    self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(bad), 0)
                else:
                    self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(bad), -1)
                self.assertFalse(self.lib.desk_sdk_bridge_is_pairing_code_configured())

    def test_pairing_logs_are_redacted(self) -> None:
        """pairing_code and session_token must never appear in clear text in logs."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)

        with capture_c_stdout() as tmp:
            rc, reply = self._process_pairing(self._official_pair_request("123456"))
            self.assertEqual(rc, 0)
            token = json.loads(reply)["session_token"]

        logged = read_captured(tmp)
        self.assertIn("[redacted", logged, "expected a redacted diagnostic log line")
        self.assertNotIn("123456", logged, "pairing_code must never be logged in clear text")
        self.assertNotIn(token, logged, "session_token must never be logged in clear text")

    def test_pairing_rejection_logs_are_redacted(self) -> None:
        """Rejection diagnostics must redact the supplied pairing code."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"654321"), 0)

        with capture_c_stdout() as tmp:
            rc, reply = self._process_pairing(self._official_pair_request("123456"))
            self.assertNotEqual(rc, 0)
            self.assertEqual(reply, "")

        logged = read_captured(tmp)
        self.assertNotIn("123456", logged, "supplied pairing_code must never be logged")
        self.assertNotIn("654321", logged, "provisioned pairing_code must never be logged")

    # ------------------------------------------------------------------
    # R9 -- set_pairing_token() sets credentials only, never the paired state
    # ------------------------------------------------------------------

    def test_set_pairing_token_does_not_imply_paired_state(self) -> None:
        """R9 (interfaces.md 6.6): provisioning a token must not mark the device paired.

        `s_paired` is only set by a validated pairing handshake. The paired state
        is not directly readable, so this asserts its wire-observable consequence:
        with a credential provisioned but no validated pairing and no explicit
        auth requirement, the handshake must not treat the client as a paired
        session. (Before R9 the `s_paired` branch fired here and a token-less
        hello was rejected with 401.)
        """
        self.assertEqual(
            self.lib.desk_sdk_bridge_set_pairing_token(VALID_SESSION_TOKEN.encode("utf-8")), 0)
        self.lib.desk_sdk_bridge_set_auth_required(False)
        # Not paired: the paired-session branch cannot fire for a token-less hello.
        resp = self._process_cmd({"type": "sys.client.hello", "data": {"role": "desktop"}})
        self.assertEqual(resp.get("type"), "sys.ack",
                         "a provisioned credential must not put the device in the paired state")
        self.assertEqual(resp.get("code"), 0)

        # The credential is nevertheless armed: a wrong token is still rejected.
        resp_bad = self._process_cmd({
            "type": "sys.client.hello",
            "data": {"role": "desktop", "token": "wrong-token"},
        })
        self.assertEqual(resp_bad.get("type"), "sys.nack")
        self.assertEqual(resp_bad.get("code"), 401)

        # ... and the provisioned token itself is accepted.
        resp_ok = self._process_cmd({
            "type": "sys.client.hello",
            "data": {"role": "desktop", "token": VALID_SESSION_TOKEN},
        })
        self.assertEqual(resp_ok.get("type"), "sys.ack")
        self.assertEqual(resp_ok.get("code"), 0)

    def test_set_pairing_token_does_not_grant_pairing_without_code_validation(self) -> None:
        """R9: provisioning credentials cannot substitute for a validated pairing handshake."""
        self.assertEqual(
            self.lib.desk_sdk_bridge_set_pairing_token(VALID_SESSION_TOKEN.encode("utf-8")), 0)
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(None), 0)
        self.assertFalse(self.lib.desk_sdk_bridge_is_pairing_code_configured())

        rc, reply = self._process_pairing(self._official_pair_request("123456"))
        self.assertNotEqual(rc, 0, "a provisioned token must not bypass pairing-code validation")
        self.assertEqual(reply, "", "no pair.accept / no session_token may be granted")

        # The legitimate validated path is unaffected by R9. A pre-provisioned
        # credential is reused verbatim (it is not re-minted), so this asserts
        # the provisioned value is handed out only once validation passes.
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        rc_ok, reply_ok = self._process_pairing(self._official_pair_request("123456"))
        self.assertEqual(rc_ok, 0)
        parsed = json.loads(reply_ok)
        self.assertEqual(parsed["type"], "pair.accept")
        self.assertEqual(parsed["session_token"], VALID_SESSION_TOKEN)

    # ------------------------------------------------------------------
    # R11 -- set_pairing_token() only accepts ^[0-9a-f]{64}$
    # ------------------------------------------------------------------

    def test_set_pairing_token_rejects_nonconforming_credentials(self) -> None:
        """R11: a credential that cannot legally appear in pair.accept must be refused.

        `pair.accept.session_token` is required by the official contract to match
        `^[0-9a-f]{64}$`. Since a provisioned credential is echoed verbatim into
        that field, any other value must be rejected at provisioning time rather
        than arming the device to emit a contract-violating message.
        """
        valid = VALID_SESSION_TOKEN.encode("utf-8")
        bad_inputs = [
            b"short",                            # too short
            b"",                                 # empty
            b"0123456789abcdef" * 3,             # 48 chars
            valid + b"a",                        # 65 chars
            valid[:-1],                          # 63 chars
            b"0123456789ABCDEF" * 4,             # 64 chars, uppercase
            b"z" * 64,                           # 64 chars, non-hex
            valid[:-1] + b"-",                   # 64 chars, trailing non-hex
            b"0x" + valid[:-2],                  # 64 chars, non-hex prefix
        ]
        for bad in bad_inputs:
            with self.subTest(token=bad[:20]):
                self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_token(bad), -1,
                                 "non-conforming credential must be refused")

        # None of the rejected inputs armed the device: with no credential
        # installed, a hello carrying an arbitrary token is not a mismatch.
        resp = self._process_cmd({
            "type": "sys.client.hello",
            "data": {"role": "desktop", "token": "whatever-token"},
        })
        self.assertEqual(resp.get("type"), "sys.ack",
                         "a refused credential must not arm the device")

        # A conforming value is accepted and does arm the device.
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_token(valid), 0)
        resp_bad = self._process_cmd({
            "type": "sys.client.hello",
            "data": {"role": "desktop", "token": "wrong-token"},
        })
        self.assertEqual(resp_bad.get("type"), "sys.nack")
        self.assertEqual(resp_bad.get("code"), 401)
        resp_ok = self._process_cmd({
            "type": "sys.client.hello",
            "data": {"role": "desktop", "token": VALID_SESSION_TOKEN},
        })
        self.assertEqual(resp_ok.get("type"), "sys.ack")

        # A later rejected call must not clobber the armed credential.
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_token(b"short"), -1)
        resp_still = self._process_cmd({
            "type": "sys.client.hello",
            "data": {"role": "desktop", "token": VALID_SESSION_TOKEN},
        })
        self.assertEqual(resp_still.get("type"), "sys.ack")

    def test_provisioned_credential_yields_contract_valid_pair_accept(self) -> None:
        """R11: the echoed pair.accept.session_token must satisfy ^[0-9a-f]{64}$."""
        self.assertEqual(
            self.lib.desk_sdk_bridge_set_pairing_token(VALID_SESSION_TOKEN.encode("utf-8")), 0)
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)

        rc, reply = self._process_pairing(self._official_pair_request("123456"))
        self.assertEqual(rc, 0)
        token = json.loads(reply)["session_token"]
        self.assertEqual(len(token), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in token))




