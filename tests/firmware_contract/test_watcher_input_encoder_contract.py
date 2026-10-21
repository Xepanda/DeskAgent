"""Contracts for translating rotary encoder detents into UI scroll events."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
INPUT_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_input.c"
BRIDGE_C = ROOT / "firmware/components/desk_tclaw/src/desk_tclaw_bridge.c"


class TestWatcherInputEncoderContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.input_source = INPUT_C.read_text(encoding="utf-8")
        cls.bridge_source = BRIDGE_C.read_text(encoding="utf-8")

    def test_rotary_decoder_emits_one_scroll_per_complete_detent(self) -> None:
        self.assertIn("WATCHER_KNOB_TRANSITIONS_PER_DETENT", self.input_source)
        self.assertIn("watcher_knob_decode_step", self.input_source)
        self.assertRegex(self.input_source, r"quarter_steps\s*>=\s*WATCHER_KNOB_TRANSITIONS_PER_DETENT")
        self.assertRegex(self.input_source, r"quarter_steps\s*<=\s*-WATCHER_KNOB_TRANSITIONS_PER_DETENT")
        worker = self.input_source.split("static void watcher_input_worker_task", 1)[1]
        self.assertIn("watcher_knob_decode_step", worker)
        self.assertIn("DESK_TOUCH_TARGET_ROLLER", worker)

    def test_knob_press_duration_still_selects_existing_short_and_long_actions(self) -> None:
        self.assertRegex(self.input_source, r"duration_ms\s*>=\s*500")
        self.assertIn("DESK_TOUCH_ACTION_LONG_PRESS", self.input_source)
        self.assertIn("DESK_TOUCH_ACTION_SHORT_PRESS", self.input_source)
        self.assertIn("AT_KEY_TALK", self.bridge_source)
        self.assertIn("AT_LONG_KEY_TALK", self.bridge_source)


if __name__ == "__main__":
    unittest.main()
