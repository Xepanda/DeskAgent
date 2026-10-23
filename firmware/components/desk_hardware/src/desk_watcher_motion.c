/**
 * @file desk_watcher_motion.c
 * @brief SenseCAP Watcher STM32 Body Coprocessor UART2 Motion Driver Implementation.
 *
 * Implements 0xA5 0x5A binary framing, 12-byte header, little-endian payload,
 * CRC16-CCITT (poly 0x1021, init 0xFFFF), 500ms timeout with up to 2 retries,
 * sequence preservation invariant, and physical limits clamping (Pan 0~180°, Tilt 100~140°).
 *
 * Hardware interface:
 *   ESP32-S3 UART_NUM_2 (TX: GPIO 19, RX: GPIO 20, 921600 bps, 8-N-1).
 */

#include "desk_watcher_motion.h"
#include <math.h>
#include <stdatomic.h>
#include <stdio.h>
#include <string.h>

#ifdef ESP_PLATFORM
#include "driver/uart.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_timer.h"
#endif

#define TAG "desk_watcher_motion"

static bool s_motion_initialized = false;
static atomic_uint_least32_t s_mcu_sequence = ATOMIC_VAR_INIT(0);
static atomic_uint_least32_t s_diag_tx_bytes = ATOMIC_VAR_INIT(0);
static atomic_uint_least32_t s_diag_rx_bytes = ATOMIC_VAR_INIT(0);
static atomic_uint_least32_t s_diag_ack_count = ATOMIC_VAR_INIT(0);
static atomic_uint_least32_t s_diag_nack_count = ATOMIC_VAR_INIT(0);
static atomic_uint_least32_t s_diag_bad_crc_count = ATOMIC_VAR_INIT(0);
static atomic_uint_least32_t s_diag_wrong_sequence_count = ATOMIC_VAR_INIT(0);
static atomic_uint_least32_t s_diag_timeout_count = ATOMIC_VAR_INIT(0);
static atomic_uint_least32_t s_diag_retry_count = ATOMIC_VAR_INIT(0);
static atomic_int s_diag_last_error = ATOMIC_VAR_INIT(DESK_WATCHER_MOTION_ERROR_NONE);
/* Local diagnostic for the one-shot STOP paired with the last ambiguous MOVE. */
static atomic_int s_last_ambiguous_move_stop_result = ATOMIC_VAR_INIT(DESK_HW_ERR_UNAVAILABLE);
/* UART2 may be shared with another initialized component. Only delete a
 * driver instance that this module installed itself. */
#ifdef ESP_PLATFORM
static bool s_uart_installed_by_motion = false;
#endif

#ifdef ESP_PLATFORM
static SemaphoreHandle_t s_motion_mutex = NULL;
#endif

/* API callers can race before they acquire the UART transaction mutex, so
 * sequence allocation must be atomic independently of transport locking. */
static uint32_t motion_next_sequence(void)
{
    return (uint32_t)(atomic_fetch_add_explicit(&s_mcu_sequence, 1U,
                                                memory_order_relaxed) + 1U);
}

static uint16_t motion_calc_crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; ++i) {
        crc ^= (uint16_t)data[i] << 8;
        for (int b = 0; b < 8; ++b) {
            if (crc & 0x8000) {
                crc = (crc << 1) ^ 0x1021;
            } else {
                crc = crc << 1;
            }
        }
    }
    return crc;
}

