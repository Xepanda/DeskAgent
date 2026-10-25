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
    uint64_t rate_window_start_ms;
    uint8_t rate_count;
    bool audio_snapshot_published;
    bool has_mqtt_state;
    bool mqtt_connected;
    bool has_stm32_timeout_time;
    uint64_t last_stm32_timeout_ms;
} desk_watcher_log_source_state_t;

_Static_assert(sizeof(desk_watcher_log_record_t) <= DESK_WATCHER_LOG_SOURCE_MAX_RECORD_BYTES,
               "CR-009 source record exceeds 192-byte limit");

static desk_watcher_log_source_state_t s_source = {.next_sequence = 1U};

void desk_watcher_log_source_set_enabled(bool enabled)
{
    SOURCE_LOCK();
    bool was_enabled = s_source.enabled;
    s_source.enabled = enabled;
    if (enabled && !was_enabled) {
        s_source.rate_window_start_ms = 0U;
        s_source.rate_count = 0U;
        s_source.audio_snapshot_published = false;
        s_source.has_mqtt_state = false;
        s_source.mqtt_connected = false;
        s_source.has_stm32_timeout_time = false;
        s_source.last_stm32_timeout_ms = 0U;
    }
    if (!enabled) {
        s_source.head = 0U;
        s_source.count = 0U;
        s_source.has_gap = false;
        memset(&s_source.gap, 0, sizeof(s_source.gap));
        s_source.rate_window_start_ms = 0U;
        s_source.rate_count = 0U;
        s_source.audio_snapshot_published = false;
        s_source.has_mqtt_state = false;
        s_source.mqtt_connected = false;
        s_source.has_stm32_timeout_time = false;
        s_source.last_stm32_timeout_ms = 0U;
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

static bool source_publish_locked(uint64_t uptime_ms,
                                  desk_watcher_log_event_id_t event_id,
                                  desk_watcher_log_component_t component,
                                  desk_watcher_log_level_t level,
                                  desk_watcher_log_state_t state)
{
    if (!s_source.enabled) return false;
    if (s_source.rate_count == 0U || uptime_ms < s_source.rate_window_start_ms ||
        uptime_ms - s_source.rate_window_start_ms >= DESK_WATCHER_LOG_SOURCE_RATE_WINDOW_MS) {
        s_source.rate_window_start_ms = uptime_ms;
        s_source.rate_count = 0U;
    }
    if (s_source.rate_count >= DESK_WATCHER_LOG_SOURCE_MAX_EVENTS_PER_MINUTE) return false;
    ++s_source.rate_count;

    const uint64_t sequence = s_source.next_sequence++;
    if (s_source.count < DESK_WATCHER_LOG_SOURCE_CAPACITY) {
        const uint8_t tail = (uint8_t)((s_source.head + s_source.count) %
                                       DESK_WATCHER_LOG_SOURCE_CAPACITY);
        s_source.records[tail] = (desk_watcher_log_record_t){
            .sequence = sequence,
            .uptime_ms = uptime_ms,
            .event_id = event_id,
            .component = component,
            .level = level,
            .state = state,
        };
        ++s_source.count;
        return true;
    }
    if (!s_source.has_gap) {
        s_source.gap.first_dropped_sequence = sequence;
        s_source.gap.last_dropped_sequence = sequence;
        s_source.gap.dropped_count = 1U;
        s_source.has_gap = true;
    } else {
        s_source.gap.last_dropped_sequence = sequence;
        ++s_source.gap.dropped_count;
    }
    return false;
}

bool desk_watcher_log_source_publish_camera_capture_empty(void)
{
    const uint64_t uptime_ms = source_uptime_ms();
    SOURCE_LOCK();
    bool enqueued = source_publish_locked(uptime_ms,
        DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY,
        DESK_WATCHER_LOG_COMPONENT_CAMERA, DESK_WATCHER_LOG_LEVEL_WARN,
        DESK_WATCHER_LOG_STATE_NONE);
    SOURCE_UNLOCK();
    return enqueued;
}

bool desk_watcher_log_source_publish_audio_driver_state(bool ready)
{
    const uint64_t uptime_ms = source_uptime_ms();
    SOURCE_LOCK();
    if (!s_source.enabled || s_source.audio_snapshot_published) {
        SOURCE_UNLOCK();
        return false;
    }
    s_source.audio_snapshot_published = true;
    bool enqueued = source_publish_locked(uptime_ms,
        DESK_WATCHER_LOG_EVENT_AUDIO_DRIVER_STATE_SNAPSHOT,
        DESK_WATCHER_LOG_COMPONENT_AUDIO, DESK_WATCHER_LOG_LEVEL_WARN,
        ready ? DESK_WATCHER_LOG_STATE_READY : DESK_WATCHER_LOG_STATE_NOT_READY);
    SOURCE_UNLOCK();
    return enqueued;
}

bool desk_watcher_log_source_publish_tuya_mqtt_state(bool connected)
{
    const uint64_t uptime_ms = source_uptime_ms();
    SOURCE_LOCK();
    if (!s_source.enabled || (s_source.has_mqtt_state && s_source.mqtt_connected == connected)) {
        SOURCE_UNLOCK();
        return false;
    }
    s_source.has_mqtt_state = true;
    s_source.mqtt_connected = connected;
    bool enqueued = source_publish_locked(uptime_ms,
        DESK_WATCHER_LOG_EVENT_TUYA_MQTT_STATE,
        DESK_WATCHER_LOG_COMPONENT_NETWORK, DESK_WATCHER_LOG_LEVEL_WARN,
        connected ? DESK_WATCHER_LOG_STATE_CONNECTED : DESK_WATCHER_LOG_STATE_DISCONNECTED);
    SOURCE_UNLOCK();
    return enqueued;
}

bool desk_watcher_log_source_publish_stm32_ack_timeout(void)
{
    const uint64_t uptime_ms = source_uptime_ms();
    SOURCE_LOCK();
    if (!s_source.enabled ||
        (s_source.has_stm32_timeout_time &&
         (uptime_ms < s_source.last_stm32_timeout_ms ||
          uptime_ms - s_source.last_stm32_timeout_ms < DESK_WATCHER_LOG_SOURCE_RATE_WINDOW_MS))) {
        SOURCE_UNLOCK();
        return false;
    }
    s_source.has_stm32_timeout_time = true;
    s_source.last_stm32_timeout_ms = uptime_ms;
    bool enqueued = source_publish_locked(uptime_ms,
        DESK_WATCHER_LOG_EVENT_STM32_ACK_TIMEOUT,
        DESK_WATCHER_LOG_COMPONENT_STM32, DESK_WATCHER_LOG_LEVEL_ERROR,
        DESK_WATCHER_LOG_STATE_NONE);
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

void desk_watcher_log_source_observe_tal_line(const char *formatted_line)
{
    /* No TAL text is an approved CR-009 v1 event. Do not inspect or retain the
     * borrowed line; this is constant-time, allocation-free, and reentrant. */
    (void)formatted_line;
}
