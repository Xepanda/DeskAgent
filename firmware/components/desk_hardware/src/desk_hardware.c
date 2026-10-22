/**
 * @file desk_hardware.c
 * @brief Implementation of Desk Agent R1 Hardware HAL, Drivers, and STM32 Coprocessor Bridge.
 */

#include "desk_hardware.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/time.h>
#include <time.h>
#include <math.h>

#ifdef ESP_PLATFORM
#include "driver/uart.h"
#endif

#if defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)
#include "desk_watcher_board.h"
#include "desk_watcher_audio.h"
#endif

/* --- Motion & STM32 Link State --- */
static float s_current_pan = 90.0f;
static float s_current_tilt = 120.0f;
static uint32_t s_mcu_seq = 0;
static uint8_t s_last_mcu_frame[64];
static size_t s_last_mcu_frame_len = 0;
static desk_hardware_mcu_tx_cb_t s_mcu_tx_cb = NULL;
static void *s_mcu_tx_user_data = NULL;
static void default_mcu_uart_sender(const uint8_t *frame_data, size_t frame_len, void *user_data);

/* Transaction state and timing */
typedef enum {
    MCU_TX_IDLE = 0,
    MCU_TX_WAITING_ACK,
    MCU_TX_ACKED,
    MCU_TX_NACKED,
    MCU_TX_TIMEOUT
} mcu_tx_state_t;

static volatile mcu_tx_state_t s_pending_state = MCU_TX_IDLE;
static volatile uint32_t s_pending_seq = 0;
static volatile uint8_t s_pending_msg_id = 0;
static volatile uint8_t s_pending_err_code = 0;

static uint32_t s_mcu_ack_timeout_ms = MCU_ACK_TIMEOUT_MS;
static uint32_t s_mcu_retry_backoff_ms = MCU_RETRY_BACKOFF_MS;
static uint32_t s_mcu_max_retries = MCU_MAX_RETRIES;

/* Statistics counters */
static uint32_t s_mcu_tx_count = 0;
static uint32_t s_mcu_retry_count = 0;
static uint32_t s_mcu_ack_count = 0;
static uint32_t s_mcu_nack_count = 0;
static uint32_t s_mcu_timeout_count = 0;
static uint32_t s_mcu_crc_error_count = 0;

static uint64_t mcu_get_time_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return ((uint64_t)tv.tv_sec * 1000ULL) + ((uint64_t)tv.tv_usec / 1000ULL);
}

/* --- Touch & Input State --- */
static desk_touch_event_cb_t s_touch_cb = NULL;
static void *s_touch_cb_user_data = NULL;
static desk_roller_delta_cb_t s_roller_delta_cb = NULL;
static void *s_roller_delta_cb_user_data = NULL;

/* --- Display & Expression State --- */
static char s_current_preset[32] = "idle";
static char s_current_status[64] = "Ready";

/* --- Audio State & Ring Buffer --- */
#define AUDIO_RING_BUFFER_SAMPLES 16384
#ifndef ESP_PLATFORM
/* Test-only ring: never reserve internal device SRAM for injected samples. */
static int16_t s_audio_rec_ring[AUDIO_RING_BUFFER_SAMPLES];
#endif
static size_t s_audio_rec_widx = 0;
static size_t s_audio_rec_ridx = 0;
static size_t s_audio_rec_count = 0;
static uint32_t s_synth_phase = 0;
static desk_hardware_audio_play_cb_t s_audio_play_cb = NULL;
static void *s_audio_play_user_data = NULL;

/* --- Camera State & Frame Buffer --- */
#define CAMERA_MAX_JPEG_LEN 65536
#ifndef ESP_PLATFORM
/* Native camera drivers own their real JPEG buffers. */
static uint8_t s_cam_frame_buf[CAMERA_MAX_JPEG_LEN];
#endif
static size_t s_cam_frame_len = 0;
static uint32_t s_cam_timestamp_ms = 1000;
static bool s_cam_streaming = false;
static float s_cam_target_fps = 0.0f;
static desk_camera_frame_cb_t s_cam_cb = NULL;
static void *s_cam_user_data = NULL;

/* --- STM32 CRC-16 (CCITT-FALSE: init 0xFFFF, poly 0x1021) --- */
static uint16_t mcu_crc16_calc(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFFu;
    for (size_t i = 0; i < len; ++i) {
        crc ^= ((uint16_t)data[i] << 8);
        for (int bit = 0; bit < 8; ++bit) {
            if (crc & 0x8000u) {
                crc = (uint16_t)((crc << 1) ^ 0x1021u);
            } else {
                crc <<= 1;
            }
        }
    }
    return crc;
}

static size_t mcu_frame_pack_motion_with_seq(uint8_t msg_id, uint32_t seq, const uint8_t *payload, uint16_t payload_len, uint8_t *out_buf)
{
    /* Prefix: 12 bytes */
    out_buf[0] = MCU_FRAME_MAGIC0;
    out_buf[1] = MCU_FRAME_MAGIC1;
    out_buf[2] = MCU_FRAME_PROTO_VERSION;
    out_buf[3] = MCU_FRAME_CLASS_MOTION;
    out_buf[4] = msg_id;
    out_buf[5] = MCU_FRAME_FLAG_ACK_REQ; /* 0x01 */

    out_buf[6] = (uint8_t)(seq & 0xFFu);
    out_buf[7] = (uint8_t)((seq >> 8) & 0xFFu);
    out_buf[8] = (uint8_t)((seq >> 16) & 0xFFu);
    out_buf[9] = (uint8_t)((seq >> 24) & 0xFFu);

    out_buf[10] = (uint8_t)(payload_len & 0xFFu);
    out_buf[11] = (uint8_t)((payload_len >> 8) & 0xFFu);

    if (payload_len > 0 && payload) {
        memcpy(&out_buf[12], payload, payload_len);
    }

    /* CRC16 over prefix + payload */
    uint16_t crc = mcu_crc16_calc(out_buf, 12 + payload_len);
    out_buf[12 + payload_len] = (uint8_t)(crc & 0xFFu);
    out_buf[13 + payload_len] = (uint8_t)((crc >> 8) & 0xFFu);

    return 12 + payload_len + 2;
}

