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
#define DESK_SDK_CAP_AUDIO_STREAM_ALT "audio_stream"
#define DESK_SDK_CAP_EXPRESSION    "expression.audio_follow.v1"
#define DESK_SDK_CAP_EXPRESSION_ALT "expression"
#define DESK_SDK_CAP_MCP_TOOLS     "mcp_tools"

/**
 * @brief Pending request states in the asynchronous correlation table.
 */
typedef enum {
    DESK_SDK_REQ_STATE_EMPTY = 0,
    DESK_SDK_REQ_STATE_PENDING,
    DESK_SDK_REQ_STATE_ACCEPTED,
    DESK_SDK_REQ_STATE_COMPLETED,
    DESK_SDK_REQ_STATE_FAILED,
    DESK_SDK_REQ_STATE_TIMED_OUT
} desk_sdk_req_state_t;

/**
 * @brief WSPK Binary Framing Constants
 */
#define DESK_WSPK_MAGIC            "WSPK"
#define DESK_WSPK_HEADER_SIZE      16
#define DESK_WSPK_TYPE_AUDIO       1
#define DESK_WSPK_TYPE_VIDEO       2
#define DESK_WSPK_TYPE_IMAGE       3
#define DESK_WSPK_TYPE_DATA        4
#define DESK_WSPK_MAX_PAYLOAD_SIZE (128 * 1024) /* 128KB max to prevent OOM */

/**
 * @brief Maximum length (bytes, excluding the terminating '\n') of a single
 *        newline-delimited JSON frame accepted on the TCP SDK channel.
 *
 * Per interfaces.md 6.3.2 the receive path MUST define and enforce a maximum
 * frame length; an oversized frame is discarded and the reader resynchronises
 * at the next '\n' without corrupting subsequent message boundaries.
 * This limit applies to the JSON control channel only -- the WSPK binary media
 * framing (<4sBBHII) is unaffected.
 */
#define DESK_SDK_MAX_FRAME_LEN 8192

/**
 * @brief Return codes for desk_sdk_bridge_process_pairing_udp().
 *
 * DESK_SDK_PAIRING_OK: the pairing code was validated and a pair.accept reply
 *                      has been written to udp_out.
 * DESK_SDK_PAIRING_REJECTED: fail-closed rejection (missing/unconfigured/
 *                      malformed/mismatching pairing_code). No wire reply is
 *                      produced; udp_out is left empty. Per interfaces.md 6.4
 *                      the official contract defines no rejection message type
 *                      (see CR-001), so the request is silently dropped.
 */
#define DESK_SDK_PAIRING_OK        0
#define DESK_SDK_PAIRING_REJECTED  1
#define DESK_SDK_PAIRING_ERR_ARG  (-1)

#pragma pack(push, 1)
typedef struct {
    char magic[4];          /* "WSPK" */
    uint8_t frame_type;     /* 1: Audio, 2: Camera JPEG, 3: Data */
    uint8_t flags;          /* Flags, e.g. 0x01: EOS, 0x02: Keyframe */
    uint16_t stream_id;     /* Stream ID (Little-Endian per official SDK) */
    uint32_t sequence;      /* Packet sequence (Little-Endian) */
    uint32_t payload_size;  /* Payload length (Little-Endian) */
} desk_wspk_header_t;
#pragma pack(pop)

#define DESK_WSPK_LEGACY_HEADER_SIZE 14

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
 * @brief Pack WSPK binary header (16-byte Little Endian <4sBBHII).
 */
int desk_wspk_pack_header(uint8_t frame_type,
                          uint8_t flags,
                          uint16_t stream_id,
                          uint32_t sequence,
                          uint32_t payload_size,
                          uint8_t *out_buf,
                          size_t buf_len);

/**
 * @brief Unpack and validate WSPK binary header (supports 16-byte and 14-byte legacy).
 */
int desk_wspk_unpack_header(const uint8_t *buf,
                            size_t buf_len,
                            desk_wspk_header_t *out_hdr);

/**
 * @brief Pack a complete WSPK media frame (Header + Payload).
 */
