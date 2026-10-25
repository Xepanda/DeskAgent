/**
 * @file desk_tclaw_bridge.c
 * @brief Implementation of TClaw Dialogue Engine, Intent Routing, and MCP Gateway.
 */

#include "desk_tclaw_bridge.h"
#include "desk_hardware.h"
#include "desk_watcher_camera.h"
#ifdef ESP_PLATFORM
#include "ai_agent.h"
#else
/* Host builds have no Tuya component; keep the cloud API weak for contract tests. */
typedef enum { AT_LONG_KEY_TALK = 7, AT_KEY_TALK = 8 } AI_ALERT_TYPE_E;
extern int ai_agent_cloud_alert(AI_ALERT_TYPE_E type) __attribute__((weak));
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdbool.h>
#include <ctype.h>
#include <time.h>
#include <sys/time.h>

static void desk_tclaw_format_iso8601(time_t t, char *out_buf, size_t buf_len)
{
    if (!out_buf || buf_len < 21) return;
    struct tm tm_info;
    gmtime_r(&t, &tm_info);
    strftime(out_buf, buf_len, "%Y-%m-%dT%H:%M:%SZ", &tm_info);
}

static uint64_t desk_tclaw_get_time_ms(void)
{
    struct timeval tv;
    gettimeofday(&tv, NULL);
    return ((uint64_t)tv.tv_sec * 1000ULL) + ((uint64_t)tv.tv_usec / 1000ULL);
}

/* --------------------------------------------------------------------------
 * Lightweight JSON Scanning Utilities
 * -------------------------------------------------------------------------- */
static const char *desk_json_find_key(const char *json, const char *key)
{
    if (!json || !key) return NULL;
    char needle[128];
    snprintf(needle, sizeof(needle), "\"%s\"", key);
    const char *pos = strstr(json, needle);
    if (!pos) return NULL;
    pos += strlen(needle);
    while (*pos && (*pos == ' ' || *pos == '\t' || *pos == '\r' || *pos == '\n')) pos++;
    if (*pos != ':') return NULL;
    pos++;
    while (*pos && (*pos == ' ' || *pos == '\t' || *pos == '\r' || *pos == '\n')) pos++;
    return pos;
}

static bool desk_json_parse_str(const char *json, const char *key, char *out_val, size_t max_len)
{
    if (!out_val || max_len == 0) return false;
    out_val[0] = '\0';
    const char *pos = desk_json_find_key(json, key);
    if (!pos || *pos != '\"') return false;
    pos++;
    size_t i = 0;
    while (*pos && *pos != '\"' && i < max_len - 1) {
        if (*pos == '\\' && *(pos + 1)) pos++;
        out_val[i++] = *pos++;
    }
    out_val[i] = '\0';
    return true;
}

static float desk_json_parse_float(const char *json, const char *key, float default_val)
{
    const char *pos = desk_json_find_key(json, key);
    if (!pos) return default_val;
    return strtof(pos, NULL);
}

static int desk_json_parse_int(const char *json, const char *key, int default_val)
{
    const char *pos = desk_json_find_key(json, key);
    if (!pos) return default_val;
    return (int)strtol(pos, NULL, 10);
}

static const char *desk_camera_capture_error_name(int rc)
{
    switch (rc) {
    case DESK_HW_ERR_UNAVAILABLE: return "capture driver unavailable";
    case DESK_HW_ERR_TIMEOUT: return "capture deadline expired";
    case DESK_HW_ERR_BUSY: return "camera resource busy";
    case DESK_HW_ERR_INVALID_ARG: return "invalid capture buffer or frame size";
    case DESK_WATCHER_CAMERA_ERR_PROTOCOL: return "PTL/media protocol validation failed";
    case DESK_WATCHER_CAMERA_ERR_CRC: return "media payload CRC mismatch";
    case DESK_WATCHER_CAMERA_ERR_SYNC_IO: return "camera SYNC input read failed";
    case DESK_WATCHER_CAMERA_ERR_SYNC_TIMEOUT: return "SYNC stayed low until deadline";
    case DESK_WATCHER_CAMERA_ERR_NOT_READY: return "camera driver not ready";
    case DESK_WATCHER_CAMERA_ERR_SPI: return "SPI transport failed";
    case DESK_WATCHER_CAMERA_ERR_EMPTY: return "camera FIFO remained empty until deadline";
    case DESK_WATCHER_CAMERA_ERR_CONTROL: return "HX6538 control command failed";
    case DESK_WATCHER_CAMERA_ERR_BUFFER_TOO_SMALL: return "JPEG output buffer too small";
    default: return "camera capture failed";
    }
}

static bool desk_camera_capture_error_retryable(int rc)
{
    return rc == DESK_HW_ERR_TIMEOUT || rc == DESK_HW_ERR_BUSY ||
           rc == DESK_WATCHER_CAMERA_ERR_SYNC_IO ||
           rc == DESK_WATCHER_CAMERA_ERR_SYNC_TIMEOUT ||
           rc == DESK_WATCHER_CAMERA_ERR_SPI ||
           rc == DESK_WATCHER_CAMERA_ERR_EMPTY;
}

static bool desk_json_parse_bool(const char *json, const char *key, bool default_val)
{
    const char *pos = desk_json_find_key(json, key);
    if (!pos) return default_val;
    if (strncmp(pos, "true", 4) == 0 || strncmp(pos, "1", 1) == 0) return true;
    if (strncmp(pos, "false", 5) == 0 || strncmp(pos, "0", 1) == 0) return false;
    return default_val;
}

/* --------------------------------------------------------------------------
 * State Variables
 * -------------------------------------------------------------------------- */
static bool s_initialized = false;
static desk_tclaw_state_t s_state = TCLAW_STATE_IDLE;
static desk_tclaw_tool_dispatcher_t s_tool_dispatcher = NULL;
static void *s_dispatcher_user_data = NULL;

static desk_tclaw_speech_stream_cb_t s_speech_stream_cb = NULL;
static void *s_stream_user_data = NULL;

static int s_turn_count = 0;
static char s_last_response[1024] = "";
static char s_tuya_session_id[128] = "";
static char s_current_pending_tool[128] = "";
static char s_pending_request_id[64] = "";
static uint64_t s_pending_tool_start_ms = 0;
static uint32_t s_tool_timeout_ms = 5000;

static tuya_cloud_connection_state_t s_tuya_conn_state = TUYA_CLOUD_DISCONNECTED;
static char s_tuya_endpoint[128] = "";
static char s_tuya_device_id[64] = "";
static char s_tuya_token[128] = "";
static char s_last_tool_result[1024] = "";

typedef enum {
    WATCHER_UI_PAGE_HOME = 0,
    WATCHER_UI_PAGE_AUDIO,
    WATCHER_UI_PAGE_VOICE,
    WATCHER_UI_PAGE_DEVICE,
    WATCHER_UI_PAGE_COUNT
} watcher_ui_page_t;

