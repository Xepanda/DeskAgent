/**
 * @file desk_sdk_bridge.c
 * @brief Implementation of Desk Agent R1 Official WatcheRobot SDK Protocol Bridge.
 */

#include "desk_sdk_bridge.h"
#include "desk_watcher_log_source.h"
#include "desk_tclaw_bridge.h"
#include "desk_notification.h"
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
#include <stdatomic.h>

static pthread_mutex_t s_ws_camera_owner_mutex = PTHREAD_MUTEX_INITIALIZER;
static char s_ws_camera_owner_id[32];

#define DESK_CAMERA_STREAM_FPS 15.0f
#define DESK_CAMERA_STREAM_PERIOD_MS 66U
#define DESK_CAMERA_STREAM_READY_TIMEOUT_MS 1800U
#define DESK_CAMERA_STREAM_MAX_JPEG (DESK_WSPK_MAX_PAYLOAD_SIZE - 20U)

typedef struct {
    pthread_mutex_t mutex;
    pthread_cond_t changed;
    bool active;
    bool starting;
    bool stop_requested;
    bool external_join;
    bool ready;
    bool ack_sent;
    bool producer_failed;
    bool thread_started;
    bool cleanup_in_progress;
    pthread_t thread;
    uint16_t stream_id;
    uint16_t last_stopped_stream_id;
    uint32_t sequence;
    uint32_t last_timestamp_ms;
    uint64_t next_capture_ms;
    char client_id[32];
    char session_id[65];
    char last_client_id[32];
    char last_session_id[65];
    uint8_t *frame_slot;
    size_t frame_len;
    uint32_t source_drop_count;
    uint32_t transport_drop_count;
} desk_camera_stream_state_t;

static desk_camera_stream_state_t s_camera_stream = {
    .mutex = PTHREAD_MUTEX_INITIALIZER,
    .changed = PTHREAD_COND_INITIALIZER,
};
static uint16_t s_camera_stream_id_seed;
static atomic_int s_camera_exclusive_state = ATOMIC_VAR_INIT(0); /* 0 idle, 1 capture, 2 stream */
static int desk_sdk_camera_stream_stop_internal(bool from_worker);
static bool desk_sdk_camera_stream_available(void);
static bool desk_sdk_log_command_id_valid(const char *id);
static bool desk_log_object_keys_allowed(const char *object, const char *const *allowed,
                                         size_t allowed_count);

static uint64_t desk_sdk_camera_worker_now_ms(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return (uint64_t)now.tv_sec * 1000ULL + (uint64_t)now.tv_nsec / 1000000ULL;
}

#ifdef DESK_SDK_TESTING
static bool s_test_camera_stream_available;
static int (*s_test_camera_binary_send_hook)(const char *, size_t);
void desk_sdk_test_camera_stream_set_available(bool available) { s_test_camera_stream_available = available; }
void desk_sdk_test_camera_binary_send_set_hook(int (*hook)(const char *, size_t)) { s_test_camera_binary_send_hook = hook; }
uint32_t desk_sdk_test_camera_stream_transport_drop_count(void)
{
    pthread_mutex_lock(&s_camera_stream.mutex);
    uint32_t count = s_camera_stream.transport_drop_count;
    pthread_mutex_unlock(&s_camera_stream.mutex);
    return count;
}
uint32_t desk_sdk_test_camera_stream_source_drop_count(void)
{
    pthread_mutex_lock(&s_camera_stream.mutex);
    uint32_t count = s_camera_stream.source_drop_count;
    pthread_mutex_unlock(&s_camera_stream.mutex);
    return count;
}
#endif

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
static atomic_uint_least32_t s_roller_input_send_failures = ATOMIC_VAR_INIT(0);
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
static unsigned s_pairing_failed_attempts = 0;
static uint64_t s_pairing_code_expires_at_ms = 0;
static pthread_mutex_t s_pairing_code_mutex = PTHREAD_MUTEX_INITIALIZER;
static char s_pair_request_id[33] = "";
static char s_pair_daemon_instance_id[33] = "";
static char s_pair_target_mode[32] = "";
static char s_pair_host[INET_ADDRSTRLEN] = "";
static uint16_t s_pair_websocket_port = 0;
static bool desk_sdk_is_lower_hex_64(const char *s);

/* CR-009 L2 stream lease. Only structured A1 records are serialized. */
static pthread_mutex_t s_log_mutex = PTHREAD_MUTEX_INITIALIZER;
static bool s_local_log_enabled = false;
static bool s_log_stream_active = false;
static bool s_log_camera_only = false;
static char s_log_stream_id[33] = "";
static char s_log_min_level[8] = "WARN";
static char s_log_boot_id[33] = "";
static desk_watcher_log_record_t s_log_pending_record;
static desk_watcher_log_gap_t s_log_pending_gap;
static bool s_log_has_pending_record;
static bool s_log_has_pending_gap;

static void desk_sdk_log_source_reset_locked(void)
{
    desk_watcher_log_source_set_enabled(false);
    s_log_has_pending_record = false;
    s_log_has_pending_gap = false;
}

void desk_sdk_bridge_set_local_log_enabled(bool enabled)
{
    pthread_mutex_lock(&s_log_mutex);
    s_local_log_enabled = enabled;
    if (!enabled) {
        s_log_stream_active = false;
        s_log_stream_id[0] = '\0';
        desk_sdk_log_source_reset_locked();
    }
    pthread_mutex_unlock(&s_log_mutex);
}

void desk_sdk_bridge_log_session_closed(void)
{
    pthread_mutex_lock(&s_log_mutex);
    s_log_stream_active = false;
    s_log_camera_only = false;
    s_log_stream_id[0] = '\0';
    desk_sdk_log_source_reset_locked();
    pthread_mutex_unlock(&s_log_mutex);
}

int desk_sdk_bridge_log_next_text(char *out_json, size_t out_len)
{
    if (!out_json || out_len == 0) return -1;
    pthread_mutex_lock(&s_log_mutex);
    if (!s_local_log_enabled || !s_log_stream_active ||
        !desk_sdk_ws_client_is_authenticated() ||
        !desk_watcher_log_source_is_enabled()) {
        pthread_mutex_unlock(&s_log_mutex);
        return 0;
    }
    if (!s_log_has_pending_record)
        s_log_has_pending_record = desk_watcher_log_source_try_dequeue(&s_log_pending_record);
    if (!s_log_has_pending_gap)
        s_log_has_pending_gap = desk_watcher_log_source_take_gap(&s_log_pending_gap);

    int n = 0;
    if (s_log_has_pending_gap && (!s_log_has_pending_record ||
        s_log_pending_gap.first_dropped_sequence < s_log_pending_record.sequence)) {
        n = snprintf(out_json, out_len,
            "{\"type\":\"sys.log.gap\",\"data\":{\"log_schema_version\":\"1.0.0\",\"stream_id\":\"%s\",\"boot_id\":\"%s\",\"first_dropped_sequence\":%llu,\"last_dropped_sequence\":%llu,\"dropped_count\":%llu,\"reason\":\"QUEUE_FULL\"}}",
            s_log_stream_id, s_log_boot_id,
            (unsigned long long)s_log_pending_gap.first_dropped_sequence,
            (unsigned long long)s_log_pending_gap.last_dropped_sequence,
            (unsigned long long)s_log_pending_gap.dropped_count);
        s_log_has_pending_gap = false;
    } else if (s_log_has_pending_record) {
        if (s_log_pending_record.event_id != DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY) {
            s_log_has_pending_record = false;
            pthread_mutex_unlock(&s_log_mutex);
            return 0;
        }
        n = snprintf(out_json, out_len,
            "{\"type\":\"sys.log.event\",\"data\":{\"log_schema_version\":\"1.0.0\",\"stream_id\":\"%s\",\"boot_id\":\"%s\",\"sequence\":%llu,\"uptime_ms\":%llu,\"level\":\"WARN\",\"component\":\"camera\",\"event_id\":\"CAMERA_CAPTURE_EMPTY\",\"fields\":{}}}",
            s_log_stream_id, s_log_boot_id,
            (unsigned long long)s_log_pending_record.sequence,
            (unsigned long long)s_log_pending_record.uptime_ms);
        s_log_has_pending_record = false;
    }
    pthread_mutex_unlock(&s_log_mutex);
    return n > 0 && (size_t)n < out_len ? n : (n < 0 ? -1 : 0);
}

#define DESK_SDK_PAIRING_CODE_TTL_MS (5ULL * 60ULL * 1000ULL)

#ifdef DESK_SDK_TESTING
static uint64_t s_test_monotonic_now_ms;

void desk_sdk_test_set_monotonic_ms(uint64_t now_ms)
{
    s_test_monotonic_now_ms = now_ms;
}

static bool desk_sdk_monotonic_now_ms(uint64_t *now_ms)
{
    if (!now_ms) return false;
    *now_ms = s_test_monotonic_now_ms;
    return true;
}
#else
static bool desk_sdk_monotonic_now_ms(uint64_t *now_ms)
{
    if (!now_ms) return false;
    struct timespec ts;
    if (clock_gettime(CLOCK_MONOTONIC, &ts) != 0 || ts.tv_sec < 0) return false;
    *now_ms = (uint64_t)ts.tv_sec * 1000ULL + (uint64_t)(ts.tv_nsec / 1000000ULL);
    return true;
}
#endif

#ifdef DESK_SDK_TESTING
typedef int (*desk_sdk_test_roller_input_send_hook_t)(desk_sdk_roller_delta_t delta,
                                                       int64_t timestamp_ms);
static desk_sdk_test_roller_input_send_hook_t s_test_roller_input_send_hook;

void desk_sdk_test_set_roller_input_send_hook(desk_sdk_test_roller_input_send_hook_t hook)
{
    s_test_roller_input_send_hook = hook;
}
#endif

static void desk_sdk_roller_input_observer(desk_roller_delta_t delta, void *user_data)
{
    (void)user_data;
    desk_sdk_roller_delta_t sdk_delta;
    if (delta == DESK_ROLLER_DELTA_UP) {
        sdk_delta = DESK_SDK_ROLLER_DELTA_POSITIVE;
    } else if (delta == DESK_ROLLER_DELTA_DOWN) {
        sdk_delta = DESK_SDK_ROLLER_DELTA_NEGATIVE;
    } else {
        atomic_fetch_add(&s_roller_input_send_failures, 1);
        return;
    }

    uint64_t monotonic_ms = 0;
    if (!desk_sdk_monotonic_now_ms(&monotonic_ms) || monotonic_ms > INT64_MAX) {
        atomic_fetch_add(&s_roller_input_send_failures, 1);
        return;
    }

#ifdef DESK_SDK_TESTING
    int rc = s_test_roller_input_send_hook
        ? s_test_roller_input_send_hook(sdk_delta, (int64_t)monotonic_ms)
        : desk_sdk_bridge_send_roller_input(sdk_delta, (int64_t)monotonic_ms);
#else
    int rc = desk_sdk_bridge_send_roller_input(sdk_delta, (int64_t)monotonic_ms);
#endif
    if (rc != DESK_SDK_ROLLER_OK)
        atomic_fetch_add(&s_roller_input_send_failures, 1);
}

uint32_t desk_sdk_bridge_get_roller_input_failure_count(void)
{
    return atomic_load(&s_roller_input_send_failures);
}

static bool desk_sdk_is_lower_hex_32(const char *s)
{
    if (!s || strlen(s) != 32) return false;
    for (size_t i = 0; i < 32; ++i) {
        if (!((s[i] >= '0' && s[i] <= '9') || (s[i] >= 'a' && s[i] <= 'f'))) return false;
    }
    return true;
}

int desk_sdk_bridge_build_hardware_hello(char *out_json, size_t max_len)
{
    if (!out_json || max_len == 0) return -1;
    pthread_mutex_lock(&s_pairing_code_mutex);
    if (!s_paired ||
        !desk_sdk_is_lower_hex_32(s_pair_request_id) ||
        !desk_sdk_is_lower_hex_32(s_pair_daemon_instance_id) ||
        !desk_sdk_is_lower_hex_64(s_active_pairing_token) ||
        !(strcmp(s_pair_target_mode, "python_sdk") == 0 ||
          strcmp(s_pair_target_mode, "desktop_link") == 0)) {
        pthread_mutex_unlock(&s_pairing_code_mutex);
        return -1;
    }

    int n = snprintf(out_json, max_len,
        "{\"type\":\"sys.client.hello\",\"code\":0,\"data\":{"
        "\"role\":\"hardware\",\"pairing_protocol\":\"watcher-lan-pairing\","
        "\"pairing_version\":\"1.0\",\"pair_request_id\":\"%s\","
        "\"daemon_instance_id\":\"%s\",\"session_token\":\"%s\",\"mode\":\"%s\"}}",
        s_pair_request_id, s_pair_daemon_instance_id, s_active_pairing_token,
        s_pair_target_mode);
    pthread_mutex_unlock(&s_pairing_code_mutex);
    return n >= 0 && (size_t)n < max_len ? n : -1;
}

