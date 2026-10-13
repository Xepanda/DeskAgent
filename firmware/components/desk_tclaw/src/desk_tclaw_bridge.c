/**
 * @file desk_tclaw_bridge.c
 * @brief Implementation of TClaw Dialogue Bridge and Tool Routing for Desk Agent R1.
 */

#include "desk_tclaw_bridge.h"
#include "desk_hardware.h"
#include <stdio.h>
#include <string.h>

static desk_tclaw_state_t s_state = TCLAW_STATE_IDLE;

int desk_tclaw_init(void)
{
    s_state = TCLAW_STATE_IDLE;
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

int desk_tclaw_inject_text(const char *user_prompt)
{
    if (!user_prompt) return -1;

    s_state = TCLAW_STATE_THINKING;
    desk_hardware_display_set_expression("thinking", "Thinking...");

    /* Transition to speaking state to simulate dialogue completion */
    s_state = TCLAW_STATE_SPEAKING;
    desk_hardware_display_set_expression("speaking", "Replying...");

    s_state = TCLAW_STATE_IDLE;
    desk_hardware_display_set_expression("idle", "Ready");
    return 0;
}

int desk_tclaw_handle_tool_response(const char *request_id,
                                    const char *status,
                                    const char *result_json,
                                    const char *error_json)
{
    if (!request_id || !status) return -1;
    return 0;
}
