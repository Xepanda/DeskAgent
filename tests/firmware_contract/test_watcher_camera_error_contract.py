"""Source contracts for preserving actionable Watcher camera errors."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[2]
CAMERA_C = ROOT / "firmware/components/desk_hardware/src/desk_watcher_camera.c"
HARDWARE_C = ROOT / "firmware/components/desk_hardware/src/desk_hardware.c"
CAMERA_H = ROOT / "firmware/components/desk_hardware/include/desk_watcher_camera.h"
CAMERA_BUS_H = ROOT / "firmware/components/desk_hardware/include/desk_watcher_camera_bus.h"
TCLAW_BRIDGE_C = ROOT / "firmware/components/desk_tclaw/src/desk_tclaw_bridge.c"


class TestWatcherCameraErrorContract(unittest.TestCase):
    def test_bus_acquire_uses_remaining_capture_deadline_ticks(self):
        source_text = CAMERA_C.read_text(encoding="utf-8")
        bus_header = CAMERA_BUS_H.read_text(encoding="utf-8")
        capture = source_text.split("int desk_watcher_camera_capture(", 1)[1]

        self.assertIn("desk_watcher_camera_bus_deadline_ticks", bus_header)
        self.assertGreaterEqual(capture.count("camera_capture_acquire_bus(camera_capture_deadline_us)"), 2)
        self.assertEqual(capture.count("return camera_capture_release(acquire_rc);"), 2)
        self.assertNotIn("spi_device_acquire_bus(s_spi_dev, portMAX_DELAY)", capture)
        self.assertIn("desk_watcher_camera_bus_acquire_error", bus_header)
        acquire = source_text.split("static int camera_capture_acquire_bus(", 1)[1].split("\n}", 1)[0]
        self.assertIn("desk_watcher_camera_bus_acquire_error(err, ESP_ERR_TIMEOUT", acquire)
        self.assertLess(acquire.index("if (err != ESP_OK)"),
                        acquire.index("desk_watcher_camera_bus_set_acquired(&s_capture_bus_acquired, true)"))
        self.assertIn("DESK_HW_ERR_TIMEOUT", acquire)
        self.assertIn("DESK_WATCHER_CAMERA_ERR_SPI", acquire)
        self.assertIn("if (esp_timer_get_time() >= deadline_us)", acquire)
        self.assertIn("camera_capture_release_bus()", acquire)

        compiler = shutil.which("gcc")
        if compiler is None:
            self.skipTest("native gcc is unavailable; deadline C harness is ready for host CI")

        harness = r'''#include <assert.h>
#include <stdint.h>
#include "desk_watcher_camera_bus.h"

int main(void) {
    uint64_t ticks = 999;
    assert(desk_watcher_camera_bus_deadline_ticks(20500, 10000, 1000, 500, &ticks));
    assert(ticks == 11); /* ceil 10.5ms to the next scheduler tick */
    ticks = 999;
    assert(!desk_watcher_camera_bus_deadline_ticks(10000, 10000, 1000, 500, &ticks));
    assert(ticks == 0); /* expired deadlines cannot wait */
    ticks = 0;
    assert(desk_watcher_camera_bus_deadline_ticks(1000000, 0, 1000, 50, &ticks));
    assert(ticks == 50); /* always cap at a finite acquire timeout */
    assert(desk_watcher_camera_bus_acquire_error(-7, -7, -3, -24) == -3);
    assert(desk_watcher_camera_bus_acquire_error(-8, -7, -3, -24) == -24);
    return 0;
}
'''
        with tempfile.TemporaryDirectory(prefix="desk-camera-deadline-") as temp_dir:
            source = Path(temp_dir) / "camera_deadline_test.c"
            executable = Path(temp_dir) / "camera_deadline_test.exe"
            source.write_text(harness, encoding="utf-8")
            built = subprocess.run(
                [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                 f"-I{CAMERA_BUS_H.parent}", str(source), "-o", str(executable)],
                capture_output=True, text=True,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            run = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_spi_bus_ownership_releases_once_after_reacquire_deadline(self):
        """Exercise the same ownership transition used by capture cleanup."""
        harness = r'''#include <assert.h>
#include <stdbool.h>
#include "desk_watcher_camera_bus.h"

static unsigned release_calls;
static void release_bus(void) { ++release_calls; }
static void cleanup(bool *owned) {
    if (desk_watcher_camera_bus_take_release(owned)) release_bus();
}

int main(void) {
    bool owned = false;
    desk_watcher_camera_bus_set_acquired(&owned, true);
    cleanup(&owned); /* empty header closed before retry */
    assert(!owned && release_calls == 1);
    desk_watcher_camera_bus_set_acquired(&owned, true); /* retry reacquired */
    cleanup(&owned); /* capture deadline expires before next header */
    assert(!owned && release_calls == 2);
    cleanup(&owned);
    assert(release_calls == 2); /* no double release */
    desk_watcher_camera_bus_set_acquired(&owned, false); /* failed acquire */
    cleanup(&owned);
    assert(release_calls == 2);
    return 0;
}
'''
        source_text = CAMERA_C.read_text(encoding="utf-8")
        capture = source_text.split("int desk_watcher_camera_capture(", 1)[1]
        self.assertIn("desk_watcher_camera_bus_set_acquired(&s_capture_bus_acquired, true)", source_text)
        self.assertIn("camera_capture_release_bus()", capture)
        empty_deadline = capture.split("if (!header_received)", 1)[1].split("int ptl_rc", 1)[0]
        self.assertIn("camera_capture_release(DESK_WATCHER_CAMERA_ERR_EMPTY)", empty_deadline)
        release = source_text.split("static int camera_capture_release(", 1)[1].split("\n}", 1)[0]
        self.assertIn("camera_capture_release_bus()", release)
        bus_release = source_text.split("static void camera_capture_release_bus(", 1)[1].split("\n}", 1)[0]
        self.assertIn("desk_watcher_camera_bus_take_release(&s_capture_bus_acquired)", bus_release)
        compiler = shutil.which("gcc")
        if compiler is None:
            self.skipTest("native gcc is unavailable; C harness is ready for host CI")

        with tempfile.TemporaryDirectory(prefix="desk-camera-bus-owner-") as temp_dir:
            source = Path(temp_dir) / "camera_bus_owner_test.c"
            executable = Path(temp_dir) / "camera_bus_owner_test.exe"
            source.write_text(harness, encoding="utf-8")
            built = subprocess.run(
                [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror",
                 f"-I{CAMERA_BUS_H.parent}", str(source), "-o", str(executable)],
                capture_output=True, text=True,
            )
            self.assertEqual(built.returncode, 0, built.stdout + built.stderr)
            run = subprocess.run([str(executable)], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stdout + run.stderr)

    def test_hardware_wrapper_propagates_camera_driver_error(self):
        source = HARDWARE_C.read_text(encoding="utf-8")
        capture = source.split("int desk_hardware_camera_capture_jpeg(", 1)[1]
        capture = capture.split("\n}", 1)[0]
        self.assertIn("return drv_ret;", capture)
        driver_path = capture.split("if (s_board_driver_ops", 1)[1].split("\n    }", 1)[0]
        self.assertNotIn("return DESK_HW_ERR_UNAVAILABLE;", driver_path)

    def test_tclaw_camera_failure_keeps_driver_code_and_actionable_cause(self):
        source = TCLAW_BRIDGE_C.read_text(encoding="utf-8")
        names = source.split("static const char *desk_camera_capture_error_name(", 1)[1].split("\n}", 1)[0]
        for error_name in (
            "PTL/media protocol validation failed",
            "media payload CRC mismatch",
            "camera SYNC input read failed",
            "SPI transport failed",
            "camera FIFO remained empty until deadline",
        ):
            self.assertIn(error_name, names)
        camera_tool = source.split('else if (strcmp(operation, "camera.capture")', 1)[1]
        camera_tool = camera_tool.split("/* 4. Display Expression Tool */", 1)[0]
        self.assertIn('"Camera capture rc=%d: %s"', camera_tool)
        self.assertIn("return_code = cam_rc;", camera_tool)
        self.assertIn("retryable = desk_camera_capture_error_retryable(cam_rc);", camera_tool)
        self.assertNotIn("return_code = DESK_HW_ERR_UNAVAILABLE;", camera_tool)

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

    def test_resynchronization_preserves_spi_transport_error(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        scan = source.split("static int camera_read_resynchronized_header(", 1)[1].split("\n}", 1)[0]
        capture = source.split("int desk_watcher_camera_capture(", 1)[1]
        scan_failure = capture.split("if (scan_rc != 0)", 1)[1].split("s_ptl_resynchronizing = false", 1)[0]
        self.assertIn("SPI PTL resynchronization scan failed", scan)
        self.assertIn("return DESK_WATCHER_CAMERA_ERR_SPI;", scan)
        self.assertIn("return DESK_WATCHER_CAMERA_ERR_PROTOCOL;", scan)
        self.assertIn("return camera_capture_release(scan_rc);", scan_failure)
        self.assertNotIn("return camera_capture_release(DESK_WATCHER_CAMERA_ERR_PROTOCOL);", scan_failure)

    def test_resynchronization_copies_all_bytes_already_clocked_after_header(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        scan = source.split("static int camera_read_resynchronized_header(", 1)[1].split("\n}", 1)[0]

        # The SPI transfer consumes the complete 16-byte chunk before the
        # rolling scanner can identify a header. The tail after the header is
        # already body data and must be prefetched exactly as in the locked
        # WatcheRobot receiver; omitting it shifts every recovered frame.
        self.assertIn("*prefetched = sizeof(chunk) - i - 1U;", scan)
        self.assertIn("memcpy(s_ptl_body, chunk + i + 1U, *prefetched)", scan)
        self.assertNotIn("i + 1U - *prefetched", scan)

    def test_resynchronization_ends_each_spi_chunk_to_match_locked_ptl_receiver(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        scan = source.split("static int camera_read_resynchronized_header(", 1)[1].split("\n}", 1)[0]
        capture = source.split("int desk_watcher_camera_capture(", 1)[1]
        normal_header = capture.split("spi_transaction_t hdr_trans", 1)[1].split("};", 1)[0]

        # The official receiver uses spi_read(..., false) for resync scans,
        # but holds CS across the ordinary header/body pair.
        self.assertIn(".rx_buffer = chunk, .flags = 0", scan)
        self.assertIn(".flags = SPI_TRANS_CS_KEEP_ACTIVE", normal_header)

    def test_zero_ptl_header_has_distinct_empty_error_and_spi_failures_keep_stage(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        capture = source.split("int desk_watcher_camera_capture(", 1)[1]
        self.assertIn("if (header_or != 0)", capture)
        self.assertIn("CAM_PTL_CAPTURE_TIMEOUT_MS", capture)
        self.assertIn("camera_capture_deadline_us", capture)
        self.assertIn("CAM_PTL_EMPTY_BACKOFF_MS", capture)
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

    def test_psram_ptl_body_uses_idf54_driver_bounce_buffer(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        body_transfer = source.split("/* Step 2: Read complete body", 1)[1].split("/* Step 3:", 1)[0]
        self.assertIn(".flags = 0", body_transfer)
        self.assertNotIn("SPI_TRANS_DMA_USE_PSRAM", body_transfer)
        self.assertIn("IDF 5.4 has no PSRAM transaction flag", source)

    def test_empty_ptl_poll_records_raw_header_and_sync_observation(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        capture = source.split("int desk_watcher_camera_capture(", 1)[1]
        empty_poll = capture.split("if (header_or != 0)", 1)[1].split("vTaskDelay(pdMS_TO_TICKS(CAM_PTL_EMPTY_BACKOFF_MS));", 1)[0]
        self.assertIn("camera_sync_is_active", empty_poll)
        self.assertIn("base_header[0]", empty_poll)
        self.assertIn("sync read rc=%d", empty_poll)

    def test_empty_headers_retry_until_capture_deadline_and_allow_later_valid_header(self):
        source = CAMERA_C.read_text(encoding="utf-8")
        capture = source.split("int desk_watcher_camera_capture(", 1)[1]
        self.assertIn("while (esp_timer_get_time() < camera_capture_deadline_us)", capture)
        self.assertIn("if (header_or != 0)", capture)
        self.assertNotIn("CAM_PTL_EMPTY_RETRY_LIMIT", capture)
        self.assertIn("camera_capture_deadline_us", capture)
        self.assertIn("DESK_WATCHER_CAMERA_ERR_EMPTY", capture)

    def test_capture_timeout_is_passed_from_hardware_api_to_board_driver(self):
        header = (ROOT / "firmware/components/desk_hardware/include/desk_hardware.h").read_text(encoding="utf-8")
        hardware = HARDWARE_C.read_text(encoding="utf-8")
        camera_header = CAMERA_H.read_text(encoding="utf-8")
        camera = CAMERA_C.read_text(encoding="utf-8")
        self.assertIn("size_t *out_len, uint32_t timeout_ms);", header)
        self.assertIn("&drv_len, timeout_ms)", hardware)
        self.assertIn("size_t *out_len, uint32_t timeout_ms);", camera_header)
        self.assertIn("size_t *out_len, uint32_t timeout_ms)", camera)
        self.assertIn("(unsigned)timeout_ms", camera)


if __name__ == "__main__":
    unittest.main()