int desk_hardware_format_stm32_motion_packet_with_seq(float pan_deg, float tilt_deg, uint32_t duration_ms,
                                                      uint32_t seq,
                                                      uint8_t *out_buf, size_t max_len, size_t *out_len)
{
    if (!out_buf || !out_len || max_len < 23) {
        return DESK_HW_ERR_INVALID_ARG;
    }

    if (pan_deg < DESK_MOTION_PAN_MIN_DEG) pan_deg = DESK_MOTION_PAN_MIN_DEG;
    if (pan_deg > DESK_MOTION_PAN_MAX_DEG) pan_deg = DESK_MOTION_PAN_MAX_DEG;
    if (tilt_deg < DESK_MOTION_TILT_MIN_DEG) tilt_deg = DESK_MOTION_TILT_MIN_DEG;
    if (tilt_deg > DESK_MOTION_TILT_MAX_DEG) tilt_deg = DESK_MOTION_TILT_MAX_DEG;
    if (duration_ms < 100) duration_ms = 100;
    if (duration_ms > 65535) duration_ms = 65535;

    uint8_t payload[9];
    payload[0] = MCU_AXIS_MASK_PAN_TILT; /* 0x03 */
    int16_t x_deg_x10 = (int16_t)(pan_deg * 10.0f + 0.5f);
    int16_t y_deg_x10 = (int16_t)(tilt_deg * 10.0f + 0.5f);
    uint16_t dur = (uint16_t)duration_ms;

    payload[1] = (uint8_t)(x_deg_x10 & 0xFF);
    payload[2] = (uint8_t)((x_deg_x10 >> 8) & 0xFF);
    payload[3] = (uint8_t)(y_deg_x10 & 0xFF);
    payload[4] = (uint8_t)((y_deg_x10 >> 8) & 0xFF);
    payload[5] = (uint8_t)(dur & 0xFF);
    payload[6] = (uint8_t)((dur >> 8) & 0xFF);
    payload[7] = 0x00; /* profile: 0x00 smooth S-curve */
    payload[8] = 0x01; /* source: 0x01 standard execution */

    size_t packed = mcu_frame_pack_motion_with_seq(MCU_MOTION_MSG_SERVO_MOVE, seq, payload, sizeof(payload), out_buf);
    *out_len = packed;
    return DESK_HW_OK;
}

int desk_hardware_format_stm32_motion_packet(float pan_deg, float tilt_deg, uint32_t duration_ms,
                                             uint8_t *out_buf, size_t max_len, size_t *out_len)
{
    s_mcu_seq++;
    return desk_hardware_format_stm32_motion_packet_with_seq(pan_deg, tilt_deg, duration_ms,
                                                             s_mcu_seq,
                                                             out_buf, max_len, out_len);
}

int desk_hardware_format_stm32_stop_packet_with_seq(bool emergency, uint32_t seq,
                                                    uint8_t *out_buf, size_t max_len, size_t *out_len)
{
    if (!out_buf || !out_len || max_len < 16) {
        return DESK_HW_ERR_INVALID_ARG;
    }
    uint8_t payload[2];
    payload[0] = emergency ? MCU_STOP_MODE_EMERGENCY : MCU_STOP_MODE_DECEL; /* 0x02 : 0x01 */
    payload[1] = MCU_AXIS_MASK_CANCEL_ALL; /* 0x01 */

    size_t packed = mcu_frame_pack_motion_with_seq(MCU_MOTION_MSG_SERVO_STOP, seq, payload, sizeof(payload), out_buf);
    *out_len = packed;
    return DESK_HW_OK;
}

int desk_hardware_format_stm32_stop_packet(bool emergency, uint8_t *out_buf, size_t max_len, size_t *out_len)
{
    s_mcu_seq++;
    return desk_hardware_format_stm32_stop_packet_with_seq(emergency, s_mcu_seq, out_buf, max_len, out_len);
}

int desk_hardware_format_stm32_ack_packet(uint32_t seq, int16_t pan_deg_x10, int16_t tilt_deg_x10,
                                          uint8_t *out_buf, size_t max_len, size_t *out_len)
{
    if (!out_buf || !out_len || max_len < 18) {
        return DESK_HW_ERR_INVALID_ARG;
    }
    out_buf[0] = MCU_FRAME_MAGIC0;
    out_buf[1] = MCU_FRAME_MAGIC1;
    out_buf[2] = MCU_FRAME_PROTO_VERSION;
    out_buf[3] = MCU_FRAME_CLASS_MOTION;
    out_buf[4] = MCU_MOTION_MSG_ACK; /* 0x00 */
    out_buf[5] = MCU_FRAME_FLAG_ACK_RESP; /* 0x02 */
    out_buf[6] = (uint8_t)(seq & 0xFFu);
    out_buf[7] = (uint8_t)((seq >> 8) & 0xFFu);
    out_buf[8] = (uint8_t)((seq >> 16) & 0xFFu);
    out_buf[9] = (uint8_t)((seq >> 24) & 0xFFu);
    out_buf[10] = 0x04;
    out_buf[11] = 0x00;

    out_buf[12] = (uint8_t)(pan_deg_x10 & 0xFF);
    out_buf[13] = (uint8_t)((pan_deg_x10 >> 8) & 0xFF);
    out_buf[14] = (uint8_t)(tilt_deg_x10 & 0xFF);
    out_buf[15] = (uint8_t)((tilt_deg_x10 >> 8) & 0xFF);

    uint16_t crc = mcu_crc16_calc(out_buf, 16);
    out_buf[16] = (uint8_t)(crc & 0xFFu);
    out_buf[17] = (uint8_t)((crc >> 8) & 0xFFu);

    *out_len = 18;
    return DESK_HW_OK;
}

int desk_hardware_format_stm32_nack_packet(uint32_t seq, uint8_t error_code, uint8_t sub_error,
                                           uint8_t *out_buf, size_t max_len, size_t *out_len)
{
    if (!out_buf || !out_len || max_len < 16) {
        return DESK_HW_ERR_INVALID_ARG;
    }
    out_buf[0] = MCU_FRAME_MAGIC0;
    out_buf[1] = MCU_FRAME_MAGIC1;
    out_buf[2] = MCU_FRAME_PROTO_VERSION;
    out_buf[3] = MCU_FRAME_CLASS_MOTION;
    out_buf[4] = MCU_MOTION_MSG_NACK; /* 0xFF */
    out_buf[5] = MCU_FRAME_FLAG_NACK_RESP; /* 0x04 */
    out_buf[6] = (uint8_t)(seq & 0xFFu);
    out_buf[7] = (uint8_t)((seq >> 8) & 0xFFu);
    out_buf[8] = (uint8_t)((seq >> 16) & 0xFFu);
    out_buf[9] = (uint8_t)((seq >> 24) & 0xFFu);
    out_buf[10] = 0x02;
    out_buf[11] = 0x00;

    out_buf[12] = error_code;
    out_buf[13] = sub_error;

    uint16_t crc = mcu_crc16_calc(out_buf, 14);
    out_buf[14] = (uint8_t)(crc & 0xFFu);
    out_buf[15] = (uint8_t)((crc >> 8) & 0xFFu);

    *out_len = 16;
    return DESK_HW_OK;
}

uint16_t desk_hardware_calc_crc16(const uint8_t *data, size_t len)
{
    return mcu_crc16_calc(data, len);
}

uint32_t desk_hardware_get_mcu_seq(void)
{
    return s_mcu_seq;
}

void desk_hardware_set_mcu_seq(uint32_t seq)
{
    s_mcu_seq = seq;
}

uint32_t desk_hardware_get_mcu_timeout_ms(void)
{
    return s_mcu_ack_timeout_ms;
}

void desk_hardware_set_mcu_timeout_ms(uint32_t timeout_ms)
{
    s_mcu_ack_timeout_ms = timeout_ms;
}

uint32_t desk_hardware_get_mcu_retry_backoff_ms(void)
{
    return s_mcu_retry_backoff_ms;
}

