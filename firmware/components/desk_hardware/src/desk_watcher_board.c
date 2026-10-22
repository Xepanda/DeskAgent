/* Native SenseCAP Watcher v1 display adapter.
 * Pin, bus, expander and panel settings follow Seeed's pinned
 * SenseCAP-Watcher-Firmware BSP commit 8e37f7ccd3d836bb99f483e0f6e74e8252406644.
 */
#include "desk_watcher_board.h"
#ifndef DESK_WATCHER_BOARD_TEST
#include "desk_watcher_audio.h"
#include "desk_watcher_motion.h"
#include "desk_watcher_camera.h"
#include "desk_watcher_input.h"
#endif

#ifdef DESK_WATCHER_BOARD_TEST
#include "watcher_board_test_platform.h"
#else
#include "driver/gpio.h"
#include "driver/i2c.h"
#include "driver/ledc.h"
#include "driver/spi_master.h"
#include "esp_err.h"
#include "esp_heap_caps.h"
#include "esp_log.h"
#include "esp_lcd_panel_io.h"
#include "esp_lcd_panel_ops.h"
#include "esp_lcd_panel_vendor.h"
#include "esp_lcd_spd2010.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#endif

#include <ctype.h>
#include <stdio.h>
#include <string.h>

#define TAG "desk_watcher_lcd"

/* SenseCAP Watcher v1 mappings from Seeed's sensecap-watcher.h. */
#define WATCHER_I2C_PORT             I2C_NUM_0
#define WATCHER_I2C_SDA              GPIO_NUM_47
#define WATCHER_I2C_SCL              GPIO_NUM_48
#define WATCHER_I2C_HZ               400000
#define WATCHER_EXPANDER_ADDR        0x21
/* TI PCA9535 datasheet Rev. K, section 8.3.2.3: input 0/1, output 2/3,
 * polarity 4/5, configuration 6/7; configuration 1=input, 0=output. */
#define WATCHER_PCA_REG_INPUT0       0x00
#define WATCHER_PCA_REG_OUTPUT1      0x03
#define WATCHER_PCA_REG_CONFIG1      0x07
#define WATCHER_LCD_POWER_PORT_MASK  (1u << 1) /* PCA9535 port 1, bit 1 */
#define WATCHER_SYSTEM_PORT_MASK     (1u << 2) /* PCA9535 port 1, bit 2 */
#define WATCHER_AUDIO_PA_PORT_MASK   (1u << 4) /* PCA9535 port 1, bit 4 / pin 12 */

#define WATCHER_LCD_SPI_HOST         SPI3_HOST
#define WATCHER_LCD_SCLK             GPIO_NUM_7
#define WATCHER_LCD_DATA0            GPIO_NUM_9
#define WATCHER_LCD_DATA1            GPIO_NUM_1
#define WATCHER_LCD_DATA2            GPIO_NUM_14
#define WATCHER_LCD_DATA3            GPIO_NUM_13
#define WATCHER_LCD_CS               GPIO_NUM_45
#define WATCHER_LCD_BACKLIGHT        GPIO_NUM_8
#define WATCHER_LCD_WIDTH            412
#define WATCHER_LCD_HEIGHT           412
#define WATCHER_LCD_PIXEL_HZ         (40 * 1000 * 1000)
#define WATCHER_LCD_BITS_PER_PIXEL   16
#define WATCHER_LCD_CMD_BITS         32
#define WATCHER_LCD_PARAM_BITS       8
#define WATCHER_LCD_QUEUE_DEPTH      10
#define WATCHER_DRAW_ROWS            8
#define WATCHER_TEXT_SCALE           3
#define WATCHER_TEXT_MAX_CHARS       22
#define WATCHER_LCD_BUFFER_BYTES     (WATCHER_LCD_WIDTH * WATCHER_DRAW_ROWS * sizeof(uint16_t))

#define WATCHER_PCA_TIMEOUT_TICKS    pdMS_TO_TICKS(50)
#define WATCHER_LCD_RAIL_SETTLE_MS   50
#define WATCHER_SYSTEM_RAIL_SETTLE_MS 100

static bool s_i2c_ready;
static bool s_spi_owned;
static bool s_display_ready;
static esp_lcd_panel_io_handle_t s_panel_io;
static esp_lcd_panel_handle_t s_panel;
static uint16_t *s_draw_buffer;
static SemaphoreHandle_t s_draw_done;
static SemaphoreHandle_t s_draw_mutex;
static volatile bool s_draw_inflight;

static esp_err_t watcher_i2c_init_or_reuse(void);

static bool watcher_color_trans_done(esp_lcd_panel_io_handle_t io,
                                     esp_lcd_panel_io_event_data_t *event,
                                     void *user_ctx)
{
    (void)io;
    (void)event;
    (void)user_ctx;
    s_draw_inflight = false;
    BaseType_t higher_priority_woken = pdFALSE;
    if (s_draw_done) xSemaphoreGiveFromISR(s_draw_done, &higher_priority_woken);
    return higher_priority_woken == pdTRUE;
}

