/**
 * @file desk_cli.c
 * @brief DeskAgent commands registered in the official TAL CLI registry.
 */

#include "desk_cli.h"
#ifdef ESP_PLATFORM
#include "tal_cli.h"
#include "tclaw_runtime_adapter.h"
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <ctype.h>
#include <errno.h>
#include <math.h>

#include "desk_hardware.h"
#include "desk_tclaw_bridge.h"
#include "desk_sdk_bridge.h"
#include "desk_watcher_input.h"
#include "desk_watcher_camera.h"
#include "desk_pairing_window.h"
#if defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)
#include "desk_watcher_audio.h"
#include "desk_watcher_motion.h"
#endif

#ifdef ESP_PLATFORM
#include "netmgr.h"
#include "esp_system.h"
#include "esp_app_desc.h"
#include "esp_random.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_heap_caps.h"
#include "esp_attr.h"
#define DESK_DIAG_BSS EXT_RAM_BSS_ATTR
#else
#define DESK_DIAG_BSS
#endif

#define MAX_CLI_COMMANDS 32
#define CLI_LINE_MAX_LEN 256
#define CLI_MAX_ARGS 8

static desk_cli_command_t s_cmd_table[MAX_CLI_COMMANDS];
static size_t s_cmd_count = 0;
static const char *s_provisioning_stage = "idle";
static const char *s_provisioning_error_stage = "none";
static int s_provisioning_error_code = 0;

static const char *cli_camera_capture_error_name(int rc)
{
    switch (rc) {
    case DESK_HW_ERR_UNAVAILABLE: return "unavailable";
    case DESK_HW_ERR_TIMEOUT: return "timeout";
    case DESK_HW_ERR_BUSY: return "busy";
    case DESK_HW_ERR_INVALID_ARG: return "invalid-arg";
    case DESK_WATCHER_CAMERA_ERR_PROTOCOL: return "protocol";
    case DESK_WATCHER_CAMERA_ERR_CRC: return "CRC";
    case DESK_WATCHER_CAMERA_ERR_SYNC_IO: return "SYNC I/O";
    case DESK_WATCHER_CAMERA_ERR_SYNC_TIMEOUT: return "SYNC-low timeout";
    case DESK_WATCHER_CAMERA_ERR_NOT_READY: return "not-ready";
    case DESK_WATCHER_CAMERA_ERR_SPI: return "SPI";
    case DESK_WATCHER_CAMERA_ERR_EMPTY: return "empty FIFO";
    case DESK_WATCHER_CAMERA_ERR_CONTROL: return "HX6538 control";
    case DESK_WATCHER_CAMERA_ERR_BUFFER_TOO_SMALL: return "buffer-too-small";
    default: return "unknown camera error";
    }
}

/* Forward declaration */
static void cmd_help(int argc, char *argv[]);
static void cmd_sys_log_enable(int argc, char *argv[]);
static void cmd_sys_version(int argc, char *argv[]);
static void cmd_sys_heap(int argc, char *argv[]);
static void cmd_system(int argc, char *argv[]);
static void cmd_display(int argc, char *argv[]);
static void cmd_audio(int argc, char *argv[]);
static void cmd_camera(int argc, char *argv[]);
static void cmd_body(int argc, char *argv[]);
static void cmd_peripherals(int argc, char *argv[]);
static void cmd_input(int argc, char *argv[]);
static void cmd_network(int argc, char *argv[]);
static void cmd_wifi(int argc, char *argv[]);
static void cmd_provisioning(int argc, char *argv[]);
static void cmd_pairing(int argc, char *argv[]);

static bool cli_parse_finite_float(const char *text, float *out_value)
{
    if (!text || !out_value || text[0] == '\0') return false;
    errno = 0;
    char *end = NULL;
    float value = strtof(text, &end);
    if (errno == ERANGE || end == text || *end != '\0' || !isfinite(value)) return false;
    *out_value = value;
    return true;
}

static bool cli_parse_duration_ms(const char *text, uint32_t *out_value)
{
    if (!text || !out_value || text[0] == '\0' || text[0] == '-') return false;
    errno = 0;
    char *end = NULL;
    unsigned long value = strtoul(text, &end, 10);
    if (errno == ERANGE || end == text || *end != '\0' || value > UINT32_MAX) return false;
    *out_value = (uint32_t)value;
    return true;
}

