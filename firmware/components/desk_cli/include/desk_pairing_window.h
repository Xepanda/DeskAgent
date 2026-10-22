#ifndef DESK_PAIRING_WINDOW_H
#define DESK_PAIRING_WINDOW_H

#include <stdbool.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

#define DESK_PAIRING_WINDOW_TTL_US (300ULL * 1000ULL * 1000ULL)
#define DESK_PAIRING_CODE_SPACE 1000000U

typedef struct {
    uint32_t (*random_u32)(void *context);
    int (*set_pairing_code)(const char *code, void *context);
    int (*show_pairing_code)(const char *code, void *context);
    void (*clear_display)(void *context);
    int64_t (*monotonic_time_us)(void *context);
    int (*start_expiry_timer)(uint64_t duration_us, void *context);
    void (*stop_expiry_timer)(void *context);
    void (*lock)(void *context);
    void (*unlock)(void *context);
    void *context;
} desk_pairing_window_ops_t;

typedef struct {
    int64_t expiry_deadline_us;
    bool active;
} desk_pairing_window_t;

void desk_pairing_window_init(desk_pairing_window_t *window);
int desk_pairing_window_open(desk_pairing_window_t *window, const desk_pairing_window_ops_t *ops);
void desk_pairing_window_close(desk_pairing_window_t *window, const desk_pairing_window_ops_t *ops);
void desk_pairing_window_expire(desk_pairing_window_t *window, const desk_pairing_window_ops_t *ops);

#ifdef __cplusplus
}
#endif

#endif
