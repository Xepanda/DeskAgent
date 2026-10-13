/**
 * @file desk_hardware.c
 * @brief Implementation of Desk Agent R1 Hardware HAL and Drivers.
 */

#include "desk_hardware.h"
#include <stdio.h>
#include <string.h>

static float s_current_pan = 90.0f;
static float s_current_tilt = 120.0f;
static desk_touch_event_cb_t s_touch_cb = NULL;
static void *s_touch_cb_user_data = NULL;

int desk_hardware_init(void)
{
    desk_arbiter_init();
    s_current_pan = 90.0f;
    s_current_tilt = 120.0f;
    return 0;
}

int desk_hardware_audio_record_start(uint32_t sample_rate_hz)
{
    if (desk_arbiter_acquire(DESK_RES_AUDIO_RECORD, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
        return -1;
    }
    return 0;
}

int desk_hardware_audio_record_read(int16_t *pcm_buf, size_t samples, size_t *read_samples)
{
    if (!pcm_buf || !read_samples) return -1;
    /* Simulated silent or baseline frame for driver layer */
    memset(pcm_buf, 0, samples * sizeof(int16_t));
    *read_samples = samples;
    return 0;
}

int desk_hardware_audio_record_stop(void)
{
    desk_arbiter_release(DESK_RES_AUDIO_RECORD, DESK_OWNER_TCLAW_VOICE);
    return 0;
}

int desk_hardware_audio_play_pcm(const int16_t *pcm_buf, size_t samples, uint32_t sample_rate_hz)
{
    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
        return -1;
    }
    return 0;
}

int desk_hardware_audio_play_stop(void)
{
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
    return 0;
}

static char s_current_preset[32] = "idle";
static char s_current_status[64] = "Ready";

int desk_hardware_motion_move_to(float pan_deg, float tilt_deg, uint32_t duration_ms)
{
    /* Enforce strict mechanical clamping */
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

    if (need_release) {
        desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE);
    }
    return 0;
}

int desk_hardware_motion_stop(bool emergency)
{
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

int desk_hardware_camera_capture_jpeg(uint8_t *out_buf, size_t max_len, size_t *out_len, uint32_t timeout_ms)
{
    if (!out_buf || !out_len || max_len < 4) return -1;

    /* Generate valid minimal JPEG image (SOI 0xFFD8, EOI 0xFFD9) */
    static const uint8_t s_min_jpeg[] = { 0xFF, 0xD8, 0xFF, 0xE0, 0x00, 0x10, 0x4A, 0x46,
                                          0x49, 0x46, 0x00, 0x01, 0x01, 0x01, 0x00, 0x48,
                                          0x00, 0x48, 0x00, 0x00, 0xFF, 0xD9 };
    if (max_len < sizeof(s_min_jpeg)) return -1;

    memcpy(out_buf, s_min_jpeg, sizeof(s_min_jpeg));
    *out_len = sizeof(s_min_jpeg);
    return 0;
}

int desk_hardware_camera_stream_start(float target_fps, desk_camera_frame_cb_t cb, void *user_data)
{
    return 0;
}

int desk_hardware_camera_stream_stop(void)
{
    return 0;
}

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