#ifdef ESP_PLATFORM
static esp_timer_handle_t s_pairing_expiry_timer = NULL;
static SemaphoreHandle_t s_pairing_mutex = NULL;
static desk_pairing_window_t s_pairing_window;
static bool s_pairing_window_initialized = false;

static uint32_t pairing_random_u32(void *context)
{
    (void)context;
    return esp_random();
}

static int pairing_set_code(const char *code, void *context)
{
    (void)context;
    return desk_sdk_bridge_set_pairing_code(code);
}

static int pairing_show_code(const char *code, void *context)
{
    (void)context;
    return desk_tclaw_pairing_ui_set_code(code);
}

static void pairing_clear_display(void *context)
{
    (void)context;
    desk_tclaw_pairing_ui_clear();
}

static int64_t pairing_monotonic_time_us(void *context)
{
    (void)context;
    return esp_timer_get_time();
}

static int pairing_start_timer(uint64_t duration_us, void *context)
{
    (void)context;
    return (int)esp_timer_start_once(s_pairing_expiry_timer, duration_us);
}

static void pairing_stop_timer(void *context)
{
    (void)context;
    (void)esp_timer_stop(s_pairing_expiry_timer);
}

static void pairing_lock(void *context)
{
    (void)context;
    if (s_pairing_mutex) (void)xSemaphoreTake(s_pairing_mutex, portMAX_DELAY);
}

static void pairing_unlock(void *context)
{
    (void)context;
    if (s_pairing_mutex) (void)xSemaphoreGive(s_pairing_mutex);
}

static const desk_pairing_window_ops_t s_pairing_window_ops = {
    .random_u32 = pairing_random_u32,
    .set_pairing_code = pairing_set_code,
    .show_pairing_code = pairing_show_code,
    .clear_display = pairing_clear_display,
    .monotonic_time_us = pairing_monotonic_time_us,
    .start_expiry_timer = pairing_start_timer,
    .stop_expiry_timer = pairing_stop_timer,
    .lock = pairing_lock,
    .unlock = pairing_unlock,
    .context = NULL,
};

static void pairing_window_expired(void *arg)
{
    (void)arg;
    desk_pairing_window_expire(&s_pairing_window, &s_pairing_window_ops);
}

static int pairing_expiry_timer_ensure(void)
{
    if (!s_pairing_mutex) {
        s_pairing_mutex = xSemaphoreCreateMutex();
        if (!s_pairing_mutex) return -1;
    }
    if (!s_pairing_window_initialized) {
        desk_pairing_window_init(&s_pairing_window);
        s_pairing_window_initialized = true;
    }
    if (s_pairing_expiry_timer) return 0;
    const esp_timer_create_args_t args = {
        .callback = pairing_window_expired,
        .arg = NULL,
        .dispatch_method = ESP_TIMER_TASK,
        .name = "pairing_expiry",
    };
    return (int)esp_timer_create(&args, &s_pairing_expiry_timer);
}

static int pairing_window_open(void)
{
    if (pairing_expiry_timer_ensure() != 0) return -1;
    return desk_pairing_window_open(&s_pairing_window, &s_pairing_window_ops);
}
#endif

static const desk_cli_command_t s_default_cmds[] = {
#ifndef ESP_PLATFORM
    {"help", "List all available diagnostic commands", cmd_help},
    {"sys_log_enable", "Set log state: sys_log_enable <on|off>", cmd_sys_log_enable},
    {"sys_version", "Display firmware build version and target info", cmd_sys_version},
    {"sys_heap", "Display current free internal heap and PSRAM", cmd_sys_heap},
#endif
    {"system", "Full system diagnostic overview (chip, heap, states)", cmd_system},
    {"display", "Display status or set expression: display [preset] [status]", cmd_display},
    {"audio", "Audio diagnostic: audio [status|sample|test|stop]", cmd_audio},
    {"camera", "Camera subsystem diagnostic: camera [status|capture]", cmd_camera},
    {"body", "STM32 control: body [status|move <pan> <tilt> [duration_ms]|stop]", cmd_body},
    {"peripherals", "Board peripherals status (RTC, LED, battery)", cmd_peripherals},
    {"input", "Read-only input driver status and event counters", cmd_input},
    {"network", "SDK TCP/UDP server status: network [status]", cmd_network},
    {"wifi", "Read-only Wi-Fi status: wifi [status]", cmd_wifi},
    {"provisioning", "Read-only Tuya provisioning state", cmd_provisioning},
    {"pairing", "Explicit SDK pairing window: pairing <open|close>", cmd_pairing},
};

