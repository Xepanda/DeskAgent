#ifndef DESK_APP_STACK_HWM_CACHE_H
#define DESK_APP_STACK_HWM_CACHE_H

#include <stdbool.h>
#include <stdint.h>
#include "freertos/FreeRTOS.h"

/* A small SMP-safe snapshot. Readers never retain or dereference a task handle. */
typedef struct {
    portMUX_TYPE mux;
    uint32_t bytes;
    bool valid;
} desk_app_stack_hwm_cache_t;

#define DESK_APP_STACK_HWM_CACHE_INITIALIZER \
    { .mux = portMUX_INITIALIZER_UNLOCKED, .bytes = 0U, .valid = false }

static inline void desk_app_stack_hwm_cache_publish(desk_app_stack_hwm_cache_t *cache,
                                                     uint32_t bytes)
{
    portENTER_CRITICAL(&cache->mux);
    cache->bytes = bytes;
    if (!cache->valid) cache->valid = true;
    portEXIT_CRITICAL(&cache->mux);
}

static inline bool desk_app_stack_hwm_cache_read(desk_app_stack_hwm_cache_t *cache,
                                                  uint32_t *bytes)
{
    bool valid;
    portENTER_CRITICAL(&cache->mux);
    valid = cache->valid;
    *bytes = valid ? cache->bytes : 0U;
    portEXIT_CRITICAL(&cache->mux);
    return valid;
}

#endif
