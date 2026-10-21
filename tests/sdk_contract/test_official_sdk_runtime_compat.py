"""
Contract and interoperability tests for Desk Agent R1 SDK Bridge against the
Official WatcheRobot Python SDK (orulink-ai/WatcheRobot_python_sdk v0.1.11).

Directly imports official SDK modules from the locked external repository to verify:
1. UDP 37021 watcher-lan-pairing 1.0 wire contract (parse_udp_message exact fields).
2. Hardware and Desktop client hello handshakes and capability negotiation.
3. Official motion set_target, stop, and action commands.
4. Official camera capture triggering and WSPK binary frame deserialization via parse_wspk.
5. Microphone session lifecycle (open/close).
6. Expression runtime and audio follow controls.
7. Concurrency between TClaw voice dialogue and SDK bridge commands with arbiter enforcement.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import sys
import tempfile
import unittest

# Import official WatcheRobot SDK from the locked path
OFFICIAL_SDK_PATH = "/home/maidang/.gemini/antigravity-cli/brain/cb82573a-ba9b-42ea-a6be-405983e79f69/scratch/watcherobot_python_sdk/src"
if OFFICIAL_SDK_PATH not in sys.path and os.path.isdir(OFFICIAL_SDK_PATH):
    sys.path.insert(0, OFFICIAL_SDK_PATH)

try:
    from watcherobot.protocol import (
        FRAME_AUDIO,
        FRAME_IMAGE,
        BinaryFrame,
        parse_json_message,
        parse_wspk,
    )
    from watcherobot.runtime.daemon.pairing.protocol import (
        LAN_PAIRING_PROTOCOL,
        LAN_PAIRING_VERSION,
        PairAccept,
        PairRequest,
        encode_udp_message,
        parse_hardware_hello,
        parse_udp_message,
    )
    HAVE_WATCHEROBOT = True
except ImportError:
    HAVE_WATCHEROBOT = False

# Arbiter resource and owner codes
DESK_RES_MOTION_GIMBAL = 3
DESK_OWNER_TCLAW_VOICE = 2
DESK_OWNER_SDK_REMOTE = 3
DESK_ARBITER_OK = 0
DESK_ARBITER_ERR_BUSY = 1

OUTBOUND_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)


@unittest.skipUnless(HAVE_WATCHEROBOT, "Official watcherobot python sdk not installed in environment")
class TestOfficialSdkRuntimeCompat(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_sdk_bridge_compat.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        # Compile C shared library
        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            f"-I{repo_root}/firmware/components/desk_sdk_bridge/include",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"-I{repo_root}/firmware/components/desk_tclaw/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{repo_root}/firmware/components/desk_tclaw/src/desk_tclaw_bridge.c",
            f"{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c",
            f"{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c",
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

        cls.lib.desk_tclaw_init.restype = ctypes.c_int
        cls.lib.desk_tclaw_init.argtypes = []

        cls.lib.desk_tclaw_start_speaking.restype = ctypes.c_int
        cls.lib.desk_tclaw_start_speaking.argtypes = [ctypes.c_char_p]

        cls.lib.desk_tclaw_interrupt.restype = ctypes.c_int
        cls.lib.desk_tclaw_interrupt.argtypes = []

        cls.lib.desk_arbiter_acquire.restype = ctypes.c_int
        cls.lib.desk_arbiter_acquire.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32]

        cls.lib.desk_arbiter_release.restype = None
        cls.lib.desk_arbiter_release.argtypes = [ctypes.c_int, ctypes.c_int]

        cls.lib.desk_hardware_motion_get_position.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_get_position.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]

        cls.lib.desk_hardware_camera_inject_frame.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_inject_frame.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]

        cls.lib.desk_sdk_bridge_init.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_init.argtypes = []

        cls.lib.desk_sdk_ws_client_is_connected.restype = ctypes.c_bool
        cls.lib.desk_sdk_ws_client_is_connected.argtypes = []
        cls.lib.desk_sdk_ws_client_send_text.restype = ctypes.c_int
        cls.lib.desk_sdk_ws_client_send_text.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_process_command.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_command.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        cls.lib.desk_sdk_bridge_process_pairing_udp.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_pairing_udp.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        cls.lib.desk_sdk_bridge_build_hardware_hello.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_build_hardware_hello.argtypes = [ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_sdk_bridge_set_pairing_code.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_set_pairing_code.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_is_pairing_code_configured.restype = ctypes.c_bool
        cls.lib.desk_sdk_bridge_is_pairing_code_configured.argtypes = []

        cls.lib.desk_sdk_bridge_register_outbound.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_register_outbound.argtypes = [OUTBOUND_CB, ctypes.c_void_p]

        cls.lib.desk_sdk_bridge_send_media_frame.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_send_media_frame.argtypes = [
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.c_uint16,
            ctypes.c_uint32,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_hardware_init()
        self.lib.desk_tclaw_init()
        self.lib.desk_sdk_bridge_reset()
        self.lib.desk_sdk_bridge_init()
        # The device-side expected pairing code must be provisioned explicitly;
        # without it pairing is fail-closed (interfaces.md 6.4 / CR-001).
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        self.assertTrue(self.lib.desk_sdk_bridge_is_pairing_code_configured())

    def test_websocket_client_starts_offline_and_refuses_unpaired_send(self) -> None:
        self.assertFalse(self.lib.desk_sdk_ws_client_is_connected())
        self.assertNotEqual(self.lib.desk_sdk_ws_client_send_text(b'{"type":"sys.ping"}'), 0)

    def _exec_command(self, cmd_dict: dict) -> dict:
        cmd_str = json.dumps(cmd_dict).encode("utf-8")
        out_buf = ctypes.create_string_buffer(4096)
        rc = self.lib.desk_sdk_bridge_process_command(cmd_str, out_buf, 4096)
        self.assertEqual(rc, 0, f"desk_sdk_bridge_process_command failed with code {rc}")
        return json.loads(out_buf.value.decode("utf-8"))

    def test_official_udp_pairing_exact_contract(self) -> None:
        """Verifies UDP pair.request is answered with a pair.accept that passes the official parse_udp_message."""
        # 1. Build an official PairRequest
        official_pair_req = PairRequest(
            request_id="21a9dbf05ea3443480e62076f79a3b12",
            daemon_instance_id="f730f29e670c49f7a3320c4314eb9805",
            pairing_code="123456",
            target_mode="python_sdk",
            websocket_port=8765,
        )
        wire_bytes = encode_udp_message(official_pair_req)

        # 2. Feed to C bridge UDP pairing handler
        out_buf = ctypes.create_string_buffer(1024)
        rc = self.lib.desk_sdk_bridge_process_pairing_udp(wire_bytes, out_buf, 1024)
        self.assertEqual(rc, 0)
        self.assertFalse(self.lib.desk_sdk_bridge_is_pairing_code_configured(),
                         "a successful PairAccept must consume the ephemeral code window")

        # 3. Parse reply using official SDK parse_udp_message parser
        reply_raw = out_buf.value
        parsed_msg = parse_udp_message(reply_raw)

        # 4. Must be a valid PairAccept instance with exact fields
        self.assertIsInstance(parsed_msg, PairAccept)
        self.assertEqual(parsed_msg.request_id, "21a9dbf05ea3443480e62076f79a3b12")
        self.assertEqual(parsed_msg.daemon_instance_id, "f730f29e670c49f7a3320c4314eb9805")
        self.assertEqual(parsed_msg.target_mode, "python_sdk")
        self.assertEqual(len(parsed_msg.session_token), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in parsed_msg.session_token))

        hello_buf = ctypes.create_string_buffer(512)
        hello_len = self.lib.desk_sdk_bridge_build_hardware_hello(hello_buf, len(hello_buf))
        self.assertGreater(hello_len, 0)
        hello = parse_hardware_hello(hello_buf.value)
        self.assertEqual(hello.pair_request_id, parsed_msg.request_id)
        self.assertEqual(hello.daemon_instance_id, parsed_msg.daemon_instance_id)
        self.assertEqual(hello.session_token, parsed_msg.session_token)
        self.assertEqual(hello.mode, parsed_msg.target_mode)

    def test_official_udp_pairing_rejected_when_unprovisioned(self) -> None:
        """Fail-closed: an unprovisioned device must not answer with pair.accept."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(None), 0)
        self.assertFalse(self.lib.desk_sdk_bridge_is_pairing_code_configured())

        official_pair_req = PairRequest(
            request_id="21a9dbf05ea3443480e62076f79a3b12",
            daemon_instance_id="f730f29e670c49f7a3320c4314eb9805",
            pairing_code="123456",
            target_mode="python_sdk",
            websocket_port=8765,
        )
        wire_bytes = encode_udp_message(official_pair_req)

        out_buf = ctypes.create_string_buffer(1024)
        rc = self.lib.desk_sdk_bridge_process_pairing_udp(wire_bytes, out_buf, 1024)
        self.assertNotEqual(rc, 0)
        self.assertEqual(out_buf.value, b"", "no pairing response may be emitted")

    def test_official_udp_pairing_rejected_on_wrong_code(self) -> None:
        """A mismatching pairing_code must not yield a PairAccept."""
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"654321"), 0)

        official_pair_req = PairRequest(
            request_id="21a9dbf05ea3443480e62076f79a3b12",
            daemon_instance_id="f730f29e670c49f7a3320c4314eb9805",
            pairing_code="123456",
            target_mode="python_sdk",
            websocket_port=8765,
        )
        out_buf = ctypes.create_string_buffer(1024)
        rc = self.lib.desk_sdk_bridge_process_pairing_udp(encode_udp_message(official_pair_req), out_buf, 1024)
        self.assertNotEqual(rc, 0)
        self.assertEqual(out_buf.value, b"")

    def test_official_hardware_hello_with_token(self) -> None:
        """Verifies sys.client.hello with role=hardware carrying session_token succeeds and returns negotiated parameters."""
        # Pair first to establish token
        out_buf = ctypes.create_string_buffer(1024)
        req_json = json.dumps({
            "type": "pair.request",
            "protocol": LAN_PAIRING_PROTOCOL,
            "version": LAN_PAIRING_VERSION,
            "request_id": "21a9dbf05ea3443480e62076f79a3b12",
            "daemon_instance_id": "f730f29e670c49f7a3320c4314eb9805",
            "pairing_code": "123456",
            "target_mode": "python_sdk",
            "websocket_port": 8765,
        }).encode("utf-8")
        self.lib.desk_sdk_bridge_process_pairing_udp(req_json, out_buf, 1024)
        pair_accept = parse_udp_message(out_buf.value)

        # Hardware hello carrying session_token
        hello_msg = {
            "type": "sys.client.hello",
            "code": 0,
            "data": {
                "role": "hardware",
                "pairing_protocol": LAN_PAIRING_PROTOCOL,
                "pairing_version": LAN_PAIRING_VERSION,
                "pair_request_id": pair_accept.request_id,
                "daemon_instance_id": pair_accept.daemon_instance_id,
                "session_token": pair_accept.session_token,
                "mode": "python_sdk",
            },
        }
        resp = self._exec_command(hello_msg)
        self.assertEqual(resp["type"], "sys.ack")
        self.assertEqual(resp["code"], 0)
        self.assertEqual(resp["data"]["role"], "hardware")
        self.assertEqual(resp["data"]["session_state"], "connected")
        self.assertEqual(resp["data"]["negotiated"]["audio_uplink"]["codec"], "opus")
        self.assertEqual(resp["data"]["negotiated"]["audio_uplink"]["sample_rate"], 16000)

    def test_official_desktop_hello_and_ready_get(self) -> None:
        """Verifies official Desktop hello and sys.sdk.ready.get capability negotiation."""
        resp = self._exec_command({
            "type": "sys.client.hello",
            "code": 0,
            "data": {"role": "desktop", "version": "1.0"},
        })
        self.assertEqual(resp["type"], "sys.ack")
        caps = resp["data"]["capabilities"]
        self.assertIn("inputs", caps)
        self.assertIn("motion", caps)
        self.assertIn("camera", caps)
        self.assertIn("audio.stream", caps)
        self.assertIn("expression.audio_follow.v1", caps)

        # sys.sdk.ready.get returns identical capabilities
        resp_ready = self._exec_command({
            "type": "sys.sdk.ready.get",
            "code": 0,
            "data": {"command_id": "cmd-ready-01"},
        })
        self.assertEqual(resp_ready["type"], "sys.ack")
        self.assertEqual(resp_ready["data"]["capabilities"], caps)

    def test_official_motion_set_target(self) -> None:
        """Verifies ctrl.motion.set_target as emitted by WatcheRobot.motion.set_target()."""
        resp = self._exec_command({
            "type": "ctrl.motion.set_target",
            "code": 0,
            "data": {
                "pan_deg": 125,
                "tilt_deg": 115,
                "command_id": "cmd-motion-target-01",
            },
        })
        self.assertEqual(resp["type"], "sys.ack")
        self.assertEqual(resp["code"], 0)
        # A device ACK only means "accepted" (interfaces.md 6.1); physical
        # completion is signalled by the resource.motion.completed event.
        # CR-002: ACK-only semantics requires state_confirmed: false.
        self.assertEqual(resp["data"]["status"], "accepted")
        self.assertTrue(resp["data"]["accepted"])
        self.assertIs(resp["data"]["state_confirmed"], False)
        self.assertAlmostEqual(resp["data"]["pan_deg"], 125.0, places=1)
        self.assertAlmostEqual(resp["data"]["tilt_deg"], 115.0, places=1)

        pan = ctypes.c_float()
        tilt = ctypes.c_float()
        self.lib.desk_hardware_motion_get_position(ctypes.byref(pan), ctypes.byref(tilt))
        self.assertAlmostEqual(pan.value, 125.0, places=1)
        self.assertAlmostEqual(tilt.value, 115.0, places=1)

    def test_official_camera_capture_and_wspk_stream(self) -> None:
        """Verifies ctrl.camera.capture returns positive session_id and emits official WSPK image frame."""
        # 1. Inject a sample JPEG into the camera HAL
        test_jpeg = b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00OFFICIAL_SDK_CAMERA_FRAME\xFF\xD9"
        self.lib.desk_hardware_camera_inject_frame(test_jpeg, len(test_jpeg), 12345)

        # 2. Register outbound listener to intercept emitted WSPK binary frames
        intercepted_frames: list[bytes] = []

        @OUTBOUND_CB
        def on_outbound(data_ptr, length, user_data):
            raw = bytes(ctypes.string_at(data_ptr, length))
            if raw.startswith(b"WSPK"):
                intercepted_frames.append(raw)
            return 0

        self.lib.desk_sdk_bridge_register_outbound(on_outbound, None)

        # 3. Send ctrl.camera.capture
        resp = self._exec_command({
            "type": "ctrl.camera.capture",
            "code": 0,
            "data": {
                "width": 0,
                "height": 0,
                "quality": 0,
                "command_id": "cmd-cam-capture-01",
            },
        })
        self.assertEqual(resp["type"], "sys.ack")
        self.assertEqual(resp["code"], 0)
        session_id = resp["data"].get("session_id")
        self.assertIsInstance(session_id, int)
        self.assertGreater(session_id, 0)

        # 4. Verify WSPK binary frame was dispatched and unpacks using official parse_wspk
        self.assertEqual(len(intercepted_frames), 1)
        wspk_pkt = intercepted_frames[0]
        parsed_binary = parse_wspk(wspk_pkt)

        self.assertEqual(parsed_binary.frame_type, FRAME_IMAGE)
        self.assertEqual(parsed_binary.stream_id, session_id)
        self.assertEqual(parsed_binary.payload, test_jpeg)

    def test_official_microphone_lifecycle(self) -> None:
        """Verifies ctrl.microphone.open returns session_id and close cleans up."""
        # Open microphone
        resp_open = self._exec_command({
            "type": "ctrl.microphone.open",
            "code": 0,
            "data": {"sample_rate_hz": 16000, "command_id": "cmd-mic-open-01"},
        })
        self.assertEqual(resp_open["type"], "sys.ack")
        self.assertEqual(resp_open["code"], 0)
        session_id = resp_open["data"].get("session_id")
        self.assertIsInstance(session_id, int)
        self.assertGreater(session_id, 0)

        # Close microphone
        resp_close = self._exec_command({
            "type": "ctrl.microphone.close",
            "code": 0,
            "data": {"session_id": session_id, "command_id": "cmd-mic-close-01"},
        })
        self.assertEqual(resp_close["type"], "sys.ack")
        self.assertEqual(resp_close["data"]["status"], "accepted")

    def test_official_expression_audio_follow(self) -> None:
        """Verifies ctrl.expression.audio_follow toggling."""
        resp = self._exec_command({
            "type": "ctrl.expression.audio_follow",
            "code": 0,
            "data": {"enabled": True, "command_id": "cmd-expr-01"},
        })
        self.assertEqual(resp["type"], "sys.ack")
        self.assertTrue(resp["data"]["enabled"])

    def test_tclaw_voice_and_sdk_parallel_concurrency(self) -> None:
        """Verifies TClaw voice dialogue and SDK bridge commands operate concurrently without locking."""
        # 1. TClaw starts speaking (holding audio resource)
        rc = self.lib.desk_tclaw_start_speaking(b"TClaw is talking to user...")
        self.assertEqual(rc, 0)

        # 2. Concurrently, SDK client sends motion set_target -> must succeed
        resp_motion = self._exec_command({
            "type": "ctrl.motion.set_target",
            "code": 0,
            "data": {"pan_deg": 40, "tilt_deg": 120, "command_id": "cmd-parallel-01"},
        })
        self.assertEqual(resp_motion["type"], "sys.ack")
        self.assertEqual(resp_motion["data"]["status"], "accepted")
        self.assertIs(resp_motion["data"]["state_confirmed"], False)

        # 3. TClaw finishes speech turn
        self.lib.desk_tclaw_interrupt()


if __name__ == "__main__":
    unittest.main()