int desk_sdk_bridge_get_pairing_target(char *host, size_t host_len, uint16_t *port)
{
    if (!host || host_len == 0 || !port) return -1;
    pthread_mutex_lock(&s_pairing_code_mutex);
    if (s_pair_host[0] == '\0' || s_pair_websocket_port == 0 || !s_paired ||
        strlen(s_pair_host) + 1 > host_len) {
        pthread_mutex_unlock(&s_pairing_code_mutex);
        return -1;
    }
    strcpy(host, s_pair_host);
    *port = s_pair_websocket_port;
    pthread_mutex_unlock(&s_pairing_code_mutex);
    return 0;
}

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

static bool desk_sdk_pairing_code_clear_locked(void)
{
    bool was_configured = s_pairing_code_configured;
    memset(s_expected_pairing_code, 0, sizeof(s_expected_pairing_code));
    s_pairing_code_configured = false;
    s_pairing_failed_attempts = 0;
    s_pairing_code_expires_at_ms = 0;
    return was_configured;
}

/* Return true when no valid code remains; `cleared` is true only when this
 * call consumed an active window and therefore owns the one UI notification. */
static bool desk_sdk_pairing_code_expire_if_needed_locked(bool *cleared)
{
    if (cleared) *cleared = false;
    uint64_t now_ms = 0;
    if (!s_pairing_code_configured) return true;
    if (!desk_sdk_monotonic_now_ms(&now_ms) || now_ms >= s_pairing_code_expires_at_ms) {
        bool was_configured = desk_sdk_pairing_code_clear_locked();
        if (cleared) *cleared = was_configured;
        return true;
    }
    return false;
}

static void desk_sdk_pairing_ui_notify_consumed(bool notify)
{
    if (notify) desk_tclaw_pairing_ui_on_pairing_consumed();
}

static void desk_sdk_pairing_code_expire_if_needed(void)
{
    bool cleared = false;
    pthread_mutex_lock(&s_pairing_code_mutex);
    (void)desk_sdk_pairing_code_expire_if_needed_locked(&cleared);
    pthread_mutex_unlock(&s_pairing_code_mutex);
    desk_sdk_pairing_ui_notify_consumed(cleared);
}

static bool desk_sdk_pairing_code_failed_attempt_locked(void)
{
    if (s_pairing_failed_attempts < DESK_SDK_PAIRING_MAX_FAILED_ATTEMPTS) {
        ++s_pairing_failed_attempts;
    }
    if (s_pairing_failed_attempts >= DESK_SDK_PAIRING_MAX_FAILED_ATTEMPTS) {
        return desk_sdk_pairing_code_clear_locked();
    }
    return false;
}

int desk_sdk_bridge_set_pairing_code(const char *code)
{
    pthread_mutex_lock(&s_pairing_code_mutex);
    if (!code || code[0] == '\0') {
        desk_sdk_pairing_code_clear_locked();
        pthread_mutex_unlock(&s_pairing_code_mutex);
        return 0;
    }
    if (!desk_sdk_is_six_digits(code)) {
        /* Reject malformed provisioning input; keep the previous configuration. */
        pthread_mutex_unlock(&s_pairing_code_mutex);
        return -1;
    }
    uint64_t now_ms = 0;
    if (!desk_sdk_monotonic_now_ms(&now_ms) || UINT64_MAX - now_ms < DESK_SDK_PAIRING_CODE_TTL_MS) {
        desk_sdk_pairing_code_clear_locked();
        pthread_mutex_unlock(&s_pairing_code_mutex);
        return -1;
    }
    memset(s_expected_pairing_code, 0, sizeof(s_expected_pairing_code));
    memcpy(s_expected_pairing_code, code, 7);
    s_pairing_code_configured = true;
    s_pairing_failed_attempts = 0;
    s_pairing_code_expires_at_ms = now_ms + DESK_SDK_PAIRING_CODE_TTL_MS;
    pthread_mutex_unlock(&s_pairing_code_mutex);
    return 0;
}

void desk_sdk_bridge_clear_pairing_code(void)
{
    pthread_mutex_lock(&s_pairing_code_mutex);
    desk_sdk_pairing_code_clear_locked();
    pthread_mutex_unlock(&s_pairing_code_mutex);
}

bool desk_sdk_bridge_is_pairing_code_configured(void)
{
    bool cleared = false;
    pthread_mutex_lock(&s_pairing_code_mutex);
    bool configured = !desk_sdk_pairing_code_expire_if_needed_locked(&cleared);
    pthread_mutex_unlock(&s_pairing_code_mutex);
    desk_sdk_pairing_ui_notify_consumed(cleared);
    return configured;
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
        pthread_mutex_lock(&s_pairing_code_mutex);
        desk_sdk_ws_client_close();
        generate_dynamic_token(s_active_pairing_token, sizeof(s_active_pairing_token));
        desk_sdk_ws_client_close();
        pthread_mutex_unlock(&s_pairing_code_mutex);
        return 0;
    }

    /* R11: refuse any credential that could not legally appear in a pair.accept.
     * Arming it would make the firmware emit a message the official SDK's own
     * parse_udp_message() rejects. The previously armed credential is preserved. */
    if (!desk_sdk_is_lower_hex_64(token)) {
        return -1;
    }

    pthread_mutex_lock(&s_pairing_code_mutex);
    bool token_changed = strcmp(s_active_pairing_token, token) != 0;
    if (token_changed) {
        /* A changed credential invalidates the current socket's handshake. */
        desk_sdk_ws_client_close();
    }
    memcpy(s_active_pairing_token, token, sizeof(s_active_pairing_token));
    /* R9 (interfaces.md 6.6): this provisioning entry point sets credential
     * material ONLY. `s_paired` means "a client has successfully completed a
     * pairing handshake" and may only be set by the validated pairing path in
     * desk_sdk_bridge_process_pairing_udp() (interfaces.md 6.4). */
    if (token_changed) {
        /* Invalidate any connect that began while the token was replaced. */
        desk_sdk_ws_client_close();
    }
    pthread_mutex_unlock(&s_pairing_code_mutex);
    return 0;
}

int desk_sdk_bridge_get_pairing_token(char *token, size_t max_len)
{
    if (!token || max_len == 0) return -1;
    pthread_mutex_lock(&s_pairing_code_mutex);
    if (s_active_pairing_token[0] == '\0') {
        generate_dynamic_token(s_active_pairing_token, sizeof(s_active_pairing_token));
    }
    strncpy(token, s_active_pairing_token, max_len - 1);
    token[max_len - 1] = '\0';
    pthread_mutex_unlock(&s_pairing_code_mutex);
    return 0;
}

void desk_sdk_bridge_set_auth_required(bool required)
{
    bool policy_changed = s_auth_required != required;
    if (policy_changed) {
        /* Changing the handshake policy invalidates any prior session latch. */
        desk_sdk_ws_client_close();
    }
    s_auth_required = required;
    if (policy_changed) {
        /* Invalidate connects that crossed the policy update before they can
         * commit their previous handshake result. */
        desk_sdk_ws_client_close();
    }
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
    desk_sdk_ws_client_close();
    desk_sdk_bridge_set_local_log_enabled(false);
    s_client_connected = false;
    s_event_seq = 0;
    atomic_store(&s_roller_input_send_failures, 0);
    s_outbound_cb = NULL;
    s_outbound_user_data = NULL;
    s_tool_response_cb = NULL;
    s_tool_response_user_data = NULL;
    s_listen_fd = -1;
    s_client_fd = -1;
    s_bound_port = 0;
    s_last_heartbeat_time = 0;
    pthread_mutex_lock(&s_pairing_code_mutex);
    desk_sdk_pairing_code_clear_locked();
    pthread_mutex_unlock(&s_pairing_code_mutex);
    desk_sdk_ws_client_close();
    desk_sdk_req_table_init();
    desk_sdk_socket_rx_reset();
    desk_sdk_ws_client_close();
    if (desk_hardware_register_roller_delta_cb(desk_sdk_roller_input_observer, NULL) != DESK_HW_OK) {
        printf("[SDK-INPUT] roller observer registration failed\n");
        return -1;
    }
    return 0;
}

void desk_sdk_bridge_reset(void)
{
    desk_sdk_ws_client_close();
    desk_sdk_bridge_set_local_log_enabled(false);
    s_client_connected = false;
    s_event_seq = 0;
    pthread_mutex_lock(&s_pairing_code_mutex);
    bool had_pairing_state = s_pairing_code_configured || s_paired ||
        s_pair_request_id[0] != '\0' || s_pair_daemon_instance_id[0] != '\0' ||
        s_pair_host[0] != '\0' || s_pair_websocket_port != 0;
    s_active_pairing_token[0] = '\0';
    s_paired = false;
    s_pair_request_id[0] = '\0';
    s_pair_daemon_instance_id[0] = '\0';
    s_pair_target_mode[0] = '\0';
    s_pair_host[0] = '\0';
    s_pair_websocket_port = 0;
    desk_sdk_pairing_code_clear_locked();
    pthread_mutex_unlock(&s_pairing_code_mutex);
    s_auth_required = false;
    desk_sdk_ws_client_close();
    s_last_heartbeat_time = 0;
    desk_sdk_req_table_init();
    desk_sdk_socket_rx_reset();
    if (had_pairing_state) desk_tclaw_pairing_ui_on_sdk_session_reset();
}

bool desk_sdk_bridge_is_connected(void)
{
    return s_client_connected || desk_sdk_ws_client_is_connected();
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

int desk_sdk_bridge_pack_wjpg_v1(uint32_t sequence,
                                 uint32_t capture_timestamp_ms,
                                 const uint8_t *jpeg,
                                 size_t jpeg_len,
                                 uint8_t *out_buf,
                                 size_t max_buf_len,
                                 size_t *out_len)
{
    const size_t header_size = 20;
    if (out_len) *out_len = 0;
    if (!jpeg || !out_buf || !out_len || jpeg_len < 4) return -1;
    if (jpeg_len > DESK_WSPK_MAX_PAYLOAD_SIZE - header_size) return -2;
    if (jpeg[0] != 0xFF || jpeg[1] != 0xD8 ||
        jpeg[jpeg_len - 2] != 0xFF || jpeg[jpeg_len - 1] != 0xD9) return -1;
    if (max_buf_len < header_size + jpeg_len) return -3;

    memcpy(out_buf, "WJPG", 4);
    out_buf[4] = 1;
    out_buf[5] = 0;
    write_u16_le(&out_buf[6], (uint16_t)header_size);
    write_u32_le(&out_buf[8], sequence);
    write_u32_le(&out_buf[12], capture_timestamp_ms);
    write_u32_le(&out_buf[16], (uint32_t)jpeg_len);
    memcpy(&out_buf[header_size], jpeg, jpeg_len);
    *out_len = header_size + jpeg_len;
    return 0;
}

static uint16_t s_camera_session_id = 100;
static uint32_t s_camera_seq = 0;

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
        if (packed_len > DESK_WSPK_MAX_FRAME_SIZE) {
            rc = -4;
        } else if (desk_sdk_ws_client_is_connected()) {
            if (desk_sdk_ws_client_send_binary(target_buf, packed_len) != 0) rc = -3;
        } else if (s_client_fd >= 0) {
            /* WSPK framing is unchanged; only the write path now guarantees the
             * full frame is transmitted (interfaces.md 6.3.5). */
            if (desk_sdk_socket_write_all(s_client_fd, target_buf, packed_len) != 0) {
                desk_sdk_socket_client_close();
                rc = -3;
            }
        }
        if (rc == 0 && s_outbound_cb) {
            s_outbound_cb((const char *)target_buf, packed_len, s_outbound_user_data);
        }
    }

    if (heap_buf) {
        desk_psram_free(heap_buf);
    }
    return rc;
}

