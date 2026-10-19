/**
 * @file desk_watcher_board.h
 * @brief SenseCAP Watcher native SPD2010 display adapter.
 */
#ifndef DESK_WATCHER_BOARD_H
#define DESK_WATCHER_BOARD_H

#include "desk_hardware.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Returns a display-only Watcher driver table. Its init performs the native
 * board power, SPD2010 panel, first-frame, and backlight sequence. */
const desk_board_driver_ops_t *desk_watcher_board_get_driver_ops(void);
bool desk_watcher_board_display_ready(void);

/* Shared Watcher I2C/expander services. These reuse the LCD-installed I2C0
 * master and never change unrelated PCA9535 outputs. */
int desk_watcher_board_i2c_ensure(void);
int desk_watcher_board_i2c_probe(uint8_t address_7bit);
int desk_watcher_board_audio_pa_set(bool enabled);

#ifdef __cplusplus
}
#endif

#endif /* DESK_WATCHER_BOARD_H */