typedef struct {
    const char *name;
    uint16_t background;
} watcher_expression_t;

/* A deliberately finite set of safe visual states; unknown names are errors. */
static const watcher_expression_t s_expressions[] = {
    { "idle",      0x0841 },
    { "listening", 0x0320 },
    { "thinking",  0xFD20 },
    { "speaking",  0x041F },
    { "happy",     0xFFE0 },
    { "error",     0xF800 },
    { "offline",   0x7BEF },
};

/* 5x7 column font for A-Z and 0-9; all other printable ASCII uses '?'. */
static const uint8_t s_font_alnum[36][5] = {
    {0x7E,0x11,0x11,0x11,0x7E}, {0x7F,0x49,0x49,0x49,0x36},
    {0x3E,0x41,0x41,0x41,0x22}, {0x7F,0x41,0x41,0x22,0x1C},
    {0x7F,0x49,0x49,0x49,0x41}, {0x7F,0x09,0x09,0x09,0x01},
    {0x3E,0x41,0x49,0x49,0x7A}, {0x7F,0x08,0x08,0x08,0x7F},
    {0x00,0x41,0x7F,0x41,0x00}, {0x20,0x40,0x41,0x3F,0x01},
    {0x7F,0x08,0x14,0x22,0x41}, {0x7F,0x40,0x40,0x40,0x40},
    {0x7F,0x02,0x0C,0x02,0x7F}, {0x7F,0x04,0x08,0x10,0x7F},
    {0x3E,0x41,0x41,0x41,0x3E}, {0x7F,0x09,0x09,0x09,0x06},
    {0x3E,0x41,0x51,0x21,0x5E}, {0x7F,0x09,0x19,0x29,0x46},
    {0x46,0x49,0x49,0x49,0x31}, {0x01,0x01,0x7F,0x01,0x01},
    {0x3F,0x40,0x40,0x40,0x3F}, {0x1F,0x20,0x40,0x20,0x1F},
    {0x3F,0x40,0x38,0x40,0x3F}, {0x63,0x14,0x08,0x14,0x63},
    {0x07,0x08,0x70,0x08,0x07}, {0x61,0x51,0x49,0x45,0x43},
    {0x3E,0x51,0x49,0x45,0x3E}, {0x00,0x42,0x7F,0x40,0x00},
    {0x42,0x61,0x51,0x49,0x46}, {0x21,0x41,0x45,0x4B,0x31},
    {0x18,0x14,0x12,0x7F,0x10}, {0x27,0x45,0x45,0x45,0x39},
    {0x3C,0x4A,0x49,0x49,0x30}, {0x01,0x71,0x09,0x05,0x03},
    {0x36,0x49,0x49,0x49,0x36}, {0x06,0x49,0x49,0x29,0x1E},
};

static esp_err_t watcher_pca_read(uint8_t reg, uint8_t *value)
{
    if (!value || reg > 7) return ESP_ERR_INVALID_ARG;
    return i2c_master_write_read_device(WATCHER_I2C_PORT, WATCHER_EXPANDER_ADDR,
                                        &reg, 1, value, 1, WATCHER_PCA_TIMEOUT_TICKS);
}

static esp_err_t watcher_pca_write(uint8_t reg, uint8_t value)
{
    uint8_t data[2] = { reg, value };
    return i2c_master_write_to_device(WATCHER_I2C_PORT, WATCHER_EXPANDER_ADDR,
                                     data, sizeof(data), WATCHER_PCA_TIMEOUT_TICKS);
}

int desk_watcher_board_i2c_ensure(void)
{
    return watcher_i2c_init_or_reuse() == ESP_OK ? DESK_HW_OK : DESK_HW_ERR_UNAVAILABLE;
}

int desk_watcher_board_i2c_probe(uint8_t address_7bit)
{
    if (address_7bit > 0x7f || watcher_i2c_init_or_reuse() != ESP_OK) {
        return DESK_HW_ERR_INVALID_ARG;
    }
    i2c_cmd_handle_t cmd = i2c_cmd_link_create();
    if (!cmd) return DESK_HW_ERR_UNAVAILABLE;
    esp_err_t err = i2c_master_start(cmd);
    if (err == ESP_OK) err = i2c_master_write_byte(cmd, (uint8_t)(address_7bit << 1), true);
    if (err == ESP_OK) err = i2c_master_stop(cmd);
    if (err == ESP_OK) err = i2c_master_cmd_begin(WATCHER_I2C_PORT, cmd, WATCHER_PCA_TIMEOUT_TICKS);
    i2c_cmd_link_delete(cmd);
    return err == ESP_OK ? DESK_HW_OK : DESK_HW_ERR_UNAVAILABLE;
}

