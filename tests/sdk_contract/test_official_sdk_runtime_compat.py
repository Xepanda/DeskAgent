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
import json
import os
import subprocess
import sys
import tempfile
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
                'int desk_sdk_test_set_pairing_target(const char *host, unsigned short port) {\n'
                '  if (!host || !port || !s_paired || strlen(host) >= sizeof(s_pair_host)) return -1;\n'
                '  strcpy(s_pair_host, host); s_pair_websocket_port = port; return 0;\n'
                '}\n'
                'const char *desk_sdk_test_last_ack(void) { return (const char *)s_ws_ack; }\n'
                'const char *desk_sdk_test_http_response(void) { return s_ws_http_response; }\n'
                'int desk_sdk_test_ws_state(void) { return (int)s_ws_state; }\n'
                'int desk_sdk_test_preflight(void) { char host[64], hello[512]; uint16_t port = 0; '
                'return desk_sdk_bridge_get_pairing_target(host, sizeof(host), &port) == 0 && '
                'desk_sdk_bridge_build_hardware_hello(hello, sizeof(hello)) > 0 ? 1 : 0; }\n'
            )

        # Compile C shared library
        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-DDESK_SDK_OPENSSL_TESTING",
            f"-I{repo_root}/firmware/components/desk_sdk_bridge/include",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"-I{repo_root}/firmware/components/desk_tclaw/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{repo_root}/firmware/components/desk_tclaw/src/desk_tclaw_bridge.c",
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
            finally:
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
        self.assertIn("inputs", caps)
        self.assertIn("motion", caps)
        self.assertIn("camera", caps)
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

    def test_audio_playback_commands_are_explicitly_unsupported(self) -> None:
        """PCM playback begin/stop must not silently accept a missing media path."""
        for msg_type in ("ctrl.audio.stream.begin", "ctrl.audio.stop"):
            with self.subTest(msg_type=msg_type):
                response = self._exec_command({
                    "type": msg_type,
                    "code": 0,
                    "data": {"stream_id": 1, "total_bytes": 320},
                })
                self.assertEqual(response["type"], "sys.nack")
                self.assertEqual(response["error"]["code"], "UNSUPPORTED_OPERATION")

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
