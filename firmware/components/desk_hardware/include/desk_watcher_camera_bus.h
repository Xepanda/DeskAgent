#ifndef DESK_WATCHER_CAMERA_BUS_H
#define DESK_WATCHER_CAMERA_BUS_H

#include <stdbool.h>
#include <stdint.h>

/* Convert an absolute microsecond deadline to a finite scheduler-tick wait.
 * Round up so a positive sub-tick remainder still receives one tick; callers
 * must recheck the absolute deadline after acquisition. */
static inline bool desk_watcher_camera_bus_deadline_ticks(uint64_t deadline_us,
                                                           uint64_t now_us,
                                                           uint64_t tick_period_us,
                                                           uint64_t max_ticks,
                                                           uint64_t *out_ticks)
{
    if (!out_ticks) return false;
    *out_ticks = 0;
    if (tick_period_us == 0 || max_ticks == 0 || now_us >= deadline_us) return false;

    uint64_t remaining_us = deadline_us - now_us;
    uint64_t ticks = remaining_us / tick_period_us;
    if (remaining_us % tick_period_us != 0) ++ticks;
    if (ticks > max_ticks) ticks = max_ticks;
    if (ticks == 0) return false;
    *out_ticks = ticks;
    return true;
}

/* Preserve timeout as a caller deadline result; other acquire failures remain
 * SPI errors. The injected values keep this classification host-testable. */
static inline int desk_watcher_camera_bus_acquire_error(int acquire_error,
                                                         int timeout_error,
                                                         int timeout_result,
                                                         int other_result)
{
    return acquire_error == timeout_error ? timeout_result : other_result;
}

/* Track the per-capture SPI bus lease independently from the camera mutex. */
static inline void desk_watcher_camera_bus_set_acquired(bool *owned, bool acquired)
{
    if (owned) *owned = acquired;
}

/* Atomically claim the one release action, so cleanup paths cannot double-release. */
static inline bool desk_watcher_camera_bus_take_release(bool *owned)
{
    if (!owned || !*owned) return false;
    *owned = false;
    return true;
}

#endif
