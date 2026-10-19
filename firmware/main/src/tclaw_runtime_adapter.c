#include "tclaw_runtime_adapter.h"
#include <stddef.h>

#ifdef ESP_PLATFORM

#include <stdio.h>
#include <string.h>

#include "ai_chat_main.h"
#include "agent_loop.h"
#include "acp_client.h"
#include "app_im.h"
#include "desk_cli.h"
#include "desk_tclaw_bridge.h"
#include "provisioning_state.h"
#include "sys_bus.h"
#include "tclaw_chat.h"
#include "tools_register.h"
#include "tuya_app_config.h"
#include "im_config.h"
#include "ws_server.h"
#include "cJSON.h"

enum {
    STAGE_SYS_BUS = 0,
    STAGE_TCLAW_CHAT,
    STAGE_APP_IM,
    STAGE_WS_SERVER,
    STAGE_TOOL_REGISTRY,
    STAGE_ACP_CLIENT,
    STAGE_AGENT_LOOP,
    STAGE_COUNT,
};

static desk_tclaw_runtime_stage_t s_stages[STAGE_COUNT] = {
    {"sys_bus", DESK_TCLAW_RUNTIME_BLOCKED, 0},
    {"tclaw_chat", DESK_TCLAW_RUNTIME_BLOCKED, 0},
    {"app_im", DESK_TCLAW_RUNTIME_DISABLED, 0},
    {"ws_server", DESK_TCLAW_RUNTIME_DISABLED, 0},
    {"tool_registry", DESK_TCLAW_RUNTIME_BLOCKED, 0},
    {"acp_client", DESK_TCLAW_RUNTIME_DISABLED, 0},
    {"agent_loop", DESK_TCLAW_RUNTIME_BLOCKED, 0},
};

static const char *runtime_state_name(desk_tclaw_runtime_state_t state)
{
    switch (state) {
    case DESK_TCLAW_RUNTIME_READY: return "ready";
    case DESK_TCLAW_RUNTIME_SUBSCRIBED: return "subscribed";
    case DESK_TCLAW_RUNTIME_DISABLED: return "disabled";
    case DESK_TCLAW_RUNTIME_FAILED: return "failed";
    case DESK_TCLAW_RUNTIME_BLOCKED: return "blocked";
    default: return "unknown";
    }
}

static void record_stage(unsigned index, desk_tclaw_runtime_state_t state, int rc)
{
    if (index >= STAGE_COUNT) return;
    s_stages[index].state = state;
    s_stages[index].error_code = rc;
    printf("[TCLAW_RUNTIME] subsystem=%s state=%s rc=%d\n",
           s_stages[index].name, runtime_state_name(state), rc);
}

static bool has_im_credentials(void)
{
    if (strcmp(IM_SECRET_CHANNEL_MODE, "feishu") == 0) {
        return IM_SECRET_FS_APP_ID[0] != '\0' && IM_SECRET_FS_APP_SECRET[0] != '\0';
    }
    if (strcmp(IM_SECRET_CHANNEL_MODE, "telegram") == 0) {
        return IM_SECRET_TG_TOKEN[0] != '\0';
    }
    if (strcmp(IM_SECRET_CHANNEL_MODE, "discord") == 0) {
        return IM_SECRET_DC_TOKEN[0] != '\0' && IM_SECRET_DC_CHANNEL_ID[0] != '\0';
    }
    if (strcmp(IM_SECRET_CHANNEL_MODE, "weixin") == 0) {
        return IM_SECRET_WX_TOKEN[0] != '\0';
    }
    if (strcmp(IM_SECRET_CHANNEL_MODE, "qqbot") == 0) {
        return IM_SECRET_QQ_APP_ID[0] != '\0' && IM_SECRET_QQ_CLIENT_SECRET[0] != '\0';
    }
    return false;
}

static bool response_text(const cJSON *object, const char *name, const char **out)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(object, name);
    if (!cJSON_IsString(item) || !item->valuestring || item->valuestring[0] == '\0') return false;
    *out = item->valuestring;
    return true;
}

const desk_tclaw_runtime_stage_t *desk_tclaw_runtime_stages(unsigned *count)
{
    if (count) *count = STAGE_COUNT;
    return s_stages;
}