/* UDP Pairing (watcher_lan_pairing_v1 on port 37021) */
static int desk_sdk_bridge_process_pairing_udp_locked(const char *udp_in, char *udp_out,
                                                       size_t max_out_len,
                                                       bool *notify_pairing_consumed)
{
    if (notify_pairing_consumed) *notify_pairing_consumed = false;
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
    int32_t websocket_port = json_parse_int(udp_in, "websocket_port", -1);

    /* interfaces.md 6.4 / CR-001: never grant a pairing before the pairing code
     * has been validated against the explicitly provisioned device-side code.
 * diagnostics contain only a reason and request_id. */
    bool code_expired = false;
    if (desk_sdk_pairing_code_expire_if_needed_locked(&code_expired)) {
        if (notify_pairing_consumed) *notify_pairing_consumed = code_expired;
        printf("[SDK-PAIR] pair.request rejected: reason=window_closed request_id=%s\n", req_id);
        return DESK_SDK_PAIRING_REJECTED;
    }
    if (!desk_sdk_is_six_digits(pairing_code)) {
        if (notify_pairing_consumed)
            *notify_pairing_consumed = desk_sdk_pairing_code_failed_attempt_locked();
        printf("[SDK-PAIR] pair.request rejected: reason=malformed_code request_id=%s\n", req_id);
        return DESK_SDK_PAIRING_REJECTED;
    }
    if (!desk_sdk_secure_equals(pairing_code, s_expected_pairing_code)) {
        if (notify_pairing_consumed)
            *notify_pairing_consumed = desk_sdk_pairing_code_failed_attempt_locked();
        printf("[SDK-PAIR] pair.request rejected: reason=code_mismatch request_id=%s\n", req_id);
        return DESK_SDK_PAIRING_REJECTED;
    }

    if (!desk_sdk_is_lower_hex_32(req_id) || !desk_sdk_is_lower_hex_32(daemon_id) ||
        websocket_port < 1 || websocket_port > 65535 ||
        !(strcmp(target_mode, "python_sdk") == 0 || strcmp(target_mode, "desktop_link") == 0)) {
        printf("[SDK-PAIR] pair.request rejected: reason=malformed_metadata request_id=%s\n", req_id);
        return DESK_SDK_PAIRING_REJECTED;
    }

    /* Validation passed: only now generate/issue a session token. */
    if (s_active_pairing_token[0] == '\0') {
        generate_dynamic_token(s_active_pairing_token, sizeof(s_active_pairing_token));
    }

    char proto[64] = "";
    json_parse_str(udp_in, "protocol", proto, sizeof(proto));

    /* Strict official watcher-lan-pairing protocol check per PairAccept contract */
    int reply_len;
    if (strcmp(proto, "watcher-lan-pairing") == 0) {
        reply_len = snprintf(udp_out, max_out_len,
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
        reply_len = snprintf(udp_out, max_out_len,
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

    if (reply_len < 0 || (size_t)reply_len >= max_out_len) {
        udp_out[0] = '\0';
        printf("[SDK-PAIR] pair.request rejected: reason=reply_too_small request_id=%s\n", req_id);
        return DESK_SDK_PAIRING_REJECTED;
    }

    /* A new accepted pairing replaces the identity/target bound to the old
     * socket, so tear it down before mutating those fields. */
    desk_sdk_ws_client_close();
    s_paired = true;
    memcpy(s_pair_request_id, req_id, sizeof(s_pair_request_id));
    memcpy(s_pair_daemon_instance_id, daemon_id, sizeof(s_pair_daemon_instance_id));
    snprintf(s_pair_target_mode, sizeof(s_pair_target_mode), "%s", target_mode);
    s_pair_websocket_port = (uint16_t)websocket_port;
    if (notify_pairing_consumed)
        *notify_pairing_consumed = desk_sdk_pairing_code_clear_locked();
    else
        (void)desk_sdk_pairing_code_clear_locked();
    desk_sdk_ws_client_close();
    printf("[SDK-PAIR] pair.accept issued: reason=validated request_id=%s\n", req_id);

    return DESK_SDK_PAIRING_OK;
}

int desk_sdk_bridge_process_pairing_udp(const char *udp_in, char *udp_out, size_t max_out_len)
{
    bool notify_pairing_consumed = false;
    pthread_mutex_lock(&s_pairing_code_mutex);
    int rc = desk_sdk_bridge_process_pairing_udp_locked(udp_in, udp_out, max_out_len,
                                                        &notify_pairing_consumed);
    pthread_mutex_unlock(&s_pairing_code_mutex);
    desk_sdk_pairing_ui_notify_consumed(notify_pairing_consumed);
    return rc;
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

    /* Live camera controls are intentionally unavailable on the legacy TCP
     * command path. They require an authenticated, identity-bound WebSocket
     * session; the current protocol exposes no trusted client/session binding. */
    if (strcmp(msg_type, "ctrl.camera.stream.start") == 0 ||
        strcmp(msg_type, "ctrl.camera.stream.stop") == 0 ||
        strcmp(msg_type, "ctrl.camera.clock.ping") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.nack\",%s\"code\":403,\"error\":{"
                 "\"code\":\"WEBSOCKET_SESSION_REQUIRED\","
                 "\"message\":\"Camera stream controls require the authenticated WebSocket session\"}}",
                 rid);
        return 0;
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

        /* Do not negotiate audio_uplink until capture, Opus encoding, WSPK
         * uplink and session teardown are implemented end to end. */
        if (strcmp(role, "hardware") == 0) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"data\":{"
                     "\"type\":\"sys.client.hello\","
                     "\"role\":\"hardware\","
                     "\"session_state\":\"connected\"}}");
            return 0;
        }

        /* Standard SDK / desktop hello response */
        if (desk_sdk_camera_stream_available()) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"data\":{"
                     "\"role\":\"device\",\"protocol_version\":\"1.0\",\"capabilities\":["
                     "\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]}}",
                     DESK_SDK_CAP_MOTION, DESK_SDK_CAP_CAMERA, DESK_SDK_CAP_EXPRESSION,
                     DESK_SDK_CAP_EXPRESSION_ALT, DESK_SDK_CAP_MCP_TOOLS,
                     DESK_SDK_CAP_INPUTS, DESK_SDK_CAP_CAMERA_STREAM);
        } else {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",\"code\":0,\"data\":{"
                     "\"role\":\"device\",\"protocol_version\":\"1.0\",\"capabilities\":["
                     "\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]}}",
                     DESK_SDK_CAP_MOTION, DESK_SDK_CAP_CAMERA, DESK_SDK_CAP_EXPRESSION,
                     DESK_SDK_CAP_EXPRESSION_ALT, DESK_SDK_CAP_MCP_TOOLS, DESK_SDK_CAP_INPUTS);
        }
        return 0;
    }

    /* System SDK ready get: sys.sdk.ready.get */
    else if (strcmp(msg_type, "sys.sdk.ready.get") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        if (desk_sdk_camera_stream_available()) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{"
                     "\"role\":\"device\",\"protocol_version\":\"1.0\",\"capabilities\":["
                     "\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]}}",
                     rid, DESK_SDK_CAP_MOTION, DESK_SDK_CAP_CAMERA, DESK_SDK_CAP_EXPRESSION,
                     DESK_SDK_CAP_EXPRESSION_ALT, DESK_SDK_CAP_MCP_TOOLS,
                     DESK_SDK_CAP_INPUTS, DESK_SDK_CAP_CAMERA_STREAM);
        } else {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{"
                     "\"role\":\"device\",\"protocol_version\":\"1.0\",\"capabilities\":["
                     "\"%s\",\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]}}",
                     rid, DESK_SDK_CAP_MOTION, DESK_SDK_CAP_CAMERA, DESK_SDK_CAP_EXPRESSION,
                     DESK_SDK_CAP_EXPRESSION_ALT, DESK_SDK_CAP_MCP_TOOLS, DESK_SDK_CAP_INPUTS);
        }
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

    /* Do not mint a synthetic job: this firmware has no action playback engine. */
    else if (strcmp(msg_type, "ctrl.motion.action.play") == 0 || strcmp(msg_type, "motion.action.play") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.nack\",%s\"code\":501,\"error\":{"
                 "\"code\":\"UNSUPPORTED_OPERATION\","
                 "\"message\":\"Motion action playback is not implemented on this device\"}}",
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

    /* CR-022 streaming remains unavailable until A1 provides a real ESP
     * producer/readiness hook and the paired-session lease-owner interface is
     * available here. Never expose the host mock pump as a device stream. */
    else if (strcmp(msg_type, "ctrl.camera.stream.start") == 0 ||
             strcmp(msg_type, "ctrl.camera.stream.stop") == 0 ||
             strcmp(msg_type, "ctrl.camera.clock.ping") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.nack\",%s\"code\":503,\"error\":{"
                 "\"code\":\"CAMERA_STREAM_UNAVAILABLE\","
                 "\"message\":\"A real camera stream producer and paired lease-owner gate are not available\"}}",
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
        int idle = 0;
        if (!atomic_compare_exchange_strong(&s_camera_exclusive_state, &idle, 1)) {
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":409,\"error\":{"
                     "\"code\":\"RESOURCE_BUSY\",\"message\":\"Camera is owned by an active capture or stream\"}}",
                     rid);
            return 0;
        }
        size_t max_jpeg = 65536;
        uint8_t *jpeg_buf = (uint8_t *)desk_psram_malloc(max_jpeg);
        if (!jpeg_buf) {
            atomic_store(&s_camera_exclusive_state, 0);
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
            atomic_store(&s_camera_exclusive_state, 0);
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
        atomic_store(&s_camera_exclusive_state, 0);
        return 0;
    }

    /* Audio transport is unsupported until both directions have real media
     * paths and the capture owner can be negotiated without stopping TClaw. */
    else if (strcmp(msg_type, "ctrl.audio.stream.begin") == 0 ||
             strcmp(msg_type, "ctrl.audio.stop") == 0 ||
             strcmp(msg_type, "ctrl.microphone.open") == 0 ||
             strcmp(msg_type, "ctrl.microphone.close") == 0) {
        char rid_scratch[128];
        const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.nack\",%s\"code\":501,\"error\":{"
                 "\"code\":\"UNSUPPORTED_OPERATION\","
                 "\"message\":\"SDK audio transport is not implemented on this device\"}}",
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

static bool desk_sdk_log_pairing_is_complete(void)
{
    bool complete;
    pthread_mutex_lock(&s_pairing_code_mutex);
    complete = s_paired && desk_sdk_is_lower_hex_32(s_pair_request_id) &&
               desk_sdk_is_lower_hex_32(s_pair_daemon_instance_id) &&
               desk_sdk_is_lower_hex_64(s_active_pairing_token) &&
               (strcmp(s_pair_target_mode, "python_sdk") == 0 ||
                strcmp(s_pair_target_mode, "desktop_link") == 0) &&
               s_pair_host[0] != '\0' && s_pair_websocket_port != 0;
    pthread_mutex_unlock(&s_pairing_code_mutex);
    return complete;
}

/* Camera ownership is scoped to the one paired Runtime connection. A valid
 * pairing token plus the WebSocket HardwareHello ACK latch is the identity
 * boundary; host/port are connection setup details, not additional auth. */
static bool desk_sdk_camera_ws_session_is_paired(void)
{
    pthread_mutex_lock(&s_pairing_code_mutex);
    bool paired = s_paired && desk_sdk_is_lower_hex_32(s_pair_request_id) &&
                  desk_sdk_is_lower_hex_32(s_pair_daemon_instance_id) &&
                  desk_sdk_is_lower_hex_64(s_active_pairing_token);
    pthread_mutex_unlock(&s_pairing_code_mutex);
    return paired && desk_sdk_ws_client_is_authenticated();
}

void desk_sdk_bridge_camera_session_closed(void)
{
    pthread_mutex_lock(&s_ws_camera_owner_mutex);
    s_ws_camera_owner_id[0] = '\0';
    pthread_mutex_unlock(&s_ws_camera_owner_mutex);
    (void)desk_sdk_camera_stream_stop_internal(false);
}

static bool desk_sdk_camera_stream_available(void)
{
#ifdef DESK_SDK_TESTING
    if (s_test_camera_stream_available) return true;
#endif
    return desk_hardware_camera_stream_is_available();
}

static bool desk_sdk_camera_jpeg_dimensions(const uint8_t *jpeg, size_t len,
                                            uint16_t *width, uint16_t *height)
{
    if (!jpeg || !width || !height || len < 4 || jpeg[0] != 0xFF || jpeg[1] != 0xD8 ||
        jpeg[len - 2] != 0xFF || jpeg[len - 1] != 0xD9) return false;
    size_t pos = 2;
    while (pos + 4 <= len - 2) {
        if (jpeg[pos++] != 0xFF) return false;
        while (pos < len && jpeg[pos] == 0xFF) ++pos;
        if (pos >= len) return false;
        uint8_t marker = jpeg[pos++];
        if (marker == 0xD9 || marker == 0xDA) break;
        if (marker == 0x01 || (marker >= 0xD0 && marker <= 0xD7)) continue;
        if (pos + 2 > len) return false;
        size_t segment_len = ((size_t)jpeg[pos] << 8) | jpeg[pos + 1];
        if (segment_len < 2 || segment_len > len - pos) return false;
        bool sof = marker >= 0xC0 && marker <= 0xCF && marker != 0xC4 &&
                   marker != 0xC8 && marker != 0xCC;
        if (sof) {
            if (segment_len < 8) return false;
            *height = (uint16_t)(((uint16_t)jpeg[pos + 3] << 8) | jpeg[pos + 4]);
            *width = (uint16_t)(((uint16_t)jpeg[pos + 5] << 8) | jpeg[pos + 6]);
            return *width && *height;
        }
        pos += segment_len;
    }
    return false;
}

static void desk_sdk_camera_stream_finalize(void)
{
    uint8_t *slot = NULL;
    pthread_mutex_lock(&s_camera_stream.mutex);
    slot = s_camera_stream.frame_slot;
    s_camera_stream.frame_slot = NULL;
    s_camera_stream.frame_len = 0;
    s_camera_stream.active = false;
    s_camera_stream.starting = false;
    s_camera_stream.stop_requested = false;
    s_camera_stream.ready = false;
    s_camera_stream.ack_sent = false;
    s_camera_stream.producer_failed = false;
    s_camera_stream.thread_started = false;
    s_camera_stream.stream_id = 0;
    s_camera_stream.client_id[0] = '\0';
    s_camera_stream.session_id[0] = '\0';
    s_camera_stream.external_join = false;
    pthread_cond_broadcast(&s_camera_stream.changed);
    pthread_mutex_unlock(&s_camera_stream.mutex);
    if (slot) desk_psram_free(slot);
    atomic_store(&s_camera_exclusive_state, 0);
}

static void desk_sdk_camera_stream_frame_cb(const uint8_t *jpeg, size_t jpeg_len,
                                            uint32_t timestamp_ms, void *user_data)
{
    (void)user_data;
    uint16_t width = 0, height = 0;
    pthread_mutex_lock(&s_camera_stream.mutex);
    bool acceptable = (s_camera_stream.active || s_camera_stream.starting) &&
                      !s_camera_stream.stop_requested && s_camera_stream.frame_slot &&
                      jpeg_len <= DESK_CAMERA_STREAM_MAX_JPEG &&
                      desk_sdk_camera_jpeg_dimensions(jpeg, jpeg_len, &width, &height) &&
                      width == 640 && height == 480 &&
                      (!s_camera_stream.last_timestamp_ms ||
                       (int32_t)(timestamp_ms - s_camera_stream.last_timestamp_ms) > 0);
    if (acceptable) {
        if (s_camera_stream.ready && !s_camera_stream.ack_sent)
            ++s_camera_stream.source_drop_count;
        uint32_t sequence = s_camera_stream.sequence + 1U;
        size_t wjpg_len = 0;
        int rc = desk_sdk_bridge_pack_wjpg_v1(sequence, timestamp_ms, jpeg, jpeg_len,
            s_camera_stream.frame_slot + DESK_WSPK_HEADER_SIZE,
            DESK_WSPK_MAX_PAYLOAD_SIZE - DESK_WSPK_HEADER_SIZE, &wjpg_len);
        if (rc == 0) rc = desk_wspk_pack_header(DESK_WSPK_TYPE_IMAGE, 0,
            s_camera_stream.stream_id, sequence, (uint32_t)wjpg_len,
            s_camera_stream.frame_slot, DESK_WSPK_HEADER_SIZE);
        if (rc == 0) {
            s_camera_stream.sequence = sequence;
            s_camera_stream.last_timestamp_ms = timestamp_ms;
            s_camera_stream.frame_len = DESK_WSPK_HEADER_SIZE + wjpg_len;
            s_camera_stream.ready = true;
            pthread_cond_broadcast(&s_camera_stream.changed);
        } else ++s_camera_stream.source_drop_count;
    } else if (s_camera_stream.active || s_camera_stream.starting) {
        ++s_camera_stream.source_drop_count;
    }
    pthread_mutex_unlock(&s_camera_stream.mutex);
}

static int desk_sdk_camera_stream_send_binary(const uint8_t *frame, size_t len)
{
#ifdef DESK_SDK_TESTING
    if (s_test_camera_binary_send_hook)
        return s_test_camera_binary_send_hook((const char *)frame, len);
#endif
    return desk_sdk_ws_client_send_binary(frame, len);
}

static int desk_sdk_camera_stream_stop_internal(bool from_worker)
{
    pthread_mutex_lock(&s_camera_stream.mutex);
    while (s_camera_stream.cleanup_in_progress)
        pthread_cond_wait(&s_camera_stream.changed, &s_camera_stream.mutex);
    if (!s_camera_stream.frame_slot && !s_camera_stream.thread_started) {
        pthread_mutex_unlock(&s_camera_stream.mutex);
        return DESK_HW_OK;
    }
    s_camera_stream.stop_requested = true;
    s_camera_stream.active = false;
    s_camera_stream.starting = false;
    bool had_thread = s_camera_stream.thread_started;
    bool self = had_thread && pthread_equal(pthread_self(), s_camera_stream.thread);
    bool join = s_camera_stream.thread_started && !from_worker && !self;
    pthread_t thread = s_camera_stream.thread;
    if (join) s_camera_stream.external_join = true;
    pthread_cond_broadcast(&s_camera_stream.changed);
    pthread_mutex_unlock(&s_camera_stream.mutex);

    if (join) {
        (void)pthread_join(thread, NULL);
        int stop_rc = desk_hardware_camera_stream_stop();
        desk_sdk_camera_stream_finalize();
        return stop_rc;
    }
    if (self) return DESK_HW_OK;
    if (!had_thread) {
        int stop_rc = desk_hardware_camera_stream_stop();
        desk_sdk_camera_stream_finalize();
        return stop_rc;
    }
    return DESK_HW_OK;
}

static void *desk_sdk_camera_stream_worker(void *unused)
{
    (void)unused;
    bool failed = false;
    for (;;) {
        pthread_mutex_lock(&s_camera_stream.mutex);
        bool stop = s_camera_stream.stop_requested;
        char client_id[sizeof(s_camera_stream.client_id)];
        memcpy(client_id, s_camera_stream.client_id, sizeof(client_id));
        uint64_t next_ms = s_camera_stream.next_capture_ms;
        pthread_mutex_unlock(&s_camera_stream.mutex);
        if (stop) break;
        if (!desk_sdk_camera_ws_session_is_paired() || !desk_arbiter_camera_lease_matches(client_id)) {
            pthread_mutex_lock(&s_ws_camera_owner_mutex);
            if (strcmp(s_ws_camera_owner_id, client_id) == 0) s_ws_camera_owner_id[0] = '\0';
            pthread_mutex_unlock(&s_ws_camera_owner_mutex);
            failed = true;
            break;
        }

        uint64_t now_ms = desk_sdk_camera_worker_now_ms();
        if (!now_ms) { failed = true; break; }
        if (!next_ms || now_ms >= next_ms) {
            int pump_rc = desk_hardware_camera_stream_pump();
            pthread_mutex_lock(&s_camera_stream.mutex);
            now_ms = desk_sdk_camera_worker_now_ms();
            s_camera_stream.next_capture_ms = now_ms + DESK_CAMERA_STREAM_PERIOD_MS;
            bool may_send = pump_rc == DESK_HW_OK && s_camera_stream.active &&
                            s_camera_stream.ack_sent && s_camera_stream.ready &&
                            !s_camera_stream.stop_requested && s_camera_stream.frame_slot;
            uint8_t *frame = s_camera_stream.frame_slot;
            size_t frame_len = s_camera_stream.frame_len;
            pthread_mutex_unlock(&s_camera_stream.mutex);
            if (may_send) {
                int send_rc = desk_sdk_camera_stream_send_binary(frame, frame_len);
                if (send_rc == -3) {
                    pthread_mutex_lock(&s_camera_stream.mutex);
                    ++s_camera_stream.transport_drop_count;
                    pthread_mutex_unlock(&s_camera_stream.mutex);
                } else if (send_rc != 0) { failed = true; break; }
            }
            if (pump_rc != DESK_HW_OK && pump_rc != DESK_HW_ERR_BUSY) {
                failed = true;
                break;
            }
        }
        struct timespec pause = { .tv_sec = 0, .tv_nsec = 5000000L };
        nanosleep(&pause, NULL);
    }
    pthread_mutex_lock(&s_camera_stream.mutex);
    s_camera_stream.stop_requested = true;
    s_camera_stream.producer_failed = failed;
    bool external_join = s_camera_stream.external_join;
    if (!external_join) s_camera_stream.cleanup_in_progress = true;
    pthread_cond_broadcast(&s_camera_stream.changed);
    pthread_mutex_unlock(&s_camera_stream.mutex);
    if (!external_join) {
        (void)desk_hardware_camera_stream_stop();
        (void)pthread_detach(pthread_self());
        desk_sdk_camera_stream_finalize();
        pthread_mutex_lock(&s_camera_stream.mutex);
        s_camera_stream.cleanup_in_progress = false;
        pthread_cond_broadcast(&s_camera_stream.changed);
        pthread_mutex_unlock(&s_camera_stream.mutex);
    }
    return NULL;
}

void desk_sdk_bridge_camera_start_ack_sent(void)
{
    pthread_mutex_lock(&s_camera_stream.mutex);
    if (s_camera_stream.active && s_camera_stream.ready)
        s_camera_stream.ack_sent = true;
    pthread_cond_broadcast(&s_camera_stream.changed);
    pthread_mutex_unlock(&s_camera_stream.mutex);
}

bool desk_sdk_bridge_camera_stream_is_active(void)
{
    pthread_mutex_lock(&s_camera_stream.mutex);
    bool active = s_camera_stream.active || s_camera_stream.starting;
    pthread_mutex_unlock(&s_camera_stream.mutex);
    return active;
}

static int desk_sdk_camera_stream_reply(char *out, size_t cap, const char *json,
                                        const char *type, const char *command_id,
                                        const char *error, int code,
                                        uint16_t stream_id, uint32_t device_ms,
                                        bool ack)
{
    char rid_scratch[128];
    const char *rid = desk_sdk_request_id_fragment(json, rid_scratch, sizeof(rid_scratch));
    int n;
    if (ack) {
        n = snprintf(out, cap,
            "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"type\":\"%s\",\"command_id\":\"%s\","
            "\"stream_id\":%u,\"device_monotonic_ms\":%u}}",
            rid, type, command_id, (unsigned)stream_id, (unsigned)device_ms);
    } else {
        n = snprintf(out, cap,
            "{\"type\":\"sys.nack\",%s\"code\":%d,\"error\":{\"code\":\"%s\","
            "\"message\":\"Camera stream control rejected\"},\"data\":{\"command_id\":\"%s\"}}",
            rid, code, error, command_id ? command_id : "");
    }
    return n >= 0 && (size_t)n < cap ? 0 : -1;
}

static int desk_sdk_camera_stream_command(const char *json, char *out, size_t cap,
                                          const char *type)
{
    char command_id[40] = "", client_id[32] = "", session_id[65] = "";
    char data[512] = "";
    (void)json_parse_top_level_str(json, "command_id", command_id, sizeof(command_id));
    (void)json_parse_top_level_str(json, "client_id", client_id, sizeof(client_id));
    (void)json_parse_top_level_str(json, "session_id", session_id, sizeof(session_id));
    if (!desk_sdk_log_command_id_valid(command_id) || client_id[0] == '\0' || session_id[0] == '\0' ||
        !json_extract_subobject(json, "data", data, sizeof(data)))
        return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                            "INVALID_ARGUMENT", 400, 0, 0, false);

    if (!desk_sdk_camera_ws_session_is_paired())
        return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                            "AUTH_REQUIRED", 403, 0, 0, false);
    pthread_mutex_lock(&s_ws_camera_owner_mutex);
    bool bound_owner = strcmp(client_id, s_ws_camera_owner_id) == 0;
    pthread_mutex_unlock(&s_ws_camera_owner_mutex);
    if (!bound_owner || !desk_arbiter_camera_lease_matches(client_id))
        return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                            "NO_ACTIVE_LEASE", 403, 0, 0, false);

    if (strcmp(type, "ctrl.camera.stream.start") == 0) {
        static const char *const keys[] = { "width", "height", "target_fps" };
        if (!desk_log_object_keys_allowed(data, keys, 3) ||
            json_parse_int(data, "width", -1) != 640 ||
            json_parse_int(data, "height", -1) != 480 ||
            json_parse_int(data, "target_fps", -1) != 15)
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                "UNSUPPORTED_FORMAT", 400, 0, 0, false);
        if (!desk_sdk_camera_stream_available())
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                "CAMERA_STREAM_UNAVAILABLE", 503, 0, 0, false);
        pthread_mutex_lock(&s_camera_stream.mutex);
        if (s_camera_stream.active && strcmp(client_id, s_camera_stream.client_id) == 0 &&
            strcmp(session_id, s_camera_stream.session_id) == 0) {
            uint16_t sid = s_camera_stream.stream_id;
            uint64_t now = 0;
            (void)desk_sdk_monotonic_now_ms(&now);
            pthread_mutex_unlock(&s_camera_stream.mutex);
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                NULL, 0, sid, (uint32_t)now, true);
        }
        if (s_camera_stream.active || s_camera_stream.starting || s_camera_stream.thread_started) {
            pthread_mutex_unlock(&s_camera_stream.mutex);
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                "RESOURCE_BUSY", 409, 0, 0, false);
        }
        int idle = 0;
        if (!atomic_compare_exchange_strong(&s_camera_exclusive_state, &idle, 2)) {
            pthread_mutex_unlock(&s_camera_stream.mutex);
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                "RESOURCE_BUSY", 409, 0, 0, false);
        }
        uint16_t stream_id = ++s_camera_stream_id_seed;
        if (!stream_id) stream_id = ++s_camera_stream_id_seed;
        size_t slot_len = DESK_WSPK_HEADER_SIZE + DESK_WSPK_MAX_PAYLOAD_SIZE;
        uint8_t *slot = (uint8_t *)desk_psram_malloc(slot_len);
        if (!slot) {
            atomic_store(&s_camera_exclusive_state, 0);
            pthread_mutex_unlock(&s_camera_stream.mutex);
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                "OUT_OF_MEMORY", 500, 0, 0, false);
        }
        s_camera_stream.frame_slot = slot;
        s_camera_stream.frame_len = 0;
        s_camera_stream.stream_id = stream_id;
        s_camera_stream.sequence = 0;
        s_camera_stream.last_timestamp_ms = 0;
        s_camera_stream.ready = false;
        s_camera_stream.ack_sent = false;
        s_camera_stream.producer_failed = false;
        s_camera_stream.source_drop_count = 0;
        s_camera_stream.transport_drop_count = 0;
        s_camera_stream.active = false;
        s_camera_stream.starting = true;
        s_camera_stream.stop_requested = false;
        s_camera_stream.external_join = false;
        snprintf(s_camera_stream.client_id, sizeof(s_camera_stream.client_id), "%s", client_id);
        snprintf(s_camera_stream.session_id, sizeof(s_camera_stream.session_id), "%s", session_id);
        pthread_mutex_unlock(&s_camera_stream.mutex);

        int start_rc = desk_hardware_camera_stream_start(client_id, DESK_CAMERA_STREAM_FPS,
                                                          desk_sdk_camera_stream_frame_cb, NULL);
        if (start_rc != DESK_HW_OK) {
            desk_sdk_camera_stream_finalize();
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                "CAMERA_START_FAILED", 503, 0, 0, false);
        }
        pthread_mutex_lock(&s_camera_stream.mutex);
        s_camera_stream.active = true;
        s_camera_stream.starting = false;
        s_camera_stream.thread_started = true;
        if (pthread_create(&s_camera_stream.thread, NULL, desk_sdk_camera_stream_worker, NULL) != 0) {
            s_camera_stream.thread_started = false;
            pthread_mutex_unlock(&s_camera_stream.mutex);
            (void)desk_hardware_camera_stream_stop();
            desk_sdk_camera_stream_finalize();
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                "CAMERA_START_FAILED", 503, 0, 0, false);
        }
        struct timespec until;
        clock_gettime(CLOCK_REALTIME, &until);
        until.tv_nsec += (long)DESK_CAMERA_STREAM_READY_TIMEOUT_MS * 1000000L;
        until.tv_sec += until.tv_nsec / 1000000000L;
        until.tv_nsec %= 1000000000L;
        int wait_rc = 0;
        while (!s_camera_stream.ready && !s_camera_stream.stop_requested &&
               !s_camera_stream.producer_failed && wait_rc == 0)
            wait_rc = pthread_cond_timedwait(&s_camera_stream.changed, &s_camera_stream.mutex, &until);
        bool ready = s_camera_stream.ready && !s_camera_stream.stop_requested &&
                     !s_camera_stream.producer_failed;
        pthread_mutex_unlock(&s_camera_stream.mutex);
        if (!ready) {
            (void)desk_sdk_camera_stream_stop_internal(false);
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                "CAMERA_NOT_READY", 503, 0, 0, false);
        }
        uint64_t now = 0;
        (void)desk_sdk_monotonic_now_ms(&now);
        return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                            NULL, 0, stream_id, (uint32_t)now, true);
    }

    if (strcmp(type, "ctrl.camera.stream.stop") == 0) {
        int32_t stream_id = json_parse_int(data, "stream_id", -1);
        pthread_mutex_lock(&s_camera_stream.mutex);
        bool match = stream_id > 0 && s_camera_stream.active &&
                     (uint16_t)stream_id == s_camera_stream.stream_id &&
                     strcmp(client_id, s_camera_stream.client_id) == 0 &&
                     strcmp(session_id, s_camera_stream.session_id) == 0;
        bool repeated = stream_id > 0 && (uint16_t)stream_id == s_camera_stream.last_stopped_stream_id &&
                        strcmp(client_id, s_camera_stream.last_client_id) == 0 &&
                        strcmp(session_id, s_camera_stream.last_session_id) == 0;
        pthread_mutex_unlock(&s_camera_stream.mutex);
        if (repeated) return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                         NULL, 0, (uint16_t)stream_id, 0, true);
        if (!match) return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                        "INVALID_SESSION", 404, 0, 0, false);
        if (desk_sdk_camera_stream_stop_internal(false) != DESK_HW_OK)
            return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                                "CAMERA_STOP_FAILED", 500, 0, 0, false);
        pthread_mutex_lock(&s_camera_stream.mutex);
        s_camera_stream.last_stopped_stream_id = (uint16_t)stream_id;
        snprintf(s_camera_stream.last_client_id, sizeof(s_camera_stream.last_client_id), "%s", client_id);
        snprintf(s_camera_stream.last_session_id, sizeof(s_camera_stream.last_session_id), "%s", session_id);
        pthread_mutex_unlock(&s_camera_stream.mutex);
        return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                            NULL, 0, (uint16_t)stream_id, 0, true);
    }

    static const char *const ping_keys[] = { "ping_id", "host_send_monotonic_ms" };
    if (!desk_log_object_keys_allowed(data, ping_keys, 2))
        return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                            "INVALID_ARGUMENT", 400, 0, 0, false);
    pthread_mutex_lock(&s_camera_stream.mutex);
    bool active = s_camera_stream.active && strcmp(client_id, s_camera_stream.client_id) == 0 &&
                  strcmp(session_id, s_camera_stream.session_id) == 0;
    pthread_mutex_unlock(&s_camera_stream.mutex);
    int32_t ping_id = json_parse_int(data, "ping_id", -1);
    int32_t host_send = json_parse_int(data, "host_send_monotonic_ms", -1);
    if (!active || ping_id < 0 || host_send < 0)
        return desk_sdk_camera_stream_reply(out, cap, json, type, command_id,
                                            active ? "INVALID_ARGUMENT" : "INVALID_SESSION", 400, 0, 0, false);
    uint64_t receive_ms = 0, send_ms = 0;
    (void)desk_sdk_monotonic_now_ms(&receive_ms);
    (void)desk_sdk_monotonic_now_ms(&send_ms);
    char rid_scratch[128];
    const char *rid = desk_sdk_request_id_fragment(json, rid_scratch, sizeof(rid_scratch));
    int n = snprintf(out, cap,
        "{\"type\":\"sys.ack\",%s\"code\":0,\"data\":{\"type\":\"%s\",\"command_id\":\"%s\","
        "\"ping_id\":%d,\"host_send_monotonic_ms\":%u,\"device_receive_monotonic_ms\":%u,\"device_send_monotonic_ms\":%u}}",
        rid, type, command_id, (int)ping_id, (uint32_t)host_send,
        (uint32_t)receive_ms, (uint32_t)send_ms);
    return n >= 0 && (size_t)n < cap ? 0 : -1;
}