static watcher_ui_page_t s_ui_focus = WATCHER_UI_PAGE_HOME;
static bool s_ui_overlay_active = false;
static bool s_ui_page_open = false;
static bool s_pairing_ui_active = false;
static char s_pairing_ui_code[7] = {0};
static char s_ui_home_preset[32] = "idle";
static char s_ui_home_status[64] = "Ready";

static const char *watcher_ui_page_name(watcher_ui_page_t page)
{
    switch (page) {
    case WATCHER_UI_PAGE_HOME: return "HOME";
    case WATCHER_UI_PAGE_AUDIO: return "AUDIO";
    case WATCHER_UI_PAGE_VOICE: return "VOICE";
    case WATCHER_UI_PAGE_DEVICE: return "DEVICE";
    default: return "HOME";
    }
}

static int watcher_ui_render(void)
{
    char status[64] = "";
    if (s_pairing_ui_active) {
        snprintf(status, sizeof(status), "PAIR %s", s_pairing_ui_code);
        return desk_hardware_display_set_expression(s_ui_home_preset, status);
    }
    if (!s_ui_overlay_active) return DESK_HW_OK;
    if (!s_ui_page_open) {
        snprintf(status, sizeof(status), "%s", watcher_ui_page_name(s_ui_focus));
    } else {
        switch (s_ui_focus) {
        case WATCHER_UI_PAGE_HOME:
            snprintf(status, sizeof(status), "%.20s", s_ui_home_status);
            break;
        case WATCHER_UI_PAGE_AUDIO: {
            desk_hardware_audio_status_t audio = {0};
            if (desk_hardware_audio_get_status(&audio) != DESK_HW_OK) {
                snprintf(status, sizeof(status), "Mic unavailable");
            } else if (audio.errors > 0) {
                snprintf(status, sizeof(status), "Mic check");
            } else {
                snprintf(status, sizeof(status), "%s", audio.running ? "Mic active" : "Mic ready");
            }
            break;
        }
        case WATCHER_UI_PAGE_VOICE:
            switch (desk_tclaw_get_state()) {
            case TCLAW_STATE_LISTENING: snprintf(status, sizeof(status), "Listening"); break;
            case TCLAW_STATE_THINKING: snprintf(status, sizeof(status), "Thinking"); break;
            case TCLAW_STATE_SPEAKING: snprintf(status, sizeof(status), "Speaking"); break;
            case TCLAW_STATE_TOOL_CALLING: snprintf(status, sizeof(status), "Working"); break;
            case TCLAW_STATE_IDLE:
            default: snprintf(status, sizeof(status), "Ready"); break;
            }
            break;
        case WATCHER_UI_PAGE_DEVICE:
            snprintf(status, sizeof(status), "Desk Agent");
            break;
        default:
            snprintf(status, sizeof(status), "Unavailable");
            break;
        }
    }
    return desk_hardware_display_set_expression(s_ui_home_preset, status);
}

int desk_tclaw_pairing_ui_set_code(const char *code)
{
    if (!code || strlen(code) != 6) return DESK_HW_ERR_INVALID_ARG;
    for (size_t i = 0; i < 6; ++i) {
        if (code[i] < '0' || code[i] > '9') return DESK_HW_ERR_INVALID_ARG;
    }
    memcpy(s_pairing_ui_code, code, sizeof(s_pairing_ui_code));
    s_pairing_ui_active = true;
    return watcher_ui_render();
}

void desk_tclaw_pairing_ui_clear(void)
{
    memset(s_pairing_ui_code, 0, sizeof(s_pairing_ui_code));
    s_pairing_ui_active = false;
    if (s_ui_overlay_active) {
        (void)watcher_ui_render();
    } else {
        (void)desk_hardware_display_set_expression(s_ui_home_preset, s_ui_home_status);
    }
}

void desk_tclaw_pairing_ui_on_pairing_consumed(void)
{
    desk_tclaw_pairing_ui_clear();
}

void desk_tclaw_pairing_ui_on_sdk_session_reset(void)
{
    desk_tclaw_pairing_ui_clear();
}

static int desk_tclaw_update_screen(const char *preset, const char *status)
{
    const char *safe_preset = preset ? preset : "idle";
    const char *safe_status = status ? status : "";
    if (s_ui_overlay_active || s_pairing_ui_active) {
        snprintf(s_ui_home_preset, sizeof(s_ui_home_preset), "%s", safe_preset);
        snprintf(s_ui_home_status, sizeof(s_ui_home_status), "%s", safe_status);
        return watcher_ui_render();
    }
    int rc = desk_hardware_display_set_expression(safe_preset, safe_status);
    if (rc != DESK_HW_OK) return rc;
    snprintf(s_ui_home_preset, sizeof(s_ui_home_preset), "%s", safe_preset);
    snprintf(s_ui_home_status, sizeof(s_ui_home_status), "%s", safe_status);
    return rc;
}

static void watcher_ui_input_event(desk_touch_target_t target,
                                   desk_touch_action_t action,
                                   void *user_data)
{
    (void)user_data;
    if (target == DESK_TOUCH_TARGET_ROLLER) {
        if (action == DESK_TOUCH_ACTION_SCROLL_UP || action == DESK_TOUCH_ACTION_SCROLL_DOWN) {
            s_ui_overlay_active = true;
            if (action == DESK_TOUCH_ACTION_SCROLL_UP) {
                s_ui_focus = (watcher_ui_page_t)((s_ui_focus + 1) % WATCHER_UI_PAGE_COUNT);
            } else {
                s_ui_focus = (watcher_ui_page_t)((s_ui_focus + WATCHER_UI_PAGE_COUNT - 1) % WATCHER_UI_PAGE_COUNT);
            }
            s_ui_page_open = false;
            (void)watcher_ui_render();
        } else if (action == DESK_TOUCH_ACTION_SHORT_PRESS || action == DESK_TOUCH_ACTION_LONG_PRESS) {
#ifdef ESP_PLATFORM
            AI_ALERT_TYPE_E alert = action == DESK_TOUCH_ACTION_SHORT_PRESS ? AT_KEY_TALK : AT_LONG_KEY_TALK;
            int rc = ai_agent_cloud_alert(alert);
#else
            int rc = ai_agent_cloud_alert
                         ? ai_agent_cloud_alert(action == DESK_TOUCH_ACTION_SHORT_PRESS ? AT_KEY_TALK : AT_LONG_KEY_TALK)
                         : DESK_HW_ERR_UNAVAILABLE;
#endif
            if (rc != 0) printf("[TCLAW_UI] Tuya talk alert failed rc=%d action=%d\n", rc, action);
        }
        return;
    }

    if (target == DESK_TOUCH_TARGET_SCREENTOUCH) {
        if (action == DESK_TOUCH_ACTION_SHORT_PRESS) {
            s_ui_overlay_active = true;
            s_ui_page_open = true;
            (void)watcher_ui_render();
        } else if (action == DESK_TOUCH_ACTION_SCROLL_DOWN && s_ui_overlay_active) {
            s_ui_overlay_active = false;
            s_ui_page_open = false;
            s_ui_focus = WATCHER_UI_PAGE_HOME;
            if (s_pairing_ui_active) (void)watcher_ui_render();
            else (void)desk_hardware_display_set_expression(s_ui_home_preset, s_ui_home_status);
        }
    }
}

