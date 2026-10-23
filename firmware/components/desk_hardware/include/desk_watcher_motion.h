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

typedef enum {
    DESK_WATCHER_MOTION_ERROR_NONE = 0,
    DESK_WATCHER_MOTION_ERROR_UART_UNAVAILABLE,
    DESK_WATCHER_MOTION_ERROR_UART_BUSY,
    DESK_WATCHER_MOTION_ERROR_TX_IO,
    DESK_WATCHER_MOTION_ERROR_BAD_CRC,
    DESK_WATCHER_MOTION_ERROR_WRONG_SEQUENCE,
    DESK_WATCHER_MOTION_ERROR_INVALID_RESPONSE,
    DESK_WATCHER_MOTION_ERROR_NACK,
    DESK_WATCHER_MOTION_ERROR_ACK_TIMEOUT,
} desk_watcher_motion_error_t;

typedef struct {
    uint32_t tx_bytes; /* Bytes reported accepted by uart_write_bytes. */
    uint32_t rx_bytes; /* Bytes returned by uart_read_bytes. */
    uint32_t ack_count; /* CRC-valid ACKs correlated to an outstanding request. */
    uint32_t nack_count;
    uint32_t bad_crc_count;
    uint32_t wrong_sequence_count;
    uint32_t timeout_count;
    uint32_t retry_count;
    desk_watcher_motion_error_t last_error;
} desk_watcher_motion_diagnostics_t;

typedef struct {
    uint8_t fw_major;
    uint8_t fw_minor;
    uint8_t fw_patch;
    uint8_t hw_version;
    uint8_t capability_bitmap;
    uint8_t sensor_bitmap;
    uint8_t default_stream_profile;
} desk_watcher_motion_peer_info_t;

/* Read-only official MCU-link HELLO. Does not issue motion commands. */
int desk_watcher_motion_probe(desk_watcher_motion_peer_info_t *out, uint32_t timeout_ms);
int desk_watcher_motion_pack_hello_req(uint32_t seq, uint8_t *out_wire, size_t wire_capacity, size_t *out_len);
int desk_watcher_motion_parse_hello_rsp(const uint8_t *wire, size_t wire_len, uint32_t expected_seq,
                                        desk_watcher_motion_peer_info_t *out);

/* Read-only, since-boot transport diagnostics. These do not represent physical
 * position or prove that UART bytes reached the STM32 pins. */
void desk_watcher_motion_get_diagnostics(desk_watcher_motion_diagnostics_t *out);

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
