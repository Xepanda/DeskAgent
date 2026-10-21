"""L2 source contracts for Watcher input readiness and event diagnostics."""

from pathlib import Path
import unittest
import re


ROOT = Path(__file__).resolve().parents[2]
INPUT_H = ROOT / "firmware/components/desk_hardware/include/desk_watcher_input.h"
INPUT_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_input.c"
CLI_C = ROOT / "firmware/components/desk_cli/src/desk_cli.c"


def function_body(source: str, name: str) -> str:
    match = re.search(rf"\b{re.escape(name)}\s*\([^;{{}}]*\)\s*\{{", source, re.S)
    if not match:
        return ""
    brace = match.end() - 1
    depth = 1
    for index in range(brace + 1, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[brace + 1:index]
    return ""


class TestWatcherInputStatusCliContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.header = INPUT_H.read_text(encoding="utf-8")
        cls.input_source = INPUT_C.read_text(encoding="utf-8")
        cls.cli_source = CLI_C.read_text(encoding="utf-8")

    def test_status_api_reports_each_input_channel_and_last_event(self) -> None:
        for token in ("DESK_WATCHER_INPUT_UNKNOWN", "DESK_WATCHER_INPUT_INIT",
                      "DESK_WATCHER_INPUT_READY", "DESK_WATCHER_INPUT_ERROR",
                      "desk_watcher_input_channel_status_t", "knob", "button", "touch",
                      "event_count", "error_code", "last_event_valid", "last_target",
                      "last_action", "desk_watcher_input_get_status"):
            self.assertIn(token, self.header)

    def test_init_errors_are_preserved_and_failed_channels_are_not_polled(self) -> None:
        init = function_body(self.input_source, "desk_watcher_input_init")
        worker = function_body(self.input_source, "watcher_input_worker_task")
        self.assertIn("gpio_config", self.input_source)
        self.assertIn("watcher_touch_i2c_init()", init)
        self.assertIn("watcher_btn_expander_init()", init)
        self.assertIn("DESK_WATCHER_INPUT_ERROR", init)
        self.assertIn("s_status.knob.state == DESK_WATCHER_INPUT_READY", worker)
        self.assertIn("s_status.button.state == DESK_WATCHER_INPUT_READY", worker)
        self.assertIn("s_status.touch.state == DESK_WATCHER_INPUT_READY", worker)

    def test_all_emitted_events_update_channel_and_last_event_counters(self) -> None:
        self.assertIn("watcher_input_record_event", self.input_source)
        worker = function_body(self.input_source, "watcher_input_worker_task")
        self.assertGreaterEqual(worker.count("watcher_input_record_event("), 4)

    def test_cli_registers_read_only_input_status_command(self) -> None:
        self.assertIn('{"input", "Read-only input driver status and event counters", cmd_input}', self.cli_source)
        cmd = function_body(self.cli_source, "cmd_input")
        for token in ("desk_watcher_input_get_status", "knob", "button", "touch",
                      "event_count", "error_code", "Last event"):
            self.assertIn(token, cmd)
        state_names = function_body(self.cli_source, "input_state_name")
        for token in ("INIT", "READY", "ERROR", "UNKNOWN"):
            self.assertIn(token, state_names)


if __name__ == "__main__":
    unittest.main()
