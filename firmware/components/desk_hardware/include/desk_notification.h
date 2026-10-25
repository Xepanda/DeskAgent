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
#define DESK_NOTIFICATION_SUMMARY_ID_MAX 128
#define DESK_NOTIFICATION_SUMMARY_BATCH_ID_MAX 128
#define DESK_NOTIFICATION_SUMMARY_MAX_ITEMS 20
#define DESK_NOTIFICATION_SUMMARY_TITLE_MAX 256
#define DESK_NOTIFICATION_TIMEZONE_MAX 64
#define DESK_NOTIFICATION_DISPLAY_TIME_MAX 26

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

/* Records and dedupe state are RAM-only and are cleared on init/reboot. */
int desk_hardware_notification_init(desk_notification_status_cb_t callback, void *user_data);

/* Validate and dedupe by notification_id for this boot session. */
int desk_hardware_notification_deliver(const desk_notification_input_t *input,
                                       desk_notification_status_event_t *out_status);

typedef struct {
    const char *reminder_id;
    const char *occurrence_id;
    const char *title;
    const char *scheduled_at;
    const char *timezone;
    const char *display_time; /* YYYY-MM-DD HH:MM UTC±HH:MM; max 26 ASCII bytes. */
} desk_notification_summary_item_t;

typedef struct {
    const char *summary_id;
    const char *batch_id;
    const char *created_at;
    const char *expires_at;
    const desk_notification_summary_item_t *items;
    size_t item_count;
    uint32_t omitted_count;
} desk_notification_summary_input_t;

typedef void (*desk_notification_summary_ack_cb_t)(const char *summary_id,
                                                    const char *source,
                                                    void *user_data);

/* Summary delivery is display-only. Input strings are consumed synchronously. */
int desk_hardware_notification_deliver_summary(const desk_notification_summary_input_t *input);
int desk_hardware_notification_register_summary_ack_callback(
    desk_notification_summary_ack_cb_t callback, void *user_data);
/* Emits the registered callback only after this explicit UI acknowledgement call. */
int desk_hardware_notification_summary_acknowledge(const char *summary_id, const char *source);

/* Serialize all calls through the owning adapter. Status callbacks run in the
 * calling context and are keyed by notification_id. Process at most one queued
 * item from A2's serialized event loop.
 * Returns DESK_HW_OK when no queued item is available. */
int desk_hardware_notification_process_pending(void);

/* Explicit acknowledgement seam; it does not change reminder ringing/completion state. */
int desk_hardware_notification_acknowledge(const char *notification_id, const char *source);
/* Dismisses only the matching volatile screen overlay; it does not create an ACK. */
int desk_hardware_notification_dismiss(const char *notification_id);
int desk_hardware_notification_get_status(const char *notification_id,
                                          desk_notification_status_event_t *out_status);

#ifdef __cplusplus
}
#endif

#endif