static int desk_sdk_log_nack(char *out, size_t cap, const char *type,
                             const char *command_id, const char *error, int code)
{
    int n = snprintf(out, cap,
        "{\"type\":\"sys.nack\",\"code\":%d,\"data\":{\"type\":\"%s\",\"command_id\":\"%s\",\"error\":\"%s\"}}",
        code, type, command_id, error);
    return n >= 0 && (size_t)n < cap ? 0 : -1;
}

static bool desk_sdk_log_command_id_valid(const char *id)
{
    if (!id || strlen(id) != 32) return false;
    for (size_t i = 0; i < 32; ++i) {
        if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f'))) return false;
    }
    return true;
}

typedef struct { const char *p; unsigned depth; } desk_log_json_parser_t;

static void desk_log_json_ws(desk_log_json_parser_t *p)
{
    while (*p->p == ' ' || *p->p == '\t' || *p->p == '\r' || *p->p == '\n') ++p->p;
}

static bool desk_log_json_string(desk_log_json_parser_t *p, const char **begin, const char **end)
{
    if (*p->p != '"') return false;
    const char *start = p->p++;
    while (*p->p) {
        unsigned char c = (unsigned char)*p->p++;
        if (c == '"') { if (begin) *begin = start; if (end) *end = p->p; return true; }
        if (c < 0x20) return false;
        if (c == '\\') {
            c = (unsigned char)*p->p++;
            if (c == 'u') {
                for (int i = 0; i < 4; ++i) {
                    char h = *p->p++;
                    if (!((h >= '0' && h <= '9') || (h >= 'a' && h <= 'f') || (h >= 'A' && h <= 'F'))) return false;
                }
            } else if (c != '"' && c != '\\' && c != '/' && c != 'b' && c != 'f' && c != 'n' && c != 'r' && c != 't') return false;
        }
    }
    return false;
}

