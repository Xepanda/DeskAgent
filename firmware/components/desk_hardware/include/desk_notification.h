#ifndef DESK_NOTIFICATION_H
#define DESK_NOTIFICATION_H

#include <stdbool.h>
#include <stdint.h>
#include "desk_hardware.h"

#ifdef __cplusplus
extern "C" {
#endif

#define DESK_NOTIFICATION_ID_MAX 64
#define DESK_NOTIFICATION_TITLE_MAX 127
#define DESK_NOTIFICATION_TIMESTAMP_MAX 31
#define DESK_NOTIFICATION_REMINDER_ID_MAX 64
#define DESK_NOTIFICATION_OCCURRENCE_ID_MAX 64
#define DESK_NOTIFICATION_ACK_SOURCE_MAX 31
#define DESK_NOTIFICATION_STORE_SLOT_EMPTY 1
/* Distinct from DESK_HW_ERR_UNAVAILABLE (for example, an untrusted clock).
 * Returned only when the durable notification provider is known unavailable. */
#define DESK_NOTIFICATION_ERR_STORAGE_UNAVAILABLE (-100)

typedef struct {
    char notification_id[DESK_NOTIFICATION_ID_MAX + 1];
    char reminder_id[DESK_NOTIFICATION_REMINDER_ID_MAX + 1];
    char occurrence_id[DESK_NOTIFICATION_OCCURRENCE_ID_MAX + 1];
    char title[DESK_NOTIFICATION_TITLE_MAX + 1];
    char scheduled_at[DESK_NOTIFICATION_TIMESTAMP_MAX + 1];
    char expires_at[DESK_NOTIFICATION_TIMESTAMP_MAX + 1];
    bool is_catchup;
} desk_notification_input_t;

typedef enum {
    DESK_NOTIFICATION_ACCEPTED = 0,
    DESK_NOTIFICATION_RENDERED,
    DESK_NOTIFICATION_PLAYED,       /* Reserved: current hardware API cannot prove completion. */
    DESK_NOTIFICATION_FAILED,
    DESK_NOTIFICATION_EXPIRED,
    DESK_NOTIFICATION_UNKNOWN,
    DESK_NOTIFICATION_ACKNOWLEDGED
} desk_notification_status_t;

typedef struct {
    char notification_id[DESK_NOTIFICATION_ID_MAX + 1];
    desk_notification_status_t status;
    int error_code;
    bool acknowledged;
    char ack_source[DESK_NOTIFICATION_ACK_SOURCE_MAX + 1];
} desk_notification_status_event_t;

typedef void (*desk_notification_status_cb_t)(const desk_notification_status_event_t *event,
                                               void *user_data);

/* A1 storage backend contract. save() must durably commit the opaque, versioned
 * record before returning DESK_HW_OK. load() returns STORE_SLOT_EMPTY only when
 * a slot is empty; storage failures must return an error. No default backend is
 * enabled on protected partitions. */
typedef struct {
    int (*load)(unsigned slot, void *out_record, size_t capacity, size_t *out_len, void *user_data);
    int (*save)(unsigned slot, const void *record, size_t record_len, void *user_data);
    int (*erase)(unsigned slot, void *user_data);
    void *user_data;
} desk_notification_store_ops_t;

/* Register an explicitly authorized durable backend before init. */
int desk_hardware_notification_register_store(const desk_notification_store_ops_t *ops);

/* Initialize after the approved persistent backend is ready. Fails closed without it. */
int desk_hardware_notification_init(desk_notification_status_cb_t callback, void *user_data);

/* Validate and durably dedupe by notification_id before returning ACCEPTED. */
int desk_hardware_notification_deliver(const desk_notification_input_t *input,
                                       desk_notification_status_event_t *out_status);

/* Serialize all calls through the owning adapter. Status callbacks run in the
 * calling context and are keyed by notification_id. Process at most one queued
 * item from A2's serialized event loop.
 * Returns DESK_HW_OK when no queued item is available. */
int desk_hardware_notification_process_pending(void);

/* Explicit acknowledgement seam; it does not change reminder ringing/completion state. */
int desk_hardware_notification_acknowledge(const char *notification_id, const char *source);
int desk_hardware_notification_get_status(const char *notification_id,
                                          desk_notification_status_event_t *out_status);

#ifdef __cplusplus
}
#endif

#endif
