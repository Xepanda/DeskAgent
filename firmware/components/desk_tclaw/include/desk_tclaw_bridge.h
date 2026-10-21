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
#include "desk_arbiter.h"

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
 * @brief Reset TClaw dialogue state and clear callbacks.
 */
void desk_tclaw_reset(void);

/**
 * @brief Start TClaw voice dialogue task.
 */
int desk_tclaw_start(void);

/**
 * @brief Query current TClaw state.
 */
desk_tclaw_state_t desk_tclaw_get_state(void);

/**
 * @brief Check if TClaw dialogue engine has been initialized.
 */
bool desk_tclaw_is_initialized(void);

/* Short-lived display-only overlay used while an operator pairing window is open. */
int desk_tclaw_pairing_ui_set_code(const char *code);
void desk_tclaw_pairing_ui_clear(void);

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

/**
 * @brief Tuya Cloud Dialogue Event Types
 */
typedef enum {
    TUYA_DIALOGUE_EVT_SESSION_START = 1,
    TUYA_DIALOGUE_EVT_STREAM_TOKEN,
    TUYA_DIALOGUE_EVT_TOOL_CALL,
    TUYA_DIALOGUE_EVT_TTS_CHUNK,
    TUYA_DIALOGUE_EVT_SESSION_END,
    TUYA_DIALOGUE_EVT_ERROR
} tuya_dialogue_event_type_t;

typedef struct {
    tuya_dialogue_event_type_t type;
    const char *session_id;
    const char *token;           /* For STREAM_TOKEN */
    bool is_final_token;
    const char *tool_name;       /* For TOOL_CALL */
    const char *tool_arguments;  /* For TOOL_CALL (JSON args) */
    const uint8_t *tts_data;     /* For TTS_CHUNK */
    size_t tts_len;
    const char *error_msg;       /* For ERROR */
} tuya_dialogue_event_t;

/**
 * @brief Tuya Cloud Connection States
 */
typedef enum {
    TUYA_CLOUD_DISCONNECTED = 0,
    TUYA_CLOUD_CONNECTING,
    TUYA_CLOUD_CONNECTED,
    TUYA_CLOUD_STREAMING,
    TUYA_CLOUD_RECONNECTING,
    TUYA_CLOUD_ERROR
} tuya_cloud_connection_state_t;

/**
 * @brief Tuya Cloud Voice Dialogue Session Management
 */
int desk_tclaw_tuya_session_connect(const char *endpoint, const char *device_id, const char *token);
int desk_tclaw_tuya_session_disconnect(void);
int desk_tclaw_tuya_session_reconnect(void);
tuya_cloud_connection_state_t desk_tclaw_tuya_session_get_connection_state(void);
int desk_tclaw_tuya_session_start(const char *session_id);
int desk_tclaw_tuya_session_feed_audio(const uint8_t *pcm_data, size_t len);
int desk_tclaw_tuya_handle_cloud_event(const tuya_dialogue_event_t *event);
int desk_tclaw_tuya_send_tool_result(const char *session_id, const char *tool_name, const char *result_json);
int desk_tclaw_tuya_get_last_tool_result(char *out_buf, size_t max_len);
int desk_tclaw_tuya_session_stop(void);
const char *desk_tclaw_tuya_get_session_id(void);

/**
 * @brief Handle Tuya MQTT cloud connection event.
 *
 * Transitions Tuya connection state to TUYA_CLOUD_CONNECTED, updates
 * screen expression to 'happy' / 'Online', and plays connection audio cue.
 *
 * @return 0 on success, negative error code on failure.
 */
int desk_tclaw_on_tuya_mqtt_connected(void);

/**
 * @brief Handle Tuya MQTT cloud disconnection event.
 *
 * Transitions Tuya connection state to TUYA_CLOUD_DISCONNECTED and
 * updates screen expression to 'idle' / 'Cloud Offline'.
 *
 * @return 0 on success, negative error code on failure.
 */
int desk_tclaw_on_tuya_mqtt_disconnected(void);