int desk_tclaw_runtime_init(void)
{
    int first_error = 0;
    int rc = sys_bus_init();
    record_stage(STAGE_SYS_BUS, rc == OPRT_OK ? DESK_TCLAW_RUNTIME_READY : DESK_TCLAW_RUNTIME_FAILED, rc);
    if (rc != OPRT_OK && first_error == 0) first_error = rc;

    rc = tclaw_chat_init();
    record_stage(STAGE_TCLAW_CHAT, rc == OPRT_OK ? DESK_TCLAW_RUNTIME_SUBSCRIBED : DESK_TCLAW_RUNTIME_FAILED, rc);
    if (rc != OPRT_OK && first_error == 0) first_error = rc;

    if (!has_im_credentials()) {
        record_stage(STAGE_APP_IM, DESK_TCLAW_RUNTIME_DISABLED, 0);
    } else if (s_stages[STAGE_SYS_BUS].state != DESK_TCLAW_RUNTIME_READY) {
        record_stage(STAGE_APP_IM, DESK_TCLAW_RUNTIME_BLOCKED, s_stages[STAGE_SYS_BUS].error_code);
    } else {
        rc = app_im_init();
        record_stage(STAGE_APP_IM, rc == OPRT_OK ? DESK_TCLAW_RUNTIME_SUBSCRIBED : DESK_TCLAW_RUNTIME_FAILED, rc);
        if (rc != OPRT_OK && first_error == 0) first_error = rc;
    }

    if (CLAW_WS_AUTH_TOKEN[0] == '\0') {
        record_stage(STAGE_WS_SERVER, DESK_TCLAW_RUNTIME_DISABLED, 0);
    } else {
        rc = ws_server_start();
        record_stage(STAGE_WS_SERVER, rc == OPRT_OK ? DESK_TCLAW_RUNTIME_READY : DESK_TCLAW_RUNTIME_FAILED, rc);
        if (rc != OPRT_OK && first_error == 0) first_error = rc;
    }

    rc = tool_registry_init();
    record_stage(STAGE_TOOL_REGISTRY,
                 rc == OPRT_OK ? DESK_TCLAW_RUNTIME_SUBSCRIBED : DESK_TCLAW_RUNTIME_FAILED, rc);
    if (rc != OPRT_OK && first_error == 0) first_error = rc;

#if defined(ACP_CLIENT_STACK_SIZE)
    if (OPENCLAW_GATEWAY_TOKEN[0] == '\0' || OPENCLAW_GATEWAY_HOST[0] == '\0') {
        record_stage(STAGE_ACP_CLIENT, DESK_TCLAW_RUNTIME_DISABLED, 0);
    } else {
        rc = acp_client_init();
        record_stage(STAGE_ACP_CLIENT,
                     rc == OPRT_OK ? DESK_TCLAW_RUNTIME_SUBSCRIBED : DESK_TCLAW_RUNTIME_FAILED, rc);
        if (rc != OPRT_OK && first_error == 0) first_error = rc;
    }
#else
    record_stage(STAGE_ACP_CLIENT, DESK_TCLAW_RUNTIME_DISABLED, 0);
#endif

    if (s_stages[STAGE_SYS_BUS].state != DESK_TCLAW_RUNTIME_READY ||
        s_stages[STAGE_TCLAW_CHAT].state == DESK_TCLAW_RUNTIME_FAILED) {
        record_stage(STAGE_AGENT_LOOP, DESK_TCLAW_RUNTIME_BLOCKED,
                     first_error ? first_error : OPRT_RESOURCE_NOT_READY);
        if (first_error == 0) first_error = OPRT_RESOURCE_NOT_READY;
    } else {
        rc = agent_loop_init();
        record_stage(STAGE_AGENT_LOOP,
                     rc == OPRT_OK ? DESK_TCLAW_RUNTIME_SUBSCRIBED : DESK_TCLAW_RUNTIME_FAILED, rc);
        if (rc != OPRT_OK && first_error == 0) first_error = rc;
    }

    if (first_error != 0) {
        for (unsigned i = 0; i < STAGE_COUNT; i++) {
            if (s_stages[i].state == DESK_TCLAW_RUNTIME_FAILED ||
                s_stages[i].state == DESK_TCLAW_RUNTIME_BLOCKED) {
                char error_stage[32];
                snprintf(error_stage, sizeof(error_stage), "tclaw_%s", s_stages[i].name);
                desk_cli_update_provisioning_status("runtime_degraded", error_stage,
                                                    s_stages[i].error_code ? s_stages[i].error_code : first_error);
                break;
            }
        }
    }
    return first_error;
}

int desk_tclaw_runtime_handle_bridge_response(const char *response_json)
{
    if (!response_json || response_json[0] == '\0') return OPRT_INVALID_PARM;

    cJSON *root = cJSON_Parse(response_json);
    if (!cJSON_IsObject(root)) {
        cJSON_Delete(root);
        return OPRT_INVALID_PARM;
    }

    const char *request_id = NULL;
    const char *status = NULL;
    int rc = OPRT_INVALID_PARM;
    if (!response_text(root, "request_id", &request_id) ||
        !response_text(root, "status", &status)) {
        goto done;
    }

    bool success = strcmp(status, "accepted") == 0 || strcmp(status, "succeeded") == 0;
    bool known_status = success || strcmp(status, "failed") == 0 ||
                        strcmp(status, "cancelled") == 0 || strcmp(status, "unknown") == 0;
    if (!known_status) goto done;

    const cJSON *result = cJSON_GetObjectItemCaseSensitive(root, "result");
    const cJSON *error = cJSON_GetObjectItemCaseSensitive(root, "error");
    if ((success && !result) || (!success && !error)) goto done;

    const char *pending_id = desk_tclaw_get_pending_request_id();
    if (!pending_id || pending_id[0] == '\0' || strcmp(pending_id, request_id) != 0) {
        rc = OPRT_RESOURCE_NOT_READY;
        goto done;
    }

    char *result_json = success ? cJSON_PrintUnformatted(result) : NULL;
    char *error_json = success ? NULL : cJSON_PrintUnformatted(error);
    if ((success && !result_json) || (!success && !error_json)) {
        cJSON_free(result_json);
        cJSON_free(error_json);
        rc = OPRT_MALLOC_FAILED;
        goto done;
    }
    rc = desk_tclaw_handle_tool_response(request_id, status, result_json, error_json);
    cJSON_free(result_json);
    cJSON_free(error_json);

done:
    cJSON_Delete(root);
    return rc;
}

#else

int desk_tclaw_runtime_init(void) { return -95; }
int desk_tclaw_runtime_handle_bridge_response(const char *response_json)
{
    (void)response_json;
    return -95;
}
const desk_tclaw_runtime_stage_t *desk_tclaw_runtime_stages(unsigned *count)
{
    if (count) *count = 0;
    return NULL;
}

#endif
