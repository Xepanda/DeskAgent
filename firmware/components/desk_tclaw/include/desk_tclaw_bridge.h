/**
 * @file desk_tclaw_bridge.h
 * @brief Desk Agent R1 TClaw Framework Integration and MCP Gateway.
 */

#ifndef DESK_TCLAW_BRIDGE_H
#define DESK_TCLAW_BRIDGE_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>

typedef enum {
    TCLAW_STATE_IDLE = 0,
    TCLAW_STATE_LISTENING,
    TCLAW_STATE_THINKING,
    TCLAW_STATE_SPEAKING,
    TCLAW_STATE_TOOL_CALLING
} desk_tclaw_state_t;

/**
 * @brief Initialize TClaw dialogue loop and MCP tools registry.
 */
int desk_tclaw_init(void);

/**
 * @brief Start TClaw voice dialogue task.
 */
int desk_tclaw_start(void);

/**
 * @brief Query current TClaw state.
 */
desk_tclaw_state_t desk_tclaw_get_state(void);

/**
 * @brief Inject synthetic user text into TClaw session (for automated testing).
 */
int desk_tclaw_inject_text(const char *user_prompt);

/**
 * @brief Handle external tool execution result returned from Python Application.
 */
int desk_tclaw_handle_tool_response(const char *request_id,
                                    const char *status,
                                    const char *result_json,
                                    const char *error_json);

#ifdef __cplusplus
}
#endif

#endif /* DESK_TCLAW_BRIDGE_H */