#ifdef ESP_PLATFORM
/* TAL stores the command array pointer, so this registry must outlive startup. */
static cli_cmd_t s_sdk_commands[sizeof(s_default_cmds) / sizeof(s_default_cmds[0])];
#endif

int desk_cli_command_register(const desk_cli_command_t *cmd, uint8_t num)
{
    if (!cmd || num == 0) return -1;
    for (uint8_t i = 0; i < num; i++) {
        if (s_cmd_count >= MAX_CLI_COMMANDS) return -2;

        /* Check for existing command to update */
        bool found = false;
        for (size_t j = 0; j < s_cmd_count; j++) {
            if (strcmp(s_cmd_table[j].name, cmd[i].name) == 0) {
                s_cmd_table[j] = cmd[i];
                found = true;
                break;
            }
        }
        if (!found) {
            s_cmd_table[s_cmd_count++] = cmd[i];
        }
    }
    return 0;
}

static void cmd_help(int argc, char *argv[])
{
    (void)argc; (void)argv;
    printf("\r\n==================== TAL CLI COMMANDS ====================\r\n");
    for (size_t i = 0; i < s_cmd_count; i++) {
        printf("  %-16s : %s\r\n", s_cmd_table[i].name, s_cmd_table[i].help ? s_cmd_table[i].help : "");
    }
    printf("==========================================================\r\n");
}

static void cmd_sys_log_enable(int argc, char *argv[])
{
    if (argc > 1 && strcmp(argv[1], "off") == 0) {
        printf("sys_log_enable: off\r\n");
    } else {
        printf("sys_log_enable: on\r\n");
    }
}

static void cmd_sys_version(int argc, char *argv[])
{
    (void)argc; (void)argv;
#ifdef ESP_PLATFORM
    const esp_app_desc_t *desc = esp_app_get_description();
    if (!desc) {
        printf("Running application identity: UNKNOWN (app descriptor unavailable)\r\n");
        return;
    }
    printf("Running Application: project=%.*s version=%.*s\r\n",
           (int)sizeof(desc->project_name), desc->project_name,
           (int)sizeof(desc->version), desc->version);
    printf("Build: date=%.*s time=%.*s ESP-IDF=%.*s\r\n",
           (int)sizeof(desc->date), desc->date,
           (int)sizeof(desc->time), desc->time,
           (int)sizeof(desc->idf_ver), desc->idf_ver);
    printf("ELF SHA256 (not source SHA): ");
    for (size_t i = 0; i < sizeof(desc->app_elf_sha256); ++i) {
        printf("%02x", (unsigned)desc->app_elf_sha256[i]);
    }
    printf("\r\n");
#else
    printf("Running application identity: UNKNOWN (host simulation)\r\n");
#endif
}

extern uint32_t desk_app_task_stack_hwm(void);
static void cmd_sys_heap(int argc, char *argv[])
{
    (void)argc; (void)argv;
#ifdef ESP_PLATFORM
    uint32_t free_sram = esp_get_free_internal_heap_size();
    uint32_t largest_sram = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint32_t min_sram = heap_caps_get_minimum_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    uint32_t free_psram = heap_caps_get_free_size(MALLOC_CAP_SPIRAM);
    uint32_t total_free = esp_get_free_heap_size();
    printf("Free Heap: %lu bytes (Internal SRAM Free: %lu, Largest: %lu, Min: %lu | PSRAM Free: %lu)\r\n",
           (unsigned long)total_free, (unsigned long)free_sram,
           (unsigned long)largest_sram, (unsigned long)min_sram,
           (unsigned long)free_psram);
    printf("Task Stack Min Remaining: app_task=%lu B, poll_worker=%lu B\r\n",
           (unsigned long)desk_app_task_stack_hwm(),
           (unsigned long)desk_firmware_background_task_stack_hwm());
#else
    printf("Free Heap: Host Simulation (SRAM: OK, PSRAM: OK)\r\n");
#endif
}

