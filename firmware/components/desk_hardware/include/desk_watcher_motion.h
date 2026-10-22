/**
 * @file desk_watcher_motion.h
 * @brief SenseCAP Watcher STM32 Body Coprocessor UART2 Motion Driver.
 */

#ifndef DESK_WATCHER_MOTION_H
#define DESK_WATCHER_MOTION_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "desk_hardware.h"

#ifdef __cplusplus
extern "C" {
#endif

int desk_watcher_motion_init(void);
int desk_watcher_motion_move(float pan_deg, float tilt_deg, uint32_t duration_ms);
int desk_watcher_motion_stop(bool emergency);
/* Result of the last best-effort STOP sent after an ambiguous MOVE failure.
 * This diagnostic does not confirm peer receipt or physical motion state. */
int desk_watcher_motion_get_last_ambiguous_move_stop_result(void);
int desk_watcher_motion_deinit(void);
bool desk_watcher_motion_is_ready(void);

/* COBS and Wire Framing functions */
int desk_watcher_motion_cobs_encode(const uint8_t *src, size_t src_len, uint8_t *dst, size_t dst_len, size_t *out_len);
int desk_watcher_motion_cobs_decode(const uint8_t *src, size_t src_len, uint8_t *dst, size_t dst_len, size_t *out_len);
int desk_watcher_motion_wire_encode(const uint8_t *raw, size_t raw_len, uint8_t *wire, size_t wire_len, size_t *out_len);
int desk_watcher_motion_wire_decode(const uint8_t *wire, size_t wire_len, uint8_t *raw, size_t raw_len, size_t *out_len);
size_t desk_watcher_motion_pack_move(float pan, float tilt, uint32_t duration_ms, uint32_t seq, uint8_t *out_buf);
size_t desk_watcher_motion_pack_stop(bool emergency, uint32_t seq, uint8_t *out_buf);
int desk_watcher_motion_parse_ack(const uint8_t *frame, size_t frame_len,
                                  uint32_t expected_seq, uint8_t expected_msg_id,
                                  uint8_t *out_nack_code, bool *out_is_nack);

#ifdef __cplusplus
}
#endif

#endif /* DESK_WATCHER_MOTION_H */