int desk_watcher_board_audio_pa_set(bool enabled)
{
    esp_err_t err = watcher_i2c_init_or_reuse();
    if (err != ESP_OK) return DESK_HW_ERR_UNAVAILABLE;
    uint8_t output1, config1;
    if ((err = watcher_pca_read(WATCHER_PCA_REG_OUTPUT1, &output1)) != ESP_OK ||
        (err = watcher_pca_read(WATCHER_PCA_REG_CONFIG1, &config1)) != ESP_OK) {
        return DESK_HW_ERR_IO;
    }
    if (enabled && ((config1 & WATCHER_SYSTEM_PORT_MASK) != 0 ||
                    (output1 & WATCHER_SYSTEM_PORT_MASK) == 0)) {
        return DESK_HW_ERR_UNAVAILABLE;
    }

    uint8_t next_output = enabled ? (uint8_t)(output1 | WATCHER_AUDIO_PA_PORT_MASK)
                                  : (uint8_t)(output1 & (uint8_t)~WATCHER_AUDIO_PA_PORT_MASK);
    uint8_t next_config = (uint8_t)(config1 & (uint8_t)~WATCHER_AUDIO_PA_PORT_MASK);
    if (next_output != output1 &&
        (err = watcher_pca_write(WATCHER_PCA_REG_OUTPUT1, next_output)) != ESP_OK) {
        return DESK_HW_ERR_IO;
    }
    if (next_config != config1 &&
        (err = watcher_pca_write(WATCHER_PCA_REG_CONFIG1, next_config)) != ESP_OK) {
        return DESK_HW_ERR_IO;
    }
    uint8_t verify_output, verify_config;
    if (watcher_pca_read(WATCHER_PCA_REG_OUTPUT1, &verify_output) != ESP_OK ||
        watcher_pca_read(WATCHER_PCA_REG_CONFIG1, &verify_config) != ESP_OK) {
        return DESK_HW_ERR_IO;
    }
    if ((verify_config & WATCHER_AUDIO_PA_PORT_MASK) != 0 ||
        (((verify_output & WATCHER_AUDIO_PA_PORT_MASK) != 0) != enabled)) {
        return DESK_HW_ERR_IO;
    }
    return DESK_HW_OK;
}

int desk_watcher_board_pca_read(uint8_t reg, uint8_t *value)
{
    return watcher_pca_read(reg, value) == ESP_OK ? DESK_HW_OK : DESK_HW_ERR_IO;
}

int desk_watcher_board_pca_write(uint8_t reg, uint8_t value)
{
    return watcher_pca_write(reg, value) == ESP_OK ? DESK_HW_OK : DESK_HW_ERR_IO;
}

static esp_err_t watcher_i2c_init_or_reuse(void)
{
    if (s_i2c_ready) return ESP_OK;

    /* A harmless input-register pointer probe reuses an already-running master
     * without changing its pins, speed, or controller configuration. */
    const uint8_t input0_reg = WATCHER_PCA_REG_INPUT0;
    esp_err_t err = i2c_master_write_to_device(WATCHER_I2C_PORT, WATCHER_EXPANDER_ADDR,
                                               &input0_reg, 1, WATCHER_PCA_TIMEOUT_TICKS);
    if (err == ESP_ERR_INVALID_STATE) {
        /* No installed master was observed. Initialize the documented Seeed
         * general bus once, then probe the actual expander before any writes. */
        const i2c_config_t config = {
            .mode = I2C_MODE_MASTER,
            .sda_io_num = WATCHER_I2C_SDA,
            .sda_pullup_en = GPIO_PULLUP_DISABLE,
            .scl_io_num = WATCHER_I2C_SCL,
            .scl_pullup_en = GPIO_PULLUP_DISABLE,
            .master.clk_speed = WATCHER_I2C_HZ,
        };
        err = i2c_param_config(WATCHER_I2C_PORT, &config);
        if (err != ESP_OK) return err;
        err = i2c_driver_install(WATCHER_I2C_PORT, I2C_MODE_MASTER, 0, 0,
                                 ESP_INTR_FLAG_SHARED);
        if (err != ESP_OK) return err;
        err = i2c_master_write_to_device(WATCHER_I2C_PORT, WATCHER_EXPANDER_ADDR,
                                         &input0_reg, 1, WATCHER_PCA_TIMEOUT_TICKS);
    }
    if (err != ESP_OK) return err;
    s_i2c_ready = true;
    return ESP_OK;
}

