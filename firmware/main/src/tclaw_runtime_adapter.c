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
#include "ai_mcp_server.h"
#include "desk_sdk_bridge.h"
#include "tal_api.h"
#include "tal_event.h"
#include "tal_time_service.h"
#include "tuya_app_config.h"
#include "im_config.h"
#include "ws_server.h"
#include "cJSON.h"
#include "desk_hardware.h"

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

static const char *__get_str_prop(const MCP_PROPERTY_LIST_T *props, const char *name)
{
    const MCP_PROPERTY_T *p = ai_mcp_property_list_find(props, name);
    if (p && p->type == MCP_PROPERTY_TYPE_STRING && p->default_val.str_val) {
        return p->default_val.str_val;
    }
    return NULL;
}

static void __gen_mcp_req_id(const char *prefix, char *out_id, size_t out_len)
{
    static uint32_t s_req_seq = 0;
    uint32_t seq = ++s_req_seq;
    uint32_t ts = (uint32_t)tal_time_get_posix();
    snprintf(out_id, out_len, "req-%s-%u-%u", prefix, (unsigned)ts, (unsigned)seq);
}

static OPERATE_RET __tool_ha_execute(const MCP_PROPERTY_LIST_T *properties,
                                     MCP_RETURN_VALUE_T *ret_val,
                                     void *user_data)
{
    (void)user_data;
    const char *entity_id = __get_str_prop(properties, "entity_id");
    const char *action = __get_str_prop(properties, "action");
    const char *parameters = __get_str_prop(properties, "parameters");
    if (!entity_id || !action) {
        ai_mcp_return_value_set_str(ret_val, "Error: 'entity_id' and 'action' are required");
        return OPRT_INVALID_PARM;
    }

    cJSON *root = cJSON_CreateObject();
    if (!root) return OPRT_MALLOC_FAILED;
    char req_id[48];
    __gen_mcp_req_id("ha", req_id, sizeof(req_id));
    cJSON_AddStringToObject(root, "request_id", req_id);
    cJSON_AddStringToObject(root, "source", "tclaw_cloud_llm");
    cJSON_AddStringToObject(root, "operation", "ha.execute");

    cJSON *args = cJSON_CreateObject();
    cJSON_AddStringToObject(args, "entity_id", entity_id);
    cJSON_AddStringToObject(args, "action", action);
    if (parameters && parameters[0] == '{') {
        cJSON *p_json = cJSON_Parse(parameters);
        if (p_json) {
            cJSON_AddItemToObject(args, "parameters", p_json);
        } else {
            cJSON_AddStringToObject(args, "parameters", parameters);
        }
    }
    cJSON_AddItemToObject(root, "arguments", args);

    char *env_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);

    if (!env_str) {
        ai_mcp_return_value_set_str(ret_val, "Error: out of memory");
        return OPRT_MALLOC_FAILED;
    }

    int rc = desk_sdk_bridge_dispatch_tool_request(env_str);
    cJSON_free(env_str);

    if (rc != 0) {
        ai_mcp_return_value_set_str(ret_val, "Error: failed to dispatch to host bridge");
        return OPRT_COM_ERROR;
    }

    char status[32] = "";
    char result_buf[256] = "";
    int wait_rc = desk_sdk_bridge_wait_tool_response(req_id, 2500, status, sizeof(status), result_buf, sizeof(result_buf));
    if (wait_rc == 0 && (strcmp(status, "succeeded") == 0 || strcmp(status, "completed") == 0)) {
        char reply[640];
        snprintf(reply, sizeof(reply), "Home Assistant service call executed successfully: %s",
                 result_buf[0] ? result_buf : "{\"success\":true}");
        ai_mcp_return_value_set_str(ret_val, reply);
        return OPRT_OK;
    } else if (wait_rc == 0 && (strcmp(status, "failed") == 0 || strcmp(status, "error") == 0)) {
        char reply[640];
        snprintf(reply, sizeof(reply), "Home Assistant service call failed: %s",
                 result_buf[0] ? result_buf : "unknown error");
        ai_mcp_return_value_set_str(ret_val, reply);
        return OPRT_COM_ERROR;
    } else {
        ai_mcp_return_value_set_str(ret_val,
            "Home Assistant service call failed: response timed out or host unreachable.");
        return OPRT_COM_ERROR;
    }
}

