/**
 * @file desk_sdk_bridge.c
 * @brief Implementation of WatcheRobot Protocol Compatibility Bridge.
 *
 * Implements:
 * - WSPK binary framing (16-byte header, network byte order, overflow defense)
 * - sys.client.hello negotiation and capability publication
 * - Resource-arbitrated motion and camera controls
 * - Standardized contract EventEnvelope formatting for hardware inputs
 * - Bidirectional TClaw tool request and response routing
 */

#include "desk_sdk_bridge.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>
#include <time.h>

static uint32_t s_event_seq = 0;
static bool s_client_connected = false;
static desk_sdk_outbound_send_cb_t s_outbound_cb = NULL;
static void *s_outbound_user_data = NULL;
static desk_sdk_tool_response_cb_t s_tool_response_cb = NULL;
static void *s_tool_response_user_data = NULL;

/* Endianness helpers */
static inline uint16_t be16_from_host(uint16_t v)
{
    uint8_t b[2];
    b[0] = (uint8_t)(v >> 8);
    b[1] = (uint8_t)(v & 0xff);
    uint16_t r;
    memcpy(&r, b, 2);
    return r;
}

static inline uint16_t host_from_be16(uint16_t v)
{
    const uint8_t *b = (const uint8_t *)&v;
    return (uint16_t)(((uint16_t)b[0] << 8) | (uint16_t)b[1]);
}

static inline uint32_t be32_from_host(uint32_t v)
{
    uint8_t b[4];
    b[0] = (uint8_t)(v >> 24);
    b[1] = (uint8_t)((v >> 16) & 0xff);
    b[2] = (uint8_t)((v >> 8) & 0xff);
    b[3] = (uint8_t)(v & 0xff);
    uint32_t r;
    memcpy(&r, b, 4);
    return r;
}

static inline uint32_t host_from_be32(uint32_t v)
{
    const uint8_t *b = (const uint8_t *)&v;
    return ((uint32_t)b[0] << 24) |
           ((uint32_t)b[1] << 16) |
           ((uint32_t)b[2] << 8) |
           (uint32_t)b[3];
}