static size_t motion_pack_move(float pan, float tilt, uint32_t duration_ms, uint32_t seq, uint8_t *out_buf)
{
    /* Do not convert NaN/Inf to an integer: that conversion is undefined. */
    if (!out_buf || !isfinite(pan) || !isfinite(tilt)) return 0;

    /* Mechanical clamping */
    if (pan < DESK_MOTION_PAN_MIN_DEG) pan = DESK_MOTION_PAN_MIN_DEG;
    if (pan > DESK_MOTION_PAN_MAX_DEG) pan = DESK_MOTION_PAN_MAX_DEG;
    if (tilt < DESK_MOTION_TILT_MIN_DEG) tilt = DESK_MOTION_TILT_MIN_DEG;
    if (tilt > DESK_MOTION_TILT_MAX_DEG) tilt = DESK_MOTION_TILT_MAX_DEG;
    if (duration_ms < 100) duration_ms = 100;
    if (duration_ms > 65535) duration_ms = 65535;

    int16_t pan_x10 = (int16_t)(pan * 10.0f + 0.5f);
    int16_t tilt_x10 = (int16_t)(tilt * 10.0f + 0.5f);
    uint16_t dur = (uint16_t)duration_ms;

    out_buf[0] = MCU_FRAME_MAGIC0;        /* 0xA5 */
    out_buf[1] = MCU_FRAME_MAGIC1;        /* 0x5A */
    out_buf[2] = MCU_FRAME_PROTO_VERSION; /* 0x01 */
    out_buf[3] = MCU_FRAME_CLASS_MOTION;  /* 0x02 */
    out_buf[4] = MCU_MOTION_MSG_SERVO_MOVE; /* 0x01 */
    out_buf[5] = MCU_FRAME_FLAG_ACK_REQ;  /* 0x01 */
    out_buf[6] = (uint8_t)(seq & 0xFF);
    out_buf[7] = (uint8_t)((seq >> 8) & 0xFF);
    out_buf[8] = (uint8_t)((seq >> 16) & 0xFF);
    out_buf[9] = (uint8_t)((seq >> 24) & 0xFF);
    out_buf[10] = 0x09; /* payload_len low */
    out_buf[11] = 0x00; /* payload_len high */

    out_buf[12] = MCU_AXIS_MASK_PAN_TILT; /* 0x03 */
    out_buf[13] = (uint8_t)(pan_x10 & 0xFF);
    out_buf[14] = (uint8_t)((pan_x10 >> 8) & 0xFF);
    out_buf[15] = (uint8_t)(tilt_x10 & 0xFF);
    out_buf[16] = (uint8_t)((tilt_x10 >> 8) & 0xFF);
    out_buf[17] = (uint8_t)(dur & 0xFF);
    out_buf[18] = (uint8_t)((dur >> 8) & 0xFF);
    out_buf[19] = 0x00; /* profile: smooth S-curve */
    out_buf[20] = 0x01; /* source */

    uint16_t crc = motion_calc_crc16(out_buf, 21);
    out_buf[21] = (uint8_t)(crc & 0xFF);
    out_buf[22] = (uint8_t)((crc >> 8) & 0xFF);
    return 23;
}

static size_t motion_pack_stop(bool emergency, uint32_t seq, uint8_t *out_buf)
{
    out_buf[0] = MCU_FRAME_MAGIC0;        /* 0xA5 */
    out_buf[1] = MCU_FRAME_MAGIC1;        /* 0x5A */
    out_buf[2] = MCU_FRAME_PROTO_VERSION; /* 0x01 */
    out_buf[3] = MCU_FRAME_CLASS_MOTION;  /* 0x02 */
    out_buf[4] = MCU_MOTION_MSG_SERVO_STOP; /* 0x02 */
    out_buf[5] = MCU_FRAME_FLAG_ACK_REQ;  /* 0x01 */
    out_buf[6] = (uint8_t)(seq & 0xFF);
    out_buf[7] = (uint8_t)((seq >> 8) & 0xFF);
    out_buf[8] = (uint8_t)((seq >> 16) & 0xFF);
    out_buf[9] = (uint8_t)((seq >> 24) & 0xFF);
    out_buf[10] = 0x02; /* payload_len low */
    out_buf[11] = 0x00; /* payload_len high */

    out_buf[12] = emergency ? MCU_STOP_MODE_EMERGENCY : MCU_STOP_MODE_DECEL;
    out_buf[13] = MCU_AXIS_MASK_CANCEL_ALL; /* 0x01 */

    uint16_t crc = motion_calc_crc16(out_buf, 14);
    out_buf[14] = (uint8_t)(crc & 0xFF);
    out_buf[15] = (uint8_t)((crc >> 8) & 0xFF);
    return 16;
}

