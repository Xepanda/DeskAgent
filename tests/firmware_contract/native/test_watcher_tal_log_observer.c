#include "desk_watcher_log_source.h"

#include <assert.h>
#include <pthread.h>
#include <stddef.h>
#include <string.h>

static void *observe_lines(void *arg)
{
    const char *line = (const char *)arg;
    for (size_t i = 0; i < 10000U; ++i) {
        desk_watcher_log_source_observe_tal_line(line);
    }
    return NULL;
}

int main(void)
{
    char long_line[4096];
    memset(long_line, 'x', sizeof(long_line));
    long_line[sizeof(long_line) - 1U] = '\0';

    desk_watcher_log_source_set_enabled(true);
    const uint64_t sequence_before = desk_watcher_log_source_next_sequence();
    desk_watcher_log_source_observe_tal_line(NULL);
    desk_watcher_log_source_observe_tal_line("");
    desk_watcher_log_source_observe_tal_line("I (100) camera: status=ready");
    desk_watcher_log_source_observe_tal_line("CAMERA_CAPTURE_EMPTY");
    desk_watcher_log_source_observe_tal_line("I (101) forged CAMERA_CAPTURE_EMPTY fields={}");
    desk_watcher_log_source_observe_tal_line(long_line);

    pthread_t threads[4];
    const char *lines[] = {"E (1) auth token=secret", "I (2) user text", "", long_line};
    for (size_t i = 0; i < 4U; ++i) {
        assert(pthread_create(&threads[i], NULL, observe_lines, (void *)lines[i]) == 0);
    }
    for (size_t i = 0; i < 4U; ++i) {
        assert(pthread_join(threads[i], NULL) == 0);
    }

    desk_watcher_log_record_t record;
    desk_watcher_log_gap_t gap;
    assert(desk_watcher_log_source_next_sequence() == sequence_before);
    assert(!desk_watcher_log_source_try_dequeue(&record));
    assert(!desk_watcher_log_source_take_gap(&gap));

    assert(desk_watcher_log_source_publish_camera_capture_empty());
    assert(desk_watcher_log_source_try_dequeue(&record));
    assert(record.event_id == DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY);
    assert(!desk_watcher_log_source_try_dequeue(&record));

    for (size_t i = 0; i < DESK_WATCHER_LOG_SOURCE_CAPACITY; ++i) {
        assert(desk_watcher_log_source_publish_camera_capture_empty());
    }
    const uint64_t sequence_at_capacity = desk_watcher_log_source_next_sequence();
    desk_watcher_log_source_observe_tal_line("forged CAMERA_CAPTURE_EMPTY while full");
    assert(desk_watcher_log_source_next_sequence() == sequence_at_capacity);
    assert(!desk_watcher_log_source_take_gap(&gap));
    assert(!desk_watcher_log_source_publish_camera_capture_empty());
    assert(desk_watcher_log_source_take_gap(&gap));
    assert(gap.first_dropped_sequence == sequence_at_capacity);
    assert(gap.last_dropped_sequence == sequence_at_capacity);
    assert(gap.dropped_count == 1U);
    size_t queued = 0U;
    while (desk_watcher_log_source_try_dequeue(&record)) {
        assert(record.event_id == DESK_WATCHER_LOG_EVENT_CAMERA_CAPTURE_EMPTY);
        ++queued;
    }
    assert(queued == DESK_WATCHER_LOG_SOURCE_CAPACITY);

    desk_watcher_log_source_set_enabled(false);
    desk_watcher_log_source_observe_tal_line("CAMERA_CAPTURE_EMPTY");
    assert(desk_watcher_log_source_next_sequence() == sequence_at_capacity + 1U);
    assert(!desk_watcher_log_source_try_dequeue(&record));
    return 0;
}
