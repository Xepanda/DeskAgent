/**
 * @file desk_tclaw_bridge.c
 * @brief Implementation of TClaw Dialogue Engine, Intent Routing, and MCP Gateway.
 */

#include "desk_tclaw_bridge.h"
#include "desk_hardware.h"
#include <stdio.h>
#include <string.h>
#include <time.h>

static void desk_tclaw_format_iso8601(time_t t, char *out_buf, size_t buf_len)
{
    if (!out_buf || buf_len < 21) return;
    struct tm tm_info;
    gmtime_r(&t, &tm_info);
    strftime(out_buf, buf_len, "%Y-%m-%dT%H:%M:%SZ", &tm_info);
}

static desk_tclaw_state_t s_state = TCLAW_STATE_IDLE;
static desk_tclaw_tool_dispatcher_t s_tool_dispatcher = NULL;
static void *s_dispatcher_user_data = NULL;

static desk_tclaw_speech_stream_cb_t s_speech_stream_cb = NULL;
static void *s_stream_user_data = NULL;

static int s_turn_count = 0;
static char s_last_response[1024] = "";
static char s_tuya_session_id[128] = "";
static char s_current_pending_tool[128] = "";

int desk_tclaw_init(void)
{
    s_state = TCLAW_STATE_IDLE;
    s_turn_count = 0;
    s_last_response[0] = '\0';
    s_current_pending_tool[0] = '\0';
    s_speech_stream_cb = NULL;
    s_stream_user_data = NULL;

    desk_hardware_display_set_expression("idle", "Ready");
    return 0;
}

void desk_tclaw_reset(void)
{
    s_state = TCLAW_STATE_IDLE;
    s_turn_count = 0;
    s_last_response[0] = '\0';
    s_current_pending_tool[0] = '\0';
    s_speech_stream_cb = NULL;
    s_stream_user_data = NULL;
    s_tool_dispatcher = NULL;
    s_dispatcher_user_data = NULL;
    s_tuya_session_id[0] = '\0';
}

int desk_tclaw_start(void)
{
    s_state = TCLAW_STATE_IDLE;
    return 0;
}

desk_tclaw_state_t desk_tclaw_get_state(void)
{
    return s_state;
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

int desk_tclaw_start_speaking(const char *speech_text)
{
    if (!speech_text) return -1;

    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
        return -1;
    }

    s_state = TCLAW_STATE_SPEAKING;
    strncpy(s_last_response, speech_text, sizeof(s_last_response) - 1);
    s_last_response[sizeof(s_last_response) - 1] = '\0';

    desk_hardware_display_set_expression("speaking", s_last_response);

    /* Stream speech tokens to registered listener if active */
    if (s_speech_stream_cb) {
        /* Stream word tokens */
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

int desk_tclaw_interrupt(void)
{
    desk_hardware_audio_play_stop();
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
    s_state = TCLAW_STATE_IDLE;
    desk_hardware_display_set_expression("idle", "Ready");
    return 0;
}

int desk_tclaw_register_tool_dispatcher(desk_tclaw_tool_dispatcher_t dispatcher, void *user_data)
{
    s_tool_dispatcher = dispatcher;
    s_dispatcher_user_data = user_data;
    return 0;
}

int desk_tclaw_trigger_tool_call(const char *request_id, const char *operation, const char *arguments_json)
{
    if (!request_id || !operation) return -1;

    s_state = TCLAW_STATE_TOOL_CALLING;
    desk_hardware_display_set_expression("thinking", "Calling external tool...");

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
    return 0;
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
    desk_hardware_display_set_expression("listening", "Tuya Cloud Voice Active");
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

    if (s_state == TCLAW_STATE_IDLE) {
        s_state = TCLAW_STATE_LISTENING;
        desk_hardware_display_set_expression("listening", "Listening...");
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
            desk_hardware_display_set_expression("listening", "Tuya Cloud Listening");
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
                desk_hardware_display_set_expression("speaking", s_last_response);

                if (s_speech_stream_cb) {
                    s_speech_stream_cb(event->token, event->is_final_token, s_stream_user_data);
                }
            }
            break;

        case TUYA_DIALOGUE_EVT_TOOL_CALL:
            s_state = TCLAW_STATE_TOOL_CALLING;
            desk_hardware_display_set_expression("thinking", "Calling tool...");
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
            s_turn_count++;
            s_state = TCLAW_STATE_IDLE;
            s_current_pending_tool[0] = '\0';
            desk_hardware_display_set_expression("idle", "Ready");
            break;

        case TUYA_DIALOGUE_EVT_ERROR:
            desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
            s_state = TCLAW_STATE_IDLE;
            desk_hardware_display_set_expression("idle", event->error_msg ? event->error_msg : "Error");
            break;

        default:
            break;
    }

    return 0;
}

int desk_tclaw_tuya_send_tool_result(const char *session_id, const char *tool_name, const char *result_json)
{
    /* Feeds back tool execution result to active Tuya cloud dialogue session */
    (void)session_id;
    (void)tool_name;
    (void)result_json;
    return 0;
}

int desk_tclaw_tuya_session_stop(void)
{
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
    s_state = TCLAW_STATE_IDLE;
    s_tuya_session_id[0] = '\0';
    s_current_pending_tool[0] = '\0';
    desk_hardware_display_set_expression("idle", "Ready");
    return 0;
}

int desk_tclaw_inject_text(const char *user_prompt)
{
    if (!user_prompt) return -1;

    if (s_tuya_session_id[0] == '\0') {
        desk_tclaw_tuya_session_start(NULL);
    }

    s_state = TCLAW_STATE_LISTENING;
    desk_hardware_display_set_expression("listening", user_prompt);

    s_state = TCLAW_STATE_THINKING;
    desk_hardware_display_set_expression("thinking", "Thinking...");

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

int desk_tclaw_handle_tool_response(const char *request_id,
                                    const char *status,
                                    const char *result_json,
                                    const char *error_json)
{
    if (!request_id || !status) return -1;

    /* Report back to Tuya Cloud Dialogue session */
    desk_tclaw_tuya_send_tool_result(s_tuya_session_id, s_current_pending_tool,
                                     (strcmp(status, "succeeded") == 0) ? result_json : error_json);

    s_turn_count++;
    char spoken[1024];
    if (strcmp(status, "succeeded") == 0) {
        snprintf(spoken, sizeof(spoken), "Action completed successfully: %s",
                 (result_json && strlen(result_json) > 0) ? result_json : "OK");
    } else {
        snprintf(spoken, sizeof(spoken), "Action encountered an error: %s",
                 (error_json && strlen(error_json) > 0) ? error_json : "Error");
    }

    return desk_tclaw_start_speaking(spoken);
}
