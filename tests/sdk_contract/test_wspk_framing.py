"""
Tests for WatcheRobot WSPK binary framing protocol.
Validates 16-byte header packing (<4sBBHII, little-endian per official Watcher SDK),
14-byte legacy read compatibility (<4sBBII), official test vectors from wspk_v1.json,
boundary conditions, and buffer overflow protections.
"""

from __future__ import annotations

import struct
import unittest

WSPK_MAGIC = b"WSPK"
WSPK_HEADER_SIZE = 16
WSPK_LEGACY_HEADER_SIZE = 14
WSPK_TYPE_AUDIO = 1
WSPK_TYPE_IMAGE = 2
WSPK_TYPE_DATA = 3
WSPK_MAX_PAYLOAD_SIZE = 128 * 1024  # 128 KB bound for embedded stability


def pack_wspk_header(
    frame_type: int,
    flags: int,
    stream_id: int,
    sequence: int,
    payload_size: int,
) -> bytes:
    """Packs a 16-byte WSPK header in little-endian order:
    b"WSPK" + frame_type(1B) + flags(1B) + stream_id(2B LE) + sequence(4B LE) + payload_size(4B LE)
    """
    if payload_size > WSPK_MAX_PAYLOAD_SIZE:
        raise ValueError(f"Payload size {payload_size} exceeds max {WSPK_MAX_PAYLOAD_SIZE}")
    return struct.pack("<4sBBHII", WSPK_MAGIC, frame_type, flags, stream_id, sequence, payload_size)


def unpack_wspk_header(buf: bytes) -> dict[str, int]:
    """Unpacks a 16-byte (or 14-byte legacy) WSPK header and validates structure."""
    if len(buf) < WSPK_LEGACY_HEADER_SIZE:
        raise ValueError(f"Buffer length {len(buf)} is shorter than minimum header size {WSPK_LEGACY_HEADER_SIZE}")
    if buf[:4] != WSPK_MAGIC:
        raise ValueError(f"Invalid WSPK magic: {buf[:4]!r}")

    frame_type = buf[4]
    flags = buf[5]

    if len(buf) >= WSPK_HEADER_SIZE:
        magic, frame_type, flags, stream_id, sequence, payload_size = struct.unpack("<4sBBHII", buf[:16])
    else:
        # Legacy 14-byte header: <4sBBII with stream_id = 0
        magic, frame_type, flags, sequence, payload_size = struct.unpack("<4sBBII", buf[:14])
        stream_id = 0

    if payload_size > WSPK_MAX_PAYLOAD_SIZE:
        raise ValueError(f"Payload size {payload_size} exceeds max limit {WSPK_MAX_PAYLOAD_SIZE}")

    return {
        "frame_type": frame_type,
        "flags": flags,
        "stream_id": stream_id,
        "sequence": sequence,
        "payload_size": payload_size,
    }


class TestWspkFraming(unittest.TestCase):
    def test_official_wspk_v1_audio_vector(self) -> None:
        """Verifies against the exact official test vector from wspk_v1.json (current_audio_first)."""
        expected_hex = "5753504b01012a00070000000300000070636d"
        raw_packet = bytes.fromhex(expected_hex)

        parsed = unpack_wspk_header(raw_packet[:16])
        self.assertEqual(parsed["frame_type"], 1)
        self.assertEqual(parsed["flags"], 1)
        self.assertEqual(parsed["stream_id"], 42)
        self.assertEqual(parsed["sequence"], 7)
        self.assertEqual(parsed["payload_size"], 3)
        self.assertEqual(raw_packet[16:], b"pcm")

        packed = pack_wspk_header(
            frame_type=1,
            flags=1,
            stream_id=42,
            sequence=7,
            payload_size=3,
        ) + b"pcm"
        self.assertEqual(packed.hex(), expected_hex)

    def test_official_wspk_v1_legacy_audio_vector(self) -> None:
        """Verifies against the exact official test vector from wspk_v1.json (legacy_audio_last)."""
        expected_hex = "5753504b010209000000030000006f6c64"
        raw_packet = bytes.fromhex(expected_hex)

        parsed = unpack_wspk_header(raw_packet[:14])
        self.assertEqual(parsed["frame_type"], 1)
        self.assertEqual(parsed["flags"], 2)
        self.assertEqual(parsed["stream_id"], 0)
        self.assertEqual(parsed["sequence"], 9)
        self.assertEqual(parsed["payload_size"], 3)
        self.assertEqual(raw_packet[14:], b"old")

    def test_pack_and_unpack_audio_frame(self) -> None:
        hdr_bytes = pack_wspk_header(
            frame_type=WSPK_TYPE_AUDIO,
            flags=0x00,
            stream_id=101,
            sequence=1,
            payload_size=960,
        )
        self.assertEqual(len(hdr_bytes), WSPK_HEADER_SIZE)
        self.assertTrue(hdr_bytes.startswith(b"WSPK"))

        parsed = unpack_wspk_header(hdr_bytes)
        self.assertEqual(parsed["frame_type"], WSPK_TYPE_AUDIO)
        self.assertEqual(parsed["flags"], 0)
        self.assertEqual(parsed["stream_id"], 101)
        self.assertEqual(parsed["sequence"], 1)
        self.assertEqual(parsed["payload_size"], 960)

    def test_pack_and_unpack_image_frame(self) -> None:
        jpeg_payload_len = 16384
        hdr_bytes = pack_wspk_header(
            frame_type=WSPK_TYPE_IMAGE,
            flags=0x02,  # Keyframe flag
            stream_id=202,
            sequence=42,
            payload_size=jpeg_payload_len,
        )
        parsed = unpack_wspk_header(hdr_bytes)
        self.assertEqual(parsed["frame_type"], WSPK_TYPE_IMAGE)
        self.assertEqual(parsed["flags"], 0x02)
        self.assertEqual(parsed["stream_id"], 202)
        self.assertEqual(parsed["sequence"], 42)
        self.assertEqual(parsed["payload_size"], jpeg_payload_len)

    def test_reject_invalid_magic(self) -> None:
        corrupt_hdr = b"FAKE" + struct.pack("<BBHII", 1, 0, 1, 1, 100)
        with self.assertRaises(ValueError) as ctx:
            unpack_wspk_header(corrupt_hdr)
        self.assertIn("Invalid WSPK magic", str(ctx.exception))

    def test_reject_short_buffer(self) -> None:
        short_buf = b"WSPK\x01\x00"
        with self.assertRaises(ValueError) as ctx:
            unpack_wspk_header(short_buf)
        self.assertIn("shorter than minimum header size", str(ctx.exception))

    def test_reject_oversized_payload(self) -> None:
        oversized = WSPK_MAX_PAYLOAD_SIZE + 1024
        with self.assertRaises(ValueError):
            pack_wspk_header(
                frame_type=WSPK_TYPE_AUDIO,
                flags=0,
                stream_id=1,
                sequence=1,
                payload_size=oversized,
            )

        corrupt_large = b"WSPK" + struct.pack("<BBHII", 1, 0, 1, 1, oversized)
        with self.assertRaises(ValueError) as ctx:
            unpack_wspk_header(corrupt_large)
        self.assertIn("exceeds max limit", str(ctx.exception))
