/**
 * @file desk_hardware.h
 * @brief Desk Agent R1 Board-Level Hardware HAL & Driver Abstraction.
 *
 * Encapsulates audio codec, display, touch inputs, STM32 body servos,
 * and HX6538 camera under an isolated ESP-IDF 5.4 compatible interface.
 *
 * @copyright Copyright (c) 2026 DeskAgent Project. All rights reserved.
 */

#ifndef DESK_HARDWARE_H
#define DESK_HARDWARE_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "desk_arbiter.h"

/* Standard Hardware Error Codes */
#define DESK_HW_OK                  0
#define DESK_HW_ERR_INVALID_ARG    -1
#define DESK_HW_ERR_UNAVAILABLE    -2
#define DESK_HW_ERR_TIMEOUT        -3
#define DESK_HW_ERR_IO             -4
#define DESK_HW_ERR_BUSY           -5
#define DESK_HW_ERR_STALL          -6

/* Motion Limit Constraints (degrees) */
#define DESK_MOTION_PAN_MIN_DEG    0.0f
#define DESK_MOTION_PAN_MAX_DEG    180.0f
#define DESK_MOTION_TILT_MIN_DEG   100.0f
#define DESK_MOTION_TILT_MAX_DEG   140.0f

typedef struct {
    float pan_deg;
    float tilt_deg;
    uint32_t duration_ms;
} desk_motion_target_t;

typedef enum {
    DESK_TOUCH_TARGET_BACKTOUCH = 0,
    DESK_TOUCH_TARGET_SCREENTOUCH,
    DESK_TOUCH_TARGET_ROLLER
} desk_touch_target_t;

typedef enum {
    DESK_TOUCH_ACTION_SHORT_PRESS = 0,
    DESK_TOUCH_ACTION_LONG_PRESS,
    DESK_TOUCH_ACTION_SCROLL_UP,
    DESK_TOUCH_ACTION_SCROLL_DOWN
} desk_touch_action_t;

typedef void (*desk_touch_event_cb_t)(desk_touch_target_t target,
                                      desk_touch_action_t action,
                                      void *user_data);

typedef void (*desk_camera_frame_cb_t)(const uint8_t *jpeg_buf,
                                       size_t jpeg_len,
                                       uint32_t timestamp_ms,
                                       void *user_data);

/**
 * @brief Board-level Peripheral Driver Operations (BSP HAL Driver Table).
 */
typedef struct {
    const char *driver_name;
    int (*init)(void);
    int (*audio_record_read)(int16_t *pcm_buf, size_t samples, size_t *read_samples);
    int (*audio_play_pcm)(const int16_t *pcm_buf, size_t samples, uint32_t sample_rate_hz);
    int (*motion_move)(float pan_deg, float tilt_deg, uint32_t duration_ms);
    int (*motion_stop)(bool emergency);
    int (*camera_capture)(uint8_t *out_buf, size_t max_len, size_t *out_len);
    int (*display_expression)(const char *preset_name, const char *status_text);
    int (*deinit)(void);
} desk_board_driver_ops_t;

/**
 * @brief Register custom board driver operations table.
 */
int desk_hardware_register_driver_ops(const desk_board_driver_ops_t *ops);

/**
 * @brief Get currently active board driver operations table.
 */
const desk_board_driver_ops_t *desk_hardware_get_driver_ops(void);

/**
 * @brief Initialize all board hardware peripherals and resource arbiter.
 */
int desk_hardware_init(void);

/* Audio API */
int desk_hardware_audio_record_start(uint32_t sample_rate_hz);
int desk_hardware_audio_record_read(int16_t *pcm_buf, size_t samples, size_t *read_samples);
int desk_hardware_audio_record_stop(void);
int desk_hardware_audio_record_inject(const int16_t *pcm_buf, size_t samples);
int desk_hardware_audio_play_pcm(const int16_t *pcm_buf, size_t samples, uint32_t sample_rate_hz);
int desk_hardware_audio_play_stop(void);