int desk_watcher_motion_init(void)
{
#ifdef ESP_PLATFORM
    if (s_motion_initialized) return DESK_HW_OK;

    if (!s_motion_mutex) {
        s_motion_mutex = xSemaphoreCreateMutex();
        if (!s_motion_mutex) {
            ESP_LOGE(TAG, "Failed to create motion mutex");
            return DESK_HW_ERR_UNAVAILABLE;
        }
    }

    if (!uart_is_driver_installed(UART_NUM_2)) {
        const uart_config_t config = {
            .baud_rate = DESK_UART_STM32_BAUD, /* 921600 */
            .data_bits = UART_DATA_8_BITS,
            .parity = UART_PARITY_DISABLE,
            .stop_bits = UART_STOP_BITS_1,
            .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
            .source_clk = UART_SCLK_DEFAULT,
        };
        esp_err_t err = uart_param_config(UART_NUM_2, &config);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "UART2 param_config failed: %s", esp_err_to_name(err));
            return DESK_HW_ERR_IO;
        }
        err = uart_set_pin(UART_NUM_2, DESK_PIN_STM32_TX, DESK_PIN_STM32_RX,
                           UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "UART2 set_pin failed: %s", esp_err_to_name(err));
            return DESK_HW_ERR_IO;
        }
        err = uart_driver_install(UART_NUM_2, 512, 512, 10, NULL, 0);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "UART2 driver_install failed: %s", esp_err_to_name(err));
            return DESK_HW_ERR_IO;
        }
        s_uart_installed_by_motion = true;
    }

    s_motion_initialized = true;
    ESP_LOGI(TAG, "Watcher STM32 UART2 motion driver initialized (TX: GPIO%d, RX: GPIO%d, %d bps)",
             DESK_PIN_STM32_TX, DESK_PIN_STM32_RX, DESK_UART_STM32_BAUD);
    return DESK_HW_OK;
#else
    s_motion_initialized = true;
    return DESK_HW_OK;
#endif
}

int desk_watcher_motion_cobs_encode(const uint8_t *src, size_t src_len, uint8_t *dst, size_t dst_len, size_t *out_len)
{
    /* Retained as a standalone compatibility utility; UART transactions use raw frames. */
    if (!src || !dst || !out_len) return -1;
    if (dst_len < src_len + (src_len / 254u) + 1u) return -2;

    size_t read_idx = 0;
    size_t write_idx = 1;
    size_t code_idx = 0;
    uint8_t code = 1;

    while (read_idx < src_len) {
        if (src[read_idx] == 0) {
            dst[code_idx] = code;
            code_idx = write_idx++;
            code = 1;
        } else {
            dst[write_idx++] = src[read_idx];
            code++;
            if (code == 0xFF) {
                dst[code_idx] = code;
                code_idx = write_idx++;
                code = 1;
            }
        }
        read_idx++;
    }
    dst[code_idx] = code;
    *out_len = write_idx;
    return 0;
}

int desk_watcher_motion_cobs_decode(const uint8_t *src, size_t src_len, uint8_t *dst, size_t dst_len, size_t *out_len)
{
    /* Retained as a standalone compatibility utility; UART transactions use raw frames. */
    if (!src || !dst || !out_len) return -1;
    size_t read_idx = 0;
    size_t write_idx = 0;

    while (read_idx < src_len) {
        uint8_t code = src[read_idx++];
        if (code == 0) return -1;
        for (uint8_t i = 1; i < code; i++) {
            if (read_idx >= src_len) return -1;
            if (write_idx >= dst_len) return -2;
            dst[write_idx++] = src[read_idx++];
        }
        if (code < 0xFF && read_idx < src_len) {
            if (write_idx >= dst_len) return -2;
            dst[write_idx++] = 0;
        }
    }
    *out_len = write_idx;
    return 0;
}

