/**
 * @file desk_watcher_input.c
 * @brief SenseCAP Watcher Touch and Rotary Knob Input Implementation.
 *
 * Implements:
 *   - SPD2010 touch controller over I2C1 (SDA: GPIO 39, SCL: GPIO 38, 400kHz, addr 0x53).
 *   - Rotary knob quadrature pulse decoding (GPIO 41 & GPIO 42).
 *   - Knob button press decoding from PCA9535 Port 0 bit 3 (Pin 3).
 *   - Dispatches events via desk_hardware_feed_touch_event().
 */

#include "desk_watcher_input.h"
#include "desk_watcher_board.h"
#include <stdio.h>
#include <string.h>

#ifdef ESP_PLATFORM
#include "driver/i2c.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "esp_log.h"
#include "esp_timer.h"
#endif

#define TAG "desk_watcher_input"

/* SPD2010 Touch on I2C1 */
#define WATCHER_TOUCH_I2C_PORT       I2C_NUM_1
#define WATCHER_TOUCH_I2C_SDA        GPIO_NUM_39
#define WATCHER_TOUCH_I2C_SCL        GPIO_NUM_38
#define WATCHER_TOUCH_I2C_HZ         400000
#define WATCHER_TOUCH_ADDR           0x53

/* Rotary Knob Quadrature on GPIO 41 / 42 */
#define WATCHER_KNOB_PIN_A           GPIO_NUM_41
#define WATCHER_KNOB_PIN_B           GPIO_NUM_42

/* Knob Button on PCA9535 Port 0 Bit 3 (Pin 3) */
#define PCA_REG_INPUT0               0x00
#define PCA_REG_CONFIG0              0x06
#define PCA_PIN_BTN_PORT0_MASK       (1u << 3)

static bool s_input_initialized = false;
static volatile bool s_input_task_running = false;

#ifdef ESP_PLATFORM
static TaskHandle_t s_input_task_handle = NULL;
#endif

extern int desk_watcher_board_pca_read(uint8_t reg, uint8_t *value);
extern int desk_watcher_board_pca_write(uint8_t reg, uint8_t value);

#ifdef ESP_PLATFORM

static esp_err_t watcher_touch_i2c_init(void)
{
    const i2c_config_t conf = {
        .mode = I2C_MODE_MASTER,
        .sda_io_num = WATCHER_TOUCH_I2C_SDA,
        .sda_pullup_en = GPIO_PULLUP_ENABLE,
        .scl_io_num = WATCHER_TOUCH_I2C_SCL,
        .scl_pullup_en = GPIO_PULLUP_ENABLE,
        .master.clk_speed = WATCHER_TOUCH_I2C_HZ,
    };
    esp_err_t err = i2c_param_config(WATCHER_TOUCH_I2C_PORT, &conf);
    if (err != ESP_OK) return err;
    return i2c_driver_install(WATCHER_TOUCH_I2C_PORT, I2C_MODE_MASTER, 0, 0, 0);
}

static void watcher_knob_gpio_init(void)
{
    const gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << WATCHER_KNOB_PIN_A) | (1ULL << WATCHER_KNOB_PIN_B),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    gpio_config(&io_conf);
}

static void watcher_btn_expander_init(void)
{
    uint8_t cfg0 = 0;
    if (desk_watcher_board_pca_read(PCA_REG_CONFIG0, &cfg0) == DESK_HW_OK) {
        uint8_t next_cfg0 = (uint8_t)(cfg0 | PCA_PIN_BTN_PORT0_MASK); /* Input */
        if (next_cfg0 != cfg0) {
            desk_watcher_board_pca_write(PCA_REG_CONFIG0, next_cfg0);
        }
    }
}