static int desk_tclaw_update_ui_screen(const char *preset, const char *status)
{
    return desk_tclaw_update_screen(preset, status);
}

/* --------------------------------------------------------------------------
 * TClaw Dialogue Lifecycle
 * -------------------------------------------------------------------------- */
int desk_tclaw_init(void)
{
    s_initialized = true;
    s_state = TCLAW_STATE_IDLE;
    s_turn_count = 0;
    s_last_response[0] = '\0';
    s_current_pending_tool[0] = '\0';
    s_pending_request_id[0] = '\0';
    s_pending_tool_start_ms = 0;
    s_tool_timeout_ms = 5000;
    s_speech_stream_cb = NULL;
    s_stream_user_data = NULL;
    s_last_tool_result[0] = '\0';
    s_tuya_session_id[0] = '\0';
    s_tuya_conn_state = TUYA_CLOUD_DISCONNECTED;
    s_ui_focus = WATCHER_UI_PAGE_HOME;
    s_ui_overlay_active = false;
    s_ui_page_open = false;
    memset(s_ui_home_preset, 0, sizeof(s_ui_home_preset));
    snprintf(s_ui_home_preset, sizeof(s_ui_home_preset), "idle");
    memset(s_ui_home_status, 0, sizeof(s_ui_home_status));
    snprintf(s_ui_home_status, sizeof(s_ui_home_status), "Ready");
    desk_tclaw_pairing_ui_clear();

    desk_hardware_register_input_cb(watcher_ui_input_event, NULL);
    desk_tclaw_update_ui_screen("idle", "Ready");
    return 0;
}

void desk_tclaw_reset(void)
{
    s_initialized = false;
    desk_hardware_register_input_cb(NULL, NULL);
    s_ui_focus = WATCHER_UI_PAGE_HOME;
    s_ui_overlay_active = false;
    s_ui_page_open = false;
    desk_tclaw_pairing_ui_clear();
    s_state = TCLAW_STATE_IDLE;
    s_turn_count = 0;
    s_last_response[0] = '\0';
    s_current_pending_tool[0] = '\0';
    s_pending_request_id[0] = '\0';
    s_pending_tool_start_ms = 0;
    s_tool_timeout_ms = 5000;
    s_speech_stream_cb = NULL;
    s_stream_user_data = NULL;
    s_tool_dispatcher = NULL;
    s_dispatcher_user_data = NULL;
    s_tuya_session_id[0] = '\0';
    s_tuya_conn_state = TUYA_CLOUD_DISCONNECTED;
    s_tuya_endpoint[0] = '\0';
    s_tuya_device_id[0] = '\0';
    s_tuya_token[0] = '\0';
    s_last_tool_result[0] = '\0';

    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
    desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE);
    desk_arbiter_release(DESK_RES_CAMERA_SENSOR, DESK_OWNER_TCLAW_VOICE);
}

int desk_tclaw_start(void)
{
    if (!s_initialized) {
        desk_tclaw_init();
    }
    s_state = TCLAW_STATE_IDLE;
    return 0;
}

desk_tclaw_state_t desk_tclaw_get_state(void)
{
    return s_state;
}

bool desk_tclaw_is_initialized(void)
{
    return s_initialized;
}

int desk_tclaw_register_speech_stream_cb(desk_tclaw_speech_stream_cb_t cb, void *user_data)
{
    s_speech_stream_cb = cb;
    s_stream_user_data = user_data;
    return 0;
}

int desk_tclaw_get_last_response(char *out_buf, size_t max_len)
{
    if (!out_buf || max_len == 0) return -1;
    strncpy(out_buf, s_last_response, max_len - 1);
    out_buf[max_len - 1] = '\0';
    return 0;
}

int desk_tclaw_get_turn_count(void)
{
    return s_turn_count;
}

const char *desk_tclaw_get_pending_tool(void)
{
    return s_current_pending_tool;
}

const char *desk_tclaw_get_pending_request_id(void)
{
    return s_pending_request_id;
}

void desk_tclaw_set_tool_timeout_ms(uint32_t timeout_ms)
{
    s_tool_timeout_ms = timeout_ms;
}

uint32_t desk_tclaw_get_tool_timeout_ms(void)
{
    return s_tool_timeout_ms;
}

/* --------------------------------------------------------------------------
 * Speech & Interruption Execution
 * -------------------------------------------------------------------------- */
static int desk_tclaw_start_speaking_with_feedback(const char *speech_text,
                                                   const char *preset,
                                                   const char *label)
{
    if (!speech_text) return -1;

    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
        return -1;
    }

    s_state = TCLAW_STATE_SPEAKING;
    strncpy(s_last_response, speech_text, sizeof(s_last_response) - 1);
    s_last_response[sizeof(s_last_response) - 1] = '\0';

    desk_tclaw_update_ui_screen(preset, label);

    /* Stream speech tokens to registered listener if active */
    if (s_speech_stream_cb) {
        char copy[1024];
        strncpy(copy, speech_text, sizeof(copy) - 1);
        copy[sizeof(copy) - 1] = '\0';

        char *token = strtok(copy, " ");
        while (token != NULL) {
            char *next = strtok(NULL, " ");
            s_speech_stream_cb(token, (next == NULL), s_stream_user_data);
            token = next;
        }
    }

    return 0;
}

int desk_tclaw_start_speaking(const char *speech_text)
{
    return desk_tclaw_start_speaking_with_feedback(speech_text, "speaking", "Speaking");
}

int desk_tclaw_interrupt(void)
{
    desk_hardware_audio_play_stop();
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
    desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE);
    desk_arbiter_release(DESK_RES_CAMERA_SENSOR, DESK_OWNER_TCLAW_VOICE);
    s_state = TCLAW_STATE_IDLE;
    s_current_pending_tool[0] = '\0';
    s_pending_request_id[0] = '\0';
    desk_tclaw_update_ui_screen("idle", "Ready");
    return 0;
}

/* --------------------------------------------------------------------------
 * MCP Tool Gateway & Built-In Hardware Tools
 * -------------------------------------------------------------------------- */
