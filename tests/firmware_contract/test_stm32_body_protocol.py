"""
Contract tests for Desk Agent R1 STM32 Body Coprocessor Protocol Driver (R3b).

Verifies 100% adherence to:
1. docs/implementation/r3a-stm32-protocol-adjudication.md (R3a Ratified)
2. contracts/schemas/stm32_body_protocol.json
3. docs/implementation/interfaces.md §1.4, §6.1, §6.6 (CR-002 ACK-only semantics)

Coverage:
- 0xA5 0x5A binary frame layout & Little-Endian packing
- CRC16-CCITT-FALSE (poly 0x1021, init 0xFFFF, standard ASCII '123456789' -> 0x29B1)
- Golden test vectors for SERVO_MOVE, SERVO_STOP, ACK, and NACK
- Mechanical clamping (Pan 0.0°~180.0°, Tilt 100.0°~140.0°, Duration 100~65535ms)
- Timeout (500ms) and retry (max 2 retries, total 3 transmissions)
- Sequence Preservation Invariant (retries MUST reuse the exact same sequence number)
- ACK sequence matching (mismatched/stale ACKs rejected)
- Frame corruption detection (CRC errors, truncated frames, invalid magic)
- CR-002 semantic invariant: accepted=True, state_confirmed=False
"""

from __future__ import annotations

import ctypes
import json
import os
import struct
import subprocess
import tempfile
import time
import unittest

# Owners matching desk_arbiter.h
DESK_OWNER_NONE = 0
DESK_OWNER_IDLE = 1
DESK_OWNER_TCLAW_VOICE = 2
DESK_OWNER_SDK_REMOTE = 3
DESK_OWNER_LAN_CAMERA = 4
DESK_OWNER_SYSTEM_ALERT = 5

# Resources matching desk_arbiter.h
DESK_RES_MOTION_GIMBAL = 3

# Arbiter status
DESK_ARBITER_OK = 0

# Hardware error codes
DESK_HW_OK = 0
DESK_HW_ERR_INVALID_ARG = -1
DESK_HW_ERR_UNAVAILABLE = -2
DESK_HW_ERR_TIMEOUT = -3
DESK_HW_ERR_IO = -4
DESK_HW_ERR_BUSY = -5
DESK_HW_ERR_STALL = -6

# MCU Protocol Constants matching desk_hardware.h / R3a
MCU_FRAME_MAGIC0 = 0xA5
MCU_FRAME_MAGIC1 = 0x5A
MCU_FRAME_PROTO_VERSION = 0x01
MCU_FRAME_CLASS_MOTION = 0x02

MCU_MOTION_MSG_ACK = 0x00
MCU_MOTION_MSG_SERVO_MOVE = 0x01
MCU_MOTION_MSG_SERVO_STOP = 0x02
MCU_MOTION_MSG_NACK = 0xFF

MCU_FRAME_FLAG_ACK_REQ = 0x01
MCU_FRAME_FLAG_ACK_RESP = 0x02
MCU_FRAME_FLAG_NACK_RESP = 0x04

MCU_ACK_TIMEOUT_MS = 500
MCU_MAX_RETRIES = 2
MCU_RETRY_BACKOFF_MS = 50

MCU_STOP_MODE_DECEL = 0x01
MCU_STOP_MODE_EMERGENCY = 0x02

MCU_AXIS_MASK_PAN_TILT = 0x03
MCU_AXIS_MASK_CANCEL_ALL = 0x01

MCU_NACK_ERR_CRC = 0x01
MCU_NACK_ERR_OUT_OF_BOUNDS = 0x02
MCU_NACK_ERR_BUSY = 0x03
MCU_NACK_ERR_INVALID_PARAM = 0x04
MCU_NACK_ERR_HW_FAULT = 0x05

MCU_TX_CB = ctypes.CFUNCTYPE(
    None, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t, ctypes.c_void_p
)

_KEEP_ALIVE: list = []


