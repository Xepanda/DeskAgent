#include <assert.h>
#include <string.h>
#include "provisioning_state.h"

int main(void)
{
    desk_provisioning_state_t state;
    desk_provisioning_state_init(&state);
    assert(strcmp(desk_provisioning_stage_name(state.stage), "idle") == 0);

    assert(desk_provisioning_apply_event(&state, DESK_PROVISIONING_EVENT_BIND_START));
    assert(strcmp(desk_provisioning_stage_name(state.stage), "bind_start") == 0);
    assert(desk_provisioning_apply_event(&state, DESK_PROVISIONING_EVENT_TOKEN_RECEIVED));
    assert(strcmp(desk_provisioning_stage_name(state.stage), "token_received") == 0);
    assert(desk_provisioning_apply_event(&state, DESK_PROVISIONING_EVENT_ACTIVATED));
    assert(strcmp(desk_provisioning_stage_name(state.stage), "activated") == 0);
    assert(desk_provisioning_apply_event(&state, DESK_PROVISIONING_EVENT_MQTT_ONLINE));
    assert(strcmp(desk_provisioning_stage_name(state.stage), "mqtt_online") == 0);
    assert(desk_provisioning_apply_event(&state, DESK_PROVISIONING_EVENT_MQTT_OFFLINE));
    assert(strcmp(desk_provisioning_stage_name(state.stage), "mqtt_offline") == 0);

    assert(!desk_provisioning_apply_event(&state, 999));
    assert(strcmp(desk_provisioning_stage_name(state.stage), "mqtt_offline") == 0);
    desk_provisioning_record_error(&state, "netcfg", -23);
    assert(strcmp(state.error_stage, "netcfg") == 0);
    assert(state.error_code == -23);
    assert(strcmp(desk_provisioning_stage_name(state.stage), "mqtt_offline") == 0);

    desk_yield_retry_state_t retry = {0};
    bool should_log = false;
    unsigned delay_ms = 0;
    assert(desk_yield_retry_step(&retry, -23, 0, false, &should_log, &delay_ms));
    assert(should_log && delay_ms > 0 && retry.consecutive_errors == 1);
    assert(desk_yield_retry_step(&retry, 0, 0, false, &should_log, &delay_ms));
    assert(!should_log && delay_ms == 0 && retry.consecutive_errors == 0);
    assert(!desk_yield_retry_step(&retry, -1, 0, true, &should_log, &delay_ms));

    assert(desk_log_line_has_sensitive_marker("token field"));
    assert(desk_log_line_has_sensitive_marker("REGIST_KEY field"));
    assert(!desk_log_line_has_sensitive_marker("network state changed"));
    return 0;
}
