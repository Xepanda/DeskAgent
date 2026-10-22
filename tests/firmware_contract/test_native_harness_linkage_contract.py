"""Keep native test harness source lists complete for referenced firmware APIs."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
LOG_SOURCE = "firmware/components/desk_hardware/src/desk_watcher_log_source.c"
HARNESS_TESTS = (
    ROOT / "tests/e2e/test_g1_official_sdk_and_tuya_interop.py",
    ROOT / "tests/firmware_contract/test_safe_startup_contract.py",
)


class TestNativeHarnessLinkageContract(unittest.TestCase):
    def test_harnesses_link_watcher_log_source_implementation(self):
        for harness in HARNESS_TESTS:
            with self.subTest(harness=harness.relative_to(ROOT)):
                self.assertTrue(
                    any(
                        line.strip().startswith('f"{repo_root}/')
                        and LOG_SOURCE in line
                        for line in harness.read_text(encoding="utf-8").splitlines()
                    ),
                    f"{harness.relative_to(ROOT)} must compile {LOG_SOURCE}",
                )


if __name__ == "__main__":
    unittest.main()