void desk_hardware_set_mcu_retry_backoff_ms(uint32_t backoff_ms)
{
    s_mcu_retry_backoff_ms = backoff_ms;
}

uint32_t desk_hardware_get_mcu_max_retries(void)
{
    return s_mcu_max_retries;
}

void desk_hardware_set_mcu_max_retries(uint32_t max_retries)
{
    s_mcu_max_retries = max_retries;
}

uint32_t desk_hardware_get_mcu_crc_error_count(void)
{
    return s_mcu_crc_error_count;
}

void desk_hardware_reset_mcu_crc_error_count(void)
{
    s_mcu_crc_error_count = 0;
}

uint32_t desk_hardware_get_mcu_tx_count(void)
{
    return s_mcu_tx_count;
}

uint32_t desk_hardware_get_mcu_retry_count(void)
{
    return s_mcu_retry_count;
}

uint32_t desk_hardware_get_mcu_ack_count(void)
{
    return s_mcu_ack_count;
}

uint32_t desk_hardware_get_mcu_nack_count(void)
{
    return s_mcu_nack_count;
}

uint32_t desk_hardware_get_mcu_timeout_count(void)
{
    return s_mcu_timeout_count;
}

void desk_hardware_reset_mcu_stats(void)
{
    s_mcu_tx_count = 0;
    s_mcu_retry_count = 0;
    s_mcu_ack_count = 0;
    s_mcu_nack_count = 0;
    s_mcu_timeout_count = 0;
    s_mcu_crc_error_count = 0;
}

static const desk_board_driver_ops_t *s_board_driver_ops = NULL;
static bool s_board_driver_initialized = false;
static bool s_strict_hardware_mode = false;
static bool s_display_hw_initialized = false;

#ifndef ESP_PLATFORM
static int s_uart_fd = -1;
static int s_audio_out_fd = -1;
#endif

void desk_hardware_set_strict_mode(bool strict)
{
    s_strict_hardware_mode = strict;
}

bool desk_hardware_is_strict_mode(void)
{
    return s_strict_hardware_mode;
}

void desk_hardware_set_display_initialized(bool init)
{
#ifndef ESP_PLATFORM
    /* Explicit host-side L2 fixture hook. Production ESP builds cannot use it. */
    s_display_hw_initialized = init;
#else
    (void)init;
#endif
}

bool desk_hardware_is_display_initialized(void)
{
    return s_display_hw_initialized;
}

bool desk_hardware_camera_has_frame(void)
{
    return s_cam_frame_len > 0;
}

float desk_hardware_get_pan(void)
{
    return s_current_pan;
}

float desk_hardware_get_tilt(void)
{
    return s_current_tilt;
}

static int default_driver_init(void)
{
#ifndef ESP_PLATFORM
    const char *uart_dev = getenv("DESK_UART_DEVICE");
    if (uart_dev && strlen(uart_dev) > 0 && s_uart_fd < 0) {
        s_uart_fd = open(uart_dev, O_RDWR | O_NOCTTY | O_NDELAY);
    }
    const char *audio_dev = getenv("DESK_AUDIO_OUT_DEVICE");
    if (audio_dev && strlen(audio_dev) > 0 && s_audio_out_fd < 0) {
        s_audio_out_fd = open(audio_dev, O_WRONLY | O_NONBLOCK);
    }
    const char *disp_dev = getenv("DESK_DISPLAY_DEVICE");
    s_display_hw_initialized = (disp_dev && strlen(disp_dev) > 0);
#else
    /* No SenseCAP Watcher display BSP is present in this target. */
    s_display_hw_initialized = false;
#endif
    return 0;
}

static int default_driver_audio_read(int16_t *pcm_buf, size_t samples, size_t *read_samples)
{
    (void)pcm_buf; (void)samples;
    *read_samples = 0;
    return DESK_HW_ERR_UNAVAILABLE;
}

static int default_driver_audio_play(const int16_t *pcm_buf, size_t samples, uint32_t sample_rate_hz)
{
    (void)sample_rate_hz;
    if (!pcm_buf || samples == 0) return 0;
#ifdef ESP_PLATFORM
    (void)pcm_buf;
    (void)samples;
    return DESK_HW_ERR_UNAVAILABLE;
#else
    if (s_audio_out_fd >= 0) {
        size_t bytes_to_write = samples * sizeof(int16_t);
        ssize_t w = write(s_audio_out_fd, pcm_buf, bytes_to_write);
        if (w < 0 && s_strict_hardware_mode) {
            return DESK_HW_ERR_IO;
        }
    } else if (s_strict_hardware_mode) {
        return DESK_HW_ERR_IO;
    }
#endif
    return 0;
}

static bool mcu_transport_is_available(void)
{
#ifdef ESP_PLATFORM
    /* UART2 must have been installed by a board layer before it can be used. */
    if (s_mcu_tx_cb && s_mcu_tx_cb != default_mcu_uart_sender) return true;
    return uart_is_driver_installed(UART_NUM_2);
#else
    if (s_uart_fd >= 0) return true;
    if (s_mcu_tx_cb && s_mcu_tx_cb != default_mcu_uart_sender) return true;
    const char *uart_dev = getenv("DESK_UART_DEVICE");
    if (uart_dev && strlen(uart_dev) > 0) return true;
    return false;
#endif
}

static int mcu_transport_send(const uint8_t *frame_data, size_t frame_len)
{
    if (!frame_data || frame_len == 0) return DESK_HW_ERR_INVALID_ARG;

#ifdef ESP_PLATFORM
    if (s_mcu_tx_cb && s_mcu_tx_cb != default_mcu_uart_sender) {
        s_mcu_tx_cb(frame_data, frame_len, s_mcu_tx_user_data);
        return DESK_HW_OK;
    }
    if (!uart_is_driver_installed(UART_NUM_2)) {
        return DESK_HW_ERR_UNAVAILABLE;
    }
    int written = uart_write_bytes(UART_NUM_2, (const char *)frame_data, frame_len);
    if (written != (int)frame_len) return DESK_HW_ERR_IO;
#else
    if (s_uart_fd >= 0) {
        ssize_t w = write(s_uart_fd, frame_data, frame_len);
        if (w != (ssize_t)frame_len) return DESK_HW_ERR_IO;
    } else {
        const char *uart_dev = getenv("DESK_UART_DEVICE");
        if (uart_dev && strlen(uart_dev) > 0) {
            s_uart_fd = open(uart_dev, O_RDWR | O_NOCTTY | O_NDELAY);
            if (s_uart_fd >= 0) {
                ssize_t w = write(s_uart_fd, frame_data, frame_len);
                if (w != (ssize_t)frame_len) return DESK_HW_ERR_IO;
            } else if (!(s_mcu_tx_cb && s_mcu_tx_cb != default_mcu_uart_sender)) {
                return DESK_HW_ERR_UNAVAILABLE;
            }
        } else if (!(s_mcu_tx_cb && s_mcu_tx_cb != default_mcu_uart_sender)) {
            return DESK_HW_ERR_UNAVAILABLE;
        }
    }
#endif

#ifndef ESP_PLATFORM
    if (s_mcu_tx_cb && s_mcu_tx_cb != default_mcu_uart_sender) {
        s_mcu_tx_cb(frame_data, frame_len, s_mcu_tx_user_data);
    }
#endif
    return DESK_HW_OK;
}

