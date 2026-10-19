#ifndef DESK_PROVISIONING_STATE_H_
#define DESK_PROVISIONING_STATE_H_

#include <stdbool.h>

typedef enum {
    DESK_PROVISIONING_IDLE = 0,
    DESK_PROVISIONING_BIND_START,
    DESK_PROVISIONING_TOKEN_RECEIVED,
    DESK_PROVISIONING_ACTIVATED,
    DESK_PROVISIONING_MQTT_ONLINE,
    DESK_PROVISIONING_MQTT_OFFLINE,
} desk_provisioning_stage_t;

typedef enum {
    DESK_PROVISIONING_EVENT_BIND_START = 1,
    DESK_PROVISIONING_EVENT_TOKEN_RECEIVED,
    DESK_PROVISIONING_EVENT_ACTIVATED,
    DESK_PROVISIONING_EVENT_MQTT_ONLINE,
    DESK_PROVISIONING_EVENT_MQTT_OFFLINE,
} desk_provisioning_event_t;

typedef struct {
    desk_provisioning_stage_t stage;
    const char *error_stage;
    int error_code;
} desk_provisioning_state_t;

typedef struct {
    unsigned consecutive_errors;
} desk_yield_retry_state_t;

void desk_provisioning_state_init(desk_provisioning_state_t *state);
bool desk_provisioning_apply_event(desk_provisioning_state_t *state, int event);
void desk_provisioning_record_error(desk_provisioning_state_t *state, const char *stage, int error_code);
const char *desk_provisioning_stage_name(desk_provisioning_stage_t stage);
desk_provisioning_state_t *desk_provisioning_current_state(void);
bool desk_yield_retry_step(desk_yield_retry_state_t *state, int rc, int success_code,
                           bool fatal_error, bool *should_log, unsigned *delay_ms);
bool desk_log_line_has_sensitive_marker(const char *line);

#endif
