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

#ifdef ESP_PLATFORM
#include "driver/uart.h"
#endif

/* --- Motion & STM32 Link State --- */
static float s_current_pan = 90.0f;
static float s_current_tilt = 120.0f;
static uint32_t s_mcu_seq = 0;
static uint8_t s_last_mcu_frame[64];
static size_t s_last_mcu_frame_len = 0;
static desk_hardware_mcu_tx_cb_t s_mcu_tx_cb = NULL;
static void *s_mcu_tx_user_data = NULL;

/* --- Touch & Input State --- */
static desk_touch_event_cb_t s_touch_cb = NULL;
static void *s_touch_cb_user_data = NULL;

/* --- Display & Expression State --- */
static char s_current_preset[32] = "idle";
static char s_current_status[64] = "Ready";

/* --- Audio State & Ring Buffer --- */
#define AUDIO_RING_BUFFER_SAMPLES 16384
static int16_t s_audio_rec_ring[AUDIO_RING_BUFFER_SAMPLES];
static size_t s_audio_rec_widx = 0;
static size_t s_audio_rec_ridx = 0;
static size_t s_audio_rec_count = 0;
static uint32_t s_synth_phase = 0;
static desk_hardware_audio_play_cb_t s_audio_play_cb = NULL;
static void *s_audio_play_user_data = NULL;

/* --- Camera State & Frame Buffer --- */
#define CAMERA_MAX_JPEG_LEN 65536
static uint8_t s_cam_frame_buf[CAMERA_MAX_JPEG_LEN];
static size_t s_cam_frame_len = 0;
static uint32_t s_cam_timestamp_ms = 1000;
static bool s_cam_streaming = false;
static float s_cam_target_fps = 0.0f;
static desk_camera_frame_cb_t s_cam_cb = NULL;
static void *s_cam_user_data = NULL;

/* Standard minimal JFIF JPEG header */
static const uint8_t s_default_minimal_jpeg[] = {
    0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46,
    0x49, 0x46, 0x00, 0x01, 0x01, 0x01, 0x00, 0x48,
    0x00, 0x48, 0x00, 0x00, 0xFF, 0xDB, 0x00, 0x43,
    0x00, 0x08, 0x06, 0x06, 0x07, 0x06, 0x05, 0x08,
    0x07, 0x07, 0x07, 0x09, 0x09, 0x08, 0x0A, 0x0C,
    0x14, 0x0D, 0x0C, 0x0B, 0x0B, 0x0C, 0x19, 0x12,
    0x13, 0x0F, 0x14, 0x1D, 0x1A, 0x1F, 0x1E, 0x1D,
    0x1A, 0x1C, 0x1C, 0x20, 0x24, 0x2E, 0x27, 0x20,
    0x22, 0x2C, 0x23, 0x1C, 0x1C, 0x28, 0x37, 0x29,
    0x2C, 0x30, 0x31, 0x34, 0x34, 0x34, 0x1F, 0x27,
    0x39, 0x3D, 0x38, 0x32, 0x3C, 0x2E, 0x33, 0x34,
    0x32, 0xFF, 0xC0, 0x00, 0x0B, 0x08, 0x00, 0x10,
    0x00, 0x10, 0x01, 0x01, 0x11, 0x00, 0xFF, 0xC4,
    0x00, 0x1F, 0x00, 0x00, 0x01, 0x05, 0x01, 0x01,
    0x01, 0x01, 0x01, 0x01, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x00, 0x01, 0x02, 0x03, 0x04,
    0x05, 0x06, 0x07, 0x08, 0x09, 0x0A, 0x0B, 0xFF,
    0xDA, 0x00, 0x08, 0x01, 0x01, 0x00, 0x00, 0x3F,
    0x00, 0x7F, 0x00, 0xFF, 0xD9
};

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

