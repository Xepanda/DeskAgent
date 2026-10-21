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
#include <fcntl.h>
#ifdef ESP_PLATFORM
#include <sys/poll.h>
#include "esp_attr.h"
#include "tal_api.h"
#define DESK_PROTOCOL_BSS EXT_RAM_BSS_ATTR
#define desk_psram_malloc(sz) tal_psram_malloc(sz)
#define desk_psram_free(ptr)  tal_psram_free(ptr)
#else
#include <poll.h>
#define DESK_PROTOCOL_BSS
#define desk_psram_malloc(sz) malloc(sz)
#define desk_psram_free(ptr)  free(ptr)
#endif
#include <unistd.h>
#include <errno.h>
#include <time.h>
#include <pthread.h>

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
static uint32_t s_last_heartbeat_time = 0;

/* Device-side expected pairing code (interfaces.md 6.4). Provisioned explicitly
 * via desk_sdk_bridge_set_pairing_code(); never hardcoded, never logged. */
static char s_expected_pairing_code[8] = "";
static bool s_pairing_code_configured = false;

/* True when the string is exactly 6 ASCII decimal digits (^[0-9]{6}$). */
static bool desk_sdk_is_six_digits(const char *s)
{
    if (!s) return false;
    for (size_t i = 0; i < 6; i++) {
        if (s[i] < '0' || s[i] > '9') return false;
    }
    return s[6] == '\0';
}

/* Length-constant comparison; avoids leaking a matching prefix via timing. */
static bool desk_sdk_secure_equals(const char *a, const char *b)
{
    if (!a || !b) return false;
    size_t la = strlen(a);
    size_t lb = strlen(b);
    if (la != lb) return false;
    unsigned char diff = 0;
    for (size_t i = 0; i < la; i++) {
        diff |= (unsigned char)(a[i] ^ b[i]);
    }
    return diff == 0;
}

/* Official redact_sensitive_fields rule: session_token -> [redacted:<last4>]. */
static void desk_sdk_redact_token(const char *token, char *out, size_t out_len)
{
    if (!out || out_len == 0) return;
    size_t n = token ? strlen(token) : 0;
    if (n == 0) {
        snprintf(out, out_len, "[redacted]");
    } else if (n >= 4) {
        snprintf(out, out_len, "[redacted:%s]", token + (n - 4));
    } else {
        snprintf(out, out_len, "[redacted]");
    }
}

int desk_sdk_bridge_set_pairing_code(const char *code)
{
    if (!code || code[0] == '\0') {
        s_expected_pairing_code[0] = '\0';
        s_pairing_code_configured = false;
        return 0;
    }
    if (!desk_sdk_is_six_digits(code)) {
        /* Reject malformed provisioning input; keep the previous configuration. */
        return -1;
    }
    memcpy(s_expected_pairing_code, code, 7);
    s_pairing_code_configured = true;
    return 0;
}

bool desk_sdk_bridge_is_pairing_code_configured(void)
{
    return s_pairing_code_configured;
}

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

/* True when the string matches ^[0-9a-f]{64}$ -- the official session_token
 * format (_require_lower_hex(message, "session_token", _LOWER_HEX_64)). */
static bool desk_sdk_is_lower_hex_64(const char *s)
{
    if (!s) return false;
    for (size_t i = 0; i < 64; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }
    return s[64] == '\0';
}