static void cmd_system(int argc, char *argv[])
{
    (void)argc; (void)argv;
    printf("\r\n--- SYSTEM DIAGNOSTIC OVERVIEW ---\r\n");
    cmd_sys_version(0, NULL);
    cmd_sys_heap(0, NULL);

    /* TClaw Dialogue State */
    desk_tclaw_state_t st = desk_tclaw_get_state();
    const char *st_str = "UNKNOWN";
    switch (st) {
        case TCLAW_STATE_IDLE: st_str = "IDLE"; break;
        case TCLAW_STATE_LISTENING: st_str = "LISTENING"; break;
        case TCLAW_STATE_THINKING: st_str = "THINKING"; break;
        case TCLAW_STATE_SPEAKING: st_str = "SPEAKING"; break;
        case TCLAW_STATE_TOOL_CALLING: st_str = "TOOL_CALLING"; break;
        default: st_str = "UNKNOWN"; break;
    }
    printf("TClaw Core State    : %s\r\n", st_str);

    tuya_cloud_connection_state_t tuya_st = desk_tclaw_tuya_session_get_connection_state();
    const char *tuya_str = "DISCONNECTED";
    switch (tuya_st) {
        case TUYA_CLOUD_DISCONNECTED: tuya_str = "DISCONNECTED"; break;
        case TUYA_CLOUD_CONNECTING: tuya_str = "CONNECTING"; break;
        case TUYA_CLOUD_CONNECTED: tuya_str = "CONNECTED"; break;
        case TUYA_CLOUD_STREAMING: tuya_str = "STREAMING"; break;
        case TUYA_CLOUD_RECONNECTING: tuya_str = "RECONNECTING"; break;
        case TUYA_CLOUD_ERROR: tuya_str = "ERROR"; break;
        default: tuya_str = "UNKNOWN"; break;
    }
    printf("Tuya Local Session  : %s (shim state; not cloud connectivity proof)\r\n", tuya_str);
#ifdef ESP_PLATFORM
    /* Local bridge state above is not proof of native SDK/AI readiness. */
    const char *native_states[] = {"ready", "subscribed", "disabled", "failed", "blocked"};
    unsigned native_count=0;
    const desk_tclaw_runtime_stage_t *native=desk_tclaw_runtime_stages(&native_count);
    for (unsigned i=0; i<native_count; ++i) {
        unsigned state=(unsigned)native[i].state;
        printf("Native %-13s: %s rc=%d\r\n", native[i].name,
               state < 5 ? native_states[state] : "unknown", native[i].error_code);
    }
#endif

    /* SDK Bridge Server State */
    int tcp_port = desk_sdk_socket_server_get_port();
    int udp_port = desk_sdk_pairing_server_get_port();
    printf("SDK TCP Stream Port : %d (%s)\r\n", tcp_port,
           tcp_port <= 0 ? "NOT_LISTENING" :
           (desk_sdk_bridge_is_connected() ? "CLIENT CONNECTED" : "LISTENING"));
    printf("SDK UDP Pairing Port: %d (%s)\r\n", udp_port,
           udp_port <= 0 ? "NOT_LISTENING" : "LISTENING");

    /* No public arbiter readiness API exists; resource owners are not a health signal. */
    printf("Hardware Arbiter Readiness: UNKNOWN (no readiness/status API)\r\n");
    printf("Display Driver Initialization: %s (software flag only; physical scanout not exposed)\r\n",
           desk_hardware_is_display_initialized() ? "INITIALIZED" : "NOT_INITIALIZED");
    printf("----------------------------------\r\n");
}

static void cmd_display(int argc, char *argv[])
{
    if (argc >= 2) {
        const char *preset = argv[1];
        const char *status = (argc >= 3) ? argv[2] : "Active";
        int rc = desk_hardware_display_set_expression(preset, status);
        printf("Display set expression: preset='%s', status='%s', rc=%d\r\n", preset, status, rc);
    } else {
        char preset[32] = "";
        char status[64] = "";
        bool initialized = desk_hardware_is_display_initialized();
        printf("Display Driver Initialization: %s\r\n",
               initialized ? "INITIALIZED" : "NOT_INITIALIZED");
        printf("Display Physical Scanout: UNKNOWN (no physical scanout status API)\r\n");
        if (!initialized) {
            printf("Display Software Shadow: UNKNOWN (driver not initialized)\r\n");
            return;
        }
        int rc = desk_hardware_display_get_expression(preset, sizeof(preset), status, sizeof(status));
        if (rc != DESK_HW_OK) {
            printf("Display Software Shadow: UNKNOWN (readback rc=%d)\r\n", rc);
            return;
        }
        printf("Display Software Shadow: AVAILABLE (cached expression); preset='%s', status='%s'\r\n",
               preset, status);
    }
}

