"""Source contract for the ratified raw STM32 UART framing."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
MOTION_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_motion.c"


class TestStm32UartNativeFrameContract(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