int desk_watcher_motion_wire_encode(const uint8_t *raw, size_t raw_len, uint8_t *wire, size_t wire_len, size_t *out_len)
{
    /* Legacy helper only. The ratified STM32 UART protocol has no COBS delimiter. */
    if (!raw || !wire || !out_len || wire_len < raw_len + 2) return -1;
    size_t cobs_len = 0;
    int err = desk_watcher_motion_cobs_encode(raw, raw_len, wire, wire_len - 1, &cobs_len);
    if (err != 0) return err;
    wire[cobs_len] = 0x00;
    *out_len = cobs_len + 1;
    return 0;
}

int desk_watcher_motion_wire_decode(const uint8_t *wire, size_t wire_len, uint8_t *raw, size_t raw_len, size_t *out_len)
{
    /* Legacy helper only. The ratified STM32 UART protocol has no COBS delimiter. */
    if (!wire || !raw || !out_len || wire_len < 2) return -1;
    if (wire[wire_len - 1] != 0x00) return -1;
    return desk_watcher_motion_cobs_decode(wire, wire_len - 1, raw, raw_len, out_len);
}

int desk_watcher_motion_parse_ack(const uint8_t *frame, size_t frame_len,
                                  uint32_t expected_seq, uint8_t expected_msg_id,
                                  uint8_t *out_nack_code, bool *out_is_nack)
{
    if (!frame || !out_nack_code || !out_is_nack || frame_len < 14 ||
        (expected_msg_id != MCU_MOTION_MSG_SERVO_MOVE &&
         expected_msg_id != MCU_MOTION_MSG_SERVO_STOP)) return DESK_HW_ERR_INVALID_ARG;
    *out_nack_code = 0;
    *out_is_nack = false;
    if (frame[0] != MCU_FRAME_MAGIC0 || frame[1] != MCU_FRAME_MAGIC1 ||
        frame[2] != MCU_FRAME_PROTO_VERSION || frame[3] != MCU_FRAME_CLASS_MOTION) {
        return DESK_HW_ERR_IO;
    }

    uint16_t payload_len = (uint16_t)frame[10] | ((uint16_t)frame[11] << 8);
    if (frame_len != (size_t)(12 + payload_len + 2)) return DESK_HW_ERR_IO;
    uint16_t calc_crc = motion_calc_crc16(frame, 12 + payload_len);
    uint16_t rx_crc = (uint16_t)frame[12 + payload_len] | ((uint16_t)frame[13 + payload_len] << 8);
    if (calc_crc != rx_crc) return DESK_HW_ERR_IO;

    uint32_t rx_seq = (uint32_t)frame[6] | ((uint32_t)frame[7] << 8) |
                      ((uint32_t)frame[8] << 16) | ((uint32_t)frame[9] << 24);
    if (rx_seq != expected_seq) return DESK_HW_ERR_BUSY;

    uint8_t flags = frame[5];
    uint8_t msg_id = frame[4];
    if ((flags & MCU_FRAME_FLAG_NACK_RESP) || msg_id == MCU_MOTION_MSG_NACK) {
        if (msg_id != MCU_MOTION_MSG_NACK || flags != MCU_FRAME_FLAG_NACK_RESP ||
            payload_len != 2 || frame[12] == 0) {
            return DESK_HW_ERR_IO;
        }
        *out_nack_code = frame[12];
        *out_is_nack = true;
        return DESK_HW_ERR_IO;
    }
    /* The frozen protocol permits a generic ACK ID or an echo of the
     * acknowledged MOVE/STOP ID. Require command correlation and exact flags. */
    if (flags == MCU_FRAME_FLAG_ACK_RESP && payload_len == 4 &&
        (msg_id == MCU_MOTION_MSG_ACK || msg_id == expected_msg_id)) {
        uint16_t pan_x10 = (uint16_t)frame[12] | ((uint16_t)frame[13] << 8);
        uint16_t tilt_x10 = (uint16_t)frame[14] | ((uint16_t)frame[15] << 8);
        if (pan_x10 > 1800U || tilt_x10 < 1000U || tilt_x10 > 1400U) {
            return DESK_HW_ERR_IO;
        }
        return DESK_HW_OK;
    }
    /* Valid command echoes and unrelated messages do not satisfy the request. */
    return DESK_HW_ERR_BUSY;
}

