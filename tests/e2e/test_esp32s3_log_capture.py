from __future__ import annotations

import json
import contextlib
import io
import tempfile
import unittest
from pathlib import Path

from tools.verification.capture_esp32s3_logs import (
    IncrementalLogParser,
    SerialLogCapture,
    build_argument_parser,
    capture_port,
    parse_file,
)


class TestEsp32S3LogCapture(unittest.TestCase):
    def test_incremental_lines_extract_runtime_signals_and_offsets(self) -> None:
        parser = IncrementalLogParser(clock=lambda: "2026-10-22T00:00:00Z")

        self.assertEqual(parser.feed(b"I audio WakeNet detected"), [])
        events = parser.feed(
            b" model=wn9_nihaoxiaozhi_tts\r\n"
            b"I tclaw_asr_result: hello\n"
            b"I audio_tts playback_started\n"
            b"E camera_capture stage=read_header PTL rc=-2 raw_error=0x00 size=0\n"
            b"I desk_stm32_tx seq=7 len=8\n"
            b"I desk_stm32_ack seq=7 status=ok\n"
            b"I tuya_mqtt state=connected\n"
            b"I sys_heap free_heap=12345 psram_free=67890\n"
        )

        self.assertEqual(
            [event["category"] for event in events],
            ["wakenet", "asr", "tts", "camera", "stm32_tx", "stm32_ack", "mqtt", "memory"],
        )
        self.assertEqual(events[0]["timestamp_utc"], "2026-10-22T00:00:00Z")
        self.assertEqual(events[0]["line_number"], 1)
        self.assertEqual(events[0]["raw_offset_start"], 0)
        self.assertEqual(events[0]["raw_offset_end"], 53)
        self.assertEqual(events[3]["stage"], "read_header")
        self.assertEqual(events[3]["raw_error"], "-2")
        self.assertEqual(events[4]["sequence"], "7")
        self.assertEqual(events[5]["sequence"], "7")
        self.assertEqual(events[6]["state"], "connected")
        self.assertEqual(events[7]["metrics"], {"free_heap": 12345, "psram_free": 67890})

    def test_structured_outputs_redact_pairing_codes_and_tokens_but_raw_bytes_are_exact(self) -> None:
        secret_line = b"I MQTT pairing_code=654321 access_token=secret-token state=connected\r\n"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            capture = SerialLogCapture(
                raw_path=root / "device.raw.bin",
                events_path=root / "events.jsonl",
                summary_path=root / "summary.json",
                clock=lambda: "2026-10-22T00:00:00Z",
                fsync=False,
            )
            capture.consume(secret_line)
            capture.close()

            self.assertEqual((root / "device.raw.bin").read_bytes(), secret_line)
            event_text = (root / "events.jsonl").read_text(encoding="utf-8")
            summary_text = (root / "summary.json").read_text(encoding="utf-8")
            self.assertNotIn("654321", event_text + summary_text)
            self.assertNotIn("secret-token", event_text + summary_text)
            self.assertIn("[REDACTED]", event_text)
            summary = json.loads(summary_text)
            self.assertEqual(summary["event_counts"], {"mqtt": 1})
            self.assertEqual(summary["raw_bytes"], len(secret_line))

    def test_unterminated_final_line_is_flushed_with_timestamp(self) -> None:
        parser = IncrementalLogParser(clock=lambda: "2026-10-22T00:01:00Z")
        self.assertEqual(parser.feed(b"E camera capture rc=-20"), [])
        events = parser.finish()
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["category"], "camera")
        self.assertEqual(events[0]["raw_error"], "-20")
        self.assertEqual(events[0]["timestamp_utc"], "2026-10-22T00:01:00Z")

    def test_utf8_line_split_inside_multibyte_character_is_reassembled(self) -> None:
        parser = IncrementalLogParser(clock=lambda: "2026-10-22T00:02:00Z")
        line = "E 相机采集阶段 phase=read_header rc=-1\n".encode("utf-8")
        split_at = line.index("采".encode("utf-8")) + 1
        self.assertEqual(parser.feed(line[:split_at]), [])
        events = parser.feed(line[split_at:])
        self.assertEqual(events[0]["category"], "camera")
        self.assertIn("相机", events[0]["text"])
        self.assertEqual(events[0]["raw_error"], "-1")

    def test_offline_file_parser_never_rewrites_source_bytes(self) -> None:
        fixture = b"I MQTT pairing_code=654321 state=connected\nE camera capture rc=-20"
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "fixture.raw.bin"
            source.write_bytes(fixture)
            events_path, summary_path = parse_file(source, root / "parsed")
            events_text = events_path.read_text(encoding="utf-8")
            summary = json.loads(summary_path.read_text(encoding="utf-8"))

            self.assertEqual(source.read_bytes(), fixture)
            self.assertNotIn("654321", events_text)
            events = [json.loads(line) for line in events_text.splitlines()]
            self.assertEqual([event["category"] for event in events], ["mqtt", "camera"])
            self.assertEqual(events[-1]["raw_error"], "-20")
            self.assertEqual(summary["parsed_lines"], 2)

    def test_serial_capture_requires_explicit_capture_flag(self) -> None:
        parser = build_argument_parser()
        with contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                parser.parse_args([])
        args = parser.parse_args(["--capture"])
        self.assertEqual(args.port, "COM21")
        self.assertEqual(args.baud, 115200)

    def test_reconnect_appends_received_bytes_and_records_transport_transitions(self) -> None:
        class FakeSerial:
            def __init__(self, chunks: list[bytes]) -> None:
                self.chunks = list(chunks)
                self.closed = False

            def read(self, _size: int) -> bytes:
                if self.chunks:
                    return self.chunks.pop(0)
                raise OSError("fixture disconnect")

            def close(self) -> None:
                self.closed = True

        serial_instances = [
            FakeSerial([b"I MQTT state=connected\n"]),
            FakeSerial([b"I ASR result: resumed\n"]),
        ]
        factory_calls: list[dict[str, object]] = []

        def serial_factory(**kwargs: object) -> FakeSerial:
            factory_calls.append(kwargs)
            return serial_instances.pop(0)

        with tempfile.TemporaryDirectory() as temp_dir, contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            exit_code = capture_port(
                "COMTEST",
                115200,
                Path(temp_dir),
                reconnect=True,
                reconnect_delay=0,
                max_reconnects=1,
                serial_factory=serial_factory,
            )
            raw_path = next(Path(temp_dir).glob("*.raw.bin"))
            event_path = next(Path(temp_dir).glob("*.events.jsonl"))
            raw_bytes = raw_path.read_bytes()
            events = [json.loads(line) for line in event_path.read_text(encoding="utf-8").splitlines()]

        self.assertEqual(exit_code, 2, "capture exits non-zero after retry budget is exhausted")
        self.assertEqual(len(factory_calls), 2, "one reconnect is attempted")
        self.assertEqual(raw_bytes, b"I MQTT state=connected\nI ASR result: resumed\n")
        self.assertEqual(
            [event.get("state") for event in events if event["category"] == "transport"],
            ["connected", "disconnected", "reconnected", "disconnected"],
        )


if __name__ == "__main__":
    unittest.main()
