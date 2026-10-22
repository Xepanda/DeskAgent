/**
 * @file desk_watcher_input.h
 * @brief SenseCAP Watcher Touch (SPD2010 on I2C1) and Knob Encoder (GPIO 41/42) Driver.
 */

#ifndef DESK_WATCHER_INPUT_H
#define DESK_WATCHER_INPUT_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "desk_hardware.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
    DESK_WATCHER_INPUT_UNKNOWN = 0,
    DESK_WATCHER_INPUT_INIT,
    DESK_WATCHER_INPUT_READY,
    DESK_WATCHER_INPUT_ERROR,
} desk_watcher_input_state_t;

typedef struct {
    desk_watcher_input_state_t state;
    int error_code;
    uint32_t event_count;
} desk_watcher_input_channel_status_t;

typedef struct {
    desk_watcher_input_channel_status_t knob;
    desk_watcher_input_channel_status_t button;
    desk_watcher_input_channel_status_t touch;
    bool last_event_valid;
    desk_touch_target_t last_target;
    desk_touch_action_t last_action;
} desk_watcher_input_status_t;

int desk_watcher_input_init(void);
int desk_watcher_input_deinit(void);
bool desk_watcher_input_is_ready(void);
int desk_watcher_input_get_status(desk_watcher_input_status_t *status);

#ifdef __cplusplus
}
#endif

#endif /* DESK_WATCHER_INPUT_H */