static OPERATE_RET __tool_ha_get_state(const MCP_PROPERTY_LIST_T *properties,
                                       MCP_RETURN_VALUE_T *ret_val,
                                       void *user_data)
{
    (void)user_data;
    const char *entity_id = __get_str_prop(properties, "entity_id");
    if (!entity_id) {
        ai_mcp_return_value_set_str(ret_val, "Error: 'entity_id' is required");
        return OPRT_INVALID_PARM;
    }

    cJSON *root = cJSON_CreateObject();
    if (!root) return OPRT_MALLOC_FAILED;
    char req_id[48];
    __gen_mcp_req_id("hag", req_id, sizeof(req_id));
    cJSON_AddStringToObject(root, "request_id", req_id);
    cJSON_AddStringToObject(root, "source", "tclaw_cloud_llm");
    cJSON_AddStringToObject(root, "operation", "ha.get_state");

    cJSON *args = cJSON_CreateObject();
    cJSON_AddStringToObject(args, "entity_id", entity_id);
    cJSON_AddItemToObject(root, "arguments", args);

    char *env_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!env_str) return OPRT_MALLOC_FAILED;

    int rc = desk_sdk_bridge_dispatch_tool_request(env_str);
    cJSON_free(env_str);
    if (rc != 0) return OPRT_COM_ERROR;

    char status[32] = "";
    char result_buf[256] = "";
    int wait_rc = desk_sdk_bridge_wait_tool_response(req_id, 2000, status, sizeof(status), result_buf, sizeof(result_buf));
    if (wait_rc == 0 && (strcmp(status, "succeeded") == 0 || strcmp(status, "completed") == 0)) {
        ai_mcp_return_value_set_str(ret_val, result_buf[0] ? result_buf : "{\"state\":\"unknown\"}");
        return OPRT_OK;
    }
    ai_mcp_return_value_set_str(ret_val, "Unable to query entity state from Home Assistant.");
    return OPRT_COM_ERROR;
}

static OPERATE_RET __tool_codex_submit(const MCP_PROPERTY_LIST_T *properties,
                                       MCP_RETURN_VALUE_T *ret_val,
                                       void *user_data)
{
    (void)user_data;
    const char *prompt = __get_str_prop(properties, "prompt");
    const char *workspace = __get_str_prop(properties, "workspace_alias");
    if (!prompt || prompt[0] == '\0') {
        ai_mcp_return_value_set_str(ret_val, "Error: 'prompt' parameter is required");
        return OPRT_INVALID_PARM;
    }

    cJSON *root = cJSON_CreateObject();
    if (!root) return OPRT_MALLOC_FAILED;
    char req_id[48];
    __gen_mcp_req_id("codex", req_id, sizeof(req_id));
    cJSON_AddStringToObject(root, "request_id", req_id);
    cJSON_AddStringToObject(root, "source", "tclaw_cloud_llm");
    cJSON_AddStringToObject(root, "operation", "codex.submit_turn");

    cJSON *args = cJSON_CreateObject();
    cJSON_AddStringToObject(args, "prompt", prompt);
    cJSON_AddStringToObject(args, "workspace_alias", (workspace && workspace[0]) ? workspace : "epanda-desk-agent");
    cJSON_AddItemToObject(root, "arguments", args);

    char *env_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);

    if (!env_str) {
        ai_mcp_return_value_set_str(ret_val, "Error: out of memory");
        return OPRT_MALLOC_FAILED;
    }

    int rc = desk_sdk_bridge_dispatch_tool_request(env_str);
    cJSON_free(env_str);

    if (rc != 0) {
        ai_mcp_return_value_set_str(ret_val, "Error: failed to dispatch to host bridge");
        return OPRT_COM_ERROR;
    }

    char status[32] = "";
    char result_buf[256] = "";
    int wait_rc = desk_sdk_bridge_wait_tool_response(req_id, 1000, status, sizeof(status), result_buf, sizeof(result_buf));
    if (wait_rc == 0 && (strcmp(status, "accepted") == 0 || strcmp(status, "succeeded") == 0)) {
        ai_mcp_return_value_set_str(ret_val,
            "Task accepted by Codex Companion on PC. It will execute in background and notify you upon completion.");
        return OPRT_OK;
    } else if (wait_rc == 0 && (strcmp(status, "failed") == 0 || strcmp(status, "error") == 0)) {
        char reply[384];
        snprintf(reply, sizeof(reply), "Failed to submit task to Codex Companion: %s",
                 result_buf[0] ? result_buf : "unknown error");
        ai_mcp_return_value_set_str(ret_val, reply);
        return OPRT_COM_ERROR;
    }

    ai_mcp_return_value_set_str(ret_val,
        "Failed to submit task to Codex Companion: response timed out or host unreachable.");
    return OPRT_COM_ERROR;
}

