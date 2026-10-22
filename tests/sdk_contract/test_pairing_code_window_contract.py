"""Contracts for the ephemeral, concurrency-safe UDP pairing-code window."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c"
HEADER = ROOT / "firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h"


class PairingCodeWindowContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SOURCE.read_text(encoding="utf-8")
        cls.header = HEADER.read_text(encoding="utf-8")

    def test_setter_status_and_pair_consume_share_mutex(self) -> None:
        self.assertIn("s_pairing_code_mutex", self.source)
        setter = self.source[self.source.index("int desk_sdk_bridge_set_pairing_code"):]
        setter = setter[:setter.index("bool desk_sdk_bridge_is_pairing_code_configured")]
        status = self.source[self.source.index("bool desk_sdk_bridge_is_pairing_code_configured"):]
        status = status[:status.index("#ifdef ESP_PLATFORM")]
        self.assertIn("pthread_mutex_lock(&s_pairing_code_mutex)", setter)
        self.assertIn("pthread_mutex_unlock(&s_pairing_code_mutex)", setter)
        self.assertIn("pthread_mutex_lock(&s_pairing_code_mutex)", status)
        self.assertIn("pthread_mutex_unlock(&s_pairing_code_mutex)", status)
        public = self.source[self.source.index("int desk_sdk_bridge_process_pairing_udp("):]
        public = public[:public.index("/* Command processing */")]
        self.assertIn("pthread_mutex_lock(&s_pairing_code_mutex)", public)
        self.assertIn("desk_sdk_bridge_process_pairing_udp_locked", public)
        self.assertIn("pthread_mutex_unlock(&s_pairing_code_mutex)", public)

    def test_five_bad_attempts_clear_the_window(self) -> None:
        self.assertIn("DESK_SDK_PAIRING_MAX_FAILED_ATTEMPTS 5", self.header)
        self.assertIn("s_pairing_failed_attempts", self.source)
        self.assertIn("desk_sdk_pairing_code_failed_attempt_locked", self.source)
        helper = self.source[self.source.index("static bool desk_sdk_pairing_code_failed_attempt_locked"):]
        helper = helper[:helper.index("\n}") + 2]
        self.assertRegex(helper, r"(?:\+\+\s*s_pairing_failed_attempts|s_pairing_failed_attempts\s*\+=\s*1|s_pairing_failed_attempts\+\+)")
        self.assertIn(">= DESK_SDK_PAIRING_MAX_FAILED_ATTEMPTS", helper)
        self.assertIn("desk_sdk_pairing_code_clear_locked", helper)
        self.assertRegex(self.source, r"if\s*\(!s_pairing_code_configured\)")

    def test_success_clears_code_and_logs_only_reason_and_request_id(self) -> None:
        process = self.source[self.source.index("static int desk_sdk_bridge_process_pairing_udp_locked"):]
        process = process[:process.index("/* Command processing */")]
        self.assertIn("desk_sdk_pairing_code_clear_locked();", process)
        self.assertIn("pair.accept issued", process)
        self.assertNotIn("pairing_code=[redacted]", process)
        self.assertNotIn("session_token=%s", process)
        self.assertNotIn("redacted_token", process)
        for line in process.splitlines():
            if "printf(" in line or "request_id=%s" in line:
                self.assertNotRegex(line, r"pairing_code|session_token|token=%s")

    def test_consumption_and_reset_ui_callbacks_follow_mutex_unlock(self) -> None:
        public = self.source[self.source.index("int desk_sdk_bridge_process_pairing_udp("):]
        public = public[:public.index("/* Command processing */")]
        self.assertLess(public.index("pthread_mutex_unlock(&s_pairing_code_mutex)"),
                        public.index("desk_sdk_pairing_ui_notify_consumed(notify_pairing_consumed)"))
        reset = self.source[self.source.index("void desk_sdk_bridge_reset(void)"):]
        reset = reset[:reset.index("bool desk_sdk_bridge_is_connected(void)")]
        self.assertLess(reset.index("pthread_mutex_unlock(&s_pairing_code_mutex)"),
                        reset.index("desk_tclaw_pairing_ui_on_sdk_session_reset()"))
        cmake = (ROOT / "firmware/components/desk_sdk_bridge/CMakeLists.txt").read_text(encoding="utf-8")
        self.assertRegex(cmake, r"REQUIRES[\s\S]*desk_hardware[\s\S]*desk_tclaw")

    def test_pairing_code_storage_is_ephemeral_and_bounded(self) -> None:
        self.assertIn("char s_expected_pairing_code[8]", self.source)
        self.assertNotRegex(self.source, r"s_expected_pairing_code\s*=\s*\"\d{6}\"")
        self.assertNotRegex(self.source, r"desk_sdk_bridge_set_pairing_code\(\s*\"\d{6}\"")
        self.assertIn("memset(s_expected_pairing_code, 0, sizeof(s_expected_pairing_code))", self.source)


if __name__ == "__main__":
    unittest.main()