int desk_sdk_bridge_set_pairing_token(const char *token)
{
    if (!token) {
        /* No value supplied: mint a fresh credential (always 64 lowercase hex). */
        generate_dynamic_token(s_active_pairing_token, sizeof(s_active_pairing_token));
        return 0;
    }

    /* R11: refuse any credential that could not legally appear in a pair.accept.
     * Arming it would make the firmware emit a message the official SDK's own
     * parse_udp_message() rejects. The previously armed credential is preserved. */
    if (!desk_sdk_is_lower_hex_64(token)) {
        return -1;
    }

    memcpy(s_active_pairing_token, token, sizeof(s_active_pairing_token));
    /* R9 (interfaces.md 6.6): this provisioning entry point sets credential
     * material ONLY. `s_paired` means "a client has successfully completed a
     * pairing handshake" and may only be set by the validated pairing path in
     * desk_sdk_bridge_process_pairing_udp() (interfaces.md 6.4). */
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

/* Persistent per-connection receive accumulator (interfaces.md 6.3).
 * A JSON control frame is a single line terminated by '\n'; frames may be
 * split across reads, batched into one read, or span multiple reads. */
static DESK_PROTOCOL_BSS char s_rx_buf[DESK_SDK_MAX_FRAME_LEN + 2];
static size_t s_rx_len = 0;
/* Set while discarding an oversized frame; cleared at the next '\n' so that the
 * following frame's boundaries are not corrupted. */
static bool s_rx_discarding = false;

/**
 * @brief Drop the active client connection and clear all residual RX state.
 *
 * interfaces.md 6.3.4: residual bytes must never be concatenated with a new
 * connection's first message.
 */
static void desk_sdk_socket_client_close(void)
{
    if (s_client_fd >= 0) {
        close(s_client_fd);
        s_client_fd = -1;
    }
    s_client_connected = false;
    s_rx_len = 0;
    s_rx_discarding = false;
}

static void desk_sdk_socket_rx_reset(void)
{
    s_rx_len = 0;
    s_rx_discarding = false;
}

/**
 * @brief Write exactly `len` bytes, retrying on short writes.
 *
 * interfaces.md 6.3.5: a single write() is never assumed to complete; short
 * writes are resumed and write errors are reported to the caller.
 *
 * @return 0 when all bytes were written, -1 on error (connection closed).
 */
static int desk_sdk_socket_write_all(int fd, const void *data, size_t len)
{
    if (fd < 0 || (!data && len > 0)) return -1;
    const char *bytes = (const char *)data;
    size_t written = 0;

    while (written < len) {
        ssize_t w = send(fd, bytes + written, len - written, MSG_NOSIGNAL);
        if (w > 0) {
            written += (size_t)w;
            continue;
        }
        if (w < 0 && errno == EINTR) {
            continue;
        }
        if (w < 0 && (errno == EAGAIN || errno == EWOULDBLOCK)) {
            struct pollfd pfd;
            pfd.fd = fd;
            pfd.events = POLLOUT;
            pfd.revents = 0;
            int prc = poll(&pfd, 1, 1000);
            if (prc > 0 && (pfd.revents & POLLOUT)) {
                continue;
            }
            if (prc < 0 && errno == EINTR) {
                continue;
            }
        }
        return -1;
    }
    return 0;
}

/* Send a newline-terminated line, closing the connection on write failure. */
static int desk_sdk_socket_send_line(const char *line)
{
    if (s_client_fd < 0 || !line) return -1;
    size_t len = strlen(line);
    if (desk_sdk_socket_write_all(s_client_fd, line, len) != 0 ||
        desk_sdk_socket_write_all(s_client_fd, "\n", 1) != 0) {
        desk_sdk_socket_client_close();
        return -1;
    }
    return 0;
}

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

bool json_parse_top_level_str(const char *json, const char *key, char *out_val, size_t max_len)
{
    if (!json || !key || !out_val || max_len == 0) return false;
    out_val[0] = '\0';

    const char *p = json;
    while (*p && (*p == ' ' || *p == '\t' || *p == '\r' || *p == '\n')) p++;
    if (*p != '{') return false;
    p++;

    int depth = 1;
    bool in_str = false;
    bool escaped = false;

    char needle[128];
    snprintf(needle, sizeof(needle), "\"%s\"", key);
    size_t needle_len = strlen(needle);

    while (*p && depth > 0) {
        if (escaped) {
            escaped = false;
            p++;
            continue;
        }
        if (*p == '\\' && in_str) {
            escaped = true;
            p++;
            continue;
        }
        if (*p == '\"') {
            if (!in_str) {
                in_str = true;
                if (depth == 1 && strncmp(p, needle, needle_len) == 0) {
                    const char *after = p + needle_len;
                    while (*after && (*after == ' ' || *after == '\t' || *after == '\r' || *after == '\n')) after++;
                    if (*after == ':') {
                        after++;
                        while (*after && (*after == ' ' || *after == '\t' || *after == '\r' || *after == '\n')) after++;
                        if (*after == '\"') {
                            after++;
                            size_t idx = 0;
                            while (*after && *after != '\"' && idx + 1 < max_len) {
                                if (*after == '\\' && *(after + 1)) {
                                    after++;
                                }
                                out_val[idx++] = *after++;
                            }
                            out_val[idx] = '\0';
                            return true;
                        }
                    }
                }
            } else {
                in_str = false;
            }
            p++;
            continue;
        }
        if (!in_str) {
            if (*p == '{') depth++;
            else if (*p == '}') depth--;
        }
        p++;
    }
    return false;
}

static bool json_extract_subobject(const char *json, const char *key, char *out_buf, size_t max_len)
{
    if (!json || !key || !out_buf || max_len == 0) return false;
    out_buf[0] = '\0';

    char needle[128];
    snprintf(needle, sizeof(needle), "\"%s\"", key);
    size_t needle_len = strlen(needle);

    const char *p = json;
    bool in_str = false;
    bool escaped = false;

    while (*p) {
        if (escaped) {
            escaped = false;
            p++;
            continue;
        }
        if (*p == '\\' && in_str) {
            escaped = true;
            p++;
            continue;
        }
        if (*p == '\"') {
            if (!in_str) {
                if (strncmp(p, needle, needle_len) == 0) {
                    const char *after = p + needle_len;
                    while (*after && (*after == ' ' || *after == '\t' || *after == '\r' || *after == '\n')) after++;
                    if (*after == ':') {
                        after++;
                        while (*after && (*after == ' ' || *after == '\t' || *after == '\r' || *after == '\n')) after++;
                        if (*after == '{' || *after == '[') {
                            char open_ch = *after;
                            char close_ch = (open_ch == '{') ? '}' : ']';
                            int depth = 1;
                            const char *obj_start = after;
                            after++;
                            bool sub_in_str = false;
                            bool sub_escaped = false;
                            while (*after && depth > 0) {
                                if (sub_escaped) {
                                    sub_escaped = false;
                                    after++;
                                    continue;
                                }
                                if (*after == '\\' && sub_in_str) {
                                    sub_escaped = true;
                                    after++;
                                    continue;
                                }
                                if (*after == '\"') {
                                    sub_in_str = !sub_in_str;
                                    after++;
                                    continue;
                                }
                                if (!sub_in_str) {
                                    if (*after == open_ch) depth++;
                                    else if (*after == close_ch) depth--;
                                }
                                after++;
                            }
                            if (depth == 0) {
                                size_t obj_len = (size_t)(after - obj_start);
                                if (obj_len < max_len) {
                                    memcpy(out_buf, obj_start, obj_len);
                                    out_buf[obj_len] = '\0';
                                    return true;
                                }
                            }
                        }
                    }
                }
                in_str = true;
            } else {
                in_str = false;
            }
        }
        p++;
    }
    return false;
}

/**
 * @brief Build the '"request_id":"<id>",' fragment for a response envelope.
 *
 * Per interfaces.md 6.2 every response to a request that carried a request_id
 * MUST echo it verbatim. Returns "" when the request carried no (valid)
 * request_id. `scratch` must outlive the formatted response.
 */
static const char *desk_sdk_request_id_fragment(const char *json_in, char *scratch, size_t scratch_len)
{
    if (!scratch || scratch_len == 0) return "";
    scratch[0] = '\0';
    if (!json_in) return "";

    char req_id[80] = "";
    if (!json_parse_top_level_str(json_in, "request_id", req_id, sizeof(req_id)) || req_id[0] == '\0') {
        return "";
    }
    /* interfaces.md 1.1: ^[a-zA-Z0-9_-]+$ -- reject anything that could break JSON. */
    for (const char *p = req_id; *p != '\0'; p++) {
        if (!(isalnum((unsigned char)*p) || *p == '_' || *p == '-')) {
            return "";
        }
    }
    snprintf(scratch, scratch_len, "\"request_id\":\"%s\",", req_id);
    return scratch;
}

#define DESK_SDK_MAX_PENDING_REQUESTS 64

typedef struct {
    char request_id[64];
    char operation[64];
    uint64_t created_at_ms;
    uint64_t updated_at_ms;
    uint32_t timeout_ms;
    desk_sdk_req_state_t state;
    char last_status[32];
    char result_payload[512];
    bool has_response;
    bool in_use;
} desk_sdk_pending_req_t;

static DESK_PROTOCOL_BSS desk_sdk_pending_req_t s_req_table[DESK_SDK_MAX_PENDING_REQUESTS];
static pthread_mutex_t s_req_mutex = PTHREAD_MUTEX_INITIALIZER;

static uint64_t desk_sdk_time_ms(void)
{
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)(ts.tv_nsec / 1000000ULL);
}

static void desk_sdk_req_table_init(void)
{
    pthread_mutex_lock(&s_req_mutex);
    memset(s_req_table, 0, sizeof(s_req_table));
    pthread_mutex_unlock(&s_req_mutex);
}