int desk_sdk_bridge_pack_media_frame(uint8_t frame_type,
                                     uint8_t flags,
                                     uint16_t stream_id,
                                     uint32_t sequence,
                                     const uint8_t *payload,
                                     size_t payload_len,
                                     uint8_t *out_buf,
                                     size_t max_buf_len,
                                     size_t *out_len);

/**
 * @brief Send a complete WSPK binary media frame over active socket connection or outbound cb.
 */
int desk_sdk_bridge_send_media_frame(uint8_t frame_type,
                                     uint8_t flags,
                                     uint16_t stream_id,
                                     uint32_t sequence,
                                     const uint8_t *payload,
                                     size_t payload_len);

/**
 * @brief Process UDP pairing request (port 37021 protocol watcher_lan_pairing_v1).
 *
 * The request's `pairing_code` is validated (6 digits, exact match) against the
 * device-side expected code provisioned via desk_sdk_bridge_set_pairing_code().
 * Fail-closed: when no expected code is configured, or the code is malformed or
 * mismatching, DESK_SDK_PAIRING_REJECTED is returned and udp_out is NOT written
 * (no `pair.accept`, no `session_token`, no `s_paired`). See interfaces.md 6.4.
 *
 * @return DESK_SDK_PAIRING_OK on success, DESK_SDK_PAIRING_REJECTED on rejection,
 *         DESK_SDK_PAIRING_ERR_ARG on invalid arguments.
 */
int desk_sdk_bridge_process_pairing_udp(const char *udp_in, char *udp_out, size_t max_out_len);

/**
 * @brief Provision the device-side expected pairing code (6 decimal digits).
 *
 * This is the ONLY authoritative source for the expected code; no production
 * pairing code is ever hardcoded. The code is never logged (redacted per the
 * official redact_sensitive_fields rule). Passing NULL (or an empty string)
 * clears the configuration, which makes pairing fail-closed again.
 *
 * @param code 6-digit code matching ^[0-9]{6}$, or NULL/"" to clear.
 * @return 0 on success, -1 if `code` is non-NULL and not exactly 6 digits.
 */
int desk_sdk_bridge_set_pairing_code(const char *code);

/**
 * @brief Report whether an expected pairing code has been provisioned.
 * @return true if pairing can succeed, false if pairing is fail-closed.
 */
bool desk_sdk_bridge_is_pairing_code_configured(void);

/**
 * @brief Parse top-level string field from JSON payload.
 *
 * Scans only top-level (depth==1) keys in a JSON object, ignoring any nested
 * occurrences inside child objects or arrays, string literals, or escaped quotes.
 *
 * @param json Input JSON string (must start with '{').
 * @param key Top-level key name to search for (without quotes).
 * @param out_val Buffer to receive the extracted string value (unescaped).
 * @param max_len Maximum length of out_val buffer including null terminator.
 * @return true if key was found at top level and parsed into out_val, false otherwise.
 */
bool json_parse_top_level_str(const char *json, const char *key, char *out_val, size_t max_len);

/**
 * @brief Process incoming JSON command from Python SDK client.
 *
 * Supported command types include:
 * - sys.ping -> sys.pong
 * - sys.client.hello -> sys.ack (capabilities & auth negotiation)
 * - ctrl.motion.move / ctrl.motion.stop -> sys.ack with CR-002 ACK-only semantics
 *   (status="accepted", accepted=true, state_confirmed=false)
 * - ctrl.camera.acquire / ctrl.camera.release / ctrl.camera.capture
 * - ctrl.microphone.open / ctrl.microphone.close
 * - ctrl.expression.play / ctrl.expression.set / ctrl.expression.audio_follow
 */
int desk_sdk_bridge_process_command(const char *json_in, char *out_buf, size_t max_out_len);
#define desk_sdk_bridge_process_line desk_sdk_bridge_process_command

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

/**
 * @brief Start local socket server for SDK and test clients.
 */
int desk_sdk_socket_server_start(uint16_t port);
int desk_sdk_socket_server_start_ex(const char *bind_addr, uint16_t port);

/**
 * @brief Poll and process socket events.
 */
int desk_sdk_socket_server_poll(int timeout_ms);

/**
 * @brief Stop local socket server.
 */
int desk_sdk_socket_server_stop(void);

/**
 * @brief Get currently bound socket port.
 */