static bool desk_log_json_value(desk_log_json_parser_t *p)
{
    desk_log_json_ws(p);
    if (++p->depth > 16) return false;
    bool ok = false;
    if (*p->p == '"') ok = desk_log_json_string(p, NULL, NULL);
    else if (*p->p == '{') {
        ++p->p; desk_log_json_ws(p);
        if (*p->p == '}') { ++p->p; ok = true; }
        else {
            ok = true;
            for (;;) {
                if (!desk_log_json_string(p, NULL, NULL)) { ok = false; break; }
                desk_log_json_ws(p);
                if (*p->p++ != ':') { ok = false; break; }
                if (!desk_log_json_value(p)) { ok = false; break; }
                desk_log_json_ws(p);
                if (*p->p == '}') { ++p->p; break; }
                if (*p->p++ != ',') { ok = false; break; }
                desk_log_json_ws(p);
            }
        }
    } else if (*p->p == '[') {
        ++p->p; desk_log_json_ws(p);
        if (*p->p == ']') { ++p->p; ok = true; }
        else {
            ok = true;
            for (;;) {
                if (!desk_log_json_value(p)) { ok = false; break; }
                desk_log_json_ws(p);
                if (*p->p == ']') { ++p->p; break; }
                if (*p->p++ != ',') { ok = false; break; }
                desk_log_json_ws(p);
            }
        }
    } else if (*p->p == 't' && strncmp(p->p, "true", 4) == 0) { p->p += 4; ok = true; }
    else if (*p->p == 'f' && strncmp(p->p, "false", 5) == 0) { p->p += 5; ok = true; }
    else if (*p->p == 'n' && strncmp(p->p, "null", 4) == 0) { p->p += 4; ok = true; }
    else {
        const char *n = p->p;
        if (*n == '-') ++n;
        if (*n == '0') ++n;
        else if (*n >= '1' && *n <= '9') { do { ++n; } while (*n >= '0' && *n <= '9'); }
        else n = p->p;
        if (n != p->p) {
            if (*n == '.') { ++n; if (*n < '0' || *n > '9') n = p->p; else do { ++n; } while (*n >= '0' && *n <= '9'); }
            if (n != p->p && (*n == 'e' || *n == 'E')) {
                ++n; if (*n == '+' || *n == '-') ++n;
                if (*n < '0' || *n > '9') n = p->p; else do { ++n; } while (*n >= '0' && *n <= '9');
            }
            if (n != p->p) { p->p = n; ok = true; }
        }
    }
    --p->depth;
    return ok;
}

static bool desk_log_json_valid(const char *json)
{
    if (!json) return false;
    desk_log_json_parser_t p = { .p = json, .depth = 0 };
    if (!desk_log_json_value(&p)) return false;
    desk_log_json_ws(&p);
    return *p.p == '\0';
}

static bool desk_log_plain_key(const char *start, const char *end, const char *key)
{
    size_t len = strlen(key);
    return (size_t)(end - start) == len + 2 && start[0] == '"' &&
           memcmp(start + 1, key, len) == 0 && end[-1] == '"';
}

static unsigned desk_log_get_member(const char *object, const char *name, char *out, size_t cap)
{
    if (!object || !out || cap == 0) return 0;
    out[0] = '\0';
    desk_log_json_parser_t p = { .p = object, .depth = 0 };
    desk_log_json_ws(&p);
    if (*p.p++ != '{') return 0;
    desk_log_json_ws(&p);
    unsigned matches = 0;
    if (*p.p == '}') return 0;
    for (;;) {
        const char *ks, *ke;
        if (!desk_log_json_string(&p, &ks, &ke)) return 0;
        desk_log_json_ws(&p);
        if (*p.p++ != ':') return 0;
        desk_log_json_ws(&p);
        const char *vs = p.p;
        if (!desk_log_json_value(&p)) return 0;
        const char *ve = p.p;
        if (desk_log_plain_key(ks, ke, name)) {
            ++matches;
            size_t n = (size_t)(ve - vs);
            if (n >= cap) return 0;
            memcpy(out, vs, n); out[n] = '\0';
        }
        desk_log_json_ws(&p);
        if (*p.p == '}') break;
        if (*p.p++ != ',') return 0;
        desk_log_json_ws(&p);
    }
    return matches;
}