static void watcher_input_worker_task(void *arg)
{
    (void)arg;
    int last_state = 0;
    bool btn_was_pressed = false;
    int64_t btn_press_start_us = 0;

    bool touch_active = false;
    int64_t touch_start_us = 0;
    int touch_start_y = 0;

    int cur_a = gpio_get_level(WATCHER_KNOB_PIN_A);
    int cur_b = gpio_get_level(WATCHER_KNOB_PIN_B);
    last_state = (cur_a << 1) | cur_b;

    while (s_input_task_running) {
        vTaskDelay(pdMS_TO_TICKS(25));

        /* --- 1. Knob Quadrature Decoding --- */
        cur_a = gpio_get_level(WATCHER_KNOB_PIN_A);
        cur_b = gpio_get_level(WATCHER_KNOB_PIN_B);
        int state = (cur_a << 1) | cur_b;

        if (state != last_state) {
            /* Quadrature state transition table */
            if ((last_state == 0 && state == 1) || (last_state == 1 && state == 3) ||
                (last_state == 3 && state == 2) || (last_state == 2 && state == 0)) {
                desk_hardware_feed_touch_event(DESK_TOUCH_TARGET_ROLLER, DESK_TOUCH_ACTION_SCROLL_UP);
            } else if ((last_state == 0 && state == 2) || (last_state == 2 && state == 3) ||
                       (last_state == 3 && state == 1) || (last_state == 1 && state == 0)) {
                desk_hardware_feed_touch_event(DESK_TOUCH_TARGET_ROLLER, DESK_TOUCH_ACTION_SCROLL_DOWN);
            }
            last_state = state;
        }

        /* --- 2. Knob Button via PCA9535 Expander --- */
        uint8_t in0 = 0xFF;
        if (desk_watcher_board_pca_read(PCA_REG_INPUT0, &in0) == DESK_HW_OK) {
            bool btn_pressed = ((in0 & PCA_PIN_BTN_PORT0_MASK) == 0); /* Active low */

            if (btn_pressed && !btn_was_pressed) {
                btn_was_pressed = true;
                btn_press_start_us = esp_timer_get_time();
            } else if (!btn_pressed && btn_was_pressed) {
                btn_was_pressed = false;
                int64_t duration_ms = (esp_timer_get_time() - btn_press_start_us) / 1000;
                if (duration_ms >= 500) {
                    desk_hardware_feed_touch_event(DESK_TOUCH_TARGET_ROLLER, DESK_TOUCH_ACTION_LONG_PRESS);
                } else if (duration_ms >= 30) {
                    desk_hardware_feed_touch_event(DESK_TOUCH_TARGET_ROLLER, DESK_TOUCH_ACTION_SHORT_PRESS);
                }
            }
        }

        /* --- 3. SPD2010 Touch on I2C1 --- */
        uint8_t touch_reg = 0x00;
        uint8_t touch_data[6] = {0};
        esp_err_t err = i2c_master_write_read_device(WATCHER_TOUCH_I2C_PORT, WATCHER_TOUCH_ADDR,
                                                    &touch_reg, 1, touch_data, sizeof(touch_data),
                                                    pdMS_TO_TICKS(10));
        if (err == ESP_OK) {
            bool pressed = (touch_data[0] > 0);
            int cur_y = (int)(((touch_data[3] & 0x0F) << 8) | touch_data[4]);

            if (pressed && !touch_active) {
                touch_active = true;
                touch_start_us = esp_timer_get_time();
                touch_start_y = cur_y;
            } else if (!pressed && touch_active) {
                touch_active = false;
                int64_t duration_ms = (esp_timer_get_time() - touch_start_us) / 1000;
                int dy = cur_y - touch_start_y;

                if (dy < -40) {
                    desk_hardware_feed_touch_event(DESK_TOUCH_TARGET_SCREENTOUCH, DESK_TOUCH_ACTION_SCROLL_UP);
                } else if (dy > 40) {
                    desk_hardware_feed_touch_event(DESK_TOUCH_TARGET_SCREENTOUCH, DESK_TOUCH_ACTION_SCROLL_DOWN);
                } else if (duration_ms >= 400) {
                    desk_hardware_feed_touch_event(DESK_TOUCH_TARGET_SCREENTOUCH, DESK_TOUCH_ACTION_LONG_PRESS);
                } else if (duration_ms >= 20) {
                    desk_hardware_feed_touch_event(DESK_TOUCH_TARGET_SCREENTOUCH, DESK_TOUCH_ACTION_SHORT_PRESS);
                }
            }
        }
    }

    vTaskDelete(NULL);
}
#endif

int desk_watcher_input_init(void)
{
#ifdef ESP_PLATFORM
    if (s_input_initialized) return DESK_HW_OK;

    watcher_knob_gpio_init();
    watcher_btn_expander_init();
    (void)watcher_touch_i2c_init();

    s_input_task_running = true;
    BaseType_t ret = xTaskCreate(watcher_input_worker_task, "watcher_input", 2048, NULL, 3, &s_input_task_handle);
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create input polling task");
        s_input_task_running = false;
        return DESK_HW_ERR_UNAVAILABLE;
    }

    s_input_initialized = true;
    ESP_LOGI(TAG, "Watcher input driver initialized (Knob: GPIO 41/42, Touch: I2C1 GPIO 39/38, Btn: PCA9535 bit 3)");
    return DESK_HW_OK;
#else
    s_input_initialized = true;
    return DESK_HW_OK;
#endif
}

int desk_watcher_input_deinit(void)
{
#ifdef ESP_PLATFORM
    if (s_input_initialized) {
        s_input_task_running = false;
        vTaskDelay(pdMS_TO_TICKS(50));
        i2c_driver_delete(WATCHER_TOUCH_I2C_PORT);
        s_input_initialized = false;
    }
#else
    s_input_initialized = false;
#endif
    return DESK_HW_OK;
}

bool desk_watcher_input_is_ready(void)
{
    return s_input_initialized;
}
