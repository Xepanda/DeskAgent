"""
Contract tests for Watcher embedded hardware drivers:
1. STM32 frame packing and standalone legacy COBS utility helpers.
2. Himax PTL camera 7-byte Base Header, 16-byte Media Header, CRC32, and multi-packet assembly.
"""

from __future__ import annotations

import ctypes
import os
import struct
import subprocess
import tempfile
import unittest


def python_crc16_ccitt(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def python_crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            mask = 0xFFFFFFFF if (crc & 1) else 0
            crc = (crc >> 1) ^ (0xEDB88320 & mask)
    return ~crc & 0xFFFFFFFF


class TestWatcherHardwareContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_watcher_hw_test.so")
        repo_root = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))

        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-Wextra",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_watcher_motion.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_watcher_camera.c",
            "-o",
            cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(f"Failed to compile desk_watcher_hw_test library: {res.stderr}")

        cls.lib = ctypes.CDLL(cls.so_path)

        # Motion functions
        cls.lib.desk_watcher_motion_cobs_encode.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t)
        ]
        cls.lib.desk_watcher_motion_cobs_encode.restype = ctypes.c_int

        cls.lib.desk_watcher_motion_cobs_decode.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t)
        ]
        cls.lib.desk_watcher_motion_cobs_decode.restype = ctypes.c_int

        cls.lib.desk_watcher_motion_wire_encode.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t)
        ]
        cls.lib.desk_watcher_motion_wire_encode.restype = ctypes.c_int

        cls.lib.desk_watcher_motion_wire_decode.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t)
        ]
        cls.lib.desk_watcher_motion_wire_decode.restype = ctypes.c_int

        cls.lib.desk_watcher_motion_pack_move.argtypes = [
            ctypes.c_float, ctypes.c_float, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8)
        ]
        cls.lib.desk_watcher_motion_pack_move.restype = ctypes.c_size_t

        cls.lib.desk_watcher_motion_pack_stop.argtypes = [
            ctypes.c_bool, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8)
        ]
        cls.lib.desk_watcher_motion_pack_stop.restype = ctypes.c_size_t

        cls.lib.desk_watcher_motion_parse_ack.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_uint32, ctypes.c_uint8,
            ctypes.POINTER(ctypes.c_uint8), ctypes.POINTER(ctypes.c_bool)
        ]
        cls.lib.desk_watcher_motion_parse_ack.restype = ctypes.c_int

        # Camera functions
        cls.lib.desk_watcher_camera_parse_ptl_header.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_uint32)
        ]
        cls.lib.desk_watcher_camera_parse_ptl_header.restype = ctypes.c_int

        cls.lib.desk_watcher_camera_validate_frame.argtypes = [
            ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_uint8)),
            ctypes.POINTER(ctypes.c_size_t)
        ]
        cls.lib.desk_watcher_camera_validate_frame.restype = ctypes.c_int

        class CameraControlState(ctypes.Structure):
            _fields_ = [
                ("result", ctypes.c_uint32),
                ("generation", ctypes.c_uint32),
                ("mode", ctypes.c_uint32),
                ("model", ctypes.c_uint32),
                ("debug", ctypes.c_uint32),
                ("error", ctypes.c_uint32),
            ]

        class CameraResync(ctypes.Structure):
            _fields_ = [("bytes", ctypes.c_uint8 * 7), ("used", ctypes.c_size_t)]

        cls.CameraControlState = CameraControlState
        cls.CameraResync = CameraResync
        cls.lib.desk_watcher_camera_parse_control_state.argtypes = [
            ctypes.c_char_p, ctypes.c_uint32, ctypes.POINTER(CameraControlState)
        ]
        cls.lib.desk_watcher_camera_parse_control_state.restype = ctypes.c_int
        cls.lib.desk_watcher_camera_resync_push.argtypes = [
            ctypes.POINTER(CameraResync), ctypes.c_uint8, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32)
        ]
        cls.lib.desk_watcher_camera_resync_push.restype = ctypes.c_bool

    def test_cobs_roundtrip_vectors(self) -> None:
        """Verifies legacy utility helpers; the STM32 UART transaction does not use COBS."""
        test_cases = [
            b"Hello World",
            b"\x00",
            b"\x00\x00",
            b"\x01\x02\x03\x00\x04\x05",
            bytes(range(1, 255)),  # 254 non-zero bytes (single COBS block)
            bytes(range(1, 255)) + b"\x00" + b"extra",
        ]
        for data in test_cases:
            src = (ctypes.c_uint8 * len(data))(*data)
            dst_len = len(data) + (len(data) // 254) + 4
            dst = (ctypes.c_uint8 * dst_len)()
            out_len = ctypes.c_size_t(0)

            rc = self.lib.desk_watcher_motion_cobs_encode(
                src, len(data), dst, dst_len, ctypes.byref(out_len)
            )
            self.assertEqual(rc, 0)
            encoded = bytes(dst[:out_len.value])
            # Encoded block MUST NOT contain 0x00
            self.assertNotIn(0, encoded)

            # Decode back
            decoded = (ctypes.c_uint8 * (len(data) + 16))()
            dec_len = ctypes.c_size_t(0)
            rc_dec = self.lib.desk_watcher_motion_cobs_decode(
                dst, out_len.value, decoded, len(decoded), ctypes.byref(dec_len)
            )
            self.assertEqual(rc_dec, 0)
            self.assertEqual(bytes(decoded[:dec_len.value]), data)

    def test_wire_framing_delimiter(self) -> None:
        """Verifies retained legacy framing helpers, not the ratified UART wire format."""
        payload = b"\xa5\x5a\x01\x02\x01\x01\x05\x00\x00\x00\x09\x00\x03\xb0\x04\x4c\x04\xc2\x01\x00\x01"
        crc = python_crc16_ccitt(payload)
        raw_frame = payload + struct.pack("<H", crc)

        src = (ctypes.c_uint8 * len(raw_frame))(*raw_frame)
        wire_buf = (ctypes.c_uint8 * 64)()
        wire_len = ctypes.c_size_t(0)

        rc = self.lib.desk_watcher_motion_wire_encode(
            src, len(raw_frame), wire_buf, 64, ctypes.byref(wire_len)
        )
        self.assertEqual(rc, 0)
        wire_bytes = bytes(wire_buf[:wire_len.value])

        # Terminal byte MUST be 0x00 delimiter
        self.assertEqual(wire_bytes[-1], 0x00)
        # All preceding bytes must be non-zero
        self.assertNotIn(0, wire_bytes[:-1])

        # Decode wire frame
        dec_buf = (ctypes.c_uint8 * 64)()
        dec_len = ctypes.c_size_t(0)
        rc_dec = self.lib.desk_watcher_motion_wire_decode(
            wire_buf, wire_len.value, dec_buf, 64, ctypes.byref(dec_len)
        )
        self.assertEqual(rc_dec, 0)
        self.assertEqual(bytes(dec_buf[:dec_len.value]), raw_frame)

    def test_motion_pack_move_and_stop_structure(self) -> None:
        """Verifies binary packet packing for SERVO_MOVE (23B) and SERVO_STOP (16B)."""
        buf = (ctypes.c_uint8 * 32)()
        sz_move = self.lib.desk_watcher_motion_pack_move(
            ctypes.c_float(90.0), ctypes.c_float(120.0), 500, 42, buf
        )
        self.assertEqual(sz_move, 23)
        raw_move = bytes(buf[:23])
        self.assertEqual(raw_move[0], 0xA5)
        self.assertEqual(raw_move[1], 0x5A)
        self.assertEqual(raw_move[2], 0x01)  # Version
        self.assertEqual(raw_move[3], 0x02)  # Motion class
        self.assertEqual(raw_move[4], 0x01)  # SERVO_MOVE
        self.assertEqual(raw_move[5], 0x01)  # ACK_REQ
        seq = struct.unpack("<I", raw_move[6:10])[0]
        self.assertEqual(seq, 42)
        plen = struct.unpack("<H", raw_move[10:12])[0]
        self.assertEqual(plen, 9)
        # Verify CRC16
        calc_crc = python_crc16_ccitt(raw_move[:21])
        pkt_crc = struct.unpack("<H", raw_move[21:23])[0]
        self.assertEqual(calc_crc, pkt_crc)

        sz_stop = self.lib.desk_watcher_motion_pack_stop(True, 43, buf)
        self.assertEqual(sz_stop, 16)
        raw_stop = bytes(buf[:16])
        self.assertEqual(raw_stop[4], 0x02)  # SERVO_STOP
        calc_stop_crc = python_crc16_ccitt(raw_stop[:14])
        pkt_stop_crc = struct.unpack("<H", raw_stop[14:16])[0]
        self.assertEqual(calc_stop_crc, pkt_stop_crc)

    def test_native_binary_ack_parser(self) -> None:
        """UART ACKs are raw fixed-length frames, not COBS-delimited frames."""
        payload = bytes([0xA5, 0x5A, 1, 2, 0, 2]) + struct.pack("<IH", 0x12345678, 4)
        payload += struct.pack("<hh", 900, 1200)
        raw = payload + struct.pack("<H", python_crc16_ccitt(payload))
        frame = (ctypes.c_uint8 * len(raw))(*raw)
        nack = ctypes.c_uint8(0xFF)
        is_nack = ctypes.c_bool(False)

        self.assertEqual(len(raw), 18)
        self.assertEqual(
            self.lib.desk_watcher_motion_parse_ack(frame, len(raw), 0x12345678, 1,
                                                   ctypes.byref(nack), ctypes.byref(is_nack)),
            0,
        )
        self.assertFalse(is_nack.value)
        self.assertEqual(
            self.lib.desk_watcher_motion_parse_ack(frame, len(raw), 0x12345679, 1,
                                                   ctypes.byref(nack), ctypes.byref(is_nack)),
            -5,
        )

        bad = (ctypes.c_uint8 * len(raw))(*raw)
        bad[-1] ^= 0x01
        self.assertEqual(
            self.lib.desk_watcher_motion_parse_ack(bad, len(raw), 0x12345678, 1,
                                                   ctypes.byref(nack), ctypes.byref(is_nack)),
            -4,
        )

    def test_native_binary_ack_requires_expected_command_and_valid_shape(self) -> None:
        payload = bytes([0xA5, 0x5A, 1, 2, 1, 2]) + struct.pack("<IH", 7, 4)
        payload += struct.pack("<hh", 900, 1200)
        raw = payload + struct.pack("<H", python_crc16_ccitt(payload))
        frame = (ctypes.c_uint8 * len(raw))(*raw)
        nack = ctypes.c_uint8(0)
        is_nack = ctypes.c_bool(False)
        parse = self.lib.desk_watcher_motion_parse_ack

        # Echoed MOVE ACK is valid only for the matching pending command.
        self.assertEqual(parse(frame, len(raw), 7, 1, ctypes.byref(nack), ctypes.byref(is_nack)), 0)
        self.assertEqual(parse(frame, len(raw), 7, 2, ctypes.byref(nack), ctypes.byref(is_nack)), -5)

        # An ACK frame with an unexpected flag bit or malformed payload is ignored.
        malformed_payload = bytes([0xA5, 0x5A, 1, 2, 1, 3]) + struct.pack("<IH", 7, 4)
        malformed_payload += struct.pack("<hh", 900, 1200)
        malformed = (ctypes.c_uint8 * (len(malformed_payload) + 2))(
            *(malformed_payload + struct.pack("<H", python_crc16_ccitt(malformed_payload)))
        )
        self.assertEqual(parse(malformed, len(malformed), 7, 1,
                               ctypes.byref(nack), ctypes.byref(is_nack)), -5)

    def test_zero_code_nack_is_rejected(self) -> None:
        payload = bytes([0xA5, 0x5A, 1, 2, 0xFF, 4]) + struct.pack("<IH", 9, 2)
        payload += bytes([0, 0])
        raw = payload + struct.pack("<H", python_crc16_ccitt(payload))
        frame = (ctypes.c_uint8 * len(raw))(*raw)
        nack = ctypes.c_uint8(0xFF)
        is_nack = ctypes.c_bool(False)
        rc = self.lib.desk_watcher_motion_parse_ack(frame, len(raw), 9, 2,
                                                   ctypes.byref(nack), ctypes.byref(is_nack))
        self.assertEqual(rc, -4)
        self.assertFalse(is_nack.value)
        self.assertEqual(nack.value, 0)

    def test_nack_requires_exact_flags(self) -> None:
        payload = bytes([0xA5, 0x5A, 1, 2, 0xFF, 0x05]) + struct.pack("<IH", 9, 2)
        payload += bytes([1, 0])
        raw = payload + struct.pack("<H", python_crc16_ccitt(payload))
        frame = (ctypes.c_uint8 * len(raw))(*raw)
        nack = ctypes.c_uint8(0)
        is_nack = ctypes.c_bool(False)
        rc = self.lib.desk_watcher_motion_parse_ack(frame, len(raw), 9, 2,
                                                   ctypes.byref(nack), ctypes.byref(is_nack))
        self.assertEqual(rc, -4)
        self.assertFalse(is_nack.value)

    def test_camera_ptl_header_parsing(self) -> None:
        """Verifies Himax PTL 7-byte Base Header parsing and negative rejection."""
        # Valid header: 0xC0 0x5A, media_type=0x01, body_size=1024 (LE)
        valid_hdr = bytes([0xC0, 0x5A, 0x01]) + struct.pack("<I", 1024)
        c_hdr = (ctypes.c_uint8 * 7)(*valid_hdr)
        body_size = ctypes.c_uint32(0)

        rc = self.lib.desk_watcher_camera_parse_ptl_header(c_hdr, 7, ctypes.byref(body_size))
        self.assertEqual(rc, 0)
        self.assertEqual(body_size.value, 1024)

        # Negative: invalid magic 0xC0 0x5B
        bad_magic = bytes([0xC0, 0x5B, 0x01]) + struct.pack("<I", 1024)
        rc_bad = self.lib.desk_watcher_camera_parse_ptl_header(
            (ctypes.c_uint8 * 7)(*bad_magic), 7, ctypes.byref(body_size)
        )
        self.assertEqual(rc_bad, -2)

        # Negative: invalid media type (not JPEG 0x01)
        bad_type = bytes([0xC0, 0x5A, 0x02]) + struct.pack("<I", 1024)
        rc_type = self.lib.desk_watcher_camera_parse_ptl_header(
            (ctypes.c_uint8 * 7)(*bad_type), 7, ctypes.byref(body_size)
        )
        self.assertEqual(rc_type, -3)

        # Negative: body size smaller than 16-byte media header
        bad_size = bytes([0xC0, 0x5A, 0x01]) + struct.pack("<I", 10)
        rc_sz = self.lib.desk_watcher_camera_parse_ptl_header(
            (ctypes.c_uint8 * 7)(*bad_size), 7, ctypes.byref(body_size)
        )
        self.assertEqual(rc_sz, -4)

    def test_camera_control_status_requires_matching_request_id_and_valid_fields(self) -> None:
        state = self.CameraControlState()
        self.assertEqual(
            self.lib.desk_watcher_camera_parse_control_state(
                b"WV1 STATE 17 0 42 0 0 0 0", 17, ctypes.byref(state)
            ),
            0,
        )
        self.assertEqual((state.generation, state.mode, state.model, state.error), (42, 0, 0, 0))
        self.assertNotEqual(
            self.lib.desk_watcher_camera_parse_control_state(
                b"WV1 STATE 18 0 42 0 0 0 0", 17, ctypes.byref(state)
            ),
            0,
        )
        self.assertNotEqual(
            self.lib.desk_watcher_camera_parse_control_state(
                b"WV1 STATE 17 0 42 9 0 0 0", 17, ctypes.byref(state)
            ),
            0,
        )

    def test_camera_ptl_resync_finds_header_across_byte_boundaries(self) -> None:
        scanner = self.CameraResync()
        body_size = ctypes.c_uint32(0)
        header = b"\xC0\x5A\x01" + struct.pack("<I", 1024)
        found = False
        for byte in b"noise" + header:
            found = self.lib.desk_watcher_camera_resync_push(
                ctypes.byref(scanner), byte, 128 * 1024, ctypes.byref(body_size)
            )
        self.assertTrue(found)
        self.assertEqual(body_size.value, 1024)

    def test_camera_ptl_resync_skips_observed_misaligned_capture_bytes(self) -> None:
        # Captured from the reported device log: these bytes end in the media
        # magic WR, so they are not a PTL header. Follow with the upstream
        # PTL base-header layout and require the C scanner to find that header.
        observed = bytes.fromhex("08 80 15 00 00 57 52")
        official_layout_header = b"\xC0\x5A\x01" + struct.pack("<I", 1024)
        scanner = self.CameraResync()
        body_size = ctypes.c_uint32(0)
        found = False
        for byte in observed + official_layout_header:
            found = self.lib.desk_watcher_camera_resync_push(
                ctypes.byref(scanner), byte, 128 * 1024, ctypes.byref(body_size)
            )
        self.assertTrue(found)
        self.assertEqual(body_size.value, 1024)

    def test_camera_ptl_frame_assembly_and_crc32(self) -> None:
        """Verifies complete PTL Media Header validation, CRC32, and JPEG extraction."""
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        crc32_val = python_crc32(fake_jpeg)

        # 16-byte Media Header: 'W' 'R', version 1, flags 0, sequence 100, timestamp 5000, crc32
        media_header = struct.pack(
            "<BBBBIII",
            0x57,  # 'W'
            0x52,  # 'R'
            1,     # Version
            0x20,  # Flags (640x480)
            100,   # Sequence
            5000,  # Timestamp ms
            crc32_val,
        )
        self.assertEqual(len(media_header), 16)

        body = media_header + fake_jpeg
        c_body = (ctypes.c_uint8 * len(body))(*body)

        out_ptr = ctypes.POINTER(ctypes.c_uint8)()
        out_sz = ctypes.c_size_t(0)

        rc = self.lib.desk_watcher_camera_validate_frame(
            c_body, len(body), ctypes.byref(out_ptr), ctypes.byref(out_sz)
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out_sz.value, len(fake_jpeg))
        extracted = bytes(ctypes.string_at(out_ptr, out_sz.value))
        self.assertEqual(extracted, fake_jpeg)

        # The locked HX media protocol requires the 640x480 source profile.
        # A structurally valid JPEG with a 320x240 profile is not a valid
        # Watcher frame and must not be exported through the board HAL.
        wrong_profile_header = struct.pack(
            "<BBBBIII", 0x57, 0x52, 1, 0x10, 100, 5000, crc32_val
        )
        wrong_profile_body = (ctypes.c_uint8 * len(body))(*(wrong_profile_header + fake_jpeg))
        rc_profile = self.lib.desk_watcher_camera_validate_frame(
            wrong_profile_body, len(body), ctypes.byref(out_ptr), ctypes.byref(out_sz)
        )
        self.assertEqual(rc_profile, -6)  # unsupported HX source profile

        # Negative test: Corrupted CRC32
        corrupted_hdr = media_header[:12] + struct.pack("<I", crc32_val ^ 0xDEADBEEF)
        bad_body = (ctypes.c_uint8 * len(body))(*(corrupted_hdr + fake_jpeg))
        rc_crc = self.lib.desk_watcher_camera_validate_frame(
            bad_body, len(body), ctypes.byref(out_ptr), ctypes.byref(out_sz)
        )
        self.assertEqual(rc_crc, -4)  # CRC mismatch

        # Negative test: Corrupted Media magic
        bad_magic_hdr = b"XX" + media_header[2:]
        bad_magic_body = (ctypes.c_uint8 * len(body))(*(bad_magic_hdr + fake_jpeg))
        rc_magic = self.lib.desk_watcher_camera_validate_frame(
            bad_magic_body, len(body), ctypes.byref(out_ptr), ctypes.byref(out_sz)
        )
        self.assertEqual(rc_magic, -2)


if __name__ == "__main__":
    unittest.main()
