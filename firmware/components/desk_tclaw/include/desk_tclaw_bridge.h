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
#include <stddef.h>

typedef enum {
    TCLAW_STATE_IDLE = 0,
    TCLAW_STATE_LISTENING,
    TCLAW_STATE_THINKING,
    TCLAW_STATE_SPEAKING,
    TCLAW_STATE_TOOL_CALLING
} desk_tclaw_state_t;

typedef int (*desk_tclaw_tool_dispatcher_t)(const char *request_envelope_json, void *user_data);

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
 * @brief Begin speech output, acquiring audio playback ownership.
 */
int desk_tclaw_start_speaking(const char *speech_text);

/**
 * @brief Barge-in / Touch interrupt active speech output.
 */
int desk_tclaw_interrupt(void);

/**
 * @brief Register callback for outbound MCP / external tool requests.
 */
int desk_tclaw_register_tool_dispatcher(desk_tclaw_tool_dispatcher_t dispatcher, void *user_data);

/**
 * @brief Trigger an external tool call through the MCP gateway.
 */
int desk_tclaw_trigger_tool_call(const char *request_id, const char *operation, const char *arguments_json);

/**
 * @brief Handle external tool execution result returned from Python Application.
 */
int desk_tclaw_handle_tool_response(const char *request_id,
                                    const char *status,
                                    const char *result_json,
                                    const char *error_json);

/**
 * @brief Speech stream token callback for streaming TTS / audio synthesis.
 */
typedef void (*desk_tclaw_speech_stream_cb_t)(const char *token, bool is_final, void *user_data);
int desk_tclaw_register_speech_stream_cb(desk_tclaw_speech_stream_cb_t cb, void *user_data);

/**
 * @brief Retrieve last dialogue response text.
 */
int desk_tclaw_get_last_response(char *out_buf, size_t max_len);

/**
 * @brief Retrieve total dialogue turns completed.
 */
int desk_tclaw_get_turn_count(void);

#ifdef __cplusplus
}
#endif

#endif /* DESK_TCLAW_BRIDGE_H */
