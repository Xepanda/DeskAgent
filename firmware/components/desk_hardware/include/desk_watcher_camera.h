/**
 * @file desk_watcher_camera.h
 * @brief SenseCAP Watcher Himax HX6538 SPI2 / PTL Camera Driver.
 */

#ifndef DESK_WATCHER_CAMERA_H
#define DESK_WATCHER_CAMERA_H

#include <stdbool.h>
#include <stdint.h>
#include <stddef.h>
#include "desk_hardware.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Camera-specific failures preserve the protocol layer that rejected a frame. */
enum {
    DESK_WATCHER_CAMERA_ERR_PROTOCOL = -20,
    DESK_WATCHER_CAMERA_ERR_CRC = -21,
    DESK_WATCHER_CAMERA_ERR_SYNC_IO = -22,
    DESK_WATCHER_CAMERA_ERR_NOT_READY = -23,
    DESK_WATCHER_CAMERA_ERR_SPI = -24,
    DESK_WATCHER_CAMERA_ERR_EMPTY = -25,
    DESK_WATCHER_CAMERA_ERR_CONTROL = -26,
};

typedef struct {
    uint32_t result;
    uint32_t generation;
    uint32_t mode;
    uint32_t model;
    uint32_t debug;
    uint32_t error;
} desk_watcher_camera_control_state_t;

typedef struct {
    uint8_t bytes[7];
    size_t used;
} desk_watcher_camera_resync_t;

int desk_watcher_camera_init(void);
int desk_watcher_camera_capture(uint8_t *out_buf, size_t max_len, size_t *out_len);
int desk_watcher_camera_deinit(void);
bool desk_watcher_camera_is_ready(void);

/* PTL Header and Media Frame validation helpers */
int desk_watcher_camera_parse_ptl_header(const uint8_t *header, size_t header_len, uint32_t *out_body_size);
int desk_watcher_camera_validate_frame(const uint8_t *body, size_t body_size, const uint8_t **out_jpeg, size_t *out_jpeg_size);
int desk_watcher_camera_parse_control_state(const char *line, uint32_t expected_id,
                                            desk_watcher_camera_control_state_t *out_state);
bool desk_watcher_camera_resync_push(desk_watcher_camera_resync_t *scanner, uint8_t byte,
                                     uint32_t max_body_size, uint32_t *out_body_size);

#ifdef __cplusplus
}
#endif

#endif /* DESK_WATCHER_CAMERA_H */