static int mcu_execute_transaction(const uint8_t *frame_data, size_t frame_len, uint32_t expected_seq)
{
    if (!mcu_transport_is_available()) {
#ifdef ESP_PLATFORM
        return DESK_HW_ERR_UNAVAILABLE;
#else
        if (s_strict_hardware_mode) return DESK_HW_ERR_UNAVAILABLE;
        return DESK_HW_OK;
#endif
    }

    s_pending_seq = expected_seq;
    s_pending_msg_id = frame_data[4];
    s_pending_state = MCU_TX_WAITING_ACK;
    s_pending_err_code = 0;

    for (uint32_t attempt = 0; attempt <= s_mcu_max_retries; ++attempt) {
        if (attempt > 0) {
            s_mcu_retry_count++;
            if (s_mcu_retry_backoff_ms > 0) {
                usleep((useconds_t)s_mcu_retry_backoff_ms * 1000);
            }
        }
        s_mcu_tx_count++;
        s_pending_state = MCU_TX_WAITING_ACK;

        int send_rc = mcu_transport_send(frame_data, frame_len);
        if (send_rc != DESK_HW_OK) {
            s_pending_state = MCU_TX_IDLE;
            return send_rc;
        }

        /* Check if ACK was processed synchronously inside TX callback */
        if (s_pending_state == MCU_TX_ACKED) {
            s_pending_state = MCU_TX_IDLE;
            return DESK_HW_OK;
        }
        if (s_pending_state == MCU_TX_NACKED) {
            if (s_pending_err_code == MCU_NACK_ERR_OUT_OF_BOUNDS ||
                s_pending_err_code == MCU_NACK_ERR_INVALID_PARAM) {
                s_pending_state = MCU_TX_IDLE;
                return DESK_HW_ERR_INVALID_ARG;
            }
            continue;
        }

        /* Wait for ACK up to s_mcu_ack_timeout_ms */
        uint64_t start_ms = mcu_get_time_ms();
        while (s_pending_state == MCU_TX_WAITING_ACK) {
            uint64_t now_ms = mcu_get_time_ms();
            if (now_ms - start_ms >= (uint64_t)s_mcu_ack_timeout_ms) {
                break;
            }

#ifndef ESP_PLATFORM
            if (s_uart_fd >= 0) {
                uint8_t rx_buf[64];
                ssize_t n = read(s_uart_fd, rx_buf, sizeof(rx_buf));
                if (n > 0) {
                    desk_hardware_mcu_receive_bytes(rx_buf, (size_t)n);
                }
            }
#else
            uint8_t rx_buf[64];
            int n = uart_read_bytes(UART_NUM_2, rx_buf, sizeof(rx_buf), pdMS_TO_TICKS(5));
            if (n > 0) {
                desk_hardware_mcu_receive_bytes(rx_buf, (size_t)n);
            }
#endif
            if (s_pending_state != MCU_TX_WAITING_ACK) {
                break;
            }
            usleep(2000);
        }

        if (s_pending_state == MCU_TX_ACKED) {
            s_pending_state = MCU_TX_IDLE;
            return DESK_HW_OK;
        }
        if (s_pending_state == MCU_TX_NACKED) {
            if (s_pending_err_code == MCU_NACK_ERR_OUT_OF_BOUNDS ||
                s_pending_err_code == MCU_NACK_ERR_INVALID_PARAM) {
                s_pending_state = MCU_TX_IDLE;
                return DESK_HW_ERR_INVALID_ARG;
            }
            continue;
        }
    }

    s_mcu_timeout_count++;
    s_pending_state = MCU_TX_TIMEOUT;
    return DESK_HW_ERR_TIMEOUT;
}

static int default_driver_motion_move(float pan_deg, float tilt_deg, uint32_t duration_ms)
{
    (void)pan_deg; (void)tilt_deg; (void)duration_ms;
    if (s_last_mcu_frame_len != 23 || s_last_mcu_frame[4] != MCU_MOTION_MSG_SERVO_MOVE) {
        int rc = desk_hardware_format_stm32_motion_packet(pan_deg, tilt_deg, duration_ms,
                                                          s_last_mcu_frame, sizeof(s_last_mcu_frame),
                                                          &s_last_mcu_frame_len);
        if (rc != DESK_HW_OK) return rc;
    }

    uint32_t seq = (uint32_t)s_last_mcu_frame[6] |
                   ((uint32_t)s_last_mcu_frame[7] << 8) |
                   ((uint32_t)s_last_mcu_frame[8] << 16) |
                   ((uint32_t)s_last_mcu_frame[9] << 24);

    return mcu_execute_transaction(s_last_mcu_frame, s_last_mcu_frame_len, seq);
}

static int default_driver_motion_stop(bool emergency)
{
    if (s_last_mcu_frame_len != 16 || s_last_mcu_frame[4] != MCU_MOTION_MSG_SERVO_STOP) {
        int rc = desk_hardware_format_stm32_stop_packet(emergency,
                                                        s_last_mcu_frame, sizeof(s_last_mcu_frame),
                                                        &s_last_mcu_frame_len);
        if (rc != DESK_HW_OK) return rc;
    }

    uint32_t seq = (uint32_t)s_last_mcu_frame[6] |
                   ((uint32_t)s_last_mcu_frame[7] << 8) |
                   ((uint32_t)s_last_mcu_frame[8] << 16) |
                   ((uint32_t)s_last_mcu_frame[9] << 24);

    return mcu_execute_transaction(s_last_mcu_frame, s_last_mcu_frame_len, seq);
}

static int default_driver_camera_capture(uint8_t *out_buf, size_t max_len, size_t *out_len, uint32_t timeout_ms)
{
    (void)timeout_ms;
#ifdef ESP_PLATFORM
    /* On ESP-IDF with Himax HX6538 SPI2 / PTL sensor */
    (void)out_buf; (void)max_len; (void)out_len;
#else
    (void)out_buf; (void)max_len; (void)out_len;
#endif
    return DESK_HW_ERR_UNAVAILABLE;
}

static int default_driver_display_expression(const char *preset_name, const char *status_text)
{
    if (!preset_name || strlen(preset_name) == 0) {
        return DESK_HW_ERR_INVALID_ARG;
    }
    if (s_strict_hardware_mode && !s_display_hw_initialized) {
        return DESK_HW_ERR_UNAVAILABLE;
    }
#ifdef ESP_PLATFORM
    return DESK_HW_ERR_UNAVAILABLE;
#endif
    (void)status_text;
    return DESK_HW_OK;
}

static int default_driver_deinit(void)
{
#ifndef ESP_PLATFORM
    if (s_uart_fd >= 0) {
        close(s_uart_fd);
        s_uart_fd = -1;
    }
    if (s_audio_out_fd >= 0) {
        close(s_audio_out_fd);
        s_audio_out_fd = -1;
    }
    s_display_hw_initialized = false;
#endif
    return 0;
}

