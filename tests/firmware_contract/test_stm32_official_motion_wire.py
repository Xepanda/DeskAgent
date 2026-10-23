"""Wire level regression against the pinned Watcher MCU-link motion format."""

from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import unittest


def crc16(data: bytes) -> int:
    crc = 0xFFFF
    for value in data:
        crc ^= value << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def cobs_decode(data: bytes) -> bytes:
    result = bytearray()
    index = 0
    while index < len(data):
        code = data[index]
        if code == 0 or index + code > len(data) + 1:
            raise ValueError("bad COBS")
        index += 1
        result.extend(data[index:index + code - 1])
        index += code - 1
        if code != 0xFF and index < len(data):
            result.append(0)
    return bytes(result)


def cobs_encode(data: bytes) -> bytes:
    encoded = bytearray([0])
    code_index, code = 0, 1
    for value in data:
        if value == 0:
            encoded[code_index] = code
            code_index = len(encoded)
            encoded.append(0)
            code = 1
        else:
            encoded.append(value)
            code += 1
            if code == 0xFF:
                encoded[code_index] = code
                code_index = len(encoded)
                encoded.append(0)
                code = 1
    encoded[code_index] = code
    return bytes(encoded)


def frame(msg_class: int, msg_id: int, seq: int, payload: bytes, flags: int = 2) -> bytes:
    raw = bytearray((0xA5, 0x5A, 1, msg_class, msg_id, flags))
    raw.extend(seq.to_bytes(4, "little"))
    raw.extend(len(payload).to_bytes(2, "little"))
    raw.extend(payload)
    raw.extend(crc16(raw).to_bytes(2, "little"))
    return bytes(raw)


class OfficialMotionWireTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        repo = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
        cls.lib_path = os.path.join(cls.tmp.name, "libdesk_motion_wire.so")
        cmd = [
            "gcc", "-shared", "-fPIC", "-O2", "-Wall", "-Wextra", "-Werror",
            "-Wno-error=unused-parameter",
            f"-I{repo}/firmware/components/desk_hardware/include",
            f"{repo}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo}/firmware/components/desk_hardware/src/desk_hardware.c",
            f"{repo}/firmware/components/desk_hardware/src/desk_watcher_motion.c",
            "-o", cls.lib_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.lib = ctypes.CDLL(cls.lib_path)
        cls.lib.desk_watcher_motion_pack_move.argtypes = [
            ctypes.c_float, ctypes.c_float, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
        ]
        cls.lib.desk_watcher_motion_pack_move.restype = ctypes.c_size_t
        cls.lib.desk_watcher_motion_pack_stop.argtypes = [
            ctypes.c_bool, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8),
        ]
        cls.lib.desk_watcher_motion_pack_stop.restype = ctypes.c_size_t
        cls.lib.desk_watcher_motion_parse_ack.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_uint32,
            ctypes.c_uint8, ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_bool),
        ]
        cls.lib.desk_watcher_motion_parse_ack.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def test_move_and_stop_use_official_cobs_wire_and_payload(self) -> None:
        seq = 0x12345678
        out = (ctypes.c_uint8 * 32)()
        length = self.lib.desk_watcher_motion_pack_move(90.0, 120.0, 250, seq, out)
        wire = bytes(out[:length])
        self.assertEqual(wire[-1], 0)
        raw = cobs_decode(wire[:-1])
        self.assertEqual(raw, frame(2, 1, seq, bytes((3, 0x84, 0x03, 0xB0, 0x04, 250, 0, 1, 0)), flags=1))

        length = self.lib.desk_watcher_motion_pack_stop(True, seq, out)
        raw = cobs_decode(bytes(out[:length - 1]))
        self.assertEqual(raw, frame(2, 2, seq, bytes((1, 0)), flags=1))

    def test_sys_ack_and_nack_correlate_by_payload_ref_seq(self) -> None:
        expected = 0x10203040
        ack = frame(1, 4, 0xDEADBEEF, expected.to_bytes(4, "little") + b"\x00\x00")
        nack = frame(1, 5, 0xAABBCCDD, expected.to_bytes(4, "little") + b"\x00\x00\x34\x12")
        ack_code, is_nack = ctypes.c_uint8(), ctypes.c_bool()

        def parse(data: bytes) -> int:
            buf = (ctypes.c_uint8 * len(data)).from_buffer_copy(data)
            return self.lib.desk_watcher_motion_parse_ack(
                buf, len(data), expected, 1, ctypes.byref(ack_code), ctypes.byref(is_nack)
            )

        self.assertEqual(parse(ack), 0)
        self.assertFalse(is_nack.value)
        self.assertEqual(parse(nack), -4)
        self.assertTrue(is_nack.value)
        self.assertEqual(ack_code.value, 0x34)
        stale_ack = frame(1, 4, expected, (expected + 1).to_bytes(4, "little") + b"\x00\x00")
        self.assertEqual(parse(stale_ack), -5)


if __name__ == "__main__":
    unittest.main()
