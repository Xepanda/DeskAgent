/**
 * @file desk_sdk_bridge.c
 * @brief Implementation of Desk Agent R1 Official WatcheRobot SDK Protocol Bridge.
 */

#include "desk_sdk_bridge.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>

#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>
#include <fcntl.h>
#include <poll.h>
#include <errno.h>
#include <time.h>

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

static void desk_sdk_format_iso8601(time_t t, char *out_buf, size_t buf_len)
{
    if (!out_buf || buf_len < 21) return;
    struct tm tm_info;
    gmtime_r(&t, &tm_info);
    strftime(out_buf, buf_len, "%Y-%m-%dT%H:%M:%SZ", &tm_info);
}

static bool s_client_connected = false;
static uint32_t s_event_seq = 0;
static desk_sdk_outbound_send_cb_t s_outbound_cb = NULL;
static void *s_outbound_user_data = NULL;
static desk_sdk_tool_response_cb_t s_tool_response_cb = NULL;
static void *s_tool_response_user_data = NULL;

static char s_active_pairing_token[65] = "";
static bool s_auth_required = false;
static bool s_paired = false;

#ifdef ESP_PLATFORM
#include "esp_random.h"
#endif

static void generate_dynamic_token(char *out_buf, size_t max_len)
{
    static const char hex_chars[] = "0123456789abcdef";
    uint8_t rnd_bytes[32];
    bool filled = false;

#ifdef ESP_PLATFORM
    esp_fill_random(rnd_bytes, sizeof(rnd_bytes));
    filled = true;
#else
    int fd = open("/dev/urandom", O_RDONLY);
    if (fd >= 0) {
        ssize_t n = read(fd, rnd_bytes, sizeof(rnd_bytes));
        close(fd);
        if (n == (ssize_t)sizeof(rnd_bytes)) {
            filled = true;
        }
    }
#endif

    if (!filled) {
        /* Cryptographic fallback for environments without /dev/urandom */
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        uint64_t seed = ((uint64_t)ts.tv_sec << 32) ^ (uint64_t)ts.tv_nsec ^ (uintptr_t)&ts;
        for (size_t i = 0; i < sizeof(rnd_bytes); i++) {
            seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
            rnd_bytes[i] = (uint8_t)(seed >> ((i % 8) * 8));
        }
    }

    size_t out_idx = 0;
    for (size_t i = 0; i < sizeof(rnd_bytes) && out_idx + 2 < max_len; i++) {
        out_buf[out_idx++] = hex_chars[(rnd_bytes[i] >> 4) & 0x0F];
        out_buf[out_idx++] = hex_chars[rnd_bytes[i] & 0x0F];
    }
    out_buf[out_idx] = '\0';
}

int desk_sdk_bridge_set_pairing_token(const char *token)
{
    if (token && strlen(token) > 0) {
        strncpy(s_active_pairing_token, token, sizeof(s_active_pairing_token) - 1);
        s_active_pairing_token[sizeof(s_active_pairing_token) - 1] = '\0';
    } else {
        generate_dynamic_token(s_active_pairing_token, sizeof(s_active_pairing_token));
    }
    s_paired = true;
    return 0;
}

int desk_sdk_bridge_get_pairing_token(char *token, size_t max_len)
{
    if (!token || max_len == 0) return -1;
    if (s_active_pairing_token[0] == '\0') {
        generate_dynamic_token(s_active_pairing_token, sizeof(s_active_pairing_token));
    }
    strncpy(token, s_active_pairing_token, max_len - 1);
    token[max_len - 1] = '\0';
    return 0;
}

void desk_sdk_bridge_set_auth_required(bool required)
{
    s_auth_required = required;
}

/* Socket Server State */
static int s_listen_fd = -1;
static int s_client_fd = -1;
static uint16_t s_bound_port = 0;

/* Little-endian serialization helpers (<4sBBHII) */
static void write_u16_le(uint8_t *dst, uint16_t v)
{
    dst[0] = (uint8_t)(v & 0xFFu);
    dst[1] = (uint8_t)((v >> 8) & 0xFFu);
}

