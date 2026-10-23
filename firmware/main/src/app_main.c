/**
 * @file app_main.c
 * @brief Desk Agent R1 Firmware Main Entry Point.
 */

#include <stdio.h>
#include <string.h>
#include "desk_hardware.h"
#include "desk_watcher_log_source.h"
#include "desk_tclaw_bridge.h"
#include "desk_sdk_bridge.h"
#include "tuya_config.h"
#include "desk_cli.h"
#include "provisioning_state.h"
#include "tclaw_runtime_adapter.h"
#include "watcher_storage.h"

#ifdef ESP_PLATFORM
#include "tuya_iot.h"
#include "tuya_authorize.h"
#include "netmgr.h"
#include "netcfg.h"
#include "tal_api.h"
#include "cJSON.h"
#include "tkl_output.h"
#include "esp_heap_caps.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h"
#else
#include <stdbool.h>

typedef int OPERATE_RET;
#ifndef OPRT_OK
#define OPRT_OK 0
#endif
typedef enum {
    TUYA_EVENT_RESET = 0,
    TUYA_EVENT_BIND_START,
    TUYA_EVENT_BIND_TOKEN_ON,
    TUYA_EVENT_ACTIVATE_SUCCESSED,
    TUYA_EVENT_MQTT_CONNECTED,
    TUYA_EVENT_MQTT_DISCONNECT,
    TUYA_EVENT_DP_RECEIVE,
} tuya_event_id_t;

typedef struct {
    tuya_event_id_t id;
    void *value;
} tuya_event_msg_t;

typedef struct tuya_iot_client_handle tuya_iot_client_t;
typedef void (*event_handle_cb_t)(tuya_iot_client_t *client, tuya_event_msg_t *event);
typedef bool (*network_check_cb_t)(void);

typedef struct {
    char *uuid;
    char *authkey;
} tuya_iot_license_t;

typedef struct {
    const char *software_ver;
    const char *productkey;
    const char *uuid;
    const char *authkey;
    event_handle_cb_t event_handler;
    network_check_cb_t network_check;
} tuya_iot_config_t;

struct tuya_iot_client_handle {
    tuya_iot_config_t config;
    bool is_started;
};

typedef enum {
    NETCONN_AUTO = 1 << 0,
    NETCONN_WIFI = 1 << 1,
} netmgr_type_e;

typedef enum {
    NETCONN_CMD_NETCFG = 72,
} netmgr_conn_config_type_e;

typedef enum {
    NETCFG_TUYA_WIFI_AP = 1 << 0,
    NETCFG_TUYA_BLE = 1 << 1,
} netcfg_type_t;

typedef struct {
    netcfg_type_t type;
    char *uuid;
    char *pincode;
} netcfg_args_t;

static inline OPERATE_RET tuya_authorize_init(void) {
    return OPRT_OK;
}

static inline OPERATE_RET tuya_authorize_read(tuya_iot_license_t *license) {
    if (!license) return -1;
    license->uuid = "host-test-uuid";
    license->authkey = "host-test-authkey";
    return OPRT_OK;
}

static inline OPERATE_RET tuya_iot_init(tuya_iot_client_t *client, const tuya_iot_config_t *config) {
    if (!client || !config) return -1;
    client->config = *config;
    client->is_started = false;
    return OPRT_OK;
}

static inline OPERATE_RET netmgr_init(netmgr_type_e type) {
    (void)type;
    return OPRT_OK;
}

static inline OPERATE_RET netmgr_conn_set(netmgr_type_e type, netmgr_conn_config_type_e cmd, void *param) {
    (void)type; (void)cmd; (void)param;
    return OPRT_OK;
}

static inline OPERATE_RET tuya_iot_start(tuya_iot_client_t *client) {
    if (!client) return -1;
    client->is_started = true;
    return OPRT_OK;
}

static inline OPERATE_RET tuya_iot_yield(tuya_iot_client_t *client) {
    (void)client;
    return OPRT_OK;
}
#endif

#ifdef ESP_PLATFORM
#include "esp_netif.h"
#include "esp_event.h"
#include "esp_wifi.h"
#include "nvs_flash.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#endif

static tuya_iot_client_t s_tuya_client;
static tuya_iot_license_t s_tuya_license;
static desk_yield_retry_state_t s_yield_retry_state;

