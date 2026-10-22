#ifndef DESK_TCLAW_RUNTIME_ADAPTER_H_
#define DESK_TCLAW_RUNTIME_ADAPTER_H_

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    DESK_TCLAW_RUNTIME_READY = 0,
    DESK_TCLAW_RUNTIME_SUBSCRIBED,
    DESK_TCLAW_RUNTIME_DISABLED,
    DESK_TCLAW_RUNTIME_FAILED,
    DESK_TCLAW_RUNTIME_BLOCKED,
} desk_tclaw_runtime_state_t;

typedef struct {
    const char *name;
    desk_tclaw_runtime_state_t state;
    int error_code;
} desk_tclaw_runtime_stage_t;

/* Initializes the vendored TClaw runtime without owning Tuya's IoT client. */
int desk_tclaw_runtime_init(void);

/* Reports the authoritative Tuya MQTT state to the runtime UI event mapper. */
void desk_tclaw_runtime_set_cloud_online(int online);

/* Parses a real SDK bridge response and dispatches only a correlated result. */
int desk_tclaw_runtime_handle_bridge_response(const char *response_json);

/* Registers native DeskAgent MCP tools (ha.execute, codex.submit_turn, reminder.create) into ai_mcp_server */
int desk_tclaw_runtime_register_mcp_tools(void);

const desk_tclaw_runtime_stage_t *desk_tclaw_runtime_stages(unsigned *count);

#ifdef __cplusplus
}
#endif

#endif