static esp_err_t watcher_expander_prepare_lcd(void)
{
    esp_err_t err = watcher_i2c_init_or_reuse();
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "PCA9535 probe at 0x%02x failed: %s", WATCHER_EXPANDER_ADDR, esp_err_to_name(err));
        return err;
    }

    uint8_t input0, input1, output1, config1;
    if ((err = watcher_pca_read(0, &input0)) != ESP_OK ||
        (err = watcher_pca_read(1, &input1)) != ESP_OK ||
        (err = watcher_pca_read(3, &output1)) != ESP_OK ||
        (err = watcher_pca_read(7, &config1)) != ESP_OK) {
        ESP_LOGE(TAG, "PCA9535 @0x%02x register read failed: %s", WATCHER_EXPANDER_ADDR, esp_err_to_name(err));
        return err;
    }
    ESP_LOGI(TAG, "PCA9535-compatible expander ACK @0x%02x input=%02x%02x",
             WATCHER_EXPANDER_ADDR, input1, input0);

    /* Seeed BSP assigns system rail to PCA bit 10 and LCD rail to bit 9.
     * Raise only the system rail first; preserve every unrelated output and
     * direction bit. Set the output latch before changing its direction. */
    const uint8_t system_output = (uint8_t)(output1 | WATCHER_SYSTEM_PORT_MASK);
    if (system_output != output1) {
        err = watcher_pca_write(WATCHER_PCA_REG_OUTPUT1, system_output);
        if (err != ESP_OK) return err;
    }
    const uint8_t system_config = (uint8_t)(config1 & (uint8_t)~WATCHER_SYSTEM_PORT_MASK);
    if (system_config != config1) {
        err = watcher_pca_write(WATCHER_PCA_REG_CONFIG1, system_config);
        if (err != ESP_OK) return err;
    }

    uint8_t verify_output, verify_config;
    if ((err = watcher_pca_read(3, &verify_output)) != ESP_OK ||
        (err = watcher_pca_read(7, &verify_config)) != ESP_OK) {
        return err;
    }
    if ((verify_output & WATCHER_SYSTEM_PORT_MASK) == 0 ||
        (verify_config & WATCHER_SYSTEM_PORT_MASK) != 0) {
        ESP_LOGE(TAG, "PCA9535 system rail bit 10 failed output-high verification");
        return ESP_ERR_INVALID_STATE;
    }
    vTaskDelay(pdMS_TO_TICKS(WATCHER_SYSTEM_RAIL_SETTLE_MS));

    const uint8_t lcd_output = (uint8_t)(verify_output | WATCHER_LCD_POWER_PORT_MASK);
    if (lcd_output != verify_output) {
        err = watcher_pca_write(WATCHER_PCA_REG_OUTPUT1, lcd_output);
        if (err != ESP_OK) return err;
    }
    const uint8_t lcd_config = (uint8_t)(verify_config & (uint8_t)~WATCHER_LCD_POWER_PORT_MASK);
    if (lcd_config != verify_config) {
        err = watcher_pca_write(WATCHER_PCA_REG_CONFIG1, lcd_config);
        if (err != ESP_OK) return err;
    }

    if ((err = watcher_pca_read(3, &verify_output)) != ESP_OK ||
        (err = watcher_pca_read(7, &verify_config)) != ESP_OK) {
        return err;
    }
    if ((verify_output & WATCHER_LCD_POWER_PORT_MASK) == 0 ||
        (verify_config & WATCHER_LCD_POWER_PORT_MASK) != 0 ||
        (verify_output & WATCHER_SYSTEM_PORT_MASK) == 0 ||
        (verify_config & WATCHER_SYSTEM_PORT_MASK) != 0) {
        return ESP_ERR_INVALID_STATE;
    }

    vTaskDelay(pdMS_TO_TICKS(WATCHER_LCD_RAIL_SETTLE_MS));
    return ESP_OK;
}

static esp_err_t watcher_backlight_init(void)
{
    const ledc_timer_config_t timer = {
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .duty_resolution = LEDC_TIMER_10_BIT,
        .timer_num = LEDC_TIMER_1,
        .freq_hz = 5000,
        .clk_cfg = LEDC_AUTO_CLK,
    };
    const ledc_channel_config_t channel = {
        .gpio_num = WATCHER_LCD_BACKLIGHT,
        .speed_mode = LEDC_LOW_SPEED_MODE,
        .channel = LEDC_CHANNEL_1,
        .intr_type = LEDC_INTR_DISABLE,
        .timer_sel = LEDC_TIMER_1,
        .duty = 0,
        .hpoint = 0,
    };
    esp_err_t err = ledc_timer_config(&timer);
    if (err != ESP_OK) return err;
    return ledc_channel_config(&channel);
}

static esp_err_t watcher_backlight_set(uint32_t percent)
{
    if (percent > 100) percent = 100;
    uint32_t duty = (1024u * percent) / 100u;
    esp_err_t err = ledc_set_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1, duty);
    if (err != ESP_OK) return err;
    return ledc_update_duty(LEDC_LOW_SPEED_MODE, LEDC_CHANNEL_1);
}