static size_t mcu_frame_pack_motion(uint8_t msg_id, const uint8_t *payload, uint16_t payload_len, uint8_t *out_buf)
{
    /* Prefix: 12 bytes */
    out_buf[0] = MCU_FRAME_MAGIC0;
    out_buf[1] = MCU_FRAME_MAGIC1;
    out_buf[2] = MCU_FRAME_PROTO_VERSION;
    out_buf[3] = MCU_FRAME_CLASS_MOTION;
    out_buf[4] = msg_id;
    out_buf[5] = 0x01; /* MCU_FRAME_FLAG_ACK_REQ */

    s_mcu_seq++;
    out_buf[6] = (uint8_t)(s_mcu_seq & 0xFFu);
    out_buf[7] = (uint8_t)((s_mcu_seq >> 8) & 0xFFu);
    out_buf[8] = (uint8_t)((s_mcu_seq >> 16) & 0xFFu);
    out_buf[9] = (uint8_t)((s_mcu_seq >> 24) & 0xFFu);

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

static const desk_board_driver_ops_t *s_board_driver_ops = NULL;

#ifndef ESP_PLATFORM
static int s_uart_fd = -1;
static int s_audio_out_fd = -1;
#endif

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
#endif
    return 0;
}

static int default_driver_audio_read(int16_t *pcm_buf, size_t samples, size_t *read_samples)
{
    (void)pcm_buf; (void)samples;
    *read_samples = 0;
    return 0;
}

static int default_driver_audio_play(const int16_t *pcm_buf, size_t samples, uint32_t sample_rate_hz)
{
    (void)sample_rate_hz;
    if (!pcm_buf || samples == 0) return 0;
#ifdef ESP_PLATFORM
    /* On ESP-IDF, write to I2S speaker peripheral if available */
#else
    if (s_audio_out_fd >= 0) {
        size_t bytes_to_write = samples * sizeof(int16_t);
        ssize_t w = write(s_audio_out_fd, pcm_buf, bytes_to_write);
        (void)w;
    }
#endif
    return 0;
}

static int default_driver_motion_move(float pan_deg, float tilt_deg, uint32_t duration_ms)
{
    (void)pan_deg; (void)tilt_deg; (void)duration_ms;
    return 0;
}

static int default_driver_motion_stop(bool emergency)
{
    (void)emergency;
    return 0;
}

static int default_driver_camera_capture(uint8_t *out_buf, size_t max_len, size_t *out_len)
{
#ifdef ESP_PLATFORM
    /* On ESP-IDF with esp-camera component */
    (void)out_buf; (void)max_len; (void)out_len;
#else
    (void)out_buf; (void)max_len; (void)out_len;
#endif
    return -1;
}

