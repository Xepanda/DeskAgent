from __future__ import annotations

import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
BRIDGE = ROOT / "firmware/components/desk_tclaw/src/desk_tclaw_bridge.c"
HARDWARE_H = ROOT / "firmware/components/desk_hardware/include/desk_hardware.h"
HARDWARE_C = ROOT / "firmware/components/desk_hardware/src/desk_hardware.c"
BOARD_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_board.c"


def function_body(source: str, name: str) -> str:
    match = re.search(rf"\b{name}\s*\([^;]*?\)\s*\{{", source, re.S)
    if not match:
        return ""
    start = match.end()
    depth = 1
    for index in range(start, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start:index]
    return ""


class TestWatcherUiInput(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.bridge = BRIDGE.read_text(encoding="utf-8")
        cls.hardware_h = HARDWARE_H.read_text(encoding="utf-8")
        cls.hardware_c = HARDWARE_C.read_text(encoding="utf-8")
        cls.board_c = BOARD_C.read_text(encoding="utf-8")

    def test_tclaw_init_registers_one_hardware_input_handler(self) -> None:
        init = function_body(self.bridge, "desk_tclaw_init")
        self.assertIn("desk_hardware_register_input_cb", init)
        self.assertIn("watcher_ui_input_event", init)

    def test_wheel_focuses_four_read_only_pages_and_touch_opens_or_returns_home(self) -> None:
        handler = function_body(self.bridge, "watcher_ui_input_event")
        for token in ("DESK_TOUCH_TARGET_ROLLER", "DESK_TOUCH_ACTION_SCROLL_UP",
                      "DESK_TOUCH_ACTION_SCROLL_DOWN", "DESK_TOUCH_TARGET_SCREENTOUCH",
                      "DESK_TOUCH_ACTION_SHORT_PRESS"):
            self.assertIn(token, handler)
        for page in ("HOME", "AUDIO", "CONNECTION", "DEVICE"):
            self.assertIn(f"WATCHER_UI_PAGE_{page}", self.bridge)
        self.assertIn("desk_hardware_display_set_expression", self.bridge)
        self.assertIn("desk_hardware_audio_get_status", self.bridge)
        self.assertIn("UNAVAILABLE", self.bridge)

    def test_rotary_button_calls_only_the_existing_tuya_talk_alert_api(self) -> None:
        handler = function_body(self.bridge, "watcher_ui_input_event")
        self.assertIn("action == DESK_TOUCH_ACTION_SHORT_PRESS ? AT_KEY_TALK : AT_LONG_KEY_TALK", handler)
        self.assertIn("ai_agent_cloud_alert(alert)", handler)
        self.assertIn("ai_agent_cloud_alert(action == DESK_TOUCH_ACTION_SHORT_PRESS ? AT_KEY_TALK : AT_LONG_KEY_TALK)", handler)
        self.assertNotRegex(handler, r"desk_hardware_audio_record_(?:start|stop)|desk_tclaw_tuya_session_(?:start|stop)")

    def test_audio_status_getter_exposes_real_driver_counters_and_unavailable_fallback(self) -> None:
        self.assertIn("desk_hardware_audio_get_status", self.hardware_h)
        body = function_body(self.hardware_c, "desk_hardware_audio_get_status")
        self.assertIn("desk_watcher_audio_status", body)
        self.assertIn("DESK_HW_ERR_UNAVAILABLE", body)

    def test_overlay_keeps_the_current_expression_and_does_not_draw_home_first(self) -> None:
        render = function_body(self.bridge, "watcher_ui_render")
        self.assertIn('desk_hardware_display_set_expression(s_ui_home_preset, status)', render)
        update = function_body(self.bridge, "desk_tclaw_update_screen")
        # Keep the latest home expression/status in memory while pairing or
        # the read-only overlay owns the display; render that UI before the
        # default home expression can be drawn directly.
        self.assertLess(update.index("if (s_ui_overlay_active || s_pairing_ui_active)"),
                        update.index("desk_hardware_display_set_expression(safe_preset, safe_status)"))
        self.assertLess(update.index("snprintf(s_ui_home_preset"),
                        update.index("return watcher_ui_render()"))

    def test_connection_page_labels_its_local_value_as_tclaw_shim_state(self) -> None:
        render = function_body(self.bridge, "watcher_ui_render")
        self.assertIn("TCLAW SHIM", render)
        self.assertNotIn("VOICE CONNECTED", render)
        self.assertNotIn("VOICE DISCONNECTED", render)

    def test_board_renderer_is_not_modified_for_status_pages(self) -> None:
        # UI pages must use the existing expression/status text interface.
        self.assertIn("watcher_display_expression", self.board_c)
        self.assertNotIn("WATCHER_UI_PAGE_", self.board_c)


if __name__ == "__main__":
    unittest.main()