static esp_err_t watcher_panel_init(void)
{
    const spi_bus_config_t bus = {
        .sclk_io_num = WATCHER_LCD_SCLK,
        .data0_io_num = WATCHER_LCD_DATA0,
        .data1_io_num = WATCHER_LCD_DATA1,
        .data2_io_num = WATCHER_LCD_DATA2,
        .data3_io_num = WATCHER_LCD_DATA3,
        .max_transfer_sz = WATCHER_LCD_BUFFER_BYTES,
    };
    esp_err_t err = spi_bus_initialize(WATCHER_LCD_SPI_HOST, &bus, SPI_DMA_CH_AUTO);
    if (err != ESP_OK) return err;
    s_spi_owned = true;

    err = watcher_backlight_init();
    if (err != ESP_OK) return err;

    s_draw_done = xSemaphoreCreateBinary();
    s_draw_mutex = xSemaphoreCreateMutex();
    if (!s_draw_done || !s_draw_mutex) return ESP_ERR_NO_MEM;

    const esp_lcd_panel_io_spi_config_t io_config = {
        .cs_gpio_num = WATCHER_LCD_CS,
        .dc_gpio_num = -1,
        .spi_mode = 3,
        .pclk_hz = WATCHER_LCD_PIXEL_HZ,
        .trans_queue_depth = WATCHER_LCD_QUEUE_DEPTH,
        .lcd_cmd_bits = WATCHER_LCD_CMD_BITS,
        .lcd_param_bits = WATCHER_LCD_PARAM_BITS,
        .on_color_trans_done = watcher_color_trans_done,
        .user_ctx = NULL,
        .flags = { .quad_mode = true },
    };
    spd2010_vendor_config_t vendor_config = {
        .flags = { .use_qspi_interface = 1 },
    };
    err = esp_lcd_new_panel_io_spi((esp_lcd_spi_bus_handle_t)WATCHER_LCD_SPI_HOST,
                                   &io_config, &s_panel_io);
    if (err != ESP_OK) return err;

    const esp_lcd_panel_dev_config_t panel_config = {
        .reset_gpio_num = GPIO_NUM_NC,
        .rgb_ele_order = LCD_RGB_ELEMENT_ORDER_RGB,
        .bits_per_pixel = WATCHER_LCD_BITS_PER_PIXEL,
        .vendor_config = &vendor_config,
    };
    err = esp_lcd_new_panel_spd2010(s_panel_io, &panel_config, &s_panel);
    if (err != ESP_OK) return err;
    if ((err = esp_lcd_panel_reset(s_panel)) != ESP_OK) return err;
    if ((err = esp_lcd_panel_init(s_panel)) != ESP_OK) return err;
    if ((err = esp_lcd_panel_mirror(s_panel, false, false)) != ESP_OK) return err;
    return esp_lcd_panel_disp_on_off(s_panel, true);
}

static const uint8_t *watcher_glyph_columns(char ch)
{
    ch = (char)toupper((unsigned char)ch);
    if (ch >= 'A' && ch <= 'Z') return s_font_alnum[(unsigned)(ch - 'A')];
    if (ch >= '0' && ch <= '9') return s_font_alnum[26u + (unsigned)(ch - '0')];
    return NULL;
}

static bool watcher_glyph_pixel(const char *text, int x, int y, int y_origin)
{
    if (y < y_origin || y >= y_origin + 7 * WATCHER_TEXT_SCALE) return false;
    size_t text_len = strlen(text);
    int char_width = 6 * WATCHER_TEXT_SCALE;
    int total_width = (int)text_len * char_width;
    int x_origin = (WATCHER_LCD_WIDTH - total_width) / 2;
    int rel_x = x - x_origin;
    if (rel_x < 0 || rel_x >= total_width) return false;
    size_t char_index = (size_t)(rel_x / char_width);
    int glyph_x = (rel_x % char_width) / WATCHER_TEXT_SCALE;
    int glyph_y = (y - y_origin) / WATCHER_TEXT_SCALE;
    if (glyph_x >= 5 || text[char_index] == ' ') return false;
    const uint8_t *columns = watcher_glyph_columns(text[char_index]);
    if (!columns) {
        static const uint8_t question[5] = {0x02, 0x01, 0x51, 0x09, 0x06};
        columns = question;
    }
    return (columns[glyph_x] & (1u << glyph_y)) != 0;
}

static const watcher_expression_t *watcher_find_expression(const char *name)
{
    if (!name) return NULL;
    for (size_t i = 0; i < sizeof(s_expressions) / sizeof(s_expressions[0]); ++i) {
        if (strcmp(name, s_expressions[i].name) == 0) return &s_expressions[i];
    }
    return NULL;
}

static int s_bloub_frame_tick = 0;

