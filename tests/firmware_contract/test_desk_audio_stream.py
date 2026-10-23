"""L2 state-machine tests for bounded SDK PCM playback HAL buffering."""

from __future__ import annotations

import ctypes
import os
import subprocess
import tempfile
import unittest

CHUNK_BYTES = 4096
QUEUE_SLOTS = 8
RECEIVING = 1
DRAINING = 2
CANCELING = 3
COMPLETE = 4
CANCELED = 5
FAILED = 6


class ResamplerState(ctypes.Structure):
    _fields_ = [
        ("pending", ctypes.c_int16 * 3),
        ("pending_count", ctypes.c_uint8),
        ("input_samples", ctypes.c_uint64),
        ("output_samples", ctypes.c_uint64),
        ("finished", ctypes.c_bool),
        ("fir_history", ctypes.c_int16 * 19),
        ("fir_position", ctypes.c_uint8),
    ]


class Slot(ctypes.Structure):
    _fields_ = [("data", ctypes.c_uint8 * CHUNK_BYTES), ("bytes", ctypes.c_uint16), ("sequence", ctypes.c_uint32)]


class StreamContext(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint64),
        ("connection_generation", ctypes.c_uint32),
        ("stream_id", ctypes.c_uint32),
        ("expected_rx_seq", ctypes.c_uint32),
        ("total_bytes", ctypes.c_uint32),
        ("accepted_bytes", ctypes.c_uint32),
        ("error_code", ctypes.c_uint32),
        ("queue_head", ctypes.c_uint8),
        ("queue_depth", ctypes.c_uint8),
        ("playing", ctypes.c_bool),
        ("owner_held", ctypes.c_bool),
        ("cancel_requested", ctypes.c_bool),
        ("finish_requested", ctypes.c_bool),
        ("state", ctypes.c_int),
        ("resampler", ResamplerState),
        ("queue", Slot * QUEUE_SLOTS),
    ]


class StreamStatus(ctypes.Structure):
    _fields_ = [
        ("handle", ctypes.c_uint64),
        ("connection_generation", ctypes.c_uint32),
        ("stream_id", ctypes.c_uint32),
        ("expected_rx_seq", ctypes.c_uint32),
        ("total_bytes", ctypes.c_uint32),
        ("accepted_bytes", ctypes.c_uint32),
        ("error_code", ctypes.c_uint32),
        ("free_frames", ctypes.c_uint16),
        ("pending_frames", ctypes.c_uint16),
        ("queue_depth", ctypes.c_uint16),
        ("start_buffer_frames", ctypes.c_uint16),
        ("state", ctypes.c_int),
        ("playing", ctypes.c_bool),
        ("tx_drained", ctypes.c_bool),
    ]


