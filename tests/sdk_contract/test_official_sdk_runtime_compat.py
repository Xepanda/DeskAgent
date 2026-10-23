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

import asyncio
import ctypes
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import socket
import threading
import time
import types
import unittest
from pathlib import Path

# Import official WatcheRobot SDK from the locked path
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_OFFICIAL_SDK_SRC = REPO_ROOT / ".target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk/src"
OFFICIAL_SDK_PATH = Path(os.environ.get("WATCHEROBOT_SDK_SRC", str(DEFAULT_OFFICIAL_SDK_SRC)))
if OFFICIAL_SDK_PATH.is_dir() and str(OFFICIAL_SDK_PATH) not in sys.path:
    sys.path.insert(0, str(OFFICIAL_SDK_PATH))

try:
    from watcherobot.protocol import (
        FLAG_FIRST,
        FLAG_LAST,
        FRAME_AUDIO,
        FRAME_IMAGE,
        BinaryFrame,
        build_wspk,
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
    import websockets
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


def _jpeg_dimensions(payload: bytes) -> tuple[int, int]:
    """Read dimensions from a baseline JPEG SOF0 segment without Pillow."""
    if not payload.startswith(b"\xff\xd8") or not payload.endswith(b"\xff\xd9"):
        raise ValueError("payload is not a complete JPEG")
    sof = payload.find(b"\xff\xc0")
    if sof < 0 or sof + 9 > len(payload):
        raise ValueError("JPEG does not contain a complete baseline SOF0 segment")
    segment_length = int.from_bytes(payload[sof + 2 : sof + 4], "big")
    if segment_length < 7 or sof + 2 + segment_length > len(payload):
        raise ValueError("JPEG SOF0 segment has an invalid length")
    height = int.from_bytes(payload[sof + 5 : sof + 7], "big")
    width = int.from_bytes(payload[sof + 7 : sof + 9], "big")
    return width, height


@unittest.skipUnless(HAVE_WATCHEROBOT, "Official watcherobot python sdk not installed in environment")
class TestOfficialSdkRuntimeCompat(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_sdk_bridge_compat.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        # The production pairing path learns the Runtime's host from the source
        # address of the UDP request. This narrow test seam fills only that
        # already-validated address/port after process_pairing_udp(); the C
        # RFC6455 client, official Runtime listener and all wire traffic below
        # remain the production implementations.
        shim_path = os.path.join(cls.temp_dir.name, "sdk_runtime_loopback_shim.c")
        with open(shim_path, "w", encoding="utf-8") as shim:
            shim.write(
                f'#include "{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c"\n'
                f'#include "{repo_root}/firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c"\n'
                'int desk_sdk_test_notification_result_nack(int rc, char *out, size_t cap, const char *id) { '
                'return desk_sdk_notification_result_nack(out, cap, id, rc); }\n'
                'int desk_sdk_test_set_pairing_target(const char *host, unsigned short port) {\n'
                '  if (!host || !port || !s_paired || strlen(host) >= sizeof(s_pair_host)) return -1;\n'
                '  strcpy(s_pair_host, host); s_pair_websocket_port = port; return 0;\n'
                '}\n'
                'const char *desk_sdk_test_last_ack(void) { return (const char *)s_ws_ack; }\n'
                'const char *desk_sdk_test_http_response(void) { return s_ws_http_response; }\n'
                'int desk_sdk_test_ws_state(void) { return (int)s_ws_state; }\n'
                'void desk_sdk_test_ws_force_disconnect(void) { ws_disconnect_and_schedule(); }\n'
                'void desk_sdk_test_seed_ws_auth_latch(void) { s_ws_ready = true; s_ws_authenticated = true; }\n'
                'void desk_sdk_test_prepare_log_pair(void) { s_paired = true; '
                'strcpy(s_pair_request_id, "31a9dbf05ea3443480e62076f79a3b12"); '
                'strcpy(s_pair_daemon_instance_id, "e730f29e670c49f7a3320c4314eb9805"); '
                'strcpy(s_active_pairing_token, "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"); '
                'strcpy(s_pair_target_mode, "python_sdk"); strcpy(s_pair_host, "127.0.0.1"); '
                's_pair_websocket_port = 8765; }\n'
                'void desk_sdk_test_set_legacy_connected(void) { s_client_connected = true; }\n'
                'uint32_t desk_sdk_test_ws_generation(void) { return ws_lifecycle_generation(); }\n'
                'unsigned desk_sdk_test_ws_audio_inflight(void) { pthread_mutex_lock(&s_ws_lifecycle_mutex); '
                'unsigned count = s_ws_audio_commands_inflight; pthread_mutex_unlock(&s_ws_lifecycle_mutex); return count; }\n'
                'int desk_sdk_test_commit_hello_ack(uint32_t generation) { '
                'return ws_commit_hardware_hello_ack(generation) ? 1 : 0; }\n'
                'void desk_sdk_test_arm_hello_pending(uint32_t generation) { pthread_mutex_lock(&s_ws_lifecycle_mutex); '
                's_ws_hello_pending = true; s_ws_hello_pending_generation = generation; '
                's_ws_state = DESK_SDK_WS_HELLO_SENT; pthread_mutex_unlock(&s_ws_lifecycle_mutex); }\n'
                'int desk_sdk_test_validate_hello_ack(const char *json) { return ws_hardware_hello_ack_valid(json) ? 1 : 0; }\n'
                'int desk_sdk_test_preflight(void) { char host[64], hello[512]; uint16_t port = 0; '
                'return desk_sdk_bridge_get_pairing_target(host, sizeof(host), &port) == 0 && '
                'desk_sdk_bridge_build_hardware_hello(hello, sizeof(hello)) > 0 ? 1 : 0; }\n'
                'void desk_sdk_test_attach_writer_fd(int fd) { '
                'pthread_mutex_lock(&s_ws_lifecycle_mutex); s_ws_fd = fd; '
                's_ws_ready = true; s_ws_authenticated = true; s_ws_state = DESK_SDK_WS_CONNECTED; '
                'pthread_mutex_unlock(&s_ws_lifecycle_mutex); }\n'
                'uint32_t desk_sdk_test_advance_ws_generation(void) { '
                'pthread_mutex_lock(&s_ws_lifecycle_mutex); while (s_ws_lifecycle_closing) '
                'pthread_cond_wait(&s_ws_lifecycle_cond, &s_ws_lifecycle_mutex); '
                's_ws_lifecycle_closing = true; while (s_ws_audio_commands_inflight != 0) '
                'pthread_cond_wait(&s_ws_lifecycle_cond, &s_ws_lifecycle_mutex); '
                '++s_ws_generation; uint32_t generation = s_ws_generation; '
                's_ws_lifecycle_closing = false; pthread_cond_broadcast(&s_ws_lifecycle_cond); '
                'pthread_mutex_unlock(&s_ws_lifecycle_mutex); '
                'return generation; }\n'
                'void desk_sdk_test_disconnect_ws_generation(uint32_t generation) { '
                'ws_disconnect_generation_and_schedule(generation); }\n'
                'int desk_sdk_test_deadline_write(int fd, const uint8_t *payload, size_t len, unsigned deadline_ms) { '
                'int old_fd = s_ws_fd; s_ws_fd = fd; int rc = ws_send_frame_deadline(0x1, payload, len, deadline_ms); '
                's_ws_fd = old_fd; return rc; }\n'
                'int desk_sdk_test_binary_deadline_write(int fd, const uint8_t *payload, size_t len, unsigned deadline_ms) { '
                'int old_fd = s_ws_fd; s_ws_fd = fd; int rc = ws_send_frame_deadline(0x2, payload, len, deadline_ms); '
                's_ws_fd = old_fd; return rc; }\n'
                'int desk_sdk_test_outbound_queue_is_zero(void) { int clean = 1; '
                'pthread_mutex_lock(&s_ws_writer_mutex); '
                'for (size_t i = 0; i < sizeof(s_ws_control_queue); ++i) if (((const uint8_t *)s_ws_control_queue)[i]) clean = 0; '
                'for (size_t i = 0; i < sizeof(s_ws_writer_current); ++i) if (((const uint8_t *)&s_ws_writer_current)[i]) clean = 0; '
                'pthread_mutex_unlock(&s_ws_writer_mutex); return clean; }\n'
                'void desk_sdk_test_pause_writer_without_thread(void) { s_ws_writer_started = true; }\n'
                'void desk_sdk_test_resume_writer(void) { s_ws_writer_started = false; }\n'
                'static desk_audio_stream_status_t test_audio_status;\n'
                'static uint32_t test_audio_free_frames = 8; static int test_audio_active; static unsigned test_audio_drain_checks; '
                'static unsigned test_audio_begin_calls; static unsigned test_audio_stop_calls;\n'
                'static unsigned test_audio_stale_begin_calls; static unsigned test_audio_stale_stop_calls;\n'
                'static int test_audio_hold_stop_drain;\n'
                'static int test_audio_hal_ready;\n'
                'static pthread_mutex_t test_audio_gate_mutex = PTHREAD_MUTEX_INITIALIZER; '
                'static pthread_cond_t test_audio_gate_cond = PTHREAD_COND_INITIALIZER; '
                'static int test_audio_pause_begin, test_audio_begin_paused, test_audio_release_begin; '
                'static int test_audio_pause_stop, test_audio_stop_paused, test_audio_release_stop;\n'
                'void desk_sdk_test_audio_reset(void) { memset(&test_audio_status, 0, sizeof(test_audio_status)); '
                'test_audio_free_frames = 8; test_audio_active = 0; test_audio_drain_checks = 0; test_audio_hold_stop_drain = 0; '
                'test_audio_hal_ready = 0; test_audio_begin_calls = 0; test_audio_stop_calls = 0; '
                'test_audio_stale_begin_calls = 0; test_audio_stale_stop_calls = 0; }\n'
                'int desk_sdk_test_audio_is_active(void) { return test_audio_active; }\n'
                'unsigned desk_sdk_test_audio_begin_calls(void) { return test_audio_begin_calls; }\n'
                'unsigned desk_sdk_test_audio_stop_calls(void) { return test_audio_stop_calls; }\n'
                'unsigned desk_sdk_test_audio_stale_begin_calls(void) { return test_audio_stale_begin_calls; }\n'
                'unsigned desk_sdk_test_audio_stale_stop_calls(void) { return test_audio_stale_stop_calls; }\n'
                'void desk_sdk_test_audio_pause_next_begin(void) { pthread_mutex_lock(&test_audio_gate_mutex); '
                'test_audio_pause_begin = 1; test_audio_begin_paused = 0; test_audio_release_begin = 0; '
                'pthread_mutex_unlock(&test_audio_gate_mutex); }\n'
                'int desk_sdk_test_audio_begin_is_paused(void) { pthread_mutex_lock(&test_audio_gate_mutex); '
                'int paused = test_audio_begin_paused; pthread_mutex_unlock(&test_audio_gate_mutex); return paused; }\n'
                'void desk_sdk_test_audio_release_begin(void) { pthread_mutex_lock(&test_audio_gate_mutex); '
                'test_audio_release_begin = 1; pthread_cond_broadcast(&test_audio_gate_cond); '
                'pthread_mutex_unlock(&test_audio_gate_mutex); }\n'
                'void desk_sdk_test_audio_pause_next_stop(void) { pthread_mutex_lock(&test_audio_gate_mutex); '
                'test_audio_pause_stop = 1; test_audio_stop_paused = 0; test_audio_release_stop = 0; '
                'pthread_mutex_unlock(&test_audio_gate_mutex); }\n'
                'int desk_sdk_test_audio_stop_is_paused(void) { pthread_mutex_lock(&test_audio_gate_mutex); '
                'int paused = test_audio_stop_paused; pthread_mutex_unlock(&test_audio_gate_mutex); return paused; }\n'
                'void desk_sdk_test_audio_release_stop(void) { pthread_mutex_lock(&test_audio_gate_mutex); '
                'test_audio_release_stop = 1; pthread_cond_broadcast(&test_audio_gate_cond); '
                'pthread_mutex_unlock(&test_audio_gate_mutex); }\n'
                'void desk_sdk_test_audio_set_hal_ready(int ready) { test_audio_hal_ready = ready; }\n'
                'int __wrap_desk_hardware_audio_get_status(desk_hardware_audio_status_t *status) { '
                'if (!status) return DESK_HW_ERR_INVALID_ARG; memset(status, 0, sizeof(*status)); '
                'status->ready = test_audio_hal_ready != 0; return status->ready ? DESK_HW_OK : DESK_HW_ERR_UNAVAILABLE; }\n'
                'void desk_sdk_test_audio_hold_stop_drain(int hold) { test_audio_hold_stop_drain = hold; }\n'
                'void desk_sdk_test_audio_release_drain(void) { test_audio_status.tx_drained = true; '
                'test_audio_status.queue_depth = 0; test_audio_status.pending_frames = 0; '
                'test_audio_status.free_frames = 8; test_audio_active = 0; }\n'
                'int desk_sdk_test_audio_inject(const uint8_t *frame, size_t len, uint32_t generation) { '
                'return desk_sdk_bridge_process_ws_audio_frame(frame, len, generation); }\n'
                'int __wrap_desk_hardware_audio_stream_begin(uint64_t handle, uint32_t generation, uint32_t stream_id, '
                'uint32_t rate, uint8_t channels, uint8_t width, uint32_t total) { '
                'pthread_mutex_lock(&test_audio_gate_mutex); if (test_audio_pause_begin) { '
                'test_audio_begin_paused = 1; pthread_cond_broadcast(&test_audio_gate_cond); '
                'while (!test_audio_release_begin) pthread_cond_wait(&test_audio_gate_cond, &test_audio_gate_mutex); '
                'test_audio_pause_begin = 0; } pthread_mutex_unlock(&test_audio_gate_mutex); '
                'if (generation != ws_lifecycle_generation()) test_audio_stale_begin_calls++; '
                'test_audio_begin_calls++; if (test_audio_active || rate != 24000 || channels != 1 || width != 2 || total < 2) return DESK_HW_ERR_BUSY; '
                'memset(&test_audio_status, 0, sizeof(test_audio_status)); test_audio_status.handle = handle; '
                'test_audio_status.connection_generation = generation; test_audio_status.stream_id = stream_id; '
                'test_audio_status.total_bytes = total; test_audio_status.free_frames = test_audio_free_frames; '
                'test_audio_status.start_buffer_frames = 4; test_audio_status.state = DESK_AUDIO_STREAM_RECEIVING; '
                'test_audio_status.tx_drained = false; test_audio_active = 1; return DESK_HW_OK; }\n'
                'int __wrap_desk_hardware_audio_stream_write(uint64_t handle, uint32_t generation, uint32_t stream_id, '
                'uint32_t sequence, const uint8_t *pcm, size_t bytes, desk_audio_stream_status_t *status) { '
                '(void)pcm; if (test_audio_status.handle != handle || test_audio_status.connection_generation != generation || '
                'test_audio_status.stream_id != stream_id || test_audio_status.expected_rx_seq != sequence || '
                'test_audio_status.free_frames == 0) return DESK_HW_ERR_INVALID_ARG; '
                'test_audio_status.expected_rx_seq++; test_audio_status.accepted_bytes += (uint32_t)bytes; '
                'test_audio_status.free_frames--; test_audio_status.queue_depth++; test_audio_status.pending_frames++; '
                'if (status) *status = test_audio_status; return DESK_HW_OK; }\n'
                'int __wrap_desk_hardware_audio_stream_finish(uint64_t handle) { '
                'if (test_audio_status.handle != handle || test_audio_status.accepted_bytes != test_audio_status.total_bytes) return DESK_HW_ERR_INVALID_ARG; '
                'test_audio_status.state = DESK_AUDIO_STREAM_DRAINING; test_audio_status.tx_drained = false; '
                'test_audio_drain_checks = 0; return DESK_HW_OK; }\n'
                'int __wrap_desk_hardware_audio_stream_stop(uint64_t handle, uint32_t timeout_ms) { (void)timeout_ms; test_audio_stop_calls++; '
                'pthread_mutex_lock(&test_audio_gate_mutex); if (test_audio_pause_stop) { '
                'test_audio_stop_paused = 1; pthread_cond_broadcast(&test_audio_gate_cond); '
                'while (!test_audio_release_stop) pthread_cond_wait(&test_audio_gate_cond, &test_audio_gate_mutex); '
                'test_audio_pause_stop = 0; } pthread_mutex_unlock(&test_audio_gate_mutex); '
                'if (test_audio_status.connection_generation != ws_lifecycle_generation()) test_audio_stale_stop_calls++; '
                'if (test_audio_status.handle != handle) return DESK_HW_ERR_INVALID_ARG; '
                'if (test_audio_hold_stop_drain) { test_audio_status.state = DESK_AUDIO_STREAM_CANCELED; '
                'test_audio_status.tx_drained = false; return DESK_HW_ERR_TIMEOUT; } '
                'test_audio_status.state = DESK_AUDIO_STREAM_CANCELED; test_audio_status.tx_drained = true; '
                'test_audio_active = 0; test_audio_status.playing = false; test_audio_status.queue_depth = 0; '
                'test_audio_status.pending_frames = 0; test_audio_status.free_frames = 8; return DESK_HW_OK; }\n'
                'int __wrap_desk_hardware_audio_stream_get_status(uint64_t handle, desk_audio_stream_status_t *status) { '
                'if (!status || test_audio_status.handle != handle) return DESK_HW_ERR_INVALID_ARG; '
                'if (test_audio_status.state == DESK_AUDIO_STREAM_DRAINING && ++test_audio_drain_checks >= 2) { '
                'test_audio_status.state = DESK_AUDIO_STREAM_COMPLETE; test_audio_status.tx_drained = true; '
                'test_audio_active = 0; test_audio_status.queue_depth = 0; test_audio_status.pending_frames = 0; '
                'test_audio_status.free_frames = 8; } '
                '*status = test_audio_status; return DESK_HW_OK; }\n'
            )

        # Compile C shared library
        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-DDESK_SDK_OPENSSL_TESTING",
            "-Wl,--wrap=desk_hardware_audio_stream_begin",
            "-Wl,--wrap=desk_hardware_audio_stream_write",
            "-Wl,--wrap=desk_hardware_audio_stream_finish",
            "-Wl,--wrap=desk_hardware_audio_stream_stop",
            "-Wl,--wrap=desk_hardware_audio_stream_get_status",
            "-Wl,--wrap=desk_hardware_audio_get_status",
            f"-I{repo_root}/firmware/components/desk_sdk_bridge/include",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"-I{repo_root}/firmware/components/desk_tclaw/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_notification.c",
            f"{repo_root}/firmware/components/desk_tclaw/src/desk_tclaw_bridge.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_watcher_log_source.c",
            shim_path,
            "-lcrypto",
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

        cls.lib.desk_hardware_notification_init.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cls.lib.desk_hardware_notification_summary_acknowledge.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_summary_acknowledge.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        cls.lib.desk_sdk_test_notification_result_nack.restype = ctypes.c_int
        cls.lib.desk_sdk_test_notification_result_nack.argtypes = [
            ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p,
        ]

        cls.lib.desk_sdk_ws_client_is_connected.restype = ctypes.c_bool
        cls.lib.desk_sdk_ws_client_is_connected.argtypes = []
        cls.lib.desk_sdk_ws_client_is_authenticated.restype = ctypes.c_bool
        cls.lib.desk_sdk_ws_client_is_authenticated.argtypes = []
        cls.lib.desk_sdk_test_seed_ws_auth_latch.restype = None
        cls.lib.desk_sdk_test_seed_ws_auth_latch.argtypes = []
        cls.lib.desk_sdk_test_set_legacy_connected.restype = None
        cls.lib.desk_sdk_test_set_legacy_connected.argtypes = []
        cls.lib.desk_sdk_test_ws_generation.restype = ctypes.c_uint32
        cls.lib.desk_sdk_test_ws_generation.argtypes = []
        cls.lib.desk_sdk_test_ws_audio_inflight.restype = ctypes.c_uint
        cls.lib.desk_sdk_test_ws_audio_inflight.argtypes = []
        cls.lib.desk_sdk_test_commit_hello_ack.restype = ctypes.c_int
        cls.lib.desk_sdk_test_commit_hello_ack.argtypes = [ctypes.c_uint32]
        cls.lib.desk_sdk_test_arm_hello_pending.restype = None
        cls.lib.desk_sdk_test_arm_hello_pending.argtypes = [ctypes.c_uint32]
        cls.lib.desk_sdk_test_validate_hello_ack.restype = ctypes.c_int
        cls.lib.desk_sdk_test_validate_hello_ack.argtypes = [ctypes.c_char_p]
        cls.lib.desk_sdk_ws_client_send_text.restype = ctypes.c_int
        cls.lib.desk_sdk_ws_client_send_text.argtypes = [ctypes.c_char_p]
        cls.lib.desk_sdk_ws_client_connect_paired.restype = ctypes.c_int
        cls.lib.desk_sdk_ws_client_connect_paired.argtypes = []
        cls.lib.desk_sdk_ws_client_poll.restype = ctypes.c_int
        cls.lib.desk_sdk_ws_client_poll.argtypes = [ctypes.c_int]

        cls.lib.desk_sdk_bridge_process_command.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_command.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.c_size_t,
        ]
        cls.lib.desk_sdk_bridge_process_ws_command.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_ws_command.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
        cls.lib.desk_sdk_bridge_process_ws_command_for_generation.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_process_ws_command_for_generation.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32
        ]
        cls.lib.desk_sdk_bridge_set_local_log_enabled.restype = None
        cls.lib.desk_sdk_bridge_set_local_log_enabled.argtypes = [ctypes.c_bool]
        cls.lib.desk_watcher_log_source_publish_camera_capture_empty.restype = ctypes.c_bool
        cls.lib.desk_watcher_log_source_publish_camera_capture_empty.argtypes = []
        cls.lib.desk_watcher_log_source_publish_audio_driver_state.restype = ctypes.c_bool
        cls.lib.desk_watcher_log_source_publish_audio_driver_state.argtypes = [ctypes.c_bool]
        cls.lib.desk_watcher_log_source_publish_tuya_mqtt_state.restype = ctypes.c_bool
        cls.lib.desk_watcher_log_source_publish_tuya_mqtt_state.argtypes = [ctypes.c_bool]
        cls.lib.desk_watcher_log_source_publish_stm32_ack_timeout.restype = ctypes.c_bool
        cls.lib.desk_watcher_log_source_publish_stm32_ack_timeout.argtypes = []
        cls.lib.desk_sdk_bridge_log_next_text.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_log_next_text.argtypes = [ctypes.c_char_p, ctypes.c_size_t]
        cls.lib.desk_sdk_test_deadline_write.restype = ctypes.c_int
        cls.lib.desk_sdk_test_deadline_write.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint]
        cls.lib.desk_sdk_test_binary_deadline_write.restype = ctypes.c_int
        cls.lib.desk_sdk_test_binary_deadline_write.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint]
        cls.lib.desk_sdk_test_outbound_queue_is_zero.restype = ctypes.c_int
        cls.lib.desk_sdk_test_outbound_queue_is_zero.argtypes = []
        cls.lib.desk_sdk_test_pause_writer_without_thread.restype = None
        cls.lib.desk_sdk_test_pause_writer_without_thread.argtypes = []
        cls.lib.desk_sdk_test_resume_writer.restype = None
        cls.lib.desk_sdk_test_resume_writer.argtypes = []
        cls.lib.desk_sdk_test_attach_writer_fd.restype = None
        cls.lib.desk_sdk_test_attach_writer_fd.argtypes = [ctypes.c_int]
        cls.lib.desk_sdk_test_advance_ws_generation.restype = ctypes.c_uint32
        cls.lib.desk_sdk_test_advance_ws_generation.argtypes = []
        cls.lib.desk_sdk_test_disconnect_ws_generation.restype = None
        cls.lib.desk_sdk_test_disconnect_ws_generation.argtypes = [ctypes.c_uint32]
        cls.lib.desk_sdk_test_ws_generation.restype = ctypes.c_uint32
        cls.lib.desk_sdk_test_ws_generation.argtypes = []
        cls.lib.desk_sdk_test_audio_begin_calls.restype = ctypes.c_uint
        cls.lib.desk_sdk_test_audio_begin_calls.argtypes = []
        cls.lib.desk_sdk_test_audio_stop_calls.restype = ctypes.c_uint
        cls.lib.desk_sdk_test_audio_stop_calls.argtypes = []
        cls.lib.desk_sdk_test_audio_stale_begin_calls.restype = ctypes.c_uint
        cls.lib.desk_sdk_test_audio_stale_begin_calls.argtypes = []
        cls.lib.desk_sdk_test_audio_stale_stop_calls.restype = ctypes.c_uint
        cls.lib.desk_sdk_test_audio_stale_stop_calls.argtypes = []
        for name in ("pause_next_begin", "release_begin", "pause_next_stop", "release_stop"):
            fn = getattr(cls.lib, f"desk_sdk_test_audio_{name}")
            fn.restype = None
            fn.argtypes = []
        for name in ("begin_is_paused", "stop_is_paused"):
            fn = getattr(cls.lib, f"desk_sdk_test_audio_{name}")
            fn.restype = ctypes.c_int
            fn.argtypes = []
        cls.lib.desk_sdk_test_prepare_log_pair.restype = None
        cls.lib.desk_sdk_test_prepare_log_pair.argtypes = []

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
        cls.lib.desk_sdk_test_set_pairing_target.restype = ctypes.c_int
        cls.lib.desk_sdk_test_set_pairing_target.argtypes = [ctypes.c_char_p, ctypes.c_ushort]
        cls.lib.desk_sdk_ws_client_connect_paired.restype = ctypes.c_int
        cls.lib.desk_sdk_ws_client_connect_paired.argtypes = []
        cls.lib.desk_sdk_ws_client_poll.restype = ctypes.c_int
        cls.lib.desk_sdk_ws_client_poll.argtypes = [ctypes.c_int]
        cls.lib.desk_sdk_test_last_ack.restype = ctypes.c_char_p
        cls.lib.desk_sdk_test_last_ack.argtypes = []
        cls.lib.desk_sdk_test_http_response.restype = ctypes.c_char_p
        cls.lib.desk_sdk_test_http_response.argtypes = []
        cls.lib.desk_sdk_test_ws_state.restype = ctypes.c_int
        cls.lib.desk_sdk_test_ws_state.argtypes = []
        cls.lib.desk_sdk_test_preflight.restype = ctypes.c_int
        cls.lib.desk_sdk_test_preflight.argtypes = []
        cls.lib.desk_sdk_bridge_send_event.restype = ctypes.c_int
        cls.lib.desk_sdk_bridge_send_event.argtypes = [ctypes.c_char_p, ctypes.c_char_p]

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
        self.lib.desk_sdk_test_audio_reset()

    def _process_ws_command(self, payload: bytes, out: ctypes.Array,
                            generation: int | None = None) -> int:
        if generation is None:
            generation = self.lib.desk_sdk_test_ws_generation()
        return self.lib.desk_sdk_bridge_process_ws_command_for_generation(
            payload, out, len(out), generation
        )

    def test_websocket_client_starts_offline_and_refuses_unpaired_send(self) -> None:
        self.assertFalse(self.lib.desk_sdk_ws_client_is_connected())
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())
        self.assertNotEqual(self.lib.desk_sdk_ws_client_send_text(b'{"type":"sys.ping"}'), 0)

    def test_authenticated_latch_is_websocket_scoped_and_invalidated_by_lifecycle_changes(self) -> None:
        """Test-only latch seeding covers lifecycle invalidators; legacy TCP is never auth."""
        # Legacy TCP may report connected, but it cannot establish a paired WS latch.
        self.lib.desk_sdk_test_set_legacy_connected()
        self.assertTrue(self.lib.desk_sdk_bridge_is_connected())
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())

        # A changed token closes the old transport and clears its latch.
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.assertTrue(self.lib.desk_sdk_ws_client_is_authenticated())
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_token(b"a" * 64), 0)
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())

        # Changing auth policy also invalidates a prior connection latch.
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.lib.desk_sdk_bridge_set_auth_required(True)
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())
        self.lib.desk_sdk_bridge_set_auth_required(False)

        # A successful new pair replaces pairing metadata only after invalidation.
        self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
        request = PairRequest(
            request_id="31a9dbf05ea3443480e62076f79a3b12",
            daemon_instance_id="e730f29e670c49f7a3320c4314eb9805",
            pairing_code="123456",
            target_mode="python_sdk",
            websocket_port=8765,
        )
        response = ctypes.create_string_buffer(1024)
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.assertEqual(
            self.lib.desk_sdk_bridge_process_pairing_udp(encode_udp_message(request), response, len(response)),
            0,
        )
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())

        # Explicit reset clears the latch independently of the stored pairing state.
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.lib.desk_sdk_bridge_reset()
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())

        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.lib.desk_sdk_bridge_init()
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())

    def test_stale_inflight_hello_ack_cannot_restore_latch_after_invalidation(self) -> None:
        """A connection generation change rejects an ACK completion from the old attempt."""
        in_flight_generation = self.lib.desk_sdk_test_ws_generation()
        self.lib.desk_sdk_test_arm_hello_pending(in_flight_generation)
        self.lib.desk_sdk_ws_client_close()
        self.assertEqual(
            self.lib.desk_sdk_test_commit_hello_ack(in_flight_generation), 0
        )
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())

    def test_cr031_hello_ack_commit_requires_unique_pending_generation(self) -> None:
        generation = self.lib.desk_sdk_test_ws_generation()
        self.lib.desk_sdk_test_arm_hello_pending(generation)
        self.assertEqual(self.lib.desk_sdk_test_commit_hello_ack(generation + 1), 0)
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())
        self.assertEqual(self.lib.desk_sdk_test_commit_hello_ack(generation), 1)
        self.assertTrue(self.lib.desk_sdk_ws_client_is_authenticated())
        # A second response has no pending first-Hello correlation to consume.
        self.assertEqual(self.lib.desk_sdk_test_commit_hello_ack(generation), 0)
        self.lib.desk_sdk_ws_client_close()

    def test_cr009_ws_subscription_gate_duplicate_cancel_and_reconnect(self) -> None:
        def dispatch(message: dict) -> dict:
            out = ctypes.create_string_buffer(1024)
            rc = self.lib.desk_sdk_bridge_process_ws_command(
                json.dumps(message).encode(), out, len(out)
            )
            self.assertEqual(rc, 0)
            return json.loads(out.value)

        sub = lambda cid: {"type": "sys.log.subscribe", "code": 0, "data": {
            "command_id": cid, "log_schema_version": "1.0.0", "min_level": "WARN", "components": []}}
        self.lib.desk_sdk_test_set_legacy_connected()
        self.lib.desk_sdk_bridge_set_local_log_enabled(True)
        denied = dispatch(sub("00000000000000000000000000000001"))
        self.assertEqual((denied["type"], denied["data"]["error"]), ("sys.nack", "AUTH_REQUIRED"))

        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        first_id = "00000000000000000000000000000002"
        first = dispatch(sub(first_id))
        self.assertEqual((first["type"], first["data"]["command_id"]), ("sys.ack", first_id))
        original_stream = first["data"]["stream_id"]
        first_sequence = first["data"]["next_sequence"]
        duplicate_id = "00000000000000000000000000000003"
        duplicate = dispatch(sub(duplicate_id))
        self.assertEqual((duplicate["type"], duplicate["data"]["error"]), ("sys.nack", "ALREADY_SUBSCRIBED"))
        self.assertEqual(duplicate["data"]["command_id"], duplicate_id)

        invalid = sub("00000000000000000000000000000004")
        invalid["data"]["components"] = ["camera", "raw"]
        self.assertEqual(dispatch(invalid)["data"]["error"], "INVALID_FILTER")
        raw = sub("00000000000000000000000000000005")
        raw["data"]["components"] = ["camera\nsecret diagnostic text"]
        self.assertEqual(dispatch(raw)["data"]["error"], "INVALID_FILTER")

        cancel = {"type": "sys.log.unsubscribe", "code": 0, "data": {
            "command_id": "00000000000000000000000000000006", "log_schema_version": "1.0.0", "stream_id": original_stream}}
        self.lib.desk_watcher_log_source_publish_camera_capture_empty()
        self.lib.desk_watcher_log_source_publish_camera_capture_empty()
        self.assertEqual(dispatch(cancel)["data"]["command_id"], cancel["data"]["command_id"])
        stale = ctypes.create_string_buffer(768)
        self.assertEqual(self.lib.desk_sdk_bridge_log_next_text(stale, len(stale)), 0)
        again = dispatch(sub("00000000000000000000000000000007"))
        self.assertEqual(again["type"], "sys.ack")
        self.assertNotEqual(again["data"]["stream_id"], original_stream)
        self.assertEqual(again["data"]["next_sequence"], first_sequence + 2)

        self.lib.desk_watcher_log_source_publish_camera_capture_empty()
        self.lib.desk_sdk_ws_client_close()
        self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        resumed = dispatch(sub("00000000000000000000000000000010"))
        self.assertEqual(resumed["data"]["next_sequence"], first_sequence + 3)
        self.assertEqual(self.lib.desk_sdk_bridge_log_next_text(stale, len(stale)), 0)
        self.lib.desk_sdk_ws_client_close()
        denied_reconnect = dispatch(sub("00000000000000000000000000000008"))
        self.assertEqual(denied_reconnect["data"]["error"], "AUTH_REQUIRED")
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.assertEqual(dispatch(sub("00000000000000000000000000000009"))["type"], "sys.ack")
        self.lib.desk_sdk_bridge_set_local_log_enabled(False)

    @staticmethod
    def _notification_deliver(command_id: str = "0123456789abcdef0123456789abcdef") -> dict:
        from datetime import datetime, timedelta, timezone

        scheduled = (datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0)
        expires = scheduled + timedelta(hours=1)
        return {
            "type": "notification.deliver",
            "code": 0,
            "data": {
                "notification_id": "occurrence-sdk-001",
                "reminder_id": "reminder-sdk-001",
                "occurrence_id": "occurrence-sdk-001",
                "title": "Reminder 🗓️",
                "scheduled_at": scheduled.isoformat().replace("+00:00", "Z"),
                "expires_at": expires.isoformat().replace("+00:00", "Z"),
                "is_catchup": False,
                "command_id": command_id,
            },
        }

    def _dispatch_notification_ws(self, command: dict) -> dict:
        out = ctypes.create_string_buffer(4096)
        rc = self.lib.desk_sdk_bridge_process_ws_command(
            json.dumps(command).encode("utf-8"), out, len(out)
        )
        self.assertEqual(rc, 0, f"dispatch rc={rc} output={out.value!r}")
        return json.loads(out.value)

    def test_notification_deliver_requires_authenticated_websocket(self) -> None:
        command = self._notification_deliver()
        response = self._dispatch_notification_ws(command)
        self.assertEqual(response, {
            "type": "sys.nack",
            "code": 401,
            "data": {
                "type": "notification.deliver",
                "command_id": command["data"]["command_id"],
                "error": "AUTH_REQUIRED",
            },
        })

    def test_notification_a1_result_nack_mappings_fail_closed(self) -> None:
        command_id = "0123456789abcdef0123456789abcdef"
        mappings = (
            (-5, 507, "QUEUE_FULL"),
            (-4, 500, "DELIVERY_FAILED"),
            (-3, 410, "EXPIRED"),
            (-1, 400, "INVALID_ARGUMENT"),
            (-2, 503, "NOTIFICATION_DELIVERY_FAILED"),
            (-6, 500, "NOTIFICATION_DELIVERY_FAILED"),
            (-999, 500, "NOTIFICATION_DELIVERY_FAILED"),
        )
        for a1_rc, code, error in mappings:
            with self.subTest(a1_rc=a1_rc):
                out = ctypes.create_string_buffer(512)
                rc = self.lib.desk_sdk_test_notification_result_nack(
                    a1_rc, out, len(out), command_id.encode("ascii")
                )
                self.assertEqual(rc, 0)
                self.assertEqual(json.loads(out.value), {
                    "type": "sys.nack",
                    "code": code,
                    "data": {
                        "type": "notification.deliver",
                        "command_id": command_id,
                        "error": error,
                    },
                })
                self.assertNotIn("accepted", json.loads(out.value))

    def test_notification_deliver_valid_payload_is_accepted_by_volatile_queue(self) -> None:
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.assertEqual(self.lib.desk_hardware_notification_init(None, None), 0)
        first = self._notification_deliver("0123456789abcdef0123456789abcdef")
        retry = self._notification_deliver("fedcba9876543210fedcba9876543210")

        first_response = self._dispatch_notification_ws(first)
        retry_response = self._dispatch_notification_ws(retry)
        for command, response in ((first, first_response), (retry, retry_response)):
            self.assertEqual(response, {"type": "sys.ack", "code": 0, "data": {
                "type": "notification.deliver", "command_id": command["data"]["command_id"]}})
            self.assertNotIn("accepted", response)
            self.assertNotIn("status", response.get("data", {}))
        self.assertEqual(first["data"]["notification_id"], retry["data"]["notification_id"])
        self.assertNotEqual(first["data"]["command_id"], retry["data"]["command_id"])

    def test_notification_deliver_rejects_malformed_payload_fields(self) -> None:
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        valid = self._notification_deliver()
        malformed = []

        missing_title = json.loads(json.dumps(valid))
        del missing_title["data"]["title"]
        malformed.append(missing_title)

        extra_field = json.loads(json.dumps(valid))
        extra_field["data"]["body"] = "not approved"
        malformed.append(extra_field)

        wrong_catchup_type = json.loads(json.dumps(valid))
        wrong_catchup_type["data"]["is_catchup"] = "false"
        malformed.append(wrong_catchup_type)

        mismatched_id = json.loads(json.dumps(valid))
        mismatched_id["data"]["occurrence_id"] = "another-occurrence"
        malformed.append(mismatched_id)

        bad_timestamp = json.loads(json.dumps(valid))
        bad_timestamp["data"]["expires_at"] = "not-a-timestamp"
        malformed.append(bad_timestamp)

        for index, command in enumerate(malformed):
            command["data"]["command_id"] = f"{index + 1:032x}"
            response = self._dispatch_notification_ws(command)
            self.assertEqual(response, {
                "type": "sys.nack",
                "code": 400,
                "data": {
                    "type": "notification.deliver",
                    "command_id": command["data"]["command_id"],
                    "error": "INVALID_ARGUMENT",
                },
            })

    def test_notification_deliver_unknown_command_remains_unknown(self) -> None:
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        response = self._dispatch_notification_ws({
            "type": "notification.deliver.unknown",
            "code": 0,
            "data": {"command_id": "0123456789abcdef0123456789abcdef"},
        })
        self.assertEqual(response["type"], "sys.nack")
        self.assertEqual(response["code"], 404)
        self.assertEqual(response["error"]["code"], "UNKNOWN_OPERATION")

    @staticmethod
    def _notification_summary(command_id: str = "0123456789abcdef0123456789abcdef") -> dict:
        return {
            "type": "notification.summary.deliver",
            "code": 0,
            "data": {
                "summary_id": "summary:recovery-20261023-01",
                "batch_id": "batch:recovery-20261023-01",
                "created_at": "2026-10-23T00:00:00Z",
                "expires_at": "2026-10-24T00:00:00Z",
                "items": [{
                    "reminder_id": "reminder-1",
                    "occurrence_id": "occurrence-1",
                    "title": "Missed reminder",
                    "scheduled_at": "2026-10-22T12:30:00Z",
                    "timezone": "Asia/Shanghai",
                    "display_time": "2026-10-22 20:30 UTC+08:00",
                }],
                "omitted_count": 0,
                "command_id": command_id,
            },
        }

    def test_summary_command_is_strict_and_enqueues_only_valid_payloads(self) -> None:
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.assertEqual(self.lib.desk_hardware_notification_init(None, None), 0)
        valid = self._notification_summary()
        response = self._dispatch_notification_ws(valid)
        self.assertEqual(response, {"type": "sys.ack", "code": 0, "data": {
            "type": "notification.summary.deliver", "command_id": valid["data"]["command_id"]}})

        malformed = []
        extra = json.loads(json.dumps(valid)); extra["data"]["unexpected"] = True; malformed.append(extra)
        bad_id = json.loads(json.dumps(valid)); bad_id["data"]["summary_id"] = "bad id"; malformed.append(bad_id)
        empty_items = json.loads(json.dumps(valid)); empty_items["data"]["items"] = []; malformed.append(empty_items)
        too_many = json.loads(json.dumps(valid)); too_many["data"]["items"] *= 21; malformed.append(too_many)
        bad_time = json.loads(json.dumps(valid)); bad_time["data"]["created_at"] = "2026-02-30T00:00:00Z"; malformed.append(bad_time)
        wrong_expiry = json.loads(json.dumps(valid)); wrong_expiry["data"]["expires_at"] = "2026-10-23T23:59:59Z"; malformed.append(wrong_expiry)
        unsorted = json.loads(json.dumps(valid))
        unsorted["data"]["items"] = [json.loads(json.dumps(valid["data"]["items"][0])) for _ in range(2)]
        unsorted["data"]["items"][0]["scheduled_at"] = "2026-10-22T13:30:00Z"
        unsorted["data"]["items"][1]["scheduled_at"] = "2026-10-22T12:30:00Z"; malformed.append(unsorted)
        item_extra = json.loads(json.dumps(valid)); item_extra["data"]["items"][0]["body"] = "extra"; malformed.append(item_extra)
        item_missing = json.loads(json.dumps(valid)); del item_missing["data"]["items"][0]["timezone"]; malformed.append(item_missing)
        display_time_missing = json.loads(json.dumps(valid)); del display_time_missing["data"]["items"][0]["display_time"]; malformed.append(display_time_missing)
        display_time_bad = json.loads(json.dumps(valid)); display_time_bad["data"]["items"][0]["display_time"] = "2026-10-22 20:30 Asia/Shanghai"; malformed.append(display_time_bad)
        display_time_bad_offset = json.loads(json.dumps(valid)); display_time_bad_offset["data"]["items"][0]["display_time"] = "2026-10-22 20:30 UTC+14:30"; malformed.append(display_time_bad_offset)
        blank_title = json.loads(json.dumps(valid)); blank_title["data"]["items"][0]["title"] = "  "; malformed.append(blank_title)
        negative_omitted = json.loads(json.dumps(valid)); negative_omitted["data"]["omitted_count"] = -1; malformed.append(negative_omitted)
        bool_omitted = json.loads(json.dumps(valid)); bool_omitted["data"]["omitted_count"] = True; malformed.append(bool_omitted)
        root_extra = json.loads(json.dumps(valid)); root_extra["unexpected"] = True; malformed.append(root_extra)
        for index, command in enumerate(malformed):
            command["data"]["command_id"] = f"{index + 1:032x}"
            response = self._dispatch_notification_ws(command)
            with self.subTest(malformed_index=index):
                self.assertEqual(response["type"], "sys.nack")
                self.assertEqual(response["code"], 400)
                self.assertEqual(response["data"]["type"], "notification.summary.deliver")
                self.assertEqual(response["data"]["command_id"], command["data"]["command_id"])
                self.assertEqual(response["data"]["error"], "INVALID_ARGUMENT")

    def test_summary_expiry_and_volatile_capacity_are_truthful_nacks(self) -> None:
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.assertEqual(self.lib.desk_hardware_notification_init(None, None), 0)
        expired = self._notification_summary("e" * 32)
        expired["data"]["created_at"] = "2020-10-23T00:00:00Z"
        expired["data"]["expires_at"] = "2020-10-24T00:00:00Z"
        response = self._dispatch_notification_ws(expired)
        self.assertEqual(response["type"], "sys.nack")
        self.assertEqual(response["code"], 410)
        self.assertEqual(response["data"]["error"], "EXPIRED")

        for index in range(16):
            command = self._notification_summary(f"{index + 1:032x}")
            command["data"]["summary_id"] = f"summary-capacity-{index}"
            self.assertEqual(self._dispatch_notification_ws(command)["type"], "sys.ack")
        full = self._notification_summary("f" * 32)
        full["data"]["summary_id"] = "summary-capacity-overflow"
        response = self._dispatch_notification_ws(full)
        self.assertEqual(response["type"], "sys.nack")
        self.assertEqual(response["code"], 507)
        self.assertEqual(response["data"]["error"], "QUEUE_FULL")

    def test_summary_explicit_ui_ack_emits_stable_summary_id_without_auto_ack(self) -> None:
        emitted: list[bytes] = []

        @OUTBOUND_CB
        def on_outbound(data: int, length: int, _user_data: int) -> int:
            emitted.append(ctypes.string_at(data, length))
            return 0

        self.lib.desk_sdk_bridge_register_outbound(on_outbound, None)
        self.addCleanup(self.lib.desk_sdk_bridge_register_outbound, OUTBOUND_CB(), None)
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.assertEqual(self.lib.desk_hardware_notification_init(None, None), 0)
        command = self._notification_summary()
        response = self._dispatch_notification_ws(command)
        self.assertEqual(response["type"], "sys.ack")
        self.assertEqual(emitted, [])
        self.assertEqual(self.lib.desk_hardware_notification_summary_acknowledge(
            b"summary:recovery-20261023-01", b"explicit_ui"), 0)
        self.assertEqual(len(emitted), 1)
        event = json.loads(emitted[0])
        self.assertEqual(event["type"], "notification.acknowledged")
        self.assertEqual(event["payload"], {
            "summary_id": "summary:recovery-20261023-01", "source": "explicit_ui"})

    def test_summary_ack_event_keeps_summary_id_separate_from_transport_command_id(self) -> None:
        emitted: list[bytes] = []

        @OUTBOUND_CB
        def on_outbound(data: int, length: int, _user_data: int) -> int:
            emitted.append(ctypes.string_at(data, length))
            return 0

        self.lib.desk_sdk_bridge_register_outbound(on_outbound, None)
        self.addCleanup(self.lib.desk_sdk_bridge_register_outbound, OUTBOUND_CB(), None)
        self.assertEqual(self.lib.desk_sdk_bridge_send_notification_summary_ack(
            b"summary:recovery-20261023-01", b"explicit_ui"), 0)
        self.assertEqual(len(emitted), 1)
        event = json.loads(emitted[0])
        self.assertEqual(event["type"], "notification.acknowledged")
        self.assertEqual(event["payload"], {
            "summary_id": "summary:recovery-20261023-01", "source": "explicit_ui"})
        self.assertNotIn("command_id", event["payload"])
        self.assertEqual(self.lib.desk_sdk_bridge_send_notification_summary_ack(b"bad id", b"explicit_ui"), -1)
        self.assertEqual(self.lib.desk_sdk_bridge_send_notification_summary_ack(
            b"summary:recovery-20261023-01", b"bad source"), -1)

    def test_cr009_event_and_queue_full_gap_are_serialized_in_sequence_order(self) -> None:
        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.lib.desk_sdk_bridge_set_local_log_enabled(True)
        subscribe = {"type": "sys.log.subscribe", "code": 0, "data": {
            "command_id": "00000000000000000000000000000031",
            "log_schema_version": "1.0.0", "min_level": "WARN", "components": []}}
        out = ctypes.create_string_buffer(1024)
        self.assertEqual(self.lib.desk_sdk_bridge_process_ws_command(
            json.dumps(subscribe).encode(), out, len(out)), 0)
        ack = json.loads(out.value)
        first_sequence = ack["data"]["next_sequence"]

        for _ in range(18):
            self.lib.desk_watcher_log_source_publish_camera_capture_empty()
        messages = []
        for _ in range(17):
            frame = ctypes.create_string_buffer(768)
            n = self.lib.desk_sdk_bridge_log_next_text(frame, len(frame))
            self.assertGreater(n, 0)
            messages.append(json.loads(frame.value))
        events = [m for m in messages if m["type"] == "sys.log.event"]
        gaps = [m for m in messages if m["type"] == "sys.log.gap"]
        self.assertEqual([m["data"]["sequence"] for m in events],
                         list(range(first_sequence, first_sequence + 16)))
        self.assertEqual(len(gaps), 1)
        self.assertEqual(gaps[0]["data"], {
            "log_schema_version": "1.0.0", "stream_id": ack["data"]["stream_id"],
            "boot_id": ack["data"]["boot_id"], "first_dropped_sequence": first_sequence + 16,
            "last_dropped_sequence": first_sequence + 17, "dropped_count": 2, "reason": "QUEUE_FULL"})
        self.lib.desk_sdk_bridge_set_local_log_enabled(False)

    def test_cr033_allowlisted_events_have_exact_wire_fields(self) -> None:
        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.lib.desk_sdk_bridge_set_local_log_enabled(True)
        subscribe = {"type": "sys.log.subscribe", "code": 0, "data": {
            "command_id": "00000000000000000000000000000032",
            "log_schema_version": "1.0.0", "min_level": "WARN", "components": []}}
        out = ctypes.create_string_buffer(1024)
        self.assertEqual(self.lib.desk_sdk_bridge_process_ws_command(
            json.dumps(subscribe).encode(), out, len(out)), 0)
        expected = [
            (self.lib.desk_watcher_log_source_publish_camera_capture_empty, (),
             "WARN", "camera", "CAMERA_CAPTURE_EMPTY", {}),
            (self.lib.desk_watcher_log_source_publish_audio_driver_state, (True,),
             "WARN", "audio", "AUDIO_DRIVER_STATE_SNAPSHOT", {"state": "READY"}),
            (self.lib.desk_watcher_log_source_publish_tuya_mqtt_state, (True,),
             "WARN", "network", "TUYA_MQTT_STATE", {"state": "CONNECTED"}),
            (self.lib.desk_watcher_log_source_publish_stm32_ack_timeout, (),
             "ERROR", "stm32", "STM32_ACK_TIMEOUT", {}),
        ]
        for producer, args, level, component, event_id, fields in expected:
            with self.subTest(event_id=event_id):
                self.assertTrue(producer(*args))
                frame = ctypes.create_string_buffer(768)
                self.assertGreater(self.lib.desk_sdk_bridge_log_next_text(frame, len(frame)), 0)
                wire = json.loads(frame.value)
                self.assertEqual(wire["type"], "sys.log.event")
                data = wire["data"]
                self.assertEqual(set(data), {"log_schema_version", "stream_id", "boot_id",
                                             "sequence", "uptime_ms", "level", "component",
                                             "event_id", "fields"})
                self.assertEqual(data["log_schema_version"], "1.0.0")
                self.assertEqual((data["level"], data["component"], data["event_id"], data["fields"]),
                                 (level, component, event_id, fields))
        self.lib.desk_sdk_bridge_set_local_log_enabled(False)

    def test_cr033_audio_snapshot_is_emitted_only_after_subscribe_ack_wire_send(self) -> None:
        async def exercise() -> tuple[dict, dict]:
            received: dict = {}
            received_event: dict = {}
            wire_messages_received = asyncio.Event()
            async def handler(websocket) -> None:
                await websocket.recv()
                await websocket.send(json.dumps({"type": "sys.ack", "code": 0,
                    "data": {"type": "sys.client.hello", "role": "hardware",
                             "session_state": "connected", "negotiated": {
                                 "audio_uplink": {"codec": "opus", "sample_rate": 16000,
                                     "channels": 1, "frame_duration_ms": 60,
                                     "packetization": "one_opus_packet_per_wspk", "version": 1}}}},
                    separators=(",", ":")))
                await websocket.send(json.dumps({"type": "sys.log.subscribe", "code": 0,
                    "data": {"command_id": "33333333333333333333333333333333",
                             "log_schema_version": "1.0.0", "min_level": "WARN", "components": []}},
                    separators=(",", ":")))
                received.update(json.loads(await asyncio.wait_for(websocket.recv(), timeout=2)))
                received_event.update(json.loads(await asyncio.wait_for(websocket.recv(), timeout=2)))
                wire_messages_received.set()

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                self.lib.desk_sdk_test_prepare_log_pair()
                self.lib.desk_sdk_bridge_set_local_log_enabled(True)
                try:
                    self.assertEqual(self.lib.desk_sdk_test_set_pairing_target(b"127.0.0.1", port), 0)
                    self.assertEqual(await asyncio.to_thread(self.lib.desk_sdk_ws_client_connect_paired), 0)
                    self.assertEqual(await asyncio.to_thread(self.lib.desk_sdk_ws_client_poll, 1000), 0)
                    # The subscribed log ACK is sent by the asynchronous writer.
                    # Keep the connection alive until it and the post-send
                    # readiness event have both reached the peer.
                    await asyncio.wait_for(wire_messages_received.wait(), timeout=2)
                finally:
                    self.lib.desk_sdk_ws_client_close()
                    self.lib.desk_sdk_bridge_set_local_log_enabled(False)
            return received, received_event

        ack, event = asyncio.run(exercise())
        self.assertEqual(ack["type"], "sys.ack")
        self.assertEqual(ack["data"]["type"], "sys.log.subscribe")
        self.assertTrue(ack["data"]["accepted"])
        self.assertEqual(event["type"], "sys.log.event")
        self.assertEqual(event["data"]["event_id"], "AUDIO_DRIVER_STATE_SNAPSHOT")
        self.assertEqual(event["data"]["component"], "audio")
        self.assertEqual(event["data"]["fields"], {"state": "NOT_READY"})

    def test_cr009_deadline_writer_times_out_on_a_full_socket(self) -> None:
        sender, receiver = socket.socketpair()
        try:
            sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
            sender.setblocking(False)
            while True:
                try:
                    sender.send(b"x" * 4096)
                except BlockingIOError:
                    break
            payload = ctypes.create_string_buffer(b"deadline")
            start = time.monotonic()
            rc = self.lib.desk_sdk_test_deadline_write(sender.fileno(), payload, 8, 150)
            elapsed = time.monotonic() - start
            self.assertNotEqual(rc, 0)
            self.assertGreaterEqual(elapsed, 0.12)
            self.assertLess(elapsed, 0.5)
        finally:
            sender.close()
            receiver.close()

    def test_cr009_deadline_writer_reassembles_short_writes(self) -> None:
        sender, receiver = socket.socketpair()
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        receiver.settimeout(0.5)
        payload = bytes(range(256)) * 32
        result: dict[str, int] = {}
        def write() -> None:
            result["rc"] = self.lib.desk_sdk_test_deadline_write(
                sender.fileno(), ctypes.create_string_buffer(payload), len(payload), 250)
        writer = threading.Thread(target=write)
        writer.start()
        wire = bytearray()
        try:
            while writer.is_alive() or len(wire) < len(payload) + 8:
                try:
                    chunk = receiver.recv(256)
                except socket.timeout:
                    break
                if not chunk:
                    break
                wire.extend(chunk)
                time.sleep(0.0005)
            writer.join(timeout=1)
            self.assertFalse(writer.is_alive())
            self.assertEqual(result.get("rc"), 0)
            self.assertEqual(wire[0], 0x81)
            self.assertEqual(wire[1] & 0x7F, 126)
            self.assertEqual(int.from_bytes(wire[2:4], "big"), len(payload))
            mask = wire[4:8]
            decoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(wire[8:]))
            self.assertEqual(decoded, payload)
        finally:
            sender.close()
            receiver.close()

    def test_cr009_deadline_caps_total_time_with_slow_progressing_peer(self) -> None:
        sender, receiver = socket.socketpair()
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        sender.setblocking(False)
        payload = bytes(range(256)) * 32
        result: dict[str, int] = {}
        stop_reader = threading.Event()

        def write() -> None:
            result["rc"] = self.lib.desk_sdk_test_deadline_write(
                sender.fileno(), ctypes.create_string_buffer(payload), len(payload), 120)

        def read_slowly() -> None:
            receiver.settimeout(0.02)
            while not stop_reader.is_set():
                try:
                    receiver.recv(64)
                except socket.timeout:
                    continue
                except OSError:
                    return
                time.sleep(0.003)

        reader = threading.Thread(target=read_slowly)
        reader.start()
        start = time.monotonic()
        writer = threading.Thread(target=write)
        writer.start()
        try:
            writer.join(timeout=0.5)
            elapsed = time.monotonic() - start
            self.assertFalse(writer.is_alive(), "bounded send exceeded test watchdog")
            self.assertNotEqual(result.get("rc"), 0)
            self.assertGreaterEqual(elapsed, 0.09)
            self.assertLess(elapsed, 0.35)
        finally:
            stop_reader.set()
            sender.close()
            receiver.close()
            reader.join(timeout=0.2)

    def test_cr009_binary_frame_over_8k_survives_short_writes(self) -> None:
        sender, receiver = socket.socketpair()
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        receiver.settimeout(0.5)
        payload = bytes(range(256)) * 64
        result: dict[str, int] = {}
        def write() -> None:
            result["rc"] = self.lib.desk_sdk_test_binary_deadline_write(
                sender.fileno(), ctypes.create_string_buffer(payload), len(payload), 250)
        writer = threading.Thread(target=write)
        writer.start()
        wire = bytearray()
        try:
            while writer.is_alive() or len(wire) < len(payload) + 8:
                try:
                    chunk = receiver.recv(512)
                except socket.timeout:
                    break
                if not chunk:
                    break
                wire.extend(chunk)
                time.sleep(0.0002)
            writer.join(timeout=1)
            self.assertFalse(writer.is_alive())
            self.assertEqual(result.get("rc"), 0)
            self.assertEqual(wire[0], 0x82)
            self.assertEqual(wire[1] & 0x7F, 126)
            self.assertEqual(int.from_bytes(wire[2:4], "big"), len(payload))
            mask = wire[4:8]
            decoded = bytes(byte ^ mask[index % 4] for index, byte in enumerate(wire[8:]))
            self.assertEqual(decoded, payload)
        finally:
            sender.close()
            receiver.close()

    def test_cr009_full_control_queue_fails_closed_without_blocking(self) -> None:
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.lib.desk_sdk_test_pause_writer_without_thread()
        start = time.monotonic()
        results = [self.lib.desk_sdk_ws_client_send_text(b'{"type":"sys.ping"}') for _ in range(4)]
        elapsed = time.monotonic() - start
        self.assertEqual(results[:3], [0, 0, 0])
        self.assertNotEqual(results[3], 0)
        self.assertFalse(self.lib.desk_sdk_ws_client_is_connected())
        self.assertLess(elapsed, 0.1)
        self.assertEqual(self.lib.desk_sdk_test_outbound_queue_is_zero(), 1)
        self.lib.desk_sdk_test_resume_writer()

    def test_cr009_live_writer_timeout_closes_connection_and_clears_queue(self) -> None:
        sender, receiver = socket.socketpair()
        sender.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1024)
        sender.setblocking(False)
        try:
            while True:
                try:
                    sender.send(b"x" * 4096)
                except BlockingIOError:
                    break
            writer_fd = os.dup(sender.fileno())
            self.lib.desk_sdk_test_attach_writer_fd(writer_fd)
            start = time.monotonic()
            self.assertEqual(self.lib.desk_sdk_ws_client_send_text(b'{"type":"sys.ack"}'), 0)
            deadline = start + 0.8
            while self.lib.desk_sdk_ws_client_is_connected() and time.monotonic() < deadline:
                time.sleep(0.005)
            elapsed = time.monotonic() - start
            self.assertFalse(self.lib.desk_sdk_ws_client_is_connected())
            self.assertGreaterEqual(elapsed, 0.18)
            self.assertLess(elapsed, 0.8)
            self.assertEqual(self.lib.desk_sdk_test_outbound_queue_is_zero(), 1)
        finally:
            sender.close()
            receiver.close()

    def test_cr009_stale_writer_failure_cannot_close_new_generation(self) -> None:
        sender, receiver = socket.socketpair()
        try:
            self.lib.desk_sdk_test_attach_writer_fd(os.dup(sender.fileno()))
            old_generation = self.lib.desk_sdk_test_ws_generation()
            self.lib.desk_sdk_test_advance_ws_generation()
            self.lib.desk_sdk_test_disconnect_ws_generation(old_generation)
            self.assertTrue(self.lib.desk_sdk_ws_client_is_connected())
        finally:
            self.lib.desk_sdk_ws_client_close()
            sender.close()
            receiver.close()

    def test_cr009_command_is_dispatched_from_live_websocket_receive_loop(self) -> None:
        async def exercise() -> dict:
            received: dict = {}
            received_event: dict = {}
            received_camera_event: dict = {}
            async def handler(websocket) -> None:
                hello = json.loads(await websocket.recv())
                self.assertEqual(hello["type"], "sys.client.hello")
                await websocket.send(json.dumps({"type": "sys.ack", "code": 0,
                    "data": {"type": "sys.client.hello", "role": "hardware",
                             "session_state": "connected", "negotiated": {
                                 "audio_uplink": {"codec": "opus", "sample_rate": 16000,
                                     "channels": 1, "frame_duration_ms": 60,
                                     "packetization": "one_opus_packet_per_wspk", "version": 1}}}},
                    separators=(",", ":")))
                await websocket.send(json.dumps({"type": "sys.log.subscribe", "code": 0,
                    "data": {"command_id": "11111111111111111111111111111111",
                             "log_schema_version": "1.0.0", "min_level": "WARN", "components": []}},
                    separators=(",", ":")))
                received.update(json.loads(await asyncio.wait_for(websocket.recv(), timeout=2)))
                received_event.update(json.loads(await asyncio.wait_for(websocket.recv(), timeout=2)))
                received_camera_event.update(json.loads(await asyncio.wait_for(websocket.recv(), timeout=2)))

            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                self.lib.desk_sdk_test_prepare_log_pair()
                self.lib.desk_sdk_bridge_set_local_log_enabled(True)
                self.assertEqual(self.lib.desk_sdk_test_set_pairing_target(b"127.0.0.1", port), 0)
                self.assertEqual(await asyncio.to_thread(self.lib.desk_sdk_ws_client_connect_paired), 0)
                self.assertTrue(self.lib.desk_sdk_ws_client_is_authenticated())
                self.assertEqual(await asyncio.to_thread(self.lib.desk_sdk_ws_client_poll, 1000), 0)
                self.lib.desk_watcher_log_source_publish_camera_capture_empty()
                await asyncio.sleep(0.05)
                self.lib.desk_sdk_ws_client_close()
                self.lib.desk_sdk_bridge_set_local_log_enabled(False)
            return {"ack": received, "event": received_event, "camera_event": received_camera_event}

        response = asyncio.run(exercise())
        self.assertEqual(response["ack"]["type"], "sys.ack")
        self.assertEqual(response["ack"]["data"]["type"], "sys.log.subscribe")
        self.assertEqual(response["ack"]["data"]["command_id"], "11111111111111111111111111111111")
        self.assertTrue(response["ack"]["data"]["accepted"])
        self.assertEqual(response["event"]["type"], "sys.log.event")
        self.assertEqual(response["event"]["data"]["event_id"], "AUDIO_DRIVER_STATE_SNAPSHOT")
        self.assertEqual(response["event"]["data"]["fields"], {"state": "NOT_READY"})
        self.assertEqual(response["event"]["data"]["sequence"], response["ack"]["data"]["next_sequence"])
        self.assertEqual(response["camera_event"]["data"]["event_id"], "CAMERA_CAPTURE_EMPTY")
        self.assertEqual(response["camera_event"]["data"]["sequence"], response["event"]["data"]["sequence"] + 1)

    def test_cr009_rejects_malformed_or_ambiguous_json_filters(self) -> None:
        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        self.lib.desk_sdk_bridge_set_local_log_enabled(True)
        cid = "22222222222222222222222222222222"
        base = ('{"type":"sys.log.subscribe","code":0,"data":{"command_id":"' + cid +
                '","log_schema_version":"1.0.0","min_level":"WARN","components":[]}}')

        def dispatch_raw(raw: str) -> tuple[int, dict | None]:
            out = ctypes.create_string_buffer(1024)
            rc = self.lib.desk_sdk_bridge_process_ws_command(raw.encode(), out, len(out))
            return rc, json.loads(out.value) if rc == 0 and out.value else None

        malformed = [
            base[:-1] + 'garbage',
            base[:-2],
            base.replace('"components":[]', '"components":["camera"]garbage'),
            base.replace(',"components":[]', ',"meta":{"components":["camera"]}'),
            base.replace(',"components":[]', ',"note":"escaped \\\"components\\\": [\\\"camera\\\"]"'),
            base[:-1] + ',"data":{"command_id":"' + cid + '","log_schema_version":"1.0.0"}}',
            base[:-1] + ',"data":{"unexpected":true}}',
        ]
        for raw in malformed:
            with self.subTest(raw=raw):
                rc, response = dispatch_raw(raw)
                self.assertTrue(rc != 0 or (response and response["type"] == "sys.nack"))

        invalid_but_well_formed = [
            base.replace('"components":[]', '"components":[],"components":["camera"]'),
            base.replace('"min_level":"WARN"', '"min_level":"WARN","min_level":"ERROR"'),
            base.replace('"log_schema_version":"1.0.0"', '"log_schema_version":"1.0.0","log_schema_version":"1.0.0"'),
            base.replace('"min_level":"WARN"', '"min_level":"WARN-extra"'),
            base.replace('"log_schema_version":"1.0.0"', '"log_schema_version":"2.0"'),
            base.replace('"components":[]', '"components":[],"unexpected":true'),
        ]
        for raw in invalid_but_well_formed:
            with self.subTest(raw=raw):
                rc, response = dispatch_raw(raw)
                self.assertTrue(rc != 0 or (response and response["type"] == "sys.nack"))
        self.lib.desk_sdk_bridge_set_local_log_enabled(False)

    def test_failed_hardware_hello_ack_does_not_set_authenticated_latch(self) -> None:
        """A NACK/failed handshake never establishes the per-WebSocket latch."""
        async def reject_hello(websocket) -> None:
            await websocket.recv()
            await websocket.send(json.dumps({
                "type": "sys.nack",
                "code": 401,
                "data": {"type": "sys.client.hello", "error": "rejected"},
            }))

        async def exercise_rejection() -> None:
            async with websockets.serve(reject_hello, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                request = PairRequest(
                    request_id="41a9dbf05ea3443480e62076f79a3b12",
                    daemon_instance_id="f730f29e670c49f7a3320c4314eb9805",
                    pairing_code="123456",
                    target_mode="python_sdk",
                    websocket_port=port,
                )
                response = ctypes.create_string_buffer(1024)
                self.assertEqual(
                    self.lib.desk_sdk_bridge_process_pairing_udp(
                        encode_udp_message(request), response, len(response)
                    ),
                    0,
                )
                self.assertEqual(
                    self.lib.desk_sdk_test_set_pairing_target(b"127.0.0.1", port),
                    0,
                )
                connect_rc = await asyncio.to_thread(
                    self.lib.desk_sdk_ws_client_connect_paired
                )
                self.assertNotEqual(connect_rc, 0)
                self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())
                self.assertFalse(self.lib.desk_sdk_ws_client_is_connected())

        asyncio.run(exercise_rejection())

    def test_cr031_hardware_hello_ack_exact_json_schema(self) -> None:
        ack = {
            "type": "sys.ack", "code": 0,
            "data": {
                "type": "sys.client.hello", "role": "hardware",
                "session_state": "connected",
                "negotiated": {"audio_uplink": {
                    "codec": "opus", "sample_rate": 16000, "channels": 1,
                    "frame_duration_ms": 60,
                    "packetization": "one_opus_packet_per_wspk", "version": 1,
                }},
            },
        }
        def accepts(raw: str) -> bool:
            return self.lib.desk_sdk_test_validate_hello_ack(raw.encode("utf-8")) == 1

        self.assertTrue(accepts(json.dumps(ack, separators=(",", ":"))))
        self.assertTrue(accepts(json.dumps(ack, separators=(",", ":")).replace('"code":0', '"code":-0')))
        self.assertTrue(accepts(" \r\n\t" + json.dumps(ack, separators=(",", ":"))))
        rejected: list[str] = []
        def reject(label: str, raw: str) -> None:
            if accepts(raw): rejected.append(label)

        for bad in ("sys.nack", "sys.client.hello", "hardware", "connected", "opus",
                    "one_opus_packet_per_wspk"):
            changed = json.loads(json.dumps(ack))
            if bad in ("sys.nack", "sys.ack"):
                changed["type"] = bad
            elif bad == "sys.client.hello": changed["data"]["type"] = "wrong"
            elif bad == "hardware": changed["data"]["role"] = "desktop"
            elif bad == "connected": changed["data"]["session_state"] = "pending"
            elif bad == "opus": changed["data"]["negotiated"]["audio_uplink"]["codec"] = "pcm"
            else: changed["data"]["negotiated"]["audio_uplink"]["packetization"] = "other"
            reject("wrong-value-" + bad, json.dumps(changed, separators=(",", ":")))
        for code in (True, False, 0.0, "0", 1, None):
            changed = json.loads(json.dumps(ack)); changed["code"] = code
            reject("code-" + repr(code), json.dumps(changed, separators=(",", ":")))

        mutations = [
            ("top-missing", lambda x: x.pop("code")),
            ("top-extra", lambda x: x.update(status="connected")),
            ("data-missing", lambda x: x["data"].pop("role")),
            ("data-extra", lambda x: x["data"].update(status="connected")),
            ("negotiated-missing", lambda x: x["data"]["negotiated"].pop("audio_uplink")),
            ("negotiated-extra", lambda x: x["data"]["negotiated"].update(extra=1)),
            ("audio-missing", lambda x: x["data"]["negotiated"]["audio_uplink"].pop("version")),
            ("audio-extra", lambda x: x["data"]["negotiated"]["audio_uplink"].update(extra=1)),
            ("sample-rate-bool", lambda x: x["data"]["negotiated"]["audio_uplink"].update(sample_rate=True)),
            ("channels-float", lambda x: x["data"]["negotiated"]["audio_uplink"].update(channels=1.0)),
            ("duration-string", lambda x: x["data"]["negotiated"]["audio_uplink"].update(frame_duration_ms="60")),
            ("version-zero", lambda x: x["data"]["negotiated"]["audio_uplink"].update(version=0)),
            ("data-type-number", lambda x: x["data"].update(type=1)),
            ("data-role-null", lambda x: x["data"].update(role=None)),
            ("negotiated-null", lambda x: x["data"].update(negotiated=None)),
            ("audio-array", lambda x: x["data"]["negotiated"].update(audio_uplink=[])),
        ]
        for label, mutate in mutations:
            changed = json.loads(json.dumps(ack)); mutate(changed)
            reject(label, json.dumps(changed, separators=(",", ":")))

        duplicates = [
            ('{"type":"sys.ack","type":"sys.ack","code":0,"data":{}}', "top"),
            (json.dumps(ack, separators=(",", ":")).replace('"role":"hardware"', '"role":"hardware","role":"hardware"'), "data"),
            (json.dumps(ack, separators=(",", ":")).replace('"audio_uplink":{', '"audio_uplink":{},"audio_uplink":{'), "negotiated"),
            (json.dumps(ack, separators=(",", ":")).replace('"codec":"opus"', '"codec":"opus","codec":"opus"'), "audio"),
        ]
        for raw, level in duplicates: reject("duplicate-" + level, raw)
        for raw, label in (("{", "truncated"), ('{"type":', "truncated-value"),
                           ('{"type":"sys.ack",}', "trailing-comma"),
                           (json.dumps(ack) + "garbage", "trailing-data"),
                           (json.dumps(ack).replace('"code": 0', '"code": true'), "boolean-code")):
            reject(label, raw)
        self.assertEqual(rejected, [], f"unexpectedly accepted ACK cases: {rejected}")

    def test_cr031_invalid_text_ack_closes_without_ready_latch(self) -> None:
        async def run() -> None:
            async def handler(websocket) -> None:
                await websocket.recv()
                # Looks like an ACK by substring, but is missing the pinned schema.
                await websocket.send('{"type":"sys.ack","code":0,"data":{"type":"sys.client.hello"}}')
            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                self.lib.desk_sdk_test_prepare_log_pair()
                self.assertEqual(self.lib.desk_sdk_test_set_pairing_target(b"127.0.0.1", port), 0)
                self.assertNotEqual(await asyncio.to_thread(self.lib.desk_sdk_ws_client_connect_paired), 0)
                self.assertFalse(self.lib.desk_sdk_ws_client_is_connected())
                self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())
                self.assertEqual(self.lib.desk_sdk_test_ws_state(), 0)
        asyncio.run(run())

    def test_cr031_nontext_first_response_closes_without_ready_latch(self) -> None:
        async def run() -> None:
            async def handler(websocket) -> None:
                await websocket.recv()
                await websocket.send(b'{"type":"sys.ack"}')
            async with websockets.serve(handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                self.lib.desk_sdk_test_prepare_log_pair()
                self.assertEqual(self.lib.desk_sdk_test_set_pairing_target(b"127.0.0.1", port), 0)
                self.assertNotEqual(await asyncio.to_thread(self.lib.desk_sdk_ws_client_connect_paired), 0)
                self.assertFalse(self.lib.desk_sdk_ws_client_is_connected())
                self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())
        asyncio.run(run())

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

    def test_generated_pairing_and_hello_are_accepted_by_official_runtime_server(self) -> None:
        """Exercise C bridge pairing output against the pinned Runtime's real loopback WS server."""
        # Importing the SDK's connections package eagerly imports its unrelated
        # application process monitor (psutil). This test exercises only the
        # official pairing authority and WebSocket listener, so keep that
        # optional monitor dependency out of this focused harness when absent.
        try:
            import psutil  # noqa: F401
        except ImportError:
            sys.modules.setdefault("psutil", types.ModuleType("psutil"))
        from watcherobot.runtime.daemon.pairing.session import DevicePairingSession
        from watcherobot.runtime.daemon.connections.websocket_server import ExternalWebSocketServer

        request_id = "21a9dbf05ea3443480e62076f79a3b12"
        daemon_id = "f730f29e670c49f7a3320c4314eb9805"
        request = PairRequest(
            request_id=request_id,
            daemon_instance_id=daemon_id,
            pairing_code="123456",
            target_mode="python_sdk",
            websocket_port=8765,
        )
        pair_reply = ctypes.create_string_buffer(1024)
        rc = self.lib.desk_sdk_bridge_process_pairing_udp(
            encode_udp_message(request), pair_reply, len(pair_reply)
        )
        self.assertEqual(rc, 0)
        pair_accept = parse_udp_message(pair_reply.value)
        self.assertIsInstance(pair_accept, PairAccept)

        hello_buf = ctypes.create_string_buffer(512)
        self.assertGreater(
            self.lib.desk_sdk_bridge_build_hardware_hello(hello_buf, len(hello_buf)), 0
        )
        hello_wire = hello_buf.value.decode("utf-8")
        parse_hardware_hello(hello_wire)

        pairing = DevicePairingSession(
            daemon_instance_id=daemon_id,
            request_id_factory=lambda: request_id,
        )
        pairing.start_pairing(
            pairing_code="123456",
            target_mode="python_sdk",
            websocket_port=8765,
            now=0.0,
        )
        pairing.accept_device(pair_accept, peer_ip="127.0.0.1", now=1.0)

        async def exercise_runtime() -> dict:
            async def authorize(hardware_hello, peer_ip: str) -> None:
                pairing.connect_device(hardware_hello, peer_ip=peer_ip, now=2.0)

            server = ExternalWebSocketServer(
                host="127.0.0.1",
                port=0,
                hardware_hello_authorizer=authorize,
            )
            await server.start()
            try:
                async with websockets.connect(server.url) as ws:
                    await ws.send(hello_wire)
                    return json.loads(await ws.recv())
            finally:
                await server.stop()

        ack = asyncio.run(exercise_runtime())
        self.assertEqual(ack["type"], "sys.ack")
        self.assertEqual(ack["code"], 0)
        self.assertEqual(ack["data"]["type"], "sys.client.hello")
        self.assertEqual(ack["data"]["role"], "hardware")
        self.assertEqual(pairing.snapshot()["state"], "connected")

    def test_production_pairing_udp_listener_connects_to_official_runtime(self) -> None:
        """The production UDP poll path accepts a host request and dials its Runtime port."""
        import time

        try:
            import psutil  # noqa: F401
        except ImportError:
            sys.modules.setdefault("psutil", types.ModuleType("psutil"))
        from watcherobot.runtime.daemon.pairing.session import DevicePairingSession
        from watcherobot.runtime.daemon.connections.websocket_server import ExternalWebSocketServer

        self.lib.desk_sdk_pairing_server_start.restype = ctypes.c_int
        self.lib.desk_sdk_pairing_server_start.argtypes = [ctypes.c_ushort]
        self.lib.desk_sdk_pairing_server_get_port.restype = ctypes.c_int
        self.lib.desk_sdk_pairing_server_get_port.argtypes = []
        self.lib.desk_sdk_pairing_server_stop.restype = ctypes.c_int
        self.lib.desk_sdk_pairing_server_stop.argtypes = []
        request_id = "21a9dbf05ea3443480e62076f79a3b12"
        daemon_id = "f730f29e670c49f7a3320c4314eb9805"
        pairing = DevicePairingSession(
            daemon_instance_id=daemon_id,
            request_id_factory=lambda: request_id,
        )

        async def exercise_runtime() -> None:
            accepted = asyncio.Event()

            async def authorize(hardware_hello, peer_ip: str) -> None:
                await asyncio.wait_for(accepted.wait(), timeout=3)
                pairing.connect_device(hardware_hello, peer_ip=peer_ip, now=2.0)

            server = ExternalWebSocketServer(
                host="127.0.0.1", port=0, hardware_hello_authorizer=authorize,
            )
            await server.start()
            poll_stop = threading.Event()
            poll_thread = None
            host_udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            host_udp.settimeout(3)
            try:
                pairing.start_pairing(
                    pairing_code="123456", target_mode="python_sdk",
                    websocket_port=server.bound_port, now=0.0,
                )
                self.assertEqual(self.lib.desk_sdk_pairing_server_start(0), 0)
                pairing_port = self.lib.desk_sdk_pairing_server_get_port()
                self.assertGreater(pairing_port, 0)

                def poll_device_pairing() -> None:
                    while not poll_stop.is_set():
                        self.lib.desk_sdk_pairing_server_poll(20)

                poll_thread = threading.Thread(target=poll_device_pairing, daemon=True)
                poll_thread.start()
                request = PairRequest(
                    request_id=request_id, daemon_instance_id=daemon_id,
                    pairing_code="123456", target_mode="python_sdk",
                    websocket_port=server.bound_port,
                )
                host_udp.sendto(encode_udp_message(request), ("127.0.0.1", pairing_port))
                response, _ = await asyncio.to_thread(host_udp.recvfrom, 2048)
                pair_accept = parse_udp_message(response)
                self.assertIsInstance(pair_accept, PairAccept)
                pairing.accept_device(pair_accept, peer_ip="127.0.0.1", now=1.0)
                accepted.set()

                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if self.lib.desk_sdk_ws_client_is_connected():
                        break
                    await asyncio.sleep(0.02)
                self.assertTrue(
                    self.lib.desk_sdk_ws_client_is_connected(),
                    f"state={self.lib.desk_sdk_test_ws_state()}, "
                    f"http={self.lib.desk_sdk_test_http_response().decode('utf-8', 'replace')!r}, "
                    f"ack={self.lib.desk_sdk_test_last_ack().decode('utf-8', 'replace')!r}",
                )
                self.assertTrue(self.lib.desk_sdk_ws_client_is_authenticated())
                self.assertEqual(pairing.snapshot()["state"], "connected")
            finally:
                self.lib.desk_sdk_ws_client_close()
                poll_stop.set()
                if poll_thread is not None:
                    poll_thread.join(timeout=2)
                self.lib.desk_sdk_pairing_server_stop()
                host_udp.close()
                await server.stop()

        asyncio.run(exercise_runtime())

    def test_c_firmware_client_runtime_commands_results_events_media_and_reconnect(self) -> None:
        """Connect the production C WebSocket client to the pinned Runtime and exchange real frames."""
        import time

        try:
            import psutil  # noqa: F401
        except ImportError:
            sys.modules.setdefault("psutil", types.ModuleType("psutil"))
        from watcherobot.runtime.daemon.pairing.session import DevicePairingSession
        from watcherobot.runtime.daemon.connections.websocket_server import ExternalWebSocketServer

        request_id = "21a9dbf05ea3443480e62076f79a3b12"
        daemon_id = "f730f29e670c49f7a3320c4314eb9805"
        pairing = DevicePairingSession(
            daemon_instance_id=daemon_id,
            request_id_factory=lambda: request_id,
        )

        async def exercise_runtime() -> None:
            clock = {"now": 1.0}

            async def authorize(hardware_hello, peer_ip: str) -> None:
                pairing.connect_device(hardware_hello, peer_ip=peer_ip, now=clock["now"])
                clock["now"] += 1.0

            async def disconnected(_peer_ip: str) -> None:
                pairing.device_disconnected(now=clock["now"])
                clock["now"] += 1.0

            server = ExternalWebSocketServer(
                host="127.0.0.1",
                port=0,
                hardware_hello_authorizer=authorize,
                device_disconnect_listener=disconnected,
            )
            await server.start()
            poll_stop = asyncio.Event()

            async def poll_c_client() -> None:
                while not poll_stop.is_set():
                    await asyncio.to_thread(self.lib.desk_sdk_ws_client_poll, 50)

            poll_task = asyncio.create_task(poll_c_client())
            try:
                request = PairRequest(
                    request_id=request_id,
                    daemon_instance_id=daemon_id,
                    pairing_code="123456",
                    target_mode="python_sdk",
                    websocket_port=server.bound_port,
                )
                pair_reply = ctypes.create_string_buffer(1024)
                rc = self.lib.desk_sdk_bridge_process_pairing_udp(
                    encode_udp_message(request), pair_reply, len(pair_reply)
                )
                self.assertEqual(rc, 0)
                pair_accept = parse_udp_message(pair_reply.value)
                pairing.start_pairing(
                    pairing_code="123456",
                    target_mode="python_sdk",
                    websocket_port=server.bound_port,
                    now=0.0,
                )
                pairing.accept_device(pair_accept, peer_ip="127.0.0.1", now=0.5)
                self.assertEqual(
                    self.lib.desk_sdk_test_set_pairing_target(
                        b"127.0.0.1", server.bound_port
                    ),
                    0,
                )
                self.assertEqual(self.lib.desk_sdk_test_preflight(), 1)
                connect_rc = await asyncio.to_thread(self.lib.desk_sdk_ws_client_connect_paired)
                self.assertEqual(connect_rc, 0,
                                 f"state={self.lib.desk_sdk_test_ws_state()}, "
                                 f"http={self.lib.desk_sdk_test_http_response().decode('utf-8', 'replace')!r}, "
                                 f"ack={self.lib.desk_sdk_test_last_ack().decode('utf-8', 'replace')!r}")
                self.assertTrue(self.lib.desk_sdk_ws_client_is_connected())
                self.assertTrue(self.lib.desk_sdk_ws_client_is_authenticated())
                # The current implementation only observes the existing protocol ACK;
                # this does not cryptographically authenticate the Runtime peer.

                async with websockets.connect(server.url) as desktop:
                    await desktop.send(json.dumps({
                        "type": "sys.client.hello", "code": 0,
                        "data": {"role": "desktop", "version": "1.0"},
                    }))
                    desktop_hello = json.loads(await desktop.recv())
                    self.assertEqual(desktop_hello["type"], "sys.ack")

                    await desktop.send(json.dumps({
                        "type": "ctrl.motion.set_target", "code": 0,
                        "request_id": "runtime-motion-01",
                        "data": {"pan_deg": 111, "tilt_deg": 99,
                                 "command_id": "runtime-motion-01"},
                    }))
                    result = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual((result["type"], result["code"]), ("sys.ack", 0))
                    self.assertEqual(result["request_id"], "runtime-motion-01")
                    self.assertEqual(result["data"]["status"], "accepted")
                    self.assertIs(result["data"]["state_confirmed"], False)

                    await desktop.send(json.dumps({
                        "type": "runtime.unsupported.operation", "code": 0, "data": {},
                    }))
                    nack = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(nack["type"], "sys.nack")
                    self.assertNotEqual(nack["code"], 0)

                    self.assertEqual(self.lib.desk_sdk_bridge_send_event(
                        b"resource.motion.completed",
                        b'{"command_id":"runtime-motion-01","status":"succeeded"}',
                    ), 0)
                    event = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(event["type"], "resource.motion.completed")
                    self.assertEqual(event["payload"]["command_id"], "runtime-motion-01")

                    jpeg = (Path(__file__).parent / "fixtures" / "interop_4x3.jpg").read_bytes()
                    payload = ctypes.create_string_buffer(jpeg)
                    self.assertEqual(self.lib.desk_sdk_bridge_send_media_frame(
                        FRAME_IMAGE, 0, 0x1234, 7, payload, len(jpeg)
                    ), 0)
                    media = parse_wspk(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual((media.frame_type, media.stream_id, media.sequence),
                                     (FRAME_IMAGE, 0x1234, 7))
                    self.assertEqual(media.payload, jpeg)
                    self.assertEqual(_jpeg_dimensions(media.payload), (4, 3))

                # Force the official Runtime to close sessions, then let the
                # C client's bounded retry path reconnect with the same token.
                await server.registry.close_all(code=1012, reason="interop reconnect check")
                deadline = time.monotonic() + 8.0
                reconnected = False
                while time.monotonic() < deadline:
                    if self.lib.desk_sdk_ws_client_is_connected() and pairing.snapshot()["state"] == "connected":
                        reconnected = True
                        break
                    await asyncio.sleep(0.1)
                self.assertTrue(reconnected, "C client did not reconnect to official Runtime")
                self.assertTrue(self.lib.desk_sdk_ws_client_is_authenticated())
                self.lib.desk_sdk_ws_client_close()
                self.assertFalse(self.lib.desk_sdk_ws_client_is_authenticated())
            finally:
                poll_stop.set()
                await poll_task
                await server.stop()

        asyncio.run(exercise_runtime())

    def test_c_roller_inputs_reach_official_sdk_wait_across_reconnect(self) -> None:
        """The production C client delivers only live detents to pinned SDK inputs.wait()."""
        try:
            import psutil  # noqa: F401
        except ImportError:
            sys.modules.setdefault("psutil", types.ModuleType("psutil"))
        from watcherobot.desktop_session import DesktopRobotSession
        from watcherobot.robot import WatcheRobot
        from watcherobot.runtime.daemon.connections.registry import ExternalClientRole
        from watcherobot.runtime.daemon.pairing.session import DevicePairingSession
        from watcherobot.runtime.daemon.connections.websocket_server import ExternalWebSocketServer

        request_id = "51a9dbf05ea3443480e62076f79a3b12"
        daemon_id = "a730f29e670c49f7a3320c4314eb9805"
        pairing = DevicePairingSession(
            daemon_instance_id=daemon_id,
            request_id_factory=lambda: request_id,
        )

        async def exercise_runtime() -> None:
            clock = {"now": 1.0}

            async def authorize(hardware_hello, peer_ip: str) -> None:
                pairing.connect_device(hardware_hello, peer_ip=peer_ip, now=clock["now"])
                clock["now"] += 1.0

            async def disconnected(_peer_ip: str) -> None:
                pairing.device_disconnected(now=clock["now"])
                clock["now"] += 1.0

            server = ExternalWebSocketServer(
                host="127.0.0.1",
                port=0,
                hardware_hello_authorizer=authorize,
                device_disconnect_listener=disconnected,
            )
            await server.start()
            poll_stop = asyncio.Event()

            async def poll_c_client() -> None:
                while not poll_stop.is_set():
                    await asyncio.to_thread(self.lib.desk_sdk_ws_client_poll, 50)

            poll_task = asyncio.create_task(poll_c_client())
            desktop = DesktopRobotSession(server.url, command_timeout=1.0)
            robot = WatcheRobot._from_transport(desktop)
            try:
                request = PairRequest(
                    request_id=request_id,
                    daemon_instance_id=daemon_id,
                    pairing_code="123456",
                    target_mode="python_sdk",
                    websocket_port=server.bound_port,
                )
                pair_reply = ctypes.create_string_buffer(1024)
                self.assertEqual(self.lib.desk_sdk_bridge_process_pairing_udp(
                    encode_udp_message(request), pair_reply, len(pair_reply)
                ), 0)
                pair_accept = parse_udp_message(pair_reply.value)
                pairing.start_pairing(
                    pairing_code="123456", target_mode="python_sdk",
                    websocket_port=server.bound_port, now=0.0,
                )
                pairing.accept_device(pair_accept, peer_ip="127.0.0.1", now=0.5)
                self.assertEqual(self.lib.desk_sdk_test_set_pairing_target(
                    b"127.0.0.1", server.bound_port
                ), 0)
                self.assertEqual(await asyncio.to_thread(
                    self.lib.desk_sdk_ws_client_connect_paired
                ), 0)
                self.assertTrue(self.lib.desk_sdk_ws_client_is_authenticated())

                await asyncio.to_thread(desktop.start)
                self.assertEqual(self.lib.desk_sdk_bridge_send_roller_input(1, 101), 0)
                positive = await asyncio.to_thread(robot.inputs.wait, 2.0)
                self.assertEqual(type(positive).__name__, "RollerEvent")
                self.assertEqual((positive.delta, positive.timestamp_ms), (1, 101))

                self.assertEqual(self.lib.desk_sdk_bridge_send_roller_input(-1, 102), 0)
                negative = await asyncio.to_thread(robot.inputs.wait, 2.0)
                self.assertEqual(type(negative).__name__, "RollerEvent")
                self.assertEqual((negative.delta, negative.timestamp_ms), (-1, 102))

                # Close only the hardware side. A detent attempted without an
                # authenticated Runtime connection must fail and must not be
                # replayed after the production C reconnect path runs.
                self.assertEqual(await server.registry.close_role(
                    ExternalClientRole.DEVICE,
                    code=1012,
                    reason="roller reconnect check",
                ), 1)
                deadline = time.monotonic() + 5.0
                while time.monotonic() < deadline and self.lib.desk_sdk_ws_client_is_connected():
                    await asyncio.sleep(0.02)
                self.assertFalse(self.lib.desk_sdk_ws_client_is_connected())
                self.assertNotEqual(self.lib.desk_sdk_bridge_send_roller_input(1, 103), 0)

                deadline = time.monotonic() + 8.0
                reconnected = False
                while time.monotonic() < deadline:
                    if (self.lib.desk_sdk_ws_client_is_authenticated()
                            and pairing.snapshot()["state"] == "connected"):
                        reconnected = True
                        break
                    await asyncio.sleep(0.05)
                self.assertTrue(reconnected, "production C client did not reconnect to pinned Runtime")

                with self.assertRaises(TimeoutError):
                    await asyncio.to_thread(robot.inputs.wait, 0.2)

                self.assertEqual(self.lib.desk_sdk_bridge_send_roller_input(-1, 104), 0)
                fresh = await asyncio.to_thread(robot.inputs.wait, 2.0)
                self.assertEqual(type(fresh).__name__, "RollerEvent")
                self.assertEqual((fresh.delta, fresh.timestamp_ms), (-1, 104))
            finally:
                desktop.close()
                poll_stop.set()
                await poll_task
                await server.stop()

        asyncio.run(exercise_runtime())

    def test_c_roller_input_overflow_drops_oldest_and_counts_drops(self) -> None:
        """Pinned Runtime loopback preserves only the newest bounded input stream."""
        try:
            import psutil  # noqa: F401
        except ImportError:
            sys.modules.setdefault("psutil", types.ModuleType("psutil"))
        from watcherobot.desktop_session import DesktopRobotSession
        from watcherobot.inputs import InputDomain
        from watcherobot.robot import WatcheRobot
        from watcherobot.runtime.daemon.connections.registry import ExternalClientRole
        from watcherobot.runtime.daemon.pairing.session import DevicePairingSession
        from watcherobot.runtime.daemon.connections.websocket_server import ExternalWebSocketServer

        request_id = "61a9dbf05ea3443480e62076f79a3b12"
        daemon_id = "b730f29e670c49f7a3320c4314eb9805"
        pairing = DevicePairingSession(
            daemon_instance_id=daemon_id,
            request_id_factory=lambda: request_id,
        )
        runtime_inputs: list[dict] = []

        async def exercise_runtime() -> None:
            clock = {"now": 1.0}

            async def authorize(hardware_hello, peer_ip: str) -> None:
                pairing.connect_device(hardware_hello, peer_ip=peer_ip, now=clock["now"])
                clock["now"] += 1.0

            async def disconnected(_peer_ip: str) -> None:
                pairing.device_disconnected(now=clock["now"])
                clock["now"] += 1.0

            async def observe_business_frame(connection, frame: str | bytes) -> None:
                if connection.role is ExternalClientRole.DEVICE and isinstance(frame, str):
                    message = json.loads(frame)
                    if message.get("type") == "evt.sdk.input":
                        runtime_inputs.append(message["data"])

            server = ExternalWebSocketServer(
                host="127.0.0.1",
                port=0,
                hardware_hello_authorizer=authorize,
                device_disconnect_listener=disconnected,
                business_frame_listener=observe_business_frame,
            )
            await server.start()
            poll_stop = asyncio.Event()

            async def poll_c_client() -> None:
                while not poll_stop.is_set():
                    await asyncio.to_thread(self.lib.desk_sdk_ws_client_poll, 50)

            poll_task = asyncio.create_task(poll_c_client())
            desktop = DesktopRobotSession(server.url, command_timeout=1.0)
            robot = WatcheRobot._from_transport(desktop)
            queue_capacity = InputDomain()._queue.maxsize
            self.assertEqual(queue_capacity, 64, "Pinned SDK InputDomain default changed")
            overflow_per_round = 4
            event_count = queue_capacity + overflow_per_round
            try:
                request = PairRequest(
                    request_id=request_id,
                    daemon_instance_id=daemon_id,
                    pairing_code="123456",
                    target_mode="python_sdk",
                    websocket_port=server.bound_port,
                )
                pair_reply = ctypes.create_string_buffer(1024)
                self.assertEqual(self.lib.desk_sdk_bridge_process_pairing_udp(
                    encode_udp_message(request), pair_reply, len(pair_reply)
                ), 0)
                pair_accept = parse_udp_message(pair_reply.value)
                pairing.start_pairing(
                    pairing_code="123456", target_mode="python_sdk",
                    websocket_port=server.bound_port, now=0.0,
                )
                pairing.accept_device(pair_accept, peer_ip="127.0.0.1", now=0.5)
                self.assertEqual(self.lib.desk_sdk_test_set_pairing_target(
                    b"127.0.0.1", server.bound_port
                ), 0)
                self.assertEqual(await asyncio.to_thread(
                    self.lib.desk_sdk_ws_client_connect_paired
                ), 0)
                self.assertTrue(self.lib.desk_sdk_ws_client_is_authenticated())
                await asyncio.to_thread(desktop.start)

                expected_drop_total = 0
                expected_runtime_total = 0
                for round_index in range(2):
                    base_timestamp = 10_000 * (round_index + 1)
                    drops_before = robot.inputs.dropped_events
                    for index in range(event_count):
                        timestamp_ms = base_timestamp + index
                        delta = 1 if index % 2 == 0 else -1
                        self.assertEqual(self.lib.desk_sdk_bridge_send_roller_input(
                            delta, timestamp_ms
                        ), 0)
                        target_runtime_total = expected_runtime_total + index + 1
                        deadline = time.monotonic() + 3.0
                        while (len(runtime_inputs) < target_runtime_total
                               and time.monotonic() < deadline):
                            await asyncio.sleep(0.005)
                        self.assertGreaterEqual(
                            len(runtime_inputs), target_runtime_total,
                            "Pinned Runtime did not receive every production C event",
                        )

                    expected_drop_total += overflow_per_round
                    expected_runtime_total += event_count
                    deadline = time.monotonic() + 3.0
                    while (robot.inputs.dropped_events < expected_drop_total
                           and time.monotonic() < deadline):
                        await asyncio.sleep(0.005)
                    self.assertEqual(
                        robot.inputs.dropped_events - drops_before,
                        overflow_per_round,
                    )
                    self.assertEqual(len(runtime_inputs), expected_runtime_total)
                    self.assertEqual(robot.inputs._queue.qsize(), queue_capacity)

                    retained_timestamps = []
                    retained_deltas = []
                    for _ in range(queue_capacity):
                        event = await asyncio.to_thread(robot.inputs.wait, 2.0)
                        retained_timestamps.append(event.timestamp_ms)
                        retained_deltas.append(event.delta)
                    expected_start = base_timestamp + overflow_per_round
                    self.assertEqual(
                        retained_timestamps,
                        list(range(expected_start, base_timestamp + event_count)),
                    )
                    self.assertEqual(
                        retained_deltas,
                        [1 if index % 2 == 0 else -1
                         for index in range(overflow_per_round, event_count)],
                    )
                    self.assertEqual(robot.inputs._queue.qsize(), 0)
                    self.assertEqual(robot.inputs.dropped_events, expected_drop_total)

                self.assertEqual(len(runtime_inputs), 2 * event_count)
            finally:
                desktop.close()
                poll_stop.set()
                await poll_task
                await server.stop()

        asyncio.run(exercise_runtime())

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
        self.assertEqual(rc, 1)  # DESK_SDK_PAIRING_REJECTED
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
        self.assertEqual(rc, 1)  # DESK_SDK_PAIRING_REJECTED
        self.assertEqual(out_buf.value, b"")

    def test_official_udp_pairing_rejects_unsupported_protocol_and_version(self) -> None:
        """Unknown pairing versions/protocols fail closed without consuming the code window."""
        request = PairRequest(
            request_id="21a9dbf05ea3443480e62076f79a3b12",
            daemon_instance_id="f730f29e670c49f7a3320c4314eb9805",
            pairing_code="123456",
            target_mode="python_sdk",
            websocket_port=8765,
        )
        official = json.loads(encode_udp_message(request))
        unsupported = [
            {**official, "version": "9.9"},
            {**official, "protocol": "unknown-pairing"},
            {key: value for key, value in official.items() if key != "protocol"},
        ]
        for payload in unsupported:
            with self.subTest(protocol=payload.get("protocol"), version=payload.get("version")):
                self.assertEqual(self.lib.desk_sdk_bridge_set_pairing_code(b"123456"), 0)
                out_buf = ctypes.create_string_buffer(1024)
                rc = self.lib.desk_sdk_bridge_process_pairing_udp(
                    json.dumps(payload).encode("utf-8"), out_buf, len(out_buf),
                )
                self.assertEqual(rc, 1)
                self.assertEqual(out_buf.value, b"")
                self.assertTrue(self.lib.desk_sdk_bridge_is_pairing_code_configured())

    def test_official_hardware_hello_does_not_negotiate_unimplemented_audio(self) -> None:
        """Hardware hello must not claim Opus uplink without its media path."""
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
        self.assertNotIn("audio_uplink", resp["data"].get("negotiated", {}))

    def test_official_desktop_hello_and_ready_get(self) -> None:
        """Verifies official Desktop hello and sys.sdk.ready.get capability negotiation."""
        resp = self._exec_command({
            "type": "sys.client.hello",
            "code": 0,
            "data": {"role": "desktop", "version": "1.0"},
        })
        self.assertEqual(resp["type"], "sys.ack")
        caps = resp["data"]["capabilities"]
        self.assertEqual(caps, [
            "motion", "camera", "expression.audio_follow.v1", "expression", "mcp_tools", "inputs",
        ])
        self.assertNotIn("audio.stream", caps)
        self.assertNotIn("audio_stream", caps)
        self.assertIn("expression.audio_follow.v1", caps)

        # sys.sdk.ready.get returns identical capabilities
        resp_ready = self._exec_command({
            "type": "sys.sdk.ready.get",
            "code": 0,
            "data": {"command_id": "cmd-ready-01"},
        })
        self.assertEqual(resp_ready["type"], "sys.ack")
        self.assertEqual(resp_ready["data"]["capabilities"], caps)

    def test_audio_playback_capability_tracks_hal_readiness(self) -> None:
        """Only advertise the SDK's PCM playback capability when A1 audio HAL is ready."""
        set_ready = self.lib.desk_sdk_test_audio_set_hal_ready
        set_ready.argtypes = [ctypes.c_int]
        set_ready.restype = None
        set_ready(0)
        hello = self._exec_command({"type": "sys.client.hello", "code": 0,
                                    "data": {"role": "desktop", "version": "1.0"}})
        self.assertNotIn("audio.stream", hello["data"]["capabilities"])

        # Readiness is queried for every capability response, so a later HAL
        # initialization failure/readiness loss cannot leave stale support.
        set_ready(1)
        ready = self._exec_command({"type": "sys.sdk.ready.get", "code": 0,
                                    "data": {"command_id": "cmd-audio-ready-01"}})
        self.assertIn("audio.stream", ready["data"]["capabilities"])
        self.assertNotIn("audio_uplink", ready["data"])
        self.assertNotIn("wake", ready["data"]["capabilities"])

        set_ready(0)
        unavailable = self._exec_command({"type": "sys.sdk.ready.get", "code": 0,
                                           "data": {"command_id": "cmd-audio-ready-02"}})
        self.assertNotIn("audio.stream", unavailable["data"]["capabilities"])

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
        # The device requires an explicit camera lease before capture. Use the
        # same client identity for acquire/release, matching the SDK lifecycle.
        lease_resp = self._exec_command({
            "type": "ctrl.camera.acquire",
            "client_id": "sdk-client",
            "lease_duration_ms": 10000,
            "data": {"command_id": "cmd-cam-acquire-01"},
        })
        self.assertEqual(lease_resp["type"], "sys.ack", lease_resp)
        self.assertEqual(lease_resp["code"], 0, lease_resp)
        self.assertTrue(lease_resp["data"]["accepted"], lease_resp)

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

        try:
            # 3. Send ctrl.camera.capture under the acquired lease.
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
            self.assertEqual(resp["type"], "sys.ack", resp)
            self.assertEqual(resp["code"], 0, resp)
            session_id = resp["data"].get("session_id")
            self.assertIsInstance(session_id, int)
            self.assertGreater(session_id, 0)

            # 4. Verify WSPK binary frame was dispatched and unpacks using official parse_wspk.
            self.assertEqual(len(intercepted_frames), 1)
            wspk_pkt = intercepted_frames[0]
            parsed_binary = parse_wspk(wspk_pkt)

            self.assertEqual(parsed_binary.frame_type, FRAME_IMAGE)
            self.assertEqual(parsed_binary.stream_id, session_id)
            self.assertEqual(parsed_binary.payload, test_jpeg)
        finally:
            release_resp = self._exec_command({
                "type": "ctrl.camera.release",
                "client_id": "sdk-client",
                "data": {"command_id": "cmd-cam-release-01"},
            })
            self.assertEqual(release_resp["type"], "sys.ack", release_resp)
            self.assertEqual(release_resp["code"], 0, release_resp)
            self.assertTrue(release_resp["data"]["accepted"], release_resp)

    def test_official_microphone_open_is_explicitly_unsupported(self) -> None:
        """Microphone control must not claim success without Opus WSPK uplink."""
        resp_open = self._exec_command({
            "type": "ctrl.microphone.open",
            "code": 0,
            "data": {"sample_rate_hz": 16000, "command_id": "cmd-mic-open-01"},
        })
        self.assertEqual(resp_open["type"], "sys.nack")
        self.assertEqual(resp_open["error"]["code"], "UNSUPPORTED_OPERATION")

    def test_audio_playback_commands_require_authenticated_sdk_session(self) -> None:
        """PCM playback commands are rejected outside the authenticated WS session."""
        for msg_type in ("ctrl.audio.stream.begin", "ctrl.audio.stop"):
            with self.subTest(msg_type=msg_type):
                response = self._exec_command({
                    "type": msg_type,
                    "code": 0,
                    "data": {"stream_id": 1, "total_bytes": 320,
                             "sample_rate_hz": 24000, "channels": 1,
                             "sample_width_bytes": 2, "audio_sha256": "0" * 64},
                })
                self.assertEqual(response["type"], "sys.nack")
                self.assertEqual(response["error"]["code"], "WEBSOCKET_SESSION_REQUIRED")
                ws_out = ctypes.create_string_buffer(1024)
                self.assertEqual(self._process_ws_command(
                    json.dumps({
                        "type": msg_type, "code": 0,
                        "request_id": "audio-unauthenticated-ws",
                        "data": {"stream_id": 1, "total_bytes": 320,
                                 "sample_rate_hz": 24000, "channels": 1,
                                 "sample_width_bytes": 2, "audio_sha256": "0" * 64},
                    }).encode(), ws_out), 0)
                self.assertEqual(json.loads(ws_out.value)["error"]["code"], "AUTH_FAILED")

    def test_audio_playback_commands_cannot_cross_from_authenticated_ws_to_legacy_tcp(self) -> None:
        """An authenticated WS latch must not authorize the legacy TCP dispatcher."""
        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_advance_ws_generation()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        begin = {"type": "ctrl.audio.stream.begin", "code": 0,
                 "request_id": "audio-tcp-isolation-begin", "data": {
                     "stream_id": 801, "total_bytes": 320,
                     "sample_rate_hz": 24000, "channels": 1,
                     "sample_width_bytes": 2, "audio_sha256": "0" * 64}}
        out = ctypes.create_string_buffer(1024)
        self.assertEqual(self._process_ws_command(json.dumps(begin).encode(), out), 0)
        self.assertEqual(json.loads(out.value)["type"], "sys.ack")
        self.assertEqual(self.lib.desk_sdk_test_audio_begin_calls(), 1)
        self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 1)

        for command in (
            {"type": "ctrl.audio.stream.begin", "code": 0,
             "request_id": "audio-tcp-begin", "data": {
                 "stream_id": 802, "total_bytes": 320,
                 "sample_rate_hz": 24000, "channels": 1,
                 "sample_width_bytes": 2, "audio_sha256": "0" * 64}},
            {"type": "ctrl.audio.stop", "code": 0,
             "request_id": "audio-tcp-stop", "data": {"stream_id": 801}},
        ):
            tcp_out = ctypes.create_string_buffer(1024)
            self.assertEqual(self.lib.desk_sdk_bridge_process_command(
                json.dumps(command).encode(), tcp_out, len(tcp_out)), 0)
            response = json.loads(tcp_out.value)
            self.assertEqual(response["type"], "sys.nack", response)
            self.assertEqual(response["error"]["code"], "WEBSOCKET_SESSION_REQUIRED", response)

        self.assertEqual(self.lib.desk_sdk_test_audio_begin_calls(), 1)
        self.assertEqual(self.lib.desk_sdk_test_audio_stop_calls(), 0)
        self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 1)

    def test_audio_stop_requires_current_generation_and_exact_stream_id(self) -> None:
        """Stale generations and missing/wrong stream IDs cannot stop playback."""
        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_advance_ws_generation()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        begin = {"type": "ctrl.audio.stream.begin", "code": 0,
                 "request_id": "audio-owner-begin", "data": {
                     "stream_id": 803, "total_bytes": 320,
                     "sample_rate_hz": 24000, "channels": 1,
                     "sample_width_bytes": 2, "audio_sha256": "0" * 64}}
        out = ctypes.create_string_buffer(1024)
        self.assertEqual(self._process_ws_command(json.dumps(begin).encode(), out), 0)
        self.assertEqual(json.loads(out.value)["type"], "sys.ack")

        for message in (
            {"type": "ctrl.audio.stop", "code": 0, "data": {}},
            {"type": "ctrl.audio.stop", "code": 0, "data": {"stream_id": 804}},
        ):
            stop_out = ctypes.create_string_buffer(1024)
            self.assertEqual(self._process_ws_command(json.dumps(message).encode(), stop_out), 0)
            self.assertEqual(json.loads(stop_out.value)["type"], "sys.nack")
            self.assertEqual(self.lib.desk_sdk_test_audio_stop_calls(), 0)
            self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 1)

        valid_stop = {"type": "ctrl.audio.stop", "code": 0,
                      "request_id": "audio-owner-stop", "data": {"stream_id": 803}}
        valid_out = ctypes.create_string_buffer(1024)
        self.assertEqual(self._process_ws_command(json.dumps(valid_stop).encode(), valid_out), 0)
        self.assertEqual(json.loads(valid_out.value)["type"], "sys.ack")
        self.assertEqual(self.lib.desk_sdk_test_audio_stop_calls(), 1)
        self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 0)
        self.lib.desk_sdk_bridge_audio_poll(self.lib.desk_sdk_test_ws_generation())

        begin_again = dict(begin)
        begin_again["request_id"] = "audio-owner-begin-again"
        begin_again["data"] = dict(begin["data"], stream_id=805)
        self.assertEqual(self._process_ws_command(json.dumps(begin_again).encode(), out), 0)
        self.assertEqual(json.loads(out.value)["type"], "sys.ack")

        self.lib.desk_sdk_test_advance_ws_generation()
        stale_stop = {"type": "ctrl.audio.stop", "code": 0,
                      "request_id": "audio-owner-stale-stop", "data": {"stream_id": 805}}
        stale_out = ctypes.create_string_buffer(1024)
        self.assertEqual(self._process_ws_command(json.dumps(stale_stop).encode(), stale_out), 0)
        self.assertEqual(json.loads(stale_out.value)["type"], "sys.nack")
        self.assertEqual(self.lib.desk_sdk_test_audio_stop_calls(), 1)
        self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 1)

    def test_audio_begin_generation_guard_is_atomic_with_disconnect(self) -> None:
        """Disconnect cannot advance the generation between begin validation and HAL entry."""
        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_advance_ws_generation()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        generation_before = self.lib.desk_sdk_test_ws_generation()
        command = {"type": "ctrl.audio.stream.begin", "code": 0,
                   "request_id": "audio-begin-race", "data": {
                       "stream_id": 806, "total_bytes": 320,
                       "sample_rate_hz": 24000, "channels": 1,
                       "sample_width_bytes": 2, "audio_sha256": "0" * 64}}
        out = ctypes.create_string_buffer(1024)
        command_rc: list[int] = []
        self.lib.desk_sdk_test_audio_pause_next_begin()
        command_thread = threading.Thread(target=lambda: command_rc.append(
            self._process_ws_command(json.dumps(command).encode(), out)))
        command_thread.start()
        deadline = time.monotonic() + 3.0
        while not self.lib.desk_sdk_test_audio_begin_is_paused() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.lib.desk_sdk_test_audio_begin_is_paused(), "begin did not reach HAL gate")

        generation_advanced = threading.Event()
        advance_thread = threading.Thread(target=lambda: (
            self.lib.desk_sdk_test_advance_ws_generation(), generation_advanced.set()))
        advance_thread.start()
        try:
            time.sleep(0.05)
            advanced_while_hal_paused = generation_advanced.is_set()
        finally:
            self.lib.desk_sdk_test_audio_release_begin()
        command_thread.join(timeout=3.0)
        advance_thread.join(timeout=3.0)
        self.assertFalse(command_thread.is_alive(), "audio begin did not release its lifecycle guard")
        self.assertFalse(advance_thread.is_alive(), "disconnect did not complete after HAL begin")
        self.assertFalse(advanced_while_hal_paused, "generation advanced while the guarded HAL call was in flight")
        self.assertEqual(command_rc, [0])
        self.assertEqual(self.lib.desk_sdk_test_audio_stale_begin_calls(), 0)
        self.assertEqual(self.lib.desk_sdk_test_ws_generation(), generation_before + 1)

    def test_audio_stop_generation_guard_is_atomic_with_disconnect(self) -> None:
        """Disconnect cannot replace the owner between stop validation and HAL entry."""
        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_advance_ws_generation()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        begin = {"type": "ctrl.audio.stream.begin", "code": 0,
                 "request_id": "audio-stop-race-begin", "data": {
                     "stream_id": 807, "total_bytes": 320,
                     "sample_rate_hz": 24000, "channels": 1,
                     "sample_width_bytes": 2, "audio_sha256": "0" * 64}}
        begin_out = ctypes.create_string_buffer(1024)
        self.assertEqual(self._process_ws_command(json.dumps(begin).encode(), begin_out), 0)
        self.assertEqual(json.loads(begin_out.value)["type"], "sys.ack")
        generation_before = self.lib.desk_sdk_test_ws_generation()
        stop = {"type": "ctrl.audio.stop", "code": 0,
                "request_id": "audio-stop-race", "data": {"stream_id": 807}}
        stop_out = ctypes.create_string_buffer(1024)
        stop_rc: list[int] = []
        self.lib.desk_sdk_test_audio_pause_next_stop()
        stop_thread = threading.Thread(target=lambda: stop_rc.append(
            self._process_ws_command(json.dumps(stop).encode(), stop_out)))
        stop_thread.start()
        deadline = time.monotonic() + 3.0
        while not self.lib.desk_sdk_test_audio_stop_is_paused() and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertTrue(self.lib.desk_sdk_test_audio_stop_is_paused(), "stop did not reach HAL gate")

        generation_advanced = threading.Event()
        advance_thread = threading.Thread(target=lambda: (
            self.lib.desk_sdk_test_advance_ws_generation(), generation_advanced.set()))
        advance_thread.start()
        try:
            time.sleep(0.05)
            advanced_while_hal_paused = generation_advanced.is_set()
        finally:
            self.lib.desk_sdk_test_audio_release_stop()
        stop_thread.join(timeout=3.0)
        advance_thread.join(timeout=3.0)
        self.assertFalse(stop_thread.is_alive(), "audio stop did not release its lifecycle guard")
        self.assertFalse(advance_thread.is_alive(), "disconnect did not complete after HAL stop")
        self.assertFalse(advanced_while_hal_paused, "generation advanced while the guarded HAL call was in flight")
        self.assertEqual(stop_rc, [0])
        self.assertEqual(self.lib.desk_sdk_test_audio_stale_stop_calls(), 0)
        self.assertEqual(self.lib.desk_sdk_test_ws_generation(), generation_before + 1)

    def test_stale_audio_generation_cannot_cancel_current_playback(self) -> None:
        """Old-session PCM and poll callbacks must not stop a newer session's HAL owner."""
        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_advance_ws_generation()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        generation = self.lib.desk_sdk_test_ws_generation()
        pcm = b"\x12\x34" * 160
        begin = {"type": "ctrl.audio.stream.begin", "code": 0,
                 "request_id": "audio-current-generation", "data": {
                     "stream_id": 808, "total_bytes": len(pcm),
                     "sample_rate_hz": 24000, "channels": 1,
                     "sample_width_bytes": 2,
                     "audio_sha256": hashlib.sha256(pcm).hexdigest()}}
        begin_out = ctypes.create_string_buffer(1024)
        self.assertEqual(self._process_ws_command(json.dumps(begin).encode(), begin_out), 0)
        self.assertEqual(json.loads(begin_out.value)["type"], "sys.ack")
        self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 1)

        inject = self.lib.desk_sdk_test_audio_inject
        inject.restype = ctypes.c_int
        inject.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]
        stale_frame = build_wspk(FRAME_AUDIO, FLAG_FIRST | FLAG_LAST, 808, 0, pcm)
        old_generation = generation - 1

        self.lib.desk_sdk_bridge_audio_poll(old_generation)
        self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 1,
                         "a delayed old-session poll must not cancel current playback")
        self.assertEqual(self.lib.desk_sdk_test_audio_stop_calls(), 0)

        self.assertNotEqual(inject(stale_frame, len(stale_frame), old_generation), 0)
        self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 1,
                         "a delayed old-session frame must not cancel current playback")
        self.assertEqual(self.lib.desk_sdk_test_audio_stop_calls(), 0)

        self.assertEqual(inject(stale_frame, len(stale_frame), generation), 0,
                         "the owning session must still be able to finish its stream")
        self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 0)

    def test_inactive_audio_frame_releases_generation_pin_for_teardown(self) -> None:
        """Rejecting a current-generation frame with no stream must not strand teardown."""
        self.lib.desk_sdk_test_prepare_log_pair()
        self.lib.desk_sdk_test_advance_ws_generation()
        self.lib.desk_sdk_test_seed_ws_auth_latch()
        generation = self.lib.desk_sdk_test_ws_generation()
        pcm = b"\x00\x00" * 160
        frame = build_wspk(FRAME_AUDIO, FLAG_FIRST | FLAG_LAST, 809, 0, pcm)
        inject = self.lib.desk_sdk_test_audio_inject
        inject.restype = ctypes.c_int
        inject.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]
        inflight_before = self.lib.desk_sdk_test_ws_audio_inflight()

        self.assertNotEqual(inject(frame, len(frame), generation), 0)
        inflight_after = self.lib.desk_sdk_test_ws_audio_inflight()
        try:
            self.assertEqual(inflight_after, inflight_before,
                             "no-stream rejection must release its lifecycle pin")
        finally:
            # Keep the shared test library usable if this regression assertion
            # fails before the production early-return path is fixed.
            for _ in range(max(0, inflight_after - inflight_before)):
                self.lib.desk_sdk_ws_client_audio_command_release()

        next_generation = self.lib.desk_sdk_test_advance_ws_generation()
        self.assertEqual(next_generation, generation + 1)

    def test_audio_playback_stream_runs_over_pinned_runtime_loopback(self) -> None:
        """Exercise begin, multi-frame WSPK, HAL-backed credit, completion and cancel."""
        try:
            import psutil  # noqa: F401
        except ImportError:
            sys.modules.setdefault("psutil", types.ModuleType("psutil"))
        from watcherobot.runtime.daemon.pairing.session import DevicePairingSession
        from watcherobot.runtime.daemon.connections.websocket_server import ExternalWebSocketServer
        import hashlib

        request_id = "a1a9dbf05ea3443480e62076f79a3b12"
        daemon_id = "b730f29e670c49f7a3320c4314eb9805"
        pairing = DevicePairingSession(daemon_instance_id=daemon_id,
                                      request_id_factory=lambda: request_id)

        async def exercise_runtime() -> None:
            async def authorize(hardware_hello, peer_ip: str) -> None:
                pairing.connect_device(hardware_hello, peer_ip=peer_ip, now=time.monotonic())

            server = ExternalWebSocketServer(host="127.0.0.1", port=0,
                hardware_hello_authorizer=authorize)
            await server.start()
            poll_stop = asyncio.Event()

            async def poll_c_client() -> None:
                while not poll_stop.is_set():
                    await asyncio.to_thread(self.lib.desk_sdk_ws_client_poll, 20)

            poll_task = asyncio.create_task(poll_c_client())
            try:
                request = PairRequest(request_id=request_id, daemon_instance_id=daemon_id,
                    pairing_code="123456", target_mode="python_sdk", websocket_port=server.bound_port)
                pair_reply = ctypes.create_string_buffer(1024)
                rc = self.lib.desk_sdk_bridge_process_pairing_udp(encode_udp_message(request),
                                                                  pair_reply, len(pair_reply))
                self.assertEqual(rc, 0)
                pair_accept = parse_udp_message(pair_reply.value)
                pairing.start_pairing(pairing_code="123456", target_mode="python_sdk",
                                      websocket_port=server.bound_port, now=time.monotonic())
                pairing.accept_device(pair_accept, peer_ip="127.0.0.1", now=time.monotonic())
                self.assertEqual(self.lib.desk_sdk_test_set_pairing_target(
                    b"127.0.0.1", server.bound_port), 0)
                self.assertEqual(await asyncio.to_thread(self.lib.desk_sdk_ws_client_connect_paired), 0)

                async with websockets.connect(server.url) as desktop:
                    self.lib.desk_sdk_test_audio_set_hal_ready(1)
                    await desktop.send(json.dumps({"type": "sys.client.hello", "code": 0,
                        "data": {"role": "desktop", "version": "1.0"}}))
                    desktop_hello = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(desktop_hello["type"], "sys.ack", desktop_hello)
                    await desktop.send(json.dumps({"type": "sys.sdk.ready.get", "code": 0,
                        "request_id": "audio-ready-capability", "data": {}}))
                    capability_ack = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(capability_ack["type"], "sys.ack", capability_ack)
                    self.assertIn("audio.stream", capability_ack["data"]["capabilities"], capability_ack)
                    self.assertNotIn("audio_uplink", capability_ack["data"], capability_ack)

                    stream_id = 71
                    pcm = bytes((i * 7) & 0xff for i in range(8192))
                    await desktop.send(json.dumps({"type": "ctrl.audio.stream.begin", "code": 0,
                        "request_id": "audio-begin-01", "data": {
                            "stream_id": stream_id, "total_bytes": len(pcm),
                            "sample_rate_hz": 24000, "channels": 1,
                            "sample_width_bytes": 2,
                            "audio_sha256": hashlib.sha256(pcm).hexdigest()}}))
                    begin_ack = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual((begin_ack["type"], begin_ack["request_id"]),
                                     ("sys.ack", "audio-begin-01"), begin_ack)
                    # The pinned SDK waits for initial credit before sending
                    # any WSPK frame; credit must reflect HAL free slots.
                    initial_flow = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(initial_flow["type"], "evt.audio.buffer_status", initial_flow)
                    self.assertEqual(initial_flow["data"]["reason"], "running", initial_flow)
                    self.assertEqual(initial_flow["data"]["free_frames"], 8, initial_flow)
                    for sequence, offset in enumerate((0, 4096)):
                        flags = FLAG_FIRST if sequence == 0 else 0
                        if sequence == 1:
                            flags |= FLAG_LAST
                        await desktop.send(build_wspk(FRAME_AUDIO, flags, stream_id,
                                                      sequence, pcm[offset:offset + 4096]))
                        flow = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                        self.assertEqual(flow["type"], "evt.audio.buffer_status", flow)
                        self.assertEqual(flow["data"]["free_frames"], 7 - sequence, flow)

                    complete = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(complete["type"], "evt.audio.buffer_status", complete)
                    self.assertEqual(complete["data"]["reason"], "complete", complete)
                    self.assertEqual(complete["data"]["audio_sha256"], hashlib.sha256(pcm).hexdigest())

                    # Bad declared SHA must stop/drain playback and surface the
                    # actual digest so the pinned Robot marks its job failed.
                    bad_digest_pcm = b"\x12\x34" * 4096
                    await desktop.send(json.dumps({"type": "ctrl.audio.stream.begin", "code": 0,
                        "request_id": "audio-begin-bad-sha", "data": {
                            "stream_id": 72, "total_bytes": len(bad_digest_pcm),
                            "sample_rate_hz": 24000, "channels": 1, "sample_width_bytes": 2,
                            "audio_sha256": "0" * 64}}))
                    bad_sha_ack = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(bad_sha_ack["type"], "sys.ack", bad_sha_ack)
                    self.assertEqual(json.loads(await asyncio.wait_for(desktop.recv(), 2))["data"]["free_frames"], 8)
                    await desktop.send(build_wspk(FRAME_AUDIO, FLAG_FIRST, 72, 0,
                                                  bad_digest_pcm[:4096]))
                    await desktop.recv()
                    await desktop.send(build_wspk(FRAME_AUDIO, FLAG_LAST, 72, 1,
                                                  bad_digest_pcm[4096:]))
                    await desktop.recv()  # Credit reflects the HAL after final enqueue.
                    bad_sha_result = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(bad_sha_result["data"]["reason"], "complete", bad_sha_result)
                    self.assertEqual(bad_sha_result["data"]["audio_sha256"],
                                     hashlib.sha256(bad_digest_pcm).hexdigest())
                    self.assertNotEqual(bad_sha_result["data"]["audio_sha256"], "0" * 64)

                    # A delayed frame from a prior generation is rejected
                    # without canceling the stream owned by the current session.
                    stale_pcm = b"\x23\x45" * 4096
                    await desktop.send(json.dumps({"type": "ctrl.audio.stream.begin", "code": 0,
                        "request_id": "audio-begin-stale", "data": {
                            "stream_id": 73, "total_bytes": len(stale_pcm),
                            "sample_rate_hz": 24000, "channels": 1, "sample_width_bytes": 2,
                            "audio_sha256": hashlib.sha256(stale_pcm).hexdigest()}}))
                    stale_ack = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(stale_ack["type"], "sys.ack", stale_ack)
                    self.assertEqual(json.loads(await asyncio.wait_for(desktop.recv(), 2))["data"]["free_frames"], 8)
                    await desktop.send(build_wspk(FRAME_AUDIO, FLAG_FIRST, 73, 0, stale_pcm[:4096]))
                    await desktop.recv()
                    stale_frame = build_wspk(FRAME_AUDIO, FLAG_LAST, 73, 1, stale_pcm[4096:])
                    inject = self.lib.desk_sdk_test_audio_inject
                    inject.restype = ctypes.c_int
                    inject.argtypes = [ctypes.c_char_p, ctypes.c_size_t, ctypes.c_uint32]
                    old_generation = self.lib.desk_sdk_test_ws_generation() - 1
                    self.assertNotEqual(inject(stale_frame, len(stale_frame), old_generation), 0)
                    self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 1,
                                     "stale frame must not release the current session's owner")
                    await desktop.send(json.dumps({"type": "ctrl.audio.stop", "code": 0,
                        "request_id": "audio-stop-stale-owner", "data": {"stream_id": 73}}))
                    stale_owner_stop = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual((stale_owner_stop["type"], stale_owner_stop["request_id"]),
                                     ("sys.ack", "audio-stop-stale-owner"), stale_owner_stop)
                    stale_owner_cancel = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(stale_owner_cancel["data"]["reason"], "aborted", stale_owner_cancel)
                    self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 0)

                    # A duplicate/out-of-order packet is explicit failure.
                    sequence_pcm = b"\x34\x56" * 4096
                    await desktop.send(json.dumps({"type": "ctrl.audio.stream.begin", "code": 0,
                        "request_id": "audio-begin-seq", "data": {
                            "stream_id": 74, "total_bytes": len(sequence_pcm),
                            "sample_rate_hz": 24000, "channels": 1, "sample_width_bytes": 2,
                            "audio_sha256": hashlib.sha256(sequence_pcm).hexdigest()}}))
                    seq_ack = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(seq_ack["type"], "sys.ack", seq_ack)
                    self.assertEqual(json.loads(await asyncio.wait_for(desktop.recv(), 2))["data"]["free_frames"], 8)
                    sequence_frame = build_wspk(FRAME_AUDIO, FLAG_FIRST, 74, 1, sequence_pcm[:4096])
                    self.assertEqual(inject(sequence_frame, len(sequence_frame),
                                            self.lib.desk_sdk_test_ws_generation()), 0)
                    sequence_result = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(sequence_result["data"]["reason"], "sequence_gap", sequence_result)

                    pcm_cancel = b"\x00\x00" * 1024
                    await desktop.send(json.dumps({"type": "ctrl.audio.stream.begin", "code": 0,
                        "request_id": "audio-begin-02", "data": {
                            "stream_id": 75, "total_bytes": len(pcm_cancel),
                            "sample_rate_hz": 24000, "channels": 1,
                            "sample_width_bytes": 2,
                            "audio_sha256": hashlib.sha256(pcm_cancel).hexdigest()}}))
                    cancel_begin = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(cancel_begin["type"], "sys.ack", cancel_begin)
                    self.assertEqual(json.loads(await asyncio.wait_for(desktop.recv(), 2))["data"]["free_frames"], 8)
                    await desktop.send(json.dumps({"type": "ctrl.audio.stop", "code": 0,
                        "request_id": "audio-stop-02", "data": {"stream_id": 75}}))
                    stop_ack = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual((stop_ack["type"], stop_ack["request_id"]),
                                     ("sys.ack", "audio-stop-02"), stop_ack)
                    cancelled = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(cancelled["type"], "evt.audio.buffer_status", cancelled)
                    self.assertEqual(cancelled["data"]["reason"], "aborted", cancelled)

                    # Disconnect cancellation keeps the HAL stream owner alive
                    # until the asynchronous output drain is confirmed.
                    disconnect_pcm = b"\x56\x78" * 512
                    await desktop.send(json.dumps({"type": "ctrl.audio.stream.begin", "code": 0,
                        "request_id": "audio-begin-disconnect", "data": {
                            "stream_id": 76, "total_bytes": len(disconnect_pcm),
                            "sample_rate_hz": 24000, "channels": 1,
                            "sample_width_bytes": 2,
                            "audio_sha256": hashlib.sha256(disconnect_pcm).hexdigest()}}))
                    disconnect_ack = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(disconnect_ack["type"], "sys.ack", disconnect_ack)
                    disconnect_credit = json.loads(await asyncio.wait_for(desktop.recv(), 2))
                    self.assertEqual(disconnect_credit["data"]["free_frames"], 8, disconnect_credit)
                    self.lib.desk_sdk_test_audio_hold_stop_drain(1)
                    self.lib.desk_sdk_test_ws_force_disconnect()
                    self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 1,
                                     "disconnect must not release playback owner before drain")
                    self.lib.desk_sdk_test_audio_release_drain()
                    self.lib.desk_sdk_bridge_audio_poll(self.lib.desk_sdk_test_ws_generation())
                    self.assertEqual(self.lib.desk_sdk_test_audio_is_active(), 0,
                                     "owner is released after HAL confirms output drain")
            finally:
                poll_stop.set()
                await poll_task
                await server.stop()

        asyncio.run(exercise_runtime())

    def test_official_microphone_close_is_explicitly_unsupported(self) -> None:
        response = self._exec_command({
            "type": "ctrl.microphone.close",
            "code": 0,
            "data": {"session_id": 1, "command_id": "cmd-mic-close-01"},
        })
        self.assertEqual(response["type"], "sys.nack")
        self.assertEqual(response["error"]["code"], "UNSUPPORTED_OPERATION")

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