static inline bool bloub_in_capsule(int rx, int ry, int hw, int hh, int cr)
{
    int ax = rx < 0 ? -rx : rx;
    int ay = ry < 0 ? -ry : ry;
    if (ax > hw || ay > hh) return false;
    if (ax > hw - cr && ay > hh - cr) {
        int dx = ax - (hw - cr);
        int dy = ay - (hh - cr);
        return (dx * dx + dy * dy) <= (cr * cr);
    }
    return true;
}

static uint16_t watcher_bloub_render_pixel(const watcher_expression_t *expr,
                                           const char *preset_name,
                                           const char *preset_text,
                                           const char *status_text,
                                           int x, int y, int tick)
{
    /* 1. Outside 412x412 circular screen glass -> black bezel */
    int c_dx = x - 206;
    int c_dy = y - 206;
    if ((c_dx * c_dx + c_dy * c_dy) > (206 * 206)) {
        return 0x0000;
    }

    /* 2. Text display in bottom circular quadrant */
    if (status_text && status_text[0] != '\0') {
        if (watcher_glyph_pixel(status_text, x, y, 310)) return 0xFFFF;
        if (watcher_glyph_pixel(preset_text, x, y, 280)) return 0xCE79;
    } else {
        if (watcher_glyph_pixel(preset_text, x, y, 310)) return 0xFFFF;
    }

    /* 3. Eye centers: Left eye at (146, 170), Right eye at (266, 170) */
    int eye_cy = 170;
    int left_cx = 146;
    int right_cx = 266;

    if (strcmp(preset_name, "happy") == 0) {
        /* Inverted smiling crescent / upward curved arc */
        for (int e = 0; e < 2; ++e) {
            int ecx = (e == 0) ? left_cx : right_cx;
            int rx = x - ecx;
            int ry = y - eye_cy;
            if (rx >= -30 && rx <= 30) {
                int y_arc = -10 + (rx * rx) / 64;
                int diff = ry - y_arc;
                if (diff >= -5 && diff <= 5) {
                    return 0xFFFF;
                }
            }
        }
    } else if (strcmp(preset_name, "error") == 0) {
        /* Diagonal 'X' cross eyes */
        for (int e = 0; e < 2; ++e) {
            int ecx = (e == 0) ? left_cx : right_cx;
            int rx = x - ecx;
            int ry = y - eye_cy;
            if (rx >= -24 && rx <= 24 && ry >= -24 && ry <= 24) {
                int d1 = rx - ry; if (d1 < 0) d1 = -d1;
                int d2 = rx + ry; if (d2 < 0) d2 = -d2;
                if (d1 <= 4 || d2 <= 4) {
                    return 0xFFFF;
                }
            }
        }
    } else if (strcmp(preset_name, "offline") == 0) {
        /* Dim sleeping eyelid slits */
        for (int e = 0; e < 2; ++e) {
            int ecx = (e == 0) ? left_cx : right_cx;
            int rx = x - ecx;
            int ry = y - eye_cy;
            if (rx >= -26 && rx <= 26 && ry >= -3 && ry <= 3) {
                return 0x5AEB;
            }
        }
        return 0x0000;
    } else if (strcmp(preset_name, "thinking") == 0) {
        /* Upward-glancing eyes with undulating thought orbit */
        int w_dx = x - 206;
        int w_dy = y - eye_cy;
        int d2 = w_dx * w_dx + w_dy * w_dy;
        if (d2 >= (72 * 72) && d2 <= (84 * 84)) {
            int wave = (w_dx + w_dy + tick * 6);
            if ((wave & 0x0F) < 8) {
                return 0xFFE0;
            }
        }

        int gaze_x = 12;
        int gaze_y = -12;
        for (int e = 0; e < 2; ++e) {
            int ecx = (e == 0) ? (left_cx + gaze_x) : (right_cx + gaze_x);
            int rx = x - ecx;
            int ry = y - (eye_cy + gaze_y);
            if (bloub_in_capsule(rx, ry, 18, 28, 14)) {
                if (rx >= 4 && rx <= 8 && ry >= -16 && ry <= -12) return 0xFFFF;
                return 0x5DDF;
            }
        }
    } else if (strcmp(preset_name, "listening") == 0) {
        /* Large attentive focused eyes leaning inward */
        for (int e = 0; e < 2; ++e) {
            int ecx = (e == 0) ? (left_cx + 5) : (right_cx - 5);
            int rx = x - ecx;
            int ry = y - eye_cy;
            if (bloub_in_capsule(rx, ry, 26, 42, 20)) {
                if ((rx >= 6 && rx <= 12 && ry >= -22 && ry <= -16) ||
                    (rx >= -12 && rx <= -7 && ry >= 8 && ry <= 13)) {
                    return 0xFFFF;
                }
                return 0x07FF;
            }
        }
    } else if (strcmp(preset_name, "speaking") == 0) {
        /* Rhythmic scaling cadence */
        int bounce = ((tick * 5) % 18) - 9;
        if (bounce < 0) bounce = -bounce;
        int hh = 26 + bounce;
        for (int e = 0; e < 2; ++e) {
            int ecx = (e == 0) ? left_cx : right_cx;
            int rx = x - ecx;
            int ry = y - eye_cy;
            if (bloub_in_capsule(rx, ry, 20, hh, 16)) {
                if (rx >= 4 && rx <= 8 && ry >= -16 && ry <= -12) return 0xFFFF;
                return 0x5DDF;
            }
        }
    } else {
        /* Default / Idle: natural capsule eyes with breathing & blinking */
        bool blink = ((tick % 32) >= 30);
        int hh = blink ? 4 : (32 + ((tick & 4) ? 2 : 0));
        int gaze_x = (((tick / 16) % 3) - 1) * 6;
        for (int e = 0; e < 2; ++e) {
            int ecx = (e == 0) ? (left_cx + gaze_x) : (right_cx + gaze_x);
            int rx = x - ecx;
            int ry = y - eye_cy;
            if (bloub_in_capsule(rx, ry, 22, hh, 16)) {
                if (!blink && rx >= 5 && rx <= 9 && ry >= -18 && ry <= -14) {
                    return 0xFFFF;
                }
                return 0x7FFF;
            }
        }
    }

    return expr->background;
}

