"""Static contract for Windows scheduled-task recovery policy verification.

This test reads the registration script only; it never invokes Task Scheduler.
"""

from __future__ import annotations

import pathlib
import re
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
REGISTER_SCRIPT = REPO_ROOT / "deploy" / "windows" / "register-autostart.ps1"


class TestWindowsAutostartRecoveryContract(unittest.TestCase):
    def test_host_registration_reads_back_and_checks_finite_recovery_policy(self) -> None:
        script = REGISTER_SCRIPT.read_text(encoding="utf-8")
        host_branch = script.split("if ($IncludeHost) {", maxsplit=1)[1]

        readback = re.search(
            r"Get-ScheduledTask\s+-TaskName\s+\$hostTaskName\s+-ErrorAction\s+Stop",
            host_branch,
        )
        self.assertIsNotNone(
            readback,
            "Host task registration must read the saved task back from Task Scheduler",
        )
        self.assertRegex(
            host_branch,
            r"\.Settings\.RestartCount",
            "Host task verification must inspect the saved RestartCount",
        )
        self.assertRegex(
            host_branch,
            r"\.Settings\.RestartInterval",
            "Host task verification must inspect the saved RestartInterval",
        )
        self.assertRegex(
            host_branch,
            r"throw\s+[\"'].*(?:recovery|restart).*policy",
            "A mismatched Host recovery policy must throw instead of reporting success",
        )
        self.assertRegex(host_branch, r"-ne\s+\$restartCount")
        self.assertRegex(host_branch, r"-ne\s+\$restartInterval")

    def test_host_policy_readback_and_comparison_precede_success(self) -> None:
        script = REGISTER_SCRIPT.read_text(encoding="utf-8")
        host_branch = script.split("if ($IncludeHost) {", maxsplit=1)[1]

        condition = re.search(
            r"if\s*\(\s*\[int\]\$registeredHost\.Settings\.RestartCount\s+-ne\s+\$restartCount\s+-or\s+"
            r"\$registeredHostRestartInterval\s+-ne\s+\$restartInterval\s*\)\s*\{\s*throw",
            host_branch,
            flags=re.DOTALL,
        )
        self.assertIsNotNone(
            condition,
            "Host recovery mismatches must compare both saved values to the requested policy and throw",
        )

        register_at = host_branch.index("Register-ScheduledTask -TaskName $hostTaskName")
        readback_at = host_branch.index(
            "Get-ScheduledTask -TaskName $hostTaskName -ErrorAction Stop"
        )
        success_at = host_branch.index("[SUCCESS] Registered '$hostTaskName'")
        assert condition is not None
        self.assertLess(register_at, readback_at)
        self.assertLess(readback_at, condition.start())
        self.assertLess(condition.start(), success_at)

    def test_companion_error_does_not_claim_task_was_not_registered(self) -> None:
        script = REGISTER_SCRIPT.read_text(encoding="utf-8")
        companion_catch = script.split("} catch {", maxsplit=1)[1].split(
            "# 4. Optionally register Host service", maxsplit=1
        )[0]

        self.assertNotIn("No Companion autostart was registered", companion_catch)
        self.assertRegex(companion_catch, r"(?i)may already exist")
        self.assertRegex(companion_catch, r"(?i)not verified|could not be verified")


if __name__ == "__main__":
    unittest.main()
