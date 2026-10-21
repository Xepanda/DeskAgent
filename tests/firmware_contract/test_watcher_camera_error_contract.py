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
        ):
            self.assertIn(error_name, header)
            self.assertIn(error_name, source)
        for diagnostic in ("SYNC read failed", "PTL header rejected", "CRC mismatch", "SPI"):
            self.assertIn(diagnostic, source)

    def test_himax_power_and_sync_guards_match_upstream_ptl_timing(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        self.assertIn("CAM_HX_POWER_SETTLE_MS    20", source)
        self.assertIn("CAM_SYNC_GUARD_MS         2", source)
        self.assertIn("vTaskDelay(pdMS_TO_TICKS(CAM_HX_POWER_SETTLE_MS))", source)
        self.assertIn("vTaskDelay(pdMS_TO_TICKS(CAM_SYNC_GUARD_MS))", source)
        self.assertIn("vTaskDelay(pdMS_TO_TICKS(1))", source)


if __name__ == "__main__":
    unittest.main()