#ifdef ESP_PLATFORM
static void desk_tuya_log_output(const char *line)
{
    if (!line) return;
    if (desk_log_line_has_sensitive_marker(line)) {
        fputs("[SDK_LOG] sensitive log line suppressed\n", stdout);
        return;
    }
    /* This SDK's ESP32 tkl_log_output ignores its varargs. Use a literal
     * string sink so '%' in an already formatted SDK line stays inert. */
    fputs(line, stdout);
    desk_watcher_log_source_observe_tal_line(line);
}
#endif

static int desk_boot_check(const char *stage, int rc)
{
    if (rc == OPRT_OK) return 0;
    desk_provisioning_record_error(desk_provisioning_current_state(), stage, rc);
    desk_cli_update_provisioning_status(
        desk_provisioning_stage_name(desk_provisioning_current_state()->stage), stage, rc);
    printf("[BOOT][ERROR] stage=%s rc=%d\n", stage, rc);
    return -1;
}

static void tuya_event_handler_on(tuya_iot_client_t *client, tuya_event_msg_t *event)
{
    (void)client;
    if (!event) return;
    switch (event->id) {
    case TUYA_EVENT_BIND_START:
        desk_provisioning_apply_event(desk_provisioning_current_state(), DESK_PROVISIONING_EVENT_BIND_START);
        desk_cli_update_provisioning_status(desk_provisioning_stage_name(desk_provisioning_current_state()->stage),
                                            desk_provisioning_current_state()->error_stage,
                                            desk_provisioning_current_state()->error_code);
        printf("[TUYA][EVENT] stage=%s\n", desk_provisioning_stage_name(desk_provisioning_current_state()->stage));
        desk_tclaw_on_tuya_netcfg_started();
        break;

    case TUYA_EVENT_BIND_TOKEN_ON:
        desk_provisioning_apply_event(desk_provisioning_current_state(), DESK_PROVISIONING_EVENT_TOKEN_RECEIVED);
        desk_cli_update_provisioning_status(desk_provisioning_stage_name(desk_provisioning_current_state()->stage),
                                            desk_provisioning_current_state()->error_stage,
                                            desk_provisioning_current_state()->error_code);
        printf("[TUYA][EVENT] stage=%s\n", desk_provisioning_stage_name(desk_provisioning_current_state()->stage));
        break;

    case TUYA_EVENT_ACTIVATE_SUCCESSED:
        desk_provisioning_apply_event(desk_provisioning_current_state(), DESK_PROVISIONING_EVENT_ACTIVATED);
        desk_cli_update_provisioning_status(desk_provisioning_stage_name(desk_provisioning_current_state()->stage),
                                            desk_provisioning_current_state()->error_stage,
                                            desk_provisioning_current_state()->error_code);
        printf("[TUYA][EVENT] stage=%s\n", desk_provisioning_stage_name(desk_provisioning_current_state()->stage));
        break;

    case TUYA_EVENT_MQTT_CONNECTED:
        desk_provisioning_apply_event(desk_provisioning_current_state(), DESK_PROVISIONING_EVENT_MQTT_ONLINE);
        desk_cli_update_provisioning_status(desk_provisioning_stage_name(desk_provisioning_current_state()->stage),
                                            desk_provisioning_current_state()->error_stage,
                                            desk_provisioning_current_state()->error_code);
        printf("[TUYA][EVENT] stage=%s\n", desk_provisioning_stage_name(desk_provisioning_current_state()->stage));
#ifdef ESP_PLATFORM
        /* This is an observed network event. The native ai_chat/ai_agent event
         * subscribers own cloud audio; do not start the host dialogue shim. */
        desk_tclaw_runtime_set_cloud_online(1);
#else
        desk_tclaw_on_tuya_mqtt_connected();
#endif
        break;

    case TUYA_EVENT_MQTT_DISCONNECT:
        desk_provisioning_apply_event(desk_provisioning_current_state(), DESK_PROVISIONING_EVENT_MQTT_OFFLINE);
        desk_cli_update_provisioning_status(desk_provisioning_stage_name(desk_provisioning_current_state()->stage),
                                            desk_provisioning_current_state()->error_stage,
                                            desk_provisioning_current_state()->error_code);
        printf("[TUYA][EVENT] stage=%s\n", desk_provisioning_stage_name(desk_provisioning_current_state()->stage));
#ifdef ESP_PLATFORM
        desk_tclaw_runtime_set_cloud_online(0);
#else
        desk_tclaw_on_tuya_mqtt_disconnected();
#endif
        break;

    case TUYA_EVENT_RESET:
        printf("[TUYA][EVENT] sdk_reset\n");
        break;

    default:
        break;
    }
}