static const desk_board_driver_ops_t s_default_esp32_driver_ops = {
    .driver_name = "esp32s3_desk_bsp_v1",
    .init = default_driver_init,
    .audio_record_read = default_driver_audio_read,
    .audio_play_pcm = default_driver_audio_play,
    .motion_move = default_driver_motion_move,
    .motion_stop = default_driver_motion_stop,
    .camera_capture = default_driver_camera_capture,
    .display_expression = default_driver_display_expression,
    .deinit = default_driver_deinit,
};

int desk_hardware_register_driver_ops(const desk_board_driver_ops_t *ops)
{
    if (s_board_driver_ops && s_board_driver_ops->deinit) {
        s_board_driver_ops->deinit();
    }
    s_board_driver_ops = ops ? ops : &s_default_esp32_driver_ops;
    s_board_driver_initialized = false;
    s_display_hw_initialized = false;
    if (s_board_driver_ops && s_board_driver_ops != &s_default_esp32_driver_ops) {
        /* Registration without init means the provider supplied an already-ready driver. */
        int rc = s_board_driver_ops->init ? s_board_driver_ops->init() : DESK_HW_OK;
        if (rc != DESK_HW_OK) return rc;
        s_board_driver_initialized = true;
        s_display_hw_initialized = s_board_driver_ops->display_expression != NULL;
    } else if (s_board_driver_ops == &s_default_esp32_driver_ops && s_board_driver_ops->init) {
        int rc = s_board_driver_ops->init();
        if (rc != DESK_HW_OK) return rc;
    }
    return 0;
}

const desk_board_driver_ops_t *desk_hardware_get_driver_ops(void)
{
    return s_board_driver_ops;
}

static void default_mcu_uart_sender(const uint8_t *frame_data, size_t frame_len, void *user_data)
{
    (void)user_data;
    if (!frame_data || frame_len == 0) return;

    /* Active board driver transmits UART frames directly; fallback sender if no driver */
    if (!s_board_driver_ops || !s_board_driver_ops->motion_move) {
#ifdef ESP_PLATFORM
        /* STM32 Body Coprocessor is wired to UART_NUM_2 (TX: GPIO 19, RX: GPIO 20) @ 921600 */
        uart_write_bytes(UART_NUM_2, (const char *)frame_data, frame_len);
#else
        if (s_uart_fd >= 0) {
            ssize_t w = write(s_uart_fd, frame_data, frame_len);
            (void)w;
        } else {
            const char *uart_dev = getenv("DESK_UART_DEVICE");
            if (uart_dev && strlen(uart_dev) > 0) {
                s_uart_fd = open(uart_dev, O_RDWR | O_NOCTTY | O_NDELAY);
                if (s_uart_fd >= 0) {
                    ssize_t w = write(s_uart_fd, frame_data, frame_len);
                    (void)w;
                }
            }
        }
#endif
    }
}

int desk_hardware_init(void)
{
    desk_arbiter_init();
    desk_hardware_register_driver_ops(&s_default_esp32_driver_ops);
    s_current_pan = 90.0f;
    s_current_tilt = 120.0f;
    s_mcu_seq = 0;
    s_last_mcu_frame_len = 0;
    s_mcu_tx_cb = default_mcu_uart_sender;
    s_mcu_tx_user_data = NULL;
    s_strict_hardware_mode = false;

    s_pending_state = MCU_TX_IDLE;
    s_pending_seq = 0;
    s_pending_err_code = 0;
    s_mcu_ack_timeout_ms = MCU_ACK_TIMEOUT_MS;
    s_mcu_retry_backoff_ms = MCU_RETRY_BACKOFF_MS;
    s_mcu_max_retries = MCU_MAX_RETRIES;
    desk_hardware_reset_mcu_stats();

    strncpy(s_current_preset, "idle", sizeof(s_current_preset) - 1);
    s_current_preset[sizeof(s_current_preset) - 1] = '\0';
    strncpy(s_current_status, "Ready", sizeof(s_current_status) - 1);
    s_current_status[sizeof(s_current_status) - 1] = '\0';

    s_display_hw_initialized = false;
#ifdef ESP_PLATFORM
    s_display_hw_initialized = false;
#else
    const char *disp_dev = getenv("DESK_DISPLAY_DEVICE");
    if (disp_dev && strlen(disp_dev) > 0) {
        s_display_hw_initialized = true;
    }
#endif

    /* Camera sensor starts uninitialized without synthetic dummy picture */
    s_cam_frame_len = 0;
    s_cam_streaming = false;
    s_cam_cb = NULL;

    /* Reset audio ring buffer */
    s_audio_rec_widx = 0;
    s_audio_rec_ridx = 0;
    s_audio_rec_count = 0;
    s_synth_phase = 0;

#if defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)
    /* Keep core/network/CLI startup alive when this board's LCD BSP is missing
     * or its physical expander/panel path cannot be verified. */
    int watcher_rc = desk_hardware_register_driver_ops(desk_watcher_board_get_driver_ops());
    s_display_hw_initialized = desk_watcher_board_display_ready();
    if (watcher_rc != DESK_HW_OK) {
        s_board_driver_initialized = false;
        s_display_hw_initialized = false;
        printf("[HW] SenseCAP Watcher display unavailable (rc=%d); continuing core startup.\n",
               watcher_rc);
    }
#endif

    return 0;
}

int desk_hardware_register_mcu_tx_cb(desk_hardware_mcu_tx_cb_t cb, void *user_data)
{
    s_mcu_tx_cb = cb ? cb : default_mcu_uart_sender;
    s_mcu_tx_user_data = user_data;
    return 0;
}

int desk_hardware_get_last_mcu_frame(uint8_t *out_buf, size_t max_len, size_t *out_len)
{
    if (!out_buf || !out_len) return -1;
    if (max_len < s_last_mcu_frame_len) return -2;
    memcpy(out_buf, s_last_mcu_frame, s_last_mcu_frame_len);
    *out_len = s_last_mcu_frame_len;
    return 0;
}

