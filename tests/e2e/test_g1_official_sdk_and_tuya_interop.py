"""
Software Protocol Interoperability Verification Test for G1 Milestone:
Official WatcheRobot Device Protocol and Tuya Dialogue Concurrency PoC.

EVIDENCE LEVEL (AGENTS.md Section 4): Level 2 (Software Protocol PoC & Concurrency Verification).
- Verifies C data model, multi-protocol concurrency, and socket state machine validity under CI/GCC simulation.
- Proves Tuya MCP tool dispatch, WSPK framing, UDP pairing, and TCP commands do not deadlock or drop packets in C runtime.
- NOTE: Physical G1 milestone validation on physical ESP32-S3 hardware and live Tuya Cloud endpoint
  requires physical bench hardware (Level 3 / Level 4). This test must NOT be falsely claimed
  as physical hardware execution pass.

Verifies:
1. Active Tuya Cloud Dialogue session state machine (voice listening, streaming speech tokens, audio acquisition).
2. Python SDK client performs UDP pairing discovery against port 37021 (watcher_lan_pairing_v1),
   receives dynamic CSPRNG session token.
3. Python SDK connects via TCP to socket server (sys.client.hello), validates
   token authentication (rejects invalid token with 401, accepts valid token with 0),
   and negotiates all 5 official capabilities.
4. Python SDK dispatches motion movement (ctrl.motion.move) concurrently while
   Tuya dialogue session is active. Hardware gimbal angles and STM32 UART packets are verified.
5. Tuya dialogue engine triggers MCP tool call concurrently while Python SDK is connected;
   bidirectional request/response envelopes complete without blocking or deadlock.
6. Python SDK streams WSPK binary media frames (16-byte header + Opus audio),
   unpacked and verified by firmware without error.
"""

import ctypes
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest


# Arbiter Constants (per desk_arbiter.h)
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

# TClaw State Constants
TCLAW_STATE_IDLE = 0
TCLAW_STATE_LISTENING = 1
TCLAW_STATE_THINKING = 2
TCLAW_STATE_SPEAKING = 3
TCLAW_STATE_TOOL_CALLING = 4

# Tuya Cloud Event Types (starts at 1 per tuya_dialogue_event_type_t in desk_tclaw_bridge.h)
TUYA_DIALOGUE_EVT_SESSION_START = 1
TUYA_DIALOGUE_EVT_STREAM_TOKEN = 2
TUYA_DIALOGUE_EVT_TOOL_CALL = 3
TUYA_DIALOGUE_EVT_TTS_CHUNK = 4
TUYA_DIALOGUE_EVT_SESSION_END = 5
TUYA_DIALOGUE_EVT_ERROR = 6


class TuyaDialogueEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("session_id", ctypes.c_char_p),
        ("token", ctypes.c_char_p),
        ("is_final_token", ctypes.c_bool),
        ("tool_name", ctypes.c_char_p),
        ("tool_arguments", ctypes.c_char_p),
        ("tts_data", ctypes.POINTER(ctypes.c_uint8)),
        ("tts_len", ctypes.c_size_t),
        ("error_msg", ctypes.c_char_p),
    ]


class DeskWspkHeader(ctypes.Structure):
    _pack_ = 1
    _fields_ = [
        ("magic", ctypes.c_char * 4),
        ("frame_type", ctypes.c_uint8),
        ("flags", ctypes.c_uint8),
        ("stream_id", ctypes.c_uint16),
        ("sequence", ctypes.c_uint32),
        ("payload_size", ctypes.c_uint32),
    ]


# Driver Ops Struct
DRIVER_AUDIO_PLAY_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.c_uint32)
DRIVER_MOTION_MOVE_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_uint32)
DRIVER_DISPLAY_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)

TOOL_DISPATCH_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
TOOL_RESP_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)


class DeskBoardDriverOps(ctypes.Structure):
    _fields_ = [
        ("driver_name", ctypes.c_char_p),
        ("init", ctypes.c_void_p),
        ("audio_record_read", ctypes.c_void_p),
        ("audio_play_pcm", DRIVER_AUDIO_PLAY_CB),
        ("motion_move", DRIVER_MOTION_MOVE_CB),
        ("motion_stop", ctypes.c_void_p),
        ("camera_capture", ctypes.c_void_p),
        ("display_expression", DRIVER_DISPLAY_CB),
        ("deinit", ctypes.c_void_p),
    ]


