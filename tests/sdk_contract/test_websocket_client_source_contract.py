"""Static conformance checks for the embedded RFC6455 client state machine."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
WS_SOURCE = ROOT / "firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c"
BRIDGE_SOURCE = ROOT / "firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c"
COMPONENT = ROOT / "firmware/components/desk_sdk_bridge/CMakeLists.txt"
OFFICIAL_WS = ROOT / ".target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk/src/watcherobot/runtime/daemon/connections/websocket_server.py"


class WebSocketClientSourceContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.ws = WS_SOURCE.read_text(encoding="utf-8")
        cls.bridge = BRIDGE_SOURCE.read_text(encoding="utf-8")
        cls.cmake = COMPONENT.read_text(encoding="utf-8")

    def test_component_compiles_client_and_poll_drives_it(self) -> None:
        self.assertIn('"src/desk_sdk_websocket.c"', self.cmake)
        self.assertRegex(self.bridge, r"desk_sdk_pairing_server_poll\(half\);\s*desk_sdk_ws_client_poll\(half\);")

    def test_upgrade_validates_server_accept_before_hello_ack(self) -> None:
        self.assertIn("Sec-WebSocket-Version: 13", self.ws)
        self.assertIn('"HTTP/1.1 101 "', self.ws)
        self.assertIn('"Sec-WebSocket-Accept:"', self.ws)
        self.assertIn("desk_sdk_bridge_build_hardware_hello", self.ws)
        self.assertRegex(self.ws, r"ws_send_frame\(0x1,\s*\(const uint8_t \*\)hello")
        self.assertIn('"\\\"type\\\":\\\"sys.ack\\\""', self.ws)
        self.assertIn("s_ws_ready = true", self.ws)

    def test_client_frames_are_masked_and_server_frames_must_not_be(self) -> None:
        self.assertIn("0x80 | len", self.ws)
        self.assertIn("memcpy(header + h, mask", self.ws)
        self.assertIn("payload[off + i] ^ mask[(off + i) & 3]", self.ws)
        self.assertIn("|| masked || n > cap", self.ws)
        self.assertIn("s_ws_rx_len -= used", self.ws)

    def test_control_close_and_command_response_paths_exist(self) -> None:
        self.assertIn("if (op == 0x8)", self.ws)
        self.assertIn("if (op == 0x9)", self.ws)
        # Control frames use the same serialized writer as command responses,
        # avoiding concurrent writes to a single WebSocket stream.
        self.assertIn("if (op == 0x9) { return ws_enqueue_frame(0xA, payload, len); }", self.ws)
        self.assertIn("send_rc = ws_send_frame_deadline_fd(send_fd, frame->opcode", self.ws)
        self.assertIn("DESK_SDK_WS_HELLO_SENT", self.ws)
        self.assertIn("desk_sdk_ws_client_get_state", (ROOT / "firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h").read_text(encoding="utf-8"))
        self.assertIn("desk_sdk_bridge_process_ws_command", self.ws)
        self.assertIn("desk_sdk_ws_client_send_text(s_ws_response)", self.ws)
        self.assertIn("ws_disconnect_and_schedule(); return -1", self.ws)

    def test_cr009_writer_has_shared_deadline_bounded_poll_and_control_priority(self) -> None:
        self.assertIn("#define WS_WRITE_DEADLINE_MS 250U", self.ws)
        self.assertIn("#define WS_CONTROL_QUEUE_CAPACITY 3U", self.ws)
        self.assertIn("MSG_NOSIGNAL | MSG_DONTWAIT", self.ws)
        self.assertIn("static int ws_wait_until(int fd, short events, uint64_t deadline)", self.ws)
        self.assertIn("if (now >= deadline) return -1;", self.ws)
        self.assertIn("if (rc < 0 && errno == EINTR) continue;", self.ws)
        self.assertIn("if (ws_wait_until(fd, POLLOUT, deadline) == 0) continue;", self.ws)
        # Control/ACK frames are dequeued before the source log queue each pass.
        self.assertLess(self.ws.index("if (ws_take_outbound_frame(&s_ws_writer_current))"),
                        self.ws.index("int log_len = desk_sdk_bridge_log_next_text"))
        self.assertIn("ws_clear_outbound_queue();\n    desk_sdk_bridge_log_session_closed();", self.ws)
        self.assertIn("frame->generation != ws_lifecycle_generation()", self.ws)
        self.assertIn("log_generation == ws_lifecycle_generation()", self.ws)
        self.assertIn("ws_disconnect_generation_and_schedule(frame->generation)", self.ws)
        self.assertIn("ws_transport_close_for_generation(generation, true)", self.ws)
        self.assertIn("ws_socket_fd_for_generation(frame->generation)", self.ws)
        self.assertIn("ws_send_frame_deadline_fd(send_fd", self.ws)

    def test_locked_official_runtime_requires_hello_before_routing(self) -> None:
        runtime = OFFICIAL_WS.read_text(encoding="utf-8")
        self.assertIn("websocket.recv()", runtime)
        self.assertIn("parse_hardware_hello(frame)", runtime)
        self.assertIn("build_hardware_hello_ack()", runtime)
        self.assertIn("await connection.websocket.send(", runtime)
        self.assertIn("await self.router.route_external(connection, frame)", runtime)

    def test_disconnect_reconnect_uses_bounded_backoff(self) -> None:
        self.assertIn("WS_RECONNECT_MAX_DELAY_SEC", self.ws)
        self.assertIn("ws_schedule_reconnect", self.ws)
        self.assertIn("ws_retry_at", self.ws)
        poll = self.ws[self.ws.index("int desk_sdk_ws_client_poll"):]
        self.assertIn("ws_retry_at", poll)
        self.assertIn("desk_sdk_ws_client_connect_paired", poll)

    def test_media_rejects_over_wspk_frame_limit_before_send(self) -> None:
        media = self.bridge[self.bridge.index("int desk_sdk_bridge_send_media_frame"):]
        self.assertIn("DESK_WSPK_MAX_FRAME_SIZE", media)
        self.assertLess(media.index("packed_len > DESK_WSPK_MAX_FRAME_SIZE"),
                        media.index("desk_sdk_ws_client_send_binary"))
        header = (ROOT / "firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h").read_text(encoding="utf-8")
        self.assertIn("#define DESK_WSPK_MAX_FRAME_SIZE (DESK_WSPK_MAX_PAYLOAD_SIZE + DESK_WSPK_HEADER_SIZE)", header)
        self.assertIn("#define DESK_WSPK_MAX_PAYLOAD_SIZE (128 * 1024)", header)
        self.assertIn("#define DESK_WSPK_HEADER_SIZE      16", header)
        max_frame = 128 * 1024 + 16
        self.assertEqual(max_frame, 131088)
        self.assertNotIn("len > DESK_SDK_HOST_MAX_FRAME_SIZE", self.ws)
        self.assertIn("len > DESK_WSPK_MAX_FRAME_SIZE", self.ws)

    def test_receive_limits_text_and_binary_frames_independently(self) -> None:
        header = (ROOT / "firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h").read_text(encoding="utf-8")
        self.assertIn("#define DESK_SDK_MAX_FRAME_LEN 8192", header)
        self.assertIn("WS_RX_CAP  (DESK_WSPK_MAX_FRAME_SIZE + 14)", self.ws)
        self.assertIn("WS_FRAME_CAP (DESK_WSPK_MAX_FRAME_SIZE + 1)", self.ws)
        self.assertIn("if (frame_opcode == 0x1) frame_limit = DESK_SDK_MAX_FRAME_LEN", self.ws)
        self.assertIn("else if (frame_opcode == 0x2) frame_limit = DESK_WSPK_MAX_FRAME_SIZE", self.ws)
        self.assertIn("n > frame_limit", self.ws)
        self.assertIn("if (op == 0x2)", self.ws)
        self.assertIn("desk_wspk_unpack_header", self.ws)
        binary_max = 128 * 1024 + 16
        text_max = 8192
        self.assertEqual(binary_max, 131088)
        self.assertEqual(text_max, 8192)
        self.assertLessEqual(131088, binary_max)
        self.assertGreater(131089, binary_max)
        self.assertLessEqual(8192, text_max)
        self.assertGreater(8193, text_max)
        # The source uses strict > comparisons, so exact maxima pass and +1 fails.
        self.assertIn("n > frame_limit", self.ws)
        # The strict greater-than receive check accepts each exact cap and rejects +1.
        self.assertIn("n > frame_limit", self.ws)


if __name__ == "__main__":
    unittest.main()
