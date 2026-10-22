#include "desk_watcher_log_source.h"

#include <assert.h>
#include <stddef.h>

int main(void)
{
    desk_watcher_log_record_t record;
    desk_watcher_log_gap_t gap;

    assert(desk_watcher_log_source_next_sequence() == 1U);
    assert(!desk_watcher_log_source_publish_camera_capture_empty());
    assert(desk_watcher_log_source_next_sequence() == 1U);

    desk_watcher_log_source_set_enabled(true);
    for (uint64_t i = 1U; i <= DESK_WATCHER_LOG_SOURCE_CAPACITY; ++i) {
        assert(desk_watcher_log_source_publish_camera_capture_empty());
    }
    assert(desk_watcher_log_source_next_sequence() == 17U);
    for (uint64_t i = 17U; i <= 20U; ++i) {
        assert(!desk_watcher_log_source_publish_camera_capture_empty());
    }
    assert(desk_watcher_log_source_next_sequence() == 21U);

    assert(desk_watcher_log_source_take_gap(&gap));
    assert(gap.first_dropped_sequence == 17U);
    assert(gap.last_dropped_sequence == 20U);
    assert(gap.dropped_count == 4U);
    assert(!desk_watcher_log_source_take_gap(&gap));

    for (uint64_t i = 1U; i <= DESK_WATCHER_LOG_SOURCE_CAPACITY; ++i) {
        assert(desk_watcher_log_source_try_dequeue(&record));
        assert(record.sequence == i);
        assert(record.event_id == DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY);
        assert(record.uptime_ms > 0U);
    }
    assert(!desk_watcher_log_source_try_dequeue(&record));
    assert(desk_watcher_log_source_publish_camera_capture_empty());
    assert(desk_watcher_log_source_try_dequeue(&record));
    assert(record.sequence == 21U);

    assert(!desk_watcher_log_source_try_dequeue(NULL));
    assert(!desk_watcher_log_source_take_gap(NULL));

    /* Disconnect ends the source stream: queued records/gaps are discarded,
     * with no synthetic DISCONNECTED gap and no sequence rollback. */
    for (uint64_t i = 0U; i < DESK_WATCHER_LOG_SOURCE_CAPACITY; ++i) {
        assert(desk_watcher_log_source_publish_camera_capture_empty());
    }
    assert(!desk_watcher_log_source_publish_camera_capture_empty());
    desk_watcher_log_source_set_enabled(false);
    assert(!desk_watcher_log_source_is_enabled());
    assert(!desk_watcher_log_source_try_dequeue(&record));
    assert(!desk_watcher_log_source_take_gap(&gap));
    assert(desk_watcher_log_source_next_sequence() == 39U);
    assert(!desk_watcher_log_source_publish_camera_capture_empty());
    assert(desk_watcher_log_source_next_sequence() == 39U);

    desk_watcher_log_source_set_enabled(true);
    assert(desk_watcher_log_source_publish_camera_capture_empty());
    assert(desk_watcher_log_source_try_dequeue(&record));
    assert(record.sequence == 39U);
    return 0;
}