static esp_err_t watcher_draw_state(const watcher_expression_t *expression,
                                   const char *preset_name, const char *status)
{
    if (!s_display_ready || !s_panel || !s_draw_buffer || !expression) return ESP_ERR_INVALID_STATE;
    if (!s_draw_mutex || !s_draw_done ||
        xSemaphoreTake(s_draw_mutex, pdMS_TO_TICKS(1000)) != pdTRUE) return ESP_ERR_TIMEOUT;
    if (s_draw_inflight) {
        xSemaphoreGive(s_draw_mutex);
        return ESP_ERR_TIMEOUT;
    }

    char preset_text[WATCHER_TEXT_MAX_CHARS + 1] = {0};
    char status_text[WATCHER_TEXT_MAX_CHARS + 1] = {0};
    size_t preset_len = strlen(preset_name);
    if (preset_len > WATCHER_TEXT_MAX_CHARS) preset_len = WATCHER_TEXT_MAX_CHARS;
    for (size_t i = 0; i < preset_len; ++i) preset_text[i] = (char)toupper((unsigned char)preset_name[i]);
    if (status) {
        size_t status_len = strlen(status);
        if (status_len > WATCHER_TEXT_MAX_CHARS) status_len = WATCHER_TEXT_MAX_CHARS;
        for (size_t i = 0; i < status_len; ++i) {
            unsigned char ch = (unsigned char)status[i];
            status_text[i] = (ch >= 0x20 && ch <= 0x7e) ? (char)ch : '?';
        }
    }

    s_bloub_frame_tick++;
    for (int y0 = 0; y0 < WATCHER_LCD_HEIGHT; y0 += WATCHER_DRAW_ROWS) {
        for (int row = 0; row < WATCHER_DRAW_ROWS; ++row) {
            const int y = y0 + row;
            for (int x = 0; x < WATCHER_LCD_WIDTH; ++x) {
                s_draw_buffer[row * WATCHER_LCD_WIDTH + x] =
                    watcher_bloub_render_pixel(expression, preset_name, preset_text, status_text, x, y, s_bloub_frame_tick);
            }
        }
        (void)xSemaphoreTake(s_draw_done, 0);
        s_draw_inflight = true;
        esp_err_t err = esp_lcd_panel_draw_bitmap(s_panel, 0, y0, WATCHER_LCD_WIDTH,
                                                 y0 + WATCHER_DRAW_ROWS, s_draw_buffer);
        if (err != ESP_OK) {
            s_draw_inflight = false;
            xSemaphoreGive(s_draw_mutex);
            return err;
        }
        if (xSemaphoreTake(s_draw_done, pdMS_TO_TICKS(1000)) != pdTRUE) {
            /* Keep the buffer and panel alive: DMA may still own the buffer. */
            xSemaphoreGive(s_draw_mutex);
            return ESP_ERR_TIMEOUT;
        }
    }
    xSemaphoreGive(s_draw_mutex);
    return ESP_OK;
}

static void watcher_release_panel(void)
{
    s_display_ready = false;
    if (s_draw_inflight) return;
    if (s_panel) {
        esp_lcd_panel_del(s_panel);
        s_panel = NULL;
    }
    if (s_panel_io) {
        esp_lcd_panel_io_del(s_panel_io);
        s_panel_io = NULL;
    }
    if (s_spi_owned) {
        spi_bus_free(WATCHER_LCD_SPI_HOST);
        s_spi_owned = false;
    }
    if (s_draw_buffer) {
        heap_caps_free(s_draw_buffer);
        s_draw_buffer = NULL;
    }
    if (s_draw_done) { vSemaphoreDelete(s_draw_done); s_draw_done = NULL; }
    if (s_draw_mutex) { vSemaphoreDelete(s_draw_mutex); s_draw_mutex = NULL; }
}