static bool desk_log_get_member_span(const char *object, const char *name,
                                     const char **value_start, size_t *value_len)
{
    if (!object || !name || !value_start || !value_len) return false;
    desk_log_json_parser_t p = { .p = object, .depth = 0 };
    desk_log_json_ws(&p);
    if (*p.p++ != '{') return false;
    desk_log_json_ws(&p);
    unsigned matches = 0;
    const char *found_start = NULL, *found_end = NULL;
    if (*p.p == '}') return false;
    for (;;) {
        const char *key_start, *key_end;
        if (!desk_log_json_string(&p, &key_start, &key_end)) return false;
        desk_log_json_ws(&p);
        if (*p.p++ != ':') return false;
        desk_log_json_ws(&p);
        const char *start = p.p;
        if (!desk_log_json_value(&p)) return false;
        const char *end = p.p;
        if (desk_log_plain_key(key_start, key_end, name)) {
            ++matches;
            found_start = start;
            found_end = end;
        }
        desk_log_json_ws(&p);
        if (*p.p == '}') break;
        if (*p.p++ != ',') return false;
        desk_log_json_ws(&p);
    }
    if (matches != 1) return false;
    *value_start = found_start;
    *value_len = (size_t)(found_end - found_start);
    return true;
}

static bool desk_log_object_keys_allowed(const char *object, const char *const *allowed, size_t allowed_count)
{
    desk_log_json_parser_t p = { .p = object, .depth = 0 };
    bool seen[8] = { false };
    if (allowed_count > sizeof(seen) / sizeof(seen[0])) return false;
    desk_log_json_ws(&p);
    if (*p.p++ != '{') return false;
    desk_log_json_ws(&p);
    if (*p.p == '}') return true;
    for (;;) {
        const char *ks, *ke;
        if (!desk_log_json_string(&p, &ks, &ke)) return false;
        size_t matched = allowed_count;
        for (size_t i = 0; i < allowed_count; ++i) {
            if (desk_log_plain_key(ks, ke, allowed[i])) { matched = i; break; }
        }
        if (matched == allowed_count || seen[matched]) return false;
        seen[matched] = true;
        desk_log_json_ws(&p);
        if (*p.p++ != ':') return false;
        if (!desk_log_json_value(&p)) return false;
        desk_log_json_ws(&p);
        if (*p.p == '}') return true;
        if (*p.p++ != ',') return false;
        desk_log_json_ws(&p);
    }
}

static bool desk_sdk_notification_append_utf8(uint32_t codepoint, char *out, size_t cap, size_t *used)
{
    uint8_t bytes[4];
    size_t count;
    if (codepoint == 0 || codepoint < 0x20 || codepoint == 0x7f || codepoint > 0x10ffff ||
        (codepoint >= 0xd800 && codepoint <= 0xdfff)) return false;
    if (codepoint < 0x80) { bytes[0] = (uint8_t)codepoint; count = 1; }
    else if (codepoint < 0x800) {
        bytes[0] = (uint8_t)(0xc0 | (codepoint >> 6));
        bytes[1] = (uint8_t)(0x80 | (codepoint & 0x3f)); count = 2;
    } else if (codepoint < 0x10000) {
        bytes[0] = (uint8_t)(0xe0 | (codepoint >> 12));
        bytes[1] = (uint8_t)(0x80 | ((codepoint >> 6) & 0x3f));
        bytes[2] = (uint8_t)(0x80 | (codepoint & 0x3f)); count = 3;
    } else {
        bytes[0] = (uint8_t)(0xf0 | (codepoint >> 18));
        bytes[1] = (uint8_t)(0x80 | ((codepoint >> 12) & 0x3f));
        bytes[2] = (uint8_t)(0x80 | ((codepoint >> 6) & 0x3f));
        bytes[3] = (uint8_t)(0x80 | (codepoint & 0x3f)); count = 4;
    }
    if (*used + count >= cap) return false;
    memcpy(out + *used, bytes, count);
    *used += count;
    return true;
}

static bool desk_sdk_notification_utf8_valid(const char *text)
{
    const unsigned char *p = (const unsigned char *)text;
    while (*p) {
        unsigned char lead = *p++;
        size_t continuation_count;
        unsigned char second_min = 0x80, second_max = 0xbf;
        if (lead < 0x80) {
            if (lead < 0x20 || lead == 0x7f) return false;
            continue;
        }
        if (lead >= 0xc2 && lead <= 0xdf) continuation_count = 1;
        else if (lead >= 0xe0 && lead <= 0xef) {
            continuation_count = 2;
            if (lead == 0xe0) second_min = 0xa0;
            if (lead == 0xed) second_max = 0x9f;
        } else if (lead >= 0xf0 && lead <= 0xf4) {
            continuation_count = 3;
            if (lead == 0xf0) second_min = 0x90;
            if (lead == 0xf4) second_max = 0x8f;
        } else return false;
        for (size_t i = 0; i < continuation_count; ++i) {
            unsigned char next = *p++;
            if (!next || (next & 0xc0) != 0x80 || (i == 0 && (next < second_min || next > second_max))) return false;
        }
    }
    return true;
}

static int desk_sdk_notification_hex4(const char *s, uint32_t *value)
{
    uint32_t result = 0;
    for (unsigned i = 0; i < 4; ++i) {
        char c = s[i];
        unsigned digit = c >= '0' && c <= '9' ? (unsigned)(c - '0') :
                         c >= 'a' && c <= 'f' ? (unsigned)(c - 'a' + 10) :
                         c >= 'A' && c <= 'F' ? (unsigned)(c - 'A' + 10) : 16u;
        if (digit > 15) return -1;
        result = (result << 4) | digit;
    }
    *value = result;
    return 0;
}

/* Decode one top-level JSON string member, including escaped Unicode and
 * surrogate pairs. Field capacity checks apply to decoded UTF-8 bytes. */
static bool desk_sdk_notification_get_string(const char *object, const char *key,
                                             char *out, size_t out_cap)
{
    char raw[1024];
    if (desk_log_get_member(object, key, raw, sizeof(raw)) != 1 || raw[0] != '"') return false;
    size_t raw_len = strlen(raw);
    if (raw_len < 2 || raw[raw_len - 1] != '"') return false;
    size_t used = 0;
    for (size_t i = 1; i + 1 < raw_len; ++i) {
        uint32_t cp = (unsigned char)raw[i];
        if (raw[i] == '\\') {
            if (++i + 1 >= raw_len) return false;
            char esc = raw[i];
            if (esc == '"' || esc == '\\' || esc == '/') cp = (unsigned char)esc;
            else if (esc == 'b') cp = '\b';
            else if (esc == 'f') cp = '\f';
            else if (esc == 'n') cp = '\n';
            else if (esc == 'r') cp = '\r';
            else if (esc == 't') cp = '\t';
            else if (esc == 'u') {
                if (i + 4 >= raw_len) return false;
                if (desk_sdk_notification_hex4(raw + i + 1, &cp) != 0) return false;
                i += 4;
                if (cp >= 0xd800 && cp <= 0xdbff) {
                    if (i + 6 >= raw_len || raw[i + 1] != '\\' || raw[i + 2] != 'u') return false;
                    uint32_t low;
                    if (desk_sdk_notification_hex4(raw + i + 3, &low) != 0 || low < 0xdc00 || low > 0xdfff) return false;
                    cp = 0x10000 + ((cp - 0xd800) << 10) + (low - 0xdc00);
                    i += 6;
                } else if (cp >= 0xdc00 && cp <= 0xdfff) return false;
            } else return false;
            if (cp < 0x80) {
                if (cp == 0 || cp < 0x20 || cp == 0x7f || used + 1 >= out_cap) return false;
                out[used++] = (char)cp;
            } else if (!desk_sdk_notification_append_utf8(cp, out, out_cap, &used)) return false;
        } else {
            if ((unsigned char)raw[i] < 0x20 || used + 1 >= out_cap) return false;
            out[used++] = raw[i];
        }
    }
    out[used] = '\0';
    return used > 0 && desk_sdk_notification_utf8_valid(out);
}

static bool desk_sdk_notification_parse_timestamp(const char *text, uint64_t *out_us)
{
    size_t len = strlen(text);
    if ((len != 20 && (len < 22 || len > 27)) || text[4] != '-' || text[7] != '-' ||
        text[10] != 'T' || text[13] != ':' || text[16] != ':' || text[len - 1] != 'Z') return false;
    const unsigned positions[] = {0,1,2,3,5,6,8,9,11,12,14,15,17,18};
    for (size_t i = 0; i < sizeof(positions) / sizeof(positions[0]); ++i)
        if (text[positions[i]] < '0' || text[positions[i]] > '9') return false;
    int year = (text[0]-'0')*1000 + (text[1]-'0')*100 + (text[2]-'0')*10 + text[3]-'0';
    int month = (text[5]-'0')*10 + text[6]-'0';
    int day = (text[8]-'0')*10 + text[9]-'0';
    int hour = (text[11]-'0')*10 + text[12]-'0';
    int minute = (text[14]-'0')*10 + text[15]-'0';
    int second = (text[17]-'0')*10 + text[18]-'0';
    if (year < 1970 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59) return false;
    static const unsigned month_days[] = {31,28,31,30,31,30,31,31,30,31,30,31};
    bool leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    if (day < 1 || (unsigned)day > month_days[month - 1] + (month == 2 && leap ? 1u : 0u)) return false;
    uint32_t micros = 0;
    if (len != 20) {
        if (text[19] != '.') return false;
        unsigned digits = 0;
        for (size_t i = 20; i < len - 1; ++i) {
            if (text[i] < '0' || text[i] > '9' || digits == 6) return false;
            micros = micros * 10u + (unsigned)(text[i] - '0');
            ++digits;
        }
        if (!digits) return false;
        while (digits++ < 6) micros *= 10u;
    }
    int y = year - (month <= 2);
    int era = (y >= 0 ? y : y - 399) / 400;
    unsigned yoe = (unsigned)(y - era * 400);
    int shifted_month = month + (month > 2 ? -3 : 9);
    unsigned doy = (153u * (unsigned)shifted_month + 2u) / 5u + (unsigned)day - 1u;
    unsigned doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
    int64_t days = (int64_t)era * 146097 + (int64_t)doe - 719468;
    if (days < 0) return false;
    *out_us = (((uint64_t)days * 86400u + (uint64_t)hour * 3600u +
                (uint64_t)minute * 60u + (uint64_t)second) * 1000000u) + micros;
    return true;
}

static int desk_sdk_notification_nack(char *out, size_t cap, const char *command_id,
                                      const char *error, int code)
{
    int n = snprintf(out, cap,
        "{\"type\":\"sys.nack\",\"code\":%d,\"data\":{\"type\":\"notification.deliver\",\"command_id\":\"%s\",\"error\":\"%s\"}}",
        code, command_id, error);
    return n >= 0 && (size_t)n < cap ? 0 : -1;
}

static int desk_sdk_notification_result_nack(char *out, size_t cap, const char *command_id, int rc)
{
    if (rc == DESK_NOTIFICATION_ERR_STORAGE_UNAVAILABLE)
        return desk_sdk_notification_nack(out, cap, command_id, "STORAGE_UNAVAILABLE", 503);
    if (rc == DESK_HW_ERR_BUSY)
        return desk_sdk_notification_nack(out, cap, command_id, "STORAGE_FULL", 507);
    if (rc == DESK_HW_ERR_IO)
        return desk_sdk_notification_nack(out, cap, command_id, "STORAGE_ERROR", 500);
    if (rc == DESK_HW_ERR_TIMEOUT)
        return desk_sdk_notification_nack(out, cap, command_id, "EXPIRED", 410);
    if (rc == DESK_HW_ERR_INVALID_ARG)
        return desk_sdk_notification_nack(out, cap, command_id, "INVALID_ARGUMENT", 400);
    if (rc == DESK_HW_ERR_UNAVAILABLE)
        return desk_sdk_notification_nack(out, cap, command_id, "NOTIFICATION_DELIVERY_FAILED", 503);
    return desk_sdk_notification_nack(out, cap, command_id, "NOTIFICATION_DELIVERY_FAILED", 500);
}

static bool desk_sdk_notification_command_id_valid(const char *id)
{
    if (!id || strlen(id) != 32) return false;
    for (size_t i = 0; i < 32; ++i)
        if (!((id[i] >= '0' && id[i] <= '9') || (id[i] >= 'a' && id[i] <= 'f'))) return false;
    return true;
}

