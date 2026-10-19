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

int desk_watcher_camera_init(void);
int desk_watcher_camera_capture(uint8_t *out_buf, size_t max_len, size_t *out_len);
int desk_watcher_camera_deinit(void);
bool desk_watcher_camera_is_ready(void);

/* PTL Header and Media Frame validation helpers */
int desk_watcher_camera_parse_ptl_header(const uint8_t *header, size_t header_len, uint32_t *out_body_size);
int desk_watcher_camera_validate_frame(const uint8_t *body, size_t body_size, const uint8_t **out_jpeg, size_t *out_jpeg_size);

#ifdef __cplusplus
}
#endif

#endif /* DESK_WATCHER_CAMERA_H */
