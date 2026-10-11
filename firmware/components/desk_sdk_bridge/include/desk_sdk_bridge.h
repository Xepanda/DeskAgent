/**
 * @file desk_sdk_bridge.h
 * @brief Desk Agent R1 Official WatcheRobot Python SDK Bridge.
 *
 * Implements standard WatcheRobot device protocol endpoints:
 * - sys.client.hello / sys.ack handshake and capabilities negotiation
 * - WSPK binary framing for audio/image streams
 * - Inputs event notifications (backtouch, screentouch, roller)
 * - Bidirectional RequestEnvelope / ResponseEnvelope tool routing
 */

#ifndef DESK_SDK_BRIDGE_H
#define DESK_SDK_BRIDGE_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>

/**
 * @brief Capabilities declared by Desk Agent firmware to the Python SDK.
 */
#define DESK_SDK_CAP_INPUTS        "inputs"
#define DESK_SDK_CAP_MOTION        "motion"
#define DESK_SDK_CAP_CAMERA        "camera"
#define DESK_SDK_CAP_AUDIO_STREAM  "audio.stream"
#define DESK_SDK_CAP_EXPRESSION    "expression.audio_follow.v1"

typedef int (*desk_sdk_request_handler_t)(const char *request_json,
                                          char *out_response_json,
                                          size_t max_out_len);

/**
 * @brief Initialize SDK Protocol Bridge.
 */
int desk_sdk_bridge_init(void);

/**
 * @brief Process incoming JSON command from Python SDK client.
 *
 * @param json_in Raw JSON command string.
 * @param out_buf Buffer to store JSON response string.
 * @param max_out_len Maximum capacity of out_buf.
 * @return 0 on success, negative error code on failure.
 */
int desk_sdk_bridge_process_command(const char *json_in, char *out_buf, size_t max_out_len);

/**
 * @brief Send an event notification to connected Python SDK client.
 *
 * Formats payload according to contracts/schemas/envelope_event.json.
 */
int desk_sdk_bridge_send_event(const char *event_type, const char *payload_json);

/**
 * @brief Forward external tool request from TClaw to Python Application.
 */
int desk_sdk_bridge_dispatch_tool_request(const char *request_envelope_json);

#ifdef __cplusplus
}
#endif

#endif /* DESK_SDK_BRIDGE_H */
