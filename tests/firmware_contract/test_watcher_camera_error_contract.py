"""Source contracts for preserving actionable Watcher camera errors."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CAMERA_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_camera.c"
HARDWARE_C = ROOT / "firmware/components/desk_hardware/src/desk_hardware.c"
CAMERA_H = ROOT / "firmware/components/desk_hardware/include/desk_watcher_camera.h"


class TestWatcherCameraErrorContract(unittest.TestCase):
    def test_hardware_wrapper_propagates_camera_driver_error(self):
        source = HARDWARE_C.read_text(encoding="utf-8")
        capture = source.split("int desk_hardware_camera_capture_jpeg(", 1)[1]
        capture = capture.split("\n}", 1)[0]
        self.assertIn("return drv_ret;", capture)
        driver_path = capture.split("if (s_board_driver_ops", 1)[1].split("\n    }", 1)[0]
        self.assertNotIn("return DESK_HW_ERR_UNAVAILABLE;", driver_path)

    def test_driver_names_sync_spi_protocol_and_crc_failures(self):
        header = CAMERA_H.read_text(encoding="utf-8")
        source = CAMERA_C.read_text(encoding="utf-8")
        for error_name in (
            "DESK_WATCHER_CAMERA_ERR_SYNC_IO",
            "DESK_WATCHER_CAMERA_ERR_PROTOCOL",
            "DESK_WATCHER_CAMERA_ERR_CRC",
            "DESK_WATCHER_CAMERA_ERR_SPI",
            "DESK_WATCHER_CAMERA_ERR_EMPTY",
        ):
            self.assertIn(error_name, header)
            self.assertIn(error_name, source)
        for diagnostic in (
            "SYNC read failed",
            "SYNC timeout",
            "PTL empty FIFO poll",
            "PTL header rejected",
            "CRC mismatch",
            "SPI PTL header transfer failed",
            "SPI PTL body transfer failed",
            "raw=%02x%02x%02x%02x%02x%02x%02x",
        ):
            self.assertIn(diagnostic, source)

    def test_zero_ptl_header_has_distinct_empty_error_and_spi_failures_keep_stage(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        capture = source.split("int desk_watcher_camera_capture(", 1)[1]
        self.assertIn("if (header_or != 0) break", capture)
        self.assertIn("CAM_PTL_EMPTY_RETRY_LIMIT", capture)
        self.assertIn("camera_capture_release(DESK_WATCHER_CAMERA_ERR_EMPTY)", capture)
        self.assertIn("camera_capture_release(DESK_WATCHER_CAMERA_ERR_SPI)", capture)
        self.assertGreaterEqual(capture.count("camera_capture_release(DESK_WATCHER_CAMERA_ERR_SPI)"), 2)

    def test_hx_uart_handshake_precedes_ready_and_capture_mode_is_restored(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        init = source.split("int desk_watcher_camera_init(", 1)[1].split("static uint32_t ptl_media_crc32", 1)[0]
        capture = source.split("int desk_watcher_camera_capture(", 1)[1].split("int desk_watcher_camera_deinit", 1)[0]
        self.assertIn("UART_NUM_1", source)
        self.assertIn("GPIO_NUM_17", source)
        self.assertIn("GPIO_NUM_18", source)
        self.assertIn("WATCHER_CAM_HX_UART_BAUD  921600", source)
        self.assertIn("uart_is_driver_installed(WATCHER_CAM_HX_UART)", source)
        self.assertLess(init.index("camera_control_init()"), init.index("s_camera_initialized = true"))
        self.assertIn("camera_control_mode(1U, 0U)", capture)
        self.assertIn("camera_control_mode(0U, 0U)", source)

    def test_ptl_resync_is_time_and_work_bounded_and_shared_spi_is_not_freed(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        self.assertIn("CAM_PTL_RESYNC_TIMEOUT_MS 50U", source)
        self.assertIn("CAM_PTL_RESYNC_MAX_CHUNKS 256U", source)
        self.assertIn("n < CAM_PTL_RESYNC_MAX_CHUNKS", source)
        self.assertIn("if (s_spi_bus_owned)", source)
        self.assertIn("s_spi_bus_owned = err == ESP_OK", source)

    def test_himax_power_and_sync_guards_match_upstream_ptl_timing(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        self.assertIn("CAM_HX_POWER_SETTLE_MS    20", source)
        self.assertIn("CAM_SYNC_GUARD_MS         2", source)
        self.assertIn("vTaskDelay(pdMS_TO_TICKS(CAM_HX_POWER_SETTLE_MS))", source)
        self.assertIn("vTaskDelay(pdMS_TO_TICKS(CAM_SYNC_GUARD_MS))", source)
        self.assertIn("vTaskDelay(pdMS_TO_TICKS(1))", source)

    def test_spi_transfer_limit_covers_maximum_ptl_body(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        self.assertIn("#define WATCHER_HX_PTL_BODY_CAPACITY (HX_MEDIA_HEADER_SIZE + WATCHER_HX_PTL_MAX_JPEG_SIZE)", source)
        self.assertIn("#define CAM_MAX_PACKET_SZ         WATCHER_HX_PTL_BODY_CAPACITY", source)
        self.assertIn(".max_transfer_sz = CAM_MAX_PACKET_SZ", source)


if __name__ == "__main__":
    unittest.main()