int desk_tclaw_register_tool_dispatcher(desk_tclaw_tool_dispatcher_t dispatcher, void *user_data)
{
    s_tool_dispatcher = dispatcher;
    s_dispatcher_user_data = user_data;
    return 0;
}

bool desk_tclaw_is_builtin_tool(const char *operation)
{
    if (!operation) return false;
    return (strcmp(operation, "motion.move") == 0 ||
            strcmp(operation, "resource.motion.move") == 0 ||
            strcmp(operation, "ctrl.motion.move") == 0 ||
            strcmp(operation, "motion.stop") == 0 ||
            strcmp(operation, "resource.motion.stop") == 0 ||
            strcmp(operation, "ctrl.motion.stop") == 0 ||
            strcmp(operation, "camera.capture") == 0 ||
            strcmp(operation, "resource.camera.capture") == 0 ||
            strcmp(operation, "expression.set") == 0 ||
            strcmp(operation, "resource.expression.set") == 0 ||
            strcmp(operation, "display.expression") == 0 ||
            strcmp(operation, "audio.stop") == 0 ||
            strcmp(operation, "resource.audio.stop") == 0);
}

int desk_tclaw_execute_tool(const char *request_id,
                            const char *operation,
                            const char *arguments_json,
                            char *out_response_json,
                            size_t max_len)
{
    if (!request_id || !operation) return -1;
    if (request_id[0] == '\0' || operation[0] == '\0') return -1;

    uint64_t start_ms = desk_tclaw_get_time_ms();
    time_t now = time(NULL);
    if (now < 1700000000) now = 1792100000;
    char timestamp[32];
    desk_tclaw_format_iso8601(now, timestamp, sizeof(timestamp));

    const char *args = arguments_json ? arguments_json : "{}";
    char result_buf[512] = "";
    char error_code[64] = "";
    char error_msg[256] = "";
    bool retryable = false;
    const char *status = "failed";
    int return_code = 0;

    /* 1. Motion Move Tool: CR-002 semantic strictly enforced (accepted=true, state_confirmed=false) */
    if (strcmp(operation, "motion.move") == 0 ||
        strcmp(operation, "resource.motion.move") == 0 ||
        strcmp(operation, "ctrl.motion.move") == 0) {

        float pan_deg = desk_json_parse_float(args, "pan_deg", 90.0f);
        float tilt_deg = desk_json_parse_float(args, "tilt_deg", 120.0f);
        uint32_t duration_ms = (uint32_t)desk_json_parse_int(args, "duration_ms", 500);

        if (pan_deg < DESK_MOTION_PAN_MIN_DEG || pan_deg > DESK_MOTION_PAN_MAX_DEG ||
            tilt_deg < DESK_MOTION_TILT_MIN_DEG || tilt_deg > DESK_MOTION_TILT_MAX_DEG ||
            duration_ms < 100 || duration_ms > 65535) {
            status = "failed";
            strncpy(error_code, "INVALID_ARGUMENT", sizeof(error_code) - 1);
            strncpy(error_msg, "Motion target or duration is outside supported limits", sizeof(error_msg) - 1);
            retryable = false;
            return_code = DESK_HW_ERR_INVALID_ARG;
        } else if (desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE, 50) != DESK_ARBITER_OK) {
            status = "failed";
            strncpy(error_code, "RESOURCE_BUSY", sizeof(error_code) - 1);
            strncpy(error_msg, "Motion gimbal is currently held by higher/equal owner", sizeof(error_msg) - 1);
            retryable = true;
            return_code = DESK_HW_ERR_BUSY;
        } else {
            int move_rc = desk_hardware_motion_move_to(pan_deg, tilt_deg, duration_ms);
            desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE);

            if (move_rc == DESK_HW_OK) {
                status = "accepted";
                snprintf(result_buf, sizeof(result_buf),
                         "{\"pan_deg\":%.1f,\"tilt_deg\":%.1f,\"duration_ms\":%u,\"accepted\":true,\"state_confirmed\":false}",
                         pan_deg, tilt_deg, (unsigned int)duration_ms);
                return_code = DESK_HW_OK;
            } else if (move_rc == DESK_HW_ERR_TIMEOUT) {
                status = "failed";
                strncpy(error_code, "OPERATION_TIMEOUT", sizeof(error_code) - 1);
                strncpy(error_msg, "STM32 coprocessor motion ACK timed out", sizeof(error_msg) - 1);
                retryable = true;
                return_code = DESK_HW_ERR_TIMEOUT;
            } else if (move_rc == DESK_HW_ERR_INVALID_ARG) {
                status = "failed";
                strncpy(error_code, "INVALID_ARGUMENT", sizeof(error_code) - 1);
                strncpy(error_msg, "Motion target parameters out of mechanical bounds", sizeof(error_msg) - 1);
                retryable = false;
                return_code = DESK_HW_ERR_INVALID_ARG;
            } else {
                status = "failed";
                strncpy(error_code, "HARDWARE_FAULT", sizeof(error_code) - 1);
                strncpy(error_msg, "STM32 body hardware communication error", sizeof(error_msg) - 1);
                retryable = false;
                return_code = DESK_HW_ERR_IO;
            }
        }
    }
    /* 2. Motion Stop Tool */
    else if (strcmp(operation, "motion.stop") == 0 ||
             strcmp(operation, "resource.motion.stop") == 0 ||
             strcmp(operation, "ctrl.motion.stop") == 0) {

        bool emergency = desk_json_parse_bool(args, "emergency", false);
        int stop_rc = desk_hardware_motion_stop(emergency);
        desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE);

        if (stop_rc == DESK_HW_OK) {
            status = "accepted";
            snprintf(result_buf, sizeof(result_buf),
                     "{\"emergency\":%s,\"accepted\":true,\"state_confirmed\":false}",
                     emergency ? "true" : "false");
            return_code = DESK_HW_OK;
        } else {
            status = "failed";
            if (stop_rc == DESK_HW_ERR_TIMEOUT) {
                strncpy(error_code, "OPERATION_TIMEOUT", sizeof(error_code) - 1);
                strncpy(error_msg, "STM32 coprocessor motion stop ACK timed out", sizeof(error_msg) - 1);
                retryable = true;
            } else if (stop_rc == DESK_HW_ERR_BUSY) {
                strncpy(error_code, "RESOURCE_BUSY", sizeof(error_code) - 1);
                strncpy(error_msg, "Motion stop resource is currently busy", sizeof(error_msg) - 1);
                retryable = true;
            } else {
                strncpy(error_code, "HARDWARE_FAULT", sizeof(error_code) - 1);
                strncpy(error_msg, "Failed to execute motion stop", sizeof(error_msg) - 1);
                retryable = false;
            }
            return_code = stop_rc;
        }
    }
    /* 3. Camera Capture Tool (Strictly real sensor frames only, never fake) */
    else if (strcmp(operation, "camera.capture") == 0 ||
             strcmp(operation, "resource.camera.capture") == 0) {

        uint32_t timeout_ms = (uint32_t)desk_json_parse_int(args, "timeout_ms", 2000);

        if (desk_arbiter_acquire(DESK_RES_CAMERA_SENSOR, DESK_OWNER_TCLAW_VOICE, 50) != DESK_ARBITER_OK) {
            status = "failed";
            strncpy(error_code, "RESOURCE_BUSY", sizeof(error_code) - 1);
            strncpy(error_msg, "Camera sensor is currently acquired by another client", sizeof(error_msg) - 1);
            retryable = true;
            return_code = DESK_HW_ERR_BUSY;
        } else {
            uint8_t jpeg_temp[1024];
            size_t out_len = 0;
            int cam_rc = desk_hardware_camera_capture_jpeg(jpeg_temp, sizeof(jpeg_temp), &out_len, timeout_ms);
            desk_arbiter_release(DESK_RES_CAMERA_SENSOR, DESK_OWNER_TCLAW_VOICE);

            if (cam_rc == DESK_HW_OK && out_len > 0) {
                status = "succeeded";
                snprintf(result_buf, sizeof(result_buf),
                         "{\"format\":\"jpeg\",\"frame_len\":%zu,\"captured\":true}", out_len);
                return_code = DESK_HW_OK;
            } else {
                status = "failed";
                strncpy(error_code, "HARDWARE_FAULT", sizeof(error_code) - 1);
                snprintf(error_msg, sizeof(error_msg), "Camera capture rc=%d: %s",
                         cam_rc, desk_camera_capture_error_name(cam_rc));
                retryable = desk_camera_capture_error_retryable(cam_rc);
                return_code = cam_rc;
            }
        }
    }
    /* 4. Display Expression Tool */
    else if (strcmp(operation, "expression.set") == 0 ||
             strcmp(operation, "resource.expression.set") == 0 ||
             strcmp(operation, "display.expression") == 0) {

        char preset[32] = "idle";
        char status_text[64] = "Ready";
        desk_json_parse_str(args, "preset", preset, sizeof(preset));
        desk_json_parse_str(args, "status_text", status_text, sizeof(status_text));

        int exp_rc = desk_tclaw_update_ui_screen(preset, status_text);
        if (exp_rc == DESK_HW_OK) {
            status = "succeeded";
            snprintf(result_buf, sizeof(result_buf),
                     "{\"preset\":\"%s\",\"status_text\":\"%s\"}", preset, status_text);
            return_code = DESK_HW_OK;
        } else if (exp_rc == DESK_HW_ERR_BUSY) {
            status = "failed";
            strncpy(error_code, "RESOURCE_BUSY", sizeof(error_code) - 1);
            strncpy(error_msg, "Display UI arbiter is busy", sizeof(error_msg) - 1);
            retryable = true;
            return_code = DESK_HW_ERR_BUSY;
        } else {
            status = "failed";
            strncpy(error_code, "INVALID_ARGUMENT", sizeof(error_code) - 1);
            strncpy(error_msg, "Invalid expression preset or parameters", sizeof(error_msg) - 1);
            retryable = false;
            return_code = DESK_HW_ERR_INVALID_ARG;
        }
    }
    /* 5. Audio Stop Tool */
    else if (strcmp(operation, "audio.stop") == 0 ||
             strcmp(operation, "resource.audio.stop") == 0) {

        desk_hardware_audio_play_stop();
        desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
        status = "succeeded";
        snprintf(result_buf, sizeof(result_buf), "{\"status\":\"stopped\"}");
        return_code = DESK_HW_OK;
    }
    /* 6. Unsupported / External Operation */
    else {
        status = "failed";
        strncpy(error_code, "UNKNOWN_OPERATION", sizeof(error_code) - 1);
        snprintf(error_msg, sizeof(error_msg), "Operation '%s' not supported as built-in tool", operation);
        retryable = false;
        return_code = -1;
    }

    uint64_t elapsed_ms = desk_tclaw_get_time_ms() - start_ms;

    /* Build Contract ResponseEnvelope */
    if (out_response_json && max_len > 0) {
        if (strcmp(status, "accepted") == 0 || strcmp(status, "succeeded") == 0) {
            snprintf(out_response_json, max_len,
                     "{\"schema_version\":\"1.0.0\","
                     "\"request_id\":\"%s\","
                     "\"status\":\"%s\","
                     "\"timestamp\":\"%s\","
                     "\"result\":%s,"
                     "\"execution_duration_ms\":%llu}",
                     request_id, status, timestamp,
                     (result_buf[0] != '\0') ? result_buf : "{}",
                     (unsigned long long)elapsed_ms);
        } else {
            snprintf(out_response_json, max_len,
                     "{\"schema_version\":\"1.0.0\","
                     "\"request_id\":\"%s\","
                     "\"status\":\"failed\","
                     "\"timestamp\":\"%s\","
                     "\"error\":{\"code\":\"%s\",\"message\":\"%s\",\"retryable\":%s},"
                     "\"execution_duration_ms\":%llu}",
                     request_id, timestamp,
                     error_code, error_msg,
                     retryable ? "true" : "false",
                     (unsigned long long)elapsed_ms);
        }
    }

    return return_code;
}

