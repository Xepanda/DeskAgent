"""
Contract and protocol tests for Desk Agent R1 WatcheRobot SDK Bridge (C shared library).
Verifies handshake negotiation, argument parsing, resource arbitration enforcement,
WSPK binary framing, input events, and bidirectional tool dispatch.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import unittest

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

        cls.lib.desk_sdk_bridge_process_pairing_udp.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_pairing_udp.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

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

        cls.lib.desk_sdk_socket_server_poll.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_poll.argtypes = [ctypes.c_int]

        cls.lib.desk_sdk_socket_server_stop.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_stop.argtypes = []

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_hardware_init()
        self.lib.desk_sdk_bridge_init()

    def _process_cmd(self, cmd_dict: dict) -> dict:
        cmd_str = json.dumps(cmd_dict).encode("utf-8")
        out_buf = ctypes.create_string_buffer(4096)
        rc = self.lib.desk_sdk_bridge_process_command(cmd_str, out_buf, 4096)
        self.assertEqual(rc, 0, f"desk_sdk_bridge_process_command returned error code {rc}")
        return json.loads(out_buf.value.decode("utf-8"))

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
        expected_caps = ["inputs", "motion", "camera", "audio.stream", "expression.audio_follow.v1"]
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
        """Verifies UDP pair.request handling and pair.accept generation per watcher_lan_pairing_v1."""
        pair_req = json.dumps({
            "type": "pair.request",
            "protocol": "watcher-lan-pairing",
            "version": "1.0",
            "request_id": "21a9dbf05ea3443480e62076f79a3b12",
            "daemon_instance_id": "f730f29e670c49f7a3320c4314eb9805",
            "pairing_code": "123456",
            "target_mode": "python_sdk",
            "websocket_port": 8765,
        })
        out_buf = ctypes.create_string_buffer(1024)
        rc = self.lib.desk_sdk_bridge_process_pairing_udp(pair_req.encode("utf-8"), out_buf, 1024)
        self.assertEqual(rc, 0)

        rsp = json.loads(out_buf.value.decode("utf-8"))
        self.assertEqual(rsp["type"], "pair.accept")
        self.assertEqual(rsp["protocol"], "watcher-lan-pairing")
        self.assertEqual(rsp["version"], "1.0")
        self.assertEqual(rsp["request_id"], "21a9dbf05ea3443480e62076f79a3b12")
        self.assertEqual(rsp["daemon_instance_id"], "f730f29e670c49f7a3320c4314eb9805")
        self.assertEqual(rsp["target_mode"], "python_sdk")
        self.assertEqual(len(rsp["session_token"]), 64)

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

        # Send sys.ping over TCP
        ping_msg = json.dumps({"type": "sys.ping"}).encode("utf-8")
        client_sock.sendall(ping_msg)

        # Poll server to process command
        self.lib.desk_sdk_socket_server_poll(100)

        # Read response from socket
        resp_data = client_sock.recv(1024).decode("utf-8").strip()
        resp_json = json.loads(resp_data)
        self.assertEqual(resp_json["type"], "sys.pong")

        # Send motion.move over TCP
        move_msg = json.dumps({"type": "motion.move", "pan_deg": 110.0, "tilt_deg": 115.0, "duration_ms": 300}).encode("utf-8")
        client_sock.sendall(move_msg)
        self.lib.desk_sdk_socket_server_poll(100)

        resp_move = client_sock.recv(1024).decode("utf-8").strip()
        resp_move_json = json.loads(resp_move)
        self.assertEqual(resp_move_json["type"], "sys.ack")
        self.assertEqual(resp_move_json["data"]["status"], "succeeded")
        self.assertEqual(resp_move_json["data"]["pan_deg"], 110.0)

        # Trigger event and verify socket receives it
        self.lib.desk_sdk_bridge_send_event(b"device.status", b'{"battery": 95}')
        event_raw = client_sock.recv(1024).decode("utf-8").strip()
        event_json = json.loads(event_raw)
        self.assertEqual(event_json["type"], "device.status")
        self.assertEqual(event_json["payload"]["battery"], 95)

        client_sock.close()
        self.lib.desk_sdk_socket_server_stop()

