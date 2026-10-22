"""Source contract for the ratified raw STM32 UART framing."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
MOTION_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_motion.c"


class TestStm32UartNativeFrameContract(unittest.TestCase):
    def test_ambiguous_move_failure_sends_one_new_sequence_stop_without_retry(self) -> None:
        source = MOTION_C.read_text(encoding="utf-8")
        move = source.split("int desk_watcher_motion_move", 1)[1].split(
            "int desk_watcher_motion_stop", 1
        )[0]
        transaction = source.split("static int motion_send_and_wait_ack", 1)[1].split(
            "int desk_watcher_motion_move", 1
        )[0]

        self.assertIn("may_have_sent", transaction)
        self.assertIn("if (result != DESK_HW_OK && may_have_sent)", move)
        self.assertIn("++s_mcu_sequence", move)
        self.assertRegex(
            move,
            r"motion_send_and_wait_ack\(\s*stop_buf, stop_len, stop_seq, false,",
        )
        self.assertIn("s_last_ambiguous_move_stop_result", move)

    def test_uart_transaction_uses_raw_binary_frames(self) -> None:
        source = MOTION_C.read_text(encoding="utf-8")
        transaction = source.split("static int motion_send_and_wait_ack", 1)[1].split(
            "int desk_watcher_motion_move", 1
        )[0]

        self.assertIn("uart_write_bytes(UART_NUM_2, (const char *)frame_data, frame_len)", transaction)
        self.assertIn("desk_watcher_motion_parse_ack", transaction)
        self.assertNotIn("wire_encode", transaction)
        self.assertNotIn("wire_decode", transaction)

    def test_raw_ack_parser_checks_crc_sequence_and_ack_type(self) -> None:
        source = MOTION_C.read_text(encoding="utf-8")
        parser = source.split("int desk_watcher_motion_parse_ack", 1)[1].split(
            "static int motion_send_and_wait_ack", 1
        )[0]
        self.assertIn("motion_calc_crc16(frame, 12 + payload_len)", parser)
        self.assertIn("rx_seq != expected_seq", parser)
        self.assertIn("MCU_FRAME_FLAG_ACK_RESP", parser)
        self.assertIn("MCU_MOTION_MSG_ACK", parser)

    def test_ack_parser_correlates_command_and_reports_nack_independently_of_code(self) -> None:
        source = MOTION_C.read_text(encoding="utf-8")
        parser = source.split("int desk_watcher_motion_parse_ack", 1)[1].split(
            "size_t desk_watcher_motion_pack_move", 1
        )[0]
        transaction = source.split("static int motion_send_and_wait_ack", 1)[1].split(
            "int desk_watcher_motion_move", 1
        )[0]

        self.assertIn("expected_msg_id", parser)
        self.assertIn("msg_id == expected_msg_id", parser)
        self.assertIn("flags == MCU_FRAME_FLAG_ACK_RESP", parser)
        self.assertIn("payload_len == 4", parser)
        self.assertIn("*out_is_nack = true", parser)
        self.assertIn("frame_data[4]", transaction)
        self.assertIn("&& is_nack", transaction)

    def test_uart_driver_deinit_respects_install_ownership(self) -> None:
        source = MOTION_C.read_text(encoding="utf-8")
        deinit = source.split("int desk_watcher_motion_deinit", 1)[1].split(
            "bool desk_watcher_motion_is_ready", 1
        )[0]
        self.assertIn("s_uart_installed_by_motion && uart_is_driver_installed(UART_NUM_2)", deinit)
        self.assertIn("s_uart_installed_by_motion = false", deinit)


if __name__ == "__main__":
    unittest.main()