static void cmd_audio(int argc, char *argv[])
{
    if (argc >= 2 && strcmp(argv[1], "stop") == 0) {
        int rc = desk_hardware_audio_play_stop();
        printf("Audio playback stop rc=%d\r\n", rc);
        return;
    }
#if defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)
    if (argc >= 2 && strcmp(argv[1], "test") == 0) {
        printf("Audio 1000ms 1kHz output test rc=%d (audibility requires physical confirmation)\r\n",
               desk_watcher_audio_test_tone());
        return;
    }
    if (argc >= 2 && strcmp(argv[1], "sample") == 0) {
        int16_t samples[320];
        size_t count=0;
        int rc=desk_watcher_audio_read(samples, 320, &count);
        uint32_t sample_peak=0;
        for (size_t i=0; i<count; ++i) {
            int32_t value=samples[i];
            uint32_t amplitude=value < 0 ? -value : value;
            if (amplitude > sample_peak) sample_peak=amplitude;
        }
        printf("ADC sample rc=%d count=%u peak=%lu\r\n", rc, (unsigned)count, (unsigned long)sample_peak);
        return;
    }
#endif
    desk_hardware_audio_status_t audio_status = {0};
    int status_rc = desk_hardware_audio_get_status(&audio_status);
    if (status_rc == DESK_HW_OK) {
        printf("Audio status: %s; mic_running=%s frames=%lu errors=%lu peak=%lu\r\n",
               audio_status.ready ? "READY" : "NOT_READY",
               audio_status.running ? "YES" : "NO",
               (unsigned long)audio_status.frames, (unsigned long)audio_status.errors,
               (unsigned long)audio_status.peak);
    } else {
        printf("Audio status: UNKNOWN (driver status rc=%d)\r\n", status_rc);
    }
    printf("  Arbiter Playback Owner: %d\r\n", (int)desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK));
    printf("  Arbiter Record Owner  : %d\r\n", (int)desk_arbiter_get_owner(DESK_RES_AUDIO_RECORD));
}

static void cmd_camera(int argc, char *argv[])
{
    if (argc >= 2 && strcmp(argv[1], "capture") == 0) {
        static DESK_DIAG_BSS uint8_t jpeg_buf[8192];
        size_t out_len = 0;
        int rc = desk_hardware_camera_capture_jpeg(jpeg_buf, sizeof(jpeg_buf), &out_len, 5000);
        printf("Camera Capture: rc=%d (%s), valid_frame=%s, out_len=%lu bytes\r\n",
               rc, rc == DESK_HW_OK ? "ok" : cli_camera_capture_error_name(rc),
               rc == DESK_HW_OK && out_len > 0 ? "YES" : "NO", (unsigned long)out_len);
    } else {
        printf("Camera Subsystem:\r\n");
        printf("  Arbiter Camera Owner  : %d\r\n", (int)desk_arbiter_get_owner(DESK_RES_CAMERA_SENSOR));
#if defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)
        printf("  Capture Driver State  : %s\r\n",
               desk_watcher_camera_is_ready() ? "READY" : "NOT_INITIALIZED");
#else
        printf("  Capture Driver State  : UNKNOWN (no board capture status API)\r\n");
#endif
        printf("  HAL Cached Frame      : %s\r\n",
               desk_hardware_camera_has_frame() ? "PRESENT" : "NONE");
    }
}

static void cmd_body(int argc, char *argv[])
{
    if (argc >= 2 && strcmp(argv[1], "move") == 0) {
        float pan = 0.0f, tilt = 0.0f;
        uint32_t duration_ms = 500;
        if ((argc != 4 && argc != 5) ||
            !cli_parse_finite_float(argc > 2 ? argv[2] : NULL, &pan) ||
            !cli_parse_finite_float(argc > 3 ? argv[3] : NULL, &tilt) ||
            (argc == 5 && !cli_parse_duration_ms(argv[4], &duration_ms))) {
            printf("Usage: body move <pan_deg> <tilt_deg> [duration_ms]\r\n");
            return;
        }
        int rc;
#ifdef ESP_PLATFORM
        desk_arbiter_status_t arb = desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE, 50);
        if (arb != DESK_ARBITER_OK) {
            printf("Body move failed: rc=%d (accepted=false, RESOURCE_BUSY)\r\n", DESK_HW_ERR_BUSY);
            return;
        }
        rc = desk_hardware_motion_move_to(pan, tilt, duration_ms);
        desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE);
