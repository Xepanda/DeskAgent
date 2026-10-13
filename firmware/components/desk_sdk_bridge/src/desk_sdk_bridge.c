/**
 * @file desk_sdk_bridge.c
 * @brief Implementation of WatcheRobot Protocol Compatibility Bridge.
 */

#include "desk_sdk_bridge.h"
#include "desk_hardware.h"
#include <stdio.h>
#include <string.h>
#include <stdlib.h>

static uint32_t s_event_seq = 0;
static bool s_client_connected = false;

int desk_sdk_bridge_init(void)
{
    s_event_seq = 0;
    s_client_connected = false;
    return 0;
}

int desk_sdk_bridge_process_command(const char *json_in, char *out_buf, size_t max_out_len)
{
    if (!json_in || !out_buf || max_out_len < 32) return -1;

    /* Handshake: sys.client.hello */
    if (strstr(json_in, "sys.client.hello") != NULL) {
        s_client_connected = true;
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{"
                 "\"role\":\"device\","
                 "\"protocol_version\":\"1.0\","
                 "\"capabilities\":[\"%s\",\"%s\",\"%s\",\"%s\",\"%s\"]}}",
                 DESK_SDK_CAP_INPUTS,
                 DESK_SDK_CAP_MOTION,
                 DESK_SDK_CAP_CAMERA,
                 DESK_SDK_CAP_AUDIO_STREAM,
                 DESK_SDK_CAP_EXPRESSION);
        return 0;
    }

    /* Motion control: ctrl.motion.move / resource.motion.move */
    if (strstr(json_in, "motion.move") != NULL) {
        /* Parse angles or set safe default */
        desk_hardware_motion_move_to(90.0f, 120.0f, 500);
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"succeeded\"}}");
        return 0;
    }

    /* Motion stop */
    if (strstr(json_in, "motion.stop") != NULL) {
        desk_hardware_motion_stop(false);
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.ack\",\"code\":0,\"data\":{\"status\":\"succeeded\"}}");
        return 0;
    }

    /* Ping / Heartbeat */
    if (strstr(json_in, "sys.ping") != NULL) {
        snprintf(out_buf, max_out_len,
                 "{\"type\":\"sys.pong\",\"code\":0,\"data\":{}}");
        return 0;
    }

    /* Default ACK */
    snprintf(out_buf, max_out_len,
             "{\"type\":\"sys.ack\",\"code\":0,\"data\":{}}");
    return 0;
}

int desk_sdk_bridge_send_event(const char *event_type, const char *payload_json)
{
    if (!event_type || !payload_json) return -1;
    s_event_seq++;
    return 0;
}

int desk_sdk_bridge_dispatch_tool_request(const char *request_envelope_json)
{
    if (!request_envelope_json) return -1;
    return 0;
}