static bool tuya_network_check_cb(void)
{
#ifdef ESP_PLATFORM
    netmgr_status_e status = NETMGR_LINK_DOWN;
    return netmgr_conn_get(NETCONN_AUTO, NETCONN_CMD_STATUS, &status) == OPRT_OK &&
           status != NETMGR_LINK_DOWN;
#else
    return false;
#endif
}

#ifndef ESP_PLATFORM
int desk_tuya_simulate_event(int event_id)
{
    tuya_event_msg_t msg;
    memset(&msg, 0, sizeof(msg));
    msg.id = (tuya_event_id_t)event_id;
    if (s_tuya_client.config.event_handler) {
        s_tuya_client.config.event_handler(&s_tuya_client, &msg);
        return 0;
    }
    return -1;
}

const char *desk_tuya_get_pid(void)
{
    return TUYA_PRODUCT_ID;
}

const char *desk_tuya_get_uuid(void)
{
    return s_tuya_license.uuid ? s_tuya_license.uuid : "";
}

const char *desk_tuya_get_authkey(void)
{
    return s_tuya_license.authkey ? s_tuya_license.authkey : "";
}
#endif

static int on_tclaw_tool_dispatch(const char *request_envelope_json, void *user_data)
{
    (void)user_data;
    return desk_sdk_bridge_dispatch_tool_request(request_envelope_json);
}

static int on_sdk_tool_response(const char *response_envelope_json, void *user_data)
{
    (void)user_data;
    return desk_tclaw_runtime_handle_bridge_response(response_envelope_json);
}

