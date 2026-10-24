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
    DESK_WATCHER_CAMERA_ERR_BUFFER_TOO_SMALL = -27,
    DESK_WATCHER_CAMERA_ERR_SYNC_TIMEOUT = -28,
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

#define DESK_WATCHER_CAMERA_SSCMA_REPLY_MAX 512U

typedef enum {
    DESK_WATCHER_CAMERA_SSCMA_QUERY_VERSION = 0,
    DESK_WATCHER_CAMERA_SSCMA_QUERY_ID = 1,
} desk_watcher_camera_sscma_query_t;

typedef struct {
    uint8_t raw_response[DESK_WATCHER_CAMERA_SSCMA_REPLY_MAX];
    size_t raw_response_len;
    desk_watcher_camera_sscma_query_t query;
} desk_watcher_camera_sscma_reply_t;

/* Sanitized fields from the optional, read-only SSCMA identity probe. */
typedef struct {
    char at_api[24];
    char software[48];
    char hardware[24];
    uint8_t raw_response[DESK_WATCHER_CAMERA_SSCMA_REPLY_MAX];
    size_t raw_response_len;
} desk_watcher_camera_sscma_identity_t;

int desk_watcher_camera_init(void);
int desk_watcher_camera_capture(uint8_t *out_buf, size_t max_len, size_t *out_len, uint32_t timeout_ms);
int desk_watcher_camera_deinit(void);
bool desk_watcher_camera_is_ready(void);
/* Explicit one-shot probe only. Normal init/capture remain on the PTL path. */
int desk_watcher_camera_probe_sscma_query(desk_watcher_camera_sscma_query_t query,
                                          desk_watcher_camera_sscma_reply_t *out_reply);
int desk_watcher_camera_probe_sscma_identity(desk_watcher_camera_sscma_identity_t *out_identity);

/* Pure bounded SSCMA wire helpers, exposed for host contract tests. */
int desk_watcher_camera_sscma_encode_packet(uint8_t operation, const uint8_t *payload,
                                            size_t payload_len, uint8_t *out_packet,
                                            size_t packet_capacity);
int desk_watcher_camera_sscma_parse_available(const uint8_t reply[2], size_t *out_available);
int desk_watcher_camera_sscma_validate_reply(const uint8_t *reply, size_t reply_len);
int desk_watcher_camera_sscma_parse_version_reply(const char *reply, size_t reply_len,
                                                  desk_watcher_camera_sscma_identity_t *out_identity);

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