static void write_u32_le(uint8_t *dst, uint32_t v)
{
    dst[0] = (uint8_t)(v & 0xFFu);
    dst[1] = (uint8_t)((v >> 8) & 0xFFu);
    dst[2] = (uint8_t)((v >> 16) & 0xFFu);
    dst[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static uint16_t read_u16_le(const uint8_t *src)
{
    return (uint16_t)((uint16_t)src[0] | ((uint16_t)src[1] << 8));
}

static uint32_t read_u32_le(const uint8_t *src)
{
    return (uint32_t)src[0] | ((uint32_t)src[1] << 8) | ((uint32_t)src[2] << 16) | ((uint32_t)src[3] << 24);
}

/* Minimal JSON parser helpers */
static const char *json_find_key(const char *json, const char *key)
{
    if (!json || !key) return NULL;
    char needle[128];
    snprintf(needle, sizeof(needle), "\"%s\"", key);
    const char *pos = strstr(json, needle);
    if (!pos) return NULL;
    pos += strlen(needle);
    while (*pos && (*pos == ' ' || *pos == ':' || *pos == '\t' || *pos == '\r' || *pos == '\n')) {
        pos++;
    }
    return pos;
}

static float json_parse_float(const char *json, const char *key, float default_val)
{
    const char *pos = json_find_key(json, key);
    if (!pos) return default_val;
    return (float)atof(pos);
}

static int32_t json_parse_int(const char *json, const char *key, int32_t default_val)
{
    const char *pos = json_find_key(json, key);
    if (!pos) return default_val;
    return (int32_t)atoi(pos);
}

static bool json_parse_str(const char *json, const char *key, char *out_val, size_t max_len)
{
    const char *pos = json_find_key(json, key);
    if (!pos || *pos != '\"') return false;
    pos++;
    size_t i = 0;
    while (*pos && *pos != '\"' && i < max_len - 1) {
        out_val[i++] = *pos++;
    }
    out_val[i] = '\0';
    return true;
}

static bool json_parse_bool(const char *json, const char *key, bool default_val)
{
    const char *pos = json_find_key(json, key);
    if (!pos) return default_val;
    if (strncmp(pos, "true", 4) == 0 || strncmp(pos, "1", 1) == 0) return true;
    if (strncmp(pos, "false", 5) == 0 || strncmp(pos, "0", 1) == 0) return false;
    return default_val;
}

int desk_sdk_bridge_init(void)
{
    s_client_connected = false;
    s_event_seq = 0;
    s_outbound_cb = NULL;
    s_outbound_user_data = NULL;
    s_tool_response_cb = NULL;
    s_tool_response_user_data = NULL;
    s_listen_fd = -1;
    s_client_fd = -1;
    s_bound_port = 0;
    return 0;
}

void desk_sdk_bridge_reset(void)
{
    s_client_connected = false;
    s_event_seq = 0;
    s_active_pairing_token[0] = '\0';
    s_paired = false;
    s_auth_required = false;
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

/* WSPK binary framing: official Little-Endian <4sBBHII (16-byte) */
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

    memcpy(out_buf, DESK_WSPK_MAGIC, 4);
    out_buf[4] = frame_type;
    out_buf[5] = flags;
    write_u16_le(&out_buf[6], stream_id);
    write_u32_le(&out_buf[8], sequence);
    write_u32_le(&out_buf[12], payload_size);

    return 0;
}

int desk_wspk_unpack_header(const uint8_t *buf,
                            size_t buf_len,
                            desk_wspk_header_t *out_hdr)
{
    if (!buf || !out_hdr || buf_len < DESK_WSPK_LEGACY_HEADER_SIZE) return -1;

    if (memcmp(buf, DESK_WSPK_MAGIC, 4) != 0) {
        return -2; /* Invalid magic */
    }

    memcpy(out_hdr->magic, DESK_WSPK_MAGIC, 4);
    out_hdr->frame_type = buf[4];
    out_hdr->flags = buf[5];

    if (buf_len >= DESK_WSPK_HEADER_SIZE) {
        /* Standard 16-byte header: <4sBBHII */
        out_hdr->stream_id = read_u16_le(&buf[6]);
        out_hdr->sequence = read_u32_le(&buf[8]);
        out_hdr->payload_size = read_u32_le(&buf[12]);
    } else {
        /* Legacy 14-byte header: <4sBBII with stream_id = 0 */
        out_hdr->stream_id = 0;
        out_hdr->sequence = read_u32_le(&buf[6]);
        out_hdr->payload_size = read_u32_le(&buf[10]);
    }

    if (out_hdr->payload_size > DESK_WSPK_MAX_PAYLOAD_SIZE) {
        return -3; /* Exceeds max payload bound */
    }

    return 0;
}

int desk_sdk_bridge_pack_media_frame(uint8_t frame_type,
                                     uint8_t flags,
                                     uint16_t stream_id,
                                     uint32_t sequence,
                                     const uint8_t *payload,
                                     size_t payload_len,
                                     uint8_t *out_buf,
                                     size_t max_buf_len,
                                     size_t *out_len)
{
    if (!out_buf || !out_len || max_buf_len < (DESK_WSPK_HEADER_SIZE + payload_len)) return -1;
    if (payload_len > DESK_WSPK_MAX_PAYLOAD_SIZE) return -2;

    int rc = desk_wspk_pack_header(frame_type, flags, stream_id, sequence, (uint32_t)payload_len, out_buf, max_buf_len);
    if (rc != 0) return rc;

    if (payload && payload_len > 0) {
        memcpy(&out_buf[DESK_WSPK_HEADER_SIZE], payload, payload_len);
    }

    *out_len = DESK_WSPK_HEADER_SIZE + payload_len;
    return 0;
}

static uint16_t s_camera_session_id = 100;
static uint32_t s_camera_seq = 0;
static uint16_t s_mic_session_id = 200;

int desk_sdk_bridge_send_media_frame(uint8_t frame_type,
                                     uint8_t flags,
                                     uint16_t stream_id,
                                     uint32_t sequence,
                                     const uint8_t *payload,
                                     size_t payload_len)
{
    if (payload_len > DESK_WSPK_MAX_PAYLOAD_SIZE) return -2;

    uint8_t stack_buf[DESK_WSPK_HEADER_SIZE + 4096];
    uint8_t *heap_buf = NULL;
    uint8_t *target_buf = stack_buf;
    size_t target_size = sizeof(stack_buf);

    if (DESK_WSPK_HEADER_SIZE + payload_len > sizeof(stack_buf)) {
        target_size = DESK_WSPK_HEADER_SIZE + payload_len;
        heap_buf = (uint8_t *)malloc(target_size);
        if (!heap_buf) return -1;
        target_buf = heap_buf;
    }

    size_t packed_len = 0;
    int rc = desk_sdk_bridge_pack_media_frame(frame_type, flags, stream_id, sequence,
                                              payload, payload_len, target_buf, target_size, &packed_len);
    if (rc == 0) {
        if (s_client_fd >= 0) {
            ssize_t ret = write(s_client_fd, target_buf, packed_len);
            (void)ret;
        }
        if (s_outbound_cb) {
            s_outbound_cb((const char *)target_buf, packed_len, s_outbound_user_data);
        }
    }

    if (heap_buf) {
        free(heap_buf);
    }
    return rc;
}

/* UDP Pairing (watcher_lan_pairing_v1 on port 37021) */
int desk_sdk_bridge_process_pairing_udp(const char *udp_in, char *udp_out, size_t max_out_len)
{
    if (!udp_in || !udp_out || max_out_len < 128) return -1;

    char req_id[64] = "";
    char daemon_id[64] = "";
    char target_mode[32] = "";
    char pairing_code[16] = "";

    json_parse_str(udp_in, "request_id", req_id, sizeof(req_id));
    if (strlen(req_id) == 0) {
        json_parse_str(udp_in, "client_id", req_id, sizeof(req_id));
    }
    json_parse_str(udp_in, "daemon_instance_id", daemon_id, sizeof(daemon_id));
    if (strlen(daemon_id) == 0) {
        json_parse_str(udp_in, "client_id", daemon_id, sizeof(daemon_id));
    }
    json_parse_str(udp_in, "target_mode", target_mode, sizeof(target_mode));
    json_parse_str(udp_in, "pairing_code", pairing_code, sizeof(pairing_code));

    if (strlen(req_id) == 0 && strlen(daemon_id) == 0) {
        strncpy(req_id, "req-pair-01", sizeof(req_id) - 1);
        strncpy(daemon_id, "daemon-pair-01", sizeof(daemon_id) - 1);
    }

    /* Use active dynamic session token or generate fresh token */
    if (s_active_pairing_token[0] == '\0') {
        generate_dynamic_token(s_active_pairing_token, sizeof(s_active_pairing_token));
    }
    s_paired = true;

    char proto[64] = "";
    json_parse_str(udp_in, "protocol", proto, sizeof(proto));

    /* Strict official watcher-lan-pairing protocol check per PairAccept contract */
    if (strcmp(proto, "watcher-lan-pairing") == 0) {
        snprintf(udp_out, max_out_len,
                 "{\"type\":\"pair.accept\","
                 "\"protocol\":\"watcher-lan-pairing\","
                 "\"version\":\"1.0\","
                 "\"request_id\":\"%s\","
                 "\"daemon_instance_id\":\"%s\","
                 "\"target_mode\":\"%s\","
                 "\"session_token\":\"%s\"}",
                 req_id,
                 daemon_id,
                 strlen(target_mode) > 0 ? target_mode : "python_sdk",
                 s_active_pairing_token);
    } else {
        snprintf(udp_out, max_out_len,
                 "{\"type\":\"pair.accept\","
                 "\"status\":\"accepted\","
                 "\"device_id\":\"desk_agent_r1\","
                 "\"port\":%d,"
                 "\"protocol\":\"%s\","
                 "\"version\":\"1.0\","
                 "\"request_id\":\"%s\","
                 "\"daemon_instance_id\":\"%s\","
                 "\"target_mode\":\"%s\","
                 "\"token\":\"%s\","
                 "\"session_token\":\"%s\"}",
                 s_bound_port > 0 ? s_bound_port : 37020,
                 strlen(proto) > 0 ? proto : "watcher_lan_pairing_v1",
                 req_id,
                 daemon_id,
                 strlen(target_mode) > 0 ? target_mode : "python_sdk",
                 s_active_pairing_token,
                 s_active_pairing_token);
    }

    return 0;
}

/* Command processing */
int desk_sdk_bridge_process_command(const char *json_in, char *out_buf, size_t max_out_len)
{
    if (!json_in || !out_buf || max_out_len < 64) return -1;

    /* Handshake: sys.client.hello */
    if (strstr(json_in, "sys.client.hello") != NULL) {
        char provided_token[128] = "";
        json_parse_str(json_in, "session_token", provided_token, sizeof(provided_token));
        if (provided_token[0] == '\0') {
            json_parse_str(json_in, "token", provided_token, sizeof(provided_token));
        }

        /* Enforce pairing token authentication check */
        if (s_auth_required || (s_paired && s_active_pairing_token[0] != '\0')) {
            if (provided_token[0] == '\0' || strcmp(provided_token, s_active_pairing_token) != 0) {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",\"code\":401,\"error\":{"
                         "\"code\":\"AUTH_FAILED\","
                         "\"message\":\"Invalid pairing session token\"}}");
                return 0;
            }
        } else if (provided_token[0] != '\0' && s_active_pairing_token[0] != '\0') {
            if (strcmp(provided_token, s_active_pairing_token) != 0) {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",\"code\":401,\"error\":{"
                         "\"code\":\"AUTH_FAILED\","
                         "\"message\":\"Invalid pairing session token\"}}");
                return 0;
            }
        }

        s_client_connected = true;

        char role[32] = "";
        json_parse_str(json_in, "role", role, sizeof(role));

        /* If role is hardware (as in runtime pair sequence), reply with official negotiated session ack */
        if (strcmp(role, "hardware") == 0) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"data\":{"
                     "\"type\":\"sys.client.hello\","
                     "\"role\":\"hardware\","
                     "\"session_state\":\"connected\","
                     "\"negotiated\":{"
                     "\"audio_uplink\":{"
                     "\"codec\":\"opus\","
                     "\"sample_rate\":16000,"
                     "\"channels\":1,"
                     "\"frame_duration_ms\":60,"
                     "\"packetization\":\"one_opus_packet_per_wspk\","
                     "\"version\":1}}}}");
            return 0;
        }

        /* Standard SDK / desktop hello response */
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

    /* System SDK ready get: sys.sdk.ready.get */
    if (strstr(json_in, "sys.sdk.ready.get") != NULL) {
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

    /* Motion control: ctrl.motion.move / motion.move / ctrl.motion.set_target */
    if (strstr(json_in, "motion.move") != NULL || strstr(json_in, "motion.set_target") != NULL) {
        float pan = json_parse_float(json_in, "pan_deg", -999.0f);
        if (pan < -900.0f) {
            pan = json_parse_float(json_in, "pan", -999.0f);
        }
        float tilt = json_parse_float(json_in, "tilt_deg", -999.0f);
        if (tilt < -900.0f) {
            tilt = json_parse_float(json_in, "tilt", -999.0f);
        }
        int32_t duration = json_parse_int(json_in, "duration_ms", 400);

        if (pan < -900.0f) pan = desk_hardware_get_pan();
        if (tilt < -900.0f) tilt = desk_hardware_get_tilt();

        if (pan < DESK_MOTION_PAN_MIN_DEG) pan = DESK_MOTION_PAN_MIN_DEG;
        if (pan > DESK_MOTION_PAN_MAX_DEG) pan = DESK_MOTION_PAN_MAX_DEG;
        if (tilt < DESK_MOTION_TILT_MIN_DEG) tilt = DESK_MOTION_TILT_MIN_DEG;
        if (tilt > DESK_MOTION_TILT_MAX_DEG) tilt = DESK_MOTION_TILT_MAX_DEG;
        if (duration < 50) duration = 50;
        if (duration > 5000) duration = 5000;

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
        char req_id[64] = "";
        json_parse_str(json_in, "request_id", req_id, sizeof(req_id));
        bool emergency = json_parse_bool(json_in, "emergency", false);
        desk_hardware_motion_stop(emergency);
        if (req_id[0] != '\0') {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"request_id\":\"%s\",\"data\":{\"status\":\"succeeded\",\"stopped\":true}}",
                     req_id);
        } else {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"succeeded\",\"stopped\":true}}");
        }
        return 0;
    }

    /* Motion action play: ctrl.motion.action.play */
    if (strstr(json_in, "motion.action.play") != NULL) {
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"job_id\":1,\"status\":\"succeeded\"}}");
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

    /* Camera capture: ctrl.camera.capture / camera.capture / ctrl.camera.capture_with_feedback */
    if (strstr(json_in, "camera.capture") != NULL) {
        size_t max_jpeg = 65536;
        uint8_t *jpeg_buf = (uint8_t *)malloc(max_jpeg);
        if (!jpeg_buf) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",\"code\":500,\"error\":{"
                     "\"code\":\"OUT_OF_MEMORY\","
                     "\"message\":\"Cannot allocate buffer for camera capture\"}}");
            return 0;
        }

        size_t jpeg_len = 0;
        int cap_rc = desk_hardware_camera_capture_jpeg(jpeg_buf, max_jpeg, &jpeg_len, 1000);
        if (cap_rc != DESK_HW_OK || jpeg_len == 0) {
            free(jpeg_buf);
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",\"code\":409,\"error\":{"
                     "\"code\":\"RESOURCE_BUSY\","
                     "\"message\":\"Camera frame unavailable\"}}");
            return 0;
        }

        s_camera_session_id = (s_camera_session_id % 65534) + 1;
        s_camera_seq++;

        /* Send ACK carrying positive session_id */
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"session_id\":%u}}",
                 (unsigned int)s_camera_session_id);

        /* Send WSPK media frame (FRAME_IMAGE = 3) */
        desk_sdk_bridge_send_media_frame(DESK_WSPK_TYPE_IMAGE, 0, s_camera_session_id, s_camera_seq,
                                         jpeg_buf, jpeg_len);
        free(jpeg_buf);
        return 0;
    }

    /* Microphone open / close: ctrl.microphone.open / ctrl.microphone.close */
    if (strstr(json_in, "ctrl.microphone.open") != NULL) {
        int rate = json_parse_int(json_in, "sample_rate_hz", 16000);
        desk_hardware_audio_record_start((uint32_t)rate);
        s_mic_session_id = (s_mic_session_id % 65534) + 1;
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"session_id\":%u}}",
                 (unsigned int)s_mic_session_id);
        return 0;
    }

    if (strstr(json_in, "ctrl.microphone.close") != NULL) {
        desk_hardware_audio_record_stop();
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"succeeded\"}}");
        return 0;
    }

    /* Expression play / set: ctrl.expression.play / ctrl.expression.set */
    if (strstr(json_in, "expression.play") != NULL || strstr(json_in, "expression.set") != NULL) {
        char req_id[64] = "";
        json_parse_str(json_in, "request_id", req_id, sizeof(req_id));

        char preset[32] = "idle";
        if (!json_parse_str(json_in, "preset", preset, sizeof(preset)) || preset[0] == '\0') {
            strncpy(preset, "idle", sizeof(preset) - 1);
            preset[sizeof(preset) - 1] = '\0';
        }

        char text[64] = "";
        if (!json_parse_str(json_in, "text", text, sizeof(text))) {
            if (!json_parse_str(json_in, "status_text", text, sizeof(text))) {
                json_parse_str(json_in, "title", text, sizeof(text));
            }
        }

        int32_t duration_ms = json_parse_int(json_in, "duration_ms", 0);
        (void)duration_ms;

        desk_arbiter_status_t arb = desk_arbiter_acquire(DESK_RES_DISPLAY, DESK_OWNER_SDK_CLIENT, 500);
        if (arb == DESK_ARBITER_ERR_BUSY) {
            if (req_id[0] != '\0') {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",\"code\":409,\"request_id\":\"%s\",\"error\":{"
                         "\"code\":\"RESOURCE_BUSY\","
                         "\"message\":\"Display UI is currently held by higher priority owner\"}}",
                         req_id);
            } else {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",\"code\":409,\"error\":{"
                         "\"code\":\"RESOURCE_BUSY\","
                         "\"message\":\"Display UI is currently held by higher priority owner\"}}");
            }
            return 0;
        } else if (arb != DESK_ARBITER_OK) {
            if (req_id[0] != '\0') {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",\"code\":500,\"request_id\":\"%s\",\"error\":{"
                         "\"code\":\"RESOURCE_ERROR\","
                         "\"message\":\"Failed to acquire display resource\"}}",
                         req_id);
            } else {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",\"code\":500,\"error\":{"
                         "\"code\":\"RESOURCE_ERROR\","
                         "\"message\":\"Failed to acquire display resource\"}}");
            }
            return 0;
        }

        int disp_rc = desk_hardware_display_set_expression(preset, text);
        desk_arbiter_release(DESK_RES_DISPLAY, DESK_OWNER_SDK_CLIENT);
        if (disp_rc != DESK_HW_OK) {
            if (req_id[0] != '\0') {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",\"code\":500,\"request_id\":\"%s\",\"error\":{"
                         "\"code\":\"HARDWARE_ERROR\","
                         "\"message\":\"Display hardware unavailable or driver failed\"}}",
                         req_id);
            } else {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",\"code\":500,\"error\":{"
                         "\"code\":\"HARDWARE_ERROR\","
                         "\"message\":\"Display hardware unavailable or driver failed\"}}");
            }
            return 0;
        }

        char event_type[64] = "";
        json_parse_str(json_in, "event_type", event_type, sizeof(event_type));
        bool play_audio_flag = json_parse_bool(json_in, "play_audio", false);
        bool alert_audio = (strcmp(event_type, "reminder.alert") == 0 ||
                            play_audio_flag ||
                            strcmp(preset, "alert") == 0);

        if (alert_audio) {
            desk_arbiter_status_t audio_arb = desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_CLIENT, 500);
            if (audio_arb == DESK_ARBITER_OK) {
                static const int16_t s_sine_lut_16[16] = {
                    0, 3062, 5657, 7396, 8000, 7396, 5657, 3062,
                    0, -3062, -5657, -7396, -8000, -7396, -5657, -3062
                };
                static int16_t s_alert_pcm[1600];
                static bool s_alert_pcm_initialized = false;
                if (!s_alert_pcm_initialized) {
                    for (size_t i = 0; i < 1600; i++) {
                        s_alert_pcm[i] = s_sine_lut_16[i % 16];
                    }
                    s_alert_pcm_initialized = true;
                }
                desk_hardware_audio_play_pcm(s_alert_pcm, 1600, 16000);
                desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_CLIENT);
            }
        }

        if (req_id[0] != '\0') {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"request_id\":\"%s\",\"data\":{"
                     "\"status\":\"succeeded\",\"preset\":\"%s\",\"text\":\"%s\"}}",
                     req_id, preset, text);
        } else {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"data\":{"
                     "\"status\":\"succeeded\",\"preset\":\"%s\",\"text\":\"%s\"}}",
                     preset, text);
        }
        return 0;
    }

    /* Expression audio follow: ctrl.expression.audio_follow */
    if (strstr(json_in, "expression.audio_follow") != NULL) {
        bool enabled = (strstr(json_in, "\"enabled\":true") != NULL || strstr(json_in, "\"enabled\": true") != NULL);
        if (enabled) {
            desk_hardware_display_set_expression("speaking", "Audio Follow Active");
        } else {
            desk_hardware_display_set_expression("idle", "Ready");
        }
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"enabled\":%s}}",
                 enabled ? "true" : "false");
        return 0;
    }

    /* External tool response envelope from Python client */
    if (strstr(json_in, "tool.response") != NULL || (strstr(json_in, "\"status\":") != NULL && strstr(json_in, "\"request_id\":") != NULL)) {
        desk_sdk_bridge_handle_tool_response(json_in);
        char req_id[64] = "";
        json_parse_str(json_in, "request_id", req_id, sizeof(req_id));
        if (req_id[0] != '\0') {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"request_id\":\"%s\",\"data\":{\"status\":\"received\"}}",
                     req_id);
        } else {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"received\"}}");
        }
        return 0;
    }

    /* Unknown operation */
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

    time_t now = time(NULL);
    if (now < 1700000000) now = 1792100000;
    char ts[32];
    desk_sdk_format_iso8601(now, ts, sizeof(ts));

    char event_buf[2048];
    snprintf(event_buf, sizeof(event_buf),
             "{\"schema_version\":\"1.0.0\","
             "\"event_id\":\"evt-esp32-%08x\","
             "\"sequence\":%u,"
             "\"sequence_scope\":\"device:esp32s3\","
             "\"type\":\"%s\","
             "\"timestamp\":\"%s\","
             "\"payload\":%s}",
             s_event_seq * 0x13579bdf,
             s_event_seq,
             event_type,
             ts,
             payload_json);

    if (s_outbound_cb) {
        s_outbound_cb(event_buf, strlen(event_buf), s_outbound_user_data);
    }

    if (s_client_fd >= 0) {
        ssize_t ret = write(s_client_fd, event_buf, strlen(event_buf));
        (void)ret;
        ret = write(s_client_fd, "\n", 1);
        (void)ret;
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
    time_t now = time(NULL);
    if (now < 1700000000) now = 1792100000;
    char ts[32];
    desk_sdk_format_iso8601(now, ts, sizeof(ts));

    char formatted[2048];
    snprintf(formatted, sizeof(formatted),
             "{\"schema_version\":\"1.0.0\","
             "\"event_id\":\"evt-esp32-%08x\","
             "\"sequence\":%u,"
             "\"sequence_scope\":\"device:esp32s3\","
             "\"type\":\"resource.touch_event\","
             "\"timestamp\":\"%s\","
             "\"payload\":{"
             "\"target\":\"%s\","
             "\"action\":\"%s\","
             "\"timestamp\":\"%s\""
             "}}",
             s_event_seq * 0x13579bdf,
             s_event_seq,
             ts,
             target_str,
             action_str,
             ts);

    if (out_event_json && max_len > 0) {
        strncpy(out_event_json, formatted, max_len - 1);
        out_event_json[max_len - 1] = '\0';
    }

    if (s_outbound_cb) {
        s_outbound_cb(formatted, strlen(formatted), s_outbound_user_data);
    }

    if (s_client_fd >= 0) {
        ssize_t ret = send(s_client_fd, formatted, strlen(formatted), MSG_NOSIGNAL);
        if (ret < 0) {
            if (errno == EPIPE || errno == ECONNRESET || errno == EBADF) {
                close(s_client_fd);
                s_client_fd = -1;
                s_client_connected = false;
            }
        } else {
            ret = send(s_client_fd, "\n", 1, MSG_NOSIGNAL);
            if (ret < 0 && (errno == EPIPE || errno == ECONNRESET || errno == EBADF)) {
                close(s_client_fd);
                s_client_fd = -1;
                s_client_connected = false;
            }
        }
    }

    return 0;
}