int desk_sdk_socket_server_get_port(void);

/**
 * @brief Start UDP Pairing Discovery Server (Port 37021 watcher_lan_pairing_v1).
 */
int desk_sdk_pairing_server_start(uint16_t port);

/**
 * @brief Poll and process UDP pairing discovery requests.
 */
int desk_sdk_pairing_server_poll(int timeout_ms);

/**
 * @brief Stop UDP pairing server.
 */
int desk_sdk_pairing_server_stop(void);

/**
 * @brief Get bound pairing UDP port.
 */
int desk_sdk_pairing_server_get_port(void);

/**
 * @brief Provision the active pairing session token (credential material only).
 *
 * The value is validated against the official `session_token` format
 * `^[0-9a-f]{64}$` (64 lowercase hexadecimal characters) and REJECTED (-1) if it
 * does not conform: a non-conforming credential would be echoed verbatim into
 * `pair.accept`, producing a message the official SDK's `parse_udp_message()`
 * refuses. A rejected call leaves any previously armed credential untouched.
 *
 * R9 (interfaces.md 6.6): this entry point sets credential material ONLY and
 * MUST NOT mark the device as paired. `s_paired` means "a client has completed
 * a pairing handshake" and is only set by the validated pairing path in
 * desk_sdk_bridge_process_pairing_udp(). Enforcing a token on the handshake
 * remains the job of desk_sdk_bridge_set_auth_required().
 *
 * @param token 64 lowercase hex characters matching `^[0-9a-f]{64}$`;
 *              pass NULL to mint a fresh conforming random credential instead.
 * @return 0 on success, -1 if `token` is non-NULL and does not conform.
 */
int desk_sdk_bridge_set_pairing_token(const char *token);

/**
 * @brief Get currently active pairing session token.
 */
int desk_sdk_bridge_get_pairing_token(char *token, size_t max_len);

/**
 * @brief Enable or disable mandatory pairing token authentication check on sys.client.hello.
 */
void desk_sdk_bridge_set_auth_required(bool required);

/**
 * @brief Poll all active firmware communication channels (TCP SDK and UDP pairing).
 */
int desk_firmware_poll(int timeout_ms);

/**
 * @brief Start persistent background worker task driving desk_firmware_poll.
 */
int desk_firmware_start_background_task(void);

/**
 * @brief Stop persistent background worker task.
 */
void desk_firmware_stop_background_task(void);

/**
 * @brief Check if background worker task is running.
 */
bool desk_firmware_is_background_task_running(void);

/**
 * @brief Get high-water mark of background polling worker task (bytes remaining).
 */
uint32_t desk_firmware_background_task_stack_hwm(void);

/**
 * @brief Query number of currently tracked pending requests.
 */
int desk_sdk_bridge_get_pending_request_count(void);

/**
 * @brief Check if a request_id is currently tracked in the correlation table.
 */
bool desk_sdk_bridge_has_pending_request(const char *request_id);

/**
 * @brief Get the status of a tracked request_id.
 * @param request_id Request identifier to find.
 * @param out_status Buffer to receive status string.
 * @param max_len Maximum length of out_status buffer.
 * @return 0 on success, -1 if not found or invalid args.
 */
int desk_sdk_bridge_get_request_status(const char *request_id, char *out_status, size_t max_len);

/**
 * @brief Clear all entries in the request correlation table.
 */
int desk_sdk_bridge_clear_pending_requests(void);

/**
 * @brief Clean up expired requests in the correlation table exceeding timeout_ms.
 * @param timeout_ms Timeout threshold in ms (0 to use per-entry timeout).
 * @return Number of expired entries cleaned up.
 */
int desk_sdk_bridge_cleanup_expired_requests(uint32_t timeout_ms);

/**
 * @brief Get timestamp (seconds) of the last received heartbeat or valid command.
 */
uint32_t desk_sdk_bridge_get_last_heartbeat_time(void);

/**
 * @brief Check if client heartbeat is alive within timeout_sec.
 */
bool desk_sdk_bridge_is_heartbeat_alive(uint32_t timeout_sec);

#ifdef __cplusplus
}
#endif

#endif /* DESK_SDK_BRIDGE_H */
