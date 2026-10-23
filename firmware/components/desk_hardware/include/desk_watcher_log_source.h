/**
 * @file desk_watcher_log_source.h
 * @brief Bounded, structured device-event source for the SDK bridge.
 */
#ifndef DESK_WATCHER_LOG_SOURCE_H
#define DESK_WATCHER_LOG_SOURCE_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DESK_WATCHER_LOG_SOURCE_CAPACITY 16U
#define DESK_WATCHER_LOG_SOURCE_MAX_RECORD_BYTES 192U

typedef enum {
    DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY = 1,
} desk_watcher_log_event_id_t;

typedef struct {
    uint64_t sequence;
    uint64_t uptime_ms;
    desk_watcher_log_event_id_t event_id;
} desk_watcher_log_record_t;

typedef struct {
    uint64_t first_dropped_sequence;
    uint64_t last_dropped_sequence;
    uint64_t dropped_count;
} desk_watcher_log_gap_t;

/* Enable only for an authorized active stream. Disable clears queued records
 * and pending gaps; it does not reset the boot-scoped sequence. */
void desk_watcher_log_source_set_enabled(bool enabled);
bool desk_watcher_log_source_is_enabled(void);
uint64_t desk_watcher_log_source_next_sequence(void);

/* Publishes only the reviewed camera-empty enum event. No strings, raw logs,
 * dynamic allocation, waiting, or network I/O occur on the producer path. */
bool desk_watcher_log_source_publish_camera_capture_empty(void);
bool desk_watcher_log_source_try_dequeue(desk_watcher_log_record_t *out_record);
bool desk_watcher_log_source_take_gap(desk_watcher_log_gap_t *out_gap);

/* Observes one already-formatted TAL output line. CR-009 v1 has no approved
 * text-to-event mapping, so this bounded observer intentionally drops every
 * line without retaining its borrowed pointer or changing the event queue. */
void desk_watcher_log_source_observe_tal_line(const char *formatted_line);

#ifdef __cplusplus
}
#endif
#endif