size_t desk_watcher_motion_pack_move(float pan, float tilt, uint32_t duration_ms, uint32_t seq, uint8_t *out_buf)
{
    return motion_pack_move(pan, tilt, duration_ms, seq, out_buf);
}

size_t desk_watcher_motion_pack_stop(bool emergency, uint32_t seq, uint8_t *out_buf)
{
    return motion_pack_stop(emergency, seq, out_buf);
}

static int motion_send_and_wait_ack(const uint8_t *frame_data, size_t frame_len,
                                    uint32_t expected_seq, bool allow_retries,
                                    bool *out_may_have_sent)
{
    if (out_may_have_sent) *out_may_have_sent = false;
#ifdef ESP_PLATFORM
    if (!s_motion_initialized || !uart_is_driver_installed(UART_NUM_2)) {
        atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_UART_UNAVAILABLE, memory_order_relaxed);
        return DESK_HW_ERR_UNAVAILABLE;
    }

    if (!s_motion_mutex || xSemaphoreTake(s_motion_mutex, pdMS_TO_TICKS(1500)) != pdTRUE) {
        atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_UART_BUSY, memory_order_relaxed);
        return DESK_HW_ERR_BUSY;
    }

    atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_NONE, memory_order_relaxed);
    uint32_t max_attempts = allow_retries ? MCU_MAX_RETRIES + 1U : 1U;
    for (uint32_t attempt = 0; attempt < max_attempts; ++attempt) {
        if (attempt > 0) {
            atomic_fetch_add_explicit(&s_diag_retry_count, 1U, memory_order_relaxed);
            vTaskDelay(pdMS_TO_TICKS(MCU_RETRY_BACKOFF_MS));
        }

        /* Flush RX buffer before transmission */
        uart_flush_input(UART_NUM_2);

        int written = uart_write_bytes(UART_NUM_2, (const char *)frame_data, frame_len);
        if (written > 0) {
            atomic_fetch_add_explicit(&s_diag_tx_bytes, (uint32_t)written, memory_order_relaxed);
        }
        /* A nonzero or indeterminate write result means some command bytes may
         * have reached the peer. Only an exact zero proves no TX occurred. */
        if (written != 0 && out_may_have_sent) *out_may_have_sent = true;
        if (written != (int)frame_len) {
            atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_TX_IO, memory_order_relaxed);
            xSemaphoreGive(s_motion_mutex);
            return DESK_HW_ERR_IO;
        }

        /* Wait up to MCU_ACK_TIMEOUT_MS (500ms) for ACK */
        uint8_t frame_rx[64];
        size_t frame_rx_len = 0;
        size_t expected_frame_len = 0;
        int64_t start_time = esp_timer_get_time();

        while ((esp_timer_get_time() - start_time) < (int64_t)(MCU_ACK_TIMEOUT_MS * 1000)) {
            uint8_t byte = 0;
            int r = uart_read_bytes(UART_NUM_2, &byte, 1, pdMS_TO_TICKS(10));
            if (r <= 0) continue;
            atomic_fetch_add_explicit(&s_diag_rx_bytes, (uint32_t)r, memory_order_relaxed);

            /* Find the native 0xA5 0x5A frame sync, then consume its fixed header. */
            if (frame_rx_len == 0 && byte != MCU_FRAME_MAGIC0) continue;
            if (frame_rx_len == 1 && byte != MCU_FRAME_MAGIC1) {
                frame_rx_len = byte == MCU_FRAME_MAGIC0 ? 1 : 0;
                if (frame_rx_len == 1) frame_rx[0] = byte;
                continue;
            }
            if (frame_rx_len >= sizeof(frame_rx)) {
                frame_rx_len = 0;
                expected_frame_len = 0;
                continue;
            }
            frame_rx[frame_rx_len++] = byte;

            if (frame_rx_len == 12) {
                uint16_t payload_len = (uint16_t)frame_rx[10] | ((uint16_t)frame_rx[11] << 8);
                expected_frame_len = 12 + payload_len + 2;
                if (frame_rx[2] != MCU_FRAME_PROTO_VERSION ||
                    frame_rx[3] != MCU_FRAME_CLASS_MOTION || expected_frame_len > sizeof(frame_rx)) {
                    frame_rx_len = 0;
                    expected_frame_len = 0;
                    continue;
                }
            }

            if (expected_frame_len && frame_rx_len == expected_frame_len) {
                uint8_t nack_code = 0;
                bool is_nack = false;
                int ack_rc = desk_watcher_motion_parse_ack(frame_rx, frame_rx_len,
                                                           expected_seq, frame_data[4],
                                                           &nack_code, &is_nack);
                if (ack_rc == DESK_HW_OK) {
                    atomic_fetch_add_explicit(&s_diag_ack_count, 1U, memory_order_relaxed);
                    atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_NONE, memory_order_relaxed);
                    xSemaphoreGive(s_motion_mutex);
                    return DESK_HW_OK;
                }
                if (ack_rc == DESK_HW_ERR_IO && is_nack) {
                    atomic_fetch_add_explicit(&s_diag_nack_count, 1U, memory_order_relaxed);
                    atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_NACK, memory_order_relaxed);
                    xSemaphoreGive(s_motion_mutex);
                    return (nack_code == MCU_NACK_ERR_OUT_OF_BOUNDS ||
                            nack_code == MCU_NACK_ERR_INVALID_PARAM)
                               ? DESK_HW_ERR_INVALID_ARG : DESK_HW_ERR_IO;
                }
                uint16_t rx_payload_len = (uint16_t)frame_rx[10] | ((uint16_t)frame_rx[11] << 8);
                uint16_t rx_crc = (uint16_t)frame_rx[12 + rx_payload_len] |
                                  ((uint16_t)frame_rx[13 + rx_payload_len] << 8);
                if (motion_calc_crc16(frame_rx, 12 + rx_payload_len) != rx_crc) {
                    atomic_fetch_add_explicit(&s_diag_bad_crc_count, 1U, memory_order_relaxed);
                    atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_BAD_CRC, memory_order_relaxed);
                } else {
                    uint32_t rx_seq = (uint32_t)frame_rx[6] | ((uint32_t)frame_rx[7] << 8) |
                                      ((uint32_t)frame_rx[8] << 16) | ((uint32_t)frame_rx[9] << 24);
                    if (rx_seq != expected_seq) {
                        atomic_fetch_add_explicit(&s_diag_wrong_sequence_count, 1U, memory_order_relaxed);
                        atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_WRONG_SEQUENCE, memory_order_relaxed);
                    } else {
                        atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_INVALID_RESPONSE, memory_order_relaxed);
                    }
                }
                frame_rx_len = 0;
                expected_frame_len = 0;
            }
        }
    }

    xSemaphoreGive(s_motion_mutex);
    atomic_fetch_add_explicit(&s_diag_timeout_count, 1U, memory_order_relaxed);
    atomic_store_explicit(&s_diag_last_error, DESK_WATCHER_MOTION_ERROR_ACK_TIMEOUT, memory_order_relaxed);
    desk_hardware_record_mcu_ack_timeout();
    return DESK_HW_ERR_TIMEOUT;