/* Minimal lightweight JSON value parsers */
static const char *json_find_key(const char *json, const char *key)
{
    if (!json || !key) return NULL;
    char pattern[64];
    snprintf(pattern, sizeof(pattern), "\"%s\"", key);
    const char *p = strstr(json, pattern);
    if (!p) return NULL;
    p += strlen(pattern);
    while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
    if (*p == ':') {
        p++;
        while (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n') p++;
        return p;
    }
    return NULL;
}

static float json_parse_float(const char *json, const char *key, float default_val)
{
    const char *p = json_find_key(json, key);
    if (!p) return default_val;
    char *endptr = NULL;
    float val = strtof(p, &endptr);
    if (endptr == p) return default_val;
    return val;
}

static int32_t json_parse_int(const char *json, const char *key, int32_t default_val)
{
    const char *p = json_find_key(json, key);
    if (!p) return default_val;
    char *endptr = NULL;
    long val = strtol(p, &endptr, 10);
    if (endptr == p) return default_val;
    return (int32_t)val;
}

static int json_parse_str(const char *json, const char *key, char *out_str, size_t max_len)
{
    const char *p = json_find_key(json, key);
    if (!p || *p != '"') return -1;
    p++; /* skip opening quote */
    size_t idx = 0;
    while (*p && *p != '"' && idx < max_len - 1) {
        out_str[idx++] = *p++;
    }
    out_str[idx] = '\0';
    return 0;
}

int desk_sdk_bridge_init(void)
{
    s_event_seq = 0;
    s_client_connected = false;
    s_outbound_cb = NULL;
    s_outbound_user_data = NULL;
    s_tool_response_cb = NULL;
    s_tool_response_user_data = NULL;
    return 0;
}

void desk_sdk_bridge_reset(void)
{
    s_event_seq = 0;
    s_client_connected = false;
}

bool desk_sdk_bridge_is_connected(void)
{
    return s_client_connected;
}

int desk_sdk_bridge_register_outbound(desk_sdk_outbound_send_cb_t cb, void *user_data)
{
    s_outbound_cb = cb;
    s_outbound_user_data = user_data;
    return 0;
}

int desk_sdk_bridge_register_tool_response_cb(desk_sdk_tool_response_cb_t cb, void *user_data)
{
    s_tool_response_cb = cb;
    s_tool_response_user_data = user_data;
    return 0;
}

/* WSPK binary framing */
int desk_wspk_pack_header(uint8_t frame_type,
                          uint8_t flags,
                          uint16_t stream_id,
                          uint32_t sequence,
                          uint32_t payload_size,
                          uint8_t *out_buf,
                          size_t buf_len)
{
    if (!out_buf || buf_len < DESK_WSPK_HEADER_SIZE) return -1;
    if (payload_size > DESK_WSPK_MAX_PAYLOAD_SIZE) return -2;

    desk_wspk_header_t hdr;
    memcpy(hdr.magic, DESK_WSPK_MAGIC, 4);
    hdr.frame_type = frame_type;
    hdr.flags = flags;
    hdr.stream_id = be16_from_host(stream_id);
    hdr.sequence = be32_from_host(sequence);
    hdr.payload_size = be32_from_host(payload_size);

    memcpy(out_buf, &hdr, sizeof(hdr));
    return 0;
}

int desk_wspk_unpack_header(const uint8_t *buf,
                            size_t buf_len,
                            desk_wspk_header_t *out_hdr)
{
    if (!buf || !out_hdr || buf_len < DESK_WSPK_HEADER_SIZE) return -1;

    desk_wspk_header_t raw;
    memcpy(&raw, buf, sizeof(raw));

    if (memcmp(raw.magic, DESK_WSPK_MAGIC, 4) != 0) {
        return -2; /* Invalid magic */
    }

    uint32_t payload_size = host_from_be32(raw.payload_size);
    if (payload_size > DESK_WSPK_MAX_PAYLOAD_SIZE) {
        return -3; /* Exceeds max payload bound */
    }

    memcpy(out_hdr->magic, raw.magic, 4);
    out_hdr->frame_type = raw.frame_type;
    out_hdr->flags = raw.flags;
    out_hdr->stream_id = host_from_be16(raw.stream_id);
    out_hdr->sequence = host_from_be32(raw.sequence);
    out_hdr->payload_size = payload_size;

    return 0;
}

/* Command processing */
int desk_sdk_bridge_process_command(const char *json_in, char *out_buf, size_t max_out_len)
{
    if (!json_in || !out_buf || max_out_len < 64) return -1;

    /* Handshake: sys.client.hello */
    if (strstr(json_in, "sys.client.hello") != NULL) {
        s_client_connected = true;
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{"
                 "\"role\":\"device\","
                 "\"protocol_version\":\"1.0\","
                 "\"capabilities\":[\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]}}",
                 DESK_SDK_CAP_INPUTS,
                 DESK_SDK_CAP_MOTION,
                 DESK_SDK_CAP_CAMERA,
                 DESK_SDK_CAP_AUDIO_STREAM,
                 DESK_SDK_CAP_EXPRESSION);
        return 0;
    }

    /* Heartbeat: sys.ping */
    if (strstr(json_in, "sys.ping") != NULL) {
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.pong\",\"code\":0,\"data\":{}}");
        return 0;
    }

    /* Motion control: ctrl.motion.move / motion.move */
    if (strstr(json_in, "motion.move") != NULL) {
        float pan = json_parse_float(json_in, "pan_deg", 90.0f);
        float tilt = json_parse_float(json_in, "tilt_deg", 120.0f);
        int32_t duration = json_parse_int(json_in, "duration_ms", 500);

        /* Hardware boundary clamping */
        if (pan < DESK_MOTION_PAN_MIN_DEG) pan = DESK_MOTION_PAN_MIN_DEG;
        if (pan > DESK_MOTION_PAN_MAX_DEG) pan = DESK_MOTION_PAN_MAX_DEG;
        if (tilt < DESK_MOTION_TILT_MIN_DEG) tilt = DESK_MOTION_TILT_MIN_DEG;
        if (tilt > DESK_MOTION_TILT_MAX_DEG) tilt = DESK_MOTION_TILT_MAX_DEG;
        if (duration < 100) duration = 100;
        if (duration > 5000) duration = 5000;

        /* Enforce resource arbitration */
        desk_arbiter_status_t arb = desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE, 50);
        if (arb == DESK_ARBITER_ERR_BUSY) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",\"code\":409,\"error\":{"
                     "\"code\":\"RESOURCE_BUSY\","
                     "\"message\":\"Motion gimbal is currently held by higher priority owner or locked by camera lease\"}}");
            return 0;
        } else if (arb != DESK_ARBITER_OK) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",\"code\":500,\"error\":{"
                     "\"code\":\"RESOURCE_ERROR\","
                     "\"message\":\"Failed to acquire motion resource\"}}");
            return 0;
        }

        desk_hardware_motion_move_to(pan, tilt, (uint32_t)duration);
        desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE);

        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"succeeded\","
                 "\"pan_deg\":%.1f,\"tilt_deg\":%.1f,\"duration_ms\":%d}}",
                 pan, tilt, duration);
        return 0;
    }

    /* Motion stop: ctrl.motion.stop / motion.stop */
    if (strstr(json_in, "motion.stop") != NULL) {
        desk_hardware_motion_stop(false);
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"succeeded\"}}");
        return 0;
    }

    /* Camera acquire lease: ctrl.camera.acquire / camera.acquire */
    if (strstr(json_in, "camera.acquire") != NULL) {
        char client_id[32] = "sdk-client";
        json_parse_str(json_in, "client_id", client_id, sizeof(client_id));
        int32_t lease_ms = json_parse_int(json_in, "lease_duration_ms", 10000);
        if (lease_ms < 1000) lease_ms = 1000;
        if (lease_ms > 60000) lease_ms = 60000;

        desk_arbiter_status_t arb = desk_arbiter_camera_acquire_lease(client_id, (uint32_t)lease_ms);
        if (arb == DESK_ARBITER_OK) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"succeeded\","
                     "\"client_id\":\"%s\",\"lease_duration_ms\":%d}}",
                     client_id, lease_ms);
        } else if (arb == DESK_ARBITER_ERR_BUSY) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",\"code\":409,\"error\":{"
                     "\"code\":\"RESOURCE_BUSY\","
                     "\"message\":\"Camera lease currently held by another client\"}}");
        } else {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",\"code\":400,\"error\":{"
                     "\"code\":\"INVALID_ARGUMENTS\","
                     "\"message\":\"Invalid camera lease parameters\"}}");
        }
        return 0;
    }

    /* Camera release lease: ctrl.camera.release / camera.release */
    if (strstr(json_in, "camera.release") != NULL) {
        char client_id[32] = "sdk-client";
        json_parse_str(json_in, "client_id", client_id, sizeof(client_id));
        desk_arbiter_camera_release_lease(client_id);
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"succeeded\"}}");
        return 0;
    }

    /* External tool response envelope from Python client */
    if (strstr(json_in, "\"status\":") != NULL && strstr(json_in, "\"request_id\":") != NULL) {
        desk_sdk_bridge_handle_tool_response(json_in);
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"received\"}}");
        return 0;
    }

    /* Unknown operation: return sys.nack with UNKNOWN_OPERATION error code */
    snprintf(out_buf, max_out_len,
             "{\"type\":\"sys.nack\",\"code\":404,\"error\":{"
             "\"code\":\"UNKNOWN_OPERATION\","
             "\"message\":\"Command not recognized by Desk Agent SDK bridge\"}}");
    return 0;
}

