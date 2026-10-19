"""L2 boundary tests for the SenseCAP Watcher native LCD adapter.

The tests compile the board adapter against a host-only IDF boundary shim. They
exercise fail-closed behavior and PCA9535 read-modify-write rules; they do not
claim that an LCD is connected or visible on physical hardware.
"""

from __future__ import annotations

import ctypes
import pathlib
import subprocess
import tempfile
import unittest


class DeskBoardDriverOps(ctypes.Structure):
    _fields_ = [
        ("driver_name", ctypes.c_char_p),
        ("init", ctypes.c_void_p),
        ("audio_record_read", ctypes.c_void_p),
        ("audio_play_pcm", ctypes.c_void_p),
        ("motion_move", ctypes.c_void_p),
        ("motion_stop", ctypes.c_void_p),
        ("camera_capture", ctypes.c_void_p),
        ("display_expression", ctypes.c_void_p),
        ("deinit", ctypes.c_void_p),
    ]


class TestDeskWatcherBoardContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory(prefix="desk-watcher-board-contract-")
        temp = pathlib.Path(cls.tmp.name)
        stubs = temp / "stubs"
        stubs.mkdir()
        (stubs / "esp_lcd_panel_vendor.h").write_text(
            """#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_NO_MEM 0x105
#define ESP_ERR_TIMEOUT 0x107
typedef void *esp_lcd_panel_io_handle_t;
typedef void *esp_lcd_panel_handle_t;
typedef struct { int unused; } esp_lcd_panel_io_event_data_t;
typedef bool (*esp_lcd_panel_io_color_trans_done_cb_t)(esp_lcd_panel_io_handle_t, esp_lcd_panel_io_event_data_t *, void *);
typedef struct { int sclk_io_num, data0_io_num, data1_io_num, data2_io_num, data3_io_num; size_t max_transfer_sz; } spi_bus_config_t;
typedef struct { int cs_gpio_num, dc_gpio_num, spi_mode, pclk_hz, trans_queue_depth, lcd_cmd_bits, lcd_param_bits; esp_lcd_panel_io_color_trans_done_cb_t on_color_trans_done; void *user_ctx; struct { bool quad_mode; } flags; } esp_lcd_panel_io_spi_config_t;
typedef struct { int reset_gpio_num, rgb_ele_order, bits_per_pixel; void *vendor_config; } esp_lcd_panel_dev_config_t;
#define LCD_RGB_ELEMENT_ORDER_RGB 0
""",
            encoding="utf-8",
        )
        (stubs / "watcher_board_test_platform.h").write_text(
            """#pragma once
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_spd2010.h"
#include "driver/gpio.h"
#include "driver/i2c.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
esp_err_t esp_lcd_new_panel_io_spi(void *, const esp_lcd_panel_io_spi_config_t *, esp_lcd_panel_io_handle_t *);
esp_err_t esp_lcd_new_panel_spd2010(esp_lcd_panel_io_handle_t, const esp_lcd_panel_dev_config_t *, esp_lcd_panel_handle_t *);
esp_err_t esp_lcd_panel_reset(esp_lcd_panel_handle_t);
esp_err_t esp_lcd_panel_init(esp_lcd_panel_handle_t);
esp_err_t esp_lcd_panel_mirror(esp_lcd_panel_handle_t, bool, bool);
esp_err_t esp_lcd_panel_disp_on_off(esp_lcd_panel_handle_t, bool);
esp_err_t esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t, int, int, int, int, const void *);
esp_err_t esp_lcd_panel_del(esp_lcd_panel_handle_t);
esp_err_t esp_lcd_panel_io_del(esp_lcd_panel_io_handle_t);
void watcher_test_reset(void);
void watcher_test_set_bus_installed(int installed);
void watcher_test_set_expander_present(int present);
void watcher_test_set_system_enabled(int enabled);
void watcher_test_set_failure(int stage);
void watcher_test_complete_pending_draw(void);
int watcher_test_free_count(void);
int watcher_test_panel_delete_count(void);
int watcher_test_i2c_config_count(void);
int watcher_test_i2c_install_count(void);
int watcher_test_write_count(void);
int watcher_test_write_reg(int index);
int watcher_test_write_value(int index);
int watcher_test_spi_init_count(void);
int watcher_test_draw_count(void);
int watcher_test_expander_probe_count(void);
int watcher_test_delay_total_ms(void);
const char *esp_err_to_name(esp_err_t);
""",
            encoding="utf-8",
        )
        for header, contents in {
            "driver/gpio.h": """#pragma once
typedef int gpio_num_t;
#define GPIO_NUM_47 47
#define GPIO_NUM_48 48
#define GPIO_NUM_7 7
#define GPIO_NUM_9 9
#define GPIO_NUM_1 1
#define GPIO_NUM_14 14
#define GPIO_NUM_13 13
#define GPIO_NUM_45 45
#define GPIO_NUM_8 8
#define GPIO_NUM_NC -1
""",
            "driver/i2c.h": """#pragma once
#include <stddef.h>
#include <stdint.h>
typedef int i2c_port_t;
typedef void *i2c_cmd_handle_t;
typedef int esp_err_t;
typedef struct { int mode, sda_io_num, scl_io_num, sda_pullup_en, scl_pullup_en; struct { uint32_t clk_speed; } master; } i2c_config_t;
#define I2C_NUM_0 0
#define I2C_MODE_MASTER 1
#define GPIO_PULLUP_DISABLE 0
#define ESP_INTR_FLAG_SHARED 0
esp_err_t i2c_param_config(i2c_port_t, const i2c_config_t *);
esp_err_t i2c_driver_install(i2c_port_t, int, size_t, size_t, int);
esp_err_t i2c_master_write_to_device(i2c_port_t, uint8_t, const uint8_t *, size_t, int);
esp_err_t i2c_master_write_read_device(i2c_port_t, uint8_t, const uint8_t *, size_t, uint8_t *, size_t, int);
i2c_cmd_handle_t i2c_cmd_link_create(void);
void i2c_cmd_link_delete(i2c_cmd_handle_t);
esp_err_t i2c_master_start(i2c_cmd_handle_t);
esp_err_t i2c_master_write_byte(i2c_cmd_handle_t, uint8_t, int);
esp_err_t i2c_master_stop(i2c_cmd_handle_t);
esp_err_t i2c_master_cmd_begin(i2c_port_t, i2c_cmd_handle_t, int);
""",
            "driver/ledc.h": """#pragma once
typedef struct { int speed_mode, duty_resolution, timer_num, freq_hz, clk_cfg; } ledc_timer_config_t;
typedef struct { int gpio_num, speed_mode, channel, intr_type, timer_sel, duty, hpoint; } ledc_channel_config_t;
#define LEDC_LOW_SPEED_MODE 0
#define LEDC_TIMER_1 1
#define LEDC_TIMER_10_BIT 10
#define LEDC_AUTO_CLK 0
#define LEDC_CHANNEL_1 1
#define LEDC_INTR_DISABLE 0
int ledc_timer_config(const ledc_timer_config_t *);
int ledc_channel_config(const ledc_channel_config_t *);
int ledc_set_duty(int, int, unsigned);
int ledc_update_duty(int, int);
""",
            "driver/spi_master.h": """#pragma once
#include <stddef.h>
#include "esp_lcd_panel_vendor.h"
#define SPI3_HOST 3
#define SPI_DMA_CH_AUTO 1
typedef int spi_host_device_t;
typedef void *esp_lcd_spi_bus_handle_t;
esp_err_t spi_bus_initialize(spi_host_device_t, const spi_bus_config_t *, int);
esp_err_t spi_bus_free(spi_host_device_t);
""",
            "esp_heap_caps.h": """#pragma once
#include <stddef.h>
#define MALLOC_CAP_DMA 1
#define MALLOC_CAP_INTERNAL 2
void *heap_caps_malloc(size_t, unsigned);
void heap_caps_free(void *);
""",
            "esp_log.h": """#pragma once
#define ESP_LOGI(tag, fmt, ...) ((void)0)
#define ESP_LOGW(tag, fmt, ...) ((void)0)
#define ESP_LOGE(tag, fmt, ...) ((void)0)
""",
            "freertos/FreeRTOS.h": """#pragma once
#define pdMS_TO_TICKS(ms) (ms)
typedef int BaseType_t;
#define pdTRUE 1
#define pdFALSE 0
""",
            "freertos/semphr.h": """#pragma once
typedef void *SemaphoreHandle_t;
SemaphoreHandle_t xSemaphoreCreateBinary(void);
SemaphoreHandle_t xSemaphoreCreateMutex(void);
int xSemaphoreTake(SemaphoreHandle_t, unsigned);
int xSemaphoreGive(SemaphoreHandle_t);
int xSemaphoreGiveFromISR(SemaphoreHandle_t, int *);
void vSemaphoreDelete(SemaphoreHandle_t);
""",
            "freertos/task.h": """#pragma once
void vTaskDelay(unsigned);
""",
        }.items():
            path = stubs / header
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(contents, encoding="utf-8")

        (temp / "support.c").write_text(
            r"""#include <stdlib.h>
#include <string.h>
#include "watcher_board_test_platform.h"
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_NO_MEM 0x105
#define ESP_ERR_TIMEOUT 0x107
enum { FAIL_NONE, FAIL_SPI, FAIL_PANEL_IO, FAIL_PANEL_NEW, FAIL_PANEL_INIT, FAIL_DRAW, FAIL_BUFFER, FAIL_DRAW_TIMEOUT };
static int bus_installed, expander_present, system_enabled, failure;
static int i2c_configs, i2c_installs, writes, spi_inits, draws, probes, delay_ms;
static int write_regs[16], write_values[16];
static uint8_t regs[8];
static uint8_t dma_buffer[412 * 8 * 2];
typedef struct { int token, mutex; } fake_sem_t;
static fake_sem_t draw_sem, mutex_sem;
static esp_lcd_panel_io_color_trans_done_cb_t draw_callback;
static void *draw_callback_ctx;
static int frees, panel_deletes;
void watcher_test_reset(void) {
    bus_installed = 0; expander_present = 1; system_enabled = 1; failure = FAIL_NONE;
    i2c_configs = i2c_installs = writes = spi_inits = draws = probes = delay_ms = 0;
    memset(write_regs, 0, sizeof(write_regs)); memset(write_values, 0, sizeof(write_values));
    memset(regs, 0, sizeof(regs));
    draw_sem.token = 0; mutex_sem.mutex = 0; draw_callback = NULL; draw_callback_ctx = NULL; frees = panel_deletes = 0;
    regs[0] = 0x20; regs[1] = 0x18;
    regs[2] = 0x34; regs[3] = system_enabled ? 0x04 : 0x00;
    regs[4] = 0x12; regs[5] = 0x34;
    regs[6] = 0xff; regs[7] = 0xfb;
}
void watcher_test_set_bus_installed(int v) { bus_installed = v; }
void watcher_test_set_expander_present(int v) { expander_present = v; }
void watcher_test_set_system_enabled(int v) { system_enabled = v; if (v) regs[3] |= 0x04; else regs[3] &= (uint8_t)~0x04; }
void watcher_test_set_failure(int v) { failure = v; }
void watcher_test_complete_pending_draw(void) { if (draw_callback) draw_callback((void*)0x11, NULL, draw_callback_ctx); }
int watcher_test_free_count(void) { return frees; }
int watcher_test_panel_delete_count(void) { return panel_deletes; }
int watcher_test_i2c_config_count(void) { return i2c_configs; }
int watcher_test_i2c_install_count(void) { return i2c_installs; }
int watcher_test_write_count(void) { return writes; }
int watcher_test_write_reg(int i) { return i >= 0 && i < writes ? write_regs[i] : -1; }
int watcher_test_write_value(int i) { return i >= 0 && i < writes ? write_values[i] : -1; }
int watcher_test_spi_init_count(void) { return spi_inits; }
int watcher_test_draw_count(void) { return draws; }
int watcher_test_expander_probe_count(void) { return probes; }
int watcher_test_delay_total_ms(void) { return delay_ms; }
esp_err_t i2c_param_config(i2c_port_t p, const i2c_config_t *c) { (void)p; i2c_configs++; return ESP_OK; }
esp_err_t i2c_driver_install(i2c_port_t p, int m, size_t a, size_t b, int f) { (void)p;(void)m;(void)a;(void)b;(void)f;i2c_installs++;bus_installed=1;return ESP_OK; }
i2c_cmd_handle_t i2c_cmd_link_create(void) { return (void*)1; }
void i2c_cmd_link_delete(i2c_cmd_handle_t h) { (void)h; }
esp_err_t i2c_master_start(i2c_cmd_handle_t h) { (void)h;return ESP_OK; }
esp_err_t i2c_master_write_byte(i2c_cmd_handle_t h, uint8_t b, int ack) { (void)h;(void)b;(void)ack;return ESP_OK; }
esp_err_t i2c_master_stop(i2c_cmd_handle_t h) { (void)h;return ESP_OK; }
esp_err_t i2c_master_cmd_begin(i2c_port_t p, i2c_cmd_handle_t h, int t) { (void)p;(void)h;(void)t;return bus_installed && expander_present ? ESP_OK : ESP_FAIL; }
esp_err_t i2c_master_write_to_device(i2c_port_t p, uint8_t a, const uint8_t *d, size_t n, int t) {
    (void)p;(void)t;
    if (!bus_installed) return ESP_ERR_INVALID_STATE;
    if (a != 0x21 || !expander_present) return ESP_FAIL;
    probes++;
    if (n == 1) return ESP_OK;
    if (n != 2 || d[0] > 7) return ESP_FAIL;
    regs[d[0]] = d[1];
    if (writes < 16) { write_regs[writes] = d[0]; write_values[writes] = d[1]; }
    writes++;
    return ESP_OK;
}
esp_err_t i2c_master_write_read_device(i2c_port_t p, uint8_t a, const uint8_t *w, size_t wn, uint8_t *r, size_t rn, int t) {
    (void)p;(void)t;
    if (!bus_installed) return ESP_ERR_INVALID_STATE;
    if (a != 0x21 || !expander_present || wn != 1 || rn != 1 || w[0] > 7) return ESP_FAIL;
    r[0] = regs[w[0]]; return ESP_OK;
}
esp_err_t spi_bus_initialize(spi_host_device_t h, const spi_bus_config_t *c, int d) { (void)h;(void)c;(void)d;spi_inits++;return failure == FAIL_SPI ? ESP_FAIL : ESP_OK; }
esp_err_t spi_bus_free(spi_host_device_t h) { (void)h; return ESP_OK; }
esp_err_t esp_lcd_new_panel_io_spi(void *b, const esp_lcd_panel_io_spi_config_t *c, esp_lcd_panel_io_handle_t *o) { (void)b;if(failure==FAIL_PANEL_IO)return ESP_FAIL;draw_callback=c->on_color_trans_done;draw_callback_ctx=c->user_ctx;*o=(void*)0x11;return ESP_OK; }
esp_err_t esp_lcd_new_panel_spd2010(esp_lcd_panel_io_handle_t i, const esp_lcd_panel_dev_config_t *c, esp_lcd_panel_handle_t *o) { (void)i;(void)c;if(failure==FAIL_PANEL_NEW)return ESP_FAIL;*o=(void*)0x22;return ESP_OK; }
esp_err_t esp_lcd_panel_reset(esp_lcd_panel_handle_t p) { (void)p;return ESP_OK; }
esp_err_t esp_lcd_panel_init(esp_lcd_panel_handle_t p) { (void)p;return failure==FAIL_PANEL_INIT?ESP_FAIL:ESP_OK; }
esp_err_t esp_lcd_panel_mirror(esp_lcd_panel_handle_t p, bool x, bool y) { (void)p;(void)x;(void)y;return ESP_OK; }
esp_err_t esp_lcd_panel_disp_on_off(esp_lcd_panel_handle_t p, bool on) { (void)p;(void)on;return ESP_OK; }
esp_err_t esp_lcd_panel_draw_bitmap(esp_lcd_panel_handle_t p, int x0, int y0, int x1, int y1, const void *b) { (void)p;(void)x0;(void)y0;(void)x1;(void)y1;(void)b;draws++;if(failure==FAIL_DRAW)return ESP_FAIL;if(failure!=FAIL_DRAW_TIMEOUT&&draw_callback)draw_callback((void*)0x11,NULL,draw_callback_ctx);return ESP_OK; }
esp_err_t esp_lcd_panel_del(esp_lcd_panel_handle_t p) { (void)p;panel_deletes++;return ESP_OK; }
esp_err_t esp_lcd_panel_io_del(esp_lcd_panel_io_handle_t p) { (void)p;return ESP_OK; }
int ledc_timer_config(const ledc_timer_config_t *c) { (void)c;return ESP_OK; }
int ledc_channel_config(const ledc_channel_config_t *c) { (void)c;return ESP_OK; }
int ledc_set_duty(int m, int c, unsigned d) { (void)m;(void)c;(void)d;return ESP_OK; }
int ledc_update_duty(int m, int c) { (void)m;(void)c;return ESP_OK; }
void *heap_caps_malloc(size_t n, unsigned c) { (void)c;return failure==FAIL_BUFFER || n>sizeof(dma_buffer)?NULL:dma_buffer; }
void heap_caps_free(void *p) { (void)p;frees++; }
SemaphoreHandle_t xSemaphoreCreateBinary(void) { draw_sem.token=0;return &draw_sem; }
SemaphoreHandle_t xSemaphoreCreateMutex(void) { mutex_sem.mutex=0;return &mutex_sem; }
int xSemaphoreTake(SemaphoreHandle_t s, unsigned timeout) { (void)timeout;fake_sem_t *f=s;if(f==&draw_sem){if(!f->token)return 0;f->token=0;return 1;}if(f->mutex)return 0;f->mutex=1;return 1; }
int xSemaphoreGive(SemaphoreHandle_t s) { fake_sem_t *f=s;if(f==&draw_sem)f->token=1;else f->mutex=0;return 1; }
int xSemaphoreGiveFromISR(SemaphoreHandle_t s, int *w) { (void)w;return xSemaphoreGive(s); }
void vSemaphoreDelete(SemaphoreHandle_t s) { (void)s; }
void vTaskDelay(unsigned ms) { delay_ms += (int)ms; }
const char *esp_err_to_name(esp_err_t err) { (void)err; return "stub-error"; }
""",
            encoding="utf-8",
        )

        repo = pathlib.Path(__file__).resolve().parents[2]
        cls.lib_path = temp / "libdesk_watcher_board_contract.so"
        command = [
            "gcc", "-shared", "-fPIC", "-O0", "-DESP_PLATFORM",
            "-DDESK_BOARD_SENSECAP_WATCHER", "-DDESK_WATCHER_BOARD_TEST",
            f"-I{stubs}",
            f"-I{repo / 'firmware/components/desk_hardware/include'}",
            f"-I{repo / 'firmware/third_party/esp_lcd_spd2010/include'}",
            f"-I{repo / 'tests/firmware_contract'}",
            str(repo / "firmware/components/desk_hardware/src/desk_watcher_board.c"),
            str(temp / "support.c"), "-o", str(cls.lib_path),
        ]
        build = subprocess.run(command, capture_output=True, text=True)
        if build.returncode:
            raise RuntimeError(f"Watcher board contract compile failed: {build.stderr}")
        cls.lib = ctypes.CDLL(str(cls.lib_path))
        cls.lib.desk_watcher_board_get_driver_ops.restype = ctypes.POINTER(DeskBoardDriverOps)
        for name in (
            "watcher_test_reset", "watcher_test_set_bus_installed", "watcher_test_set_expander_present",
            "watcher_test_set_system_enabled", "watcher_test_set_failure",
        ):
            getattr(cls.lib, name).argtypes = [ctypes.c_int] if name != "watcher_test_reset" else []
        for name in (
            "watcher_test_i2c_config_count", "watcher_test_i2c_install_count", "watcher_test_write_count",
            "watcher_test_spi_init_count", "watcher_test_draw_count", "watcher_test_expander_probe_count",
            "watcher_test_delay_total_ms",
        ):
            getattr(cls.lib, name).restype = ctypes.c_int
        cls.lib.watcher_test_write_reg.argtypes = [ctypes.c_int]
        cls.lib.watcher_test_write_reg.restype = ctypes.c_int
        cls.lib.watcher_test_write_value.argtypes = [ctypes.c_int]
        cls.lib.watcher_test_write_value.restype = ctypes.c_int
        for name in ("watcher_test_free_count", "watcher_test_panel_delete_count"):
            getattr(cls.lib, name).restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls) -> None:
        cls.tmp.cleanup()

    def setUp(self) -> None:
        previous_ops = self.lib.desk_watcher_board_get_driver_ops().contents
        previous_deinit = ctypes.CFUNCTYPE(ctypes.c_int)(previous_ops.deinit)
        previous_deinit()
        self.lib.watcher_test_reset()
        ops = self.lib.desk_watcher_board_get_driver_ops().contents
        self.init = ctypes.CFUNCTYPE(ctypes.c_int)(ops.init)
        self.display = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_char_p, ctypes.c_char_p)(ops.display_expression)
        self.deinit = ctypes.CFUNCTYPE(ctypes.c_int)(ops.deinit)

    def test_existing_i2c_master_is_probed_without_reconfiguration(self) -> None:
        self.lib.watcher_test_set_bus_installed(1)
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.lib.watcher_test_i2c_config_count(), 0)
        self.assertEqual(self.lib.watcher_test_i2c_install_count(), 0)
        self.assertGreater(self.lib.watcher_test_expander_probe_count(), 0)
        self.assertGreater(self.lib.watcher_test_draw_count(), 0)
        self.assertEqual(self.lib.watcher_test_delay_total_ms(), 50)

    def test_uninstalled_i2c_is_initialized_on_official_general_bus_pins(self) -> None:
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.lib.watcher_test_i2c_config_count(), 1)
        self.assertEqual(self.lib.watcher_test_i2c_install_count(), 1)
        self.assertGreater(self.lib.watcher_test_draw_count(), 0)

    def test_missing_expander_or_disabled_system_rail_never_touches_spi_or_lcd_power(self) -> None:
        self.lib.watcher_test_set_bus_installed(1)
        self.lib.watcher_test_set_expander_present(0)
        self.assertEqual(self.init(), -2)
        self.assertEqual(self.lib.watcher_test_spi_init_count(), 0)
        self.assertEqual(self.lib.watcher_test_write_count(), 0)

        self.lib.watcher_test_reset()
        self.lib.watcher_test_set_bus_installed(1)
        self.lib.watcher_test_set_system_enabled(0)
        self.assertEqual(self.init(), -2)
        self.assertEqual(self.lib.watcher_test_spi_init_count(), 0)
        self.assertEqual(self.lib.watcher_test_write_count(), 0)

    def test_expander_rmw_changes_only_lcd_power_and_direction_bits(self) -> None:
        self.lib.watcher_test_set_bus_installed(1)
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.lib.watcher_test_write_count(), 2)
        self.assertEqual(self.lib.watcher_test_write_reg(0), 3)
        self.assertEqual(self.lib.watcher_test_write_value(0), 0x06)
        self.assertEqual(self.lib.watcher_test_write_reg(1), 7)
        self.assertEqual(self.lib.watcher_test_write_value(1), 0xF9)

    def test_panel_or_draw_failure_keeps_init_unavailable_and_callback_fails_closed(self) -> None:
        for stage in (2, 3, 4, 5, 6):
            self.lib.watcher_test_reset()
            self.lib.watcher_test_set_bus_installed(1)
            self.lib.watcher_test_set_failure(stage)
            self.assertEqual(self.init(), -2)
            self.assertEqual(self.display(b"idle", b"Ready"), -2)

    def test_display_expression_accepts_only_known_presets_and_reports_render_errors(self) -> None:
        self.lib.watcher_test_set_bus_installed(1)
        self.assertEqual(self.init(), 0)
        self.assertEqual(self.display(b"idle", b"Ready"), 0)
        self.assertEqual(self.display(b"arbitrary-unbounded-preset", b"Ready"), -1)
        self.lib.watcher_test_set_failure(5)
        self.assertEqual(self.display(b"thinking", b"Calling tool..."), -4)

    def test_async_timeout_retains_dma_owned_resources_until_completion(self) -> None:
        self.lib.watcher_test_set_bus_installed(1)
        self.lib.watcher_test_set_failure(7)
        self.assertEqual(self.init(), -2)
        self.assertEqual(self.lib.watcher_test_draw_count(), 1)
        self.assertEqual(self.lib.watcher_test_free_count(), 0)
        self.assertEqual(self.lib.watcher_test_panel_delete_count(), 0)
        self.lib.watcher_test_complete_pending_draw()
        self.assertEqual(self.deinit(), 0)
        self.assertGreater(self.lib.watcher_test_free_count(), 0)
        self.assertGreater(self.lib.watcher_test_panel_delete_count(), 0)


if __name__ == "__main__":
    unittest.main()
