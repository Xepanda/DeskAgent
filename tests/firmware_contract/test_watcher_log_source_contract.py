"""Source contracts for the bounded CR-009 A1 event producer."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_log_source.c"
SOURCE_H = ROOT / "firmware/components/desk_hardware/include/desk_watcher_log_source.h"
CAMERA_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_camera.c"


class TestWatcherLogSourceContract(unittest.TestCase):
    def test_fixed_bounded_queue_and_single_allowed_event(self):
        header = SOURCE_H.read_text(encoding="utf-8")
        source = SOURCE_C.read_text(encoding="utf-8")
        self.assertIn("DESK_WATCHER_LOG_SOURCE_CAPACITY 16U", header)
        self.assertIn("DESK_WATCHER_LOG_SOURCE_MAX_RECORD_BYTES 192U", header)
        self.assertIn("DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY = 1", header)
        self.assertIn("_Static_assert(sizeof(desk_watcher_log_record_t)", source)
        self.assertIn("desk_watcher_log_record_t records[DESK_WATCHER_LOG_SOURCE_CAPACITY]", source)
        self.assertNotIn("malloc(", source)

    def test_drop_new_advances_boot_sequence_and_accumulates_only_queue_full_range(self):
        source = SOURCE_C.read_text(encoding="utf-8")
        publish = source.split("bool desk_watcher_log_source_publish_camera_capture_empty(", 1)[1]
        self.assertIn("const uint64_t sequence = s_source.next_sequence++", publish)
        self.assertIn("s_source.gap.first_dropped_sequence = sequence", publish)
        self.assertIn("s_source.gap.last_dropped_sequence = sequence", publish)
        self.assertIn("++s_source.gap.dropped_count", publish)
        self.assertNotIn("DISCONNECTED", source)

    def test_only_exhausted_empty_ptl_header_publishes_event(self):
        camera = CAMERA_C.read_text(encoding="utf-8")
        capture = camera.split("int desk_watcher_camera_capture(", 1)[1]
        empty_path = capture.split("if (header_or != 0) break;", 1)[1].split(
            "vTaskDelay(pdMS_TO_TICKS(CAM_PTL_EMPTY_BACKOFF_MS));", 1
        )[0]
        self.assertIn("if (empty_attempt + 1U >= CAM_PTL_EMPTY_RETRY_LIMIT)", empty_path)
        self.assertIn("desk_watcher_log_source_publish_camera_capture_empty()", empty_path)
        self.assertIn("camera_capture_release(DESK_WATCHER_CAMERA_ERR_EMPTY)", empty_path)

    def test_disconnect_clears_queue_and_gap_but_preserves_sequence(self):
        source = SOURCE_C.read_text(encoding="utf-8")
        disable = source.split("void desk_watcher_log_source_set_enabled(", 1)[1].split(
            "bool desk_watcher_log_source_is_enabled", 1
        )[0]
        self.assertIn("s_source.head = 0U", disable)
        self.assertIn("s_source.count = 0U", disable)
        self.assertIn("s_source.has_gap = false", disable)
        self.assertNotIn("s_source.next_sequence =", disable)


if __name__ == "__main__":
    unittest.main()