int desk_sdk_bridge_send_event(const char *event_type, const char *payload_json)
{
    if (!event_type || !payload_json) return -1;
    s_event_seq++;

    char event_buf[2048];
    snprintf(event_buf, sizeof(event_buf),
             "{\"schema_version\":\"1.0.0\","
             "\"event_id\":\"evt-esp32-%08x\","
             "\"sequence\":%u,"
             "\"sequence_scope\":\"device:esp32s3\","
             "\"type\":\"%s\","
             "\"timestamp\":\"2026-10-13T10:00:00Z\","
             "\"payload\":%s}",
             s_event_seq * 0x13579bdf,
             s_event_seq,
             event_type,
             payload_json);

    if (s_outbound_cb) {
        s_outbound_cb(event_buf, strlen(event_buf), s_outbound_user_data);
    }
    return 0;
}

int desk_sdk_bridge_send_touch_event(desk_touch_target_t target,
                                     desk_touch_action_t action,
                                     char *out_event_json,
                                     size_t max_len)
{
    const char *target_str = "backtouch";
    switch (target) {
        case DESK_TOUCH_TARGET_BACKTOUCH:   target_str = "backtouch"; break;
        case DESK_TOUCH_TARGET_SCREENTOUCH: target_str = "screentouch"; break;
        case DESK_TOUCH_TARGET_ROLLER:      target_str = "roller"; break;
        default: break;
    }

    const char *action_str = "short_press";
    switch (action) {
        case DESK_TOUCH_ACTION_SHORT_PRESS: action_str = "short_press"; break;
        case DESK_TOUCH_ACTION_LONG_PRESS:  action_str = "long_press"; break;
        case DESK_TOUCH_ACTION_SCROLL_UP:   action_str = "scroll_up"; break;
        case DESK_TOUCH_ACTION_SCROLL_DOWN: action_str = "scroll_down"; break;
        default: break;
    }

    s_event_seq++;
    char formatted[2048];
    snprintf(formatted, sizeof(formatted),
             "{\"schema_version\":\"1.0.0\","
             "\"event_id\":\"evt-esp32-%08x\","
             "\"sequence\":%u,"
             "\"sequence_scope\":\"device:esp32s3\","
             "\"type\":\"resource.touch_event\","
             "\"timestamp\":\"2026-10-13T10:00:00Z\","
             "\"payload\":{"
             "\"target\":\"%s\","
             "\"action\":\"%s\","
             "\"timestamp\":\"2026-10-13T10:00:00Z\""
             "}}",
             s_event_seq * 0x13579bdf,
             s_event_seq,
             target_str,
             action_str);

    if (out_event_json && max_len > 0) {
        strncpy(out_event_json, formatted, max_len - 1);
        out_event_json[max_len - 1] = '\0';
    }

    if (s_outbound_cb) {
        s_outbound_cb(formatted, strlen(formatted), s_outbound_user_data);
    }

    return 0;
}

int desk_sdk_bridge_dispatch_tool_request(const char *request_envelope_json)
{
    if (!request_envelope_json) return -1;
    if (s_outbound_cb) {
        return s_outbound_cb(request_envelope_json, strlen(request_envelope_json), s_outbound_user_data);
    }
    return 0;
}

int desk_sdk_bridge_handle_tool_response(const char *response_envelope_json)
{
    if (!response_envelope_json) return -1;
    if (s_tool_response_cb) {
        return s_tool_response_cb(response_envelope_json, s_tool_response_user_data);
    }
    return 0;
}
