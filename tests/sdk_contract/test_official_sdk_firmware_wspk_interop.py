"""Interop the current firmware WSPK image contract with the pinned SDK parser.

The target C component cannot be built in the Windows contract-test environment,
so ``firmware_sender_packet`` mirrors the production packer's byte layout. The
test also guards the production call chain/constants before feeding those bytes
to the unmodified, pinned SDK parser and image handler.
"""

from __future__ import annotations

import io
import queue
import re
import struct
import sys
import threading
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / ".target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk"
sys.path.insert(0, str(SDK_ROOT / "src"))

from watcherobot.protocol import FRAME_IMAGE, parse_wspk  # noqa: E402
from watcherobot.robot import WatcheRobot  # noqa: E402


def firmware_sender_packet(
    payload: bytes, *, stream_id: int, sequence: int, flags: int = 0
) -> bytes:
    """Mirror desk_sdk_bridge_pack_media_frame + desk_wspk_pack_header."""
    if len(payload) > 128 * 1024:
        raise ValueError("firmware WSPK payload limit exceeded")
    return struct.pack("<4sBBHII", b"WSPK", FRAME_IMAGE, flags, stream_id,
                       sequence, len(payload)) + payload


def make_jpeg() -> bytes:
    image = Image.new("RGB", (3, 2), (27, 91, 163))
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


class OfficialSdkFirmwareWspkInterop(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.sdk_sha = __import__("subprocess").check_output(
            ["git", "-C", str(SDK_ROOT), "rev-parse", "HEAD"], text=True
        ).strip()
        cls.source = (ROOT / "firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c").read_text()
        cls.header = (ROOT / "firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h").read_text()

    def test_current_sender_image_is_accepted_by_official_protocol_and_image_parser(self) -> None:
        packer = self.source.split("int desk_sdk_bridge_pack_media_frame(", 1)[1].split(
            "int desk_sdk_bridge_send_media_frame(", 1
        )[0]
        sender = self.source.split("int desk_sdk_bridge_send_media_frame(", 1)[1].split(
            "static uint16_t s_camera_session_id", 1
        )[0]
        self.assertIn("desk_wspk_pack_header", packer)
        self.assertIn("memcpy(&out_buf[DESK_WSPK_HEADER_SIZE], payload, payload_len)", packer)
        self.assertIn("desk_sdk_ws_client_send_binary(target_buf, packed_len)", sender)
        self.assertRegex(sender, r"DESK_WSPK_MAX_PAYLOAD_SIZE")
        camera = self.source.split("/* Camera capture:", 1)[1].split(
            "/* Microphone open", 1
        )[0]
        self.assertIn('"session_id\\":%u', camera)
        self.assertIn("(unsigned int)s_camera_session_id", camera)
        self.assertIn(
            "desk_sdk_bridge_send_media_frame(DESK_WSPK_TYPE_IMAGE, 0, s_camera_session_id, s_camera_seq,",
            camera,
        )

        jpeg = make_jpeg()
        with Image.open(io.BytesIO(jpeg)) as check:
            check.verify()
        session_id, sequence = 0x1234, 37
        raw = firmware_sender_packet(jpeg, stream_id=session_id, sequence=sequence)
        parsed = parse_wspk(raw)  # Actual parser from pinned official SDK.
        self.assertEqual((parsed.frame_type, parsed.flags, parsed.stream_id, parsed.sequence),
                         (FRAME_IMAGE, 0, session_id, sequence))
        self.assertEqual(parsed.payload, jpeg)

        # Exercise the same pinned SDK image callback used after parse_wspk().
        robot = object.__new__(WatcheRobot)
        robot._image_queue = queue.Queue(maxsize=1)
        robot._image_assembly_lock = threading.Lock()
        robot._image_assemblies = {}
        robot._on_image_frame(parsed)
        image_frame = robot._image_queue.get_nowait()
        self.assertEqual(image_frame.session_id, session_id)
        self.assertEqual(image_frame.sequence, sequence)
        self.assertEqual(image_frame.data, jpeg)
        with Image.open(io.BytesIO(image_frame.data)) as check:
            check.load()

    def test_firmware_capture_cap_and_wspk_sink_cap_are_distinct(self) -> None:
        capture_branch = self.source.split("/* Camera capture:", 1)[1].split(
            "/* Microphone open", 1
        )[0]
        capture_match = re.search(r"size_t\s+max_jpeg\s*=\s*(\d+)\s*;", capture_branch)
        self.assertIsNotNone(capture_match)
        capture_cap = int(capture_match.group(1))
        self.assertEqual(capture_cap, 65536)

        payload_match = re.search(
            r"#define\s+DESK_WSPK_MAX_PAYLOAD_SIZE\s+\(128\s*\*\s*1024\)", self.header
        )
        self.assertIsNotNone(payload_match)
        wspk_payload_cap = 128 * 1024
        self.assertEqual(wspk_payload_cap, 131072)
        self.assertEqual(16 + wspk_payload_cap, 131088)
        self.assertEqual(16 + capture_cap, 65552)

        for size in (capture_cap, wspk_payload_cap):
            parsed = parse_wspk(firmware_sender_packet(
                b"J" * size, stream_id=7, sequence=size
            ))
            self.assertEqual(len(parsed.payload), size)
        with self.assertRaises(ValueError):
            firmware_sender_packet(b"J" * (wspk_payload_cap + 1), stream_id=7, sequence=1)

    def test_sdk_pin_is_reported_for_reproducibility(self) -> None:
        self.assertEqual(len(self.sdk_sha), 40)
        self.assertEqual(self.sdk_sha, "8bc13967a06ef073459be47e930ff43ef37ee8fb")


if __name__ == "__main__":
    unittest.main()
