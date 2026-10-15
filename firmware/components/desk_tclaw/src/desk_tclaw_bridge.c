/**
 * @file desk_tclaw_bridge.c
 * @brief Implementation of TClaw Dialogue Engine, Intent Routing, and MCP Gateway.
 */

#include "desk_tclaw_bridge.h"
#include "desk_hardware.h"
#include <stdio.h>
#include <string.h>

static desk_tclaw_state_t s_state = TCLAW_STATE_IDLE;
static desk_tclaw_tool_dispatcher_t s_tool_dispatcher = NULL;
static void *s_dispatcher_user_data = NULL;

static desk_tclaw_speech_stream_cb_t s_speech_stream_cb = NULL;
static void *s_stream_user_data = NULL;

static int s_turn_count = 0;
static char s_last_response[1024] = "";

int desk_tclaw_init(void)
{
    s_state = TCLAW_STATE_IDLE;
    s_tool_dispatcher = NULL;
    s_dispatcher_user_data = NULL;
    s_speech_stream_cb = NULL;
    s_stream_user_data = NULL;
    s_turn_count = 0;
    s_last_response[0] = '\0';

    desk_hardware_display_set_expression("idle", "Ready");
    return 0;
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
        char env[2048];
        snprintf(env, sizeof(env),
                 "{\"schema_version\":\"1.0.0\","
                 "\"request_id\":\"%s\","
                 "\"operation\":\"%s\","
                 "\"arguments\":%s,"
                 "\"expires_at\":\"2026-10-15T23:59:59Z\"}",
                 request_id,
                 operation,
                 arguments_json ? arguments_json : "{}");

        return s_tool_dispatcher(env, s_dispatcher_user_data);
    }
    return 0;
}

int desk_tclaw_inject_text(const char *user_prompt)
{
    if (!user_prompt) return -1;

    s_state = TCLAW_STATE_LISTENING;
    desk_hardware_display_set_expression("listening", user_prompt);

    s_state = TCLAW_STATE_THINKING;
    desk_hardware_display_set_expression("thinking", "Thinking...");

    /* Detect intent for tool dispatching */
    char req_id[64];
    snprintf(req_id, sizeof(req_id), "req-tclaw-%08x", (s_turn_count + 1) * 0x13579bdf);

    if (strstr(user_prompt, "light") != NULL || strstr(user_prompt, "turn on") != NULL) {
        return desk_tclaw_trigger_tool_call(req_id, "ha.call_service",
                                            "{\"domain\":\"light\",\"service\":\"turn_on\",\"entity_id\":\"light.desk_lamp\"}");
    } else if (strstr(user_prompt, "remind") != NULL) {
        return desk_tclaw_trigger_tool_call(req_id, "life.create_reminder",
                                            "{\"title\":\"desk reminder\",\"remind_at\":\"2026-10-15T20:00:00Z\"}");
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