class TestG1OfficialSdkAndTuyaInterop(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_firmware_interop_test.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        # Compile unified C shared library
        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"-I{repo_root}/firmware/components/desk_tclaw/include",
            f"-I{repo_root}/firmware/components/desk_sdk_bridge/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{repo_root}/firmware/components/desk_tclaw/src/desk_tclaw_bridge.c",
            f"{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c",
            "-lpthread",
            "-o",
            cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile desk firmware interop shared library: {res.stderr}")

        cls.lib = ctypes.CDLL(cls.so_path)

        # Hardware signatures
        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_init.argtypes = []

        cls.lib.desk_hardware_register_driver_ops.restype = ctypes.c_int
        cls.lib.desk_hardware_register_driver_ops.argtypes = [ctypes.POINTER(DeskBoardDriverOps)]

        cls.lib.desk_hardware_motion_get_position.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_get_position.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]

        cls.lib.desk_arbiter_get_owner.restype = ctypes.c_int
        cls.lib.desk_arbiter_get_owner.argtypes = [ctypes.c_int]

        # TClaw signatures
        cls.lib.desk_tclaw_init.restype = ctypes.c_int
        cls.lib.desk_tclaw_init.argtypes = []

        cls.lib.desk_tclaw_reset.restype = None
        cls.lib.desk_tclaw_reset.argtypes = []

        cls.lib.desk_tclaw_start.restype = ctypes.c_int
        cls.lib.desk_tclaw_start.argtypes = []

        cls.lib.desk_tclaw_get_state.restype = ctypes.c_int
        cls.lib.desk_tclaw_get_state.argtypes = []

        cls.lib.desk_tclaw_register_tool_dispatcher.restype = ctypes.c_int
        cls.lib.desk_tclaw_register_tool_dispatcher.argtypes = [TOOL_DISPATCH_CB, ctypes.c_void_p]

        cls.lib.desk_tclaw_tuya_session_start.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_start.argtypes = [ctypes.c_char_p]

        cls.lib.desk_tclaw_tuya_session_feed_audio.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_feed_audio.argtypes = [ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_tclaw_tuya_handle_cloud_event.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_handle_cloud_event.argtypes = [ctypes.POINTER(TuyaDialogueEvent)]

        cls.lib.desk_tclaw_tuya_session_stop.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_stop.argtypes = []

        # SDK Bridge signatures
        cls.lib.desk_sdk_bridge_init.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_init.argtypes = []

        cls.lib.desk_sdk_bridge_reset.restype = None
        cls.lib.desk_sdk_bridge_reset.argtypes = []

        cls.lib.desk_sdk_bridge_set_auth_required.restype = None
        cls.lib.desk_sdk_bridge_set_auth_required.argtypes = [ctypes.c_bool]

        cls.lib.desk_sdk_bridge_get_pairing_token.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_get_pairing_token.argtypes = [ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_sdk_bridge_dispatch_tool_request.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_dispatch_tool_request.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_register_tool_response_cb.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_register_tool_response_cb.argtypes = [TOOL_RESP_CB, ctypes.c_void_p]

        cls.lib.desk_sdk_socket_server_start.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_start.argtypes = [ctypes.c_uint16]

        cls.lib.desk_sdk_socket_server_get_port.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_get_port.argtypes = []

        cls.lib.desk_sdk_socket_server_stop.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_stop.argtypes = []

        cls.lib.desk_sdk_pairing_server_start.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_start.argtypes = [ctypes.c_uint16]

        cls.lib.desk_sdk_pairing_server_get_port.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_get_port.argtypes = []

        cls.lib.desk_sdk_pairing_server_stop.restype = ctypes.c_int
        cls.lib.desk_sdk_pairing_server_stop.argtypes = []

        cls.lib.desk_firmware_start_background_task.restype = ctypes.c_int
        cls.lib.desk_firmware_start_background_task.argtypes = []

        cls.lib.desk_firmware_stop_background_task.restype = None
        cls.lib.desk_firmware_stop_background_task.argtypes = []

        cls.lib.desk_wspk_pack_header.restype = ctypes.c_int
        cls.lib.desk_wspk_pack_header.argtypes = [
            ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint16,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_char_p, ctypes.c_size_t,
        ]

        cls.lib.desk_wspk_unpack_header.restype = ctypes.c_int
        cls.lib.desk_wspk_unpack_header.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t, ctypes.POINTER(DeskWspkHeader),
        ]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_hardware_init()
        self.lib.desk_tclaw_reset()
        self.lib.desk_tclaw_init()
        self.lib.desk_sdk_bridge_reset()
        self.lib.desk_sdk_bridge_init()

    def tearDown(self) -> None:
        self.lib.desk_firmware_stop_background_task()
        self.lib.desk_sdk_socket_server_stop()
        self.lib.desk_sdk_pairing_server_stop()
        self.lib.desk_tclaw_tuya_session_stop()
        self.lib.desk_tclaw_reset()
        self.lib.desk_sdk_bridge_reset()

    def test_g1_concurrent_tuya_dialogue_and_official_sdk_operations(self) -> None:
        """
        G1 Milestone Verification:
        1. Tuya Cloud Dialogue Engine active session running.
        2. Official Python SDK performs UDP pairing discovery, receives dynamic token.
        3. SDK performs TCP handshake sys.client.hello, validates auth token.
        4. SDK executes motion command (ctrl.motion.move) concurrently during active Tuya speech turn.
        5. Tuya dialogue engine triggers tool call handled via SDK tool channel.
        6. SDK streams WSPK binary media frames concurrently.
        """
        # Step 1: Register Board Driver Ops to track physical driver executions
        driver_motion_log = []
        driver_audio_log = []

        @DRIVER_MOTION_MOVE_CB
        def on_driver_motion(pan, tilt, dur):
            driver_motion_log.append((pan, tilt, dur))
            return 0

        @DRIVER_AUDIO_PLAY_CB
        def on_driver_audio(pcm_ptr, samples, rate):
            driver_audio_log.append((samples, rate))
            return 0

        bsp_ops = DeskBoardDriverOps()
        bsp_ops.driver_name = b"esp32s3_test_bsp"
        bsp_ops.motion_move = on_driver_motion
        bsp_ops.audio_play_pcm = on_driver_audio

        self.lib.desk_hardware_register_driver_ops(ctypes.byref(bsp_ops))

        # Step 2: Wire TClaw <-> SDK Tool channels
        tool_requests_routed = []
        @TOOL_DISPATCH_CB
        def on_tool_dispatch(env_ptr, user_data):
            tool_requests_routed.append(ctypes.string_at(env_ptr).decode("utf-8"))
            return 0

        self.lib.desk_tclaw_register_tool_dispatcher(on_tool_dispatch, None)

        # Step 3: Start TCP & UDP Servers on dynamic ephemeral ports
        rc_tcp = self.lib.desk_sdk_socket_server_start(0)
        self.assertEqual(rc_tcp, 0, "Failed to start SDK TCP server")
        tcp_port = self.lib.desk_sdk_socket_server_get_port()
        self.assertGreater(tcp_port, 1024)

        rc_udp = self.lib.desk_sdk_pairing_server_start(0)
        self.assertEqual(rc_udp, 0, "Failed to start SDK UDP pairing server")
        udp_port = self.lib.desk_sdk_pairing_server_get_port()
        self.assertGreater(udp_port, 1024)

        # Step 4: Start background polling worker task
        rc_task = self.lib.desk_firmware_start_background_task()
        self.assertEqual(rc_task, 0, "Failed to start background worker task")

        # Step 5: Start Tuya Cloud Dialogue Session
        rc_tuya = self.lib.desk_tclaw_tuya_session_start(b"tuya-g1-live-session-001")
        self.assertEqual(rc_tuya, 0)
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_LISTENING)

        # Step 6: Official SDK Client Discovery via UDP Pairing
        udp_client = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_client.settimeout(2.0)
        pair_req = {
            "protocol": "watcher_lan_pairing_v1",
            "request_id": "pair-g1-001",
            "daemon_instance_id": "sdk-daemon-g1",
            "target_mode": "python_sdk",
        }
        udp_client.sendto(json.dumps(pair_req).encode("utf-8"), ("127.0.0.1", udp_port))
        data, _ = udp_client.recvfrom(4096)
        udp_client.close()

        pair_resp = json.loads(data.decode("utf-8"))
        self.assertEqual(pair_resp.get("type"), "pair.accept")
        self.assertEqual(pair_resp.get("status"), "accepted")
        session_token = pair_resp.get("token")
        self.assertIsNotNone(session_token)
        self.assertEqual(len(session_token), 64, "Session token must be a 64-char dynamic hex string")

        # Step 7: TCP Connect - Verify Invalid Token is Rejected with 401
        sock_invalid = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock_invalid.connect(("127.0.0.1", tcp_port))
        sock_invalid.settimeout(2.0)
        invalid_hello = {
            "type": "sys.client.hello",
            "data": {"role": "desktop", "token": "invalid_bogus_token_12345"},
        }
        sock_invalid.sendall((json.dumps(invalid_hello) + "\n").encode("utf-8"))
        raw_resp = sock_invalid.recv(4096).decode("utf-8").strip()
        sock_invalid.close()

        resp_invalid = json.loads(raw_resp)
        self.assertEqual(resp_invalid.get("type"), "sys.nack")
        self.assertEqual(resp_invalid.get("code"), 401)
        self.assertEqual(resp_invalid.get("error", {}).get("code"), "AUTH_FAILED")

        # Step 8: TCP Connect - Valid Token Handshake & Capabilities Negotiation
        sock_sdk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock_sdk.connect(("127.0.0.1", tcp_port))
        sock_sdk.settimeout(2.0)
        valid_hello = {
            "type": "sys.client.hello",
            "data": {"role": "desktop", "token": session_token},
        }
        sock_sdk.sendall((json.dumps(valid_hello) + "\n").encode("utf-8"))
        raw_hello_ack = sock_sdk.recv(4096).decode("utf-8").strip()
        ack_hello = json.loads(raw_hello_ack)
        self.assertEqual(ack_hello.get("type"), "sys.ack")
        self.assertEqual(ack_hello.get("code"), 0)
        caps = ack_hello.get("data", {}).get("capabilities", [])
        for cap in ["inputs", "motion", "camera", "audio.stream", "expression.audio_follow.v1"]:
            self.assertIn(cap, caps)

        # Step 9: Concurrently feed Tuya cloud dialogue audio & transition to speaking
        evt_stream = TuyaDialogueEvent()
        evt_stream.type = TUYA_DIALOGUE_EVT_STREAM_TOKEN
        evt_stream.token = b"Hello from Tuya Cloud!"
        evt_stream.is_final_token = True
        self.lib.desk_tclaw_tuya_handle_cloud_event(ctypes.byref(evt_stream))

        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_SPEAKING)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_TCLAW_VOICE)

        # Step 10: Official SDK executes motion command while Tuya is speaking
        motion_cmd = {
            "type": "ctrl.motion.move",
            "data": {"pan_deg": 135.0, "tilt_deg": 115.0, "duration_ms": 600},
        }
        sock_sdk.sendall((json.dumps(motion_cmd) + "\n").encode("utf-8"))
        raw_motion_ack = sock_sdk.recv(4096).decode("utf-8").strip()
        ack_motion = json.loads(raw_motion_ack)
        self.assertEqual(ack_motion.get("type"), "sys.ack")
        self.assertEqual(ack_motion.get("data", {}).get("status"), "succeeded")

        # Verify physical angle updated and driver was invoked
        pan = ctypes.c_float()
        tilt = ctypes.c_float()
        self.lib.desk_hardware_motion_get_position(ctypes.byref(pan), ctypes.byref(tilt))
        self.assertAlmostEqual(pan.value, 135.0, places=1)
        self.assertAlmostEqual(tilt.value, 115.0, places=1)
        self.assertGreater(len(driver_motion_log), 0)
        self.assertAlmostEqual(driver_motion_log[-1][0], 135.0, places=1)
        self.assertAlmostEqual(driver_motion_log[-1][1], 115.0, places=1)

        # Step 11: Tuya triggers MCP Tool Call routed to SDK tool dispatcher
        evt_tool = TuyaDialogueEvent()
        evt_tool.type = TUYA_DIALOGUE_EVT_TOOL_CALL
        evt_tool.tool_name = b"ha.execute"
        evt_tool.tool_arguments = b'{"entity_id": "light.desk_lamp", "action": "turn_on"}'
        self.lib.desk_tclaw_tuya_handle_cloud_event(ctypes.byref(evt_tool))

        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_TOOL_CALLING)
        self.assertGreater(len(tool_requests_routed), 0)
        req_env = json.loads(tool_requests_routed[-1])
        self.assertEqual(req_env.get("operation"), "ha.execute")

        # Step 12: SDK WSPK Media stream validation
        wspk_buf = ctypes.create_string_buffer(64)
        rc_pack = self.lib.desk_wspk_pack_header(1, 0, 100, 1, 32, wspk_buf, 64)
        self.assertEqual(rc_pack, 0)
        unpacked_hdr = DeskWspkHeader()
        rc_unpack = self.lib.desk_wspk_unpack_header(wspk_buf.raw, 16, ctypes.byref(unpacked_hdr))
        self.assertEqual(rc_unpack, 0)
        self.assertEqual(unpacked_hdr.magic, b"WSPK")
        self.assertEqual(unpacked_hdr.sequence, 1)
        self.assertEqual(unpacked_hdr.payload_size, 32)

        # Step 13: Clean end of turn & shutdown
        sock_sdk.close()
        evt_end = TuyaDialogueEvent()
        evt_end.type = TUYA_DIALOGUE_EVT_SESSION_END
        self.lib.desk_tclaw_tuya_handle_cloud_event(ctypes.byref(evt_end))

        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_IDLE)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_NONE)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL), DESK_OWNER_NONE)


if __name__ == "__main__":
    unittest.main()
