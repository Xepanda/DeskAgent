"""Contract tests for stateful 24 kHz mono PCM playback conversion."""

from __future__ import annotations

import ctypes
import math
import os
import subprocess
import tempfile
import unittest


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


class TestDeskAudioResampler(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        repo = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))
        cls.lib_path = os.path.join(cls.temp_dir.name, "libdesk_audio_resampler.so")
        result = subprocess.run(
            [
                "gcc", "-shared", "-fPIC", "-O2", "-Wall", "-Werror",
                f"-I{repo}/firmware/components/desk_hardware/include",
                f"{repo}/firmware/components/desk_hardware/src/desk_audio_resampler.c",
                "-o", cls.lib_path,
            ], capture_output=True, text=True,
        )
        if result.returncode:
            raise RuntimeError(result.stderr)
        cls.lib = ctypes.CDLL(cls.lib_path)
        cls.lib.desk_audio_resampler_init.argtypes = [ctypes.POINTER(ResamplerState)]
        cls.lib.desk_audio_resampler_process.argtypes = [
            ctypes.POINTER(ResamplerState), ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t,
            ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        ]
        cls.lib.desk_audio_resampler_process.restype = ctypes.c_int
        cls.lib.desk_audio_resampler_finish.argtypes = [
            ctypes.POINTER(ResamplerState), ctypes.POINTER(ctypes.c_int16),
            ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        ]
        cls.lib.desk_audio_resampler_finish.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def convert(self, chunks: list[list[int]]) -> list[int]:
        state = ResamplerState()
        self.lib.desk_audio_resampler_init(ctypes.byref(state))
        result: list[int] = []
        for chunk in chunks:
            source = (ctypes.c_int16 * len(chunk))(*chunk)
            target = (ctypes.c_int16 * 5000)()
            written = ctypes.c_size_t()
            rc = self.lib.desk_audio_resampler_process(
                ctypes.byref(state), source, len(chunk), target, len(target), ctypes.byref(written)
            )
            self.assertEqual(rc, 0)
            result.extend(target[:written.value])
        tail = (ctypes.c_int16 * 4)()
        written = ctypes.c_size_t()
        self.assertEqual(
            self.lib.desk_audio_resampler_finish(ctypes.byref(state), tail, len(tail), ctypes.byref(written)), 0
        )
        result.extend(tail[:written.value])
        self.assertEqual(state.output_samples, len(result))
        return result

    def convert_tone(self, frequency_hz: int, sample_count: int = 24000) -> list[int]:
        samples = [round(12000 * math.sin(2 * math.pi * frequency_hz * i / 24000))
                   for i in range(sample_count)]
        return self.convert([samples[i:i + 2400] for i in range(0, sample_count, 2400)])

    @staticmethod
    def tone_peak(samples: list[int], frequency_hz: int, sample_rate_hz: int = 16000) -> float:
        # Omit both ends so a causal FIR's startup and finite-stream tail do not
        # dominate the steady-state frequency-response measurement.
        steady = samples[300:-300]
        real = sum(sample * math.cos(2 * math.pi * frequency_hz * i / sample_rate_hz)
                   for i, sample in enumerate(steady))
        imag = sum(sample * math.sin(2 * math.pi * frequency_hz * i / sample_rate_hz)
                   for i, sample in enumerate(steady))
        return 2 * math.hypot(real, imag) / len(steady)

    def test_chunk_boundaries_do_not_change_output_or_lose_duration(self) -> None:
        samples = [((index * 733) % 30000) - 15000 for index in range(6001)]
        whole = self.convert([samples])
        split = self.convert([samples[:1], samples[1:17], samples[17:2050], samples[2050:4097], samples[4097:]])
        self.assertEqual(split, whole)
        self.assertEqual(len(whole), (2 * len(samples) + 1) // 3)

    def test_three_input_samples_produce_two_filtered_output_samples(self) -> None:
        whole = self.convert([[0, 300, 900]])
        split = self.convert([[0], [300], [900]])
        self.assertEqual(len(whole), 2)
        self.assertEqual(split, whole)

    def test_empty_and_repeated_finish_are_safe(self) -> None:
        self.assertEqual(self.convert([[], []]), [])

    def test_output_capacity_failure_does_not_consume_input(self) -> None:
        state = ResamplerState()
        self.lib.desk_audio_resampler_init(ctypes.byref(state))
        source = (ctypes.c_int16 * 3)(1, 2, 3)
        target = (ctypes.c_int16 * 1)()
        written = ctypes.c_size_t()
        rc = self.lib.desk_audio_resampler_process(
            ctypes.byref(state), source, 3, target, 1, ctypes.byref(written)
        )
        self.assertNotEqual(rc, 0)
        self.assertEqual(state.input_samples, 0)
        self.assertEqual(written.value, 0)

    def test_12khz_nyquist_input_is_suppressed(self) -> None:
        converted = self.convert_tone(12000)
        self.assertLess(self.tone_peak(converted, 4000), 500)

    def test_antialias_filter_retains_passband_and_suppresses_folded_tones(self) -> None:
        pass_1khz = self.tone_peak(self.convert_tone(1000), 1000)
        pass_6khz = self.tone_peak(self.convert_tone(6000), 6000)
        alias_9khz = self.tone_peak(self.convert_tone(9000), 7000)
        alias_10khz = self.tone_peak(self.convert_tone(10000), 6000)

        self.assertGreater(pass_1khz, 11400)
        self.assertGreater(pass_6khz, 8000)
        self.assertLess(alias_9khz, pass_6khz * 0.15)
        self.assertLess(alias_10khz, pass_6khz * 0.03)


if __name__ == "__main__":
    unittest.main()