static int default_driver_display_expression(const char *preset_name, const char *status_text)
{
    (void)preset_name; (void)status_text;
    return 0;
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
    if (s_board_driver_ops && s_board_driver_ops->init) {
        s_board_driver_ops->init();
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

#ifdef ESP_PLATFORM
    uart_write_bytes(UART_NUM_1, (const char *)frame_data, frame_len);
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

    /* Initialize default camera frame */
    memcpy(s_cam_frame_buf, s_default_minimal_jpeg, sizeof(s_default_minimal_jpeg));
    s_cam_frame_len = sizeof(s_default_minimal_jpeg);
    s_cam_streaming = false;
    s_cam_cb = NULL;

    /* Reset audio ring buffer */
    s_audio_rec_widx = 0;
    s_audio_rec_ridx = 0;
    s_audio_rec_count = 0;
    s_synth_phase = 0;

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
    if (!data || len < 14) return -1;
    if (data[0] != MCU_FRAME_MAGIC0 || data[1] != MCU_FRAME_MAGIC1) return -2;

    uint16_t payload_len = (uint16_t)(data[10] | (data[11] << 8));
    if (len < (size_t)(12 + payload_len + 2)) return -3;

    /* Verify CRC16 */
    uint16_t expected_crc = mcu_crc16_calc(data, 12 + payload_len);
    uint16_t actual_crc = (uint16_t)(data[12 + payload_len] | (data[13 + payload_len] << 8));
    if (expected_crc != actual_crc) return -4;

    uint8_t msg_class = data[3];
    if (msg_class == MCU_FRAME_CLASS_MOTION) {
        if (payload_len >= 4) {
            int16_t pan_x10 = (int16_t)(data[12] | (data[13] << 8));
            int16_t tilt_x10 = (int16_t)(data[14] | (data[15] << 8));
            s_current_pan = pan_x10 / 10.0f;
            s_current_tilt = tilt_x10 / 10.0f;
        }
    }
    return 0;
}

/* Audio API */
int desk_hardware_audio_record_start(uint32_t sample_rate_hz)
{
    if (desk_arbiter_acquire(DESK_RES_AUDIO_RECORD, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
        return -1;
    }
    return 0;
}

int desk_hardware_audio_record_inject(const int16_t *pcm_buf, size_t samples)
{
    if (!pcm_buf || samples == 0) return -1;
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
}

int desk_hardware_audio_record_read(int16_t *pcm_buf, size_t samples, size_t *read_samples)
{
    if (!pcm_buf || !read_samples) return -1;

    size_t out_count = 0;
    /* Read available samples from injected ring buffer */
    while (s_audio_rec_count > 0 && out_count < samples) {
        pcm_buf[out_count++] = s_audio_rec_ring[s_audio_rec_ridx];
        s_audio_rec_ridx = (s_audio_rec_ridx + 1) % AUDIO_RING_BUFFER_SAMPLES;
        s_audio_rec_count--;
    }

    /* Return actual samples read without synthesizing artificial 440Hz test sine tone */
    *read_samples = out_count;
    return 0;
}

int desk_hardware_audio_record_stop(void)
{
    desk_arbiter_release(DESK_RES_AUDIO_RECORD, DESK_OWNER_TCLAW_VOICE);
    return 0;
}

int desk_hardware_register_audio_play_cb(desk_hardware_audio_play_cb_t cb, void *user_data)
{
    s_audio_play_cb = cb;
    s_audio_play_user_data = user_data;
    return 0;
}

int desk_hardware_audio_play_pcm(const int16_t *pcm_buf, size_t samples, uint32_t sample_rate_hz)
{
    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
        return -1;
    }
    if (s_audio_play_cb && pcm_buf && samples > 0) {
        s_audio_play_cb(pcm_buf, samples, sample_rate_hz, s_audio_play_user_data);
    }
    if (s_board_driver_ops && s_board_driver_ops->audio_play_pcm && pcm_buf && samples > 0) {
        s_board_driver_ops->audio_play_pcm(pcm_buf, samples, sample_rate_hz);
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
    /* Mechanical clamping */
    if (pan_deg < DESK_MOTION_PAN_MIN_DEG) pan_deg = DESK_MOTION_PAN_MIN_DEG;
    if (pan_deg > DESK_MOTION_PAN_MAX_DEG) pan_deg = DESK_MOTION_PAN_MAX_DEG;
    if (tilt_deg < DESK_MOTION_TILT_MIN_DEG) tilt_deg = DESK_MOTION_TILT_MIN_DEG;
    if (tilt_deg > DESK_MOTION_TILT_MAX_DEG) tilt_deg = DESK_MOTION_TILT_MAX_DEG;

    desk_resource_owner_t current = desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL);
    bool need_release = false;
    if (current == DESK_OWNER_NONE) {
        if (desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE, 50) != DESK_ARBITER_OK) {
            return -1; /* Gimbal locked or busy */
        }
        need_release = true;
    }

    s_current_pan = pan_deg;
    s_current_tilt = tilt_deg;

    /* Construct real STM32 MCU_MOTION_MSG_SERVO_MOVE binary frame:
     * payload[0]: axis_mask (0x03)
     * payload[1..2]: pan_deg * 10 (i16 LE)
     * payload[3..4]: tilt_deg * 10 (i16 LE)
     * payload[5..6]: duration_ms (u16 LE)
     * payload[7]: motion_profile (0x00)
     * payload[8]: source (0x01 SDK_REMOTE)
     */
    uint8_t payload[9];
    payload[0] = 0x03;
    int16_t x_deg_x10 = (int16_t)(pan_deg * 10.0f);
    int16_t y_deg_x10 = (int16_t)(tilt_deg * 10.0f);
    uint16_t dur = (uint16_t)(duration_ms > 65535 ? 65535 : duration_ms);

    payload[1] = (uint8_t)(x_deg_x10 & 0xFF);
    payload[2] = (uint8_t)((x_deg_x10 >> 8) & 0xFF);
    payload[3] = (uint8_t)(y_deg_x10 & 0xFF);
    payload[4] = (uint8_t)((y_deg_x10 >> 8) & 0xFF);
    payload[5] = (uint8_t)(dur & 0xFF);
    payload[6] = (uint8_t)((dur >> 8) & 0xFF);
    payload[7] = 0x00;
    payload[8] = 0x01;

    s_last_mcu_frame_len = mcu_frame_pack_motion(MCU_MOTION_MSG_SERVO_MOVE, payload, sizeof(payload), s_last_mcu_frame);
    if (s_mcu_tx_cb) {
        s_mcu_tx_cb(s_last_mcu_frame, s_last_mcu_frame_len, s_mcu_tx_user_data);
    }
    if (s_board_driver_ops && s_board_driver_ops->motion_move) {
        s_board_driver_ops->motion_move(pan_deg, tilt_deg, duration_ms);
    }

    if (need_release) {
        desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE);
    }
    return 0;
}

