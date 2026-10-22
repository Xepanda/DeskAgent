#ifndef DESK_WATCHER_CAMERA_BUS_H
#define DESK_WATCHER_CAMERA_BUS_H

#include <stdbool.h>

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