static OPERATE_RET __tool_reminder_create(const MCP_PROPERTY_LIST_T *properties,
                                         MCP_RETURN_VALUE_T *ret_val,
                                         void *user_data)
{
    (void)user_data;
    const char *title = __get_str_prop(properties, "title");
    const char *due_time = __get_str_prop(properties, "due_time");
    if (!title || title[0] == '\0') {
        ai_mcp_return_value_set_str(ret_val, "Error: 'title' parameter is required");
        return OPRT_INVALID_PARM;
    }

    cJSON *root = cJSON_CreateObject();
    if (!root) return OPRT_MALLOC_FAILED;
    char req_id[48];
    __gen_mcp_req_id("rem", req_id, sizeof(req_id));
    cJSON_AddStringToObject(root, "request_id", req_id);
    cJSON_AddStringToObject(root, "source", "tclaw_cloud_llm");
    cJSON_AddStringToObject(root, "operation", "reminder.create");

    cJSON *args = cJSON_CreateObject();
    cJSON_AddStringToObject(args, "title", title);
    if (due_time && due_time[0]) {
        cJSON_AddStringToObject(args, "due_at", due_time);
        cJSON_AddStringToObject(args, "due_time", due_time);
    }
    cJSON_AddItemToObject(root, "arguments", args);

    char *env_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);

    if (!env_str) {
        ai_mcp_return_value_set_str(ret_val, "Error: out of memory");
        return OPRT_MALLOC_FAILED;
    }

    int rc = desk_sdk_bridge_dispatch_tool_request(env_str);
    cJSON_free(env_str);

    if (rc != 0) {
        ai_mcp_return_value_set_str(ret_val, "Error: failed to dispatch to host bridge");
        return OPRT_COM_ERROR;
    }

    char status[32] = "";
    char result_buf[256] = "";
    int wait_rc = desk_sdk_bridge_wait_tool_response(req_id, 2000, status, sizeof(status), result_buf, sizeof(result_buf));
    if (wait_rc == 0 && (strcmp(status, "succeeded") == 0 || strcmp(status, "completed") == 0)) {
        char reply[640];
        snprintf(reply, sizeof(reply), "Reminder scheduled successfully: %s",
                 result_buf[0] ? result_buf : title);
        ai_mcp_return_value_set_str(ret_val, reply);
        return OPRT_OK;
    } else if (wait_rc == 0 && (strcmp(status, "failed") == 0 || strcmp(status, "error") == 0)) {
        char reply[640];
        snprintf(reply, sizeof(reply), "Failed to schedule reminder: %s",
                 result_buf[0] ? result_buf : "unknown error");
        ai_mcp_return_value_set_str(ret_val, reply);
        return OPRT_COM_ERROR;
    } else {
        ai_mcp_return_value_set_str(ret_val, "Failed to schedule reminder: response timed out or host unreachable.");
        return OPRT_COM_ERROR;
    }
}

static OPERATE_RET __tool_reminder_list(const MCP_PROPERTY_LIST_T *properties,
                                       MCP_RETURN_VALUE_T *ret_val,
                                       void *user_data)
{
    (void)user_data;
    const char *status_filter = __get_str_prop(properties, "status");

    cJSON *root = cJSON_CreateObject();
    if (!root) return OPRT_MALLOC_FAILED;
    char req_id[48];
    __gen_mcp_req_id("reml", req_id, sizeof(req_id));
    cJSON_AddStringToObject(root, "request_id", req_id);
    cJSON_AddStringToObject(root, "source", "tclaw_cloud_llm");
    cJSON_AddStringToObject(root, "operation", "reminder.list");

    cJSON *args = cJSON_CreateObject();
    if (status_filter && status_filter[0]) {
        cJSON_AddStringToObject(args, "status", status_filter);
    }
    cJSON_AddItemToObject(root, "arguments", args);

    char *env_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!env_str) return OPRT_MALLOC_FAILED;

    int rc = desk_sdk_bridge_dispatch_tool_request(env_str);
    cJSON_free(env_str);
    if (rc != 0) return OPRT_COM_ERROR;

    char status[32] = "";
    char result_buf[256] = "";
    int wait_rc = desk_sdk_bridge_wait_tool_response(req_id, 2000, status, sizeof(status), result_buf, sizeof(result_buf));
    if (wait_rc == 0 && (strcmp(status, "succeeded") == 0 || strcmp(status, "completed") == 0)) {
        ai_mcp_return_value_set_str(ret_val, result_buf[0] ? result_buf : "{\"items\":[]}");
        return OPRT_OK;
    }
    ai_mcp_return_value_set_str(ret_val, "Unable to fetch reminders list.");
    return OPRT_COM_ERROR;
}

