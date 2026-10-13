/**
 * @file desk_sdk_bridge.h
 * @brief Desk Agent R1 Official WatcheRobot Python SDK Bridge.
 *
 * Implements standard WatcheRobot device protocol endpoints:
 * - sys.client.hello / sys.ack handshake and capabilities negotiation
 * - WSPK binary framing for audio/image streams
 * - Inputs event notifications (backtouch, screentouch, roller)
 * - Motion & Camera commands with strict resource arbitration
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
#include "desk_hardware.h"
#include "desk_arbiter.h"

/**
 * @brief Capabilities declared by Desk Agent firmware to the Python SDK.
 */
#define DESK_SDK_CAP_INPUTS        "inputs"
#define DESK_SDK_CAP_MOTION        "motion"
#define DESK_SDK_CAP_CAMERA        "camera"
#define DESK_SDK_CAP_AUDIO_STREAM  "audio.stream"
#define DESK_SDK_CAP_EXPRESSION    "expression.audio_follow.v1"

/**
 * @brief WSPK Binary Framing Constants
 */
#define DESK_WSPK_MAGIC            "WSPK"
#define DESK_WSPK_HEADER_SIZE      16
#define DESK_WSPK_TYPE_AUDIO       1
#define DESK_WSPK_TYPE_IMAGE       2
#define DESK_WSPK_TYPE_DATA        3
#define DESK_WSPK_MAX_PAYLOAD_SIZE (128 * 1024) /* 128KB max to prevent OOM */

#pragma pack(push, 1)
typedef struct {
    char magic[4];          /* "WSPK" */
    uint8_t frame_type;     /* 1: Audio, 2: Camera JPEG, 3: Data */
    uint8_t flags;          /* Flags, e.g. 0x01: EOS, 0x02: Keyframe */
    uint16_t stream_id;     /* Stream ID (Big-Endian network order) */
    uint32_t sequence;      /* Packet sequence (Big-Endian network order) */
    uint32_t payload_size;  /* Payload length (Big-Endian network order) */
} desk_wspk_header_t;
#pragma pack(pop)

typedef int (*desk_sdk_outbound_send_cb_t)(const char *data, size_t len, void *user_data);
typedef int (*desk_sdk_tool_response_cb_t)(const char *response_envelope_json, void *user_data);

/**
 * @brief Initialize SDK Protocol Bridge.
 */
int desk_sdk_bridge_init(void);

/**
 * @brief Reset bridge state (counters, connection flag).
 */
void desk_sdk_bridge_reset(void);

/**
 * @brief Check if client has established connection via sys.client.hello.
 */
bool desk_sdk_bridge_is_connected(void);

/**
 * @brief Register callback for outbound message transmission (events, tool requests).
 */
int desk_sdk_bridge_register_outbound(desk_sdk_outbound_send_cb_t cb, void *user_data);

/**
 * @brief Register callback to receive external tool response envelopes from Python app.
 */
int desk_sdk_bridge_register_tool_response_cb(desk_sdk_tool_response_cb_t cb, void *user_data);

/**
 * @brief Pack WSPK binary header.
 */
int desk_wspk_pack_header(uint8_t frame_type,
                          uint8_t flags,
                          uint16_t stream_id,
                          uint32_t sequence,
                          uint32_t payload_size,
                          uint8_t *out_buf,
                          size_t buf_len);

/**
 * @brief Unpack and validate WSPK binary header.
 */
int desk_wspk_unpack_header(const uint8_t *buf,
                            size_t buf_len,
                            desk_wspk_header_t *out_hdr);

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
 */
int desk_sdk_bridge_send_event(const char *event_type, const char *payload_json);

/**
 * @brief Format and dispatch hardware touch input event as contract EventEnvelope.
 */
int desk_sdk_bridge_send_touch_event(desk_touch_target_t target,
                                     desk_touch_action_t action,
                                     char *out_event_json,
                                     size_t max_len);

/**
 * @brief Forward external tool request from TClaw to Python Application.
 */
int desk_sdk_bridge_dispatch_tool_request(const char *request_envelope_json);

/**
 * @brief Handle external tool response envelope returned by Python Application.
 */
int desk_sdk_bridge_handle_tool_response(const char *response_envelope_json);

#ifdef __cplusplus
}
#endif

#endif /* DESK_SDK_BRIDGE_H */