class TestDeskAudioStream(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        repo = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
        cls.lib_path = os.path.join(cls.temp_dir.name, "libdesk_audio_stream.so")
        result = subprocess.run(
            [
                "gcc", "-shared", "-fPIC", "-O2", "-Wall", "-Werror",
                f"-I{repo}/firmware/components/desk_hardware/include",
                f"{repo}/firmware/components/desk_hardware/src/desk_audio_resampler.c",
                f"{repo}/firmware/components/desk_hardware/src/desk_audio_stream.c",
                "-o", cls.lib_path,
            ], capture_output=True, text=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.lib = ctypes.CDLL(cls.lib_path)
        cls.lib.desk_audio_stream_begin.argtypes = [
            ctypes.POINTER(StreamContext), ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.c_uint8, ctypes.c_uint8, ctypes.c_uint32, ctypes.c_bool,
        ]
        cls.lib.desk_audio_stream_write.argtypes = [
            ctypes.POINTER(StreamContext), ctypes.c_uint64, ctypes.c_uint32, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint8), ctypes.c_size_t,
        ]
        cls.lib.desk_audio_stream_finish.argtypes = [ctypes.POINTER(StreamContext), ctypes.c_uint64]
        cls.lib.desk_audio_stream_cancel.argtypes = [ctypes.POINTER(StreamContext), ctypes.c_uint64, ctypes.c_uint32]
        cls.lib.desk_audio_stream_next_output.argtypes = [
            ctypes.POINTER(StreamContext), ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_size_t), ctypes.POINTER(ctypes.c_bool),
        ]
        cls.lib.desk_audio_stream_write_complete.argtypes = [ctypes.POINTER(StreamContext), ctypes.c_uint64, ctypes.c_int]
        cls.lib.desk_audio_stream_confirm_static.argtypes = [ctypes.POINTER(StreamContext), ctypes.c_uint64, ctypes.c_bool]
        cls.lib.desk_audio_stream_get_status.argtypes = [ctypes.POINTER(StreamContext), ctypes.c_uint64, ctypes.POINTER(StreamStatus)]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def begin(self, total: int, capture_active: bool = False, handle: int = 0x1234) -> StreamContext:
        ctx = StreamContext()
        self.assertEqual(self.lib.desk_audio_stream_begin(ctypes.byref(ctx), handle, 41, 7, 24000, 1, 2, total, capture_active), 0)
        return ctx

    def write(self, ctx: StreamContext, seq: int, payload: bytes, generation: int = 41, stream_id: int = 7) -> int:
        array = (ctypes.c_uint8 * len(payload)).from_buffer_copy(payload)
        return self.lib.desk_audio_stream_write(ctypes.byref(ctx), ctx.handle, generation, stream_id, seq, array, len(payload))

    def status(self, ctx: StreamContext) -> StreamStatus:
        result = StreamStatus()
        self.assertEqual(self.lib.desk_audio_stream_get_status(ctypes.byref(ctx), ctx.handle, ctypes.byref(result)), 0)
        return result

    def test_bounded_queue_credit_tracks_slots_and_backpressure_is_retryable(self) -> None:
        ctx = self.begin(CHUNK_BYTES * (QUEUE_SLOTS + 1))
        chunk = bytes(CHUNK_BYTES)
        for seq in range(QUEUE_SLOTS):
            self.assertEqual(self.write(ctx, seq, chunk), 0)
        status = self.status(ctx)
        self.assertEqual((status.queue_depth, status.pending_frames, status.free_frames), (8, 8, 0))
        self.assertEqual(self.write(ctx, QUEUE_SLOTS, chunk), -3)
        self.assertEqual(ctx.accepted_bytes, CHUNK_BYTES * QUEUE_SLOTS)
        self.assertEqual(ctx.expected_rx_seq, QUEUE_SLOTS)

        output = (ctypes.c_int16 * 2048)()
        written = ctypes.c_size_t()
        needs_static = ctypes.c_bool()
        self.assertEqual(self.lib.desk_audio_stream_next_output(ctypes.byref(ctx), output, 2048, ctypes.byref(written), ctypes.byref(needs_static)), 1)
        self.assertTrue(ctx.playing)
        self.assertEqual(self.lib.desk_audio_stream_write_complete(ctypes.byref(ctx), ctx.handle, 0), 0)
        self.assertEqual(self.status(ctx).free_frames, 1)
        self.assertEqual(self.write(ctx, QUEUE_SLOTS, chunk), 0)
        self.assertEqual(self.status(ctx).expected_rx_seq, QUEUE_SLOTS + 1)

    def test_capture_busy_format_errors_generation_and_sequence_fail_closed(self) -> None:
        ctx = StreamContext()
        self.assertEqual(self.lib.desk_audio_stream_begin(ctypes.byref(ctx), 1, 41, 7, 24000, 1, 2, 2, True), -2)
        self.assertEqual(self.lib.desk_audio_stream_begin(ctypes.byref(ctx), 1, 41, 7, 16000, 1, 2, 2, False), -1)
        ctx = self.begin(4)
        self.assertNotEqual(self.write(ctx, 0, b"\0\0", generation=42), 0)
        self.assertEqual(ctx.state, CANCELING)
        self.assertTrue(ctx.owner_held)
        self.assertEqual(self.lib.desk_audio_stream_confirm_static(ctypes.byref(ctx), ctx.handle, True), 0)
        self.assertFalse(ctx.owner_held)
        self.assertEqual(ctx.state, FAILED)

        ctx = self.begin(4, handle=0x5678)
        self.assertNotEqual(self.write(ctx, 1, b"\0\0"), 0)
        self.assertEqual(ctx.state, CANCELING)
        self.assertEqual(ctx.error_code, 4)  # sequence failure

    def test_cancel_waits_for_inflight_write_and_static_confirmation(self) -> None:
        ctx = self.begin(4096)
        self.assertEqual(self.write(ctx, 0, bytes(4096)), 0)
        output = (ctypes.c_int16 * 2048)()
        written = ctypes.c_size_t()
        needs_static = ctypes.c_bool()
        self.assertEqual(self.lib.desk_audio_stream_next_output(ctypes.byref(ctx), output, 2048, ctypes.byref(written), ctypes.byref(needs_static)), 1)
        self.assertEqual(self.lib.desk_audio_stream_cancel(ctypes.byref(ctx), ctx.handle, 0), 0)
        self.assertEqual(self.lib.desk_audio_stream_confirm_static(ctypes.byref(ctx), ctx.handle, True), -1)
        self.assertTrue(ctx.owner_held)
        self.assertEqual(self.lib.desk_audio_stream_write_complete(ctypes.byref(ctx), ctx.handle, 0), 0)
        self.assertEqual(self.lib.desk_audio_stream_next_output(ctypes.byref(ctx), output, 2048, ctypes.byref(written), ctypes.byref(needs_static)), 0)
        self.assertTrue(needs_static.value)
        self.assertEqual(self.lib.desk_audio_stream_confirm_static(ctypes.byref(ctx), ctx.handle, False), -2)
        self.assertTrue(ctx.owner_held)  # timeout/error retains owner
        self.assertEqual(self.lib.desk_audio_stream_confirm_static(ctypes.byref(ctx), ctx.handle, True), 0)
        status = self.status(ctx)
        self.assertFalse(ctx.owner_held)
        self.assertEqual(status.state, CANCELED)
        self.assertTrue(status.tx_drained)

    def test_finish_reports_complete_only_after_tail_and_drain_confirmation(self) -> None:
        ctx = self.begin(6)
        self.assertEqual(self.write(ctx, 0, b"\x01\x00\x02\x00\x03\x00"), 0)
        self.assertEqual(self.lib.desk_audio_stream_finish(ctypes.byref(ctx), ctx.handle), 0)
        output = (ctypes.c_int16 * 2048)()
        written = ctypes.c_size_t()
        needs_static = ctypes.c_bool()
        self.assertEqual(self.lib.desk_audio_stream_next_output(ctypes.byref(ctx), output, 2048, ctypes.byref(written), ctypes.byref(needs_static)), 1)
        self.assertEqual(written.value, 2)
        self.assertEqual(self.lib.desk_audio_stream_write_complete(ctypes.byref(ctx), ctx.handle, 0), 0)
        self.assertEqual(self.lib.desk_audio_stream_next_output(ctypes.byref(ctx), output, 2048, ctypes.byref(written), ctypes.byref(needs_static)), 0)
        self.assertTrue(needs_static.value)
        self.assertEqual(ctx.state, DRAINING)
        self.assertTrue(ctx.owner_held)
        self.assertEqual(self.lib.desk_audio_stream_confirm_static(ctypes.byref(ctx), ctx.handle, True), 0)
        self.assertEqual(self.status(ctx).state, COMPLETE)


if __name__ == "__main__":
    unittest.main()