typedef void (*desk_hardware_audio_play_cb_t)(const int16_t *pcm_buf,
                                              size_t samples,
                                              uint32_t sample_rate_hz,
                                              void *user_data);
int desk_hardware_register_audio_play_cb(desk_hardware_audio_play_cb_t cb, void *user_data);

/* STM32 Coprocessor MCU Frame Definitions (R3a Ratified) */
#define MCU_FRAME_MAGIC0          0xA5u
#define MCU_FRAME_MAGIC1          0x5Au
#define MCU_FRAME_PROTO_VERSION   0x01u
#define MCU_FRAME_CLASS_MOTION    0x02u

/* MCU Message IDs */
#define MCU_MOTION_MSG_ACK        0x00u
#define MCU_MOTION_MSG_SERVO_MOVE 0x01u
#define MCU_MOTION_MSG_SERVO_STOP 0x02u
#define MCU_MOTION_MSG_NACK       0xFFu

/* MCU Frame Flags */
#define MCU_FRAME_FLAG_ACK_REQ    0x01u
#define MCU_FRAME_FLAG_ACK_RESP   0x02u
#define MCU_FRAME_FLAG_NACK_RESP  0x04u

/* Timing, Retry & Safety Contracts */
#define MCU_ACK_TIMEOUT_MS        500u
#define MCU_MAX_RETRIES           2u
#define MCU_RETRY_BACKOFF_MS      50u

/* Stop Mode Definitions */
#define MCU_STOP_MODE_DECEL       0x01u
#define MCU_STOP_MODE_EMERGENCY   0x02u

/* Axis Mask Definitions */
#define MCU_AXIS_MASK_PAN_TILT    0x03u
#define MCU_AXIS_MASK_CANCEL_ALL  0x01u

/* NACK Error Codes */
#define MCU_NACK_ERR_CRC          0x01u
#define MCU_NACK_ERR_OUT_OF_BOUNDS 0x02u
#define MCU_NACK_ERR_BUSY         0x03u
#define MCU_NACK_ERR_INVALID_PARAM 0x04u
#define MCU_NACK_ERR_HW_FAULT     0x05u

typedef void (*desk_hardware_mcu_tx_cb_t)(const uint8_t *frame_data,
                                          size_t frame_len,
                                          void *user_data);
int desk_hardware_register_mcu_tx_cb(desk_hardware_mcu_tx_cb_t cb, void *user_data);
int desk_hardware_get_last_mcu_frame(uint8_t *out_buf, size_t max_len, size_t *out_len);

/* Motion / Gimbal API */
int desk_hardware_motion_move_to(float pan_deg, float tilt_deg, uint32_t duration_ms);
int desk_hardware_motion_stop(bool emergency);
int desk_hardware_motion_get_position(float *out_pan, float *out_tilt);
int desk_hardware_format_stm32_motion_packet(float pan_deg, float tilt_deg, uint32_t duration_ms,
                                             uint8_t *out_buf, size_t max_len, size_t *out_len);
int desk_hardware_format_stm32_motion_packet_with_seq(float pan_deg, float tilt_deg, uint32_t duration_ms,
                                                      uint32_t seq,
                                                      uint8_t *out_buf, size_t max_len, size_t *out_len);
int desk_hardware_format_stm32_stop_packet(bool emergency,
                                           uint8_t *out_buf, size_t max_len, size_t *out_len);
int desk_hardware_format_stm32_stop_packet_with_seq(bool emergency, uint32_t seq,
                                                    uint8_t *out_buf, size_t max_len, size_t *out_len);
int desk_hardware_format_stm32_ack_packet(uint32_t seq, int16_t pan_deg_x10, int16_t tilt_deg_x10,
                                          uint8_t *out_buf, size_t max_len, size_t *out_len);
int desk_hardware_format_stm32_nack_packet(uint32_t seq, uint8_t error_code, uint8_t sub_error,
                                           uint8_t *out_buf, size_t max_len, size_t *out_len);

/* Checksum and Sequence Helpers */
uint16_t desk_hardware_calc_crc16(const uint8_t *data, size_t len);
uint32_t desk_hardware_get_mcu_seq(void);
void desk_hardware_set_mcu_seq(uint32_t seq);

