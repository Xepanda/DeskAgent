"""Executable ESP-branch UART fault harness for CR-020 (L2 only)."""

from __future__ import annotations

import pathlib
import os
import subprocess
import tempfile
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[2]
HARDWARE = ROOT / "firmware/components/desk_hardware"
NATIVE = ROOT / "tests/firmware_contract/native"
STUBS = NATIVE / "esp_motion_stubs"


class TestMotionAmbiguousStopNative(unittest.TestCase):
    def test_esp_uart_failure_stop_and_arbiter_contract(self) -> None:
        with tempfile.TemporaryDirectory(prefix="desk-motion-stop-") as temp:
            temp_path = pathlib.Path(temp)
            executable = temp_path / "motion_ambiguous_stop"
            motion_object = temp_path / "motion_esp.o"
            hardware_object = temp_path / "hardware_native.o"
            motion_source = pathlib.Path(
                os.environ.get(
                    "DESK_MOTION_SOURCE",
                    str(HARDWARE / "src/desk_watcher_motion.c"),
                )
            )
            common = [
                "gcc",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Wshadow",
                f"-I{STUBS}",
                f"-I{HARDWARE / 'include'}",
            ]
            compile_steps = [
                common + ["-DESP_PLATFORM", "-c", str(motion_source), "-o", str(motion_object)],
                common + ["-D_DEFAULT_SOURCE", "-c", str(HARDWARE / "src/desk_hardware.c"), "-o", str(hardware_object)],
                common + [
                    "-D_DEFAULT_SOURCE",
                    str(NATIVE / "test_motion_ambiguous_stop.c"),
                    str(hardware_object),
                    str(motion_object),
                    "-lm",
                    "-o",
                    str(executable),
                ],
            ]
            for command in compile_steps:
                compiled = subprocess.run(command, capture_output=True, text=True)
                self.assertEqual(
                    compiled.returncode,
                    0,
                    f"native ESP-path harness compile failed:\n{compiled.stderr}",
                )
            completed = subprocess.run(
                [str(executable)], capture_output=True, text=True, timeout=15
            )
            self.assertEqual(
                completed.returncode,
                0,
                f"native ESP-path harness failed:\n{completed.stdout}\n{completed.stderr}",
            )
            self.assertIn("ambiguous STOP native ESP transaction harness: PASS", completed.stdout)


if __name__ == "__main__":
    unittest.main()