#else
        /* Host HAL is a simulation and must not report an accepted physical move. */
        rc = DESK_HW_ERR_UNAVAILABLE;
#endif
        if (rc == DESK_HW_OK) {
            printf("Body move accepted=true, state_confirmed=false (requested %.1f, %.1f deg; duration=%lu ms)\r\n",
                   (double)pan, (double)tilt, (unsigned long)duration_ms);
        } else {
            printf("Body move failed: rc=%d (accepted=false)\r\n", rc);
        }
        return;
    }
    if (argc == 2 && strcmp(argv[1], "stop") == 0) {
        int rc;
#ifdef ESP_PLATFORM
        rc = desk_hardware_motion_stop(true);
#else
        rc = DESK_HW_ERR_UNAVAILABLE;
#endif
        if (rc == DESK_HW_OK) {
            printf("Body emergency stop accepted=true; physical stop is unconfirmed.\r\n");
        } else {
            printf("Body emergency stop failed: rc=%d\r\n", rc);
        }
        return;
    }
    if (argc == 1 || (argc == 2 && strcmp(argv[1], "status") == 0)) {
        float pan = 0.0f, tilt = 0.0f;
        int position_rc = desk_hardware_motion_get_position(&pan, &tilt);
        uint8_t last_frame[64];
        size_t last_len = 0;
        int frame_rc = desk_hardware_get_last_mcu_frame(last_frame, sizeof(last_frame), &last_len);
        printf("STM32 Body Coprocessor:\r\n");
#if defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)
        printf("  UART Driver State: %s\r\n",
               desk_watcher_motion_is_ready() ? "READY" : "NOT_INITIALIZED");
#else
        printf("  UART Driver State: UNKNOWN (no board status API)\r\n");
#endif
        if (position_rc == DESK_HW_OK) {
            printf("  HAL cached setpoint: Pan=%.1f deg, Tilt=%.1f deg (not physical feedback)\r\n",
                   (double)pan, (double)tilt);
        } else {
            printf("  Setpoint: UNKNOWN (read rc=%d)\r\n", position_rc);
        }
        if (frame_rc == DESK_HW_OK) {
            printf("  Last formatted TX request length: %lu bytes (not an RX/ACK counter)\r\n",
                   (unsigned long)last_len);
        } else {
            printf("  Last formatted TX request length: UNKNOWN (read rc=%d)\r\n", frame_rc);
        }
        printf("  STM32 peer/ACK state: UNKNOWN (no independent peer status in CLI)\r\n");
        printf("  UART Protocol: A5 5A Class=0x02 Motion Frame\r\n");
        return;
    }
    printf("Usage: body [status|move <pan_deg> <tilt_deg> [duration_ms]|stop]\r\n");
}

static void cmd_peripherals(int argc, char *argv[])
{
    (void)argc; (void)argv;
    printf("Peripherals Status:\r\n");
    printf("  RTC state             : UNKNOWN (no read-only status API)\r\n");
    printf("  RGB LED state         : UNKNOWN (no readback API)\r\n");
    printf("  Battery state         : UNKNOWN (no ADC status API)\r\n");
    printf("  IO Expander state     : UNKNOWN (probe may initialize board power)\r\n");
}

static const char *input_state_name(desk_watcher_input_state_t state)
{
    switch (state) {
    case DESK_WATCHER_INPUT_INIT: return "INIT";
    case DESK_WATCHER_INPUT_READY: return "READY";
    case DESK_WATCHER_INPUT_ERROR: return "ERROR";
    case DESK_WATCHER_INPUT_UNKNOWN:
    default: return "UNKNOWN";
    }
}

static const char *input_target_name(desk_touch_target_t target)
{
    switch (target) {
    case DESK_TOUCH_TARGET_BACKTOUCH: return "BACKTOUCH";
    case DESK_TOUCH_TARGET_SCREENTOUCH: return "SCREENTOUCH";
    case DESK_TOUCH_TARGET_ROLLER: return "ROLLER";
    default: return "UNKNOWN";
    }
}

