/**
 * @file desk_tclaw_bridge.c
 * @brief Implementation of TClaw Dialogue Bridge and Tool Routing for Desk Agent R1.
 */

#include "desk_tclaw_bridge.h"
#include "desk_hardware.h"
#include <stdio.h>
#include <string.h>

static desk_tclaw_state_t s_state = TCLAW_STATE_IDLE;
static desk_tclaw_tool_dispatcher_t s_tool_dispatcher = NULL;
static void *s_dispatcher_user_data = NULL;

int desk_tclaw_init(void)
{
    s_state = TCLAW_STATE_IDLE;
    s_tool_dispatcher = NULL;
    s_dispatcher_user_data = NULL;
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

int desk_tclaw_start_speaking(const char *speech_text)
{
    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
        return -1;
    }

    s_state = TCLAW_STATE_SPEAKING;
    desk_hardware_display_set_expression("speaking", speech_text ? speech_text : "Speaking...");
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

int desk_tclaw_inject_text(const char *user_prompt)
{
    if (!user_prompt) return -1;

    s_state = TCLAW_STATE_LISTENING;
    desk_hardware_display_set_expression("listening", user_prompt);

    s_state = TCLAW_STATE_THINKING;
    desk_hardware_display_set_expression("thinking", "Thinking...");

    /* Start speech output */
    return desk_tclaw_start_speaking("Processed response");
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
                 "\"expires_at\":\"2026-10-13T23:59:59Z\"}",
                 request_id,
                 operation,
                 arguments_json ? arguments_json : "{}");

        return s_tool_dispatcher(env, s_dispatcher_user_data);
    }
    return 0;
}

int desk_tclaw_handle_tool_response(const char *request_id,
                                    const char *status,
                                    const char *result_json,
                                    const char *error_json)
{
    if (!request_id || !status) return -1;

    if (strcmp(status, "succeeded") == 0) {
        return desk_tclaw_start_speaking("Tool action completed successfully");
    } else {
        return desk_tclaw_start_speaking("Tool action encountered an error");
    }
}
