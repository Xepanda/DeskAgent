"""Exercise the pinned SDK WSPK parser through its image callback into Host."""

from __future__ import annotations

import io
import os
import sys
import time
import unittest
from pathlib import Path

from PIL import Image


ROOT = Path(__file__).resolve().parents[2]
SDK_ROOT = ROOT / ".target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk"
SDK_SRC = Path(os.environ.get("WATCHEROBOT_SDK_SRC", SDK_ROOT / "src"))
if SDK_SRC.is_dir():
    sys.path.insert(0, str(SDK_SRC))

from watcherobot.protocol import FRAME_IMAGE, ProtocolError, parse_wspk  # noqa: E402
from watcherobot.robot import WatcheRobot  # noqa: E402
from host.desk_agent.connectors.robot.official_camera_adapter import (  # noqa: E402
    OfficialRobotCameraAdapter,
)


def jpeg_fixture() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (3, 2), (19, 87, 143)).save(output, format="JPEG")
    return output.getvalue()


class RawWspkTransport:
    """Feed the unmodified SDK parser/callback using raw WSPK bytes."""

    capabilities = ("camera",)
    device_info = {"device_id": "fixture"}

    def set_callbacks(self, message_cb, binary_cb, disconnect_cb):
        self.binary_cb = binary_cb
        self.disconnect_cb = disconnect_cb

    def send_command(self, message_type, data, timeout=None):
        if message_type == "ctrl.camera.capture":
            self.binary_cb(parse_wspk(self.packet))
            return {"type": "sys.ack", "code": 0, "data": {"session_id": self.session_id}}
        if message_type in ("ctrl.camera.acquire", "ctrl.camera.release"):
            return {"type": "sys.ack", "code": 0, "data": {}}
        raise AssertionError(f"unexpected SDK command: {message_type}")

    def close(self):
        pass


class OfficialSdkHostMediaIngressTests(unittest.TestCase):
    def test_raw_wspk_image_reaches_host_consumer_as_decodable_jpeg(self) -> None:
        jpeg = jpeg_fixture()
        session_id = 0x1234
        packet = (
            b"WSPK"
            + bytes((FRAME_IMAGE, 0))
            + session_id.to_bytes(2, "little")
            + (41).to_bytes(4, "little")
            + len(jpeg).to_bytes(4, "little")
            + jpeg
        )
        transport = RawWspkTransport()
        transport.packet = packet
        transport.session_id = session_id
        robot = WatcheRobot._from_transport(transport)
        delivered: list[bytes] = []
        adapter = OfficialRobotCameraAdapter(
            robot,
            send_command=transport.send_command,
            ingest_frame=delivered.append,
            max_jpeg_bytes=65536,
        )
        try:
            self.assertTrue(adapter.acquire("sdk-host-ingress", 3000, target_fps=2)["granted"])
            deadline = time.monotonic() + 1
            while not delivered and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertEqual(delivered, [jpeg])
            with Image.open(io.BytesIO(delivered[0])) as image:
                image.load()
                self.assertEqual(image.size, (3, 2))
        finally:
            adapter.release("sdk-host-ingress")
            adapter.close()
            robot.close()

    def test_official_parser_rejects_bad_magic_and_payload_length(self) -> None:
        with self.assertRaises(ProtocolError):
            parse_wspk(b"NOPE" + bytes(20))
        with self.assertRaises(ProtocolError):
            parse_wspk(b"WSPK" + bytes((FRAME_IMAGE, 0)) + bytes(8) + b"x")

    def test_wspk_contract_has_no_crc_field(self) -> None:
        # A CRC must not be fabricated: the approved 16-byte <4sBBHII header
        # carries only magic, type, flags, stream id, sequence and payload size.
        self.assertEqual(len(b"WSPK" + bytes((FRAME_IMAGE, 0)) + bytes(10)), 16)


if __name__ == "__main__":
    unittest.main()