static const char *input_action_name(desk_touch_action_t action)
{
    switch (action) {
    case DESK_TOUCH_ACTION_SHORT_PRESS: return "SHORT_PRESS";
    case DESK_TOUCH_ACTION_LONG_PRESS: return "LONG_PRESS";
    case DESK_TOUCH_ACTION_SCROLL_UP: return "SCROLL_UP";
    case DESK_TOUCH_ACTION_SCROLL_DOWN: return "SCROLL_DOWN";
    default: return "UNKNOWN";
    }
}

static void cmd_input(int argc, char *argv[])
{
    (void)argc; (void)argv;
    printf("Input Status (read-only):\r\n");
#if defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)
    desk_watcher_input_status_t status = {0};
    int rc = desk_watcher_input_get_status(&status);
    if (rc != DESK_HW_OK) {
        printf("  Status read: UNKNOWN (rc=%d)\r\n", rc);
        return;
    }
    printf("  Knob  : %s events=%lu error_code=%d\r\n", input_state_name(status.knob.state),
           (unsigned long)status.knob.event_count, status.knob.error_code);
    printf("  Button: %s events=%lu error_code=%d\r\n", input_state_name(status.button.state),
           (unsigned long)status.button.event_count, status.button.error_code);
    printf("  Touch : %s events=%lu error_code=%d\r\n", input_state_name(status.touch.state),
           (unsigned long)status.touch.event_count, status.touch.error_code);
    if (status.last_event_valid) {
        printf("  Last event: target=%s action=%s\r\n", input_target_name(status.last_target),
               input_action_name(status.last_action));
    } else {
        printf("  Last event: UNKNOWN (none observed)\r\n");
    }
#else
    printf("  Knob  : UNKNOWN events=0 error_code=0\r\n");
    printf("  Button: UNKNOWN events=0 error_code=0\r\n");
    printf("  Touch : UNKNOWN events=0 error_code=0\r\n");
    printf("  Last event: UNKNOWN (input hardware status unavailable)\r\n");
#endif
}

static void cmd_network(int argc, char *argv[])
{
    (void)argc; (void)argv;
#ifdef ESP_PLATFORM
    netmgr_status_e status = NETMGR_LINK_DOWN;
    int rc = netmgr_conn_get(NETCONN_AUTO, NETCONN_CMD_STATUS, &status);
    const char *link_name = "UNKNOWN";
    if (rc == 0) {
        if (status == NETMGR_LINK_UP) link_name = "LINK_UP";
        else if (status == NETMGR_LINK_DOWN) link_name = "LINK_DOWN";
    }
    printf("Network status: rc=%d link=%s (%d)\r\n", rc, link_name, (int)status);
#else
    printf("Network status: host simulation\r\n");
#endif
    printf("Network & SDK Server Status:\r\n");
    printf("  TCP Server Port : %d\r\n", desk_sdk_socket_server_get_port());
    int tcp_port = desk_sdk_socket_server_get_port();
    int udp_port = desk_sdk_pairing_server_get_port();
    printf("  TCP Client State: %s\r\n", tcp_port <= 0 ? "UNKNOWN (server not listening)" :
           (desk_sdk_bridge_is_connected() ? "CONNECTED" : "WAITING"));
    printf("  UDP Pairing Port: %d (%s)\r\n", udp_port,
           udp_port <= 0 ? "NOT_LISTENING" : "LISTENING");
}

static void cmd_provisioning(int argc, char *argv[])
{
    (void)argc; (void)argv;
    printf("Provisioning stage: %s\r\n", s_provisioning_stage);
    printf("Last error: stage=%s rc=%d\r\n", s_provisioning_error_stage, s_provisioning_error_code);
}

static void cmd_pairing(int argc, char *argv[])
{
#ifdef ESP_PLATFORM
    if (argc == 2 && strcmp(argv[1], "open") == 0) {
        if (pairing_window_open() == 0) {
            printf("Pairing window open for 300 seconds; read the code from the device display.\r\n");
        } else {
            printf("Pairing window could not be opened; code cleared.\r\n");
        }
        return;
    }
    if (argc == 2 && (strcmp(argv[1], "close") == 0 || strcmp(argv[1], "cancel") == 0)) {
        if (pairing_expiry_timer_ensure() == 0) {
            desk_pairing_window_close(&s_pairing_window, &s_pairing_window_ops);
        } else {
            (void)desk_sdk_bridge_set_pairing_code(NULL);
            desk_tclaw_pairing_ui_clear();
            memset(&s_pairing_window, 0, sizeof(s_pairing_window));
        }
        printf("Pairing window closed; code cleared.\r\n");
        return;
    }
    printf("Usage: pairing <open|close>\r\n");
#else
    (void)argc; (void)argv;
    printf("Pairing window is available only on the device.\r\n");
#endif
}