static int desk_agent_main_worker(void)
{
    printf("========================================\n");
    printf("  Desk Agent R1 Firmware Booting...     \n");
    printf("  Target: ESP32-S3 | Baseline: IDF v5.4 \n");
    printf("========================================\n");

    /* 1. Initialize Board Hardware & Unified Resource Arbiter */
    int hardware_rc = desk_hardware_init();
    if (hardware_rc != 0) {
        printf("[BOOT][WARN] stage=hardware_init rc=%d; continuing with available provisioning services\n", hardware_rc);
    } else {
        printf("[BOOT] Hardware drivers & Resource Arbiter initialized.\n");
    }

    /* Display is optional for provisioning; never claim an absent panel is verified. */
    desk_display_self_test_t disp_test;
    int disp_rc = desk_hardware_display_self_test(&disp_test);
    if (disp_rc != DESK_HW_OK) {
        printf("[BOOT][WARN] stage=display_unavailable rc=%d; continuing without display\n", disp_rc);
    } else {
        printf("[BOOT] stage=display_status_available\n");
    }

    /* 3. Initialize TClaw Core Dialogue Engine */
    if (desk_tclaw_init() != 0) {
        printf("[ERROR] Failed to initialize TClaw dialogue engine\n");
        return -1;
    }
    printf("[BOOT] TClaw Core Dialogue Engine initialized.\n");

    /* 4. Initialize SDK Compatibility Protocol Bridge */
    if (desk_sdk_bridge_init() != 0) {
        printf("[ERROR] Failed to initialize SDK bridge\n");
        return -1;
    }
    printf("[BOOT] WatcheRobot SDK Protocol Bridge initialized.\n");

    /* 5. Wire TClaw & SDK Bridge MCP Tool Channels AFTER both are initialized */
    desk_tclaw_register_tool_dispatcher(on_tclaw_tool_dispatch, NULL);
    desk_sdk_bridge_register_tool_response_cb(on_sdk_tool_response, NULL);
    printf("[BOOT] TClaw <-> SDK Bridge MCP Tool Channels connected.\n");

    desk_provisioning_state_init(desk_provisioning_current_state());
    desk_cli_update_provisioning_status("idle", "none", 0);
#ifdef ESP_PLATFORM
    /* Match TuyaOpen switch_demo: prepare NVS, cJSON allocator, time and logs
     * before services that allocate, log, or timestamp from TAL. */
    esp_err_t nvs_rc = nvs_flash_init();
    if (nvs_rc != ESP_OK) {
        return desk_boot_check("nvs_init", (int)nvs_rc);
    }

    /* JSON is task-context data; retain internal SRAM for radio/DMA. Hooks are
     * installed once before the SDK or native TClaw can allocate JSON. */
    cJSON_InitHooks(&(cJSON_Hooks){.malloc_fn = tal_psram_malloc, .free_fn = tal_psram_free});
    if (desk_boot_check("tal_time_service_init", tal_time_service_init()) != 0) return -1;
    if (desk_boot_check("tal_log_init", tal_log_init(TAL_LOG_LEVEL_INFO, 1024,
                                                       desk_tuya_log_output)) != 0) return -1;
    if (desk_boot_check("tal_sw_timer_init", tal_sw_timer_init()) != 0) return -1;
    if (desk_boot_check("tal_workq_init", tal_workq_init()) != 0) return -1;

    /* The official SDK owns the sole UART/CLI worker and system registry. */
    bool cli_ready = false;
    int cli_rc = tal_cli_init();
    if (cli_rc != OPRT_OK) {
        printf("[BOOT][WARN] stage=tal_cli_init rc=%d\n", cli_rc);
    } else {
        int desk_cli_rc = desk_cli_register_commands();
        if (desk_cli_rc != OPRT_OK) {
            printf("[BOOT][WARN] stage=desk_cli_register rc=%d\n", desk_cli_rc);
        } else {
            cli_ready = true;
        }
    }
#else
    if (desk_cli_init() != 0) {
        printf("[BOOT][WARN] stage=host_cli_init\n");
    } else {
        /* Host fixture only. Production readiness is based on TAL registration. */
        printf("[BOOT] stage=host_cli_ready\n");
    }
#endif
#ifdef ESP_PLATFORM
    printf("[BOOT] stage=cli_ready cli_ready=%d\n", cli_ready ? 1 : 0);
    /* Persistent-storage failure must leave the physical diagnostic console
     * available. Do not erase or remap any factory credential partition. */
    if (desk_boot_check("watcher_storage_init", desk_watcher_storage_init()) != 0) return -1;
    if (desk_boot_check("tal_kv_init", tal_kv_init(&(tal_kv_cfg_t){
            .seed = "vmlkasdh93dlvlcy",
            .key  = "dflfuap134ddlduq",
        })) != 0) return -1;
#endif

    int authorize_init_rc = tuya_authorize_init();
    if (desk_boot_check("tuya_authorize_init", authorize_init_rc) != 0) return -1;
    int authorize_rc = tuya_authorize_read(&s_tuya_license);
    if (authorize_rc != OPRT_OK || !s_tuya_license.uuid || s_tuya_license.uuid[0] == '\0' ||
        !s_tuya_license.authkey || s_tuya_license.authkey[0] == '\0') {
        int license_read_error = authorize_rc != OPRT_OK ? authorize_rc : -1;
        desk_provisioning_record_error(desk_provisioning_current_state(), "authorize_read", license_read_error);
        desk_cli_update_provisioning_status("idle", "authorize_read", license_read_error);
        printf("[BOOT][WARN] stage=authorize_read rc=%d; using local configured license\n", license_read_error);
        s_tuya_license.uuid = (char *)TUYA_OPENSDK_UUID;
        s_tuya_license.authkey = (char *)TUYA_OPENSDK_AUTHKEY;
    }
    if (!s_tuya_license.uuid || s_tuya_license.uuid[0] == '\0' ||
        !s_tuya_license.authkey || s_tuya_license.authkey[0] == '\0') {
        return desk_boot_check("license_unavailable", -1);
    }

    const tuya_iot_config_t iot_cfg = {
        .software_ver = "1.0.0",
        .productkey = TUYA_PRODUCT_ID,
        .uuid = s_tuya_license.uuid,
        .authkey = s_tuya_license.authkey,
        .event_handler = tuya_event_handler_on,
        .network_check = tuya_network_check_cb,
    };
    if (desk_boot_check("tuya_iot_init", tuya_iot_init(&s_tuya_client, &iot_cfg)) != 0) return -1;
    printf("[BOOT] stage=tuya_iot_initialized\n");
#ifdef ESP_PLATFORM
    printf("[BOOT] stage=radio_memory internal_free=%u largest=%u\n",
           (unsigned)heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT),
           (unsigned)heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
#endif

    if (desk_boot_check("netmgr_init", netmgr_init(NETCONN_WIFI)) != 0) return -1;
    const netcfg_args_t netcfg_args = {
        .type = NETCFG_TUYA_BLE | NETCFG_TUYA_WIFI_AP,
    };
    if (desk_boot_check("netcfg_configure",
                        netmgr_conn_set(NETCONN_WIFI, NETCONN_CMD_NETCFG, (void *)&netcfg_args)) != 0) return -1;
    printf("[BOOT] stage=netcfg_configured mode=BLE_AP\n");

    /* Set screen expression: Waiting Net (idle) */
    desk_hardware_set_screen_expression("idle", "Ready - Waiting Net");

    /* 7. Start SDK TCP Server (0.0.0.0:37020) and UDP Pairing Server (0.0.0.0:37021) */
    if (desk_sdk_socket_server_start(37020) != 0) {
        printf("[WARN] SDK TCP Server on 37020 could not bind (in use or non-root)\n");
    } else {
        printf("[BOOT] WatcheRobot SDK TCP Server listening on 0.0.0.0:%d\n", desk_sdk_socket_server_get_port());
    }

    if (desk_sdk_pairing_server_start(37021) != 0) {
        printf("[WARN] SDK UDP Pairing Server on 37021 could not bind\n");
    } else {
        printf("[BOOT] WatcheRobot SDK UDP Pairing Server listening on 0.0.0.0:%d\n", desk_sdk_pairing_server_get_port());
    }

    /* Production uses the pinned native TClaw stack. Host tests retain the
     * local dialogue shim so their deterministic L2 behavior remains stable. */
#ifdef ESP_PLATFORM
    int runtime_rc = desk_tclaw_runtime_init();
    if (runtime_rc != OPRT_OK) {
        printf("[BOOT][WARN] stage=tclaw_runtime_init rc=%d; provisioning remains available\n", runtime_rc);
    }
#else
    if (desk_tclaw_start() != 0) {
        printf("[ERROR] Failed to start TClaw dialogue engine\n");
        return -1;
    }
    printf("[BOOT] stage=host_desk_tclaw_started state=%d\n", (int)desk_tclaw_get_state());
#endif

    /* All independent TClaw subscribers are installed before the single Tuya
     * IoT client starts dispatching events. Runtime failures remain diagnosable
     * while provisioning and the CLI stay alive. */
    if (desk_boot_check("tuya_iot_start", tuya_iot_start(&s_tuya_client)) != 0) return -1;
    printf("[BOOT] stage=tuya_iot_started cli_ready=%d\n",
#ifdef ESP_PLATFORM
           cli_ready ? 1 : 0
#else
           1
#endif
    );

    /* 9. Start Persistent Background Polling Task (FreeRTOS task or pthread) */
    if (desk_firmware_start_background_task() != 0) {
        printf("[WARN] Background worker task could not be spawned, falling back to synchronous poll\n");
        desk_firmware_poll(10);
    } else {
        printf("[BOOT] Persistent firmware background polling worker task started.\n");
    }

    /* Enforce safe boot post-conditions. A stored authorization may reconnect
     * during startup, so cloud-disconnected is not a boot invariant. */
#ifndef ESP_PLATFORM
    if (desk_tclaw_get_state() != TCLAW_STATE_IDLE) {
        printf("[ERROR] Safety violation: TClaw not IDLE at boot!\n");
        return -1;
    }
#endif
    for (int r = 0; r < DESK_RES_MAX; r++) {
#ifdef ESP_PLATFORM
        /* Native TClaw deliberately owns audio while its capture worker is
         * open, and display rendering can briefly own the UI arbiter while
         * startup events update the screen. Neither transient lease should
         * abort the only Tuya IoT yield loop before provisioning. */
        if (r == DESK_RES_AUDIO_RECORD || r == DESK_RES_AUDIO_PLAYBACK ||
            r == DESK_RES_DISPLAY_UI) continue;
#endif
        if (desk_arbiter_get_owner((desk_resource_type_t)r) != DESK_OWNER_NONE) {
            printf("[ERROR] Safety violation: Resource arbiter %d not in clean state at boot!\n", r);
            return -1;
        }
    }

    printf("[READY] Provisioning stack started; waiting for observed Tuya lifecycle events.\n");
#ifdef ESP_PLATFORM
    printf("[BOOT] stage=stack_status app_task_stack_remaining=%lu B\n",
           (unsigned long)uxTaskGetStackHighWaterMark(NULL) * sizeof(StackType_t));
    /* Deterministic lifecycle supervisory loop for main task */
    while (1) {
        int yield_rc = tuya_iot_yield(&s_tuya_client);
        bool should_log = false;
        unsigned delay_ms = 0;
        /* The client pointer is known non-NULL and start succeeded. Errors from
         * state operations can reuse SDK error codes, so preserve SDK retries. */
        bool fatal = false;
        if (!desk_yield_retry_step(&s_yield_retry_state, yield_rc, OPRT_OK,
                                   fatal, &should_log, &delay_ms)) {
            return desk_boot_check("tuya_iot_yield", yield_rc);
        }
        if (should_log) {
            printf("[TUYA][WARN] stage=yield rc=%d retry_in_ms=%u consecutive=%u\n",
                   yield_rc, delay_ms, s_yield_retry_state.consecutive_errors);
        }
        (void)desk_hardware_ui_overlay_process_expiry();
        vTaskDelay(pdMS_TO_TICKS(delay_ms ? delay_ms : 50));
    }
#endif
    return 0;
}

