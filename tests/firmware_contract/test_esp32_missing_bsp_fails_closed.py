"""L2 target-configuration test for production ESP32 defaults without a BSP.

This compiles the real hardware HAL with ESP_PLATFORM and a small UART boundary
stub. It proves missing production drivers fail closed; it does not simulate or
claim physical hardware behavior.
"""

from __future__ import annotations

import ctypes
import os
import pathlib
import subprocess
import tempfile
import unittest


DESK_HW_OK = 0
DESK_HW_ERR_UNAVAILABLE = -2
DESK_HW_ERR_TIMEOUT = -3
DESK_HW_ERR_IO = -4
DESK_HW_ERR_INVALID_ARG = -1

DRIVER_INIT_CB = ctypes.CFUNCTYPE(ctypes.c_int)
DRIVER_DISPLAY_CB = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)
DRIVER_AUDIO_READ_CB = ctypes.CFUNCTYPE(
    ctypes.c_int,
    ctypes.POINTER(ctypes.c_int16),
    ctypes.c_size_t,
    ctypes.POINTER(ctypes.c_size_t),
)


class DeskBoardDriverOps(ctypes.Structure):
    _fields_ = [
        ("driver_name", ctypes.c_char_p),
        ("init", ctypes.c_void_p),
        ("audio_record_read", DRIVER_AUDIO_READ_CB),
        ("audio_play_pcm", ctypes.c_void_p),
        ("motion_move", ctypes.c_void_p),
        ("motion_stop", ctypes.c_void_p),
        ("camera_capture", ctypes.c_void_p),
        ("display_expression", DRIVER_DISPLAY_CB),
        ("deinit", ctypes.c_void_p),
    ]