static OPERATE_RET __tool_reminder_delete(const MCP_PROPERTY_LIST_T *properties,
                                         MCP_RETURN_VALUE_T *ret_val,
                                         void *user_data)
{
    (void)user_data;
    const char *reminder_id = __get_str_prop(properties, "reminder_id");
    if (!reminder_id || reminder_id[0] == '\0') {
        ai_mcp_return_value_set_str(ret_val, "Error: 'reminder_id' is required");
        return OPRT_INVALID_PARM;
    }

    cJSON *root = cJSON_CreateObject();
    if (!root) return OPRT_MALLOC_FAILED;
    char req_id[48];
    __gen_mcp_req_id("remd", req_id, sizeof(req_id));
    cJSON_AddStringToObject(root, "request_id", req_id);
    cJSON_AddStringToObject(root, "source", "tclaw_cloud_llm");
    cJSON_AddStringToObject(root, "operation", "reminder.delete");

    cJSON *args = cJSON_CreateObject();
    cJSON_AddStringToObject(args, "reminder_id", reminder_id);
    cJSON_AddItemToObject(root, "arguments", args);

    char *env_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!env_str) return OPRT_MALLOC_FAILED;

    int rc = desk_sdk_bridge_dispatch_tool_request(env_str);
    cJSON_free(env_str);
    if (rc != 0) return OPRT_COM_ERROR;

    char status[32] = "";
    char result_buf[256] = "";
    int wait_rc = desk_sdk_bridge_wait_tool_response(req_id, 2000, status, sizeof(status), result_buf, sizeof(result_buf));
    if (wait_rc == 0 && (strcmp(status, "succeeded") == 0 || strcmp(status, "completed") == 0)) {
        ai_mcp_return_value_set_str(ret_val, "Reminder deleted successfully.");
        return OPRT_OK;
    }
    ai_mcp_return_value_set_str(ret_val, "Failed to delete reminder.");
    return OPRT_COM_ERROR;
}

static OPERATE_RET __tool_note_create(const MCP_PROPERTY_LIST_T *properties,
                                     MCP_RETURN_VALUE_T *ret_val,
                                     void *user_data)
{
    (void)user_data;
    const char *title = __get_str_prop(properties, "title");
    const char *content = __get_str_prop(properties, "content");
    if (!content || content[0] == '\0') {
        content = title;
    }
    if (!content || content[0] == '\0') {
        ai_mcp_return_value_set_str(ret_val, "Error: 'content' or 'title' is required");
        return OPRT_INVALID_PARM;
    }

    cJSON *root = cJSON_CreateObject();
    if (!root) return OPRT_MALLOC_FAILED;
    char req_id[48];
    __gen_mcp_req_id("note", req_id, sizeof(req_id));
    cJSON_AddStringToObject(root, "request_id", req_id);
    cJSON_AddStringToObject(root, "source", "tclaw_cloud_llm");
    cJSON_AddStringToObject(root, "operation", "note.create");

    cJSON *args = cJSON_CreateObject();
    cJSON_AddStringToObject(args, "title", (title && title[0]) ? title : "Voice Memo");
    cJSON_AddStringToObject(args, "content", content);
    cJSON_AddItemToObject(root, "arguments", args);

    char *env_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!env_str) return OPRT_MALLOC_FAILED;

    int rc = desk_sdk_bridge_dispatch_tool_request(env_str);
    cJSON_free(env_str);
    if (rc != 0) return OPRT_COM_ERROR;

    char status[32] = "";
    char result_buf[256] = "";
    int wait_rc = desk_sdk_bridge_wait_tool_response(req_id, 2000, status, sizeof(status), result_buf, sizeof(result_buf));
    if (wait_rc == 0 && (strcmp(status, "succeeded") == 0 || strcmp(status, "completed") == 0)) {
        ai_mcp_return_value_set_str(ret_val, "Note saved successfully.");
        return OPRT_OK;
    } else if (wait_rc == 0 && (strcmp(status, "failed") == 0 || strcmp(status, "error") == 0)) {
        char reply[384];
        snprintf(reply, sizeof(reply), "Failed to save note: %s",
                 result_buf[0] ? result_buf : "unknown error");
        ai_mcp_return_value_set_str(ret_val, reply);
        return OPRT_COM_ERROR;
    }
    ai_mcp_return_value_set_str(ret_val, "Failed to save note: response timed out or host unreachable.");
    return OPRT_COM_ERROR;
}