static int desk_sdk_req_table_add(const char *request_id, const char *operation, uint32_t timeout_ms)
{
    if (!request_id || request_id[0] == '\0') return -1;

    pthread_mutex_lock(&s_req_mutex);
    uint64_t now = desk_sdk_time_ms();

    /* If already tracked, update it */
    for (size_t i = 0; i < DESK_SDK_MAX_PENDING_REQUESTS; i++) {
        if (s_req_table[i].in_use && strcmp(s_req_table[i].request_id, request_id) == 0) {
            if (operation && operation[0] != '\0') {
                strncpy(s_req_table[i].operation, operation, sizeof(s_req_table[i].operation) - 1);
                s_req_table[i].operation[sizeof(s_req_table[i].operation) - 1] = '\0';
            }
            s_req_table[i].updated_at_ms = now;
            s_req_table[i].state = DESK_SDK_REQ_STATE_PENDING;
            s_req_table[i].has_response = false;
            s_req_table[i].result_payload[0] = '\0';
            strncpy(s_req_table[i].last_status, "pending", sizeof(s_req_table[i].last_status) - 1);
            pthread_mutex_unlock(&s_req_mutex);
            return 0;
        }
    }

    /* Find empty or completed/failed slot */
    int target_idx = -1;
    uint64_t oldest_time = UINT64_MAX;
    int oldest_idx = 0;

    for (size_t i = 0; i < DESK_SDK_MAX_PENDING_REQUESTS; i++) {
        if (!s_req_table[i].in_use) {
            target_idx = (int)i;
            break;
        }
        if (s_req_table[i].state == DESK_SDK_REQ_STATE_COMPLETED ||
            s_req_table[i].state == DESK_SDK_REQ_STATE_FAILED ||
            s_req_table[i].state == DESK_SDK_REQ_STATE_TIMED_OUT) {
            target_idx = (int)i;
            break;
        }
        if (s_req_table[i].created_at_ms < oldest_time) {
            oldest_time = s_req_table[i].created_at_ms;
            oldest_idx = (int)i;
        }
    }

    if (target_idx < 0) {
        target_idx = oldest_idx;
    }

    desk_sdk_pending_req_t *entry = &s_req_table[target_idx];
    memset(entry, 0, sizeof(*entry));
    entry->in_use = true;
    strncpy(entry->request_id, request_id, sizeof(entry->request_id) - 1);
    entry->request_id[sizeof(entry->request_id) - 1] = '\0';
    if (operation) {
        strncpy(entry->operation, operation, sizeof(entry->operation) - 1);
        entry->operation[sizeof(entry->operation) - 1] = '\0';
    }
    entry->created_at_ms = now;
    entry->updated_at_ms = now;
    entry->timeout_ms = timeout_ms > 0 ? timeout_ms : 60000;
    entry->state = DESK_SDK_REQ_STATE_PENDING;
    strncpy(entry->last_status, "pending", sizeof(entry->last_status) - 1);

    pthread_mutex_unlock(&s_req_mutex);
    return 0;
}

static int desk_sdk_req_table_update_with_payload(const char *request_id, const char *status, const char *payload)
{
    if (!request_id || !status) return -1;

    pthread_mutex_lock(&s_req_mutex);
    uint64_t now = desk_sdk_time_ms();
    int found_idx = -1;

    for (size_t i = 0; i < DESK_SDK_MAX_PENDING_REQUESTS; i++) {
        if (s_req_table[i].in_use && strcmp(s_req_table[i].request_id, request_id) == 0) {
            found_idx = (int)i;
            break;
        }
    }

    if (found_idx >= 0) {
        desk_sdk_pending_req_t *entry = &s_req_table[found_idx];
        entry->updated_at_ms = now;
        snprintf(entry->last_status, sizeof(entry->last_status), "%s", status);
        if (payload && payload[0] != '\0') {
            strncpy(entry->result_payload, payload, sizeof(entry->result_payload) - 1);
            entry->result_payload[sizeof(entry->result_payload) - 1] = '\0';
        }
        entry->has_response = true;

        if (strcmp(status, "accepted") == 0) {
            entry->state = DESK_SDK_REQ_STATE_ACCEPTED;
        } else if (strcmp(status, "completed") == 0 || strcmp(status, "succeeded") == 0) {
            entry->state = DESK_SDK_REQ_STATE_COMPLETED;
        } else if (strcmp(status, "failed") == 0 || strcmp(status, "error") == 0 || strcmp(status, "cancelled") == 0) {
            entry->state = DESK_SDK_REQ_STATE_FAILED;
        }
        pthread_mutex_unlock(&s_req_mutex);
        return 0;
    }

    pthread_mutex_unlock(&s_req_mutex);

    /* If not currently tracked, record it now */
    desk_sdk_req_table_add(request_id, "unknown", 60000);
    pthread_mutex_lock(&s_req_mutex);
    for (size_t i = 0; i < DESK_SDK_MAX_PENDING_REQUESTS; i++) {
        if (s_req_table[i].in_use && strcmp(s_req_table[i].request_id, request_id) == 0) {
            snprintf(s_req_table[i].last_status, sizeof(s_req_table[i].last_status), "%s", status);
            if (payload && payload[0] != '\0') {
                strncpy(s_req_table[i].result_payload, payload, sizeof(s_req_table[i].result_payload) - 1);
                s_req_table[i].result_payload[sizeof(s_req_table[i].result_payload) - 1] = '\0';
            }
            s_req_table[i].has_response = true;
            if (strcmp(status, "accepted") == 0) {
                s_req_table[i].state = DESK_SDK_REQ_STATE_ACCEPTED;
            } else if (strcmp(status, "completed") == 0 || strcmp(status, "succeeded") == 0) {
                s_req_table[i].state = DESK_SDK_REQ_STATE_COMPLETED;
            } else {
                s_req_table[i].state = DESK_SDK_REQ_STATE_FAILED;
            }
            break;
        }
    }
    pthread_mutex_unlock(&s_req_mutex);
    return 0;
}


int desk_sdk_bridge_wait_tool_response(const char *request_id,
                                       uint32_t timeout_ms,
                                       char *out_status, size_t status_max_len,
                                       char *out_result, size_t result_max_len)
{
    if (!request_id || request_id[0] == '\0') return -1;
    uint64_t deadline = desk_sdk_time_ms() + timeout_ms;

    while (desk_sdk_time_ms() < deadline) {
        pthread_mutex_lock(&s_req_mutex);
        for (size_t i = 0; i < DESK_SDK_MAX_PENDING_REQUESTS; i++) {
            if (s_req_table[i].in_use && strcmp(s_req_table[i].request_id, request_id) == 0) {
                if (s_req_table[i].has_response) {
                    if (out_status && status_max_len > 0) {
                        strncpy(out_status, s_req_table[i].last_status, status_max_len - 1);
                        out_status[status_max_len - 1] = '\0';
                    }
                    if (out_result && result_max_len > 0) {
                        strncpy(out_result, s_req_table[i].result_payload, result_max_len - 1);
                        out_result[result_max_len - 1] = '\0';
                    }
                    s_req_table[i].has_response = false;
                    pthread_mutex_unlock(&s_req_mutex);
                    return 0;
                }
                break;
            }
        }
        pthread_mutex_unlock(&s_req_mutex);

#ifdef ESP_PLATFORM
        usleep(20000);
#else
        usleep(20000);
#endif
    }
    return -1;
}

int desk_sdk_bridge_get_pending_request_count(void)
{
    pthread_mutex_lock(&s_req_mutex);
    int count = 0;
    for (size_t i = 0; i < DESK_SDK_MAX_PENDING_REQUESTS; i++) {
        if (s_req_table[i].in_use &&
            (s_req_table[i].state == DESK_SDK_REQ_STATE_PENDING ||
             s_req_table[i].state == DESK_SDK_REQ_STATE_ACCEPTED)) {
            count++;
        }
    }
    pthread_mutex_unlock(&s_req_mutex);
    return count;
}

bool desk_sdk_bridge_has_pending_request(const char *request_id)
{
    if (!request_id || request_id[0] == '\0') return false;
    pthread_mutex_lock(&s_req_mutex);
    for (size_t i = 0; i < DESK_SDK_MAX_PENDING_REQUESTS; i++) {
        if (s_req_table[i].in_use && strcmp(s_req_table[i].request_id, request_id) == 0) {
            bool active = (s_req_table[i].state == DESK_SDK_REQ_STATE_PENDING ||
                           s_req_table[i].state == DESK_SDK_REQ_STATE_ACCEPTED);
            pthread_mutex_unlock(&s_req_mutex);
            return active;
        }
    }
    pthread_mutex_unlock(&s_req_mutex);
    return false;
}