int desk_tclaw_trigger_tool_call(const char *request_id, const char *operation, const char *arguments_json)
{
    if (!request_id || !operation) return -1;

    s_state = TCLAW_STATE_TOOL_CALLING;
    strncpy(s_current_pending_tool, operation, sizeof(s_current_pending_tool) - 1);
    s_current_pending_tool[sizeof(s_current_pending_tool) - 1] = '\0';
    strncpy(s_pending_request_id, request_id, sizeof(s_pending_request_id) - 1);
    s_pending_request_id[sizeof(s_pending_request_id) - 1] = '\0';
    s_pending_tool_start_ms = desk_tclaw_get_time_ms();

    desk_tclaw_update_ui_screen("thinking", "Calling external tool...");

    if (s_tool_dispatcher) {
        time_t now = time(NULL);
        if (now < 1700000000) {
            now = 1792100000;
        }
        char expires_at[32];
        desk_tclaw_format_iso8601(now + 60, expires_at, sizeof(expires_at));

        char env[2048];
        snprintf(env, sizeof(env),
                 "{\"schema_version\":\"1.0.0\","
                 "\"request_id\":\"%s\","
                 "\"operation\":\"%s\","
                 "\"arguments\":%s,"
                 "\"expires_at\":\"%s\"}",
                 request_id,
                 operation,
                 arguments_json ? arguments_json : "{}",
                 expires_at);

        return s_tool_dispatcher(env, s_dispatcher_user_data);
    }

    /* Fallback: If no dispatcher is registered, execute directly if it's a built-in tool */
    if (desk_tclaw_is_builtin_tool(operation)) {
        char resp_env[2048];
        int exec_rc = desk_tclaw_execute_tool(request_id, operation, arguments_json, resp_env, sizeof(resp_env));
        (void)exec_rc;
        const char *status_str = "failed";
        if (strstr(resp_env, "\"status\":\"accepted\"")) {
            status_str = "accepted";
        } else if (strstr(resp_env, "\"status\":\"succeeded\"")) {
            status_str = "succeeded";
        }
        return desk_tclaw_handle_tool_response(request_id, status_str,
                                               (strcmp(status_str, "failed") != 0) ? resp_env : NULL,
                                               (strcmp(status_str, "failed") == 0) ? resp_env : NULL);
    }

    /* If neither dispatcher nor built-in tool, report error back immediately */
    char err_json[256];
    snprintf(err_json, sizeof(err_json), "{\"code\":\"UPSTREAM_UNAVAILABLE\",\"message\":\"No tool dispatcher registered\"}");
    desk_tclaw_handle_tool_response(request_id, "failed", NULL, err_json);
    return -1;
}