#else
    (void)frame_data; (void)frame_len; (void)expected_seq; (void)allow_retries;
    return DESK_HW_OK;
#endif
}

int desk_watcher_motion_move(float pan_deg, float tilt_deg, uint32_t duration_ms)
{
    if (!isfinite(pan_deg) || !isfinite(tilt_deg) ||
        pan_deg < DESK_MOTION_PAN_MIN_DEG || pan_deg > DESK_MOTION_PAN_MAX_DEG ||
        tilt_deg < DESK_MOTION_TILT_MIN_DEG || tilt_deg > DESK_MOTION_TILT_MAX_DEG) {
        return DESK_HW_ERR_INVALID_ARG;
    }

    uint8_t tx_buf[32];
    uint32_t seq = motion_next_sequence();
    size_t len = motion_pack_move(pan_deg, tilt_deg, duration_ms, seq, tx_buf);
    if (len == 0) return DESK_HW_ERR_INVALID_ARG;
    atomic_store_explicit(&s_last_ambiguous_move_stop_result,
                          DESK_HW_ERR_UNAVAILABLE, memory_order_relaxed);
    bool may_have_sent = false;
    int result = motion_send_and_wait_ack(tx_buf, len, seq, true, &may_have_sent);
    if (result != DESK_HW_OK && may_have_sent) {
        /* Keep this inside the MOVE driver call: its caller still holds the
         * gimbal owner. Allocate a distinct sequence and never retry STOP. */
        uint8_t stop_buf[32];
        uint32_t stop_seq = motion_next_sequence();
        size_t stop_len = motion_pack_stop(false, stop_seq, stop_buf);
        bool stop_may_have_sent = false;
        int stop_result = motion_send_and_wait_ack(
            stop_buf, stop_len, stop_seq, false, &stop_may_have_sent);
        atomic_store_explicit(&s_last_ambiguous_move_stop_result,
                              stop_result, memory_order_relaxed);
#ifdef ESP_PLATFORM
        ESP_LOGW(TAG, "ambiguous MOVE seq=%lu rc=%d; one-shot STOP seq=%lu rc=%d",
                 (unsigned long)seq, result, (unsigned long)stop_seq,
                 stop_result);
#endif
    }
    return result;
}

