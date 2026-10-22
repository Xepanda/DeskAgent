#include "desk_watcher_log_source.h"

#include <string.h>

#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
#include "esp_timer.h"
static portMUX_TYPE s_source_mux = portMUX_INITIALIZER_UNLOCKED;
#define SOURCE_LOCK()   portENTER_CRITICAL(&s_source_mux)
#define SOURCE_UNLOCK() portEXIT_CRITICAL(&s_source_mux)
static uint64_t source_uptime_ms(void) { return (uint64_t)(esp_timer_get_time() / 1000); }
#else
#include <time.h>
#define SOURCE_LOCK()   ((void)0)
#define SOURCE_UNLOCK() ((void)0)
static uint64_t source_uptime_ms(void)
{
    struct timespec now;
    if (clock_gettime(CLOCK_MONOTONIC, &now) != 0) return 0;
    return (uint64_t)now.tv_sec * 1000U + (uint64_t)now.tv_nsec / 1000000U;
}
#endif

typedef struct {
    desk_watcher_log_record_t records[DESK_WATCHER_LOG_SOURCE_CAPACITY];
    uint8_t head;
    uint8_t count;
    uint64_t next_sequence;
    bool enabled;
    bool has_gap;
    desk_watcher_log_gap_t gap;
} desk_watcher_log_source_state_t;

_Static_assert(sizeof(desk_watcher_log_record_t) <= DESK_WATCHER_LOG_SOURCE_MAX_RECORD_BYTES,
               "CR-009 source record exceeds 192-byte limit");

static desk_watcher_log_source_state_t s_source = {.next_sequence = 1U};

void desk_watcher_log_source_set_enabled(bool enabled)
{
    SOURCE_LOCK();
    s_source.enabled = enabled;
    if (!enabled) {
        s_source.head = 0U;
        s_source.count = 0U;
        s_source.has_gap = false;
        memset(&s_source.gap, 0, sizeof(s_source.gap));
    }
    SOURCE_UNLOCK();
}

bool desk_watcher_log_source_is_enabled(void)
{
    bool enabled;
    SOURCE_LOCK();
    enabled = s_source.enabled;
    SOURCE_UNLOCK();
    return enabled;
}

uint64_t desk_watcher_log_source_next_sequence(void)
{
    uint64_t next;
    SOURCE_LOCK();
    next = s_source.next_sequence;
    SOURCE_UNLOCK();
    return next;
}

bool desk_watcher_log_source_publish_camera_capture_empty(void)
{
    const uint64_t uptime_ms = source_uptime_ms();
    bool enqueued = false;
    SOURCE_LOCK();
    if (s_source.enabled) {
        const uint64_t sequence = s_source.next_sequence++;
        if (s_source.count < DESK_WATCHER_LOG_SOURCE_CAPACITY) {
            const uint8_t tail = (uint8_t)((s_source.head + s_source.count) %
                                           DESK_WATCHER_LOG_SOURCE_CAPACITY);
            s_source.records[tail] = (desk_watcher_log_record_t){
                .sequence = sequence,
                .uptime_ms = uptime_ms,
                .event_id = DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY,
            };
            ++s_source.count;
            enqueued = true;
        } else {
            if (!s_source.has_gap) {
                s_source.gap.first_dropped_sequence = sequence;
                s_source.gap.last_dropped_sequence = sequence;
                s_source.gap.dropped_count = 1U;
                s_source.has_gap = true;
            } else {
                s_source.gap.last_dropped_sequence = sequence;
                ++s_source.gap.dropped_count;
            }
        }
    }
    SOURCE_UNLOCK();
    return enqueued;
}

bool desk_watcher_log_source_try_dequeue(desk_watcher_log_record_t *out_record)
{
    bool available = false;
    if (!out_record) return false;
    SOURCE_LOCK();
    if (s_source.count != 0U) {
        *out_record = s_source.records[s_source.head];
        s_source.head = (uint8_t)((s_source.head + 1U) % DESK_WATCHER_LOG_SOURCE_CAPACITY);
        --s_source.count;
        available = true;
    }
    SOURCE_UNLOCK();
    return available;
}

bool desk_watcher_log_source_take_gap(desk_watcher_log_gap_t *out_gap)
{
    bool available = false;
    if (!out_gap) return false;
    SOURCE_LOCK();
    if (s_source.has_gap) {
        *out_gap = s_source.gap;
        memset(&s_source.gap, 0, sizeof(s_source.gap));
        s_source.has_gap = false;
        available = true;
    }
    SOURCE_UNLOCK();
    return available;
}