int desk_tclaw_handle_tool_response(const char *request_id,
                                    const char *status,
                                    const char *result_json,
                                    const char *error_json)
{
    if (!request_id || !status) return -1;

    char extracted_status[32] = "";
    const char *eff_status = status;
    const char *eff_result = result_json;
    const char *eff_error = error_json;

    /* Extract true envelope status if result_json carries a contract envelope */
    if (result_json && strstr(result_json, "\"schema_version\"") != NULL) {
        if (desk_json_parse_str(result_json, "status", extracted_status, sizeof(extracted_status))) {
            if (strcmp(extracted_status, "accepted") == 0 ||
                strcmp(extracted_status, "succeeded") == 0 ||
                strcmp(extracted_status, "failed") == 0 ||
                strcmp(extracted_status, "cancelled") == 0 ||
                strcmp(extracted_status, "unknown") == 0) {
                eff_status = extracted_status;
            }
        }
    }

    bool is_success = (strcmp(eff_status, "succeeded") == 0 || strcmp(eff_status, "accepted") == 0);

    /* Report back to Tuya Cloud Dialogue session */
    desk_tclaw_tuya_send_tool_result(s_tuya_session_id, s_current_pending_tool,
                                     is_success ? (eff_result ? eff_result : "{}")
                                                : (eff_error ? eff_error : "{\"error\":\"failed\"}"));

    s_turn_count++;
    char spoken[1024];
    const char *feedback_preset = "error";
    const char *feedback_label = "Failed";
    if (strcmp(eff_status, "accepted") == 0) {
        snprintf(spoken, sizeof(spoken), "Action accepted successfully: %s",
                 (eff_result && strlen(eff_result) > 0) ? eff_result : "OK");
        feedback_preset = "thinking";
        feedback_label = "Accepted";
    } else if (strcmp(eff_status, "succeeded") == 0) {
        snprintf(spoken, sizeof(spoken), "Action completed successfully: %s",
                 (eff_result && strlen(eff_result) > 0) ? eff_result : "OK");
        feedback_preset = "happy";
        feedback_label = "Done";
    } else {
        snprintf(spoken, sizeof(spoken), "Action encountered an error: %s",
                 (eff_error && strlen(eff_error) > 0) ? eff_error : "Error");
    }

    s_current_pending_tool[0] = '\0';
    s_pending_request_id[0] = '\0';
    s_pending_tool_start_ms = 0;

    return desk_tclaw_start_speaking_with_feedback(spoken, feedback_preset, feedback_label);
}

int desk_tclaw_handle_tool_timeout(const char *request_id)
{
    if (s_state != TCLAW_STATE_TOOL_CALLING) return -1;
    if (request_id && strlen(request_id) > 0 && strcmp(s_pending_request_id, request_id) != 0) {
        return -1;
    }

    char err_json[256];
    snprintf(err_json, sizeof(err_json),
             "{\"code\":\"OPERATION_TIMEOUT\",\"message\":\"External tool execution timed out\",\"retryable\":true}");

    const char *req = (request_id && strlen(request_id) > 0) ? request_id : s_pending_request_id;
    return desk_tclaw_handle_tool_response(req, "failed", NULL, err_json);
}

int desk_tclaw_poll_timeouts(uint64_t now_ms)
{
    if (s_state != TCLAW_STATE_TOOL_CALLING) return 0;
    if (s_pending_tool_start_ms == 0) return 0;

    if (now_ms == 0) {
        now_ms = desk_tclaw_get_time_ms();
    }

    if (now_ms >= s_pending_tool_start_ms + s_tool_timeout_ms) {
        desk_tclaw_handle_tool_timeout(s_pending_request_id);
        return 1;
    }
    return 0;
}

/* --------------------------------------------------------------------------
 * Tuya Cloud Dialogue Session Management
 * -------------------------------------------------------------------------- */
int desk_tclaw_tuya_session_connect(const char *endpoint, const char *device_id, const char *token)
{
    if (!endpoint || !device_id || !token) return -1;
    if (strlen(endpoint) == 0 || strlen(device_id) == 0 || strlen(token) == 0) return -1;

    s_tuya_conn_state = TUYA_CLOUD_CONNECTING;

    strncpy(s_tuya_endpoint, endpoint, sizeof(s_tuya_endpoint) - 1);
    s_tuya_endpoint[sizeof(s_tuya_endpoint) - 1] = '\0';

    strncpy(s_tuya_device_id, device_id, sizeof(s_tuya_device_id) - 1);
    s_tuya_device_id[sizeof(s_tuya_device_id) - 1] = '\0';

    strncpy(s_tuya_token, token, sizeof(s_tuya_token) - 1);
    s_tuya_token[sizeof(s_tuya_token) - 1] = '\0';

    s_tuya_conn_state = TUYA_CLOUD_CONNECTED;
    desk_tclaw_tuya_session_start(NULL);
    return 0;
}

int desk_tclaw_tuya_session_disconnect(void)
{
    desk_tclaw_tuya_session_stop();
    s_tuya_conn_state = TUYA_CLOUD_DISCONNECTED;
    return 0;
}

int desk_tclaw_tuya_session_reconnect(void)
{
    if (s_tuya_endpoint[0] == '\0' || s_tuya_device_id[0] == '\0') {
        s_tuya_conn_state = TUYA_CLOUD_ERROR;
        return -1;
    }
    s_tuya_conn_state = TUYA_CLOUD_RECONNECTING;
    s_tuya_conn_state = TUYA_CLOUD_CONNECTED;
    desk_tclaw_tuya_session_start(NULL);
    return 0;
}

