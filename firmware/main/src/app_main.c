/**
 * @file app_main.c
 * @brief Desk Agent R1 Firmware Main Entry Point.
 */

#include <stdio.h>
#include <string.h>
#include "desk_hardware.h"
#include "desk_tclaw_bridge.h"
#include "desk_sdk_bridge.h"

static int on_tclaw_tool_dispatch(const char *request_envelope_json, void *user_data)
{
    (void)user_data;
    return desk_sdk_bridge_dispatch_tool_request(request_envelope_json);
}

static int on_sdk_tool_response(const char *response_envelope_json, void *user_data)
{
    (void)user_data;
    return desk_tclaw_handle_tool_response("res-bridge", "succeeded", response_envelope_json, NULL);
}

int app_main(void)
{
    printf("========================================\n");
    printf("  Desk Agent R1 Firmware Booting...     \n");
    printf("  Target: ESP32-S3 | Baseline: IDF v5.4 \n");
    printf("========================================\n");

    /* 1. Initialize Board Hardware & Unified Resource Arbiter */
    if (desk_hardware_init() != 0) {
        printf("[ERROR] Failed to initialize desk hardware\n");
        return -1;
    }
    printf("[BOOT] Hardware drivers & Resource Arbiter initialized.\n");

    /* 2. Initialize SDK Compatibility Protocol Bridge */
    if (desk_sdk_bridge_init() != 0) {
        printf("[ERROR] Failed to initialize SDK bridge\n");
        return -1;
    }
    printf("[BOOT] WatcheRobot SDK Protocol Bridge initialized.\n");

    /* 3. Wire TClaw & SDK Bridge MCP Tool Channels */
    desk_tclaw_register_tool_dispatcher(on_tclaw_tool_dispatch, NULL);
    desk_sdk_bridge_register_tool_response_cb(on_sdk_tool_response, NULL);
    printf("[BOOT] TClaw <-> SDK Bridge MCP Tool Channels connected.\n");

    /* 4. Start SDK TCP Server (0.0.0.0:37020) and UDP Pairing Server (0.0.0.0:37021) */
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

    /* 5. Initialize & Start TClaw Core Dialogue Loop & Tuya Session */
    if (desk_tclaw_init() != 0 || desk_tclaw_start() != 0) {
        printf("[ERROR] Failed to start TClaw dialogue engine\n");
        return -1;
    }
    desk_tclaw_tuya_session_start("tuya-live-boot-session");
    printf("[BOOT] TClaw Dialogue Engine & Tuya Cloud Session started successfully.\n");

    /* Run initial poll check to verify I/O loop readiness */
    desk_firmware_poll(10);

    printf("[READY] Desk Agent R1 Ready for Voice & SDK Operations.\n");
    return 0;
}

#ifndef ESP_PLATFORM
/* Standard host main wrapper for Linux / test suite build */
int main(int argc, char **argv)
{
    int ret = app_main();
    if (ret != 0) return ret;

    /* If asked to poll in arguments, run poll loop */
    int poll_count = 0;
    if (argc > 1 && strcmp(argv[1], "--poll") == 0) {
        poll_count = 10;
    }
    for (int i = 0; i < poll_count; i++) {
        desk_firmware_poll(50);
    }

    /* Clean shutdown for test runs */
    desk_sdk_socket_server_stop();
    desk_sdk_pairing_server_stop();
    desk_tclaw_tuya_session_stop();
    return 0;
}
#endif
