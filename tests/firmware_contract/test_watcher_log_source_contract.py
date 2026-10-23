"""Source contracts for the bounded CR-009 A1 event producer."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_log_source.c"
SOURCE_H = ROOT / "firmware/components/desk_hardware/include/desk_watcher_log_source.h"
CAMERA_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_camera.c"
APP_MAIN_C = ROOT / "firmware/main/src/app_main.c"
HARDWARE_C = ROOT / "firmware/components/desk_hardware/src/desk_hardware.c"
HARDWARE_H = ROOT / "firmware/components/desk_hardware/include/desk_hardware.h"
MOTION_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_motion.c"


class TestWatcherLogSourceContract(unittest.TestCase):
    def test_fixed_bounded_queue_and_closed_event_allowlist(self):
        header = SOURCE_H.read_text(encoding="utf-8")
        source = SOURCE_C.read_text(encoding="utf-8")
        self.assertIn("DESK_WATCHER_LOG_SOURCE_CAPACITY 16U", header)
        self.assertIn("DESK_WATCHER_LOG_SOURCE_MAX_RECORD_BYTES 192U", header)
        self.assertIn("DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY = 1", header)
        for event in (
            "DESK_WATCHER_LOG_EVENT_AUDIO_DRIVER_STATE_SNAPSHOT",
            "DESK_WATCHER_LOG_EVENT_TUYA_MQTT_STATE",
            "DESK_WATCHER_LOG_EVENT_STM32_ACK_TIMEOUT",
        ):
            self.assertIn(event, header)
        self.assertIn("DESK_WATCHER_LOG_SOURCE_MAX_EVENTS_PER_MINUTE 30U", header)
        self.assertIn("DESK_WATCHER_LOG_SOURCE_RATE_WINDOW_MS 60000U", header)
        self.assertIn("desk_watcher_log_component_t component", header)
        self.assertIn("desk_watcher_log_level_t level", header)
        self.assertIn("desk_watcher_log_state_t state", header)
        self.assertIn("_Static_assert(sizeof(desk_watcher_log_record_t)", source)
        self.assertIn("desk_watcher_log_record_t records[DESK_WATCHER_LOG_SOURCE_CAPACITY]", source)
        self.assertNotIn("malloc(", source)

    def test_drop_new_advances_boot_sequence_and_accumulates_only_queue_full_range(self):
        source = SOURCE_C.read_text(encoding="utf-8")
        publish = source.split("static bool source_publish_locked(", 1)[1].split(
            "bool desk_watcher_log_source_publish_camera_capture_empty(", 1
        )[0]
        self.assertIn("s_source.rate_count >= DESK_WATCHER_LOG_SOURCE_MAX_EVENTS_PER_MINUTE", publish)
        self.assertLess(publish.index("s_source.rate_count >="),
                        publish.index("const uint64_t sequence = s_source.next_sequence++"))
        self.assertIn("s_source.gap.first_dropped_sequence = sequence", publish)
        self.assertIn("s_source.gap.last_dropped_sequence = sequence", publish)
        self.assertIn("++s_source.gap.dropped_count", publish)
        self.assertLess(publish.index("const uint64_t sequence = s_source.next_sequence++"),
                        publish.index("if (s_source.count < DESK_WATCHER_LOG_SOURCE_CAPACITY)"))

    def test_only_exhausted_empty_ptl_header_publishes_event(self):
        camera = CAMERA_C.read_text(encoding="utf-8")
        capture = camera.split("int desk_watcher_camera_capture(", 1)[1]
        empty_path = capture.split("if (header_or != 0)", 1)[1].split(
            "int ptl_rc = desk_watcher_camera_parse_ptl_header", 1
        )[0]
        self.assertIn("camera_capture_deadline_us", empty_path)
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
        for token in ("audio_snapshot_published = false", "has_mqtt_state = false",
                      "has_stm32_timeout_time = false", "rate_count = 0U"):
            self.assertIn(token, disable)

    def test_tal_text_is_observed_only_after_the_existing_stdout_sink(self):
        header = SOURCE_H.read_text(encoding="utf-8")
        source = SOURCE_C.read_text(encoding="utf-8")
        app_main = APP_MAIN_C.read_text(encoding="utf-8")
        self.assertIn("desk_watcher_log_source_observe_tal_line", header)
        output = app_main.split("static void desk_tuya_log_output(", 1)[1].split("\n}", 1)[0]
        sensitive_check = output.index("desk_log_line_has_sensitive_marker(line)")
        sensitive_return = output.index("return;", sensitive_check)
        stdout_sink = output.index("fputs(line, stdout)")
        observer_call = output.index("desk_watcher_log_source_observe_tal_line(line)")
        self.assertLess(sensitive_check, sensitive_return)
        self.assertLess(sensitive_return, stdout_sink)
        self.assertLess(stdout_sink, observer_call)
        observer = source.split("void desk_watcher_log_source_observe_tal_line(", 1)[1].split(
            "\n}", 1
        )[0]
        for forbidden in ("publish_camera_capture_empty", "malloc(", "ESP_LOG", "tal_log"):
            self.assertNotIn(forbidden, observer)

    def test_allowed_diagnostic_events_have_closed_component_level_and_state_enums(self):
        header = SOURCE_H.read_text(encoding="utf-8")
        for event in (
            "DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY",
            "DESK_WATCHER_LOG_EVENT_AUDIO_DRIVER_STATE_SNAPSHOT",
            "DESK_WATCHER_LOG_EVENT_TUYA_MQTT_STATE",
            "DESK_WATCHER_LOG_EVENT_STM32_ACK_TIMEOUT",
        ):
            self.assertIn(event, header)
        for token in (
            "DESK_WATCHER_LOG_COMPONENT_CAMERA",
            "DESK_WATCHER_LOG_COMPONENT_AUDIO",
            "DESK_WATCHER_LOG_COMPONENT_NETWORK",
            "DESK_WATCHER_LOG_COMPONENT_STM32",
            "DESK_WATCHER_LOG_LEVEL_WARN",
            "DESK_WATCHER_LOG_LEVEL_ERROR",
            "DESK_WATCHER_LOG_STATE_READY",
            "DESK_WATCHER_LOG_STATE_NOT_READY",
            "DESK_WATCHER_LOG_STATE_CONNECTED",
            "DESK_WATCHER_LOG_STATE_DISCONNECTED",
        ):
            self.assertIn(token, header)
        for producer in (
            "desk_watcher_log_source_publish_audio_driver_state",
            "desk_watcher_log_source_publish_tuya_mqtt_state",
            "desk_watcher_log_source_publish_stm32_ack_timeout",
        ):
            self.assertIn(producer, header)

    def test_mqtt_events_are_emitted_only_from_the_two_real_tuya_callback_cases(self):
        app_main = APP_MAIN_C.read_text(encoding="utf-8")
        handler = app_main.split("static void tuya_event_handler_on(", 1)[1].split(
            "static bool tuya_network_check_cb", 1
        )[0]
        connected = handler.split("case TUYA_EVENT_MQTT_CONNECTED:", 1)[1].split(
            "case TUYA_EVENT_MQTT_DISCONNECT:", 1
        )[0]
        disconnected = handler.split("case TUYA_EVENT_MQTT_DISCONNECT:", 1)[1].split(
            "case TUYA_EVENT_RESET:", 1
        )[0]
        self.assertIn("desk_watcher_log_source_publish_tuya_mqtt_state(true)", connected)
        self.assertIn("desk_watcher_log_source_publish_tuya_mqtt_state(false)", disconnected)
        self.assertNotIn("desk_watcher_log_source_publish_tuya_mqtt_state", handler.split(
            "case TUYA_EVENT_BIND_START:", 1
        )[1].split("case TUYA_EVENT_MQTT_CONNECTED:", 1)[0])

    def test_audio_snapshot_reads_real_status_only_after_source_is_enabled(self):
        hardware = HARDWARE_C.read_text(encoding="utf-8")
        api = HARDWARE_H.read_text(encoding="utf-8")
        self.assertIn("desk_hardware_publish_audio_driver_state_snapshot", api)
        body = hardware.split("bool desk_hardware_publish_audio_driver_state_snapshot(", 1)[1].split(
            "\n}", 1
        )[0]
        self.assertLess(body.index("desk_watcher_log_source_is_enabled()"),
                        body.index("desk_hardware_audio_get_status(&status)"))
        self.assertIn("desk_watcher_log_source_publish_audio_driver_state(status.ready)", body)

    def test_stm32_ack_timeout_sources_are_deadline_expiry_paths_only(self):
        hardware = HARDWARE_C.read_text(encoding="utf-8")
        motion = MOTION_C.read_text(encoding="utf-8")
        self.assertIn("MCU_ACK_TIMEOUT_RATE_LIMIT_MS 60000U", hardware)
        self.assertIn("desk_hardware_record_mcu_ack_timeout()", hardware)
        timeout_path = hardware.split("static int mcu_execute_transaction(", 1)[1].split(
            "static int default_driver_motion_move", 1
        )[0]
        self.assertIn("desk_hardware_record_mcu_ack_timeout()", timeout_path)
        physical_timeout = motion.split("static int motion_send_and_wait_ack(", 1)[1].split(
            "int desk_watcher_motion_move(", 1
        )[0]
        self.assertIn("desk_hardware_record_mcu_ack_timeout()", physical_timeout)


if __name__ == "__main__":
    unittest.main()