int desk_hardware_motion_stop(bool emergency)
{
    /* Construct real STM32 MCU_MOTION_MSG_SERVO_STOP binary frame:
     * payload[0]: all_pending (1)
     * payload[1]: source (0x01)
     */
    uint8_t payload[2];
    payload[0] = 0x01;
    payload[1] = 0x01;

    s_last_mcu_frame_len = mcu_frame_pack_motion(MCU_MOTION_MSG_SERVO_STOP, payload, sizeof(payload), s_last_mcu_frame);
    if (s_mcu_tx_cb) {
        s_mcu_tx_cb(s_last_mcu_frame, s_last_mcu_frame_len, s_mcu_tx_user_data);
    }
    if (s_board_driver_ops && s_board_driver_ops->motion_stop) {
        s_board_driver_ops->motion_stop(emergency);
    }

    desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE);
    desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE);
    return 0;
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
    if (!jpeg_buf || jpeg_len == 0 || jpeg_len > CAMERA_MAX_JPEG_LEN) return -1;
    memcpy(s_cam_frame_buf, jpeg_buf, jpeg_len);
    s_cam_frame_len = jpeg_len;
    s_cam_timestamp_ms = timestamp_ms;

    if (s_cam_streaming && s_cam_cb) {
        s_cam_cb(s_cam_frame_buf, s_cam_frame_len, s_cam_timestamp_ms, s_cam_user_data);
    }
    return 0;
}

int desk_hardware_camera_capture_jpeg(uint8_t *out_buf, size_t max_len, size_t *out_len, uint32_t timeout_ms)
{
    if (!out_buf || !out_len) return -1;
    (void)timeout_ms;

    /* First attempt capturing live frame from registered hardware driver */
    if (s_board_driver_ops && s_board_driver_ops->camera_capture) {
        size_t drv_len = 0;
        int drv_ret = s_board_driver_ops->camera_capture(out_buf, max_len, &drv_len);
        if (drv_ret == 0 && drv_len > 0) {
            *out_len = drv_len;
            return 0;
        }
    }

    if (max_len < s_cam_frame_len) return -2;

    memcpy(out_buf, s_cam_frame_buf, s_cam_frame_len);
    *out_len = s_cam_frame_len;
    return 0;
}

int desk_hardware_camera_stream_start(float target_fps, desk_camera_frame_cb_t cb, void *user_data)
{
    s_cam_target_fps = target_fps;
    s_cam_cb = cb;
    s_cam_user_data = user_data;
    s_cam_streaming = true;
    return 0;
}

int desk_hardware_camera_stream_pump(void)
{
    if (!s_cam_streaming || !s_cam_cb) return -1;
    s_cam_timestamp_ms += (uint32_t)(s_cam_target_fps > 0 ? (1000.0f / s_cam_target_fps) : 66);
    s_cam_cb(s_cam_frame_buf, s_cam_frame_len, s_cam_timestamp_ms, s_cam_user_data);
    return 0;
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
    if (preset_name) {
        strncpy(s_current_preset, preset_name, sizeof(s_current_preset) - 1);
        s_current_preset[sizeof(s_current_preset) - 1] = '\0';
    }
    if (status_text) {
        strncpy(s_current_status, status_text, sizeof(s_current_status) - 1);
        s_current_status[sizeof(s_current_status) - 1] = '\0';
    }
    if (s_board_driver_ops && s_board_driver_ops->display_expression) {
        s_board_driver_ops->display_expression(preset_name, status_text);
    }
    return 0;
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