static int desk_sdk_notification_dispatch(const char *json_in, char *out_buf, size_t max_out_len,
                                          const char *data)
{
    static const char *const root_keys[] = { "type", "code", "data" };
    static const char *const data_keys[] = {
        "notification_id", "reminder_id", "occurrence_id", "title",
        "scheduled_at", "expires_at", "is_catchup", "command_id"
    };
    char root_code[16] = "", command_id[40] = "";
    unsigned code_count = desk_log_get_member(json_in, "code", root_code, sizeof(root_code));
    bool outer_valid = desk_log_object_keys_allowed(json_in, root_keys, 3) &&
                       code_count == 1 && strcmp(root_code, "0") == 0 &&
                       desk_log_object_keys_allowed(data, data_keys, 8);
    bool has_command_id = desk_sdk_notification_get_string(data, "command_id", command_id, sizeof(command_id)) &&
                         desk_sdk_notification_command_id_valid(command_id);
    if (!has_command_id) return -1;
    if (!outer_valid)
        return desk_sdk_notification_nack(out_buf, max_out_len, command_id, "INVALID_ARGUMENT", 400);
    if (!desk_sdk_ws_client_is_authenticated())
        return desk_sdk_notification_nack(out_buf, max_out_len, command_id, "AUTH_REQUIRED", 401);

    desk_notification_input_t input = {0};
    char catchup_raw[8] = "";
    uint64_t scheduled_us = 0, expires_us = 0;
    bool fields_valid =
        desk_sdk_notification_get_string(data, "notification_id", input.notification_id, sizeof(input.notification_id)) &&
        desk_sdk_notification_get_string(data, "reminder_id", input.reminder_id, sizeof(input.reminder_id)) &&
        desk_sdk_notification_get_string(data, "occurrence_id", input.occurrence_id, sizeof(input.occurrence_id)) &&
        desk_sdk_notification_get_string(data, "title", input.title, sizeof(input.title)) &&
        desk_sdk_notification_get_string(data, "scheduled_at", input.scheduled_at, sizeof(input.scheduled_at)) &&
        desk_sdk_notification_get_string(data, "expires_at", input.expires_at, sizeof(input.expires_at)) &&
        desk_log_get_member(data, "is_catchup", catchup_raw, sizeof(catchup_raw)) == 1 &&
        (strcmp(catchup_raw, "true") == 0 || strcmp(catchup_raw, "false") == 0) &&
        desk_sdk_notification_parse_timestamp(input.scheduled_at, &scheduled_us) &&
        desk_sdk_notification_parse_timestamp(input.expires_at, &expires_us) &&
        expires_us > scheduled_us &&
        strcmp(input.notification_id, input.occurrence_id) == 0;
    if (!fields_valid)
        return desk_sdk_notification_nack(out_buf, max_out_len, command_id, "INVALID_ARGUMENT", 400);
    input.is_catchup = strcmp(catchup_raw, "true") == 0;

    desk_notification_status_event_t status = {0};
    int rc = desk_hardware_notification_deliver(&input, &status);
    if (rc != DESK_HW_OK)
        return desk_sdk_notification_result_nack(out_buf, max_out_len, command_id, rc);
    int n = snprintf(out_buf, max_out_len,
        "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"type\":\"notification.deliver\",\"command_id\":\"%s\"}}",
        command_id);
    return n >= 0 && (size_t)n < max_out_len ? 0 : -1;
}

static bool desk_sdk_summary_identifier_valid(const char *id)
{
    size_t len = id ? strlen(id) : 0;
    if (len == 0 || len > 128) return false;
    for (size_t i = 0; i < len; ++i) {
        char c = id[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '.' || c == '_' || c == ':' || c == '-')) return false;
    }
    return true;
}

static bool desk_sdk_summary_count_valid(const char *raw)
{
    if (!raw || !raw[0]) return false;
    uint64_t value = 0;
    for (const char *p = raw; *p; ++p) {
        if (*p < '0' || *p > '9') return false;
        unsigned digit = (unsigned)(*p - '0');
        if (value > (UINT64_MAX - digit) / 10u) return false;
        value = value * 10u + digit;
    }
    return true;
}

static bool desk_sdk_summary_text_nonblank(const char *text)
{
    if (!text || !text[0]) return false;
    for (const unsigned char *p = (const unsigned char *)text; *p; ++p)
        if (*p != ' ' && *p != '\t' && *p != '\r' && *p != '\n') return true;
    return false;
}

static bool desk_sdk_summary_validate_items(const char *raw_items, size_t items_len)
{
    desk_log_json_parser_t p = { .p = raw_items, .depth = 0 };
    if (*p.p++ != '[') return false;
    desk_log_json_ws(&p);
    uint64_t previous_us = 0;
    unsigned count = 0;
    if (*p.p == ']') return false;
    for (;;) {
        const char *start = p.p;
        if (!desk_log_json_value(&p)) return false;
        size_t raw_len = (size_t)(p.p - start);
        if (raw_len >= 2048 || ++count > 20) return false;
        char item[2048];
        memcpy(item, start, raw_len); item[raw_len] = '\0';
        static const char *const keys[] = {
            "reminder_id", "occurrence_id", "title", "scheduled_at", "timezone"
        };
        char reminder_id[129] = "", occurrence_id[129] = "", title[1025] = "";
        char scheduled_at[32] = "", timezone_name[65] = "";
        uint64_t scheduled_us = 0;
        if (!desk_log_object_keys_allowed(item, keys, sizeof(keys) / sizeof(keys[0])) ||
            !desk_sdk_notification_get_string(item, "reminder_id", reminder_id, sizeof(reminder_id)) ||
            !desk_sdk_notification_get_string(item, "occurrence_id", occurrence_id, sizeof(occurrence_id)) ||
            !desk_sdk_notification_get_string(item, "title", title, sizeof(title)) ||
            !desk_sdk_notification_get_string(item, "scheduled_at", scheduled_at, sizeof(scheduled_at)) ||
            !desk_sdk_notification_get_string(item, "timezone", timezone_name, sizeof(timezone_name)) ||
            strlen(reminder_id) > 128 || strlen(occurrence_id) > 128 ||
            !desk_sdk_summary_text_nonblank(title) || strlen(title) > 256 ||
            strlen(timezone_name) > 64 || !timezone_name[0] ||
            !desk_sdk_notification_parse_timestamp(scheduled_at, &scheduled_us) ||
            (count > 1 && scheduled_us < previous_us)) return false;
        previous_us = scheduled_us;
        desk_log_json_ws(&p);
        if (*p.p == ']') { ++p.p; return count >= 1 && (size_t)(p.p - raw_items) == items_len; }
        if (*p.p++ != ',') return false;
        desk_log_json_ws(&p);
    }
}

static int desk_sdk_notification_summary_nack(char *out, size_t cap, const char *command_id,
                                              const char *error, int code)
{
    int n = snprintf(out, cap,
        "{\"type\":\"sys.nack\",\"code\":%d,\"data\":{\"type\":\"notification.summary.deliver\",\"command_id\":\"%s\",\"error\":\"%s\"}}",
        code, command_id, error);
    return n >= 0 && (size_t)n < cap ? 0 : -1;
}

static int desk_sdk_notification_summary_dispatch(const char *json_in, char *out_buf,
                                                  size_t max_out_len)
{
    static const char *const root_keys[] = { "type", "code", "data" };
    static const char *const data_keys[] = {
        "summary_id", "batch_id", "created_at", "expires_at", "items", "omitted_count", "command_id"
    };
    char command_id[40] = "", command_raw[48] = "", root_code[16] = "";
    const char *data = NULL;
    size_t data_len = 0;
    if (!desk_log_get_member_span(json_in, "data", &data, &data_len) || data_len == 0 ||
        desk_log_get_member(data, "command_id", command_raw, sizeof(command_raw)) != 1 ||
        !json_parse_top_level_str(data, "command_id", command_id, sizeof(command_id)) ||
        !desk_sdk_notification_command_id_valid(command_id)) return -1;
    if (!desk_log_object_keys_allowed(json_in, root_keys, sizeof(root_keys) / sizeof(root_keys[0])) ||
        desk_log_get_member(json_in, "code", root_code, sizeof(root_code)) != 1 || strcmp(root_code, "0") != 0 ||
        !desk_log_object_keys_allowed(data, data_keys, sizeof(data_keys) / sizeof(data_keys[0])))
        return desk_sdk_notification_summary_nack(out_buf, max_out_len, command_id, "INVALID_ARGUMENT", 400);
    if (!desk_sdk_ws_client_is_authenticated())
        return desk_sdk_notification_summary_nack(out_buf, max_out_len, command_id, "AUTH_REQUIRED", 401);

    char summary_id[129] = "", batch_id[129] = "", created_at[32] = "", expires_at[32] = "";
    char omitted_count[32] = "";
    const char *items = NULL;
    size_t items_len = 0;
    uint64_t created_us = 0, expires_us = 0;
    bool valid =
        desk_sdk_notification_get_string(data, "summary_id", summary_id, sizeof(summary_id)) &&
        desk_sdk_notification_get_string(data, "batch_id", batch_id, sizeof(batch_id)) &&
        desk_sdk_summary_identifier_valid(summary_id) && desk_sdk_summary_identifier_valid(batch_id) &&
        desk_sdk_notification_get_string(data, "created_at", created_at, sizeof(created_at)) &&
        desk_sdk_notification_get_string(data, "expires_at", expires_at, sizeof(expires_at)) &&
        desk_sdk_notification_parse_timestamp(created_at, &created_us) &&
        desk_sdk_notification_parse_timestamp(expires_at, &expires_us) &&
        expires_us > created_us && expires_us - created_us == 86400000000ULL &&
        desk_log_get_member_span(data, "items", &items, &items_len) &&
        desk_sdk_summary_validate_items(items, items_len) &&
        desk_log_get_member(data, "omitted_count", omitted_count, sizeof(omitted_count)) == 1 &&
        desk_sdk_summary_count_valid(omitted_count);
    if (!valid)
        return desk_sdk_notification_summary_nack(out_buf, max_out_len, command_id, "INVALID_ARGUMENT", 400);

    /* CR-018 has no approved durable device store/provider for summary IDs.
     * Preserve the stable payload ID at the bridge boundary and fail closed. */
    return desk_sdk_notification_summary_nack(out_buf, max_out_len, command_id, "STORAGE_UNAVAILABLE", 503);
}

static bool desk_log_component_filter_valid(const char *raw, bool *camera_only)
{
    desk_log_json_parser_t p = { .p = raw, .depth = 0 };
    desk_log_json_ws(&p);
    if (*p.p++ != '[') return false;
    desk_log_json_ws(&p);
    if (*p.p == ']') { ++p.p; desk_log_json_ws(&p); *camera_only = false; return *p.p == '\0'; }
    const char *start, *end;
    if (!desk_log_json_string(&p, &start, &end) || !desk_log_plain_key(start, end, "camera")) return false;
    desk_log_json_ws(&p);
    if (*p.p++ != ']') return false;
    desk_log_json_ws(&p);
    *camera_only = true;
    return *p.p == '\0';
}

/* This is deliberately called only by the WebSocket receive loop. The legacy
 * TCP line server continues to use desk_sdk_bridge_process_command(). */
