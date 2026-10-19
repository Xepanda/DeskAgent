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

int desk_watcher_input_init(void);
int desk_watcher_input_deinit(void);
bool desk_watcher_input_is_ready(void);

#ifdef __cplusplus
}
#endif

#endif /* DESK_WATCHER_INPUT_H */
