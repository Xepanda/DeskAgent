/**
 * @file app_main.c
 * @brief Desk Agent R1 Firmware Main Entry Point.
 */

#include <stdio.h>
#include <string.h>
#include "desk_hardware.h"
#include "desk_tclaw_bridge.h"
#include "desk_sdk_bridge.h"

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

    /* 3. Initialize & Start TClaw Core Dialogue Loop */
    if (desk_tclaw_init() != 0 || desk_tclaw_start() != 0) {
        printf("[ERROR] Failed to start TClaw dialogue engine\n");
        return -1;
    }
    printf("[BOOT] TClaw Dialogue Engine & MCP Tools started successfully.\n");

    printf("[READY] Desk Agent R1 Ready for Voice & SDK Operations.\n");
    return 0;
}

#ifndef ESP_PLATFORM
/* Standard host main wrapper for Linux / test suite build */
int main(int argc, char **argv)
{
    return app_main();
}
#endif