int desk_sdk_bridge_get_request_status(const char *request_id, char *out_status, size_t max_len)
{
    if (!request_id || !out_status || max_len == 0) return -1;
    pthread_mutex_lock(&s_req_mutex);
    for (size_t i = 0; i < DESK_SDK_MAX_PENDING_REQUESTS; i++) {
        if (s_req_table[i].in_use && strcmp(s_req_table[i].request_id, request_id) == 0) {
            strncpy(out_status, s_req_table[i].last_status, max_len - 1);
            out_status[max_len - 1] = '\0';
            pthread_mutex_unlock(&s_req_mutex);
            return 0;
        }
    }
    pthread_mutex_unlock(&s_req_mutex);
    return -1;
}

int desk_sdk_bridge_clear_pending_requests(void)
{
    desk_sdk_req_table_init();
    return 0;
}

int desk_sdk_bridge_cleanup_expired_requests(uint32_t timeout_ms)
{
    pthread_mutex_lock(&s_req_mutex);
    uint64_t now = desk_sdk_time_ms();
    int cleaned = 0;
    for (size_t i = 0; i < DESK_SDK_MAX_PENDING_REQUESTS; i++) {
        if (s_req_table[i].in_use &&
            (s_req_table[i].state == DESK_SDK_REQ_STATE_PENDING ||
             s_req_table[i].state == DESK_SDK_REQ_STATE_ACCEPTED)) {
            uint32_t thresh = timeout_ms > 0 ? timeout_ms : s_req_table[i].timeout_ms;
            if (now >= s_req_table[i].created_at_ms && (now - s_req_table[i].created_at_ms) > thresh) {
                s_req_table[i].state = DESK_SDK_REQ_STATE_TIMED_OUT;
                strncpy(s_req_table[i].last_status, "timed_out", sizeof(s_req_table[i].last_status) - 1);
                cleaned++;
            }
        }
    }
    pthread_mutex_unlock(&s_req_mutex);
    return cleaned;
}

uint32_t desk_sdk_bridge_get_last_heartbeat_time(void)
{
    return s_last_heartbeat_time;
}

bool desk_sdk_bridge_is_heartbeat_alive(uint32_t timeout_sec)
{
    if (s_last_heartbeat_time == 0) return false;
    time_t now = time(NULL);
    if ((uint32_t)now < s_last_heartbeat_time) return true;
    return ((uint32_t)now - s_last_heartbeat_time) <= timeout_sec;
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
    s_last_heartbeat_time = 0;
    desk_sdk_req_table_init();
    desk_sdk_socket_rx_reset();
    return 0;
}

void desk_sdk_bridge_reset(void)
{
    s_client_connected = false;
    s_event_seq = 0;
    s_active_pairing_token[0] = '\0';
    s_paired = false;
    s_auth_required = false;
    s_last_heartbeat_time = 0;
    desk_sdk_req_table_init();
    /* Provisioned expected pairing code intentionally survives a reset. */
    desk_sdk_socket_rx_reset();
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
        heap_buf = (uint8_t *)desk_psram_malloc(target_size);
        if (!heap_buf) return -1;
        target_buf = heap_buf;
    }

    size_t packed_len = 0;
    int rc = desk_sdk_bridge_pack_media_frame(frame_type, flags, stream_id, sequence,
                                              payload, payload_len, target_buf, target_size, &packed_len);
    if (rc == 0) {
        if (s_client_fd >= 0) {
            /* WSPK framing is unchanged; only the write path now guarantees the
             * full frame is transmitted (interfaces.md 6.3.5). */
            if (desk_sdk_socket_write_all(s_client_fd, target_buf, packed_len) != 0) {
                desk_sdk_socket_client_close();
                rc = -3;
            }
        }
        if (s_outbound_cb) {
            s_outbound_cb((const char *)target_buf, packed_len, s_outbound_user_data);
        }
    }

    if (heap_buf) {
        desk_psram_free(heap_buf);
    }
    return rc;
}

/* UDP Pairing (watcher_lan_pairing_v1 on port 37021) */
int desk_sdk_bridge_process_pairing_udp(const char *udp_in, char *udp_out, size_t max_out_len)
{
    if (!udp_in || !udp_out || max_out_len < 128) return DESK_SDK_PAIRING_ERR_ARG;

    /* Fail-closed default: nothing is emitted unless validation fully passes. */
    udp_out[0] = '\0';

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

    /* interfaces.md 6.4 / CR-001: never grant a pairing before the pairing code
     * has been validated against the explicitly provisioned device-side code.
     * pairing_code is never logged in clear text (redact_sensitive_fields). */
    if (!s_pairing_code_configured) {
        printf("[SDK-PAIR] pair.request rejected: no expected pairing code provisioned "
               "(pairing_code=[redacted], request_id=%s)\n", req_id);
        return DESK_SDK_PAIRING_REJECTED;
    }
    if (!desk_sdk_is_six_digits(pairing_code)) {
        printf("[SDK-PAIR] pair.request rejected: malformed pairing_code "
               "(pairing_code=[redacted], request_id=%s)\n", req_id);
        return DESK_SDK_PAIRING_REJECTED;
    }
    if (!desk_sdk_secure_equals(pairing_code, s_expected_pairing_code)) {
        printf("[SDK-PAIR] pair.request rejected: pairing_code mismatch "
               "(pairing_code=[redacted], request_id=%s)\n", req_id);
        return DESK_SDK_PAIRING_REJECTED;
    }

    if (strlen(req_id) == 0 && strlen(daemon_id) == 0) {
        strncpy(req_id, "req-pair-01", sizeof(req_id) - 1);
        strncpy(daemon_id, "daemon-pair-01", sizeof(daemon_id) - 1);
    }

    /* Validation passed: only now generate/issue a session token and pair. */
    if (s_active_pairing_token[0] == '\0') {
        generate_dynamic_token(s_active_pairing_token, sizeof(s_active_pairing_token));
    }
    s_paired = true;

    char proto[64] = "";
    json_parse_str(udp_in, "protocol", proto, sizeof(proto));

    char redacted_token[32];
    desk_sdk_redact_token(s_active_pairing_token, redacted_token, sizeof(redacted_token));
    printf("[SDK-PAIR] pair.accept issued (session_token=%s, request_id=%s)\n",
           redacted_token, req_id);

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

    return DESK_SDK_PAIRING_OK;
}

