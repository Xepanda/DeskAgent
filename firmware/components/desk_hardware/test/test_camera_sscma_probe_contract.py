"""Source contract for the opt-in, single-owner HX SSCMA identity probe."""

from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[4]
HEADER = ROOT / "firmware/components/desk_hardware/include/desk_watcher_camera.h"
SOURCE = ROOT / "firmware/components/desk_hardware/src/desk_watcher_camera.c"


class CameraSscmaProbeContract(unittest.TestCase):
    def test_probe_returns_bounded_raw_reply_and_restores_ptl_mode(self):
        header = HEADER.read_text(encoding="utf-8")
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("DESK_WATCHER_CAMERA_SSCMA_REPLY_MAX", header)
        self.assertIn("raw_response", header)
        self.assertIn("raw_response_len", header)
        self.assertIn("DESK_WATCHER_CAMERA_SSCMA_QUERY_VERSION", header)
        self.assertIn("DESK_WATCHER_CAMERA_SSCMA_QUERY_ID", header)
        self.assertIn("desk_watcher_camera_probe_sscma_query", header)

        probe = source.split("int desk_watcher_camera_probe_sscma_query(", 1)[1].split("\n}", 1)[0]
        self.assertIn("s_cam_mutex", probe)
        self.assertIn("camera_control_status_until", probe)
        self.assertIn("camera_control_mode_until(0U, 0U", probe)
        self.assertIn("camera_control_mode_until(saved_state.mode, saved_state.model", probe)
        self.assertIn("AT+VER?\\r", probe)
        self.assertIn("AT+ID?\\r", probe)
        self.assertIn("DESK_WATCHER_CAMERA_SSCMA_REPLY_MAX", probe)
        self.assertIn("raw_response_len", probe)
        self.assertIn("mode_change_attempted = true", probe)
        self.assertIn("goto restore", probe)
        self.assertIn("s_camera_needs_idle_recovery = true", probe)
        self.assertIn("xSemaphoreGive(s_cam_mutex)", probe)
        for forbidden in ("AT+SAMPLE", "AT+INVOKE", "AT+RESET", "spi_bus_add_device"):
            self.assertNotIn(forbidden, probe)

        capture = source.split("int desk_watcher_camera_capture(", 1)[1].split(
            "int desk_watcher_camera_deinit", 1
        )[0]
        self.assertNotIn("probe_sscma", capture)

    def test_reply_framing_and_size_limits_in_native_helper(self):
        compiler = shutil.which("gcc")
        if compiler is None:
            self.skipTest("native gcc unavailable; run this contract under WSL/Linux")
        harness = f'''#include <assert.h>
#include <stdint.h>
#include <string.h>
#include "{(ROOT / "firmware/components/desk_hardware/src/desk_watcher_camera.c").as_posix()}"

int main(void) {{
    uint8_t frame[DESK_WATCHER_CAMERA_SSCMA_REPLY_MAX] = {{0}};
    uint8_t packet[256] = {{0}};
    const uint8_t command[] = "AT+VER?\\r";
    assert(desk_watcher_camera_sscma_encode_packet(2U, command, sizeof(command)-1U,
                                                    packet, sizeof(packet)) == 0);
    assert(packet[0] == 0x10U && packet[1] == 2U);
    assert(packet[2] == 0U && packet[3] == sizeof(command)-1U);
    assert(memcmp(packet + 4, command, sizeof(command)-1U) == 0);
    assert(packet[4 + sizeof(command)-1U] == 0xFFU);
    assert(packet[5 + sizeof(command)-1U] == 0xFFU);
    assert(desk_watcher_camera_sscma_validate_reply(NULL, 2U) == -1);
    assert(desk_watcher_camera_sscma_validate_reply(frame, 0U) == -1);
    assert(desk_watcher_camera_sscma_validate_reply(frame, 1U) == -1);
    frame[0] = '\\r'; frame[1] = '\\n';
    assert(desk_watcher_camera_sscma_validate_reply(frame, 2U) == 0);
    frame[0] = 'x';
    assert(desk_watcher_camera_sscma_validate_reply(frame, 2U) == -1);
    frame[0] = '\\r'; frame[1] = '\\n';
    frame[1] = '\\r';
    assert(desk_watcher_camera_sscma_validate_reply(frame, 2U) == -1);
    frame[1] = '\\n';
    frame[16] = 0;
    assert(desk_watcher_camera_sscma_validate_reply(frame, 17U) == -1);
    memset(frame, 'x', sizeof(frame)); frame[0] = '\\r'; frame[sizeof(frame)-1] = '\\n';
    assert(desk_watcher_camera_sscma_validate_reply(frame, sizeof(frame)) == 0);
    uint8_t oversized[DESK_WATCHER_CAMERA_SSCMA_REPLY_MAX + 1U] = {{0}};
    oversized[0] = '\\r'; oversized[sizeof(oversized)-1] = '\\n';
    assert(desk_watcher_camera_sscma_validate_reply(oversized, sizeof(oversized)) == -1);
    return 0;
}}
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            source = Path(temp_dir) / "camera_sscma_reply_test.c"
            binary = Path(temp_dir) / "camera_sscma_reply_test"
            source.write_text(harness, encoding="utf-8")
            include_args = [
                arg
                for include_dir in sorted((ROOT / "firmware/components").glob("*/include"))
                for arg in ("-I", str(include_dir))
            ]
            subprocess.run(
                [compiler, "-std=c11", *include_args, "-ffunction-sections", "-fdata-sections",
                 str(source), "-Wl,--gc-sections", "-o", str(binary)],
                cwd=ROOT, check=True,
            )
            subprocess.run([str(binary)], check=True)

    def test_probe_response_name_must_match_query_before_success(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        probe = source.split("int desk_watcher_camera_probe_sscma_query(", 1)[1].split("\n}", 1)[0]
        self.assertIn("desk_watcher_camera_sscma_validate_query_reply", probe)
        self.assertIn("camera_sscma_discard_pending_reply(overall_deadline_us)", probe)
        self.assertLess(
            probe.index("camera_sscma_discard_pending_reply(overall_deadline_us)"),
            probe.index("camera_sscma_exchange_tx(packet"),
        )

        compiler = shutil.which("gcc")
        if compiler is None:
            self.skipTest("native gcc unavailable; run this contract under WSL/Linux")
        harness = f'''#include <assert.h>