/* Protocol Timing Configuration & Statistics */
uint32_t desk_hardware_get_mcu_timeout_ms(void);
void desk_hardware_set_mcu_timeout_ms(uint32_t timeout_ms);
uint32_t desk_hardware_get_mcu_retry_backoff_ms(void);
void desk_hardware_set_mcu_retry_backoff_ms(uint32_t backoff_ms);
uint32_t desk_hardware_get_mcu_max_retries(void);
void desk_hardware_set_mcu_max_retries(uint32_t max_retries);

uint32_t desk_hardware_get_mcu_crc_error_count(void);
void desk_hardware_reset_mcu_crc_error_count(void);
uint32_t desk_hardware_get_mcu_tx_count(void);
uint32_t desk_hardware_get_mcu_retry_count(void);
uint32_t desk_hardware_get_mcu_ack_count(void);
uint32_t desk_hardware_get_mcu_nack_count(void);
uint32_t desk_hardware_get_mcu_timeout_count(void);
void desk_hardware_reset_mcu_stats(void);

/* Camera API */
int desk_hardware_camera_capture_jpeg(uint8_t *out_buf, size_t max_len, size_t *out_len, uint32_t timeout_ms);
int desk_hardware_camera_stream_start(float target_fps, desk_camera_frame_cb_t cb, void *user_data);
int desk_hardware_camera_stream_stop(void);
int desk_hardware_camera_inject_frame(const uint8_t *jpeg_buf, size_t jpeg_len, uint32_t timestamp_ms);
int desk_hardware_camera_stream_pump(void);
int desk_hardware_mcu_receive_bytes(const uint8_t *data, size_t len);

/* R0 Hardware Mapping & Physical Safety Constants */
#define DESK_PIN_UART0_TX            43
#define DESK_PIN_UART0_RX            44
#define DESK_PIN_STM32_TX            19
#define DESK_PIN_STM32_RX            20
#define DESK_UART_STM32_BAUD         921600
#define DESK_DISPLAY_WIDTH           412
#define DESK_DISPLAY_HEIGHT          412
#define DESK_DISPLAY_CONTROLLER      "SPD2010"

/* Display Diagnostic & Self-Test Structures (R2) */
typedef struct {
    bool initialized;                     /**< Display controller initialized and ready */
    bool strict_mode;                     /**< Strict hardware validation mode flag */
    char current_preset[32];              /**< Active expression preset (e.g. "idle", "listening") */
    char current_status[64];              /**< Active screen status text (e.g. "Ready") */
    desk_resource_owner_t arbiter_owner; /**< Current owner of DESK_RES_DISPLAY_UI */
    int last_error_code;                  /**< 0 if operational, or DESK_HW_ERR_* */
} desk_display_self_test_t;

/* Display & Expression API (R2) */
int desk_hardware_set_screen_expression(const char *preset_name, const char *status_text);
int desk_hardware_display_set_expression(const char *preset_name, const char *status_text);
int desk_hardware_display_get_expression(char *out_preset, size_t max_preset_len, char *out_status, size_t max_status_len);
int desk_hardware_display_self_test(desk_display_self_test_t *out_result);
int desk_hardware_screen_self_test(desk_display_self_test_t *out_result);
void desk_hardware_set_display_initialized(bool init);
bool desk_hardware_is_display_initialized(void);

/* Input Event Subscription */
int desk_hardware_register_input_cb(desk_touch_event_cb_t cb, void *user_data);
int desk_hardware_inject_touch_event(desk_touch_target_t target, desk_touch_action_t action);
int desk_hardware_feed_touch_event(desk_touch_target_t target, desk_touch_action_t action);

/* Strict Hardware Mode & Status Inspection */
void desk_hardware_set_strict_mode(bool strict);
bool desk_hardware_is_strict_mode(void);
bool desk_hardware_camera_has_frame(void);
float desk_hardware_get_pan(void);
float desk_hardware_get_tilt(void);

#ifdef __cplusplus
}
#endif

#endif /* DESK_HARDWARE_H */
