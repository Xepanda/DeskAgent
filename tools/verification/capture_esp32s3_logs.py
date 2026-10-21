#!/usr/bin/env python3
"""Read-only ESP32-S3 UART capture and offline log parser.

Importing this module never opens a serial port. Hardware access is available
only through the explicit ``--capture`` CLI mode.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
import re
import sys
import time
from pathlib import Path
from typing import Callable, Iterable, Optional, TextIO


DEFAULT_PORT = "COM21"
DEFAULT_BAUD = 115200
DEFAULT_OUTPUT_DIR = Path(".target_logging/serial-capture")
READ_SIZE = 4096

_CLASSIFIERS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("wakenet", re.compile(r"(?<![A-Za-z0-9])(?:wakenet|wake\s*word|wakeword|keyword\s+detected)(?![A-Za-z0-9])|唤醒", re.I)),
    ("asr", re.compile(r"(?<![A-Za-z0-9])ASR(?![A-Za-z0-9])|speech\s+recogn(?:ition|ized)|识别结果", re.I)),
    ("tts", re.compile(r"\bTTS\b|text[- ]to[- ]speech|playback[_ ]+(?:start|started|done)|speaker[_ ]+play", re.I)),
    ("camera", re.compile(r"(?<![A-Za-z0-9])(?:camera|PTL|SPI|CRC|capture)(?![A-Za-z0-9])|相机", re.I)),
    ("stm32_tx", re.compile(r"(?<![A-Za-z0-9])STM32(?![A-Za-z0-9]).*?(?<![A-Za-z0-9])(?:TX|SEND|WRITE)(?![A-Za-z0-9])|(?<![A-Za-z0-9])(?:TX|SEND)(?![A-Za-z0-9]).*?STM32", re.I)),
    ("stm32_ack", re.compile(r"(?<![A-Za-z0-9])STM32(?![A-Za-z0-9]).*?(?<![A-Za-z0-9])ACK(?![A-Za-z0-9])|(?<![A-Za-z0-9])ACK(?![A-Za-z0-9]).*?STM32", re.I)),
    ("mqtt", re.compile(r"(?<![A-Za-z0-9])MQTT(?![A-Za-z0-9])|provision(?:ing)?", re.I)),
    ("memory", re.compile(r"(?<![A-Za-z0-9])(?:heap|PSRAM|free[_ ]?memory)(?![A-Za-z0-9])", re.I)),
)
_SENSITIVE_KEY_VALUE = re.compile(
    r"(?i)\b(?:pair(?:ing)?[_ -]?code|pairing[_ -]?token|access[_ -]?token|token|"
    r"authorization|secret|password|passwd|api[_ -]?key)\b\s*[:=]\s*"
    r"(?:Bearer\s+)?(?:\"[^\"]*\"|'[^']*'|[^\s,;&]+)"
)
_BEARER_VALUE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*")
_QUERY_SECRET = re.compile(
    r"(?i)([?&](?:pairing[_-]?code|access[_-]?token|token|code)=)[^&\s]+"
)
_FIELD = re.compile(r"(?i)\b([a-z][a-z0-9_.-]*)\s*[:=]\s*(-?(?:0x)?[a-z0-9]+)")
_ERROR = re.compile(r"(?i)\b(?:rc|ret|errno|error(?:_?code)?)\s*[:=]\s*(-?(?:0x)?[a-z0-9]+)")
_STAGE = re.compile(r"(?i)\b(?:stage|phase)\s*[:=]\s*([a-z0-9_.-]+)")
_SEQUENCE = re.compile(r"(?i)\b(?:seq|sequence|request_id)\s*[:=]\s*([a-z0-9_.-]+)")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def redact_sensitive(text: str) -> str:
    """Redact common credential/code fields before writing structured output."""
    text = _QUERY_SECRET.sub(r"\1[REDACTED]", text)
    text = _SENSITIVE_KEY_VALUE.sub("[REDACTED]", text)
    return _BEARER_VALUE.sub("Bearer [REDACTED]", text)


def _numeric_value(raw: str) -> Optional[int]:
    try:
        return int(raw, 0 if raw.lower().startswith("0x") else 10)
    except ValueError:
        return None


class IncrementalLogParser:
    """Split arbitrary serial byte chunks into timestamped diagnostic lines."""

    def __init__(self, clock: Callable[[], str] = utc_now) -> None:
        self._clock = clock
        self._buffer = bytearray()
        self._total_bytes = 0
        self._next_line_offset = 0
        self.line_count = 0
        self._finished = False

    def feed(self, chunk: bytes) -> list[dict[str, object]]:
        if self._finished:
            raise RuntimeError("parser is already finished")
        if not chunk:
            return []
        self._buffer.extend(chunk)
        self._total_bytes += len(chunk)
        events: list[dict[str, object]] = []
        while True:
            newline_at = self._buffer.find(b"\n")
            if newline_at < 0:
                break
            raw_line = bytes(self._buffer[: newline_at + 1])
            del self._buffer[: newline_at + 1]
            start = self._next_line_offset
            self._next_line_offset += len(raw_line)
            events.extend(self._parse_line(raw_line, start, self._next_line_offset))
        return events

    def finish(self) -> list[dict[str, object]]:
        if self._finished:
            return []
        self._finished = True
        if not self._buffer:
            return []
        raw_line = bytes(self._buffer)
        self._buffer.clear()
        start = self._next_line_offset
        self._next_line_offset += len(raw_line)
        return self._parse_line(raw_line, start, self._next_line_offset)

    def _parse_line(self, raw_line: bytes, start: int, end: int) -> list[dict[str, object]]:
        self.line_count += 1
        line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
        categories = [name for name, pattern in _CLASSIFIERS if pattern.search(line)]
        if not categories:
            return []

        fields = {key.lower(): value for key, value in _FIELD.findall(line)}
        stage_match = _STAGE.search(line)
        error_match = _ERROR.search(line)
        sequence_match = _SEQUENCE.search(line)
        metrics: dict[str, int] = {}
        for key, value in fields.items():
            if any(term in key for term in ("heap", "psram", "free_mem")):
                number = _numeric_value(value)
                if number is not None:
                    metrics[key] = number

        common: dict[str, object] = {
            "timestamp_utc": self._clock(),
            "line_number": self.line_count,
            "raw_offset_start": start,
            "raw_offset_end": end,
            "text": redact_sensitive(line)[:2000],
        }
        events: list[dict[str, object]] = []
        for category in categories:
            event = dict(common)
            event["category"] = category
            if category == "camera":
                if stage_match:
                    event["stage"] = stage_match.group(1)
                elif re.search(r"\bPTL\b|header", line, re.I):
                    event["stage"] = "ptl_header"
                elif re.search(r"\bSPI\b", line, re.I):
                    event["stage"] = "spi"
                elif re.search(r"\bCRC\b", line, re.I):
                    event["stage"] = "crc"
                elif re.search(r"capture", line, re.I):
                    event["stage"] = "capture"
                if error_match:
                    event["raw_error"] = error_match.group(1)
            if category.startswith("stm32_") and sequence_match:
                event["sequence"] = sequence_match.group(1)
            if category == "mqtt":
                state_match = re.search(r"(?i)\b(connected|disconnected|connecting|online|offline)\b", line)
                if state_match:
                    event["state"] = state_match.group(1).lower()
            if category == "memory" and metrics:
                event["metrics"] = metrics
            events.append(event)
        # One line can emit several categories, but a fresh timestamp per event
        # would imply separate arrival times. All events share the same stamp.
        return events


class RawByteSink:
    """Persist bytes unchanged; flush and fsync every received chunk."""

    def __init__(self, path: Path, fsync: bool = True) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._file = path.open("xb")
        self._fsync = fsync
        self.byte_count = 0

    def write(self, chunk: bytes) -> None:
        self._file.write(chunk)
        self.byte_count += len(chunk)
        self._file.flush()
        if self._fsync:
            import os

            os.fsync(self._file.fileno())

    def close(self) -> None:
        if not self._file.closed:
            self._file.flush()
            self._file.close()


class SerialLogCapture:
    """Write raw bytes and redacted JSONL diagnostics plus a count-only summary."""

    def __init__(
        self,
        raw_path: Optional[Path],
        events_path: Path,
        summary_path: Path,
        *,
        clock: Callable[[], str] = utc_now,
        fsync: bool = True,
    ) -> None:
        events_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        self._raw = RawByteSink(raw_path, fsync=fsync) if raw_path else None
        self._events_file: TextIO = events_path.open("x", encoding="utf-8", newline="\n")
        self._summary_path = summary_path
        self._parser = IncrementalLogParser(clock=clock)
        self._clock = clock
        self._event_counts: Counter[str] = Counter()
        self._start_time: Optional[str] = None
        self._last_time: Optional[str] = None
        self._closed = False

    @property
    def raw_bytes(self) -> int:
        return self._raw.byte_count if self._raw else self._parser._total_bytes

    def consume(self, chunk: bytes) -> None:
        if self._closed:
            raise RuntimeError("capture is already closed")
        if self._raw:
            self._raw.write(chunk)
        events = self._parser.feed(chunk)
        self._write_events(events)

    def transport_event(self, state: str, detail: str = "") -> None:
        self._write_events(
            [
                {
                    "timestamp_utc": self._clock(),
                    "category": "transport",
                    "state": state,
                    "detail": redact_sensitive(detail)[:500],
                }
            ]
        )

    def close(self) -> dict[str, object]:
        if self._closed:
            return self._summary()
        self._write_events(self._parser.finish())
        self._events_file.flush()
        self._events_file.close()
        if self._raw:
            self._raw.close()
        summary = self._summary()
        temp_path = self._summary_path.with_suffix(self._summary_path.suffix + ".tmp")
        temp_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp_path.replace(self._summary_path)
        self._closed = True
        return summary

    def _write_events(self, events: Iterable[dict[str, object]]) -> None:
        for event in events:
            category = str(event.get("category", "unknown"))
            timestamp = str(event.get("timestamp_utc", self._clock()))
            self._start_time = self._start_time or timestamp
            self._last_time = timestamp
            self._event_counts[category] += 1
            self._events_file.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
        if events:
            self._events_file.flush()

    def _summary(self) -> dict[str, object]:
        return {
            "schema_version": 1,
            "started_at_utc": self._start_time,
            "ended_at_utc": self._last_time,
            "raw_bytes": self.raw_bytes,
            "parsed_lines": self._parser.line_count,
            "event_counts": dict(sorted(self._event_counts.items())),
        }


def _new_output_paths(output_dir: Path, port: str) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    safe_port = re.sub(r"[^A-Za-z0-9_.-]", "_", port)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_%fZ")
    base = output_dir / f"esp32s3_{safe_port}_{timestamp}"
    raw, events, summary = base.with_suffix(".raw.bin"), base.with_suffix(".events.jsonl"), base.with_suffix(".summary.json")
    suffix = 1
    while raw.exists() or events.exists() or summary.exists():
        base = output_dir / f"esp32s3_{safe_port}_{timestamp}_{suffix}"
        raw, events, summary = base.with_suffix(".raw.bin"), base.with_suffix(".events.jsonl"), base.with_suffix(".summary.json")
        suffix += 1
    return raw, events, summary


def _open_serial(port: str, baud: int, timeout: float):
    try:
        import serial  # type: ignore[import-not-found]
    except ImportError as exc:
        raise RuntimeError("pyserial is required for --capture; install it in the project environment") from exc
    # Set modem-control lines before opening so the capture does not assert
    # DTR/RTS and unexpectedly reset or toggle the board during a read-only run.
    handle = serial.Serial(port=None, baudrate=baud, timeout=timeout)
    handle.dtr = False
    handle.rts = False
    handle.port = port
    handle.open()
    return handle


def capture_port(
    port: str,
    baud: int,
    output_dir: Path,
    *,
    reconnect: bool = False,
    reconnect_delay: float = 2.0,
    max_reconnects: int = 5,
    read_timeout: float = 0.5,
    serial_factory: Callable[..., object] = _open_serial,
) -> int:
    raw_path, events_path, summary_path = _new_output_paths(output_dir, port)
    capture = SerialLogCapture(raw_path, events_path, summary_path)
    serial_handle = None
    reconnects = 0
    ever_connected = False
    exit_code = 0
    try:
        while True:
            try:
                serial_handle = serial_factory(port=port, baud=baud, timeout=read_timeout)
                capture.transport_event("reconnected" if ever_connected else "connected", f"port={port} baud={baud}")
                ever_connected = True
                while True:
                    chunk = serial_handle.read(READ_SIZE)  # type: ignore[attr-defined]
                    if chunk:
                        capture.consume(bytes(chunk))
            except KeyboardInterrupt:
                break
            except Exception as exc:  # serial backend exceptions vary by OS/driver
                capture.transport_event("disconnected" if ever_connected else "connect_failed", str(exc))
                if serial_handle is not None:
                    try:
                        serial_handle.close()  # type: ignore[attr-defined]
                    except Exception:
                        pass
                    serial_handle = None
                if not reconnect or reconnects >= max_reconnects:
                    exit_code = 2
                    break
                reconnects += 1
                time.sleep(max(0.0, reconnect_delay))
    finally:
        if serial_handle is not None:
            try:
                serial_handle.close()  # type: ignore[attr-defined]
            except Exception:
                pass
        summary = capture.close()
    print(f"Raw capture:   {raw_path}")
    print(f"Event summary: {events_path}")
    print(f"Counts:        {summary_path}")
    if exit_code:
        print("Serial capture ended after disconnect; see transport event for details.", file=sys.stderr)
    return exit_code


def parse_file(input_path: Path, output_dir: Path) -> tuple[Path, Path]:
    _, events_path, summary_path = _new_output_paths(output_dir, "offline")
    capture = SerialLogCapture(None, events_path, summary_path)
    with input_path.open("rb") as source:
        while chunk := source.read(READ_SIZE):
            capture.consume(chunk)
    capture.close()
    return events_path, summary_path


def build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--capture", action="store_true", help="explicitly open a serial port and capture read-only")
    mode.add_argument("--parse-file", type=Path, help="parse a saved raw byte log offline; does not access serial")
    parser.add_argument("--port", default=DEFAULT_PORT, help=f"serial port (default: {DEFAULT_PORT})")
    parser.add_argument("--baud", type=int, default=DEFAULT_BAUD, help=f"serial baud rate (default: {DEFAULT_BAUD})")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--reconnect", action="store_true", help="retry after disconnect")
    parser.add_argument("--reconnect-delay", type=float, default=2.0)
    parser.add_argument("--max-reconnects", type=int, default=5, help="maximum retry attempts when --reconnect is set")
    parser.add_argument("--read-timeout", type=float, default=0.5)
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    parser = build_argument_parser()
    args = parser.parse_args(argv)
    if args.baud <= 0 or args.read_timeout <= 0 or args.reconnect_delay < 0 or args.max_reconnects < 0:
        parser.error("baud/read-timeout must be positive; reconnect delay and max reconnects cannot be negative")
    if args.capture:
        print("Read-only UART capture. Raw bytes are preserved unchanged and may contain credentials; keep all output local.")
        return capture_port(
            args.port,
            args.baud,
            args.output_dir,
            reconnect=args.reconnect,
            reconnect_delay=args.reconnect_delay,
            max_reconnects=args.max_reconnects,
            read_timeout=args.read_timeout,
        )
    if not args.parse_file.is_file():
        parser.error(f"raw input file does not exist: {args.parse_file}")
    events_path, summary_path = parse_file(args.parse_file, args.output_dir)
    print(f"Event summary: {events_path}")
    print(f"Counts:        {summary_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
