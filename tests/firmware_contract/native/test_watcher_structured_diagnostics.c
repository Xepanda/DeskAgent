#include "desk_watcher_log_source.h"

#include <assert.h>
#include <pthread.h>
#include <stddef.h>
#include <stdint.h>

/* The native production source intentionally has no platform lock. Serialize
 * each harness call so this test exercises event deduplication/order without
 * introducing C data races that do not exist on ESP's critical-section path. */
static pthread_mutex_t s_publish_mutex = PTHREAD_MUTEX_INITIALIZER;

static void *publish_same_mqtt_state(void *arg)
{
    const bool connected = (bool)(uintptr_t)arg;
    for (size_t i = 0; i < 1000U; ++i) {
        assert(pthread_mutex_lock(&s_publish_mutex) == 0);
        (void)desk_watcher_log_source_publish_tuya_mqtt_state(connected);
        assert(pthread_mutex_unlock(&s_publish_mutex) == 0);
    }
    return NULL;
}

static void *publish_audio_snapshot(void *arg)
{
    const bool ready = (bool)(uintptr_t)arg;
    for (size_t i = 0; i < 1000U; ++i) {
        assert(pthread_mutex_lock(&s_publish_mutex) == 0);
        (void)desk_watcher_log_source_publish_audio_driver_state(ready);
        assert(pthread_mutex_unlock(&s_publish_mutex) == 0);
    }
    return NULL;
}

static desk_watcher_log_record_t take_one(void)
{
    desk_watcher_log_record_t record;
    assert(desk_watcher_log_source_try_dequeue(&record));
    return record;
}

