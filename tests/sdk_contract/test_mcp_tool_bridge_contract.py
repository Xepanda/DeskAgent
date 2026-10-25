"""
Contract and loopback tests for native TClaw MCP cloud tools bridge and official
SDK compatibility (TCP port 37020 & UDP port 37021).

Verifies:
1. Capabilities accurate reporting: inputs, motion, camera, audio_stream, expression, mcp_tools.
2. Request ID asynchronous correlation table tracking (dispatched, pending, accepted, completed, failed).
3. Cloud MCP tool requests for Home Assistant (ha.execute), Codex Companion (codex.submit_turn),
   and Life Services (reminder.create).
4. Response envelope loopback with normalization of "accepted", "completed" -> "succeeded", and "failed".
5. TCP 37020 wire loopback with RobotAdapter and concurrent out-of-order responses.
6. Heartbeat keepalive (sys.ping -> sys.pong), liveness tracking, and reconnect session token management.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

# C types definition
OUTBOUND_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_size_t, ctypes.c_void_p)
TOOL_RESP_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)


class TestMcpToolBridgeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_sdk_mcp_bridge.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        # Compile C shared library with gcc
        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-DDESK_SDK_TESTING",
            f"-I{repo_root}/firmware/components/desk_sdk_bridge/include",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"-I{repo_root}/firmware/components/desk_tclaw/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_notification.c",
            f"{repo_root}/firmware/components/desk_tclaw/src/desk_tclaw_bridge.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_watcher_log_source.c",
            f"{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c",
            f"{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c",
            "-o",
            cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile desk_sdk_bridge shared library: {res.stderr}")

        cls.lib = ctypes.CDLL(cls.so_path)

        # Bridge API signatures
        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_init.argtypes = []

        cls.lib.desk_tclaw_init.restype = ctypes.c_int
        cls.lib.desk_tclaw_init.argtypes = []

        cls.lib.desk_sdk_bridge_init.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_init.argtypes = []

        cls.lib.desk_sdk_bridge_reset.restype = None
        cls.lib.desk_sdk_bridge_reset.argtypes = []

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

        cls.lib.desk_sdk_bridge_wait_tool_response.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_wait_tool_response.argtypes = [
            ctypes.c_char_p, ctypes.c_uint32,
            ctypes.c_char_p, ctypes.c_size_t,
            ctypes.c_char_p, ctypes.c_size_t,
        ]
        cls.lib.desk_sdk_test_pending_result_bytes.restype = ctypes.c_size_t
        cls.lib.desk_sdk_test_pending_result_bytes.argtypes = []

        cls.lib.desk_sdk_bridge_get_last_heartbeat_time.restype = ctypes.c_uint32
        cls.lib.desk_sdk_bridge_get_last_heartbeat_time.argtypes = []

        cls.lib.desk_sdk_bridge_is_heartbeat_alive.restype = ctypes.c_bool
        cls.lib.desk_sdk_bridge_is_heartbeat_alive.argtypes = [ctypes.c_uint32]

        cls.lib.desk_sdk_bridge_process_command.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_command.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_sdk_bridge_process_pairing_udp.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_pairing_udp.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_sdk_bridge_set_pairing_code.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_set_pairing_code.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_set_pairing_token.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_set_pairing_token.argtypes = [ctypes.c_char_p]

        cls.lib.desk_sdk_bridge_get_pairing_token.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_get_pairing_token.argtypes = [ctypes.c_char_p, ctypes.c_size_t]

        cls.lib.desk_sdk_bridge_set_auth_required.restype = None
        cls.lib.desk_sdk_bridge_set_auth_required.argtypes = [ctypes.c_bool]

        cls.lib.desk_sdk_socket_server_start.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_start.argtypes = [ctypes.c_uint16]

        cls.lib.desk_sdk_socket_server_poll.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_poll.argtypes = [ctypes.c_int]

        cls.lib.desk_sdk_socket_server_stop.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_stop.argtypes = []

        cls.lib.desk_sdk_socket_server_get_port.restype = ctypes.c_int
        cls.lib.desk_sdk_socket_server_get_port.argtypes = []

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_hardware_init()
        self.lib.desk_tclaw_init()
        self.lib.desk_sdk_bridge_init()
        self.lib.desk_sdk_bridge_reset()
        self.lib.desk_sdk_bridge_clear_pending_requests()

    def tearDown(self) -> None:
        self.lib.desk_sdk_socket_server_stop()
        self.lib.desk_sdk_bridge_reset()

    def _exec_command(self, cmd_dict: dict) -> dict:
        in_bytes = (json.dumps(cmd_dict) + "\n").encode("utf-8")
        out_buf = ctypes.create_string_buffer(4096)
        rc = self.lib.desk_sdk_bridge_process_command(in_bytes, out_buf, ctypes.sizeof(out_buf))
        self.assertEqual(rc, 0)
        return json.loads(out_buf.value.decode("utf-8"))

    # -------------------------------------------------------------------------
    # 1. Capability Dictionary Reporting
    # -------------------------------------------------------------------------
    def test_capabilities_accurate_reporting(self) -> None:
        """Verifies implemented capabilities are reported and unsupported audio is omitted."""
        resp = self._exec_command({
            "type": "sys.client.hello",
            "data": {"role": "desktop", "version": "1.0"},
        })
        self.assertEqual(resp.get("type"), "sys.ack")
        self.assertEqual(resp.get("code"), 0)
        caps = resp.get("data", {}).get("capabilities", [])

        # Inputs means SDK roller events only; button and touch inputs are not included.
        self.assertEqual(caps, ["motion", "camera", "expression.audio_follow.v1", "expression", "mcp_tools", "inputs"])
        self.assertNotIn("audio_stream", caps)
        self.assertNotIn("audio.stream", caps)
        self.assertIn("mcp_tools", caps)

        # Check sys.sdk.ready.get reports identical capabilities
        resp_ready = self._exec_command({
            "type": "sys.sdk.ready.get",
            "data": {"command_id": "ready-check-01"},
        })
        self.assertEqual(resp_ready.get("type"), "sys.ack")
        self.assertEqual(resp_ready.get("data", {}).get("capabilities"), caps)

    # -------------------------------------------------------------------------
    # 2. HA MCP Tool Dispatch & Completed Response
    # -------------------------------------------------------------------------
    def test_ha_mcp_tool_dispatch_and_completed_loopback(self) -> None:
        """Verifies HA execute tool dispatch, correlation table tracking, and completed response normalization."""
        outbound_requests: list[str] = []
        @OUTBOUND_CB
        def on_outbound(data_ptr, length, user_data):
            outbound_requests.append(ctypes.string_at(data_ptr, length).decode("utf-8"))
            return 0

        received_responses: list[dict] = []
        @TOOL_RESP_CB
        def on_tool_response(resp_ptr, user_data):
            raw = ctypes.string_at(resp_ptr).decode("utf-8")
            received_responses.append(json.loads(raw))
            return 0

        self.lib.desk_sdk_bridge_register_outbound(on_outbound, None)
        self.lib.desk_sdk_bridge_register_tool_response_cb(on_tool_response, None)

        req_id = "req-mcp-ha-turn-on-light-01"
        req_json = json.dumps({
            "schema_version": "1.0.0",
            "request_id": req_id,
            "operation": "ha.execute",
            "arguments": {
                "domain": "light",
                "service": "turn_on",
                "target": {"entity_id": "light.desk_lamp"},
            },
            "expires_at": "2030-01-01T00:00:00Z",
        })

        rc_disp = self.lib.desk_sdk_bridge_dispatch_tool_request(req_json.encode("utf-8"))
        self.assertEqual(rc_disp, 0)
        self.assertEqual(len(outbound_requests), 1)

        # Check correlation table has this request pending
        self.assertTrue(self.lib.desk_sdk_bridge_has_pending_request(req_id.encode("utf-8")))
        self.assertEqual(self.lib.desk_sdk_bridge_get_pending_request_count(), 1)
        st_buf = ctypes.create_string_buffer(32)
        self.lib.desk_sdk_bridge_get_request_status(req_id.encode("utf-8"), st_buf, ctypes.sizeof(st_buf))
        self.assertEqual(st_buf.value.decode("utf-8"), "pending")

        # Host returns "completed" status (as used by some MCP orchestrators)
        host_resp = json.dumps({
            "schema_version": "1.0.0",
            "request_id": req_id,
            "status": "completed",
            "timestamp": "2026-10-19T10:00:00Z",
            "result": {"state": "on", "brightness": 255},
        })
        rc_resp = self.lib.desk_sdk_bridge_handle_tool_response(host_resp.encode("utf-8"))
        self.assertEqual(rc_resp, 0)
        self.assertEqual(len(received_responses), 1)

        # Normalized to "succeeded" for TClaw dialogue engine consumption
        res_env = received_responses[0]
        self.assertEqual(res_env.get("request_id"), req_id)
        self.assertEqual(res_env.get("status"), "succeeded")
        self.assertEqual(res_env.get("data", {}).get("status"), "succeeded")
        self.assertEqual(res_env.get("result", {}).get("state"), "on")

        # Correlation table marked completed
        self.lib.desk_sdk_bridge_get_request_status(req_id.encode("utf-8"), st_buf, ctypes.sizeof(st_buf))
        self.assertEqual(st_buf.value.decode("utf-8"), "completed")
        self.assertFalse(self.lib.desk_sdk_bridge_has_pending_request(req_id.encode("utf-8")))

    def test_large_utf8_result_round_trips_and_small_wait_buffer_is_retryable(self) -> None:
        req_id = "req-large-utf8-result"
        self.assertEqual(self.lib.desk_sdk_bridge_dispatch_tool_request(json.dumps({
            "schema_version": "1.0.0", "request_id": req_id,
            "operation": "test.large_result", "arguments": {},
            "expires_at": "2030-01-01T00:00:00Z",
        }).encode("utf-8")), 0)
        chinese = "设备结果已完整返回" * 80
        host_response = json.dumps({
            "schema_version": "1.0.0", "request_id": req_id,
            "status": "succeeded", "result": {"message": chinese},
        }, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(host_response), 512)
        self.assertLessEqual(len(host_response), 12 * 1024)
        self.assertEqual(self.lib.desk_sdk_bridge_handle_tool_response(host_response), 0)

        status = ctypes.create_string_buffer(32)
        too_small = ctypes.create_string_buffer(512)
        rc = self.lib.desk_sdk_bridge_wait_tool_response(
            req_id.encode("utf-8"), 1, status, len(status), too_small, len(too_small)
        )
        self.assertEqual(rc, -2, "undersized buffers must fail without consuming a response")

        result = ctypes.create_string_buffer(16 * 1024 + 1)
        rc = self.lib.desk_sdk_bridge_wait_tool_response(
            req_id.encode("utf-8"), 1, status, len(status), result, len(result)
        )
        self.assertEqual(rc, 0)
        self.assertEqual(status.value.decode("utf-8"), "succeeded")
        self.assertEqual(json.loads(result.value.decode("utf-8")), {"message": chinese})
        self.assertEqual(self.lib.desk_sdk_test_pending_result_bytes(), 0)

    def test_12k_host_page_result_is_preserved(self) -> None:
        req_id = "req-large-page-result"
        self.assertEqual(self.lib.desk_sdk_bridge_dispatch_tool_request(json.dumps({
            "schema_version": "1.0.0", "request_id": req_id,
            "operation": "codex.page.read", "arguments": {},
            "expires_at": "2030-01-01T00:00:00Z",
        }).encode("utf-8")), 0)
        page = "x" * (10 * 1024)
        host_response = json.dumps({
            "schema_version": "1.0.0", "request_id": req_id,
            "status": "succeeded", "result": {"page": page},
        }, ensure_ascii=False).encode("utf-8")
        self.assertGreater(len(host_response), 10 * 1024)
        self.assertLessEqual(len(host_response), 12 * 1024)
        self.assertEqual(self.lib.desk_sdk_bridge_handle_tool_response(host_response), 0)

        status = ctypes.create_string_buffer(32)
        result = ctypes.create_string_buffer(16 * 1024 + 1)
        self.assertEqual(self.lib.desk_sdk_bridge_wait_tool_response(
            req_id.encode("utf-8"), 1, status, len(status), result, len(result)
        ), 0)
        self.assertEqual(json.loads(result.value.decode("utf-8")), {"page": page})

    def test_oversize_host_envelope_is_rejected_without_completing_request(self) -> None:
        req_id = "req-oversize-envelope"
        self.assertEqual(self.lib.desk_sdk_bridge_dispatch_tool_request(json.dumps({
            "schema_version": "1.0.0", "request_id": req_id,
            "operation": "test.oversize", "arguments": {},
            "expires_at": "2030-01-01T00:00:00Z",
        }).encode("utf-8")), 0)
        oversized = json.dumps({
            "schema_version": "1.0.0", "request_id": req_id,
            "status": "succeeded", "result": {"page": "x" * (13 * 1024)},
        }).encode("utf-8")
        self.assertGreater(len(oversized), 12 * 1024)
        self.assertEqual(self.lib.desk_sdk_bridge_handle_tool_response(oversized), -3)
        status = ctypes.create_string_buffer(32)
        self.assertEqual(self.lib.desk_sdk_bridge_get_request_status(
            req_id.encode("utf-8"), status, len(status)
        ), 0)
        self.assertEqual(status.value.decode("utf-8"), "pending")
        self.assertEqual(self.lib.desk_sdk_test_pending_result_bytes(), 0)

    def test_timeout_clear_and_late_generation_response_release_large_results(self) -> None:
        req_id = "req-timeout-release-result"
        self.assertEqual(self.lib.desk_sdk_bridge_dispatch_tool_request(json.dumps({
            "schema_version": "1.0.0", "request_id": req_id,
            "operation": "test.timeout", "arguments": {},
            "expires_at": "2030-01-01T00:00:00Z",
        }).encode("utf-8")), 0)
        accepted = json.dumps({
            "schema_version": "1.0.0", "request_id": req_id,
            "status": "accepted", "result": {"working": "y" * 1200},
        }).encode("utf-8")
        self.assertEqual(self.lib.desk_sdk_bridge_handle_tool_response(accepted), 0)
        self.assertGreater(self.lib.desk_sdk_test_pending_result_bytes(), 512)
        time.sleep(0.01)
        self.assertEqual(self.lib.desk_sdk_bridge_cleanup_expired_requests(1), 1)
        self.assertEqual(self.lib.desk_sdk_test_pending_result_bytes(), 0)

        # Session generation teardown clears remaining stored payloads; a late
        # response from the old connection is no longer correlated.
        self.assertEqual(self.lib.desk_sdk_bridge_dispatch_tool_request(json.dumps({
            "schema_version": "1.0.0", "request_id": req_id + "-next",
            "operation": "test.disconnect", "arguments": {},
            "expires_at": "2030-01-01T00:00:00Z",
        }).encode("utf-8")), 0)
        pending_response = json.dumps({
            "schema_version": "1.0.0", "request_id": req_id + "-next",
            "status": "succeeded", "result": {"body": "z" * 900},
        }).encode("utf-8")
        self.assertEqual(self.lib.desk_sdk_bridge_handle_tool_response(pending_response), 0)
        self.assertGreater(self.lib.desk_sdk_test_pending_result_bytes(), 0)
        self.assertEqual(self.lib.desk_sdk_bridge_clear_pending_requests(), 0)
        self.assertEqual(self.lib.desk_sdk_test_pending_result_bytes(), 0)
        self.assertNotEqual(self.lib.desk_sdk_bridge_handle_tool_response(pending_response), 0)

    # -------------------------------------------------------------------------
    # 3. Codex MCP Tool Two-Phase Turn (Accepted -> Completed)
    # -------------------------------------------------------------------------
    def test_codex_mcp_tool_two_phase_turn_loopback(self) -> None:
        """Verifies Codex submit_turn dispatch through accepted intermediate and completed final state."""
        received_responses: list[dict] = []
        @TOOL_RESP_CB
        def on_tool_response(resp_ptr, user_data):
            raw = ctypes.string_at(resp_ptr).decode("utf-8")
            received_responses.append(json.loads(raw))
            return 0

        self.lib.desk_sdk_bridge_register_tool_response_cb(on_tool_response, None)

        req_id = "req-mcp-codex-turn-42"
        req_json = json.dumps({
            "schema_version": "1.0.0",
            "request_id": req_id,
            "operation": "codex.submit_turn",
            "arguments": {"prompt": "Fix race condition in driver", "workspace": "firmware"},
            "expires_at": "2030-01-01T00:00:00Z",
        })

        rc_disp = self.lib.desk_sdk_bridge_dispatch_tool_request(req_json.encode("utf-8"))
        self.assertEqual(rc_disp, 0)
        self.assertTrue(self.lib.desk_sdk_bridge_has_pending_request(req_id.encode("utf-8")))

        # Phase 1: Host acknowledges turn start with "accepted"
        accepted_resp = json.dumps({
            "type": "tool.response",
            "request_id": req_id,
            "data": {
                "schema_version": "1.0.0",
                "request_id": req_id,
                "status": "accepted",
                "result": {"turn_id": "turn-42-sub1", "state": "executing"},
            },
        })
        rc_phase1 = self.lib.desk_sdk_bridge_handle_tool_response(accepted_resp.encode("utf-8"))
        self.assertEqual(rc_phase1, 0)
        self.assertEqual(len(received_responses), 1)
        self.assertEqual(received_responses[0].get("status"), "accepted")

        # Request is still active in accepted state
        st_buf = ctypes.create_string_buffer(32)
        self.lib.desk_sdk_bridge_get_request_status(req_id.encode("utf-8"), st_buf, ctypes.sizeof(st_buf))
        self.assertEqual(st_buf.value.decode("utf-8"), "accepted")
        self.assertTrue(self.lib.desk_sdk_bridge_has_pending_request(req_id.encode("utf-8")))

        # Phase 2: Host finishes turn and replies "succeeded"
        final_resp = json.dumps({
            "type": "tool.response",
            "request_id": req_id,
            "data": {
                "schema_version": "1.0.0",
                "request_id": req_id,
                "status": "succeeded",
                "result": {"turn_id": "turn-42-sub1", "state": "done", "files_changed": 1},
            },
        })
        rc_phase2 = self.lib.desk_sdk_bridge_handle_tool_response(final_resp.encode("utf-8"))
        self.assertEqual(rc_phase2, 0)
        self.assertEqual(len(received_responses), 2)
        self.assertEqual(received_responses[1].get("status"), "succeeded")
        self.assertEqual(received_responses[1].get("result", {}).get("files_changed"), 1)

        # Request completed
        self.assertFalse(self.lib.desk_sdk_bridge_has_pending_request(req_id.encode("utf-8")))

    # -------------------------------------------------------------------------
    # 4. Life Service MCP Tool Failed Handling
    # -------------------------------------------------------------------------
    def test_life_reminder_mcp_tool_failed_loopback(self) -> None:
        """Verifies Life reminder tool failure returns standardized failed envelope with error payload."""
        received_responses: list[dict] = []
        @TOOL_RESP_CB
        def on_tool_response(resp_ptr, user_data):
            raw = ctypes.string_at(resp_ptr).decode("utf-8")
            received_responses.append(json.loads(raw))
            return 0

        self.lib.desk_sdk_bridge_register_tool_response_cb(on_tool_response, None)

        req_id = "req-mcp-life-rem-expired"
        req_json = json.dumps({
            "schema_version": "1.0.0",
            "request_id": req_id,
            "operation": "reminder.create",
            "arguments": {"title": "Expired standup", "due_at": "2020-01-01T00:00:00Z"},
            "expires_at": "2030-01-01T00:00:00Z",
        })

        rc_disp = self.lib.desk_sdk_bridge_dispatch_tool_request(req_json.encode("utf-8"))
        self.assertEqual(rc_disp, 0)

        # Host reports failure
        fail_resp = json.dumps({
            "schema_version": "1.0.0",
            "request_id": req_id,
            "status": "failed",
            "error": {
                "code": "INVALID_ARGUMENT",
                "message": "due_at cannot be in the past",
                "retryable": False,
            },
        })
        rc_fail = self.lib.desk_sdk_bridge_handle_tool_response(fail_resp.encode("utf-8"))
        self.assertEqual(rc_fail, 0)
        self.assertEqual(len(received_responses), 1)

        res_env = received_responses[0]
        self.assertEqual(res_env.get("request_id"), req_id)
        self.assertEqual(res_env.get("status"), "failed")
        self.assertIn("error", res_env)
        self.assertEqual(res_env["error"].get("code"), "INVALID_ARGUMENT")

        st_buf = ctypes.create_string_buffer(32)
        self.lib.desk_sdk_bridge_get_request_status(req_id.encode("utf-8"), st_buf, ctypes.sizeof(st_buf))
        self.assertEqual(st_buf.value.decode("utf-8"), "failed")

    # -------------------------------------------------------------------------
    # 5. Concurrent Asynchronous Request ID Table Correlation
    # -------------------------------------------------------------------------
    def test_concurrent_async_request_id_correlation(self) -> None:
        """Verifies multiple concurrent tool requests receive out-of-order responses correlated accurately."""
        received_responses: list[dict] = []
        @TOOL_RESP_CB
        def on_tool_response(resp_ptr, user_data):
            raw = ctypes.string_at(resp_ptr).decode("utf-8")
            received_responses.append(json.loads(raw))
            return 0

        self.lib.desk_sdk_bridge_register_tool_response_cb(on_tool_response, None)

        # Dispatch 3 distinct tools
        req_ids = ["req-multi-ha", "req-multi-codex", "req-multi-life"]
        operations = ["ha.execute", "codex.submit_turn", "reminder.create"]

        for rid, op in zip(req_ids, operations):
            req_data = json.dumps({
                "schema_version": "1.0.0",
                "request_id": rid,
                "operation": op,
                "arguments": {"test": 1},
                "expires_at": "2030-01-01T00:00:00Z",
            })
            self.lib.desk_sdk_bridge_dispatch_tool_request(req_data.encode("utf-8"))

        self.assertEqual(self.lib.desk_sdk_bridge_get_pending_request_count(), 3)
        for rid in req_ids:
            self.assertTrue(self.lib.desk_sdk_bridge_has_pending_request(rid.encode("utf-8")))

        # Return responses in reverse order: life, then ha, then codex
        self.lib.desk_sdk_bridge_handle_tool_response(json.dumps({
            "schema_version": "1.0.0",
            "request_id": "req-multi-life",
            "status": "succeeded",
            "result": {"reminder_id": "rem-101"},
        }).encode("utf-8"))

        self.lib.desk_sdk_bridge_handle_tool_response(json.dumps({
            "schema_version": "1.0.0",
            "request_id": "req-multi-ha",
            "status": "completed",
            "result": {"switched": True},
        }).encode("utf-8"))

        self.lib.desk_sdk_bridge_handle_tool_response(json.dumps({
            "schema_version": "1.0.0",
            "request_id": "req-multi-codex",
            "status": "failed",
            "error": {"code": "TIMEOUT", "message": "Worker timed out"},
        }).encode("utf-8"))

        self.assertEqual(len(received_responses), 3)
        resp_map = {r.get("request_id"): r for r in received_responses}

        self.assertEqual(resp_map["req-multi-life"]["status"], "succeeded")
        self.assertEqual(resp_map["req-multi-ha"]["status"], "succeeded")  # Normalized from completed
        self.assertEqual(resp_map["req-multi-codex"]["status"], "failed")

        # All pending requests are resolved
        self.assertEqual(self.lib.desk_sdk_bridge_get_pending_request_count(), 0)

    # -------------------------------------------------------------------------
    # 6. Live TCP Socket Tool Dispatch & Response Loopback
    # -------------------------------------------------------------------------
    def test_live_tcp_tool_dispatch_and_response_loopback(self) -> None:
        """Verifies tool request dispatch and response loopback over live TCP 37020 socket."""
        rc = self.lib.desk_sdk_socket_server_start(0)
        self.assertEqual(rc, 0)
        port = self.lib.desk_sdk_socket_server_get_port()

        received_on_bridge: list[dict] = []
        @TOOL_RESP_CB
        def on_bridge_response(resp_ptr, user_data):
            raw = ctypes.string_at(resp_ptr).decode("utf-8")
            received_on_bridge.append(json.loads(raw))
            return 0

        self.lib.desk_sdk_bridge_register_tool_response_cb(on_bridge_response, None)

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("127.0.0.1", port))
        sock.settimeout(2.0)
        self.lib.desk_sdk_socket_server_poll(50)

        # Step 1: Handshake
        hello = json.dumps({"type": "sys.client.hello", "data": {"role": "desktop", "version": "1.0"}}) + "\n"
        sock.sendall(hello.encode("utf-8"))
        self.lib.desk_sdk_socket_server_poll(50)
        hello_ack = json.loads(sock.recv(4096).decode("utf-8").strip())
        self.assertEqual(hello_ack.get("type"), "sys.ack")
        self.assertIn("mcp_tools", hello_ack.get("data", {}).get("capabilities", []))

        # Step 2: Firmware dispatches MCP tool request over socket
        req_id = "req-live-mcp-ha-01"
        tool_req = json.dumps({
            "schema_version": "1.0.0",
            "request_id": req_id,
            "operation": "ha.get_state",
            "arguments": {"entity_id": "sensor.desk_temperature"},
            "expires_at": "2030-01-01T00:00:00Z",
        })
        self.assertEqual(self.lib.desk_sdk_bridge_dispatch_tool_request(tool_req.encode("utf-8")), 0)

        # Step 3: Client reads tool request from socket
        raw_req = sock.recv(4096).decode("utf-8").strip()
        client_req = json.loads(raw_req)
        self.assertEqual(client_req.get("request_id"), req_id)
        self.assertEqual(client_req.get("operation"), "ha.get_state")

        # Step 4: Client replies with tool response
        client_resp = json.dumps({
            "type": "tool.response",
            "request_id": req_id,
            "data": {
                "schema_version": "1.0.0",
                "request_id": req_id,
                "status": "completed",
                "result": {"temperature": 24.5, "unit": "°C"},
            },
        }) + "\n"
        sock.sendall(client_resp.encode("utf-8"))

        # Step 5: Poll server and verify ACK received by client
        self.lib.desk_sdk_socket_server_poll(50)
        raw_ack = sock.recv(4096).decode("utf-8").strip()
        ack = json.loads(raw_ack)
        self.assertEqual(ack.get("type"), "sys.ack")
        self.assertEqual(ack.get("request_id"), req_id)
        self.assertEqual(ack.get("data", {}).get("status"), "received")

        # Step 6: Verify bridge invoked registered callback with normalized response
        self.assertEqual(len(received_on_bridge), 1)
        self.assertEqual(received_on_bridge[0].get("request_id"), req_id)
        self.assertEqual(received_on_bridge[0].get("status"), "succeeded")
        self.assertEqual(received_on_bridge[0].get("result", {}).get("temperature"), 24.5)

        sock.close()

    # -------------------------------------------------------------------------
    # 7. Heartbeat Keepalive & Liveness Tracking
    # -------------------------------------------------------------------------
    def test_heartbeat_keepalive_and_liveness(self) -> None:
        """Verifies sys.ping updates heartbeat timestamp and liveness check reflects connection status."""
        self.assertFalse(self.lib.desk_sdk_bridge_is_heartbeat_alive(10))

        ping_resp = self._exec_command({
            "type": "sys.ping",
            "request_id": "ping-req-007",
            "data": {},
        })
        self.assertEqual(ping_resp.get("type"), "sys.pong")
        self.assertEqual(ping_resp.get("request_id"), "ping-req-007")

        hb_time = self.lib.desk_sdk_bridge_get_last_heartbeat_time()
        self.assertGreater(hb_time, 0)
        self.assertTrue(self.lib.desk_sdk_bridge_is_heartbeat_alive(60))

    # -------------------------------------------------------------------------
    # 8. UDP 37021 Pairing, Session Token & Reconnect Compatibility
    # -------------------------------------------------------------------------
    def test_udp_pairing_and_session_token_reconnect(self) -> None:
        """Verifies UDP 37021 fail-closed pairing validation and session token persistence across reconnects."""
        udp_out = ctypes.create_string_buffer(2048)

        # 1. Unprovisioned pairing code -> fail-closed rejection
        pair_req = json.dumps({
            "protocol": "watcher_lan_pairing_v1",
            "request_id": "0123456789abcdef0123456789abcdef",
            "daemon_instance_id": "abcdef0123456789abcdef0123456789",
            "target_mode": "python_sdk",
            "websocket_port": 37020,
            "pairing_code": "123456",
        }).encode("utf-8")
        rc_rej = self.lib.desk_sdk_bridge_process_pairing_udp(pair_req, udp_out, ctypes.sizeof(udp_out))
        self.assertEqual(rc_rej, 1)  # DESK_SDK_PAIRING_REJECTED

        # 2. Provision valid 6-digit pairing code
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"888888"), 0)

        # 3. Wrong pairing code -> rejected
        pair_req_bad = json.dumps({
            "protocol": "watcher_lan_pairing_v1",
            "request_id": "1123456789abcdef0123456789abcdef",
            "daemon_instance_id": "abcdef0123456789abcdef0123456789",
            "target_mode": "python_sdk",
            "websocket_port": 37020,
            "pairing_code": "111111",
        }).encode("utf-8")
        self.assertEqual(self.lib.desk_sdk_bridge_process_pairing_udp(pair_req_bad, udp_out, ctypes.sizeof(udp_out)), 1)

        # 4. Valid pairing code -> accepted with 64-char hex session token
        pair_req_ok = json.dumps({
            "protocol": "watcher_lan_pairing_v1",
            "request_id": "2123456789abcdef0123456789abcdef",
            "daemon_instance_id": "abcdef0123456789abcdef0123456789",
            "target_mode": "python_sdk",
            "websocket_port": 37020,
            "pairing_code": "888888",
        }).encode("utf-8")
        rc_ok = self.lib.desk_sdk_bridge_process_pairing_udp(pair_req_ok, udp_out, ctypes.sizeof(udp_out))
        self.assertEqual(rc_ok, 0)  # DESK_SDK_PAIRING_OK
        pair_accept = json.loads(udp_out.value.decode("utf-8"))
        self.assertEqual(pair_accept.get("type"), "pair.accept")
        token = pair_accept.get("session_token")
        self.assertIsNotNone(token)
        self.assertEqual(len(token), 64)

        # 5. Enable mandatory auth check
        self.lib.desk_sdk_bridge_set_auth_required(True)

        # 6. Hello without valid token -> 401
        hello_bad = self._exec_command({
            "type": "sys.client.hello",
            "session_token": "wrongtokenwrongtokenwrongtokenwrongtokenwrongtokenwrongtokenwrong",
        })
        self.assertEqual(hello_bad.get("code"), 401)

        # 7. Hello with matching session token -> code 0 OK
        hello_good = self._exec_command({
            "type": "sys.client.hello",
            "session_token": token,
        })
        self.assertEqual(hello_good.get("code"), 0)

        # 8. Client reconnects with same session token -> accepted without re-pairing
        hello_reconnect = self._exec_command({
            "type": "sys.client.hello",
            "session_token": token,
        })
        self.assertEqual(hello_reconnect.get("code"), 0)


if __name__ == "__main__":
    unittest.main()