int desk_watcher_motion_stop(bool emergency)
{
    uint8_t tx_buf[32];
    uint32_t seq = motion_next_sequence();
    size_t len = motion_pack_stop(emergency, seq, tx_buf);
    return motion_send_and_wait_ack(tx_buf, len, seq, true, NULL);
}

int desk_watcher_motion_get_last_ambiguous_move_stop_result(void)
{
    return atomic_load_explicit(&s_last_ambiguous_move_stop_result,
                                memory_order_relaxed);
}

int desk_watcher_motion_deinit(void)
{
#ifdef ESP_PLATFORM
    if (s_motion_initialized) {
        if (s_uart_installed_by_motion && uart_is_driver_installed(UART_NUM_2)) {
            uart_driver_delete(UART_NUM_2);
            s_uart_installed_by_motion = false;
        }
        if (s_motion_mutex) {
            vSemaphoreDelete(s_motion_mutex);
            s_motion_mutex = NULL;
        }
        s_motion_initialized = false;
    }
#else
    s_motion_initialized = false;
#endif
    return DESK_HW_OK;
}

bool desk_watcher_motion_is_ready(void)
{
    return s_motion_initialized;
}

void desk_watcher_motion_get_diagnostics(desk_watcher_motion_diagnostics_t *out)
{
    if (!out) return;
    out->tx_bytes = atomic_load_explicit(&s_diag_tx_bytes, memory_order_relaxed);
    out->rx_bytes = atomic_load_explicit(&s_diag_rx_bytes, memory_order_relaxed);
    out->ack_count = atomic_load_explicit(&s_diag_ack_count, memory_order_relaxed);
    out->nack_count = atomic_load_explicit(&s_diag_nack_count, memory_order_relaxed);
    out->bad_crc_count = atomic_load_explicit(&s_diag_bad_crc_count, memory_order_relaxed);
    out->wrong_sequence_count = atomic_load_explicit(&s_diag_wrong_sequence_count, memory_order_relaxed);
    out->timeout_count = atomic_load_explicit(&s_diag_timeout_count, memory_order_relaxed);
    out->retry_count = atomic_load_explicit(&s_diag_retry_count, memory_order_relaxed);
    out->last_error = (desk_watcher_motion_error_t)atomic_load_explicit(&s_diag_last_error, memory_order_relaxed);
}
