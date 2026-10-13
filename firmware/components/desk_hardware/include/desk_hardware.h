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
 * @brief Initialize all board hardware peripherals and resource arbiter.
 */
int desk_hardware_init(void);

/* Audio API */
int desk_hardware_audio_record_start(uint32_t sample_rate_hz);
int desk_hardware_audio_record_read(int16_t *pcm_buf, size_t samples, size_t *read_samples);
int desk_hardware_audio_record_stop(void);
int desk_hardware_audio_play_pcm(const int16_t *pcm_buf, size_t samples, uint32_t sample_rate_hz);
int desk_hardware_audio_play_stop(void);

/* Motion / Gimbal API */
int desk_hardware_motion_move_to(float pan_deg, float tilt_deg, uint32_t duration_ms);
int desk_hardware_motion_stop(bool emergency);
int desk_hardware_motion_get_position(float *out_pan, float *out_tilt);

/* Camera API */
int desk_hardware_camera_capture_jpeg(uint8_t *out_buf, size_t max_len, size_t *out_len, uint32_t timeout_ms);
int desk_hardware_camera_stream_start(float target_fps, desk_camera_frame_cb_t cb, void *user_data);
int desk_hardware_camera_stream_stop(void);

/* Display & Expression API */
int desk_hardware_display_set_expression(const char *preset_name, const char *status_text);

/* Input Event Subscription */
int desk_hardware_register_input_cb(desk_touch_event_cb_t cb, void *user_data);

#ifdef __cplusplus
}
#endif

#endif /* DESK_HARDWARE_H */