static int watcher_display_init(void)
{
    if (s_display_ready) return DESK_HW_OK;
    if (s_draw_inflight) return DESK_HW_ERR_UNAVAILABLE;
    watcher_release_panel();

    s_draw_buffer = heap_caps_malloc(WATCHER_LCD_BUFFER_BYTES, MALLOC_CAP_DMA | MALLOC_CAP_INTERNAL);
    if (!s_draw_buffer) {
        ESP_LOGE(TAG, "LCD DMA line buffer allocation failed (%u bytes)",
                 (unsigned)WATCHER_LCD_BUFFER_BYTES);
        return DESK_HW_ERR_UNAVAILABLE;
    }

    esp_err_t err = watcher_expander_prepare_lcd();
    if (err != ESP_OK) goto fail;
    if ((err = watcher_panel_init()) != ESP_OK) goto fail;

    /* Mark ready only provisionally: registration follows a successful native
     * first-frame draw, so the HAL flag can never mean cached state alone. */
    s_display_ready = true;
    const watcher_expression_t *initial = watcher_find_expression("idle");
    err = watcher_draw_state(initial, "idle", "Ready");
    if (err != ESP_OK) goto fail;
    if ((err = watcher_backlight_set(25)) != ESP_OK) goto fail;
    ESP_LOGI(TAG, "SPD2010 native first frame drawn: 412x412 RGB565 over SPI3/QSPI");
    return DESK_HW_OK;

fail:
    ESP_LOGE(TAG, "Watcher LCD initialization/render failed: %s", esp_err_to_name(err));
    watcher_release_panel();
    return DESK_HW_ERR_UNAVAILABLE;
}

static int watcher_display_expression(const char *preset_name, const char *status_text)
{
    if (!preset_name) return DESK_HW_ERR_INVALID_ARG;
    const watcher_expression_t *expression = watcher_find_expression(preset_name);
    if (!expression) return DESK_HW_ERR_INVALID_ARG;
    if (!s_display_ready) return DESK_HW_ERR_UNAVAILABLE;
    esp_err_t err = watcher_draw_state(expression, preset_name, status_text ? status_text : "");
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPD2010 draw_bitmap failed: %s", esp_err_to_name(err));
        if (err == ESP_ERR_TIMEOUT) {
            s_display_ready = false;
            return DESK_HW_ERR_TIMEOUT;
        }
        return DESK_HW_ERR_IO;
    }
    return DESK_HW_OK;
}

static int watcher_board_deinit(void)
{
#ifndef DESK_WATCHER_BOARD_TEST
    desk_watcher_input_deinit();
    desk_watcher_camera_deinit();
    desk_watcher_motion_deinit();
#endif
    (void)watcher_backlight_set(0);
    watcher_release_panel();
    /* Keep the shared I2C controller installed; the next init re-probes it. */
    s_i2c_ready = false;
    return DESK_HW_OK;
}

bool desk_watcher_board_display_ready(void) { return s_display_ready; }

static int watcher_board_init(void)
{
    int lcd = watcher_display_init();
#ifndef DESK_WATCHER_BOARD_TEST
    int audio = desk_watcher_audio_init();
    int motion = desk_watcher_motion_init();
    int camera = desk_watcher_camera_init();
    int input = desk_watcher_input_init();
    (void)camera;
    (void)input;
    return (lcd == DESK_HW_OK || audio == DESK_HW_OK || motion == DESK_HW_OK) ? DESK_HW_OK : DESK_HW_ERR_UNAVAILABLE;
#else
    return lcd;
#endif
}

static const desk_board_driver_ops_t s_watcher_driver_ops = {
    .driver_name = "sensecap_watcher_spd2010",
    .init = watcher_board_init,
#ifndef DESK_WATCHER_BOARD_TEST
    .audio_record_read = desk_watcher_audio_read,
    .audio_play_pcm = desk_watcher_audio_play,
    .motion_move = desk_watcher_motion_move,
    .motion_stop = desk_watcher_motion_stop,
    .camera_capture = desk_watcher_camera_capture,
#else
    .audio_record_read = NULL,
    .audio_play_pcm = NULL,
    .motion_move = NULL,
    .motion_stop = NULL,
    .camera_capture = NULL,
#endif
    .display_expression = watcher_display_expression,
    .deinit = watcher_board_deinit,
};

const desk_board_driver_ops_t *desk_watcher_board_get_driver_ops(void)
{
    return &s_watcher_driver_ops;
}
