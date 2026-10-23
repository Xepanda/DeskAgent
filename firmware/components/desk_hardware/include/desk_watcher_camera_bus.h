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

/* Reject a synchronous SPI transaction that cannot plausibly finish before
 * the caller deadline. The guard covers driver/DMA setup beyond wire time. */
static inline bool desk_watcher_camera_spi_deadline_available(uint64_t deadline_us,
                                                                uint64_t now_us,
                                                                uint64_t transfer_bytes,
                                                                uint64_t spi_hz,
                                                                uint64_t guard_us)
{
    if (now_us >= deadline_us || transfer_bytes == 0 || spi_hz == 0 ||
        transfer_bytes > UINT64_MAX / 8000000ULL) return false;
    uint64_t wire_bits = transfer_bytes * 8ULL;
    uint64_t wire_us = (wire_bits * 1000000ULL + spi_hz - 1ULL) / spi_hz;
    return deadline_us - now_us >= wire_us + guard_us;
}

/* Reserve at most one quarter of the total operation, capped by the caller's
 * cleanup limit, so normal work cannot consume the idle-restore window. */
static inline bool desk_watcher_camera_work_deadline(uint64_t start_us,
                                                      uint64_t overall_deadline_us,
                                                      uint64_t max_cleanup_us,
                                                      uint64_t *out_work_deadline_us)
{
    if (!out_work_deadline_us || start_us >= overall_deadline_us || max_cleanup_us == 0) return false;
    const uint64_t total_us = overall_deadline_us - start_us;
    uint64_t reserve_us = total_us / 4ULL;
    if (reserve_us > max_cleanup_us) reserve_us = max_cleanup_us;
    if (reserve_us == 0 || reserve_us >= total_us) return false;
    *out_work_deadline_us = overall_deadline_us - reserve_us;
    return true;
}

/* A failed idle restore is observable and forces recovery before another capture. */
static inline int desk_watcher_camera_cleanup_result(int capture_result,
                                                      bool idle_restored,
                                                      int control_error,
                                                      bool *needs_recovery)
{
    if (!needs_recovery) return capture_result;
    *needs_recovery = !idle_restored;
    return !idle_restored && capture_result == 0 ? control_error : capture_result;
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