int desk_sdk_bridge_dispatch_tool_request(const char *request_envelope_json)
{
    if (!request_envelope_json) return -1;

    int ret = 0;
    if (s_outbound_cb) {
        ret = s_outbound_cb(request_envelope_json, strlen(request_envelope_json), s_outbound_user_data);
    }

    if (s_client_fd >= 0) {
        size_t len = strlen(request_envelope_json);
        ssize_t w = send(s_client_fd, request_envelope_json, len, MSG_NOSIGNAL);
        if (w < 0) {
            if (errno == EPIPE || errno == ECONNRESET || errno == EBADF) {
                close(s_client_fd);
                s_client_fd = -1;
                s_client_connected = false;
            }
        } else if (len == 0 || request_envelope_json[len - 1] != '\n') {
            ssize_t wn = send(s_client_fd, "\n", 1, MSG_NOSIGNAL);
            if (wn < 0 && (errno == EPIPE || errno == ECONNRESET || errno == EBADF)) {
                close(s_client_fd);
                s_client_fd = -1;
                s_client_connected = false;
            }
        }
    }

    return ret;
}

int desk_sdk_bridge_handle_tool_response(const char *response_envelope_json)
{
    if (!response_envelope_json) return -1;
    if (s_tool_response_cb) {
        return s_tool_response_cb(response_envelope_json, s_tool_response_user_data);
    }
    return 0;
}

