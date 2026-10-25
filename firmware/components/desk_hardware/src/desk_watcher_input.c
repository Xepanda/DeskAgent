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
#include "freertos/portmacro.h"
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
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
#define WATCHER_KNOB_TRANSITIONS_PER_DETENT 4

/* Knob Button on PCA9535 Port 0 Bit 3 (Pin 3) */
#define PCA_REG_INPUT0               0x00
#define PCA_REG_CONFIG0              0x06
#define PCA_PIN_BTN_PORT0_MASK       (1u << 3)

static bool s_input_initialized = false;
static volatile bool s_input_task_running = false;
static bool s_touch_i2c_owned = false;
static desk_watcher_input_status_t s_status = {
    .knob = { .state = DESK_WATCHER_INPUT_UNKNOWN },
    .button = { .state = DESK_WATCHER_INPUT_UNKNOWN },
    .touch = { .state = DESK_WATCHER_INPUT_UNKNOWN },
};

#ifdef ESP_PLATFORM
static TaskHandle_t s_input_task_handle = NULL;
static portMUX_TYPE s_status_mux = portMUX_INITIALIZER_UNLOCKED;
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

static int watcher_knob_gpio_init(void)
{
    const gpio_config_t io_conf = {
        .pin_bit_mask = (1ULL << WATCHER_KNOB_PIN_A) | (1ULL << WATCHER_KNOB_PIN_B),
        .mode = GPIO_MODE_INPUT,
        .pull_up_en = GPIO_PULLUP_ENABLE,
        .pull_down_en = GPIO_PULLDOWN_DISABLE,
        .intr_type = GPIO_INTR_DISABLE,
    };
    return (int)gpio_config(&io_conf);
}

static int watcher_btn_expander_init(void)
{
    uint8_t cfg0 = 0;
    int rc = desk_watcher_board_pca_read(PCA_REG_CONFIG0, &cfg0);
    if (rc != DESK_HW_OK) return rc;
    uint8_t next_cfg0 = (uint8_t)(cfg0 | PCA_PIN_BTN_PORT0_MASK); /* Input */
    if (next_cfg0 != cfg0) {
        rc = desk_watcher_board_pca_write(PCA_REG_CONFIG0, next_cfg0);
    }
    return rc;
}

static void watcher_input_record_event_status(desk_watcher_input_channel_status_t *channel,
                                              desk_touch_target_t target,
                                              desk_touch_action_t action)
{
#ifdef ESP_PLATFORM
    portENTER_CRITICAL(&s_status_mux);
#endif
    channel->event_count++;
    s_status.last_event_valid = true;
    s_status.last_target = target;
    s_status.last_action = action;
#ifdef ESP_PLATFORM
    portEXIT_CRITICAL(&s_status_mux);
#endif
}

static void watcher_input_record_event(desk_watcher_input_channel_status_t *channel,
                                       desk_touch_target_t target,
                                       desk_touch_action_t action)
{
    watcher_input_record_event_status(channel, target, action);
    (void)desk_hardware_feed_touch_event(target, action);
}

static bool watcher_knob_decode_step(int previous_state, int current_state,
                                     int *quarter_steps, desk_touch_action_t *action)
{
    if (!quarter_steps || !action || previous_state < 0 || previous_state > 3 ||
        current_state < 0 || current_state > 3) {
        return false;
    }

    int delta = 0;
    if ((previous_state == 0 && current_state == 1) ||
        (previous_state == 1 && current_state == 3) ||
        (previous_state == 3 && current_state == 2) ||
        (previous_state == 2 && current_state == 0)) {
        delta = 1;
    } else if ((previous_state == 0 && current_state == 2) ||
               (previous_state == 2 && current_state == 3) ||
               (previous_state == 3 && current_state == 1) ||
               (previous_state == 1 && current_state == 0)) {
        delta = -1;
    } else if ((previous_state ^ current_state) == 3) {
        /* A skipped/bounced diagonal transition is not a scroll step. */
        *quarter_steps = 0;
    }

    *quarter_steps += delta;
    if (*quarter_steps >= WATCHER_KNOB_TRANSITIONS_PER_DETENT) {
        *quarter_steps = 0;
        *action = DESK_TOUCH_ACTION_SCROLL_UP;
        return true;
    }
    if (*quarter_steps <= -WATCHER_KNOB_TRANSITIONS_PER_DETENT) {
        *quarter_steps = 0;
        *action = DESK_TOUCH_ACTION_SCROLL_DOWN;
        return true;
    }
    return false;
}