/**
 * @brief Handle Tuya Network Configuration start event (e.g. BLE netcfg).
 *
 * Updates screen expression to 'thinking' / 'BLE Netcfg'.
 *
 * @return 0 on success, negative error code on failure.
 */
int desk_tclaw_on_tuya_netcfg_started(void);

/**
 * @brief TClaw Single Board Self-Test Report Structure.
 */
typedef struct {
    bool initialized;                     /**< TClaw framework initialized */
    desk_tclaw_state_t state;             /**< Current dialogue state */
    tuya_cloud_connection_state_t tuya_state; /**< Tuya cloud connection state */
    int turn_count;                       /**< Completed dialogue turns */
    char session_id[128];                 /**< Active Tuya session identifier */
    char last_response[256];              /**< Last spoken response text */
    char pending_tool[128];               /**< Name of currently pending tool call */
    desk_resource_owner_t audio_owner;    /**< Current owner of DESK_RES_AUDIO_PLAYBACK */
    desk_resource_owner_t motion_owner;   /**< Current owner of DESK_RES_MOTION_GIMBAL */
    desk_resource_owner_t camera_owner;   /**< Current owner of DESK_RES_CAMERA_SENSOR */
    int last_error_code;                  /**< 0 if normal, or DESK_HW_ERR_* */
} desk_tclaw_self_test_t;

/**
 * @brief Execute TClaw single board self-test.
 * @return 0 on success (all checks pass), negative DESK_HW_ERR_* on failure.
 */
int desk_tclaw_self_test(void);

/**
 * @brief Execute TClaw single board self-test and retrieve diagnostic report.
 * @param out_report Pointer to self-test diagnostic report structure (can be NULL).
 * @return 0 on success, negative DESK_HW_ERR_* on failure.
 */
int desk_tclaw_self_test_report(desk_tclaw_self_test_t *out_report);

/**
 * @brief Execute a built-in hardware tool or format contract ResponseEnvelope.
 *
 * Implements Watcher BSP operations (motion, camera, screen expression) with:
 * - Strict resource arbitration (AUDIO_PLAYBACK, MOTION_GIMBAL, CAMERA_SENSOR, DISPLAY_UI)
 * - CR-002 motion semantic compliance (status="accepted", accepted=true, state_confirmed=false)
 * - True camera capture (errors if unavailable, strictly never generates synthetic frames)
 *
 * @param request_id Unique request ID.
 * @param operation Tool operation name (e.g., "motion.move", "camera.capture", "expression.set").
 * @param arguments_json JSON arguments object string.
 * @param out_response_json Optional buffer to receive full contract ResponseEnvelope JSON.
 * @param max_len Size of out_response_json buffer.
 * @return 0 on successful execution or dispatch, negative error code on fatal failure.
 */
int desk_tclaw_execute_tool(const char *request_id,
                            const char *operation,
                            const char *arguments_json,
                            char *out_response_json,
                            size_t max_len);

/**
 * @brief Check if an operation name corresponds to a supported built-in hardware tool.
 */
bool desk_tclaw_is_builtin_tool(const char *operation);

/**
 * @brief External tool execution timeout configuration (milliseconds).
 */
void desk_tclaw_set_tool_timeout_ms(uint32_t timeout_ms);
uint32_t desk_tclaw_get_tool_timeout_ms(void);

/**
 * @brief Explicitly trigger tool execution timeout handling for a pending request.
 */
int desk_tclaw_handle_tool_timeout(const char *request_id);

/**
 * @brief Poll for external tool execution timeout.
 * @param now_ms Current system timestamp in milliseconds (pass 0 to query clock).
 * @return 0 if normal, 1 if a timeout was triggered, negative on error.
 */
int desk_tclaw_poll_timeouts(uint64_t now_ms);

/**
 * @brief Retrieve pending tool operation name and request ID.
 */
const char *desk_tclaw_get_pending_tool(void);
const char *desk_tclaw_get_pending_request_id(void);

#ifdef __cplusplus
}
#endif

#endif /* DESK_TCLAW_BRIDGE_H */