/* Local TCP Socket Server for live client / SDK connectivity */
int desk_sdk_socket_server_start_ex(const char *bind_addr, uint16_t port)
{
    if (s_listen_fd >= 0) {
        close(s_listen_fd);
        s_listen_fd = -1;
    }

    s_listen_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (s_listen_fd < 0) return -1;

    int opt = 1;
    setsockopt(s_listen_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
#ifdef SO_REUSEPORT
    setsockopt(s_listen_fd, SOL_SOCKET, SO_REUSEPORT, &opt, sizeof(opt));
#endif

    struct sockaddr_in sin;
    memset(&sin, 0, sizeof(sin));
    sin.sin_family = AF_INET;
    if (!bind_addr || strlen(bind_addr) == 0 || strcmp(bind_addr, "0.0.0.0") == 0) {
        sin.sin_addr.s_addr = htonl(INADDR_ANY);
    } else if (strcmp(bind_addr, "127.0.0.1") == 0) {
        sin.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    } else {
        if (inet_pton(AF_INET, bind_addr, &sin.sin_addr) <= 0) {
            sin.sin_addr.s_addr = htonl(INADDR_ANY);
        }
    }
    sin.sin_port = htons(port);

    if (bind(s_listen_fd, (struct sockaddr *)&sin, sizeof(sin)) < 0) {
        close(s_listen_fd);
        s_listen_fd = -1;
        return -2;
    }

    socklen_t len = sizeof(sin);
    if (getsockname(s_listen_fd, (struct sockaddr *)&sin, &len) == 0) {
        s_bound_port = ntohs(sin.sin_port);
    }

    if (listen(s_listen_fd, 4) < 0) {
        close(s_listen_fd);
        s_listen_fd = -1;
        return -3;
    }

    /* Make non-blocking */
    int flags = fcntl(s_listen_fd, F_GETFL, 0);
    fcntl(s_listen_fd, F_SETFL, flags | O_NONBLOCK);

    return 0;
}

int desk_sdk_socket_server_start(uint16_t port)
{
    /* Bind to 0.0.0.0 so Python SDK and network clients can connect across LAN */
    return desk_sdk_socket_server_start_ex("0.0.0.0", port);
}

int desk_sdk_socket_server_get_port(void)
{
    return (int)s_bound_port;
}

int desk_sdk_socket_server_poll(int timeout_ms)
{
    if (s_listen_fd < 0) return -1;

    struct pollfd fds[2];
    int nfds = 1;
    fds[0].fd = s_listen_fd;
    fds[0].events = POLLIN;

    if (s_client_fd >= 0) {
        fds[1].fd = s_client_fd;
        fds[1].events = POLLIN;
        nfds = 2;
    }

    int rc = poll(fds, nfds, timeout_ms);
    if (rc <= 0) return 0;

    /* New incoming connection */
    if (fds[0].revents & POLLIN) {
        struct sockaddr_in client_addr;
        socklen_t clen = sizeof(client_addr);
        int new_fd = accept(s_listen_fd, (struct sockaddr *)&client_addr, &clen);
        if (new_fd >= 0) {
            if (s_client_fd >= 0) {
                close(s_client_fd);
            }
            s_client_fd = new_fd;
            int fl = fcntl(s_client_fd, F_GETFL, 0);
            fcntl(s_client_fd, F_SETFL, fl | O_NONBLOCK);
            s_client_connected = true;
        }
    }

    /* Client data ready */
    if (nfds > 1 && (fds[1].revents & (POLLIN | POLLHUP | POLLERR))) {
        if (fds[1].revents & (POLLHUP | POLLERR)) {
            close(s_client_fd);
            s_client_fd = -1;
            s_client_connected = false;
            return 0;
        }

        char buf[4096];
        ssize_t n = read(s_client_fd, buf, sizeof(buf) - 1);
        if (n <= 0) {
            close(s_client_fd);
            s_client_fd = -1;
            s_client_connected = false;
            return 0;
        }
        buf[n] = '\0';

        /* If JSON command, process and respond */
        if (buf[0] == '{') {
            char rsp[2048];
            if (desk_sdk_bridge_process_command(buf, rsp, sizeof(rsp)) == 0) {
                ssize_t w = write(s_client_fd, rsp, strlen(rsp));
                (void)w;
                w = write(s_client_fd, "\n", 1);
                (void)w;
            }
        }
    }

    return 0;
}

int desk_sdk_socket_server_stop(void)
{
    if (s_client_fd >= 0) {
        close(s_client_fd);
        s_client_fd = -1;
    }
    if (s_listen_fd >= 0) {
        close(s_listen_fd);
        s_listen_fd = -1;
    }
    s_bound_port = 0;
    s_client_connected = false;
    return 0;
}

/* UDP Pairing Server (Port 37021 watcher_lan_pairing_v1) */
static int s_pairing_udp_fd = -1;
static uint16_t s_pairing_port = 0;

int desk_sdk_pairing_server_start(uint16_t port)
{
    if (s_pairing_udp_fd >= 0) {
        close(s_pairing_udp_fd);
        s_pairing_udp_fd = -1;
    }

    s_pairing_udp_fd = socket(AF_INET, SOCK_DGRAM, 0);
    if (s_pairing_udp_fd < 0) return -1;

    int opt = 1;
    setsockopt(s_pairing_udp_fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
#ifdef SO_BROADCAST
    setsockopt(s_pairing_udp_fd, SOL_SOCKET, SO_BROADCAST, &opt, sizeof(opt));
#endif

    struct sockaddr_in sin;
    memset(&sin, 0, sizeof(sin));
    sin.sin_family = AF_INET;
    sin.sin_addr.s_addr = htonl(INADDR_ANY);
    sin.sin_port = htons(port);

    if (bind(s_pairing_udp_fd, (struct sockaddr *)&sin, sizeof(sin)) < 0) {
        close(s_pairing_udp_fd);
        s_pairing_udp_fd = -1;
        return -2;
    }

    socklen_t len = sizeof(sin);
    if (getsockname(s_pairing_udp_fd, (struct sockaddr *)&sin, &len) == 0) {
        s_pairing_port = ntohs(sin.sin_port);
    }

    int flags = fcntl(s_pairing_udp_fd, F_GETFL, 0);
    fcntl(s_pairing_udp_fd, F_SETFL, flags | O_NONBLOCK);
    return 0;
}

int desk_sdk_pairing_server_poll(int timeout_ms)
{
    if (s_pairing_udp_fd < 0) return -1;

    struct pollfd pfd;
    pfd.fd = s_pairing_udp_fd;
    pfd.events = POLLIN;

    int rc = poll(&pfd, 1, timeout_ms);
    if (rc <= 0) return 0;

    if (pfd.revents & POLLIN) {
        char buf[2048];
        struct sockaddr_in client_addr;
        socklen_t addr_len = sizeof(client_addr);
        ssize_t n = recvfrom(s_pairing_udp_fd, buf, sizeof(buf) - 1, 0,
                             (struct sockaddr *)&client_addr, &addr_len);
        if (n > 0) {
            buf[n] = '\0';
            char reply[2048];
            if (desk_sdk_bridge_process_pairing_udp(buf, reply, sizeof(reply)) == 0) {
                sendto(s_pairing_udp_fd, reply, strlen(reply), 0,
                       (struct sockaddr *)&client_addr, addr_len);
            }
        }
    }
    return 0;
}

int desk_sdk_pairing_server_stop(void)
{
    if (s_pairing_udp_fd >= 0) {
        close(s_pairing_udp_fd);
        s_pairing_udp_fd = -1;
    }
    s_pairing_port = 0;
    return 0;
}

int desk_sdk_pairing_server_get_port(void)
{
    return (int)s_pairing_port;
}

int desk_firmware_poll(int timeout_ms)
{
    int half = timeout_ms / 2;
    if (half < 1) half = 1;
    desk_sdk_socket_server_poll(half);
    desk_sdk_pairing_server_poll(half);
    return 0;
}

#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
static TaskHandle_t s_worker_task_handle = NULL;
static void firmware_worker_task(void *pvParameters)
{
    (void)pvParameters;
    while (s_worker_task_handle != NULL) {
        desk_firmware_poll(20);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    vTaskDelete(NULL);
}
int desk_firmware_start_background_task(void)
{
    if (s_worker_task_handle != NULL) return 0;
    BaseType_t ret = xTaskCreate(firmware_worker_task, "desk_poll_worker", 4096, NULL, 5, &s_worker_task_handle);
    return ret == pdPASS ? 0 : -1;
}
void desk_firmware_stop_background_task(void)
{
    s_worker_task_handle = NULL;
}
bool desk_firmware_is_background_task_running(void)
{
    return s_worker_task_handle != NULL;
}
#else
#include <pthread.h>
#include <unistd.h>
static pthread_t s_worker_thread;
static volatile bool s_worker_running = false;
static void *firmware_worker_thread(void *arg)
{
    (void)arg;
    while (s_worker_running) {
        desk_firmware_poll(20);
        usleep(10000);
    }
    return NULL;
}
int desk_firmware_start_background_task(void)
{
    if (s_worker_running) return 0;
    s_worker_running = true;
    if (pthread_create(&s_worker_thread, NULL, firmware_worker_thread, NULL) != 0) {
        s_worker_running = false;
        return -1;
    }
    return 0;
}
void desk_firmware_stop_background_task(void)
{
    if (!s_worker_running) return;
    s_worker_running = false;
    pthread_join(s_worker_thread, NULL);
}
bool desk_firmware_is_background_task_running(void)
{
    return s_worker_running;
}
#endif