static OPERATE_RET __tool_note_list(const MCP_PROPERTY_LIST_T *properties,
                                   MCP_RETURN_VALUE_T *ret_val,
                                   void *user_data)
{
    (void)user_data;
    const char *query = __get_str_prop(properties, "query");

    cJSON *root = cJSON_CreateObject();
    if (!root) return OPRT_MALLOC_FAILED;
    char req_id[48];
    __gen_mcp_req_id("notel", req_id, sizeof(req_id));
    cJSON_AddStringToObject(root, "request_id", req_id);
    cJSON_AddStringToObject(root, "source", "tclaw_cloud_llm");
    cJSON_AddStringToObject(root, "operation", "note.list");

    cJSON *args = cJSON_CreateObject();
    if (query && query[0]) {
        cJSON_AddStringToObject(args, "query", query);
    }
    cJSON_AddItemToObject(root, "arguments", args);

    char *env_str = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (!env_str) return OPRT_MALLOC_FAILED;

    int rc = desk_sdk_bridge_dispatch_tool_request(env_str);
    cJSON_free(env_str);
    if (rc != 0) return OPRT_COM_ERROR;

    char status[32] = "";
    char result_buf[256] = "";
    int wait_rc = desk_sdk_bridge_wait_tool_response(req_id, 2000, status, sizeof(status), result_buf, sizeof(result_buf));
    if (wait_rc == 0 && (strcmp(status, "succeeded") == 0 || strcmp(status, "completed") == 0)) {
        ai_mcp_return_value_set_str(ret_val, result_buf[0] ? result_buf : "{\"items\":[]}");
        return OPRT_OK;
    }
    ai_mcp_return_value_set_str(ret_val, "Unable to query notes list.");
    return OPRT_COM_ERROR;
}

int desk_tclaw_runtime_register_mcp_tools(void)
{
    OPERATE_RET rt = OPRT_OK;
    PR_INFO("[TCLAW_RUNTIME] registering DeskAgent native MCP tools (ha, codex, reminder, note)...");

    TUYA_CALL_ERR_RETURN(AI_MCP_TOOL_ADD(
        "ha_execute",
        "Control smart home entities via Home Assistant (lights, switches, climate, covers, etc.).",
        __tool_ha_execute,
        NULL,
        MCP_PROP_STR("entity_id", "Home Assistant entity identifier, e.g. 'light.desk' or 'switch.heater'"),
        MCP_PROP_STR("action", "Action to perform, e.g. 'turn_on', 'turn_off', 'toggle'"),
        MCP_PROP_STR_DEF("parameters", "Optional JSON parameters for service call", "{}")
    ));

    TUYA_CALL_ERR_RETURN(AI_MCP_TOOL_ADD(
        "ha_get_state",
        "Get current state of a smart home entity in Home Assistant.",
        __tool_ha_get_state,
        NULL,
        MCP_PROP_STR("entity_id", "Home Assistant entity identifier, e.g. 'light.desk'")
    ));

    TUYA_CALL_ERR_RETURN(AI_MCP_TOOL_ADD(
        "codex_submit_turn",
        "Submit a coding instruction or development task to the Codex desktop companion.",
        __tool_codex_submit,
        NULL,
        MCP_PROP_STR("prompt", "Development instruction or prompt for Codex assistant"),
        MCP_PROP_STR_DEF("workspace_alias", "Target workspace alias", "epanda-desk-agent")
    ));

    TUYA_CALL_ERR_RETURN(AI_MCP_TOOL_ADD(
        "reminder_create",
        "Create a timed reminder or calendar alert on DeskAgent.",
        __tool_reminder_create,
        NULL,
        MCP_PROP_STR("title", "Content or summary of the reminder"),
        MCP_PROP_STR("due_time", "Target ISO-8601 timestamp for alert trigger")
    ));

    TUYA_CALL_ERR_RETURN(AI_MCP_TOOL_ADD(
        "reminder_list",
        "List upcoming or pending reminders on DeskAgent.",
        __tool_reminder_list,
        NULL,
        MCP_PROP_STR_DEF("status", "Status filter ('pending' or 'completed')", "pending")
    ));

    TUYA_CALL_ERR_RETURN(AI_MCP_TOOL_ADD(
        "reminder_delete",
        "Delete a reminder by ID.",
        __tool_reminder_delete,
        NULL,
        MCP_PROP_STR("reminder_id", "Reminder identifier to delete")
    ));

    TUYA_CALL_ERR_RETURN(AI_MCP_TOOL_ADD(
        "note_create",
        "Create a quick note, memo, or thought log on DeskAgent.",
        __tool_note_create,
        NULL,
        MCP_PROP_STR("title", "Title of the note"),
        MCP_PROP_STR("content", "Content of the note")
    ));

    TUYA_CALL_ERR_RETURN(AI_MCP_TOOL_ADD(
        "note_list",
        "List or search stored notes and memos.",
        __tool_note_list,
        NULL,
        MCP_PROP_STR_DEF("query", "Search keyword query", "")
    ));

    PR_INFO("[TCLAW_RUNTIME] native MCP tools registered successfully");
    return OPRT_OK;
}

