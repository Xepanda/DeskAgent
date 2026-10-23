"""Source contract for the official COBS-delimited STM32 MCU-link framing."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
MOTION_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_motion.c"


class TestStm32UartNativeFrameContract(unittest.TestCase):
    def test_move_and_stop_allocate_sequences_atomically(self) -> None:
        source = MOTION_C.read_text(encoding="utf-8")
        self.assertIn("static uint32_t motion_next_sequence(void)", source)
        allocator = source.split("static uint32_t motion_next_sequence(void)", 1)[1].split(
            "static uint16_t motion_calc_crc16", 1
        )[0]
        self.assertIn("atomic_fetch_add_explicit(&s_mcu_sequence", allocator)
        self.assertNotRegex(source, r"\+\+s_mcu_sequence")
        self.assertEqual(source.count("motion_next_sequence()"), 4)  # MOVE, STOP, recovery STOP, HELLO

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
        self.assertGreaterEqual(move.count("motion_next_sequence()"), 2)
        self.assertRegex(
            move,
            r"motion_send_and_wait_ack\(\s*stop_buf, stop_len, stop_seq, MCU_MOTION_MSG_SERVO_STOP, false,",
        )
        self.assertIn("s_last_ambiguous_move_stop_result", move)

    def test_uart_transaction_decodes_cobs_delimited_frames(self) -> None:
        source = MOTION_C.read_text(encoding="utf-8")
        transaction = source.split("static int motion_send_and_wait_ack", 1)[1].split(
            "int desk_watcher_motion_move", 1
        )[0]

        self.assertIn("uart_write_bytes(UART_NUM_2, (const char *)frame_data, frame_len)", transaction)
        self.assertIn("desk_watcher_motion_parse_ack", transaction)
        self.assertIn("desk_watcher_motion_cobs_decode", transaction)
        self.assertIn("discarding_frame", transaction)
        self.assertIn("byte != 0u", transaction)

    def test_raw_ack_parser_checks_crc_sequence_and_ack_type(self) -> None:
        source = MOTION_C.read_text(encoding="utf-8")
        parser = source.split("int desk_watcher_motion_parse_ack", 1)[1].split(
            "static int motion_send_and_wait_ack", 1
        )[0]
        self.assertIn("motion_calc_crc16(frame, 12 + payload_len)", parser)
        self.assertIn("ref_seq != expected_seq", parser)
        self.assertIn("MCU_FRAME_CLASS_SYS", parser)
        self.assertIn("MCU_SYS_MSG_ACK", parser)

    def test_ack_parser_correlates_command_and_reports_nack_independently_of_code(self) -> None:
        source = MOTION_C.read_text(encoding="utf-8")
        parser = source.split("int desk_watcher_motion_parse_ack", 1)[1].split(
            "size_t desk_watcher_motion_pack_move", 1
        )[0]
        transaction = source.split("static int motion_send_and_wait_ack", 1)[1].split(
            "int desk_watcher_motion_move", 1
        )[0]

        self.assertIn("expected_msg_id", parser)
        self.assertIn("msg_id == MCU_SYS_MSG_ACK", parser)
        self.assertIn("msg_id == MCU_SYS_MSG_NACK", parser)
        self.assertIn("payload_len < 6u", parser)
        self.assertIn("payload_len < 8u", parser)
        self.assertIn("*out_is_nack = true", parser)
        self.assertIn("expected_seq, expected_msg_id", transaction)
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
