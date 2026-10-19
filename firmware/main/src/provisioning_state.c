#include "provisioning_state.h"

#include <string.h>
#include <limits.h>

static desk_provisioning_state_t s_state;
static bool s_state_initialized;

void desk_provisioning_state_init(desk_provisioning_state_t *state)
{
    if (state) {
        state->stage = DESK_PROVISIONING_IDLE;
        state->error_stage = "none";
        state->error_code = 0;
    }
}

desk_provisioning_state_t *desk_provisioning_current_state(void)
{
    if (!s_state_initialized) {
        desk_provisioning_state_init(&s_state);
        s_state_initialized = true;
    }
    return &s_state;
}

bool desk_provisioning_apply_event(desk_provisioning_state_t *state, int event)
{
    if (!state) return false;
    switch (event) {
    case DESK_PROVISIONING_EVENT_BIND_START: state->stage = DESK_PROVISIONING_BIND_START; break;
    case DESK_PROVISIONING_EVENT_TOKEN_RECEIVED: state->stage = DESK_PROVISIONING_TOKEN_RECEIVED; break;
    case DESK_PROVISIONING_EVENT_ACTIVATED: state->stage = DESK_PROVISIONING_ACTIVATED; break;
    case DESK_PROVISIONING_EVENT_MQTT_ONLINE: state->stage = DESK_PROVISIONING_MQTT_ONLINE; break;
    case DESK_PROVISIONING_EVENT_MQTT_OFFLINE: state->stage = DESK_PROVISIONING_MQTT_OFFLINE; break;
    default: return false;
    }
    return true;
}

void desk_provisioning_record_error(desk_provisioning_state_t *state, const char *stage, int error_code)
{
    if (!state) return;
    state->error_stage = stage ? stage : "unknown";
    state->error_code = error_code;
}

const char *desk_provisioning_stage_name(desk_provisioning_stage_t stage)
{
    switch (stage) {
    case DESK_PROVISIONING_BIND_START: return "bind_start";
    case DESK_PROVISIONING_TOKEN_RECEIVED: return "token_received";
    case DESK_PROVISIONING_ACTIVATED: return "activated";
    case DESK_PROVISIONING_MQTT_ONLINE: return "mqtt_online";
    case DESK_PROVISIONING_MQTT_OFFLINE: return "mqtt_offline";
    case DESK_PROVISIONING_IDLE:
    default: return "idle";
    }
}

bool desk_yield_retry_step(desk_yield_retry_state_t *state, int rc, int success_code,
                           bool fatal_error, bool *should_log, unsigned *delay_ms)
{
    if (should_log) *should_log = false;
    if (delay_ms) *delay_ms = 0;
    if (!state) return false;
    if (rc == success_code) {
        state->consecutive_errors = 0;
        return true;
    }
    if (fatal_error) return false;

    if (state->consecutive_errors < UINT_MAX) state->consecutive_errors++;
    if (should_log && (state->consecutive_errors == 1 || state->consecutive_errors % 32 == 0)) {
        *should_log = true;
    }
    if (delay_ms) {
        unsigned exponent = state->consecutive_errors > 6 ? 5 : state->consecutive_errors - 1;
        *delay_ms = 100U << exponent;
    }
    return true;
}

static bool contains_case_insensitive(const char *line, const char *needle)
{
    if (!line || !needle || !*needle) return false;
    const size_t needle_len = strlen(needle);
    for (; *line; line++) {
        size_t i = 0;
        while (i < needle_len && line[i] &&
               (line[i] == needle[i] ||
                (line[i] >= 'A' && line[i] <= 'Z' && line[i] + ('a' - 'A') == needle[i]) ||
                (line[i] >= 'a' && line[i] <= 'z' && line[i] - ('a' - 'A') == needle[i]))) {
            i++;
        }
        if (i == needle_len) return true;
    }
    return false;
}

bool desk_log_line_has_sensitive_marker(const char *line)
{
    static const char *const markers[] = {
        "uuid", "authkey", "token", "regist_key", "password", "passwd",
        "ssid", "localkey", "access_id", "secret",
    };
    if (!line) return false;
    for (size_t i = 0; i < sizeof(markers) / sizeof(markers[0]); i++) {
        if (contains_case_insensitive(line, markers[i])) return true;
    }
    return false;
}