#include <stdint.h>
#include "{(ROOT / "firmware/components/desk_hardware/src/desk_watcher_camera.c").as_posix()}"

int main(void) {{
    static const char stale_version[] = "\\r{{\\"type\\":0,\\"name\\":\\"VER?\\",\\"code\\":0}}\\n";
    static const char id_reply[] = "\\r{{\\"type\\":0,\\"name\\":\\"ID?\\",\\"code\\":0}}\\n";
    static const char id_error[] = "\\r{{\\"type\\":0,\\"name\\":\\"ID?\\",\\"code\\":1}}\\n";
    assert(desk_watcher_camera_sscma_validate_query_reply(
               DESK_WATCHER_CAMERA_SSCMA_QUERY_ID,
               (const uint8_t *)stale_version, sizeof(stale_version)-1U) != 0);
    assert(desk_watcher_camera_sscma_validate_query_reply(
               DESK_WATCHER_CAMERA_SSCMA_QUERY_ID,
               (const uint8_t *)id_reply, sizeof(id_reply)-1U) == 0);
    assert(desk_watcher_camera_sscma_validate_query_reply(
               DESK_WATCHER_CAMERA_SSCMA_QUERY_ID,
               (const uint8_t *)id_error, sizeof(id_error)-1U) == 1);
    return 0;
}}
'''
        with tempfile.TemporaryDirectory() as temp_dir:
            source_file = Path(temp_dir) / "camera_sscma_query_reply_test.c"
            binary = Path(temp_dir) / "camera_sscma_query_reply_test"
            source_file.write_text(harness, encoding="utf-8")
            include_args = [
                arg
                for include_dir in sorted((ROOT / "firmware/components").glob("*/include"))
                for arg in ("-I", str(include_dir))
            ]
            subprocess.run(
                [compiler, "-std=c11", *include_args, "-ffunction-sections", "-fdata-sections",
                 str(source_file), "-Wl,--gc-sections", "-o", str(binary)],
                cwd=ROOT, check=True,
            )
            subprocess.run([str(binary)], check=True)

    def test_probe_bounds_wait_and_restores_after_all_query_errors(self):
        source = SOURCE.read_text(encoding="utf-8")
        probe = source.split("int desk_watcher_camera_probe_sscma_query(", 1)[1].split("\n}", 1)[0]
        self.assertIn("query_deadline_us = overall_deadline_us - 3500000LL", probe)
        self.assertIn("while (esp_timer_get_time() < query_deadline_us)", probe)
        self.assertIn("available < 2U || available > DESK_WATCHER_CAMERA_SSCMA_REPLY_MAX", probe)
        self.assertIn("camera_sscma_validate_query_reply(", probe)
        self.assertIn("response_mismatch = true", probe)
        self.assertIn("response_mismatch ? DESK_WATCHER_CAMERA_ERR_PROTOCOL : DESK_HW_ERR_TIMEOUT", probe)
        self.assertIn("goto restore", probe)
        self.assertIn("restore:", probe)
        restore = probe.split("restore:", 1)[1].split("done:", 1)[0]
        self.assertIn("if (camera_control_mode_until(saved_state.mode, saved_state.model", restore)
        self.assertIn("s_camera_needs_idle_recovery = true", restore)
        self.assertIn("rc = DESK_WATCHER_CAMERA_ERR_CONTROL", restore)
        self.assertIn("xSemaphoreGive(s_cam_mutex)", probe.split("done:", 1)[1])

    def test_same_name_late_reply_fails_closed_without_transaction_or_reset_proof(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        probe = source.split("int desk_watcher_camera_probe_sscma_query(", 1)[1].split("\n}", 1)[0]
        self.assertIn("no transaction ID", probe)
        self.assertIn("no verified target acknowledgment", probe)
        self.assertIn("rc = DESK_WATCHER_CAMERA_ERR_PROTOCOL", probe)
        self.assertNotIn("response_received = true", probe)
        self.assertLess(probe.index("out_reply->raw_response_len = available"),
                        probe.index("desk_watcher_camera_sscma_validate_query_reply("))

    def test_stale_reply_drain_is_time_byte_and_chunk_bounded(self) -> None:
        source = SOURCE.read_text(encoding="utf-8")
        drain = source.split("static int camera_sscma_discard_pending_reply(", 1)[1].split("\n}", 1)[0]
        self.assertIn("SSCMA_SPI_DRAIN_TIMEOUT_US", source)
        self.assertIn("SSCMA_SPI_DRAIN_MAX_BYTES", drain)
        self.assertIn("SSCMA_SPI_DRAIN_CHUNK_BYTES", drain)
        self.assertIn("camera_sscma_read(chunk_size, discard, drain_deadline_us)", drain)
        self.assertIn("DESK_HW_ERR_TIMEOUT", drain)
        self.assertIn("DESK_WATCHER_CAMERA_ERR_PROTOCOL", drain)
        read = source.split("static esp_err_t camera_sscma_read(", 1)[1].split("\n}", 1)[0]
        self.assertIn("spi_device_acquire_bus(s_spi_dev, portMAX_DELAY)", read)
        self.assertIn("camera_sscma_available(&available, drain_deadline_us)", drain)
        exchange = source.split("static esp_err_t camera_sscma_exchange_tx(", 1)[1].split("\n}", 1)[0]
        self.assertIn("esp_timer_get_time() >= deadline_us", exchange)


if __name__ == "__main__":
    unittest.main()
