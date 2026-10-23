"""Native concurrency and allowlist checks for the CR-032 TAL text observer."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "firmware/components/desk_hardware/src/desk_watcher_log_source.c"
HARNESS = ROOT / "tests/firmware_contract/native/test_watcher_tal_log_observer.c"


class TestWatcherTalLogObserverNative(unittest.TestCase):
    def test_tal_lines_never_become_network_events_and_concurrent_calls_are_safe(self):
        compiler = shutil.which("gcc") or shutil.which("cc")
        if compiler is None:
            self.skipTest("native C compiler unavailable")

        with tempfile.TemporaryDirectory() as temp_dir:
            executable = Path(temp_dir) / "watcher_tal_log_observer_test"
            compile_result = subprocess.run(
                [
                    compiler,
                    "-std=c11",
                    "-Wall",
                    "-Wextra",
                    "-Werror",
                    "-pthread",
                    f"-I{ROOT / 'firmware/components/desk_hardware/include'}",
                    str(HARNESS),
                    str(SOURCE),
                    "-o",
                    str(executable),
                ],
                capture_output=True,
                text=True,
                check=False,
            )
            self.assertEqual(compile_result.returncode, 0, compile_result.stderr)
            run_result = subprocess.run(
                [str(executable)], capture_output=True, text=True, timeout=10, check=False
            )
            self.assertEqual(run_result.returncode, 0, run_result.stderr)


if __name__ == "__main__":
    unittest.main()