int desk_hardware_mcu_receive_bytes(const uint8_t *data, size_t len)
{
    if (!data || len < 14) return DESK_HW_ERR_INVALID_ARG;
    if (data[0] != MCU_FRAME_MAGIC0 || data[1] != MCU_FRAME_MAGIC1) return -2;
    if (data[2] != MCU_FRAME_PROTO_VERSION) return -2;

    uint16_t payload_len = (uint16_t)(data[10] | ((uint16_t)data[11] << 8));
    if (len != (size_t)(12 + payload_len + 2)) return -3;

    /* Verify CRC16 */
    uint16_t expected_crc = mcu_crc16_calc(data, 12 + payload_len);
    uint16_t actual_crc = (uint16_t)(data[12 + payload_len] | ((uint16_t)data[13 + payload_len] << 8));
    if (expected_crc != actual_crc) {
        s_mcu_crc_error_count++;
        return -4;
    }

    uint32_t seq = (uint32_t)data[6] |
                   ((uint32_t)data[7] << 8) |
                   ((uint32_t)data[8] << 16) |
                   ((uint32_t)data[9] << 24);

    uint8_t msg_class = data[3];
    uint8_t msg_id = data[4];
    uint8_t flags = data[5];

    if (msg_class != MCU_FRAME_CLASS_MOTION) return DESK_HW_ERR_IO;
    if (s_pending_state != MCU_TX_WAITING_ACK || s_pending_seq != seq) return DESK_HW_ERR_BUSY;

    /* Check for NACK */
    if (msg_id == MCU_MOTION_MSG_NACK || flags == MCU_FRAME_FLAG_NACK_RESP) {
        if (msg_id != MCU_MOTION_MSG_NACK || flags != MCU_FRAME_FLAG_NACK_RESP ||
            payload_len != 2 || data[12] == 0) {
            return DESK_HW_ERR_IO;
        }
        s_mcu_nack_count++;
        s_pending_err_code = data[12];
        s_pending_state = MCU_TX_NACKED;
        return DESK_HW_ERR_IO;
    }

    /* Check for ACK */
    if (flags == MCU_FRAME_FLAG_ACK_RESP && payload_len == 4 &&
        (msg_id == MCU_MOTION_MSG_ACK || msg_id == s_pending_msg_id)) {
        uint16_t pan_x10 = (uint16_t)(data[12] | ((uint16_t)data[13] << 8));
        uint16_t tilt_x10 = (uint16_t)(data[14] | ((uint16_t)data[15] << 8));
        if (pan_x10 > 1800U || tilt_x10 < 1000U || tilt_x10 > 1400U) {
            return DESK_HW_ERR_IO;
        }
        s_mcu_ack_count++;
        s_current_pan = pan_x10 / 10.0f;
        s_current_tilt = tilt_x10 / 10.0f;
        s_pending_state = MCU_TX_ACKED;
        return DESK_HW_OK;
    }
    return DESK_HW_ERR_IO;
}

/* Audio API */
int desk_hardware_audio_record_start(uint32_t sample_rate_hz)
{
#ifdef ESP_PLATFORM
    if (!s_board_driver_initialized || !s_board_driver_ops ||
        !s_board_driver_ops->audio_record_read) {
        return DESK_HW_ERR_UNAVAILABLE;
    }
#endif
    if (desk_arbiter_acquire(DESK_RES_AUDIO_RECORD, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
        return -1;
    }
    return 0;
}

int desk_hardware_audio_record_inject(const int16_t *pcm_buf, size_t samples)
{
    if (!pcm_buf || samples == 0) return DESK_HW_ERR_INVALID_ARG;
#ifdef ESP_PLATFORM
    /* Injection is an L2 host-test facility, never a production microphone source. */
    return DESK_HW_ERR_UNAVAILABLE;
#else
    for (size_t i = 0; i < samples; i++) {
        s_audio_rec_ring[s_audio_rec_widx] = pcm_buf[i];
        s_audio_rec_widx = (s_audio_rec_widx + 1) % AUDIO_RING_BUFFER_SAMPLES;
        if (s_audio_rec_count < AUDIO_RING_BUFFER_SAMPLES) {
            s_audio_rec_count++;
        } else {
            /* Overwriting oldest unread sample */
            s_audio_rec_ridx = (s_audio_rec_ridx + 1) % AUDIO_RING_BUFFER_SAMPLES;
        }
    }
    return 0;
#endif
}

int desk_hardware_audio_record_read(int16_t *pcm_buf, size_t samples, size_t *read_samples)
{
    if (read_samples) *read_samples = 0;
    if (!pcm_buf || !read_samples || samples == 0) return DESK_HW_ERR_INVALID_ARG;

    size_t out_count = 0;
#ifndef ESP_PLATFORM
    /* Read available samples from injected ring buffer */
    while (s_audio_rec_count > 0 && out_count < samples) {
        pcm_buf[out_count++] = s_audio_rec_ring[s_audio_rec_ridx];
        s_audio_rec_ridx = (s_audio_rec_ridx + 1) % AUDIO_RING_BUFFER_SAMPLES;
        s_audio_rec_count--;
    }
#endif

    /* A registered ESP BSP owns the physical mic path. */
#ifdef ESP_PLATFORM
    if (out_count == 0) {
        if (!s_board_driver_initialized || !s_board_driver_ops ||
            !s_board_driver_ops->audio_record_read) {
            *read_samples = 0;
            return DESK_HW_ERR_UNAVAILABLE;
        }
        size_t driver_count = 0;
        int rc = s_board_driver_ops->audio_record_read(pcm_buf, samples, &driver_count);
        if (rc != DESK_HW_OK) return rc;
        if (driver_count > samples) return DESK_HW_ERR_IO;
        if (driver_count == 0) return DESK_HW_ERR_UNAVAILABLE;
        *read_samples = driver_count;
        return DESK_HW_OK;
    }
#endif
    /* Return actual injected samples without synthesizing artificial test tone. */
    *read_samples = out_count;
    return 0;
}

int desk_hardware_audio_record_stop(void)
{
    desk_arbiter_release(DESK_RES_AUDIO_RECORD, DESK_OWNER_TCLAW_VOICE);
    return 0;
}

int desk_hardware_audio_get_status(desk_hardware_audio_status_t *out_status)
{
    if (!out_status) return DESK_HW_ERR_INVALID_ARG;
    memset(out_status, 0, sizeof(*out_status));
#if defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)
    desk_watcher_audio_status(&out_status->ready, &out_status->running,
                              &out_status->frames, &out_status->errors,
                              &out_status->peak);
    return out_status->ready ? DESK_HW_OK : DESK_HW_ERR_UNAVAILABLE;
#else
    return DESK_HW_ERR_UNAVAILABLE;
#endif
}

int desk_hardware_register_audio_play_cb(desk_hardware_audio_play_cb_t cb, void *user_data)
{
    s_audio_play_cb = cb;
    s_audio_play_user_data = user_data;
    return 0;
}

int desk_hardware_audio_play_pcm(const int16_t *pcm_buf, size_t samples, uint32_t sample_rate_hz)
{
#ifdef ESP_PLATFORM
    if (!s_board_driver_initialized || !s_board_driver_ops ||
        !s_board_driver_ops->audio_play_pcm) {
        return DESK_HW_ERR_UNAVAILABLE;
    }
#endif
    desk_resource_owner_t current = desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK);
    if (current == DESK_OWNER_NONE) {
        if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
            return -1;
        }
    }
    if (s_audio_play_cb && pcm_buf && samples > 0) {
        s_audio_play_cb(pcm_buf, samples, sample_rate_hz, s_audio_play_user_data);
    }
    if (s_board_driver_ops && s_board_driver_ops->audio_play_pcm && pcm_buf && samples > 0) {
        int rc = s_board_driver_ops->audio_play_pcm(pcm_buf, samples, sample_rate_hz);
        if (rc != DESK_HW_OK) return rc;
    }
    return 0;
}

int desk_hardware_audio_play_stop(void)
{
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
    return 0;
}