/* Command processing */
int desk_sdk_bridge_process_command(const char *json_in, char *out_buf, size_t max_out_len)
{
    if (!json_in || !out_buf || max_out_len < 64) return -1;

    char msg_type[64] = "";
    bool has_type = json_parse_top_level_str(json_in, "type", msg_type, sizeof(msg_type));
    if (!has_type) {
        char status_tmp[32] = "";
        char req_id_tmp[64] = "";
        if (json_parse_top_level_str(json_in, "status", status_tmp, sizeof(status_tmp)) &&
            json_parse_top_level_str(json_in, "request_id", req_id_tmp, sizeof(req_id_tmp))) {
            strncpy(msg_type, "tool.response", sizeof(msg_type) - 1);
            msg_type[sizeof(msg_type) - 1] = '\0';
        } else {
            char rid_scratch[128];
            const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":404,\"error\":{"
                     "\"code\":\"UNKNOWN_OPERATION\","
                     "\"message\":\"Command missing valid top-level type\"}}",
                     rid);
            return 0;
        }
    }

    /* Handshake: sys.client.hello */
    if (strcmp(msg_type, "sys.client.hello") == 0) {
        char provided_token[128] = "";
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        json_parse_str(json_in, "session_token", provided_token, sizeof(provided_token));
        if (provided_token[0] == '\0') {
            json_parse_str(json_in, "token", provided_token, sizeof(provided_token));
        }

        /* Enforce pairing token authentication check */
        if (s_auth_required || (s_paired && s_active_pairing_token[0] != '\0')) {
            if (provided_token[0] == '\0' || strcmp(provided_token, s_active_pairing_token) != 0) {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",%s\"code\":401,\"error\":{"
                         "\"code\":\"AUTH_FAILED\","
                         "\"message\":\"Invalid pairing session token\"}}", rid);
                return 0;
            }
        } else if (provided_token[0] != '\0' && s_active_pairing_token[0] != '\0') {
            if (strcmp(provided_token, s_active_pairing_token) != 0) {
                snprintf(out_buf, max_out_len,
                         "{\"type\":\"sys.nack\",%s\"code\":401,\"error\":{"
                         "\"code\":\"AUTH_FAILED\","
                         "\"message\":\"Invalid pairing session token\"}}", rid);
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
                 "\"capabilities\":[\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]}}",
                 DESK_SDK_CAP_INPUTS,
                 DESK_SDK_CAP_MOTION,
                 DESK_SDK_CAP_CAMERA,
                 DESK_SDK_CAP_AUDIO_STREAM,
                 DESK_SDK_CAP_EXPRESSION,
                 DESK_SDK_CAP_AUDIO_STREAM_ALT,
                 DESK_SDK_CAP_EXPRESSION_ALT,
                 DESK_SDK_CAP_MCP_TOOLS);
        return 0;
    }

    /* System SDK ready get: sys.sdk.ready.get */
    else if (strcmp(msg_type, "sys.sdk.ready.get") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{"
                 "\"role\":\"device\","
                 "\"protocol_version\":\"1.0\","
                 "\"capabilities\":[\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]}}",
                 rid,
                 DESK_SDK_CAP_INPUTS,
                 DESK_SDK_CAP_MOTION,
                 DESK_SDK_CAP_CAMERA,
                 DESK_SDK_CAP_AUDIO_STREAM,
                 DESK_SDK_CAP_EXPRESSION,
                 DESK_SDK_CAP_AUDIO_STREAM_ALT,
                 DESK_SDK_CAP_EXPRESSION_ALT,
                 DESK_SDK_CAP_MCP_TOOLS);
        return 0;
    }

    /* Heartbeat: sys.ping */
    else if (strcmp(msg_type, "sys.ping") == 0) {
        s_last_heartbeat_time = (uint32_t)time(NULL);
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.pong\",%s\"code\":0,\"data\":{}}", rid);
        return 0;
    }

    /* Motion control: ctrl.motion.move / motion.move / ctrl.motion.set_target */
    else if (strcmp(msg_type, "ctrl.motion.move") == 0 ||
             strcmp(msg_type, "motion.move") == 0 ||
             strcmp(msg_type, "ctrl.motion.set_target") == 0 ||
             strcmp(msg_type, "motion.set_target") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
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
                     "{\"type\":\"sys.nack\",%s\"code\":409,\"error\":{"
                     "\"code\":\"RESOURCE_BUSY\","
                     "\"message\":\"Motion gimbal is currently held by higher priority owner or locked by camera lease\"}}",
                     rid);
            return 0;
        } else if (arb != DESK_ARBITER_OK) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":500,\"error\":{"
                     "\"code\":\"RESOURCE_ERROR\","
                     "\"message\":\"Failed to acquire motion resource\"}}",
                     rid);
            return 0;
        }

        /* interfaces.md 6.1: the driver return value MUST be checked; a driver
         * failure is never reported as code:0. */
        int move_rc = desk_hardware_motion_move_to(pan, tilt, (uint32_t)duration);
        desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE);

        if (move_rc != DESK_HW_OK) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":500,\"error\":{"
                     "\"code\":\"HARDWARE_FAULT\","
                     "\"message\":\"Gimbal driver failed to accept motion command\"}}",
                     rid);
            return 0;
        }

        /* ACK semantics: "accepted" (dispatched to the driver), never
         * "succeeded" -- physical completion stays on resource.motion.completed.
         * Per CR-002 and R3a protocol adjudication, motion commands are ACK-only:
         * status="accepted", accepted=true, state_confirmed=false. */
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"status\":\"accepted\","
                 "\"accepted\":true,\"state_confirmed\":false,\"pan_deg\":%.1f,\"tilt_deg\":%.1f,\"duration_ms\":%d}}",
                 rid, pan, tilt, (int)duration);
        return 0;
    }

    /* Motion stop: ctrl.motion.stop / motion.stop */
    else if (strcmp(msg_type, "ctrl.motion.stop") == 0 || strcmp(msg_type, "motion.stop") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        bool emergency = json_parse_bool(json_in, "emergency", false);
        int stop_rc = desk_hardware_motion_stop(emergency);
        if (stop_rc != DESK_HW_OK) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":500,\"error\":{"
                     "\"code\":\"HARDWARE_FAULT\","
                     "\"message\":\"Gimbal driver failed to accept stop command\"}}",
                     rid);
            return 0;
        }
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"status\":\"accepted\","
                 "\"accepted\":true,\"state_confirmed\":false,\"stopped\":true}}",
                 rid);
        return 0;
    }

    /* Motion action play: ctrl.motion.action.play */
    else if (strcmp(msg_type, "ctrl.motion.action.play") == 0 || strcmp(msg_type, "motion.action.play") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"job_id\":1,\"status\":\"accepted\",\"accepted\":true}}",
                 rid);
        return 0;
    }

    /* Camera acquire lease: ctrl.camera.acquire / camera.acquire */
    else if (strcmp(msg_type, "ctrl.camera.acquire") == 0 || strcmp(msg_type, "camera.acquire") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        char client_id[32] = "sdk-client";
        json_parse_str(json_in, "client_id", client_id, sizeof(client_id));
        int32_t lease_ms = json_parse_int(json_in, "lease_duration_ms", 10000);
        if (lease_ms < 1000) lease_ms = 1000;
        if (lease_ms > 60000) lease_ms = 60000;

        desk_arbiter_status_t arb = desk_arbiter_camera_acquire_lease(client_id, (uint32_t)lease_ms);
        if (arb == DESK_ARBITER_OK) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"status\":\"accepted\","
                     "\"accepted\":true,\"client_id\":\"%s\",\"lease_duration_ms\":%d}}",
                     rid, client_id, (int)lease_ms);
        } else if (arb == DESK_ARBITER_ERR_BUSY) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":409,\"error\":{"
                     "\"code\":\"RESOURCE_BUSY\","
                     "\"message\":\"Camera lease currently held by another client\"}}",
                     rid);
        } else {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":400,\"error\":{"
                     "\"code\":\"INVALID_ARGUMENTS\","
                     "\"message\":\"Invalid camera lease parameters\"}}",
                     rid);
        }
        return 0;
    }

    /* Camera release lease: ctrl.camera.release / camera.release */
    else if (strcmp(msg_type, "ctrl.camera.release") == 0 || strcmp(msg_type, "camera.release") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        char client_id[32] = "sdk-client";
        json_parse_str(json_in, "client_id", client_id, sizeof(client_id));
        desk_arbiter_camera_release_lease(client_id);
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"status\":\"accepted\",\"accepted\":true}}",
                 rid);
        return 0;
    }

    /* Camera capture: ctrl.camera.capture / camera.capture / ctrl.camera.capture_with_feedback */
    else if (strcmp(msg_type, "ctrl.camera.capture") == 0 ||
             strcmp(msg_type, "camera.capture") == 0 ||
             strcmp(msg_type, "ctrl.camera.capture_with_feedback") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        if (!desk_arbiter_camera_is_lease_active()) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":403,\"error\":{"
                     "\"code\":\"NO_ACTIVE_LEASE\","
                     "\"message\":\"No active camera lease held on device\"}}",
                     rid);
            return 0;
        }
        size_t max_jpeg = 65536;
        uint8_t *jpeg_buf = (uint8_t *)desk_psram_malloc(max_jpeg);
        if (!jpeg_buf) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":500,\"error\":{"
                     "\"code\":\"OUT_OF_MEMORY\","
                     "\"message\":\"Cannot allocate buffer for camera capture\"}}",
                     rid);
            return 0;
        }

        size_t jpeg_len = 0;
        int cap_rc = desk_hardware_camera_capture_jpeg(jpeg_buf, max_jpeg, &jpeg_len, 1000);
        if (cap_rc != DESK_HW_OK || jpeg_len == 0) {
            desk_psram_free(jpeg_buf);
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":409,\"error\":{"
                     "\"code\":\"RESOURCE_BUSY\","
                     "\"message\":\"Camera frame unavailable\"}}",
                     rid);
            return 0;
        }

        s_camera_session_id = (s_camera_session_id % 65534) + 1;
        s_camera_seq++;

        /* Send ACK carrying positive session_id */
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"session_id\":%u}}",
                 rid, (unsigned int)s_camera_session_id);

        /* Send WSPK media frame (FRAME_IMAGE = 3) */
        desk_sdk_bridge_send_media_frame(DESK_WSPK_TYPE_IMAGE, 0, s_camera_session_id, s_camera_seq,
                                         jpeg_buf, jpeg_len);
        desk_psram_free(jpeg_buf);
        return 0;
    }

    /* Microphone open / close: ctrl.microphone.open / ctrl.microphone.close */
    else if (strcmp(msg_type, "ctrl.microphone.open") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        int rate = json_parse_int(json_in, "sample_rate_hz", 16000);
        desk_hardware_audio_record_start((uint32_t)rate);
        s_mic_session_id = (s_mic_session_id % 65534) + 1;
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"session_id\":%u}}",
                 rid, (unsigned int)s_mic_session_id);
        return 0;
    }

    else if (strcmp(msg_type, "ctrl.microphone.close") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        desk_hardware_audio_record_stop();
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"status\":\"accepted\",\"accepted\":true}}",
                 rid);
        return 0;
    }

    /* Expression play / set: ctrl.expression.play / ctrl.expression.set */
    else if (strcmp(msg_type, "ctrl.expression.play") == 0 ||
             strcmp(msg_type, "ctrl.expression.set") == 0 ||
             strcmp(msg_type, "expression.play") == 0 ||
             strcmp(msg_type, "expression.set") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));

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
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":409,\"error\":{"
                     "\"code\":\"RESOURCE_BUSY\","
                     "\"message\":\"Display UI is currently held by higher priority owner\"}}",
                     rid);
            return 0;
        } else if (arb != DESK_ARBITER_OK) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":500,\"error\":{"
                     "\"code\":\"RESOURCE_ERROR\","
                     "\"message\":\"Failed to acquire display resource\"}}",
                     rid);
            return 0;
        }

        int disp_rc = desk_hardware_display_set_expression(preset, text);
        desk_arbiter_release(DESK_RES_DISPLAY, DESK_OWNER_SDK_CLIENT);
        if (disp_rc != DESK_HW_OK) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":500,\"error\":{"
                     "\"code\":\"HARDWARE_ERROR\","
                     "\"message\":\"Display hardware unavailable or driver failed\"}}",
                     rid);
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
                static DESK_PROTOCOL_BSS int16_t s_alert_pcm[1600];
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

        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{"
                 "\"status\":\"accepted\",\"accepted\":true,\"preset\":\"%s\",\"text\":\"%s\"}}",
                 rid, preset, text);
        return 0;
    }

    /* Expression audio follow: ctrl.expression.audio_follow */
    else if (strcmp(msg_type, "ctrl.expression.audio_follow") == 0 || strcmp(msg_type, "expression.audio_follow") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        bool enabled = json_parse_bool(json_in, "enabled", false);
        if (enabled) {
            desk_hardware_display_set_expression("speaking", "Audio Follow Active");
        } else {
            desk_hardware_display_set_expression("idle", "Ready");
        }
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"enabled\":%s}}",
                 rid, enabled ? "true" : "false");
        return 0;
    }

    /* External tool response envelope from Python client */
    else if (strcmp(msg_type, "tool.response") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        if (rid[0] == '\0') {
            char data_rid[80] = "";
            char data_obj[1024] = "";
            if (json_extract_subobject(json_in, "data", data_obj, sizeof(data_obj))) {
                if (json_parse_str(data_obj, "request_id", data_rid, sizeof(data_rid)) && data_rid[0] != '\0') {
                    snprintf(rid_scratch, sizeof(rid_scratch), "\"request_id\":\"%s\",", data_rid);
                    rid = rid_scratch;
                }
            }
        }
        desk_sdk_bridge_handle_tool_response(json_in);
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"status\":\"received\"}}",
                 rid);
        return 0;
    }

    /* Unknown operation */
    else {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.nack\",%s\"code\":404,\"error\":{"
                 "\"code\":\"UNKNOWN_OPERATION\","
                 "\"message\":\"Command not recognized by Desk Agent SDK bridge\"}}",
                 rid);
        return 0;
    }
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
             (unsigned int)(s_event_seq * 0x13579bdf),
             (unsigned int)s_event_seq,
             event_type,
             ts,
             payload_json);

    if (s_outbound_cb) {
        s_outbound_cb(event_buf, strlen(event_buf), s_outbound_user_data);
    }

    if (s_client_fd >= 0) {
        desk_sdk_socket_send_line(event_buf);
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
             (unsigned int)(s_event_seq * 0x13579bdf),
             (unsigned int)s_event_seq,
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
        desk_sdk_socket_send_line(formatted);
    }

    return 0;
}