tuya_cloud_connection_state_t desk_tclaw_tuya_session_get_connection_state(void)
{
    return s_tuya_conn_state;
}

int desk_tclaw_tuya_session_start(const char *session_id)
{
    if (session_id && strlen(session_id) > 0) {
        strncpy(s_tuya_session_id, session_id, sizeof(s_tuya_session_id) - 1);
        s_tuya_session_id[sizeof(s_tuya_session_id) - 1] = '\0';
    } else {
        snprintf(s_tuya_session_id, sizeof(s_tuya_session_id), "tuya-session-%08x", (s_turn_count + 1) * 0x7654321);
    }

    s_state = TCLAW_STATE_LISTENING;
    desk_tclaw_update_ui_screen("listening", "Listening");
    return 0;
}

const char *desk_tclaw_tuya_get_session_id(void)
{
    return s_tuya_session_id;
}

int desk_tclaw_tuya_session_feed_audio(const uint8_t *pcm_data, size_t len)
{
    if (!pcm_data || len == 0) return -1;

    if (s_tuya_session_id[0] == '\0') {
        desk_tclaw_tuya_session_start(NULL);
    }

    if (s_tuya_conn_state == TUYA_CLOUD_CONNECTED) {
        s_tuya_conn_state = TUYA_CLOUD_STREAMING;
    }

    if (s_state == TCLAW_STATE_IDLE) {
        s_state = TCLAW_STATE_LISTENING;
        desk_tclaw_update_ui_screen("listening", "Listening");
    }

    return 0;
}

int desk_tclaw_tuya_handle_cloud_event(const tuya_dialogue_event_t *event)
{
    if (!event) return -1;

    switch (event->type) {
        case TUYA_DIALOGUE_EVT_SESSION_START:
            if (event->session_id) {
                strncpy(s_tuya_session_id, event->session_id, sizeof(s_tuya_session_id) - 1);
                s_tuya_session_id[sizeof(s_tuya_session_id) - 1] = '\0';
            }
            s_state = TCLAW_STATE_LISTENING;
            desk_tclaw_update_ui_screen("listening", "Listening");
            break;

        case TUYA_DIALOGUE_EVT_STREAM_TOKEN:
            if (s_state != TCLAW_STATE_SPEAKING) {
                if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
                    return -1;
                }
                s_state = TCLAW_STATE_SPEAKING;
                s_last_response[0] = '\0';
            }

            if (event->token) {
                size_t cur_len = strlen(s_last_response);
                if (cur_len + strlen(event->token) < sizeof(s_last_response) - 1) {
                    strcat(s_last_response, event->token);
                }
                desk_tclaw_update_ui_screen("speaking", "Speaking");

                if (s_speech_stream_cb) {
                    s_speech_stream_cb(event->token, event->is_final_token, s_stream_user_data);
                }
            }
            break;

        case TUYA_DIALOGUE_EVT_TOOL_CALL:
            s_state = TCLAW_STATE_TOOL_CALLING;
            desk_tclaw_update_ui_screen("thinking", "Working");
            if (event->tool_name) {
                strncpy(s_current_pending_tool, event->tool_name, sizeof(s_current_pending_tool) - 1);
                s_current_pending_tool[sizeof(s_current_pending_tool) - 1] = '\0';

                char req_id[64];
                snprintf(req_id, sizeof(req_id), "req-tuya-%08x", (s_turn_count + 1) * 0x13579bdf);
                return desk_tclaw_trigger_tool_call(req_id, event->tool_name,
                                                    event->tool_arguments ? event->tool_arguments : "{}");
            }
            break;

        case TUYA_DIALOGUE_EVT_TTS_CHUNK:
            if (event->tts_data && event->tts_len > 0) {
                if (s_state != TCLAW_STATE_SPEAKING) {
                    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) == DESK_ARBITER_OK) {
                        s_state = TCLAW_STATE_SPEAKING;
                    }
                }
                desk_hardware_audio_play_pcm((const int16_t *)event->tts_data,
                                             event->tts_len / sizeof(int16_t),
                                             16000);
            }
            break;

        case TUYA_DIALOGUE_EVT_SESSION_END:
            desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
            desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE);
            desk_arbiter_release(DESK_RES_CAMERA_SENSOR, DESK_OWNER_TCLAW_VOICE);
            s_turn_count++;
            s_state = TCLAW_STATE_IDLE;
            s_current_pending_tool[0] = '\0';
            s_pending_request_id[0] = '\0';
            desk_tclaw_update_ui_screen("idle", "Ready");
            break;

        case TUYA_DIALOGUE_EVT_ERROR:
            desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
            desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE);
            desk_arbiter_release(DESK_RES_CAMERA_SENSOR, DESK_OWNER_TCLAW_VOICE);
            s_state = TCLAW_STATE_IDLE;
            desk_tclaw_update_ui_screen("error", "Try again");
            break;

        default:
            break;
    }

    return 0;
}

int desk_tclaw_tuya_send_tool_result(const char *session_id, const char *tool_name, const char *result_json)
{
    (void)session_id;
    (void)tool_name;
    if (result_json) {
        strncpy(s_last_tool_result, result_json, sizeof(s_last_tool_result) - 1);
        s_last_tool_result[sizeof(s_last_tool_result) - 1] = '\0';
    } else {
        s_last_tool_result[0] = '\0';
    }
    return 0;
}

int desk_tclaw_tuya_get_last_tool_result(char *out_buf, size_t max_len)
{
    if (!out_buf || max_len == 0) return -1;
    strncpy(out_buf, s_last_tool_result, max_len - 1);
    out_buf[max_len - 1] = '\0';
    return 0;
}

int desk_tclaw_tuya_session_stop(void)
{
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
    desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_TCLAW_VOICE);
    desk_arbiter_release(DESK_RES_CAMERA_SENSOR, DESK_OWNER_TCLAW_VOICE);
    s_state = TCLAW_STATE_IDLE;
    s_tuya_session_id[0] = '\0';
    s_current_pending_tool[0] = '\0';
    s_pending_request_id[0] = '\0';
    if (s_tuya_conn_state == TUYA_CLOUD_STREAMING) {
        s_tuya_conn_state = TUYA_CLOUD_CONNECTED;
    }
    desk_tclaw_update_ui_screen("idle", "Ready");
    return 0;
}

int desk_tclaw_on_tuya_netcfg_started(void)
{
    return desk_tclaw_update_ui_screen("thinking", "BLE Netcfg");
}

