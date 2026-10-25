"""A7 source contracts for the standalone host CMake verification step."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "tools/verification/verify_all.sh"


class TestVerifyAllHostCmakeContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SCRIPT.read_text(encoding="utf-8")
        match = re.search(
            r'(?ms)^# -{10,}\n# 2\. Level 2:.*?(?=^# -{10,}\n# 3\.)',
            cls.source,
        )
        cls.step2 = match.group(0) if match else ""

    def test_step2_stops_with_readable_error_when_cmake_is_missing(self) -> None:
        self.assertTrue(self.step2, "Step 2 block should be present")
        self.assertRegex(self.step2, r"command -v cmake")
        self.assertRegex(self.step2, r"(?i)(cmake.*(not installed|not found|missing)|install.*cmake)")
        self.assertIn("step_fail", self.step2)

    def test_step2_uses_unique_host_build_dir_outside_firmware_build(self) -> None:
        self.assertTrue(self.step2, "Step 2 block should be present")
        self.assertRegex(self.step2, r"mktemp\s+-d")
        self.assertRegex(self.step2, r'HOST_BUILD_DIR=.*mktemp -d "/tmp/desk-agent-host-cmake\.XXXXXX"')
        self.assertRegex(self.step2, r"-B\s+\"?\$\{?HOST_BUILD_DIR")
        self.assertNotRegex(self.step2, r"-B\s+firmware/build(?:\s|$)")
        self.assertNotIn("--clean-first", self.step2)

    def test_step2_explicitly_unsets_idf_path_for_standalone_host_build(self) -> None:
        self.assertTrue(self.step2, "Step 2 block should be present")
        self.assertRegex(self.step2, r"(?m)^unset\s+IDF_PATH\s*$")
        self.assertIn("IDF_PATH", self.step2)


if __name__ == "__main__":
    unittest.main()
