"""
Unit and contract tests for Desk Agent R1 TClaw Dialogue Engine and Tool Gateway.
Compiles and loads the C shared library for desk_hardware, desk_arbiter, and desk_tclaw_bridge,
verifying dialogue turn state machine, audio playback acquisition, user speech/touch interruption,
and MCP external tool RequestEnvelope dispatch / ResponseEnvelope handling.
"""

from __future__ import annotations

import ctypes
import json
import os
import subprocess
import tempfile
import unittest

TCLAW_STATE_IDLE = 0
TCLAW_STATE_LISTENING = 1
TCLAW_STATE_THINKING = 2
TCLAW_STATE_SPEAKING = 3
TCLAW_STATE_TOOL_CALLING = 4

DESK_RES_AUDIO_PLAYBACK = 0
DESK_OWNER_NONE = 0
DESK_OWNER_TCLAW_VOICE = 2

TOOL_DISPATCH_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)


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

        cls.lib.desk_arbiter_get_owner.restype = ctypes.c_int
        cls.lib.desk_arbiter_get_owner.argtypes = [ctypes.c_int]

        cls.lib.desk_tclaw_init.restype = ctypes.c_int
        cls.lib.desk_tclaw_init.argtypes = []

        cls.lib.desk_tclaw_start.restype = ctypes.c_int
        cls.lib.desk_tclaw_start.argtypes = []

        cls.lib.desk_tclaw_get_state.restype = ctypes.c_int
        cls.lib.desk_tclaw_get_state.argtypes = []

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

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_hardware_init()
        self.lib.desk_tclaw_init()

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

        # End of turn
        self.lib.desk_tclaw_interrupt()
        self.assertEqual(self.lib.desk_tclaw_get_state(), TCLAW_STATE_IDLE)