int desk_sdk_bridge_process_ws_command(const char *json_in, char *out_buf, size_t max_out_len)
{
    char type_raw[96] = "", code_raw[16] = "";
    char data[3072] = "";
    if (!json_in || strnlen(json_in, DESK_SDK_MAX_FRAME_LEN + 1U) > DESK_SDK_MAX_FRAME_LEN ||
        !desk_log_json_valid(json_in)) return -1;
    if (desk_log_get_member(json_in, "type", type_raw, sizeof(type_raw)) != 1) return -1;
    char type[64] = "";
    if (!json_parse_top_level_str(json_in, "type", type, sizeof(type))) return -1;
    if (strcmp(type, "ctrl.camera.stream.start") == 0 ||
        strcmp(type, "ctrl.camera.stream.stop") == 0 ||
        strcmp(type, "ctrl.camera.clock.ping") == 0) {
        return desk_sdk_camera_stream_command(json_in, out_buf, max_out_len, type);
    }
    if (strcmp(type, "ctrl.camera.acquire") == 0 || strcmp(type, "camera.acquire") == 0) {
        if (!desk_sdk_camera_ws_session_is_paired()) {
            char rid_scratch[128];
            const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":403,\"error\":{"
                     "\"code\":\"AUTH_REQUIRED\",\"message\":\"Camera lease requires paired WebSocket\"}}", rid);
            return 0;
        }
        int rc = desk_sdk_bridge_process_command(json_in, out_buf, max_out_len);
        if (rc == 0 && strstr(out_buf, "\"type\":\"sys.ack\"")) {
            char client_id[32] = "";
            if (json_parse_top_level_str(json_in, "client_id", client_id, sizeof(client_id)) && client_id[0] &&
                desk_arbiter_camera_lease_matches(client_id)) {
                pthread_mutex_lock(&s_ws_camera_owner_mutex);
                snprintf(s_ws_camera_owner_id, sizeof(s_ws_camera_owner_id), "%s", client_id);
                pthread_mutex_unlock(&s_ws_camera_owner_mutex);
            }
        }
        return rc;
    }
    if (strcmp(type, "ctrl.camera.release") == 0 || strcmp(type, "camera.release") == 0) {
        char client_id[32] = "";
        (void)json_parse_top_level_str(json_in, "client_id", client_id, sizeof(client_id));
        pthread_mutex_lock(&s_ws_camera_owner_mutex);
        bool owner_matches = client_id[0] && strcmp(client_id, s_ws_camera_owner_id) == 0;
        pthread_mutex_unlock(&s_ws_camera_owner_mutex);
        if (!desk_sdk_camera_ws_session_is_paired() || !owner_matches) {
            char rid_scratch[128];
            const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":403,\"error\":{"
                     "\"code\":\"NO_ACTIVE_LEASE\",\"message\":\"Paired WebSocket does not own this lease\"}}", rid);
            return 0;
        }
        pthread_mutex_lock(&s_camera_stream.mutex);
        bool stream_owned = s_camera_stream.active &&
                            strcmp(client_id, s_camera_stream.client_id) == 0;
        pthread_mutex_unlock(&s_camera_stream.mutex);
        if (stream_owned && desk_sdk_camera_stream_stop_internal(false) != DESK_HW_OK) {
            char rid_scratch[128];
            const char *rid = desk_sdk_request_id_fragment(json_in, rid_scratch, sizeof(rid_scratch));
            snprintf(out_buf, max_out_len,
                     "{\"type\":\"sys.nack\",%s\"code\":500,\"error\":{"
                     "\"code\":\"CAMERA_STOP_FAILED\",\"message\":\"Camera stream could not be stopped\"}}", rid);
            return 0;
        }
        int rc = desk_sdk_bridge_process_command(json_in, out_buf, max_out_len);
        if (rc == 0 && strstr(out_buf, "\"type\":\"sys.ack\"")) {
            pthread_mutex_lock(&s_ws_camera_owner_mutex);
            s_ws_camera_owner_id[0] = '\0';
            pthread_mutex_unlock(&s_ws_camera_owner_mutex);
        }
        return rc;
    }
    if (strcmp(type, "notification.summary.deliver") == 0)
        return desk_sdk_notification_summary_dispatch(json_in, out_buf, max_out_len);
    if (!desk_log_get_member(json_in, "data", data, sizeof(data))) return -1;
    if (strcmp(type, "notification.deliver") == 0)
        return desk_sdk_notification_dispatch(json_in, out_buf, max_out_len, data);
    if (strcmp(type, "sys.log.subscribe") != 0 && strcmp(type, "sys.log.unsubscribe") != 0)
        return desk_sdk_bridge_process_command(json_in, out_buf, max_out_len);
    static const char *const root_keys[] = { "type", "code", "data" };
    static const char *const subscribe_keys[] = { "command_id", "log_schema_version", "min_level", "components" };
    static const char *const unsubscribe_keys[] = { "command_id", "log_schema_version", "stream_id" };
    char command_id[65] = "";
    if (!out_buf || max_out_len < 128 ||
        (strcmp(type_raw, "\"sys.log.subscribe\"") != 0 &&
         strcmp(type_raw, "\"sys.log.unsubscribe\"") != 0) ||
        !desk_log_object_keys_allowed(json_in, root_keys, sizeof(root_keys) / sizeof(root_keys[0])) ||
        desk_log_get_member(json_in, "data", data, sizeof(data)) != 1 ||
        desk_log_get_member(json_in, "code", code_raw, sizeof(code_raw)) != 1 || strcmp(code_raw, "0") != 0 ||
        !desk_log_object_keys_allowed(data, strcmp(type, "sys.log.subscribe") == 0 ? subscribe_keys : unsubscribe_keys,
            strcmp(type, "sys.log.subscribe") == 0 ? sizeof(subscribe_keys) / sizeof(subscribe_keys[0]) : sizeof(unsubscribe_keys) / sizeof(unsubscribe_keys[0])) ||
        desk_log_get_member(data, "command_id", command_id, sizeof(command_id)) != 1 ||
        !json_parse_top_level_str(data, "command_id", command_id, sizeof(command_id)) ||
        !desk_sdk_log_command_id_valid(command_id)) return -1;

    if (!desk_sdk_ws_client_is_authenticated() || !desk_sdk_log_pairing_is_complete())
        return desk_sdk_log_nack(out_buf, max_out_len, type, command_id, "AUTH_REQUIRED", 401);
    pthread_mutex_lock(&s_log_mutex);
    if (!s_local_log_enabled) {
        pthread_mutex_unlock(&s_log_mutex);
        return desk_sdk_log_nack(out_buf, max_out_len, type, command_id, "LOGGING_DISABLED", 403);
    }

    char schema[16] = "", stream_id[40] = "", min_level[8] = "WARN", field_raw[512] = "";
    if (desk_log_get_member(data, "log_schema_version", field_raw, sizeof(field_raw)) != 1 ||
        !json_parse_top_level_str(data, "log_schema_version", schema, sizeof(schema)) ||
        strcmp(schema, "1.0.0") != 0) {
        pthread_mutex_unlock(&s_log_mutex);
        return desk_sdk_log_nack(out_buf, max_out_len, type, command_id, "UNSUPPORTED_SCHEMA", 400);
    }
    if (strcmp(type, "sys.log.subscribe") == 0) {
        unsigned min_count = desk_log_get_member(data, "min_level", field_raw, sizeof(field_raw));
        if (min_count > 1 || (min_count == 1 && !json_parse_top_level_str(data, "min_level", min_level, sizeof(min_level)))) {
            pthread_mutex_unlock(&s_log_mutex);
            return desk_sdk_log_nack(out_buf, max_out_len, type, command_id, "INVALID_FILTER", 400);
        }
        if (strcmp(min_level, "ERROR") != 0 && strcmp(min_level, "WARN") != 0 &&
            strcmp(min_level, "INFO") != 0 && strcmp(min_level, "DEBUG") != 0) {
            pthread_mutex_unlock(&s_log_mutex);
            return desk_sdk_log_nack(out_buf, max_out_len, type, command_id, "INVALID_FILTER", 400);
        }
        /* v1 currently has only the camera component. Accept omitted/empty or
         * exactly ["camera"]; reject every other shape rather than widening. */
        bool camera_only = false;
        unsigned component_count = desk_log_get_member(data, "components", field_raw, sizeof(field_raw));
        if (component_count > 1 || (component_count == 1 && !desk_log_component_filter_valid(field_raw, &camera_only))) {
                pthread_mutex_unlock(&s_log_mutex);
                return desk_sdk_log_nack(out_buf, max_out_len, type, command_id, "INVALID_FILTER", 400);
        }
        if (s_log_stream_active) {
            pthread_mutex_unlock(&s_log_mutex);
            return desk_sdk_log_nack(out_buf, max_out_len, type, command_id, "ALREADY_SUBSCRIBED", 409);
        }
        generate_dynamic_token(s_log_stream_id, sizeof(s_log_stream_id));
        if (!s_log_boot_id[0]) generate_dynamic_token(s_log_boot_id, sizeof(s_log_boot_id));
        s_log_stream_active = true;
        s_log_camera_only = camera_only;
        strcpy(s_log_min_level, min_level);
        s_log_has_pending_record = false;
        s_log_has_pending_gap = false;
        desk_watcher_log_source_set_enabled(true);
        int n = snprintf(out_buf, max_out_len,
            "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"type\":\"sys.log.subscribe\",\"command_id\":\"%s\",\"accepted\":true,\"stream_id\":\"%s\",\"boot_id\":\"%s\",\"next_sequence\":%llu,\"min_level\":\"%s\",\"components\":%s}}",
            command_id, s_log_stream_id, s_log_boot_id,
            (unsigned long long)desk_watcher_log_source_next_sequence(),
            s_log_min_level, s_log_camera_only ? "[\"camera\"]" : "[]");
        if (n < 0 || (size_t)n >= max_out_len) {
            s_log_stream_active = false;
            s_log_stream_id[0] = '\0';
            desk_sdk_log_source_reset_locked();
        }
        pthread_mutex_unlock(&s_log_mutex);
        return n >= 0 && (size_t)n < max_out_len ? 0 : -1;
    }

    if (desk_log_get_member(data, "stream_id", field_raw, sizeof(field_raw)) != 1 ||
        !json_parse_top_level_str(data, "stream_id", stream_id, sizeof(stream_id)) ||
        !s_log_stream_active || strcmp(stream_id, s_log_stream_id) != 0) {
        pthread_mutex_unlock(&s_log_mutex);
        return desk_sdk_log_nack(out_buf, max_out_len, type, command_id, "INVALID_STREAM", 409);
    }
    s_log_stream_active = false;
    s_log_stream_id[0] = '\0';
    desk_sdk_log_source_reset_locked();
    int n = snprintf(out_buf, max_out_len,
        "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"type\":\"sys.log.unsubscribe\",\"command_id\":\"%s\",\"accepted\":true}}",
        command_id);
    pthread_mutex_unlock(&s_log_mutex);
    return n >= 0 && (size_t)n < max_out_len ? 0 : -1;
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

    if (desk_sdk_ws_client_is_connected()) {
        desk_sdk_ws_client_send_text(event_buf);
    } else if (s_client_fd >= 0) {
        desk_sdk_socket_send_line(event_buf);
    }

    return 0;
}

int desk_sdk_bridge_send_notification_summary_ack(const char *summary_id, const char *source)
{
    if (!desk_sdk_summary_identifier_valid(summary_id) || !source) return -1;
    size_t source_len = strlen(source);
    if (source_len == 0 || source_len > 64) return -1;
    for (size_t i = 0; i < source_len; ++i) {
        char c = source[i];
        if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
              (c >= '0' && c <= '9') || c == '.' || c == '_' || c == ':' || c == '-')) return -1;
    }
    char payload[256];
    int n = snprintf(payload, sizeof(payload),
                     "{\"summary_id\":\"%s\",\"source\":\"%s\"}", summary_id, source);
    if (n < 0 || (size_t)n >= sizeof(payload)) return -1;
    return desk_sdk_bridge_send_event("notification.acknowledged", payload);
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

    if (desk_sdk_ws_client_is_connected()) {
        desk_sdk_ws_client_send_text(formatted);
    } else if (s_client_fd >= 0) {
        desk_sdk_socket_send_line(formatted);
    }

    return 0;
}

int desk_sdk_bridge_format_roller_input(desk_sdk_roller_delta_t delta,
                                        int64_t timestamp_ms,
                                        char *out_json,
                                        size_t max_len)
{
    if ((delta != DESK_SDK_ROLLER_DELTA_POSITIVE &&
         delta != DESK_SDK_ROLLER_DELTA_NEGATIVE) ||
        timestamp_ms < 0 || !out_json || max_len == 0) {
        return DESK_SDK_ROLLER_ERR_INVALID;
    }

    int written = snprintf(out_json, max_len,
        "{\"type\":\"evt.sdk.input\",\"data\":{"
        "\"source\":\"roller\",\"action\":\"rotate\","
        "\"delta\":%d,\"timestamp_ms\":%lld}}",
        (int)delta, (long long)timestamp_ms);
    if (written < 0 || (size_t)written >= max_len) {
        out_json[0] = '\0';
        return DESK_SDK_ROLLER_ERR_BUFFER;
    }
    return DESK_SDK_ROLLER_OK;
}

int desk_sdk_bridge_send_roller_input(desk_sdk_roller_delta_t delta,
                                      int64_t timestamp_ms)
{
    char event_json[160];
    int rc = desk_sdk_bridge_format_roller_input(delta, timestamp_ms,
                                                  event_json, sizeof(event_json));
    if (rc != DESK_SDK_ROLLER_OK) return rc;
    int send_rc = desk_sdk_ws_client_send_text_nonblocking(event_json);
    if (send_rc == -1) return DESK_SDK_ROLLER_ERR_NOT_CONNECTED;
    return send_rc == 0
        ? DESK_SDK_ROLLER_OK
        : DESK_SDK_ROLLER_ERR_SEND;
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

    if (desk_sdk_ws_client_is_connected()) {
        desk_sdk_ws_client_send_text(payload_to_send);
    } else if (s_client_fd >= 0) {
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
                pthread_mutex_lock(&s_pairing_code_mutex);
                desk_sdk_ws_client_close();
                inet_ntop(AF_INET, &client_addr.sin_addr, s_pair_host, sizeof(s_pair_host));
                desk_sdk_ws_client_close();
                pthread_mutex_unlock(&s_pairing_code_mutex);
                sendto(s_pairing_udp_fd, s_pairing_reply, strlen(s_pairing_reply), 0,
                       (struct sockaddr *)&client_addr, addr_len);
                desk_sdk_ws_client_connect_paired();
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
    desk_sdk_pairing_code_expire_if_needed();
    int half = timeout_ms / 2;
    if (half < 1) half = 1;
    desk_sdk_socket_server_poll(half);
    desk_sdk_pairing_server_poll(half);
    desk_sdk_ws_client_poll(half);
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
