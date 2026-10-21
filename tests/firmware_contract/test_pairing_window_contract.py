"""A1 pairing-window source contracts: explicit trigger, display-only secret, expiry."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLI = ROOT / "firmware/components/desk_cli/src/desk_cli.c"
TCLAW = ROOT / "firmware/components/desk_tclaw/src/desk_tclaw_bridge.c"


class TestPairingWindowContract(unittest.TestCase):
    def test_pairing_is_explicit_cli_action_with_runtime_code_and_ttl(self) -> None:
        source = CLI.read_text(encoding="utf-8")
        self.assertIn('{"pairing",', source)
        command = source.rsplit("static void cmd_pairing(int argc, char *argv[])", 1)[1].split(
            "static void cmd_wifi", 1
        )[0]
        for required in (
            'strcmp(argv[1], "open")',
            'strcmp(argv[1], "cancel")',
            "pairing_window_open()",
        ):
            self.assertIn(required, command)
        for line in command.splitlines():
            if "printf(" in line:
                self.assertNotIn("s_pairing_code", line)
        opening = source.split("static int pairing_window_open(void)", 1)[1].split(
            "static const desk_cli_command_t s_default_cmds", 1
        )[0]
        for required in (
            "esp_random()",
            "desk_sdk_bridge_set_pairing_code",
            "esp_timer_start_once",
            "PAIRING_WINDOW_TTL_US",
            "desk_tclaw_pairing_ui_set_code",
        ):
            self.assertIn(required, opening)

    def test_expiry_clears_bridge_and_ui_secret(self) -> None:
        source = CLI.read_text(encoding="utf-8")
        clear = source.split("static void pairing_window_clear_locked", 1)[1].split(
            "static void pairing_window_expired", 1
        )[0]
        self.assertIn("desk_sdk_bridge_set_pairing_code(NULL)", clear)
        self.assertIn("memset(s_pairing_code, 0, sizeof(s_pairing_code))", clear)
        self.assertIn("s_pairing_expiry_deadline_us = 0", clear)
        self.assertIn("desk_tclaw_pairing_ui_clear", clear)
        expiry = source.split("static void pairing_window_expired", 1)[1].split(
            "static int pairing_expiry_timer_ensure", 1
        )[0]
        self.assertIn("s_pairing_expiry_deadline_us != 0", expiry)
        self.assertIn("esp_timer_get_time() >= s_pairing_expiry_deadline_us", expiry)
        self.assertIn("pairing_window_clear_locked()", expiry)

    def test_pairing_display_survives_normal_ui_refresh_without_changing_voice_input(self) -> None:
        source = TCLAW.read_text(encoding="utf-8")
        render = source.split("static int watcher_ui_render", 1)[1].split(
            "static int desk_tclaw_update_screen", 1
        )[0]
        self.assertIn("s_pairing_ui_active", render)
        self.assertIn('snprintf(status, sizeof(status), "PAIR %s"', render)
        self.assertIn("desk_tclaw_pairing_ui_set_code", source)
        self.assertIn("s_ui_overlay_active || s_pairing_ui_active", source)
        input_cb = source.split("static void watcher_ui_input_event", 1)[1].split(
            "static int desk_tclaw_update_ui_screen", 1
        )[0]
        self.assertIn("AT_KEY_TALK", input_cb)
        self.assertIn("AT_LONG_KEY_TALK", input_cb)
        self.assertIn("DESK_TOUCH_ACTION_SCROLL_UP", input_cb)
        self.assertIn("DESK_TOUCH_ACTION_SCROLL_DOWN", input_cb)


if __name__ == "__main__":
    unittest.main()