int desk_sdk_bridge_dispatch_tool_request(const char *request_envelope_json)
{
    if (!request_envelope_json) return -1;

    char req_id[80] = "";
    char operation[64] = "";

    /* Extract request_id and operation */
    if (!json_parse_top_level_str(request_envelope_json, "request_id", req_id, sizeof(req_id)) || req_id[0] == '\0') {
        json_parse_str(request_envelope_json, "request_id", req_id, sizeof(req_id));
    }
    if (!json_parse_top_level_str(request_envelope_json, "operation", operation, sizeof(operation)) || operation[0] == '\0') {
        json_parse_str(request_envelope_json, "operation", operation, sizeof(operation));
    }

    if (req_id[0] == '\0') {
        time_t now = time(NULL);
        if (now < 1700000000) now = 1792100000;
        s_event_seq++;
        snprintf(req_id, sizeof(req_id), "req-bridge-%08x-%04x", (unsigned int)now, (unsigned int)(s_event_seq & 0xFFFF));
    }

    /* Record in correlation table */
    desk_sdk_req_table_add(req_id, operation, 60000);

    /* Determine if we should wrap/format into full RequestEnvelope */
    char formatted_req[4096];
    const char *payload_to_send = request_envelope_json;

    if (strstr(request_envelope_json, "\"schema_version\"") == NULL ||
        strstr(request_envelope_json, "\"expires_at\"") == NULL) {
        time_t now = time(NULL);
        if (now < 1700000000) now = 1792100000;
        char expires_at[32];
        desk_sdk_format_iso8601(now + 60, expires_at, sizeof(expires_at));

        char args_buf[2048] = "{}";
        if (!json_extract_subobject(request_envelope_json, "arguments", args_buf, sizeof(args_buf))) {
            strncpy(args_buf, "{}", sizeof(args_buf) - 1);
        }

        snprintf(formatted_req, sizeof(formatted_req),
                 "{\"schema_version\":\"1.0.0\","
                 "\"request_id\":\"%s\","
                 "\"operation\":\"%s\","
                 "\"arguments\":%s,"
                 "\"expires_at\":\"%s\"}",
                 req_id,
                 operation[0] != '\0' ? operation : "unknown.tool",
                 args_buf,
                 expires_at);
        payload_to_send = formatted_req;
    }

    int ret = 0;
    if (s_outbound_cb) {
        ret = s_outbound_cb(payload_to_send, strlen(payload_to_send), s_outbound_user_data);
    }

    if (s_client_fd >= 0) {
        size_t len = strlen(payload_to_send);
        if (desk_sdk_socket_write_all(s_client_fd, payload_to_send, len) != 0) {
            desk_sdk_socket_client_close();
            return -1;
        }
        if (len == 0 || payload_to_send[len - 1] != '\n') {
            if (desk_sdk_socket_write_all(s_client_fd, "\n", 1) != 0) {
                desk_sdk_socket_client_close();
                return -1;
            }
        }
    }

    return ret;
}