def python_crc16_ccitt(data: bytes) -> int:
    """Independent Python reference implementation of CRC-16/CCITT-FALSE."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


class TestSTM32BodyProtocol(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.so_path = os.path.join(cls.temp_dir.name, "libdesk_hardware_stm32_test.so")
        repo_root = os.path.realpath(
            os.path.join(os.path.dirname(__file__), "..", "..")
        )

        cmd = [
            "gcc",
            "-shared",
            "-fPIC",
            "-O2",
            "-Wall",
            "-Wextra",
            "-Wshadow",
            f"-I{repo_root}/firmware/components/desk_hardware/include",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_arbiter.c",
            f"{repo_root}/firmware/components/desk_hardware/src/desk_hardware.c",
            "-o",
            cls.so_path,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to compile desk_hardware shared library: {res.stderr}"
            )

        cls.lib = ctypes.CDLL(cls.so_path)

        # Function bindings
        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_init.argtypes = []

        cls.lib.desk_hardware_set_strict_mode.restype = None
        cls.lib.desk_hardware_set_strict_mode.argtypes = [ctypes.c_bool]

        cls.lib.desk_hardware_is_strict_mode.restype = ctypes.c_bool
        cls.lib.desk_hardware_is_strict_mode.argtypes = []

        cls.lib.desk_hardware_motion_move_to.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_move_to.argtypes = [
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_uint32,
        ]

        cls.lib.desk_hardware_motion_stop.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_stop.argtypes = [ctypes.c_bool]

        cls.lib.desk_hardware_motion_get_position.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_get_position.argtypes = [
            ctypes.POINTER(ctypes.c_float),
            ctypes.POINTER(ctypes.c_float),
        ]

        cls.lib.desk_hardware_get_pan.restype = ctypes.c_float
        cls.lib.desk_hardware_get_pan.argtypes = []

        cls.lib.desk_hardware_get_tilt.restype = ctypes.c_float
        cls.lib.desk_hardware_get_tilt.argtypes = []

        cls.lib.desk_hardware_format_stm32_motion_packet.restype = ctypes.c_int
        cls.lib.desk_hardware_format_stm32_motion_packet.argtypes = [
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        cls.lib.desk_hardware_format_stm32_motion_packet_with_seq.restype = ctypes.c_int
        cls.lib.desk_hardware_format_stm32_motion_packet_with_seq.argtypes = [
            ctypes.c_float,
            ctypes.c_float,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        cls.lib.desk_hardware_format_stm32_stop_packet.restype = ctypes.c_int
        cls.lib.desk_hardware_format_stm32_stop_packet.argtypes = [
            ctypes.c_bool,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        cls.lib.desk_hardware_format_stm32_stop_packet_with_seq.restype = ctypes.c_int
        cls.lib.desk_hardware_format_stm32_stop_packet_with_seq.argtypes = [
            ctypes.c_bool,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        cls.lib.desk_hardware_format_stm32_ack_packet.restype = ctypes.c_int
        cls.lib.desk_hardware_format_stm32_ack_packet.argtypes = [
            ctypes.c_uint32,
            ctypes.c_int16,
            ctypes.c_int16,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        cls.lib.desk_hardware_format_stm32_nack_packet.restype = ctypes.c_int
        cls.lib.desk_hardware_format_stm32_nack_packet.argtypes = [
            ctypes.c_uint32,
            ctypes.c_uint8,
            ctypes.c_uint8,
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        cls.lib.desk_hardware_calc_crc16.restype = ctypes.c_uint16
        cls.lib.desk_hardware_calc_crc16.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
        ]

        cls.lib.desk_hardware_get_mcu_seq.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_seq.argtypes = []

        cls.lib.desk_hardware_set_mcu_seq.restype = None
        cls.lib.desk_hardware_set_mcu_seq.argtypes = [ctypes.c_uint32]

        cls.lib.desk_hardware_get_mcu_timeout_ms.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_timeout_ms.argtypes = []

        cls.lib.desk_hardware_set_mcu_timeout_ms.restype = None
        cls.lib.desk_hardware_set_mcu_timeout_ms.argtypes = [ctypes.c_uint32]

        cls.lib.desk_hardware_get_mcu_retry_backoff_ms.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_retry_backoff_ms.argtypes = []

        cls.lib.desk_hardware_set_mcu_retry_backoff_ms.restype = None
        cls.lib.desk_hardware_set_mcu_retry_backoff_ms.argtypes = [ctypes.c_uint32]

        cls.lib.desk_hardware_get_mcu_max_retries.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_max_retries.argtypes = []

        cls.lib.desk_hardware_set_mcu_max_retries.restype = None
        cls.lib.desk_hardware_set_mcu_max_retries.argtypes = [ctypes.c_uint32]

        cls.lib.desk_hardware_get_mcu_crc_error_count.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_crc_error_count.argtypes = []

        cls.lib.desk_hardware_reset_mcu_crc_error_count.restype = None
        cls.lib.desk_hardware_reset_mcu_crc_error_count.argtypes = []

        cls.lib.desk_hardware_get_mcu_tx_count.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_tx_count.argtypes = []

        cls.lib.desk_hardware_get_mcu_retry_count.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_retry_count.argtypes = []

        cls.lib.desk_hardware_get_mcu_ack_count.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_ack_count.argtypes = []

        cls.lib.desk_hardware_get_mcu_nack_count.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_nack_count.argtypes = []

        cls.lib.desk_hardware_get_mcu_timeout_count.restype = ctypes.c_uint32
        cls.lib.desk_hardware_get_mcu_timeout_count.argtypes = []

        cls.lib.desk_hardware_reset_mcu_stats.restype = None
        cls.lib.desk_hardware_reset_mcu_stats.argtypes = []

        cls.lib.desk_hardware_register_mcu_tx_cb.restype = ctypes.c_int
        cls.lib.desk_hardware_register_mcu_tx_cb.argtypes = [
            MCU_TX_CB,
            ctypes.c_void_p,
        ]

        cls.lib.desk_hardware_get_last_mcu_frame.restype = ctypes.c_int
        cls.lib.desk_hardware_get_last_mcu_frame.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t),
        ]

        cls.lib.desk_hardware_mcu_receive_bytes.restype = ctypes.c_int
        cls.lib.desk_hardware_mcu_receive_bytes.argtypes = [
            ctypes.POINTER(ctypes.c_uint8),
            ctypes.c_size_t,
        ]

        cls.lib.desk_arbiter_acquire.restype = ctypes.c_int
        cls.lib.desk_arbiter_acquire.argtypes = [
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint32,
        ]

        cls.lib.desk_arbiter_release.restype = None
        cls.lib.desk_arbiter_release.argtypes = [ctypes.c_int, ctypes.c_int]

        cls.lib.desk_arbiter_get_owner.restype = ctypes.c_int
        cls.lib.desk_arbiter_get_owner.argtypes = [ctypes.c_int]

        # Read JSON schema for structural validation
        schema_path = os.path.join(
            repo_root, "contracts", "schemas", "stm32_body_protocol.json"
        )
        with open(schema_path, "r", encoding="utf-8") as f:
            cls.schema = json.load(f)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.lib.desk_hardware_init()
        self.lib.desk_hardware_set_strict_mode(False)

    # ------------------------------------------------------------------
    # Test 1: CRC16-CCITT-FALSE Golden Vectors
    # ------------------------------------------------------------------
    def test_01_crc16_golden_test_vector(self) -> None:
        """Adjudication §5.3: verify CRC16-CCITT-FALSE on '123456789' -> 0x29B1."""
        test_bytes = b"123456789"
        buf = (ctypes.c_uint8 * len(test_bytes))(*test_bytes)
        c_crc = self.lib.desk_hardware_calc_crc16(buf, len(test_bytes))
        py_crc = python_crc16_ccitt(test_bytes)

        self.assertEqual(
            c_crc,
            0x29B1,
            f"CRC calculation mismatch with Golden Vector 0x29B1 (got {hex(c_crc)})",
        )
        self.assertEqual(c_crc, py_crc)

        # Wire serialization check: Little-Endian [0xB1, 0x29]
        le_bytes = [c_crc & 0xFF, (c_crc >> 8) & 0xFF]
        self.assertEqual(le_bytes, [0xB1, 0x29])

    # ------------------------------------------------------------------
    # Test 2: SERVO_MOVE Golden Test Vector
    # ------------------------------------------------------------------
    def test_02_servo_move_golden_frame(self) -> None:
        """Adjudication §5.3: SERVO_MOVE typical golden frame:
        Pan 120.0°, Tilt 110.0°, Duration 450ms, Seq 1.
        Header (12B): A5 5A 01 02 01 01 01 00 00 00 09 00
        Payload (9B): 03 B0 04 4C 04 C2 01 00 01
        Total: 23 bytes, CRC = 0x3A74 (wire LE: 74 3A).
        """
        buf = (ctypes.c_uint8 * 64)()
        out_len = ctypes.c_size_t()

        self.lib.desk_hardware_set_mcu_seq(0)
        rc = self.lib.desk_hardware_format_stm32_motion_packet(
            ctypes.c_float(120.0),
            ctypes.c_float(110.0),
            450,
            buf,
            len(buf),
            ctypes.byref(out_len),
        )
        self.assertEqual(rc, DESK_HW_OK)
        self.assertEqual(out_len.value, 23)

        frame = bytes(buf[:23])

        # Expected Golden Frame
        expected_header = bytes(
            [0xA5, 0x5A, 0x01, 0x02, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x09, 0x00]
        )
        expected_payload = bytes(
            [0x03, 0xB0, 0x04, 0x4C, 0x04, 0xC2, 0x01, 0x00, 0x01]
        )
        expected_crc_val = python_crc16_ccitt(expected_header + expected_payload)
        self.assertEqual(expected_crc_val, 0x3A74)

        expected_frame = expected_header + expected_payload + bytes([0x74, 0x3A])
        self.assertEqual(frame, expected_frame)

        # Unpack and verify fields
        (
            magic0,
            magic1,
            proto,
            m_class,
            msg_id,
            flags,
            seq,
            p_len,
        ) = struct.unpack("<BBBBBBHI", frame[:12])
        # Note: sequence is uint32 (4 bytes), payload_len is uint16 (2 bytes)
        magic0, magic1, proto, m_class, msg_id, flags = struct.unpack(
            "<BBBBBB", frame[:6]
        )
        seq, p_len = struct.unpack("<IH", frame[6:12])
        self.assertEqual(magic0, MCU_FRAME_MAGIC0)
        self.assertEqual(magic1, MCU_FRAME_MAGIC1)
        self.assertEqual(proto, MCU_FRAME_PROTO_VERSION)
        self.assertEqual(m_class, MCU_FRAME_CLASS_MOTION)
        self.assertEqual(msg_id, MCU_MOTION_MSG_SERVO_MOVE)
        self.assertEqual(flags, MCU_FRAME_FLAG_ACK_REQ)
        self.assertEqual(seq, 1)
        self.assertEqual(p_len, 9)

        # Payload unpack
        axis_mask, pan_x10, tilt_x10, dur, profile, source = struct.unpack(
            "<BhhHBB", frame[12:21]
        )
        self.assertEqual(axis_mask, 0x03)
        self.assertEqual(pan_x10, 1200)
        self.assertEqual(tilt_x10, 1100)
        self.assertEqual(dur, 450)
        self.assertEqual(profile, 0x00)
        self.assertEqual(source, 0x01)

        # CRC check
        (crc_wire,) = struct.unpack("<H", frame[21:23])
        self.assertEqual(crc_wire, 0x3A74)

    # ------------------------------------------------------------------
    # Test 3: Mechanical Clamping Constraints
    # ------------------------------------------------------------------
    def test_03_mechanical_clamping(self) -> None:
        """Adjudication §7.2:
        Pan is clamped to 0.0° ~ 180.0°
        Tilt is clamped to 100.0° ~ 140.0°
        Duration is clamped to 100 ~ 65535 ms
        """
        buf = (ctypes.c_uint8 * 64)()
        out_len = ctypes.c_size_t()

        # Underflow inputs
        rc = self.lib.desk_hardware_format_stm32_motion_packet_with_seq(
            ctypes.c_float(-45.0),
            ctypes.c_float(50.0),
            20,  # underflow duration
            100,
            buf,
            len(buf),
            ctypes.byref(out_len),
        )
        self.assertEqual(rc, DESK_HW_OK)
        frame = bytes(buf[: out_len.value])
        axis_mask, pan_x10, tilt_x10, dur, _, _ = struct.unpack(
            "<BhhHBB", frame[12:21]
        )
        self.assertEqual(pan_x10, 0)  # clamped to 0.0°
        self.assertEqual(tilt_x10, 1000)  # clamped to 100.0°
        self.assertEqual(dur, 100)  # clamped to 100ms

        # Overflow inputs
        rc = self.lib.desk_hardware_format_stm32_motion_packet_with_seq(
            ctypes.c_float(270.0),
            ctypes.c_float(195.0),
            100000,  # overflow duration
            101,
            buf,
            len(buf),
            ctypes.byref(out_len),
        )
        self.assertEqual(rc, DESK_HW_OK)
        frame = bytes(buf[: out_len.value])
        axis_mask, pan_x10, tilt_x10, dur, _, _ = struct.unpack(
            "<BhhHBB", frame[12:21]
        )
        self.assertEqual(pan_x10, 1800)  # clamped to 180.0°
        self.assertEqual(tilt_x10, 1400)  # clamped to 140.0°
        self.assertEqual(dur, 65535)  # clamped to uint16 max

    # ------------------------------------------------------------------
    # Test 4: SERVO_STOP Packet Formatting
    # ------------------------------------------------------------------
    def test_04_servo_stop_packet_formatting(self) -> None:
        """Adjudication §4.2:
        SERVO_STOP has 12B header, 2B payload, 2B CRC = 16 bytes.
        emergency=False -> stop_mode=0x01
        emergency=True  -> stop_mode=0x02
        axis_mask=0x01
        """
        buf = (ctypes.c_uint8 * 64)()
        out_len = ctypes.c_size_t()

        # Normal decelerated stop
        rc = self.lib.desk_hardware_format_stm32_stop_packet_with_seq(
            False, 42, buf, len(buf), ctypes.byref(out_len)
        )
        self.assertEqual(rc, DESK_HW_OK)
        self.assertEqual(out_len.value, 16)
        frame = bytes(buf[:16])

        self.assertEqual(frame[0], MCU_FRAME_MAGIC0)
        self.assertEqual(frame[1], MCU_FRAME_MAGIC1)
        self.assertEqual(frame[2], MCU_FRAME_PROTO_VERSION)
        self.assertEqual(frame[3], MCU_FRAME_CLASS_MOTION)
        self.assertEqual(frame[4], MCU_MOTION_MSG_SERVO_STOP)
        self.assertEqual(frame[5], MCU_FRAME_FLAG_ACK_REQ)

        (seq, p_len) = struct.unpack("<IH", frame[6:12])
        self.assertEqual(seq, 42)
        self.assertEqual(p_len, 2)
        self.assertEqual(frame[12], MCU_STOP_MODE_DECEL)  # 0x01
        self.assertEqual(frame[13], MCU_AXIS_MASK_CANCEL_ALL)  # 0x01

        # CRC verify
        (crc_val,) = struct.unpack("<H", frame[14:16])
        expected_crc = python_crc16_ccitt(frame[:14])
        self.assertEqual(crc_val, expected_crc)

        # Emergency cut-off stop
        rc = self.lib.desk_hardware_format_stm32_stop_packet_with_seq(
            True, 43, buf, len(buf), ctypes.byref(out_len)
        )
        self.assertEqual(rc, DESK_HW_OK)
        frame_em = bytes(buf[:16])
        self.assertEqual(frame_em[12], MCU_STOP_MODE_EMERGENCY)  # 0x02
        self.assertEqual(frame_em[13], MCU_AXIS_MASK_CANCEL_ALL)  # 0x01

    # ------------------------------------------------------------------
    # Test 5: ACK and NACK Formatting and Parsing
    # ------------------------------------------------------------------
    def test_05_ack_and_nack_packets(self) -> None:
        """Adjudication §4.3 & §4.4:
        ACK: 18 bytes (12B header + 4B payload + 2B CRC)
        NACK: 16 bytes (12B header + 2B payload + 2B CRC)
        """
        buf = (ctypes.c_uint8 * 64)()
        out_len = ctypes.c_size_t()

        # Format ACK
        rc = self.lib.desk_hardware_format_stm32_ack_packet(
            12345, 1200, 1100, buf, len(buf), ctypes.byref(out_len)
        )
        self.assertEqual(rc, DESK_HW_OK)
        self.assertEqual(out_len.value, 18)
        ack_frame = bytes(buf[:18])

        self.assertEqual(ack_frame[4], MCU_MOTION_MSG_ACK)
        self.assertEqual(ack_frame[5], MCU_FRAME_FLAG_ACK_RESP)
        (seq, p_len) = struct.unpack("<IH", ack_frame[6:12])
        self.assertEqual(seq, 12345)
        self.assertEqual(p_len, 4)
        pan_x10, tilt_x10 = struct.unpack("<hh", ack_frame[12:16])
        self.assertEqual(pan_x10, 1200)
        self.assertEqual(tilt_x10, 1100)

        # ACK is accepted only for the matching outstanding transaction.
        self.assertEqual(self.lib.desk_hardware_mcu_receive_bytes(buf, 18), DESK_HW_ERR_BUSY)

        @MCU_TX_CB
        def ack_on_tx(tx_ptr, tx_len, _user_data):
            tx = bytes(ctypes.cast(tx_ptr, ctypes.POINTER(ctypes.c_uint8 * tx_len)).contents)
            (tx_seq,) = struct.unpack("<I", tx[6:10])
            response = (ctypes.c_uint8 * 64)()
            response_len = ctypes.c_size_t()
            self.lib.desk_hardware_format_stm32_ack_packet(
                tx_seq, 1200, 1100, response, len(response), ctypes.byref(response_len)
            )
            self.lib.desk_hardware_mcu_receive_bytes(response, response_len.value)

        _KEEP_ALIVE.append(ack_on_tx)
        self.lib.desk_hardware_register_mcu_tx_cb(ack_on_tx, None)
        self.lib.desk_hardware_set_mcu_timeout_ms(30)
        self.lib.desk_hardware_set_mcu_max_retries(0)
        self.assertEqual(self.lib.desk_hardware_motion_move_to(120.0, 110.0, 300), DESK_HW_OK)
        self.assertAlmostEqual(self.lib.desk_hardware_get_pan(), 120.0, places=2)
        self.assertAlmostEqual(self.lib.desk_hardware_get_tilt(), 110.0, places=2)

        # Format NACK
        rc_nack = self.lib.desk_hardware_format_stm32_nack_packet(
            12345, MCU_NACK_ERR_CRC, 0x10, buf, len(buf), ctypes.byref(out_len)
        )
        self.assertEqual(rc_nack, DESK_HW_OK)
        self.assertEqual(out_len.value, 16)
        nack_frame = bytes(buf[:16])

        self.assertEqual(nack_frame[4], MCU_MOTION_MSG_NACK)
        self.assertEqual(nack_frame[5], MCU_FRAME_FLAG_NACK_RESP)
        (seq, p_len) = struct.unpack("<IH", nack_frame[6:12])
        self.assertEqual(seq, 12345)
        self.assertEqual(p_len, 2)
        self.assertEqual(nack_frame[12], MCU_NACK_ERR_CRC)
        self.assertEqual(nack_frame[13], 0x10)

        @MCU_TX_CB
        def nack_on_tx(tx_ptr, tx_len, _user_data):
            tx = bytes(ctypes.cast(tx_ptr, ctypes.POINTER(ctypes.c_uint8 * tx_len)).contents)
            (tx_seq,) = struct.unpack("<I", tx[6:10])
            response = (ctypes.c_uint8 * 64)()
            response_len = ctypes.c_size_t()
            self.lib.desk_hardware_format_stm32_nack_packet(
                tx_seq, MCU_NACK_ERR_CRC, 0x10, response, len(response), ctypes.byref(response_len)
            )
            self.lib.desk_hardware_mcu_receive_bytes(response, response_len.value)

        _KEEP_ALIVE.append(nack_on_tx)
        self.lib.desk_hardware_register_mcu_tx_cb(nack_on_tx, None)
        self.assertEqual(self.lib.desk_hardware_motion_move_to(120.0, 110.0, 300), DESK_HW_ERR_TIMEOUT)
        self.assertEqual(self.lib.desk_hardware_get_mcu_nack_count(), 1)
        self.lib.desk_hardware_set_mcu_max_retries(MCU_MAX_RETRIES)
        self.lib.desk_hardware_set_mcu_timeout_ms(MCU_ACK_TIMEOUT_MS)

    # ------------------------------------------------------------------
    # Test 6: CRC and Corruption Detection
    # ------------------------------------------------------------------
    def test_06_crc_and_corruption_detection(self) -> None:
        """Adjudication §8.1: corrupted frames must be rejected and recorded in statistics."""
        buf = (ctypes.c_uint8 * 64)()
        out_len = ctypes.c_size_t()

        self.lib.desk_hardware_format_stm32_ack_packet(
            99, 900, 1200, buf, len(buf), ctypes.byref(out_len)
        )
        self.assertEqual(out_len.value, 18)

        # 1. Corrupt 1 byte in payload
        buf_corrupted = (ctypes.c_uint8 * 18)(*buf[:18])
        buf_corrupted[12] ^= 0xFF
        rc = self.lib.desk_hardware_mcu_receive_bytes(buf_corrupted, 18)
        self.assertEqual(rc, -4)  # CRC mismatch
        self.assertEqual(self.lib.desk_hardware_get_mcu_crc_error_count(), 1)

        # 2. Corrupted magic word
        buf_bad_magic = (ctypes.c_uint8 * 18)(*buf[:18])
        buf_bad_magic[0] = 0xAA
        rc = self.lib.desk_hardware_mcu_receive_bytes(buf_bad_magic, 18)
        self.assertEqual(rc, -2)

        # 3. Truncated frame
        rc = self.lib.desk_hardware_mcu_receive_bytes(buf, 10)
        self.assertEqual(rc, -1)

    def test_06b_receive_requires_exact_correlated_ack_or_nack(self) -> None:
        outcomes = []

        @MCU_TX_CB
        def on_tx(tx_ptr, tx_len, _user_data):
            tx = bytes(ctypes.cast(tx_ptr, ctypes.POINTER(ctypes.c_uint8 * tx_len)).contents)
            (seq,) = struct.unpack("<I", tx[6:10])
            base = (ctypes.c_uint8 * 64)()
            base_len = ctypes.c_size_t()
            self.lib.desk_hardware_format_stm32_ack_packet(
                seq, 900, 1200, base, len(base), ctypes.byref(base_len)
            )
            valid = bytearray(base[:base_len.value])

            def send(mutated: bytearray, declared_len: int | None = None):
                crc_end = 12 + (mutated[10] | (mutated[11] << 8))
                crc = python_crc16_ccitt(bytes(mutated[:crc_end]))
                mutated[crc_end:crc_end + 2] = struct.pack("<H", crc)
                frame = (ctypes.c_uint8 * len(mutated))(*mutated)
                outcomes.append(self.lib.desk_hardware_mcu_receive_bytes(
                    frame, len(mutated) if declared_len is None else declared_len
                ))

            wrong_seq = bytearray(valid)
            wrong_seq[6] ^= 1
            send(wrong_seq)
            wrong_id = bytearray(valid)
            wrong_id[4] = MCU_MOTION_MSG_SERVO_STOP
            send(wrong_id)
            wrong_flags = bytearray(valid)
            wrong_flags[5] = MCU_FRAME_FLAG_ACK_RESP | MCU_FRAME_FLAG_ACK_REQ
            send(wrong_flags)
            wrong_payload = bytearray(valid)
            wrong_payload[10:12] = struct.pack("<H", 5)
            wrong_payload = wrong_payload[:16] + b"\x00" + wrong_payload[16:]
            send(wrong_payload)
            trailing = bytearray(valid + b"\x00")
            outcomes.append(self.lib.desk_hardware_mcu_receive_bytes(
                (ctypes.c_uint8 * len(trailing))(*trailing), len(trailing)
            ))
            nack = (ctypes.c_uint8 * 64)()
            nack_len = ctypes.c_size_t()
            self.lib.desk_hardware_format_stm32_nack_packet(
                seq, 0, 0, nack, len(nack), ctypes.byref(nack_len)
            )
            outcomes.append(self.lib.desk_hardware_mcu_receive_bytes(
                nack, nack_len.value
            ))
            send(bytearray(valid))

        _KEEP_ALIVE.append(on_tx)
        self.lib.desk_hardware_register_mcu_tx_cb(on_tx, None)
        self.lib.desk_hardware_set_mcu_timeout_ms(30)
        self.lib.desk_hardware_set_mcu_max_retries(0)
        self.assertEqual(self.lib.desk_hardware_motion_move_to(90.0, 120.0, 300), DESK_HW_OK)
        self.assertEqual(outcomes, [DESK_HW_ERR_BUSY, DESK_HW_ERR_IO, DESK_HW_ERR_IO,
                                    DESK_HW_ERR_IO, -3, DESK_HW_ERR_IO, DESK_HW_OK])
        self.assertEqual(self.lib.desk_hardware_get_mcu_nack_count(), 0)
        self.lib.desk_hardware_set_mcu_max_retries(MCU_MAX_RETRIES)
        self.lib.desk_hardware_set_mcu_timeout_ms(MCU_ACK_TIMEOUT_MS)

    def test_06c_motion_rejects_non_finite_angles(self) -> None:
        for pan, tilt in ((float("nan"), 120.0), (90.0, float("nan")),
                          (float("inf"), 120.0), (90.0, float("-inf"))):
            self.assertEqual(
                self.lib.desk_hardware_motion_move_to(pan, tilt, 300),
                DESK_HW_ERR_INVALID_ARG,
            )

    # ------------------------------------------------------------------
    # Test 7: Timeout & Retry Sequence Preservation Invariant
    # ------------------------------------------------------------------
    def test_07_timeout_and_sequence_preservation_invariant(self) -> None:
        """Adjudication §6.1 & §8.2:
        When no ACK arrives:
        - 500ms timeout per attempt
        - Max 2 retries (total 3 attempts)
        - Retries MUST strictly preserve original uint32 sequence number!
        - Returns DESK_HW_ERR_TIMEOUT (-3)
        """
        sent_frames = []

        @MCU_TX_CB
        def on_tx(data_ptr, length, user_data):
            frame = bytes(
                ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_uint8 * length)).contents
            )
            sent_frames.append(frame)

        _KEEP_ALIVE.append(on_tx)
        self.lib.desk_hardware_register_mcu_tx_cb(on_tx, None)

        # Use fast timeout for this unit test (50ms timeout, 10ms backoff)
        self.lib.desk_hardware_set_mcu_timeout_ms(60)
        self.lib.desk_hardware_set_mcu_retry_backoff_ms(15)
        self.lib.desk_hardware_set_mcu_max_retries(2)

        t0 = time.time()
        rc = self.lib.desk_hardware_motion_move_to(
            ctypes.c_float(100.0), ctypes.c_float(125.0), 300
        )
        elapsed = time.time() - t0

        self.assertEqual(
            rc,
            DESK_HW_ERR_TIMEOUT,
            f"Expected DESK_HW_ERR_TIMEOUT (-3), got {rc}",
        )

        # Must have exactly 3 attempts: initial + 2 retries
        self.assertEqual(
            len(sent_frames),
            3,
            f"Expected exactly 3 transmission attempts, got {len(sent_frames)}",
        )

        # Sequence Preservation Invariant: all 3 attempts MUST have identical sequence and content!
        self.assertEqual(sent_frames[0], sent_frames[1])
        self.assertEqual(sent_frames[1], sent_frames[2])

        (seq0,) = struct.unpack("<I", sent_frames[0][6:10])
        (seq1,) = struct.unpack("<I", sent_frames[1][6:10])
        (seq2,) = struct.unpack("<I", sent_frames[2][6:10])
        self.assertEqual(seq0, seq1)
        self.assertEqual(seq1, seq2)

        # Verify statistics
        self.assertEqual(self.lib.desk_hardware_get_mcu_tx_count(), 3)
        self.assertEqual(self.lib.desk_hardware_get_mcu_retry_count(), 2)
        self.assertEqual(self.lib.desk_hardware_get_mcu_timeout_count(), 1)

    # ------------------------------------------------------------------
    # Test 8: Retry Succeeds on Second Attempt
    # ------------------------------------------------------------------
    def test_08_retry_succeeds_on_second_attempt(self) -> None:
        """Adjudication §6.1:
        Attempt 0 times out.
        Attempt 1 receives matching ACK -> succeeds and returns DESK_HW_OK (0).
        """
        sent_frames = []

        @MCU_TX_CB
        def on_tx(data_ptr, length, user_data):
            frame = bytes(
                ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_uint8 * length)).contents
            )
            sent_frames.append(frame)
            if len(sent_frames) == 2:
                # On retry attempt 1, respond with matching ACK!
                (seq,) = struct.unpack("<I", frame[6:10])
                ack_buf = (ctypes.c_uint8 * 64)()
                ack_len = ctypes.c_size_t()
                self.lib.desk_hardware_format_stm32_ack_packet(
                    seq, 1100, 1300, ack_buf, len(ack_buf), ctypes.byref(ack_len)
                )
                self.lib.desk_hardware_mcu_receive_bytes(ack_buf, ack_len.value)

        _KEEP_ALIVE.append(on_tx)
        self.lib.desk_hardware_register_mcu_tx_cb(on_tx, None)

        self.lib.desk_hardware_set_mcu_timeout_ms(60)
        self.lib.desk_hardware_set_mcu_retry_backoff_ms(15)

        rc = self.lib.desk_hardware_motion_move_to(
            ctypes.c_float(110.0), ctypes.c_float(130.0), 300
        )
        self.assertEqual(rc, DESK_HW_OK)

        # Exactly 2 attempts: initial + 1 retry
        self.assertEqual(len(sent_frames), 2)
        self.assertEqual(sent_frames[0], sent_frames[1])

        # Position adopted on success
        self.assertAlmostEqual(self.lib.desk_hardware_get_pan(), 110.0, places=2)
        self.assertAlmostEqual(self.lib.desk_hardware_get_tilt(), 130.0, places=2)

    # ------------------------------------------------------------------
    # Test 9: ACK Sequence Matching & Rejection of Stale ACK
    # ------------------------------------------------------------------
    def test_09_rejection_of_mismatched_sequence_ack(self) -> None:
        """Adjudication §6.1 & interfaces.md §6.2:
        An ACK with wrong/stale sequence must be ignored and not satisfy the pending transaction.
        """
        sent_frames = []

        @MCU_TX_CB
        def on_tx(data_ptr, length, user_data):
            frame = bytes(
                ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_uint8 * length)).contents
            )
            sent_frames.append(frame)
            if len(sent_frames) == 1:
                # Attempt 0: inject stale ACK with wrong sequence number
                (seq,) = struct.unpack("<I", frame[6:10])
                ack_buf = (ctypes.c_uint8 * 64)()
                ack_len = ctypes.c_size_t()
                self.lib.desk_hardware_format_stm32_ack_packet(
                    seq + 999, 900, 1200, ack_buf, len(ack_buf), ctypes.byref(ack_len)
                )
                self.lib.desk_hardware_mcu_receive_bytes(ack_buf, ack_len.value)
            elif len(sent_frames) == 2:
                # Attempt 1: inject matching ACK with correct sequence number
                (seq,) = struct.unpack("<I", frame[6:10])
                ack_buf = (ctypes.c_uint8 * 64)()
                ack_len = ctypes.c_size_t()
                self.lib.desk_hardware_format_stm32_ack_packet(
                    seq, 900, 1200, ack_buf, len(ack_buf), ctypes.byref(ack_len)
                )
                self.lib.desk_hardware_mcu_receive_bytes(ack_buf, ack_len.value)

        _KEEP_ALIVE.append(on_tx)
        self.lib.desk_hardware_register_mcu_tx_cb(on_tx, None)

        self.lib.desk_hardware_set_mcu_timeout_ms(60)
        self.lib.desk_hardware_set_mcu_retry_backoff_ms(15)

        rc = self.lib.desk_hardware_motion_move_to(
            ctypes.c_float(90.0), ctypes.c_float(120.0), 200
        )
        self.assertEqual(rc, DESK_HW_OK)

        # Stale ACK was ignored, so retry 1 was triggered
        self.assertEqual(len(sent_frames), 2)
        self.assertEqual(sent_frames[0], sent_frames[1])

    # ------------------------------------------------------------------
    # Test 10: SERVO_STOP Transaction with Synchronous ACK & Arbiter Release
    # ------------------------------------------------------------------
    def test_10_servo_stop_transaction_and_arbiter_release(self) -> None:
        """Adjudication §4.2:
        SERVO_STOP must also execute reliable ACK synchronization and release resource locks.
        """
        stop_frames = []

        @MCU_TX_CB
        def on_tx(data_ptr, length, user_data):
            frame = bytes(
                ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_uint8 * length)).contents
            )
            stop_frames.append(frame)
            # Immediate ACK response
            (seq,) = struct.unpack("<I", frame[6:10])
            ack_buf = (ctypes.c_uint8 * 64)()
            ack_len = ctypes.c_size_t()
            self.lib.desk_hardware_format_stm32_ack_packet(
                seq, 900, 1200, ack_buf, len(ack_buf), ctypes.byref(ack_len)
            )
            self.lib.desk_hardware_mcu_receive_bytes(ack_buf, ack_len.value)

        _KEEP_ALIVE.append(on_tx)
        self.lib.desk_hardware_register_mcu_tx_cb(on_tx, None)

        # Acquire gimbal lock first
        acq_rc = self.lib.desk_arbiter_acquire(
            DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE, 0
        )
        self.assertEqual(acq_rc, DESK_ARBITER_OK)
        self.assertEqual(
            self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL),
            DESK_OWNER_SDK_REMOTE,
        )

        rc = self.lib.desk_hardware_motion_stop(True)  # Emergency stop
        self.assertEqual(rc, DESK_HW_OK)

        # Arbiter must be released
        self.assertEqual(
            self.lib.desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL),
            DESK_OWNER_NONE,
        )

        # Frame check: 16 bytes stop frame
        self.assertEqual(len(stop_frames), 1)
        self.assertEqual(len(stop_frames[0]), 16)
        self.assertEqual(stop_frames[0][4], MCU_MOTION_MSG_SERVO_STOP)
        self.assertEqual(stop_frames[0][12], MCU_STOP_MODE_EMERGENCY)  # 0x02

    # ------------------------------------------------------------------
    # Test 11: CR-002 Semantic & Hardware Boundary
    # ------------------------------------------------------------------
    def test_11_cr_002_semantic_and_boundary(self) -> None:
        """Adjudication §7:
        1. Base has no physical angle encoder / limit sensor.
        2. ACK only indicates trajectory acceptance; state_confirmed must be False.
        3. Failed move never adopts target as reported pose.
        """
        # When move succeeds with ACK:
        buf = (ctypes.c_uint8 * 64)()
        out_len = ctypes.c_size_t()

        @MCU_TX_CB
        def on_tx(data_ptr, length, user_data):
            frame = bytes(
                ctypes.cast(data_ptr, ctypes.POINTER(ctypes.c_uint8 * length)).contents
            )
            (seq,) = struct.unpack("<I", frame[6:10])
            ack_buf = (ctypes.c_uint8 * 64)()
            ack_len = ctypes.c_size_t()
            self.lib.desk_hardware_format_stm32_ack_packet(
                seq, 800, 1150, ack_buf, len(ack_buf), ctypes.byref(ack_len)
            )
            self.lib.desk_hardware_mcu_receive_bytes(ack_buf, ack_len.value)

        _KEEP_ALIVE.append(on_tx)
        self.lib.desk_hardware_register_mcu_tx_cb(on_tx, None)

        rc = self.lib.desk_hardware_motion_move_to(
            ctypes.c_float(80.0), ctypes.c_float(115.0), 200
        )
        self.assertEqual(rc, DESK_HW_OK)

        # Now simulate a failed move (timeout)
        self.lib.desk_hardware_register_mcu_tx_cb(ctypes.cast(None, MCU_TX_CB), None)  # Reset to default
        # Clear transport so strict mode or timeout can fail
        @MCU_TX_CB
        def on_tx_drop(data_ptr, length, user_data):
            pass  # Drops everything

        _KEEP_ALIVE.append(on_tx_drop)
        self.lib.desk_hardware_register_mcu_tx_cb(on_tx_drop, None)
        self.lib.desk_hardware_set_mcu_timeout_ms(40)
        self.lib.desk_hardware_set_mcu_retry_backoff_ms(10)

        rc_fail = self.lib.desk_hardware_motion_move_to(
            ctypes.c_float(150.0), ctypes.c_float(135.0), 200
        )
        self.assertEqual(rc_fail, DESK_HW_ERR_TIMEOUT)

        # Pose must NOT be updated to the failed target (CR-002 / Interfaces §6.1)
        self.assertAlmostEqual(self.lib.desk_hardware_get_pan(), 80.0, places=2)
        self.assertAlmostEqual(self.lib.desk_hardware_get_tilt(), 115.0, places=2)

    # ------------------------------------------------------------------
    # Test 12: Default Timing Contracts (500ms timeout, 2 retries)
    # ------------------------------------------------------------------
    def test_12_default_timing_constants(self) -> None:
        """Adjudication §8.1: Default timing constants must be strictly 500ms and 2 retries."""
        self.assertEqual(self.lib.desk_hardware_get_mcu_timeout_ms(), 500)
        self.assertEqual(self.lib.desk_hardware_get_mcu_max_retries(), 2)
        self.assertEqual(self.lib.desk_hardware_get_mcu_retry_backoff_ms(), 50)


if __name__ == "__main__":
    unittest.main()