/* Motion / Gimbal API */
int desk_hardware_motion_move_to(float pan_deg, float tilt_deg, uint32_t duration_ms)
{
    if (!isfinite(pan_deg) || !isfinite(tilt_deg)) return DESK_HW_ERR_INVALID_ARG;
#ifdef ESP_PLATFORM
    if (!s_board_driver_ops || !s_board_driver_ops->motion_move) return DESK_HW_ERR_UNAVAILABLE;
#endif
    /* Mechanical clamping */
    if (pan_deg < DESK_MOTION_PAN_MIN_DEG) pan_deg = DESK_MOTION_PAN_MIN_DEG;
    if (pan_deg > DESK_MOTION_PAN_MAX_DEG) pan_deg = DESK_MOTION_PAN_MAX_DEG;
    if (tilt_deg < DESK_MOTION_TILT_MIN_DEG) tilt_deg = DESK_MOTION_TILT_MIN_DEG;
    if (tilt_deg > DESK_MOTION_TILT_MAX_DEG) tilt_deg = DESK_MOTION_TILT_MAX_DEG;

    if (s_strict_hardware_mode) {
#ifndef ESP_PLATFORM
        if (s_uart_fd < 0 && s_mcu_tx_cb == default_mcu_uart_sender) {
            return DESK_HW_ERR_UNAVAILABLE;
        }
#endif
    }

    desk_resource_owner_t current = desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL);
    bool need_release = false;
    if (current == DESK_OWNER_NONE) {
        if (desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE, 50) != DESK_ARBITER_OK) {
            return -1; /* Gimbal locked or busy */
        }
        need_release = true;
    } else if (current != DESK_OWNER_SDK_REMOTE && current != DESK_OWNER_TCLAW_VOICE) {
        return -1; /* Gimbal held by another owner (e.g. SYSTEM_ALERT or LAN_CAMERA lease) */
    }

    desk_hardware_format_stm32_motion_packet(pan_deg, tilt_deg, duration_ms,
                                             s_last_mcu_frame, sizeof(s_last_mcu_frame), &s_last_mcu_frame_len);
    if (s_board_driver_ops && s_board_driver_ops->motion_move != default_driver_motion_move) {
        if (s_mcu_tx_cb && s_mcu_tx_cb != default_mcu_uart_sender) {
            s_mcu_tx_cb(s_last_mcu_frame, s_last_mcu_frame_len, s_mcu_tx_user_data);
        }
    }

    /* interfaces.md 6.1: a driver failure is never reported as success. The
     * driver return value propagates unconditionally; it is NOT gated by
     * s_strict_hardware_mode. */
    int result = DESK_HW_OK;
    if (s_board_driver_ops && s_board_driver_ops->motion_move) {
        result = s_board_driver_ops->motion_move(pan_deg, tilt_deg, duration_ms);
    }

    /* The requested target only becomes the reported position once the driver
     * has confirmed it. A failed move must leave the last successfully
     * confirmed pose intact for desk_hardware_motion_get_position() /
     * desk_hardware_get_pan() / desk_hardware_get_tilt(). */
    if (result == DESK_HW_OK) {
        s_current_pan = pan_deg;
        s_current_tilt = tilt_deg;
    }

    /* Single release point for all exits after acquisition; release() is
     * idempotent and only clears when current_owner == owner. */
    if (need_release) {
        desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE);
    }
    return result;
}

int desk_hardware_motion_stop(bool emergency)
{
#ifdef ESP_PLATFORM
    if (!s_board_driver_ops || !s_board_driver_ops->motion_stop) return DESK_HW_ERR_UNAVAILABLE;
#endif
    desk_hardware_format_stm32_stop_packet(emergency,
                                           s_last_mcu_frame, sizeof(s_last_mcu_frame), &s_last_mcu_frame_len);
    if (s_board_driver_ops && s_board_driver_ops->motion_stop != default_driver_motion_stop) {
        if (s_mcu_tx_cb && s_mcu_tx_cb != default_mcu_uart_sender) {
            s_mcu_tx_cb(s_last_mcu_frame, s_last_mcu_frame_len, s_mcu_tx_user_data);
        }
    }
    int drv_rc = DESK_HW_OK;
    if (s_board_driver_ops && s_board_driver_ops->motion_stop) {
        drv_rc = s_board_driver_ops->motion_stop(emergency);
    }

    desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE);
    desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE);

    /* interfaces.md 6.1: propagate the driver failure unconditionally; the
     * strict-mode flag must not gate error reporting. */
    return drv_rc;
}

int desk_hardware_motion_get_position(float *out_pan, float *out_tilt)
{
    if (!out_pan || !out_tilt) return -1;
    *out_pan = s_current_pan;
    *out_tilt = s_current_tilt;
    return 0;
}

/* Camera API */
int desk_hardware_camera_inject_frame(const uint8_t *jpeg_buf, size_t jpeg_len, uint32_t timestamp_ms)
{
#ifdef ESP_PLATFORM
    (void)jpeg_buf; (void)jpeg_len; (void)timestamp_ms;
    return DESK_HW_ERR_UNAVAILABLE;
#else
    if (!jpeg_buf || jpeg_len == 0 || jpeg_len > CAMERA_MAX_JPEG_LEN) return -1;
    memcpy(s_cam_frame_buf, jpeg_buf, jpeg_len);
    s_cam_frame_len = jpeg_len;
    s_cam_timestamp_ms = timestamp_ms;

    if (s_cam_streaming && s_cam_cb) {
        s_cam_cb(s_cam_frame_buf, s_cam_frame_len, s_cam_timestamp_ms, s_cam_user_data);
    }
    return 0;
#endif
}

int desk_hardware_camera_capture_jpeg(uint8_t *out_buf, size_t max_len, size_t *out_len, uint32_t timeout_ms)
{
    if (out_len) *out_len = 0;
    if (!out_buf || !out_len) return DESK_HW_ERR_INVALID_ARG;
#ifndef ESP_PLATFORM
    /* Host simulator frames are the capture source for L2 tests. The default
     * host driver intentionally reports unavailable, so consume an injected
     * frame before asking that stub. ESP production never accepts injection. */
    if (s_cam_frame_len > 0) {
        if (max_len < s_cam_frame_len) return DESK_HW_ERR_INVALID_ARG;
        memcpy(out_buf, s_cam_frame_buf, s_cam_frame_len);
        *out_len = s_cam_frame_len;
        return DESK_HW_OK;
    }
#endif

    /* First attempt capturing live frame from registered hardware driver */
    if (s_board_driver_ops && s_board_driver_ops->camera_capture) {
        size_t drv_len = 0;
        int drv_ret = s_board_driver_ops->camera_capture(out_buf, max_len, &drv_len, timeout_ms);
        if (drv_ret == 0 && drv_len > 0) {
            *out_len = drv_len;
            return DESK_HW_OK;
        }
        if (drv_ret != DESK_HW_OK) {
            return drv_ret;
        }
        return DESK_HW_ERR_IO;
    }

#ifdef ESP_PLATFORM
    return DESK_HW_ERR_UNAVAILABLE;
#else
    if (s_cam_frame_len == 0) {
        /* No frame in sensor buffer and driver unavailable: return real error, do not fake dummy */
        return DESK_HW_ERR_UNAVAILABLE;
    }

    if (max_len < s_cam_frame_len) return DESK_HW_ERR_INVALID_ARG;

    memcpy(out_buf, s_cam_frame_buf, s_cam_frame_len);
    *out_len = s_cam_frame_len;
    return DESK_HW_OK;
#endif
}