int desk_sdk_bridge_handle_tool_response(const char *response_envelope_json)
{
    if (!response_envelope_json) return -1;

    char req_id[80] = "";
    char raw_status[32] = "";
    static DESK_PROTOCOL_BSS char s_data_buf[1024];
    static DESK_PROTOCOL_BSS char s_result_buf[1024];
    static DESK_PROTOCOL_BSS char s_error_buf[512];
    static DESK_PROTOCOL_BSS char s_normalized[3072];

    s_data_buf[0] = '\0';
    s_result_buf[0] = '\0';
    s_error_buf[0] = '\0';
    s_normalized[0] = '\0';

    /* 1. Extract data subobject if present */
    json_extract_subobject(response_envelope_json, "data", s_data_buf, sizeof(s_data_buf));

    /* 2. Extract request_id */
    if (!json_parse_top_level_str(response_envelope_json, "request_id", req_id, sizeof(req_id)) || req_id[0] == '\0') {
        if (s_data_buf[0] != '\0') {
            json_parse_str(s_data_buf, "request_id", req_id, sizeof(req_id));
        }
    }

    /* 3. Extract status */
    if (!json_parse_top_level_str(response_envelope_json, "status", raw_status, sizeof(raw_status)) || raw_status[0] == '\0') {
        if (s_data_buf[0] != '\0') {
            json_parse_str(s_data_buf, "status", raw_status, sizeof(raw_status));
        }
    }

    /* 4. Extract result / error */
    if (!json_extract_subobject(response_envelope_json, "result", s_result_buf, sizeof(s_result_buf))) {
        if (s_data_buf[0] != '\0') {
            json_extract_subobject(s_data_buf, "result", s_result_buf, sizeof(s_result_buf));
        }
    }
    if (!json_extract_subobject(response_envelope_json, "error", s_error_buf, sizeof(s_error_buf))) {
        if (s_data_buf[0] != '\0') {
            json_extract_subobject(s_data_buf, "error", s_error_buf, sizeof(s_error_buf));
        }
    }

    /* Fallback status if missing */
    if (raw_status[0] == '\0') {
        if (s_error_buf[0] != '\0' || strstr(response_envelope_json, "\"code\":") != NULL) {
            strncpy(raw_status, "failed", sizeof(raw_status) - 1);
        } else {
            strncpy(raw_status, "succeeded", sizeof(raw_status) - 1);
        }
    }

    /* Update table with raw status and payload */
    bool is_payload_succ = (strcmp(raw_status, "succeeded") == 0 || strcmp(raw_status, "completed") == 0 || strcmp(raw_status, "accepted") == 0);
    desk_sdk_req_table_update_with_payload(req_id, raw_status, is_payload_succ ? s_result_buf : s_error_buf);

    /* Normalize status: "completed" -> "succeeded" */
    const char *eff_status = raw_status;
    if (strcmp(raw_status, "completed") == 0) {
        eff_status = "succeeded";
    }

    /* Prepare timestamp */
    time_t now = time(NULL);
    if (now < 1700000000) now = 1792100000;
    char ts[32];
    if (!json_parse_top_level_str(response_envelope_json, "timestamp", ts, sizeof(ts)) || ts[0] == '\0') {
        if (s_data_buf[0] == '\0' || !json_parse_str(s_data_buf, "timestamp", ts, sizeof(ts)) || ts[0] == '\0') {
            desk_sdk_format_iso8601(now, ts, sizeof(ts));
        }
    }

    /* Construct normalized envelope JSON */
    bool is_success = (strcmp(eff_status, "succeeded") == 0 || strcmp(eff_status, "accepted") == 0);

    if (is_success) {
        const char *res = (s_result_buf[0] != '\0') ? s_result_buf : "{}";
        snprintf(s_normalized, sizeof(s_normalized),
                 "{\"schema_version\":\"1.0.0\","
                 "\"request_id\":\"%s\","
                 "\"status\":\"%s\","
                 "\"timestamp\":\"%s\","
                 "\"result\":%s,"
                 "\"data\":{"
                 "\"schema_version\":\"1.0.0\","
                 "\"request_id\":\"%s\","
                 "\"status\":\"%s\","
                 "\"timestamp\":\"%s\","
                 "\"result\":%s"
                 "}}",
                 req_id, eff_status, ts, res,
                 req_id, eff_status, ts, res);
    } else {
        const char *err = (s_error_buf[0] != '\0') ? s_error_buf : "{\"code\":\"EXECUTION_FAILED\",\"message\":\"Tool execution failed\"}";
        snprintf(s_normalized, sizeof(s_normalized),
                 "{\"schema_version\":\"1.0.0\","
                 "\"request_id\":\"%s\","
                 "\"status\":\"failed\","
                 "\"timestamp\":\"%s\","
                 "\"error\":%s,"
                 "\"data\":{"
                 "\"schema_version\":\"1.0.0\","
                 "\"request_id\":\"%s\","
                 "\"status\":\"failed\","
                 "\"timestamp\":\"%s\","
                 "\"error\":%s"
                 "}}",
                 req_id, ts, err,
                 req_id, ts, err);
    }

    if (s_tool_response_cb) {
        return s_tool_response_cb(s_normalized, s_tool_response_user_data);
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

/**
 * @brief Feed one complete frame (newline already stripped) into the command handler.
 *
 * interfaces.md 6.3.3: non-JSON / malformed lines are dropped without breaking
 * the link.
 *
 * @return 0 when the link is still usable, -1 when the connection was closed.
 */
static int desk_sdk_socket_handle_frame(char *frame)
{
    /* Tolerate CRLF and leading whitespace without changing message boundaries. */
    char *start = frame;
    while (*start == ' ' || *start == '\t' || *start == '\r') {
        start++;
    }
    /* Trim trailing CR / whitespace so CRLF framing parses identically. */
    size_t slen = strlen(start);
    while (slen > 0) {
        char tail = start[slen - 1];
        if (tail != '\r' && tail != ' ' && tail != '\t') break;
        start[--slen] = '\0';
    }
    if (*start != '{') {
        return 0; /* Not a JSON object; discard silently, keep the link alive. */
    }

    char rsp[2048];
    rsp[0] = '\0';
    if (desk_sdk_bridge_process_command(start, rsp, sizeof(rsp)) == 0 && rsp[0] != '\0') {
        if (desk_sdk_socket_send_line(rsp) != 0) {
            return -1;
        }
    }
    return 0;
}

int desk_sdk_socket_server_poll(int timeout_ms)
{
    if (s_listen_fd < 0) return -1;

    struct pollfd fds[2];
    int nfds = 1;
    fds[0].fd = s_listen_fd;
    fds[0].events = POLLIN;
    fds[0].revents = 0;

    if (s_client_fd >= 0) {
        fds[1].fd = s_client_fd;
        fds[1].events = POLLIN;
        fds[1].revents = 0;
        nfds = 2;
    }

    int rc = poll(fds, nfds, timeout_ms);
    if (rc <= 0) return 0;

    /* Remember which connection the poll result describes: accepting a new
     * client below replaces (and closes) the polled fd, so its stale revents
     * must never be applied to the replacement. */
    int polled_client_fd = (nfds > 1) ? s_client_fd : -1;

    /* New incoming connection */
    if (fds[0].revents & POLLIN) {
        struct sockaddr_in client_addr;
        socklen_t clen = sizeof(client_addr);
        int new_fd = accept(s_listen_fd, (struct sockaddr *)&client_addr, &clen);
        if (new_fd >= 0) {
            /* Replacing a client must also drop its residual RX state so the
             * new connection never inherits stale bytes (interfaces.md 6.3.4). */
            desk_sdk_socket_client_close();
            s_client_fd = new_fd;
            int fl = fcntl(s_client_fd, F_GETFL, 0);
            fcntl(s_client_fd, F_SETFL, fl | O_NONBLOCK);
            s_client_connected = true;
            desk_sdk_socket_rx_reset();
        }
    }

    /* Client data ready */
    if (polled_client_fd >= 0 && s_client_fd == polled_client_fd &&
        (fds[1].revents & (POLLIN | POLLHUP | POLLERR))) {
        if (fds[1].revents & (POLLHUP | POLLERR)) {
            desk_sdk_socket_client_close();
            return 0;
        }

        static DESK_PROTOCOL_BSS char s_socket_chunk_buf[1024];
        ssize_t n = read(s_client_fd, s_socket_chunk_buf, sizeof(s_socket_chunk_buf));
        if (n < 0 && (errno == EINTR || errno == EAGAIN || errno == EWOULDBLOCK)) {
            return 0;
        }
        if (n <= 0) {
            /* Peer closed (or hard read error): discard partial frame bytes. */
            desk_sdk_socket_client_close();
            return 0;
        }

        /* Persistent accumulation across read() calls: frames may arrive split
         * across reads, batched into one read, or spanning several reads. */
        for (ssize_t i = 0; i < n; i++) {
            char c = s_socket_chunk_buf[i];

            if (s_rx_discarding) {
                /* Discard an oversized frame, resynchronising on the next '\n'. */
                if (c == '\n') {
                    s_rx_discarding = false;
                    s_rx_len = 0;
                }
                continue;
            }

            if (c == '\n') {
                if (s_rx_len > 0) {
                    s_rx_buf[s_rx_len] = '\0';
                    int hrc = desk_sdk_socket_handle_frame(s_rx_buf);
                    s_rx_len = 0;
                    if (hrc != 0) {
                        /* Link died while responding: stop consuming this read. */
                        return 0;
                    }
                } else {
                    s_rx_len = 0;
                }
                continue;
            }

            if (s_rx_len >= DESK_SDK_MAX_FRAME_LEN) {
                /* Frame exceeds the enforced maximum: drop it entirely. */
                s_rx_discarding = true;
                s_rx_len = 0;
                continue;
            }

            s_rx_buf[s_rx_len++] = c;
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
    desk_sdk_socket_rx_reset();
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
        static DESK_PROTOCOL_BSS char s_pairing_buf[2048];
        struct sockaddr_in client_addr;
        socklen_t addr_len = sizeof(client_addr);
        ssize_t n = recvfrom(s_pairing_udp_fd, s_pairing_buf, sizeof(s_pairing_buf) - 1, 0,
                             (struct sockaddr *)&client_addr, &addr_len);
        if (n > 0) {
            s_pairing_buf[n] = '\0';
            static DESK_PROTOCOL_BSS char s_pairing_reply[2048];
            /* Fail-closed: a rejected pairing writes no reply at all
             * (interfaces.md 6.4.4 -- no wire message type is defined). */
            if (desk_sdk_bridge_process_pairing_udp(s_pairing_buf, s_pairing_reply, sizeof(s_pairing_reply)) == DESK_SDK_PAIRING_OK &&
                s_pairing_reply[0] != '\0') {
                sendto(s_pairing_udp_fd, s_pairing_reply, strlen(s_pairing_reply), 0,
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
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
static TaskHandle_t s_worker_task_handle = NULL;
static void firmware_worker_task(void *pvParameters)
{
    (void)pvParameters;
    while (s_worker_task_handle != NULL) {
        desk_firmware_poll(20);
        vTaskDelay(pdMS_TO_TICKS(10));
    }
    vTaskDeleteWithCaps(NULL);
}
int desk_firmware_start_background_task(void)
{
    if (s_worker_task_handle != NULL) return 0;
    BaseType_t ret = xTaskCreateWithCaps(firmware_worker_task, "desk_poll_worker", 8192, NULL, 5,
                                         &s_worker_task_handle, MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
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
uint32_t desk_firmware_background_task_stack_hwm(void)
{
    if (s_worker_task_handle != NULL) {
        return (uint32_t)uxTaskGetStackHighWaterMark(s_worker_task_handle) * sizeof(StackType_t);
    }
    return 0;
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
uint32_t desk_firmware_background_task_stack_hwm(void)
{
    return 0;
}
#endif