static OPERATE_RET __desk_mcp_tools_on_mqtt(void *data)
{
    (void)data;
    return desk_tclaw_runtime_register_mcp_tools();
}

const desk_tclaw_runtime_stage_t *desk_tclaw_runtime_stages(unsigned *count)
{
    if (count) *count = STAGE_COUNT;
    return s_stages;
}

static const char *s_current_ai_preset = NULL;

static void __desk_ai_chat_event_handler(AI_NOTIFY_EVENT_T *event)
{
    if (!event) return;
    const char *new_preset = NULL;
    const char *status_text = NULL;

    switch (event->type) {
    case AI_USER_EVT_KEY_WAKEUP:
    case AI_USER_EVT_MIC_DATA:
        new_preset = "listening";
        status_text = "Listening...";
        break;
    case AI_USER_EVT_ASR_OK:
        new_preset = "thinking";
        status_text = "Thinking...";
        break;
    case AI_USER_EVT_TEXT_STREAM_START:
    case AI_USER_EVT_TTS_START:
    case AI_USER_EVT_TTS_DATA:
        new_preset = "speaking";
        status_text = "Speaking...";
        break;
    case AI_USER_EVT_TTS_STOP:
    case AI_USER_EVT_END:
        new_preset = "idle";
        status_text = "Ready";
        break;
    case AI_USER_EVT_ASR_ERROR:
    case AI_USER_EVT_TTS_ERROR:
        new_preset = "error";
        status_text = "Error";
        break;
    default:
        break;
    }

    if (new_preset && (s_current_ai_preset == NULL || strcmp(s_current_ai_preset, new_preset) != 0)) {
        s_current_ai_preset = new_preset;
        desk_hardware_set_screen_expression(new_preset, status_text);
    }
}

int desk_tclaw_runtime_init(void)
{
    int first_error = 0;
    int rc = sys_bus_init();
    record_stage(STAGE_SYS_BUS, rc == OPRT_OK ? DESK_TCLAW_RUNTIME_READY : DESK_TCLAW_RUNTIME_FAILED, rc);
    if (rc != OPRT_OK && first_error == 0) first_error = rc;


    rc = tclaw_chat_init();
    record_stage(STAGE_TCLAW_CHAT, rc == OPRT_OK ? DESK_TCLAW_RUNTIME_SUBSCRIBED : DESK_TCLAW_RUNTIME_FAILED, rc);
    if (rc == OPRT_OK) {
        tclaw_chat_register_event_hook(__desk_ai_chat_event_handler);
    }
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

    tal_event_subscribe(EVENT_MQTT_CONNECTED, "desk_mcp_tools", __desk_mcp_tools_on_mqtt, SUBSCRIBE_TYPE_ONETIME);

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
    if (pending_id && pending_id[0] != '\0' && strcmp(pending_id, request_id) == 0) {
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
    } else {
        /* Native MCP tool responses or unsolicited completions are recorded in s_req_table */
        rc = OPRT_OK;
    }

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
int desk_tclaw_runtime_register_mcp_tools(void) { return 0; }

#endif