int desk_hardware_camera_stream_start(float target_fps, desk_camera_frame_cb_t cb, void *user_data)
{
#ifdef ESP_PLATFORM
    /* Only single real JPEG capture is supported on-device at present. */
    (void)target_fps; (void)cb; (void)user_data;
    return DESK_HW_ERR_UNAVAILABLE;
#else
    s_cam_target_fps = target_fps;
    s_cam_cb = cb;
    s_cam_user_data = user_data;
    s_cam_streaming = true;
    return 0;
#endif
}

int desk_hardware_camera_stream_pump(void)
{
#ifdef ESP_PLATFORM
    return DESK_HW_ERR_UNAVAILABLE;
#else
    if (!s_cam_streaming || !s_cam_cb) return -1;
    s_cam_timestamp_ms += (uint32_t)(s_cam_target_fps > 0 ? (1000.0f / s_cam_target_fps) : 66);
    s_cam_cb(s_cam_frame_buf, s_cam_frame_len, s_cam_timestamp_ms, s_cam_user_data);
    return 0;
#endif
}

int desk_hardware_camera_stream_stop(void)
{
    s_cam_streaming = false;
    s_cam_cb = NULL;
    s_cam_user_data = NULL;
    return 0;
}

/* Display & Expression API */
int desk_hardware_display_set_expression(const char *preset_name, const char *status_text)
{
    if (!preset_name || strlen(preset_name) == 0) {
        return DESK_HW_ERR_INVALID_ARG;
    }

#ifdef ESP_PLATFORM
    if (!s_display_hw_initialized || !s_board_driver_initialized ||
        !s_board_driver_ops || !s_board_driver_ops->display_expression) {
        return DESK_HW_ERR_UNAVAILABLE;
    }
#else
    if (s_strict_hardware_mode && !s_display_hw_initialized) {
        return DESK_HW_ERR_UNAVAILABLE;
    }
#endif

    desk_resource_owner_t current = desk_arbiter_get_owner(DESK_RES_DISPLAY_UI);
    bool need_release = false;
    if (current == DESK_OWNER_NONE) {
        if (desk_arbiter_acquire(DESK_RES_DISPLAY_UI, DESK_OWNER_IDLE, 50) != DESK_ARBITER_OK) {
            return DESK_HW_ERR_BUSY;
        }
        need_release = true;
    }

    int rc = DESK_HW_OK;
    if (s_board_driver_ops && s_board_driver_ops->display_expression) {
        rc = s_board_driver_ops->display_expression(preset_name, status_text);
    }

    if (rc == DESK_HW_OK) {
        strncpy(s_current_preset, preset_name, sizeof(s_current_preset) - 1);
        s_current_preset[sizeof(s_current_preset) - 1] = '\0';

        if (status_text) {
            strncpy(s_current_status, status_text, sizeof(s_current_status) - 1);
            s_current_status[sizeof(s_current_status) - 1] = '\0';
        } else {
            s_current_status[0] = '\0';
        }
    }

    if (need_release) {
        desk_arbiter_release(DESK_RES_DISPLAY_UI, DESK_OWNER_IDLE);
    }

    return rc;
}

int desk_hardware_set_screen_expression(const char *preset_name, const char *status_text)
{
    return desk_hardware_display_set_expression(preset_name, status_text);
}

int desk_hardware_display_self_test(desk_display_self_test_t *out_result)
{
    bool hw_ok = s_display_hw_initialized;
    int err = DESK_HW_OK;

#ifdef ESP_PLATFORM
    if (!hw_ok || !s_board_driver_initialized || !s_board_driver_ops ||
        !s_board_driver_ops->display_expression) {
        err = DESK_HW_ERR_UNAVAILABLE;
    }
#else
    if (s_strict_hardware_mode && !hw_ok) {
        err = DESK_HW_ERR_UNAVAILABLE;
    }
#endif

    if (out_result) {
        out_result->initialized = hw_ok;
        out_result->strict_mode = s_strict_hardware_mode;
        strncpy(out_result->current_preset, s_current_preset, sizeof(out_result->current_preset) - 1);
        out_result->current_preset[sizeof(out_result->current_preset) - 1] = '\0';
        strncpy(out_result->current_status, s_current_status, sizeof(out_result->current_status) - 1);
        out_result->current_status[sizeof(out_result->current_status) - 1] = '\0';
        out_result->arbiter_owner = desk_arbiter_get_owner(DESK_RES_DISPLAY_UI);
        out_result->last_error_code = err;
    }

    return err;
}

int desk_hardware_screen_self_test(desk_display_self_test_t *out_result)
{
    return desk_hardware_display_self_test(out_result);
}

int desk_hardware_display_get_expression(char *out_preset, size_t max_preset_len, char *out_status, size_t max_status_len)
{
    if (out_preset && max_preset_len > 0) {
        strncpy(out_preset, s_current_preset, max_preset_len - 1);
        out_preset[max_preset_len - 1] = '\0';
    }
    if (out_status && max_status_len > 0) {
        strncpy(out_status, s_current_status, max_status_len - 1);
        out_status[max_status_len - 1] = '\0';
    }
    return 0;
}

int desk_hardware_register_input_cb(desk_touch_event_cb_t cb, void *user_data)
{
    s_touch_cb = cb;
    s_touch_cb_user_data = user_data;
    return 0;
}

int desk_hardware_inject_touch_event(desk_touch_target_t target, desk_touch_action_t action)
{
    if (s_touch_cb) {
        s_touch_cb(target, action, s_touch_cb_user_data);
        return 0;
    }
    return -1;
}

int desk_hardware_feed_touch_event(desk_touch_target_t target, desk_touch_action_t action)
{
    return desk_hardware_inject_touch_event(target, action);
}

int desk_hardware_register_roller_delta_cb(desk_roller_delta_cb_t cb, void *user_data)
{
    s_roller_delta_cb = cb;
    s_roller_delta_cb_user_data = user_data;
    return DESK_HW_OK;
}

int desk_hardware_feed_roller_delta(desk_roller_delta_t delta)
{
    desk_touch_action_t action;
    if (delta == DESK_ROLLER_DELTA_UP) {
        action = DESK_TOUCH_ACTION_SCROLL_UP;
    } else if (delta == DESK_ROLLER_DELTA_DOWN) {
        action = DESK_TOUCH_ACTION_SCROLL_DOWN;
    } else {
        return DESK_HW_ERR_INVALID_ARG;
    }

    int rc = desk_hardware_feed_touch_event(DESK_TOUCH_TARGET_ROLLER, action);
    if (s_roller_delta_cb) {
        s_roller_delta_cb(delta, s_roller_delta_cb_user_data);
    }
    return rc;
}