static void watcher_input_worker_task(void *arg)
{
    (void)arg;
    int last_state = 0;
    int knob_quarter_steps = 0;
    bool btn_was_pressed = false;
    int64_t btn_press_start_us = 0;

    bool touch_active = false;
    int64_t touch_start_us = 0;
    int touch_start_y = 0;

    if (s_status.knob.state == DESK_WATCHER_INPUT_READY) {
        int cur_a = gpio_get_level(WATCHER_KNOB_PIN_A);
        int cur_b = gpio_get_level(WATCHER_KNOB_PIN_B);
        last_state = (cur_a << 1) | cur_b;
    }

    while (s_input_task_running) {
        vTaskDelay(pdMS_TO_TICKS(25));

        /* --- 1. Knob Quadrature Decoding --- */
        if (s_status.knob.state == DESK_WATCHER_INPUT_READY) {
            int cur_a = gpio_get_level(WATCHER_KNOB_PIN_A);
            int cur_b = gpio_get_level(WATCHER_KNOB_PIN_B);
            int state = (cur_a << 1) | cur_b;

            if (state != last_state) {
                desk_touch_action_t action;
                if (watcher_knob_decode_step(last_state, state, &knob_quarter_steps, &action)) {
                    desk_roller_delta_t delta = action == DESK_TOUCH_ACTION_SCROLL_UP
                        ? DESK_ROLLER_DELTA_UP : DESK_ROLLER_DELTA_DOWN;
                    watcher_input_record_event_status(&s_status.knob, DESK_TOUCH_TARGET_ROLLER, action);
                    (void)desk_hardware_feed_roller_delta(delta);
                }
                last_state = state;
            }
        }

        /* --- 2. Knob Button via PCA9535 Expander --- */
        uint8_t in0 = 0xFF;
        if (s_status.button.state == DESK_WATCHER_INPUT_READY &&
            desk_watcher_board_pca_read(PCA_REG_INPUT0, &in0) == DESK_HW_OK) {
            bool btn_pressed = ((in0 & PCA_PIN_BTN_PORT0_MASK) == 0); /* Active low */

            if (btn_pressed && !btn_was_pressed) {
                btn_was_pressed = true;
                btn_press_start_us = esp_timer_get_time();
            } else if (!btn_pressed && btn_was_pressed) {
                btn_was_pressed = false;
                int64_t duration_ms = (esp_timer_get_time() - btn_press_start_us) / 1000;
                if (duration_ms >= 500) {
                    watcher_input_record_event(&s_status.button, DESK_TOUCH_TARGET_ROLLER,
                                               DESK_TOUCH_ACTION_LONG_PRESS);
                } else if (duration_ms >= 30) {
                    watcher_input_record_event(&s_status.button, DESK_TOUCH_TARGET_ROLLER,
                                               DESK_TOUCH_ACTION_SHORT_PRESS);
                }
            }
        }

        /* --- 3. SPD2010 Touch on I2C1 --- */
        uint8_t touch_reg = 0x00;
        uint8_t touch_data[6] = {0};
        esp_err_t err = ESP_FAIL;
        if (s_status.touch.state == DESK_WATCHER_INPUT_READY) {
            err = i2c_master_write_read_device(WATCHER_TOUCH_I2C_PORT, WATCHER_TOUCH_ADDR,
                                               &touch_reg, 1, touch_data, sizeof(touch_data),
                                               pdMS_TO_TICKS(10));
        }
        if (s_status.touch.state == DESK_WATCHER_INPUT_READY && err == ESP_OK) {
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
                    watcher_input_record_event(&s_status.touch, DESK_TOUCH_TARGET_SCREENTOUCH,
                                               DESK_TOUCH_ACTION_SCROLL_UP);
                } else if (dy > 40) {
                    watcher_input_record_event(&s_status.touch, DESK_TOUCH_TARGET_SCREENTOUCH,
                                               DESK_TOUCH_ACTION_SCROLL_DOWN);
                } else if (duration_ms >= 400) {
                    watcher_input_record_event(&s_status.touch, DESK_TOUCH_TARGET_SCREENTOUCH,
                                               DESK_TOUCH_ACTION_LONG_PRESS);
                } else if (duration_ms >= 20) {
                    watcher_input_record_event(&s_status.touch, DESK_TOUCH_TARGET_SCREENTOUCH,
                                               DESK_TOUCH_ACTION_SHORT_PRESS);
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

    memset(&s_status, 0, sizeof(s_status));
    s_status.knob = (desk_watcher_input_channel_status_t){ .state = DESK_WATCHER_INPUT_INIT };
    s_status.button = (desk_watcher_input_channel_status_t){ .state = DESK_WATCHER_INPUT_INIT };
    s_status.touch = (desk_watcher_input_channel_status_t){ .state = DESK_WATCHER_INPUT_INIT };

    int knob_rc = watcher_knob_gpio_init();
    s_status.knob.state = knob_rc == 0 ? DESK_WATCHER_INPUT_READY : DESK_WATCHER_INPUT_ERROR;
    s_status.knob.error_code = knob_rc;

    int button_rc = watcher_btn_expander_init();
    s_status.button.state = button_rc == DESK_HW_OK ? DESK_WATCHER_INPUT_READY : DESK_WATCHER_INPUT_ERROR;
    s_status.button.error_code = button_rc;

    int touch_rc = (int)watcher_touch_i2c_init();
    s_status.touch.state = touch_rc == 0 ? DESK_WATCHER_INPUT_READY : DESK_WATCHER_INPUT_ERROR;
    s_status.touch.error_code = touch_rc;
    s_touch_i2c_owned = touch_rc == 0;

    if (s_status.knob.state != DESK_WATCHER_INPUT_READY &&
        s_status.button.state != DESK_WATCHER_INPUT_READY &&
        s_status.touch.state != DESK_WATCHER_INPUT_READY) {
        return DESK_HW_ERR_UNAVAILABLE;
    }

    s_input_task_running = true;
    #if defined(CONFIG_ENABLE_EXT_RAM) || defined(CONFIG_ESP32S3_SPIRAM_SUPPORT)
    BaseType_t ret = xTaskCreateWithCaps(watcher_input_worker_task, "watcher_input", 4096, NULL, 3,
                                         &s_input_task_handle, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
#else
    BaseType_t ret = xTaskCreate(watcher_input_worker_task, "watcher_input", 4096, NULL, 3, &s_input_task_handle);
#endif
    if (ret != pdPASS) {
        ESP_LOGE(TAG, "Failed to create input polling task");
        s_input_task_running = false;
        const int task_error = DESK_HW_ERR_UNAVAILABLE;
        if (s_status.knob.state == DESK_WATCHER_INPUT_READY) {
            s_status.knob.state = DESK_WATCHER_INPUT_ERROR;
            s_status.knob.error_code = task_error;
        }
        if (s_status.button.state == DESK_WATCHER_INPUT_READY) {
            s_status.button.state = DESK_WATCHER_INPUT_ERROR;
            s_status.button.error_code = task_error;
        }
        if (s_status.touch.state == DESK_WATCHER_INPUT_READY) {
            s_status.touch.state = DESK_WATCHER_INPUT_ERROR;
            s_status.touch.error_code = task_error;
        }
        return DESK_HW_ERR_UNAVAILABLE;
    }

    s_input_initialized = true;
    ESP_LOGI(TAG, "Watcher input polling active (knob=%d button=%d touch=%d)",
             (int)s_status.knob.state, (int)s_status.button.state, (int)s_status.touch.state);
    return (knob_rc == 0 && button_rc == DESK_HW_OK && touch_rc == 0) ? DESK_HW_OK : DESK_HW_ERR_IO;
#else
    s_status.knob.state = DESK_WATCHER_INPUT_UNKNOWN;
    s_status.button.state = DESK_WATCHER_INPUT_UNKNOWN;
    s_status.touch.state = DESK_WATCHER_INPUT_UNKNOWN;
    s_input_initialized = false;
    return DESK_HW_OK;
#endif
}

int desk_watcher_input_deinit(void)
{
#ifdef ESP_PLATFORM
    if (s_input_initialized) {
        s_input_task_running = false;
        vTaskDelay(pdMS_TO_TICKS(50));
        s_input_initialized = false;
    }
    if (s_touch_i2c_owned) {
        i2c_driver_delete(WATCHER_TOUCH_I2C_PORT);
        s_touch_i2c_owned = false;
    }
#else
    s_input_initialized = false;
#endif
    memset(&s_status, 0, sizeof(s_status));
    s_status.knob.state = DESK_WATCHER_INPUT_UNKNOWN;
    s_status.button.state = DESK_WATCHER_INPUT_UNKNOWN;
    s_status.touch.state = DESK_WATCHER_INPUT_UNKNOWN;
    return DESK_HW_OK;
}

bool desk_watcher_input_is_ready(void)
{
    return s_input_initialized;
}

int desk_watcher_input_get_status(desk_watcher_input_status_t *status)
{
    if (!status) return DESK_HW_ERR_INVALID_ARG;
#ifdef ESP_PLATFORM
    portENTER_CRITICAL(&s_status_mux);
#endif
    *status = s_status;
#ifdef ESP_PLATFORM
    portEXIT_CRITICAL(&s_status_mux);
#endif
    return DESK_HW_OK;
}