static void cmd_wifi(int argc, char *argv[])
{
    (void)argc; (void)argv;
#ifdef ESP_PLATFORM
    netmgr_status_e status = NETMGR_LINK_DOWN;
    int rc = netmgr_conn_get(NETCONN_AUTO, NETCONN_CMD_STATUS, &status);
    const char *link_name = "UNKNOWN";
    if (rc == 0) {
        if (status == NETMGR_LINK_UP) link_name = "LINK_UP";
        else if (status == NETMGR_LINK_DOWN) link_name = "LINK_DOWN";
    }
    printf("Wi-Fi status: rc=%d link=%s (%d)\r\n", rc, link_name, (int)status);
#else
    printf("Wi-Fi status: UNKNOWN (host simulation; no radio state)\r\n");
#endif
}

int desk_cli_exec(const char *cmd_line)
{
    if (!cmd_line) return 0;

    size_t command_length = strlen(cmd_line);
    if (command_length == 0) return 0;
    if (command_length >= CLI_LINE_MAX_LEN) {
        printf("Command rejected: input exceeds %u bytes.\r\n", (unsigned)(CLI_LINE_MAX_LEN - 1));
        return -1;
    }

    char line_copy[CLI_LINE_MAX_LEN];
    strncpy(line_copy, cmd_line, sizeof(line_copy) - 1);
    line_copy[sizeof(line_copy) - 1] = '\0';

    /* Strip leading whitespace */
    char *p = line_copy;
    while (*p && isspace((unsigned char)*p)) p++;
    if (!*p) return 0;

    /* Parse argv */
    char *argv[CLI_MAX_ARGS];
    int argc = 0;

    while (*p && argc < CLI_MAX_ARGS) {
        argv[argc++] = p;
        while (*p && !isspace((unsigned char)*p)) p++;
        if (*p) {
            *p++ = '\0';
            while (*p && isspace((unsigned char)*p)) p++;
        }
    }

    if (*p) {
        printf("Command rejected: maximum %u arguments supported.\r\n", (unsigned)CLI_MAX_ARGS);
        return -1;
    }

    if (argc == 0) return 0;

    /* Find command */
    for (size_t i = 0; i < s_cmd_count; i++) {
        if (strcmp(s_cmd_table[i].name, argv[0]) == 0) {
            s_cmd_table[i].func(argc, argv);
            return 0;
        }
    }

    printf("Unknown command: '%s'. Type 'help' for available diagnostic commands.\r\n", argv[0]);
    return -1;
}

void desk_cli_echo(const char *string)
{
    if (string) printf("%s\r\n", string);
}

void desk_cli_update_provisioning_status(const char *stage, const char *error_stage, int error_code)
{
    s_provisioning_stage = stage ? stage : "unknown";
    s_provisioning_error_stage = error_stage ? error_stage : "none";
    s_provisioning_error_code = error_code;
}

int desk_cli_register_commands(void)
{
#ifdef ESP_PLATFORM
    for (size_t i = 0; i < sizeof(s_default_cmds) / sizeof(s_default_cmds[0]); ++i) {
        s_sdk_commands[i].name = (char *)s_default_cmds[i].name;
        s_sdk_commands[i].help = (char *)s_default_cmds[i].help;
        s_sdk_commands[i].func = s_default_cmds[i].func;
    }
    return tal_cli_cmd_register(s_sdk_commands, (uint8_t)(sizeof(s_sdk_commands) / sizeof(s_sdk_commands[0])));
#else
    return desk_cli_command_register(s_default_cmds,
                                     (uint8_t)(sizeof(s_default_cmds) / sizeof(s_default_cmds[0])));
#endif
}

#ifndef ESP_PLATFORM
int desk_cli_init(void)
{
    return desk_cli_register_commands();
}
#endif