int desk_tclaw_on_tuya_mqtt_connected(void)
{
    s_tuya_conn_state = TUYA_CLOUD_CONNECTED;
    int rc = desk_tclaw_update_ui_screen("happy", "Online");

    /* Play brief connection chime / audio tone (800Hz PCM, 50ms) */
    static const int16_t s_connect_chime[64] = {
        0, 3211, 6392, 9511, 12539, 15446, 18204, 20787,
        23170, 25329, 27245, 28898, 30273, 31356, 32137, 32609,
        32767, 32609, 32137, 31356, 30273, 28898, 27245, 25329,
        23170, 20787, 18204, 15446, 12539, 9511, 6392, 3211,
        0, -3211, -6392, -9511, -12539, -15446, -18204, -20787,
        -23170, -25329, -27245, -28898, -30273, -31356, -32137, -32609,
        -32767, -32609, -32137, -31356, -30273, -28898, -27245, -25329,
        -23170, -20787, -18204, -15446, -12539, -9511, -6392, -3211
    };
    desk_hardware_audio_play_pcm(s_connect_chime, 64, 16000);
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);

    return rc;
}

int desk_tclaw_on_tuya_mqtt_disconnected(void)
{
    s_tuya_conn_state = TUYA_CLOUD_DISCONNECTED;
    if (s_tuya_session_id[0] != '\0') {
        desk_tclaw_tuya_session_stop();
    }
    return desk_tclaw_update_ui_screen("offline", "Offline");
}

int desk_tclaw_inject_text(const char *user_prompt)
{
    if (!user_prompt) return -1;

    if (s_tuya_session_id[0] == '\0') {
        desk_tclaw_tuya_session_start(NULL);
    }

    s_state = TCLAW_STATE_LISTENING;
    desk_tclaw_update_ui_screen("listening", "Listening");

    s_state = TCLAW_STATE_THINKING;
    desk_tclaw_update_ui_screen("thinking", "Thinking");

    /* Detect intent for tool dispatching through Tuya dialogue engine */
    char req_id[64];
    snprintf(req_id, sizeof(req_id), "req-tclaw-%08x", (s_turn_count + 1) * 0x13579bdf);

    if (strstr(user_prompt, "light") != NULL || strstr(user_prompt, "turn on") != NULL) {
        return desk_tclaw_trigger_tool_call(req_id, "ha.call_service",
                                            "{\"domain\":\"light\",\"service\":\"turn_on\",\"entity_id\":\"light.desk_lamp\"}");
    } else if (strstr(user_prompt, "remind") != NULL) {
        time_t now = time(NULL);
        if (now < 1700000000) now = 1792100000;
        char remind_at[32];
        desk_tclaw_format_iso8601(now + 3600, remind_at, sizeof(remind_at));
        char remind_args[256];
        snprintf(remind_args, sizeof(remind_args),
                 "{\"title\":\"desk reminder\",\"remind_at\":\"%s\"}", remind_at);
        return desk_tclaw_trigger_tool_call(req_id, "life.create_reminder", remind_args);
    } else if (strstr(user_prompt, "camera") != NULL || strstr(user_prompt, "snapshot") != NULL) {
        return desk_tclaw_trigger_tool_call(req_id, "camera.capture", "{\"timeout_ms\":2000}");
    } else if (strstr(user_prompt, "move") != NULL || strstr(user_prompt, "look") != NULL) {
        return desk_tclaw_trigger_tool_call(req_id, "ctrl.motion.move", "{\"pan_deg\":90.0,\"tilt_deg\":110.0,\"duration_ms\":500}");
    } else if (strstr(user_prompt, "codex") != NULL || strstr(user_prompt, "review") != NULL) {
        return desk_tclaw_trigger_tool_call(req_id, "codex.submit_turn", "{\"prompt\":\"review current branch\"}");
    }

    /* Natural contextual speech response instead of static text */
    s_turn_count++;
    char contextual_response[1024];
    snprintf(contextual_response, sizeof(contextual_response),
             "I heard your request: \"%s\". How can I help you next?", user_prompt);

    return desk_tclaw_start_speaking(contextual_response);
}

/* --------------------------------------------------------------------------
 * Single-Board Self-Test & Diagnostic Inspection
 * -------------------------------------------------------------------------- */
int desk_tclaw_self_test_report(desk_tclaw_self_test_t *out_report)
{
    desk_tclaw_self_test_t diag;
    memset(&diag, 0, sizeof(diag));
    diag.initialized = s_initialized;
    diag.state = s_state;
    diag.tuya_state = s_tuya_conn_state;
    diag.turn_count = s_turn_count;
    strncpy(diag.session_id, s_tuya_session_id, sizeof(diag.session_id) - 1);
    strncpy(diag.last_response, s_last_response, sizeof(diag.last_response) - 1);
    strncpy(diag.pending_tool, s_current_pending_tool, sizeof(diag.pending_tool) - 1);
    diag.audio_owner = desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK);
    diag.motion_owner = desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL);
    diag.camera_owner = desk_arbiter_get_owner(DESK_RES_CAMERA_SENSOR);
    diag.last_error_code = 0;

    if (!s_initialized) {
        diag.last_error_code = DESK_HW_ERR_UNAVAILABLE;
        if (out_report) *out_report = diag;
        return DESK_HW_ERR_UNAVAILABLE;
    }

    /* Dialogue state range check */
    if (s_state < TCLAW_STATE_IDLE || s_state > TCLAW_STATE_TOOL_CALLING) {
        diag.last_error_code = DESK_HW_ERR_INVALID_ARG;
        if (out_report) *out_report = diag;
        return DESK_HW_ERR_INVALID_ARG;
    }

    /* Tuya Cloud state range check */
    if (s_tuya_conn_state < TUYA_CLOUD_DISCONNECTED || s_tuya_conn_state > TUYA_CLOUD_ERROR) {
        diag.last_error_code = DESK_HW_ERR_INVALID_ARG;
        if (out_report) *out_report = diag;
        return DESK_HW_ERR_INVALID_ARG;
    }

    /* Arbiter invariant: When IDLE, audio playback must not be held by TCLAW_VOICE */
    if (s_state == TCLAW_STATE_IDLE && diag.audio_owner == DESK_OWNER_TCLAW_VOICE) {
        diag.last_error_code = DESK_HW_ERR_BUSY;
        if (out_report) *out_report = diag;
        return DESK_HW_ERR_BUSY;
    }

    /* Arbiter invariant: When SPEAKING, audio playback MUST be owned by TCLAW_VOICE */
    if (s_state == TCLAW_STATE_SPEAKING && diag.audio_owner != DESK_OWNER_TCLAW_VOICE) {
        diag.last_error_code = DESK_HW_ERR_BUSY;
        if (out_report) *out_report = diag;
        return DESK_HW_ERR_BUSY;
    }

    if (out_report) {
        *out_report = diag;
    }
    return 0;
}

int desk_tclaw_self_test(void)
{
    return desk_tclaw_self_test_report(NULL);
}