int main(void)
{
    desk_watcher_log_record_t record;
    desk_watcher_log_gap_t gap;
    const uint64_t initial_sequence = desk_watcher_log_source_next_sequence();

    /* Disabled source must suppress collection and sequence allocation. */
    assert(!desk_watcher_log_source_publish_audio_driver_state(true));
    assert(!desk_watcher_log_source_publish_tuya_mqtt_state(true));
    assert(!desk_watcher_log_source_publish_stm32_ack_timeout());
    assert(desk_watcher_log_source_next_sequence() == initial_sequence);

    desk_watcher_log_source_set_enabled(true);
    assert(desk_watcher_log_source_publish_audio_driver_state(true));
    record = take_one();
    assert(record.event_id == DESK_WATCHER_LOG_EVENT_AUDIO_DRIVER_STATE_SNAPSHOT);
    assert(record.component == DESK_WATCHER_LOG_COMPONENT_AUDIO);
    assert(record.level == DESK_WATCHER_LOG_LEVEL_WARN);
    assert(record.state == DESK_WATCHER_LOG_STATE_READY);
    uint64_t after_audio_snapshot = desk_watcher_log_source_next_sequence();
    assert(!desk_watcher_log_source_publish_audio_driver_state(false));
    assert(desk_watcher_log_source_next_sequence() == after_audio_snapshot);
    assert(!desk_watcher_log_source_try_dequeue(&record));

    assert(desk_watcher_log_source_publish_tuya_mqtt_state(true));
    record = take_one();
    assert(record.event_id == DESK_WATCHER_LOG_EVENT_TUYA_MQTT_STATE);
    assert(record.component == DESK_WATCHER_LOG_COMPONENT_NETWORK);
    assert(record.level == DESK_WATCHER_LOG_LEVEL_WARN);
    assert(record.state == DESK_WATCHER_LOG_STATE_CONNECTED);
    uint64_t after_mqtt_connected = desk_watcher_log_source_next_sequence();
    assert(!desk_watcher_log_source_publish_tuya_mqtt_state(true));
    assert(desk_watcher_log_source_next_sequence() == after_mqtt_connected);
    assert(desk_watcher_log_source_publish_tuya_mqtt_state(false));
    record = take_one();
    assert(record.state == DESK_WATCHER_LOG_STATE_DISCONNECTED);

    assert(desk_watcher_log_source_publish_stm32_ack_timeout());
    record = take_one();
    assert(record.event_id == DESK_WATCHER_LOG_EVENT_STM32_ACK_TIMEOUT);
    assert(record.component == DESK_WATCHER_LOG_COMPONENT_STM32);
    assert(record.level == DESK_WATCHER_LOG_LEVEL_ERROR);
    assert(record.state == DESK_WATCHER_LOG_STATE_NONE);
    uint64_t after_timeout = desk_watcher_log_source_next_sequence();
    assert(!desk_watcher_log_source_publish_stm32_ack_timeout());
    assert(desk_watcher_log_source_next_sequence() == after_timeout);
    assert(!desk_watcher_log_source_try_dequeue(&record));

    /* First observed state is an edge; concurrent duplicates collapse to one. */
    desk_watcher_log_source_set_enabled(false);
    desk_watcher_log_source_set_enabled(true);
    pthread_t threads[8];
    for (size_t i = 0; i < 4U; ++i) {
        assert(pthread_create(&threads[i], NULL, publish_same_mqtt_state,
                              (void *)(uintptr_t)true) == 0);
        assert(pthread_create(&threads[i + 4U], NULL, publish_audio_snapshot,
                              (void *)(uintptr_t)false) == 0);
    }
    for (size_t i = 0; i < 8U; ++i) assert(pthread_join(threads[i], NULL) == 0);
    bool saw_mqtt_state = false;
    bool saw_audio_state = false;
    for (size_t i = 0; i < 2U; ++i) {
        record = take_one();
        if (record.event_id == DESK_WATCHER_LOG_EVENT_TUYA_MQTT_STATE) {
            assert(!saw_mqtt_state);
            assert(record.component == DESK_WATCHER_LOG_COMPONENT_NETWORK);
            assert(record.level == DESK_WATCHER_LOG_LEVEL_WARN);
            assert(record.state == DESK_WATCHER_LOG_STATE_CONNECTED);
            saw_mqtt_state = true;
        } else if (record.event_id == DESK_WATCHER_LOG_EVENT_AUDIO_DRIVER_STATE_SNAPSHOT) {
            assert(!saw_audio_state);
            assert(record.component == DESK_WATCHER_LOG_COMPONENT_AUDIO);
            assert(record.level == DESK_WATCHER_LOG_LEVEL_WARN);
            assert(record.state == DESK_WATCHER_LOG_STATE_NOT_READY);
            saw_audio_state = true;
        } else {
            assert(0);
        }
    }
    assert(saw_mqtt_state);
    assert(saw_audio_state);
    assert(!desk_watcher_log_source_try_dequeue(&record));

    /* Global 30/minute cap applies before sequence allocation; only queue-full
     * drops after allocation contribute to the CR-009 gap. */
    desk_watcher_log_source_set_enabled(false);
    desk_watcher_log_source_set_enabled(true);
    const uint64_t before_camera = desk_watcher_log_source_next_sequence();
    for (size_t i = 0; i < 30U; ++i) {
        (void)desk_watcher_log_source_publish_camera_capture_empty();
    }
    const uint64_t after_thirty = desk_watcher_log_source_next_sequence();
    assert(after_thirty == before_camera + 30U);
    assert(!desk_watcher_log_source_publish_camera_capture_empty());
    assert(desk_watcher_log_source_next_sequence() == after_thirty);
    size_t queued = 0U;
    while (desk_watcher_log_source_try_dequeue(&record)) {
        assert(record.event_id == DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY);
        assert(record.component == DESK_WATCHER_LOG_COMPONENT_CAMERA);
        assert(record.level == DESK_WATCHER_LOG_LEVEL_WARN);
        assert(record.state == DESK_WATCHER_LOG_STATE_NONE);
        ++queued;
    }
    assert(queued == DESK_WATCHER_LOG_SOURCE_CAPACITY);
    assert(desk_watcher_log_source_take_gap(&gap));
    assert(gap.first_dropped_sequence == before_camera + DESK_WATCHER_LOG_SOURCE_CAPACITY);
    assert(gap.last_dropped_sequence == after_thirty - 1U);
    assert(gap.dropped_count == 30U - DESK_WATCHER_LOG_SOURCE_CAPACITY);
    assert(!desk_watcher_log_source_take_gap(&gap));

    desk_watcher_log_source_set_enabled(false);
    assert(!desk_watcher_log_source_try_dequeue(&record));
    assert(!desk_watcher_log_source_take_gap(&gap));
    return 0;
}