#ifdef ESP_PLATFORM
static TaskHandle_t s_app_task_handle = NULL;

uint32_t desk_app_task_stack_hwm(void)
{
    if (s_app_task_handle != NULL) {
        return (uint32_t)uxTaskGetStackHighWaterMark(s_app_task_handle) * sizeof(StackType_t);
    }
    return 0;
}

static void desk_app_task(void *pvParameters)
{
    (void)pvParameters;
    int rc = desk_agent_main_worker();
    if (rc != 0) {
        printf("[ERROR] desk_agent_main_worker exited with code %d\n", rc);
    }
    vTaskDelete(NULL);
}
#else
uint32_t desk_app_task_stack_hwm(void) { return 0; }
#endif

int app_main(void)
{
#ifdef ESP_PLATFORM
    /* Spawn dedicated FreeRTOS application task with 8KB stack in INTERNAL SRAM.
     * Retaining an internal SRAM stack guarantees that Flash/NVS/KV operations (which
     * temporarily disable the SPI flash cache bus) do not trigger cache-disabled access faults.
     * Sized at 8KB (down from 16KB) while allowing main_task (CONFIG_ESP_MAIN_TASK_STACK_SIZE = 3584 B)
     * to exit and return its stack memory to the internal heap upon app_main() return. */
    BaseType_t task_rc = xTaskCreateWithCaps(desk_app_task, "desk_app_task", 8192, NULL, 5,
                                            &s_app_task_handle, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (task_rc != pdPASS) {
        printf("[ERROR] Failed to spawn desk_app_task in internal SRAM (rc=%d), falling back to synchronous execution\n", (int)task_rc);
        return desk_agent_main_worker();
    }

    /* Returning from app_main() allows ESP-IDF to automatically delete the main supervisory task
     * (main_task), returning its stack memory (3,584 B) back to internal SRAM. */
    return 0;
#else
    return desk_agent_main_worker();
#endif
}

#ifndef ESP_PLATFORM
/* Standard host main wrapper for Linux / test suite build */
int main(int argc, char **argv)
{
    int ret = app_main();
    if (ret != 0) return ret;

    /* If asked to poll in arguments, sleep while background worker processes events */
    int poll_ms = 0;
    if (argc > 1 && strcmp(argv[1], "--poll") == 0) {
        poll_ms = 500;
    }
    if (poll_ms > 0) {
        for (int i = 0; i < poll_ms / 50; i++) {
            int yield_rc = tuya_iot_yield(&s_tuya_client);
            bool should_log = false;
            unsigned delay_ms = 0;
            bool fatal = false;
            if (!desk_yield_retry_step(&s_yield_retry_state, yield_rc, OPRT_OK,
                                       fatal, &should_log, &delay_ms)) {
                return desk_boot_check("tuya_iot_yield", yield_rc);
            }
            if (should_log) {
                printf("[TUYA][WARN] stage=yield rc=%d retry_in_ms=%u consecutive=%u\n",
                       yield_rc, delay_ms, s_yield_retry_state.consecutive_errors);
            }
            desk_firmware_poll(delay_ms ? delay_ms : 50);
        }
    }

    /* Clean shutdown for test runs */
    desk_firmware_stop_background_task();
    desk_sdk_socket_server_stop();
    desk_sdk_pairing_server_stop();
    desk_tclaw_tuya_session_stop();
    return 0;
}
#endif