class TestEsp32MissingBspFailsClosed(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="desk-hw-esp-contract-")
        tmp = pathlib.Path(cls.tmp.name)
        (tmp / "include" / "driver").mkdir(parents=True)
        (tmp / "include" / "driver" / "uart.h").write_text(
            """#pragma once
#include <stddef.h>
int uart_is_driver_installed(int port);
int uart_write_bytes(int port, const char *data, size_t len);
int uart_read_bytes(int port, void *data, size_t len, int ticks);
#define UART_NUM_2 2
#define pdMS_TO_TICKS(ms) (ms)
void test_uart_set_installed(int installed);
void test_uart_set_write_result(int result);
void test_uart_reset_writes(void);
int test_uart_write_count(void);
""",
            encoding="utf-8",
        )
        (tmp / "include" / "freertos").mkdir(parents=True)
        (tmp / "include" / "freertos" / "FreeRTOS.h").write_text(
            "#pragma once\n#define pdMS_TO_TICKS(ms) (ms)\n", encoding="utf-8"
        )
        (tmp / "include" / "freertos" / "semphr.h").write_text(
            "#pragma once\n", encoding="utf-8"
        )
        (tmp / "include" / "esp_timer.h").write_text(
            "#pragma once\n#include <stdint.h>\nint64_t esp_timer_get_time(void);\n",
            encoding="utf-8",
        )
        (tmp / "uart_stub.c").write_text(
            """#include <stddef.h>
#include <stdint.h>
static int installed;
static int write_result = -999;
static int writes;
int uart_is_driver_installed(int port) { (void)port; return installed; }
int uart_write_bytes(int port, const char *data, size_t len) {
    (void)port; (void)data; writes++;
    return write_result == -999 ? (int)len : write_result;
}
int uart_read_bytes(int port, void *data, size_t len, int ticks) {
    (void)port; (void)data; (void)len; (void)ticks; return 0;
}
void test_uart_set_installed(int value) { installed = value; }
void test_uart_set_write_result(int value) { write_result = value; }
void test_uart_reset_writes(void) { writes = 0; }
int test_uart_write_count(void) { return writes; }
int64_t esp_timer_get_time(void) { static int64_t now; return now += 1000; }
""",
            encoding="utf-8",
        )
        repo = pathlib.Path(__file__).resolve().parents[2]
        cls.library_path = tmp / "libdesk_hardware_esp_contract.so"
        command = [
            "gcc", "-shared", "-fPIC", "-O0", "-DESP_PLATFORM",
            f"-I{tmp / 'include'}",
            f"-I{repo / 'firmware/components/desk_hardware/include'}",
            str(repo / "firmware/components/desk_hardware/src/desk_arbiter.c"),
            str(repo / "firmware/components/desk_hardware/src/desk_hardware.c"),
            str(tmp / "uart_stub.c"), "-o", str(cls.library_path),
        ]
        built = subprocess.run(command, capture_output=True, text=True)
        if built.returncode:
            raise RuntimeError(f"ESP32 contract compile failed: {built.stderr}")

        cls.lib = ctypes.CDLL(str(cls.library_path))
        cls.lib.desk_hardware_init.restype = ctypes.c_int
        cls.lib.desk_hardware_display_self_test.restype = ctypes.c_int
        cls.lib.desk_hardware_display_self_test.argtypes = [ctypes.c_void_p]
        cls.lib.desk_hardware_is_display_initialized.restype = ctypes.c_bool
        cls.lib.desk_hardware_display_set_expression.restype = ctypes.c_int
        cls.lib.desk_hardware_display_set_expression.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        cls.lib.desk_hardware_camera_capture_jpeg.restype = ctypes.c_int
        cls.lib.desk_hardware_camera_capture_jpeg.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t), ctypes.c_uint32
        ]
        cls.lib.desk_hardware_audio_record_start.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_record_start.argtypes = [ctypes.c_uint32]
        cls.lib.desk_hardware_audio_record_read.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_record_read.argtypes = [
            ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t)
        ]
        cls.lib.desk_hardware_audio_record_inject.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_record_inject.argtypes = [
            ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t
        ]
        cls.lib.desk_hardware_audio_play_pcm.restype = ctypes.c_int
        cls.lib.desk_hardware_audio_play_pcm.argtypes = [
            ctypes.POINTER(ctypes.c_int16), ctypes.c_size_t, ctypes.c_uint32
        ]
        cls.lib.desk_hardware_motion_stop.restype = ctypes.c_int
        cls.lib.desk_hardware_motion_stop.argtypes = [ctypes.c_bool]
        cls.lib.desk_hardware_set_strict_mode.argtypes = [ctypes.c_bool]
        cls.lib.desk_hardware_register_driver_ops.restype = ctypes.c_int
        cls.lib.desk_hardware_register_driver_ops.argtypes = [ctypes.POINTER(DeskBoardDriverOps)]
        cls.lib.desk_hardware_set_mcu_max_retries.argtypes = [ctypes.c_uint32]
        cls.lib.desk_hardware_set_mcu_timeout_ms.argtypes = [ctypes.c_uint32]
        cls.lib.test_uart_set_installed.argtypes = [ctypes.c_int]
        cls.lib.test_uart_set_write_result.argtypes = [ctypes.c_int]
        cls.lib.test_uart_reset_writes.argtypes = []
        cls.lib.test_uart_write_count.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def setUp(self) -> None:
        self.lib.test_uart_set_installed(0)
        self.lib.test_uart_set_write_result(-999)
        self.lib.test_uart_reset_writes()
        self.lib.desk_hardware_init()

    def test_default_esp32_driver_does_not_claim_missing_peripherals(self) -> None:
        self.assertFalse(self.lib.desk_hardware_is_display_initialized())
        self.assertEqual(self.lib.desk_hardware_display_self_test(None), DESK_HW_ERR_UNAVAILABLE)
        self.assertEqual(
            self.lib.desk_hardware_display_set_expression(b"thinking", b"starting"),
            DESK_HW_ERR_UNAVAILABLE,
        )

        pcm = (ctypes.c_int16 * 4)(1, 2, 3, 4)
        self.assertEqual(self.lib.desk_hardware_audio_play_pcm(pcm, 4, 16000), DESK_HW_ERR_UNAVAILABLE)
        self.assertEqual(self.lib.desk_hardware_audio_record_start(16000), DESK_HW_ERR_UNAVAILABLE)
        read_count = ctypes.c_size_t(99)
        self.assertEqual(
            self.lib.desk_hardware_audio_record_read(pcm, 4, ctypes.byref(read_count)),
            DESK_HW_ERR_UNAVAILABLE,
        )
        self.assertEqual(read_count.value, 0)
        jpeg = ctypes.create_string_buffer(64)
        jpeg_len = ctypes.c_size_t(99)
        self.assertEqual(
            self.lib.desk_hardware_camera_capture_jpeg(jpeg, 64, ctypes.byref(jpeg_len), 50),
            DESK_HW_ERR_UNAVAILABLE,
        )
        self.assertEqual(jpeg_len.value, 0)

    def test_camera_invalid_arguments_clear_available_output_length(self) -> None:
        jpeg_len = ctypes.c_size_t(99)
        self.assertEqual(
            self.lib.desk_hardware_camera_capture_jpeg(None, 64, ctypes.byref(jpeg_len), 50),
            DESK_HW_ERR_INVALID_ARG,
        )
        self.assertEqual(jpeg_len.value, 0)

    def test_esp_production_does_not_accept_injected_microphone_samples(self) -> None:
        samples = (ctypes.c_int16 * 2)(11, 22)
        self.assertEqual(
            self.lib.desk_hardware_audio_record_inject(samples, 2),
            DESK_HW_ERR_UNAVAILABLE,
        )
        read_count = ctypes.c_size_t(99)
        self.assertEqual(
            self.lib.desk_hardware_audio_record_read(samples, 2, ctypes.byref(read_count)),
            DESK_HW_ERR_UNAVAILABLE,
        )
        self.assertEqual(read_count.value, 0)

    def test_esp_production_does_not_accept_injected_camera_frames(self) -> None:
        frame = (ctypes.c_uint8 * 4)(0xff, 0xd8, 0xff, 0xd9)
        self.lib.desk_hardware_camera_inject_frame.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32]
        self.lib.desk_hardware_camera_inject_frame.restype = ctypes.c_int
        self.assertEqual(self.lib.desk_hardware_camera_inject_frame(frame, 4, 123), DESK_HW_ERR_UNAVAILABLE)

    def test_audio_driver_read_count_is_committed_only_when_valid(self) -> None:
        cases = ((-4, 3, DESK_HW_ERR_IO, 0), (0, 3, DESK_HW_ERR_IO, 0), (0, 2, 0, 2))
        for driver_rc, reported_count, expected_rc, expected_count in cases:
            self.lib.desk_hardware_init()

            @DRIVER_AUDIO_READ_CB
            def read_cb(_pcm, _capacity, count_ptr, rc=driver_rc, count=reported_count):
                count_ptr[0] = count
                return rc

            @DRIVER_INIT_CB
            def init_cb():
                return 0

            ops = DeskBoardDriverOps()
            ops.driver_name = b"audio_contract_fixture"
            ops.init = ctypes.cast(init_cb, ctypes.c_void_p)
            ops.audio_record_read = read_cb
            self.assertEqual(self.lib.desk_hardware_register_driver_ops(ctypes.byref(ops)), 0)

            buffer = (ctypes.c_int16 * 2)()
            read_count = ctypes.c_size_t(99)
            rc = self.lib.desk_hardware_audio_record_read(buffer, 2, ctypes.byref(read_count))
            self.assertEqual(rc, expected_rc)
            self.assertEqual(read_count.value, expected_count)

    def test_display_initialized_only_for_registered_display_capability(self) -> None:
        @DRIVER_DISPLAY_CB
        def display_cb(_preset, _status):
            return 0

        @DRIVER_INIT_CB
        def failed_init():
            return DESK_HW_ERR_IO

        # Audio-only driver without init is treated as initialized, but cannot
        # claim display capability.
        self.lib.desk_hardware_init()
        @DRIVER_AUDIO_READ_CB
        def audio_cb(_pcm, _capacity, count_ptr):
            count_ptr[0] = 1
            return 0
        audio_ops = DeskBoardDriverOps()
        audio_ops.driver_name = b"audio_only_fixture"
        audio_ops.audio_record_read = audio_cb
        self.assertEqual(self.lib.desk_hardware_register_driver_ops(ctypes.byref(audio_ops)), 0)
        self.assertFalse(self.lib.desk_hardware_is_display_initialized())

        # A failed init cannot promote a display callback to initialized.
        self.lib.desk_hardware_init()
        failed_ops = DeskBoardDriverOps()
        failed_ops.driver_name = b"failed_display_fixture"
        failed_ops.init = ctypes.cast(failed_init, ctypes.c_void_p)
        failed_ops.display_expression = display_cb
        self.assertEqual(self.lib.desk_hardware_register_driver_ops(ctypes.byref(failed_ops)), DESK_HW_ERR_IO)
        self.assertFalse(self.lib.desk_hardware_is_display_initialized())

        # An already initialized ops table may omit init; registering an actual
        # display callback makes that individual capability available.
        self.lib.desk_hardware_init()
        display_ops = DeskBoardDriverOps()
        display_ops.driver_name = b"display_fixture"
        display_ops.display_expression = display_cb
        self.assertEqual(self.lib.desk_hardware_register_driver_ops(ctypes.byref(display_ops)), 0)
        self.assertTrue(self.lib.desk_hardware_is_display_initialized())

    def test_uninstalled_stm32_uart_is_neither_available_nor_written(self) -> None:
        self.assertEqual(self.lib.desk_hardware_motion_stop(True), DESK_HW_ERR_UNAVAILABLE)
        self.assertEqual(self.lib.test_uart_write_count(), 0)

    def test_registered_partial_board_cannot_claim_missing_body_stop(self) -> None:
        ops = DeskBoardDriverOps()
        ops.driver_name = b"display_audio_only"
        self.assertEqual(self.lib.desk_hardware_register_driver_ops(ctypes.byref(ops)), 0)
        self.assertEqual(self.lib.desk_hardware_motion_stop(True), DESK_HW_ERR_UNAVAILABLE)
        self.assertEqual(self.lib.test_uart_write_count(), 0)

    def test_short_or_failed_stm32_uart_write_is_an_io_error_in_either_mode(self) -> None:
        self.lib.test_uart_set_installed(1)
        self.lib.desk_hardware_set_mcu_max_retries(0)
        self.lib.desk_hardware_set_mcu_timeout_ms(1)

        for write_result, strict_mode in ((1, False), (-1, True)):
            self.lib.test_uart_set_write_result(write_result)
            self.lib.test_uart_reset_writes()
            self.lib.desk_hardware_set_strict_mode(strict_mode)
            self.assertEqual(self.lib.desk_hardware_motion_stop(True), DESK_HW_ERR_IO)
            self.assertEqual(self.lib.test_uart_write_count(), 1)


if __name__ == "__main__":
    unittest.main()
