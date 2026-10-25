"""
Unit and contract tests for Desk Agent R1 TClaw Dialogue Engine and Tool Gateway.
Compiles and loads the C shared library for desk_hardware, desk_arbiter, and desk_tclaw_bridge,
verifying dialogue turn state machine, audio playback acquisition, user speech/touch interruption,
MCP external tool RequestEnvelope dispatch / ResponseEnvelope handling, CR-002 motion semantics,
built-in hardware tool execution, strict camera capture gating, and single-board self-test.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import time
import unittest

TCLAW_STATE_IDLE = 0
TCLAW_STATE_LISTENING = 1
TCLAW_STATE_THINKING = 2
TCLAW_STATE_SPEAKING = 3
TCLAW_STATE_TOOL_CALLING = 4

TUYA_CLOUD_DISCONNECTED = 0
TUYA_CLOUD_CONNECTING = 1
TUYA_CLOUD_CONNECTED = 2
TUYA_CLOUD_STREAMING = 3
TUYA_CLOUD_RECONNECTING = 4
TUYA_CLOUD_ERROR = 5

DESK_RES_AUDIO_PLAYBACK = 0
DESK_RES_AUDIO_RECORD = 1
DESK_RES_CAMERA_SENSOR = 2
DESK_RES_MOTION_GIMBAL = 3
DESK_RES_DISPLAY_UI = 4

DESK_OWNER_NONE = 0
DESK_OWNER_IDLE = 1
DESK_OWNER_TCLAW_VOICE = 2
DESK_OWNER_SDK_REMOTE = 3
DESK_OWNER_LAN_CAMERA = 4
DESK_OWNER_SYSTEM_ALERT = 5

DESK_HW_OK = 0
DESK_HW_ERR_INVALID_ARG = -1
DESK_HW_ERR_UNAVAILABLE = -2
DESK_HW_ERR_TIMEOUT = -3
DESK_HW_ERR_IO = -4
DESK_HW_ERR_BUSY = -5

TOOL_DISPATCH_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)
MOTION_MOVE_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_float, ctypes.c_float, ctypes.c_uint32)
MOTION_STOP_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_bool)


class DeskBoardDriverOps(ctypes.Structure):
    _fields_ = [
        ("driver_name", ctypes.c_char_p),
        ("init", ctypes.c_void_p),
        ("audio_record_read", ctypes.c_void_p),
        ("audio_play_pcm", ctypes.c_void_p),
        ("motion_move", MOTION_MOVE_CB),
        ("motion_stop", MOTION_STOP_CB),
        ("camera_capture", ctypes.c_void_p),
        ("display_expression", ctypes.c_void_p),
        ("deinit", ctypes.c_void_p),
    ]


class DeskTclawSelfTest(ctypes.Structure):
    _fields_ = [
        ("initialized", ctypes.c_bool),
        ("state", ctypes.c_int),
        ("tuya_state", ctypes.c_int),
        ("turn_count", ctypes.c_int),
        ("session_id", ctypes.c_char * 128),
        ("last_response", ctypes.c_char * 256),
        ("pending_tool", ctypes.c_char * 128),
        ("audio_owner", ctypes.c_int),
        ("motion_owner", ctypes.c_int),
        ("camera_owner", ctypes.c_int),
        ("last_error_code", ctypes.c_int),
    ]


class TestTClawDialogue(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_tclaw_test.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        # Compile desk_hardware + desk_arbiter + desk_tclaw_bridge into shared library
        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"-I{repo_root}/firmware/components/desk_tclaw/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{repo_root}/firmware/components/desk_tclaw/src/desk_tclaw_bridge.c",
            "-o",
            cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile desk_tclaw shared library: {res.stderr}")

        cls.lib = ctypes.CDLL(cls.so_path)

        # Setup signatures
        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_init.argtypes = []
        cls.lib.desk_hardware_register_driver_ops.restype = ctypes.c_int
        cls.lib.desk_hardware_register_driver_ops.argtypes = [ctypes.POINTER(DeskBoardDriverOps)]

        cls.lib.desk_arbiter_get_owner.restype = ctypes.c_int
        cls.lib.desk_arbiter_get_owner.argtypes = [ctypes.c_int]

        cls.lib.desk_arbiter_acquire.restype = ctypes.c_int
        cls.lib.desk_arbiter_acquire.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32]

        cls.lib.desk_arbiter_release.restype = None
        cls.lib.desk_arbiter_release.argtypes = [ctypes.c_int, ctypes.c_int]

        cls.lib.desk_hardware_display_get_expression.restype = ctypes.c_int
        cls.lib.desk_hardware_display_get_expression.argtypes = [
            ctypes.c_char_p,
            ctypes.c_size_t,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        cls.lib.desk_hardware_camera_inject_frame.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_inject_frame.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]

        cls.lib.desk_tclaw_init.restype = ctypes.c_int
        cls.lib.desk_tclaw_init.argtypes = []

        cls.lib.desk_tclaw_start.restype = ctypes.c_int
        cls.lib.desk_tclaw_start.argtypes = []

        cls.lib.desk_tclaw_get_state.restype = ctypes.c_int
        cls.lib.desk_tclaw_get_state.argtypes = []

        cls.lib.desk_tclaw_is_initialized.restype = ctypes.c_bool
        cls.lib.desk_tclaw_is_initialized.argtypes = []

        cls.lib.desk_tclaw_inject_text.restype = ctypes.c_int
        cls.lib.desk_tclaw_inject_text.argtypes = [ctypes.c_char_p]

        cls.lib.desk_tclaw_start_speaking.restype = ctypes.c_int
        cls.lib.desk_tclaw_start_speaking.argtypes = [ctypes.c_char_p]

        cls.lib.desk_tclaw_interrupt.restype = ctypes.c_int
        cls.lib.desk_tclaw_interrupt.argtypes = []

        cls.lib.desk_tclaw_register_tool_dispatcher.restype = ctypes.c_int
        cls.lib.desk_tclaw_register_tool_dispatcher.argtypes = [TOOL_DISPATCH_CB, ctypes.c_void_p]

        cls.lib.desk_tclaw_trigger_tool_call.restype = ctypes.c_int
        cls.lib.desk_tclaw_trigger_tool_call.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]

        cls.lib.desk_tclaw_handle_tool_response.restype = ctypes.c_int
        cls.lib.desk_tclaw_handle_tool_response.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
        ]

        SPEECH_STREAM_CB = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_bool, ctypes.c_void_p)
        cls.SPEECH_STREAM_CB = SPEECH_STREAM_CB
        cls.lib.desk_tclaw_register_speech_stream_cb.restype = ctypes.c_int
        cls.lib.desk_tclaw_register_speech_stream_cb.argtypes = [SPEECH_STREAM_CB, ctypes.c_void_p]

        cls.lib.desk_tclaw_get_last_response.restype = ctypes.c_int
        cls.lib.desk_tclaw_get_last_response.argtypes = [ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_tclaw_get_turn_count.restype = ctypes.c_int
        cls.lib.desk_tclaw_get_turn_count.argtypes = []

        # Tuya Cloud Dialogue Session API
        cls.lib.desk_tclaw_tuya_session_connect.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_connect.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]

        cls.lib.desk_tclaw_tuya_session_disconnect.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_disconnect.argtypes = []

        cls.lib.desk_tclaw_tuya_session_reconnect.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_reconnect.argtypes = []

        cls.lib.desk_tclaw_tuya_session_get_connection_state.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_get_connection_state.argtypes = []

        cls.lib.desk_tclaw_tuya_session_start.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_start.argtypes = [ctypes.c_char_p]

        cls.lib.desk_tclaw_tuya_get_session_id.restype = ctypes.c_char_p
        cls.lib.desk_tclaw_tuya_get_session_id.argtypes = []

        cls.lib.desk_tclaw_tuya_session_feed_audio.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_feed_audio.argtypes = [ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_tclaw_tuya_send_tool_result.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_send_tool_result.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p]

        cls.lib.desk_tclaw_tuya_get_last_tool_result.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_get_last_tool_result.argtypes = [ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_tclaw_tuya_session_stop.restype = ctypes.c_int
        cls.lib.desk_tclaw_tuya_session_stop.argtypes = []

        cls.lib.desk_tclaw_reset.restype = None
        cls.lib.desk_tclaw_reset.argtypes = []

        # Self-Test and Diagnostics API
        cls.lib.desk_tclaw_self_test.restype = ctypes.c_int
        cls.lib.desk_tclaw_self_test.argtypes = []

        cls.lib.desk_tclaw_self_test_report.restype = ctypes.c_int
        cls.lib.desk_tclaw_self_test_report.argtypes = [ctypes.POINTER(DeskTclawSelfTest)]

        # Built-in Tool Execution & Timeout API
        cls.lib.desk_tclaw_execute_tool.restype = ctypes.c_int
        cls.lib.desk_tclaw_execute_tool.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]

        cls.lib.desk_tclaw_is_builtin_tool.restype = ctypes.c_bool
        cls.lib.desk_tclaw_is_builtin_tool.argtypes = [ctypes.c_char_p]

        cls.lib.desk_tclaw_set_tool_timeout_ms.restype = None
        cls.lib.desk_tclaw_set_tool_timeout_ms.argtypes = [ctypes.c_uint32]

        cls.lib.desk_tclaw_get_tool_timeout_ms.restype = ctypes.c_uint32
        cls.lib.desk_tclaw_get_tool_timeout_ms.argtypes = []

        cls.lib.desk_tclaw_handle_tool_timeout.restype = ctypes.c_int
        cls.lib.desk_tclaw_handle_tool_timeout.argtypes = [ctypes.c_char_p]

        cls.lib.desk_tclaw_poll_timeouts.restype = ctypes.c_int
        cls.lib.desk_tclaw_poll_timeouts.argtypes = [ctypes.c_uint64]

        cls.lib.desk_tclaw_get_pending_tool.restype = ctypes.c_char_p
        cls.lib.desk_tclaw_get_pending_tool.argtypes = []

        cls.lib.desk_tclaw_get_pending_request_id.restype = ctypes.c_char_p
        cls.lib.desk_tclaw_get_pending_request_id.argtypes = []

        cls.lib.desk_tclaw_on_tuya_mqtt_connected.restype = ctypes.c_int
        cls.lib.desk_tclaw_on_tuya_mqtt_connected.argtypes = []

        cls.lib.desk_tclaw_on_tuya_mqtt_disconnected.restype = ctypes.c_int
        cls.lib.desk_tclaw_on_tuya_mqtt_disconnected.argtypes = []

        cls.lib.desk_tclaw_on_tuya_netcfg_started.restype = ctypes.c_int
        cls.lib.desk_tclaw_on_tuya_netcfg_started.argtypes = []

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_hardware_init()
        self.lib.desk_tclaw_reset()
        self.lib.desk_tclaw_init()

    def tearDown(self) -> None:
        self.lib.desk_tclaw_reset()

    def test_initial_state_is_idle(self) -> None:
        """Verifies initial state is IDLE and audio playback is unowned."""
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_IDLE)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_NONE)

    def test_speaking_acquires_audio_and_idle_releases(self) -> None:
        """Verifies speaking state acquires audio playback under TCLAW_VOICE owner."""
        rc = self.lib.desk_tclaw_start_speaking(b"Hello from Desk Agent!")
        self.assertEqual(rc, 0)
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_SPEAKING)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_TCLAW_VOICE)

        # Completing speech turn releases audio resource
        rc_finish = self.lib.desk_tclaw_interrupt()
        self.assertEqual(rc_finish, 0)
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_IDLE)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_NONE)

    def test_speech_interruption(self) -> None:
        """Verifies user touch or barge-in interrupt halts speech immediately and resets state."""
        self.lib.desk_tclaw_start_speaking(b"Long spoken response that should be interrupted...")
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_SPEAKING)

        # Trigger interrupt
        rc = self.lib.desk_tclaw_interrupt()
        self.assertEqual(rc, 0)
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_IDLE)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_NONE)

    def test_tool_calling_roundtrip(self) -> None:
        """Verifies tool call dispatch and response handling workflow."""
        dispatched_envelopes: list[str] = []

        @TOOL_DISPATCH_CB
        def dispatcher(env_ptr, user_data):
            dispatched_envelopes.append(ctypes.string_at(env_ptr).decode("utf-8"))
            return 0

        self.lib.desk_tclaw_register_tool_dispatcher(dispatcher, None)

        # Trigger external tool call
        rc = self.lib.desk_tclaw_trigger_tool_call(
            b"req-tool-001",
            b"ha.execute",
            b'{"entity_id": "light.desk_lamp", "action": "turn_on"}',
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_TOOL_CALLING)
        self.assertEqual(len(dispatched_envelopes), 1)

        req_env = json.loads(dispatched_envelopes[0])
        self.assertEqual(req_env.get("schema_version"), "1.0.0")
        self.assertEqual(req_env.get("request_id"), "req-tool-001")
        self.assertEqual(req_env.get("operation"), "ha.execute")

        # Simulate Python application returning successful result
        rc_resp = self.lib.desk_tclaw_handle_tool_response(
            b"req-tool-001",
            b"succeeded",
            b'{"status": "on"}',
            None,
        )
        self.assertEqual(rc_resp, 0)
        # Should have transitioned to SPEAKING with audio owner
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_SPEAKING)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_TCLAW_VOICE)

        # Verify dynamic response text reflects tool result rather than fixed text
        resp_buf = ctypes.create_string_buffer(512)
        self.lib.desk_tclaw_get_last_response(resp_buf, 512)
        resp_text = resp_buf.value.decode("utf-8")
        self.assertIn("Action completed successfully", resp_text)
        self.assertIn("status", resp_text)

        # End of turn
        self.lib.desk_tclaw_interrupt()
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_IDLE)

    def test_intent_recognition_and_tool_dispatch(self) -> None:
        """Verifies natural speech intent parses into external tool calls."""
        dispatched = []
        @TOOL_DISPATCH_CB
        def dispatcher(env_ptr, user_data):
            dispatched.append(json.loads(ctypes.string_at(env_ptr).decode("utf-8")))
            return 0

        self.lib.desk_tclaw_register_tool_dispatcher(dispatcher, None)

        # 1. Light intent -> ha.call_service
        self.lib.desk_tclaw_inject_text(b"Please turn on the light in the office")
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["operation"], "ha.call_service")
        self.assertEqual(dispatched[0]["arguments"]["entity_id"], "light.desk_lamp")

        # 2. Reminder intent -> life.create_reminder
        self.lib.desk_tclaw_inject_text(b"Set a reminder for my meeting")
        self.assertEqual(len(dispatched), 2)
        self.assertEqual(dispatched[1]["operation"], "life.create_reminder")

        # 3. Camera intent -> camera.capture
        self.lib.desk_tclaw_inject_text(b"Take a camera snapshot")
        self.assertEqual(len(dispatched), 3)
        self.assertEqual(dispatched[2]["operation"], "camera.capture")

    def test_contextual_response_and_token_streaming(self) -> None:
        """Verifies conversational response echoes context dynamically and streams speech tokens."""
        streamed_tokens = []
        def on_stream(token_ptr, is_final, user_data):
            streamed_tokens.append((ctypes.string_at(token_ptr).decode("utf-8"), is_final))

        cb_holder = self.SPEECH_STREAM_CB(on_stream)
        self.lib.desk_tclaw_register_speech_stream_cb(cb_holder, None)

        # Non-tool conversational prompt
        prompt = b"What time is it right now?"
        self.lib.desk_tclaw_inject_text(prompt)

        # Verify response text is contextual and NOT hardcoded "Processed response"
        resp_buf = ctypes.create_string_buffer(512)
        self.lib.desk_tclaw_get_last_response(resp_buf, 512)
        resp_text = resp_buf.value.decode("utf-8")

        self.assertNotIn("Processed response", resp_text)
        self.assertIn("What time is it right now?", resp_text)

        # Verify streaming tokens
        self.assertGreater(len(streamed_tokens), 0)
        self.assertTrue(streamed_tokens[-1][1], "Last token must have is_final=True")
        full_streamed = " ".join(t[0] for t in streamed_tokens)
        self.assertEqual(full_streamed, resp_text)

    def test_tuya_cloud_dialogue_session_and_events(self) -> None:
        """Verifies Tuya Cloud dialogue session lifecycle and audio streaming feeding."""
        # 1. Start Tuya session
        rc = self.lib.desk_tclaw_tuya_session_start(b"tuya-test-sess-001")
        self.assertEqual(rc, 0)
        sess_id = self.lib.desk_tclaw_tuya_get_session_id().decode("utf-8")
        self.assertEqual(sess_id, "tuya-test-sess-001")
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_LISTENING)

        # 2. Feed microphone audio data into active session
        fake_pcm = b"\x00\x10" * 32
        rc_audio = self.lib.desk_tclaw_tuya_session_feed_audio(fake_pcm, len(fake_pcm))
        self.assertEqual(rc_audio, 0)
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_LISTENING)

        # 3. Stop session
        rc_stop = self.lib.desk_tclaw_tuya_session_stop()
        self.assertEqual(rc_stop, 0)
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_IDLE)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_NONE)

    def test_tuya_cloud_connection_state_machine(self) -> None:
        """Verifies Tuya Cloud session connection state machine: DISCONNECTED -> CONNECTED -> STREAMING -> RECONNECTING."""
        # Initial state is DISCONNECTED
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_DISCONNECTED)

        # Connect with credentials
        rc = self.lib.desk_tclaw_tuya_session_connect(b"wss://tuya.cloud/voice", b"dev-watcher-01", b"secret-token")
        self.assertEqual(rc, 0)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_CONNECTED)
        self.assertTrue(len(self.lib.desk_tclaw_tuya_get_session_id()) > 0)

        # Feed audio transitions to STREAMING
        fake_pcm = b"\x00\x20" * 16
        self.lib.desk_tclaw_tuya_session_feed_audio(fake_pcm, len(fake_pcm))
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_STREAMING)

        # Disconnect transitions to DISCONNECTED
        self.lib.desk_tclaw_tuya_session_disconnect()
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_DISCONNECTED)

        # Reconnect restores connection
        rc_rec = self.lib.desk_tclaw_tuya_session_reconnect()
        self.assertEqual(rc_rec, 0)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_CONNECTED)

    def test_tuya_tool_result_recording(self) -> None:
        """Verifies tool results sent back to Tuya session are preserved in context."""
        rc = self.lib.desk_tclaw_tuya_send_tool_result(
            b"sess-123",
            b"ha.turn_on",
            b'{"status": "success", "brightness": 255}',
        )
        self.assertEqual(rc, 0)

        res_buf = ctypes.create_string_buffer(256)
        rc_get = self.lib.desk_tclaw_tuya_get_last_tool_result(res_buf, 256)
        self.assertEqual(rc_get, 0)
        self.assertEqual(res_buf.value.decode("utf-8"), '{"status": "success", "brightness": 255}')

    # ----------------------------------------------------------------------
    # R4 New Contracts: Self-Test, Diagnostics, Built-in Tools, CR-002, Timeouts
    # ----------------------------------------------------------------------
    def test_tclaw_self_test_and_diagnostics(self) -> None:
        """Verifies single-board self-test desk_tclaw_self_test() and diagnostic reporting."""
        # 1. Operational self-test PASS
        rc = self.lib.desk_tclaw_self_test()
        self.assertEqual(rc, DESK_HW_OK)

        # 2. Detailed diagnostics report inspects safe state
        report = DeskTclawSelfTest()
        rc_diag = self.lib.desk_tclaw_self_test_report(ctypes.byref(report))
        self.assertEqual(rc_diag, DESK_HW_OK)
        self.assertTrue(report.initialized)
        self.assertEqual(report.state, TCLAW_STATE_IDLE)
        self.assertEqual(report.tuya_state, TUYA_CLOUD_DISCONNECTED)
        self.assertEqual(report.audio_owner, DESK_OWNER_NONE)
        self.assertEqual(report.motion_owner, DESK_OWNER_NONE)
        self.assertEqual(report.camera_owner, DESK_OWNER_NONE)
        self.assertEqual(report.last_error_code, 0)

        # 3. Uninitialized failure: when reset, self-test fails with DESK_HW_ERR_UNAVAILABLE
        self.lib.desk_tclaw_reset()
        rc_uninit = self.lib.desk_tclaw_self_test()
        self.assertEqual(rc_uninit, DESK_HW_ERR_UNAVAILABLE)

        uninit_report = DeskTclawSelfTest()
        rc_uninit_report = self.lib.desk_tclaw_self_test_report(ctypes.byref(uninit_report))
        self.assertEqual(rc_uninit_report, DESK_HW_ERR_UNAVAILABLE)
        self.assertFalse(uninit_report.initialized)

        # 4. Re-initialization restores operational status
        self.lib.desk_tclaw_init()
        self.assertEqual(self.lib.desk_tclaw_self_test(), DESK_HW_OK)

    def test_builtin_motion_tool_cr002_compliance(self) -> None:
        """Verifies built-in motion tool execution complies strictly with CR-002 (accepted=True, state_confirmed=False)."""
        resp_buf = ctypes.create_string_buffer(2048)
        rc = self.lib.desk_tclaw_execute_tool(
            b"req-mv-cr002",
            b"motion.move",
            b'{"pan_deg": 120.0, "tilt_deg": 115.0, "duration_ms": 400}',
            resp_buf,
            2048,
        )
        self.assertEqual(rc, DESK_HW_OK)

        resp_env = json.loads(resp_buf.value.decode("utf-8"))
        self.assertEqual(resp_env.get("schema_version"), "1.0.0")
        self.assertEqual(resp_env.get("request_id"), "req-mv-cr002")
        # CR-002: status MUST be 'accepted', NOT 'succeeded' or 'completed'
        self.assertEqual(resp_env.get("status"), "accepted")
        self.assertTrue(resp_env.get("result", {}).get("accepted"))
        self.assertFalse(resp_env.get("result", {}).get("state_confirmed"))
        self.assertEqual(resp_env.get("result", {}).get("pan_deg"), 120.0)
        self.assertEqual(resp_env.get("result", {}).get("tilt_deg"), 115.0)

        # Gimbal arbiter must be cleanly released
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL), DESK_OWNER_NONE)

        # Test motion stop tool
        rc_stop = self.lib.desk_tclaw_execute_tool(
            b"req-stop-001",
            b"motion.stop",
            b'{"emergency": false}',
            resp_buf,
            2048,
        )
        self.assertEqual(rc_stop, DESK_HW_OK)
        stop_env = json.loads(resp_buf.value.decode("utf-8"))
        self.assertEqual(stop_env.get("status"), "accepted")
        self.assertTrue(stop_env.get("result", {}).get("accepted"))
        self.assertFalse(stop_env.get("result", {}).get("state_confirmed"))
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL), DESK_OWNER_NONE)

    def test_builtin_motion_arbitration_rejection(self) -> None:
        """Verifies motion tool execution is rejected with RESOURCE_BUSY when held by another owner."""
        # Hold lock with SYSTEM_ALERT
        self.lib.desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SYSTEM_ALERT, 0)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL), DESK_OWNER_SYSTEM_ALERT)

        resp_buf = ctypes.create_string_buffer(2048)
        rc = self.lib.desk_tclaw_execute_tool(
            b"req-mv-busy",
            b"motion.move",
            b'{"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 300}',
            resp_buf,
            2048,
        )
        self.assertEqual(rc, DESK_HW_ERR_BUSY)
        resp_env = json.loads(resp_buf.value.decode("utf-8"))
        self.assertEqual(resp_env.get("status"), "failed")
        self.assertEqual(resp_env.get("error", {}).get("code"), "RESOURCE_BUSY")
        self.assertTrue(resp_env.get("error", {}).get("retryable"))

        # Release SYSTEM_ALERT lock
        self.lib.desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SYSTEM_ALERT)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL), DESK_OWNER_NONE)

    def test_builtin_motion_rejects_out_of_range_target(self) -> None:
        resp_buf = ctypes.create_string_buffer(2048)
        rc = self.lib.desk_tclaw_execute_tool(
            b"req-mv-invalid",
            b"ctrl.motion.move",
            b'{"pan_deg":181,"tilt_deg":120,"duration_ms":300}',
            resp_buf,
            2048,
        )
        self.assertEqual(rc, DESK_HW_ERR_INVALID_ARG)
        response = json.loads(resp_buf.value.decode("utf-8"))
        self.assertEqual(response.get("status"), "failed")
        self.assertEqual(response.get("error", {}).get("code"), "INVALID_ARGUMENT")

    def test_builtin_motion_timeout_is_reported_for_move_and_stop(self) -> None:
        @MOTION_MOVE_CB
        def move_timeout(_pan_deg, _tilt_deg, _duration_ms):
            return DESK_HW_ERR_TIMEOUT

        @MOTION_STOP_CB
        def stop_timeout(_emergency):
            return DESK_HW_ERR_TIMEOUT

        ops = DeskBoardDriverOps()
        ops.driver_name = b"contract-timeout"
        ops.motion_move = move_timeout
        ops.motion_stop = stop_timeout
        self.assertEqual(self.lib.desk_hardware_register_driver_ops(ctypes.byref(ops)), DESK_HW_OK)
        try:
            resp_buf = ctypes.create_string_buffer(2048)
            for request_id, operation, arguments in (
                (b"req-mv-timeout", b"ctrl.motion.move",
                 b'{"pan_deg":90,"tilt_deg":120,"duration_ms":300}'),
                (b"req-stop-timeout", b"ctrl.motion.stop", b'{"emergency":true}'),
            ):
                rc = self.lib.desk_tclaw_execute_tool(
                    request_id, operation, arguments, resp_buf, 2048
                )
                self.assertEqual(rc, DESK_HW_ERR_TIMEOUT)
                response = json.loads(resp_buf.value.decode("utf-8"))
                self.assertEqual(response.get("status"), "failed")
                self.assertEqual(response.get("error", {}).get("code"), "OPERATION_TIMEOUT")
                self.assertTrue(response.get("error", {}).get("retryable"))
        finally:
            self.lib.desk_hardware_register_driver_ops(None)

    def test_builtin_camera_capture_real_and_unavailable(self) -> None:
        """Verifies camera tool fails closed when no frame is present, and succeeds when real JPEG is injected."""
        resp_buf = ctypes.create_string_buffer(2048)

        # 1. No frame present: must fail closed, strictly never synthesizing dummy image
        rc_no_frame = self.lib.desk_tclaw_execute_tool(
            b"req-cam-001",
            b"camera.capture",
            b'{"timeout_ms": 1000}',
            resp_buf,
            2048,
        )
        self.assertEqual(rc_no_frame, DESK_HW_ERR_UNAVAILABLE)
        env_no_frame = json.loads(resp_buf.value.decode("utf-8"))
        self.assertEqual(env_no_frame.get("status"), "failed")
        self.assertEqual(env_no_frame.get("error", {}).get("code"), "HARDWARE_FAULT")
        self.assertIn("unavailable", env_no_frame.get("error", {}).get("message"))
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_CAMERA_SENSOR), DESK_OWNER_NONE)

        # 2. Inject real camera frame: capture succeeds
        fake_jpeg = b"\xFF\xD8\xFF\xE0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xFF\xDB\x00C\x00"
        self.lib.desk_hardware_camera_inject_frame(fake_jpeg, len(fake_jpeg), 12345)

        rc_cam = self.lib.desk_tclaw_execute_tool(
            b"req-cam-002",
            b"camera.capture",
            b'{"timeout_ms": 1000}',
            resp_buf,
            2048,
        )
        self.assertEqual(rc_cam, DESK_HW_OK)
        env_cam = json.loads(resp_buf.value.decode("utf-8"))
        self.assertEqual(env_cam.get("status"), "succeeded")
        self.assertEqual(env_cam.get("result", {}).get("format"), "jpeg")
        self.assertEqual(env_cam.get("result", {}).get("frame_len"), len(fake_jpeg))
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_CAMERA_SENSOR), DESK_OWNER_NONE)

    def test_builtin_expression_tool(self) -> None:
        """Verifies screen expression tool updates display and returns contract response."""
        resp_buf = ctypes.create_string_buffer(2048)
        rc = self.lib.desk_tclaw_execute_tool(
            b"req-exp-001",
            b"expression.set",
            b'{"preset": "happy", "status_text": "Good Morning"}',
            resp_buf,
            2048,
        )
        self.assertEqual(rc, DESK_HW_OK)
        env = json.loads(resp_buf.value.decode("utf-8"))
        self.assertEqual(env.get("status"), "succeeded")
        self.assertEqual(env.get("result", {}).get("preset"), "happy")
        self.assertEqual(env.get("result", {}).get("status_text"), "Good Morning")

        # Verify display readback
        preset_buf = ctypes.create_string_buffer(64)
        status_buf = ctypes.create_string_buffer(128)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, status_buf, 128)
        self.assertEqual(preset_buf.value.decode("utf-8"), "happy")
        self.assertEqual(status_buf.value.decode("utf-8"), "Good Morning")

    def test_cr002_accepted_tool_response_handling(self) -> None:
        """Verifies CR-002 'accepted' tool responses are treated as successful turns rather than errors."""
        accepted_payload = b'{"pan_deg": 90.0, "tilt_deg": 120.0, "accepted": true, "state_confirmed": false}'
        rc = self.lib.desk_tclaw_handle_tool_response(
            b"req-mv-acc",
            b"accepted",
            accepted_payload,
            None,
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_SPEAKING)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_TCLAW_VOICE)

        # Spoken text must reflect accepted action and NOT report error
        resp_buf = ctypes.create_string_buffer(512)
        self.lib.desk_tclaw_get_last_response(resp_buf, 512)
        resp_text = resp_buf.value.decode("utf-8")
        self.assertIn("Action accepted successfully", resp_text)
        self.assertNotIn("Action encountered an error", resp_text)

        # End turn
        self.lib.desk_tclaw_interrupt()
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_IDLE)

    def test_tool_timeout_handling_and_recovery(self) -> None:
        """Verifies external tool execution timeout handling and clean recovery."""
        # Register a dispatcher that simulates hang (does not reply)
        @TOOL_DISPATCH_CB
        def hanging_dispatcher(env_ptr, user_data):
            return 0  # Accepts request but never sends back response

        self.lib.desk_tclaw_register_tool_dispatcher(hanging_dispatcher, None)

        rc = self.lib.desk_tclaw_trigger_tool_call(
            b"req-hang-001",
            b"ha.slow_service",
            b'{"domain": "climate", "service": "set_temperature"}',
        )
        self.assertEqual(rc, 0)
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_TOOL_CALLING)
        self.assertEqual(self.lib.desk_tclaw_get_pending_tool(), b"ha.slow_service")
        self.assertEqual(self.lib.desk_tclaw_get_pending_request_id(), b"req-hang-001")

        # Explicit timeout handling
        rc_timeout = self.lib.desk_tclaw_handle_tool_timeout(b"req-hang-001")
        self.assertEqual(rc_timeout, 0)

        # Transitions to SPEAKING error notice
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_SPEAKING)
        resp_buf = ctypes.create_string_buffer(512)
        self.lib.desk_tclaw_get_last_response(resp_buf, 512)
        resp_text = resp_buf.value.decode("utf-8")
        self.assertIn("Action encountered an error", resp_text)
        self.assertIn("OPERATION_TIMEOUT", resp_text)

        # End turn
        self.lib.desk_tclaw_interrupt()
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_IDLE)

    def test_tuya_connection_security_and_no_fake_connection(self) -> None:
        """Verifies Tuya cloud connection rejects invalid/empty credentials and never fakes connection."""
        # 1. Null / empty connect attempts are rejected
        rc_null = self.lib.desk_tclaw_tuya_session_connect(None, None, None)
        self.assertEqual(rc_null, -1)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_DISCONNECTED)

        rc_empty = self.lib.desk_tclaw_tuya_session_connect(b"", b"", b"")
        self.assertEqual(rc_empty, -1)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_DISCONNECTED)

        # 2. Reconnecting without credentials fails into ERROR
        self.lib.desk_tclaw_reset()
        rc_rec_fail = self.lib.desk_tclaw_tuya_session_reconnect()
        self.assertEqual(rc_rec_fail, -1)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_ERROR)

    def test_tuya_mqtt_state_machine_hooks(self) -> None:
        """Verifies desk_tclaw_on_tuya_mqtt_connected and disconnected update state, screen and audio."""
        self.lib.desk_tclaw_init()

        preset_buf = ctypes.create_string_buffer(64)
        text_buf = ctypes.create_string_buffer(256)

        # 1. Netcfg started
        rc_net = self.lib.desk_tclaw_on_tuya_netcfg_started()
        self.assertEqual(rc_net, 0)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "thinking")
        self.assertEqual(text_buf.value.decode("utf-8"), "BLE Netcfg")

        # 2. Connected
        rc_conn = self.lib.desk_tclaw_on_tuya_mqtt_connected()
        self.assertEqual(rc_conn, 0)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_CONNECTED)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "happy")
        self.assertEqual(text_buf.value.decode("utf-8"), "Online")

        # Audio playback lock must be released (arbiter clean)
        self.assertEqual(self.lib.desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK), DESK_OWNER_NONE)

        # 3. Disconnected
        rc_disc = self.lib.desk_tclaw_on_tuya_mqtt_disconnected()
        self.assertEqual(rc_disc, 0)
        self.assertEqual(self.lib.desk_tclaw_tuya_session_get_connection_state(), TUYA_CLOUD_DISCONNECTED)
        self.lib.desk_hardware_display_get_expression(preset_buf, 64, text_buf, 256)
        self.assertEqual(preset_buf.value.decode("utf-8"), "offline")
        self.assertEqual(text_buf.value.decode("utf-8"), "Offline")

    def test_dialogue_motion_voice_intent_routing(self) -> None:
        """Verifies voice dialogue intent routing for motion prompts dispatches ctrl.motion.move."""
        dispatched: list[dict] = []

        @TOOL_DISPATCH_CB
        def dispatcher(env_ptr, user_data):
            raw = ctypes.string_at(env_ptr).decode("utf-8")
            dispatched.append(json.loads(raw))
            return 0

        self.lib.desk_tclaw_register_tool_dispatcher(dispatcher, None)

        rc = self.lib.desk_tclaw_inject_text(b"Please look up at the ceiling")
        self.assertEqual(rc, 0)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["operation"], "ctrl.motion.move")
        self.assertEqual(dispatched[0]["arguments"]["pan_deg"], 90.0)
        self.assertEqual(dispatched[0]["arguments"]["tilt_deg"], 110.0)


if __name__ == "__main__":
    unittest.main()
