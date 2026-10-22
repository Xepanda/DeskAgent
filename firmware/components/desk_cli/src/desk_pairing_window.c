#include "desk_pairing_window.h"

#include <stddef.h>
#include <stdio.h>
#include <string.h>

#define DESK_PAIRING_RANDOM_MAX_REJECTIONS 128U

static void pairing_secure_zero(void *buffer, size_t size)
{
    volatile unsigned char *cursor = (volatile unsigned char *)buffer;
    while (size-- > 0) *cursor++ = 0;
}

static void pairing_lock(const desk_pairing_window_ops_t *ops)
{
    if (ops->lock) ops->lock(ops->context);
}

static void pairing_unlock(const desk_pairing_window_ops_t *ops)
{
    if (ops->unlock) ops->unlock(ops->context);
}

static void pairing_clear_locked(desk_pairing_window_t *window, const desk_pairing_window_ops_t *ops)
{
    (void)ops->set_pairing_code(NULL, ops->context);
    window->expiry_deadline_us = 0;
    window->active = false;
    ops->clear_display(ops->context);
}

static bool pairing_generate_code(const desk_pairing_window_ops_t *ops, char code[7])
{
    const uint32_t range_limit = (UINT32_MAX / DESK_PAIRING_CODE_SPACE) * DESK_PAIRING_CODE_SPACE;
    uint32_t random_value;
    for (uint32_t attempt = 0; attempt < DESK_PAIRING_RANDOM_MAX_REJECTIONS; ++attempt) {
        random_value = ops->random_u32(ops->context);
        if (random_value < range_limit) {
            (void)snprintf(code, 7, "%06lu", (unsigned long)(random_value % DESK_PAIRING_CODE_SPACE));
            return true;
        }
    }
    pairing_secure_zero(code, 7);
    return false;
}

static bool pairing_ops_valid(const desk_pairing_window_ops_t *ops)
{
    return ops && ops->random_u32 && ops->set_pairing_code && ops->show_pairing_code &&
           ops->clear_display && ops->monotonic_time_us && ops->start_expiry_timer &&
           ops->stop_expiry_timer;
}

void desk_pairing_window_init(desk_pairing_window_t *window)
{
    if (!window) return;
    memset(window, 0, sizeof(*window));
}

int desk_pairing_window_open(desk_pairing_window_t *window, const desk_pairing_window_ops_t *ops)
{
    if (!window || !pairing_ops_valid(ops)) return -1;

    pairing_lock(ops);
    ops->stop_expiry_timer(ops->context);
    pairing_clear_locked(window, ops);

    char generated_code[7] = {0};
    if (!pairing_generate_code(ops, generated_code)) {
        pairing_unlock(ops);
        return -1;
    }

    if (ops->set_pairing_code(generated_code, ops->context) != 0) {
        pairing_secure_zero(generated_code, sizeof(generated_code));
        pairing_clear_locked(window, ops);
        pairing_unlock(ops);
        return -1;
    }

    const int64_t now_us = ops->monotonic_time_us(ops->context);
    if (now_us < 0 || now_us > INT64_MAX - (int64_t)DESK_PAIRING_WINDOW_TTL_US) {
        pairing_secure_zero(generated_code, sizeof(generated_code));
        pairing_clear_locked(window, ops);
        pairing_unlock(ops);
        return -1;
    }
    window->expiry_deadline_us = now_us + (int64_t)DESK_PAIRING_WINDOW_TTL_US;
    if (ops->start_expiry_timer(DESK_PAIRING_WINDOW_TTL_US, ops->context) != 0) {
        pairing_secure_zero(generated_code, sizeof(generated_code));
        ops->stop_expiry_timer(ops->context);
        pairing_clear_locked(window, ops);
        pairing_unlock(ops);
        return -1;
    }

    /* The code becomes visible only after setter and timer setup both succeed. */
    if (ops->show_pairing_code(generated_code, ops->context) != 0) {
        pairing_secure_zero(generated_code, sizeof(generated_code));
        ops->stop_expiry_timer(ops->context);
        pairing_clear_locked(window, ops);
        pairing_unlock(ops);
        return -1;
    }

    pairing_secure_zero(generated_code, sizeof(generated_code));
    window->active = true;
    pairing_unlock(ops);
    return 0;
}

void desk_pairing_window_close(desk_pairing_window_t *window, const desk_pairing_window_ops_t *ops)
{
    if (!window || !pairing_ops_valid(ops)) return;
    pairing_lock(ops);
    ops->stop_expiry_timer(ops->context);
    pairing_clear_locked(window, ops);
    pairing_unlock(ops);
}

void desk_pairing_window_expire(desk_pairing_window_t *window, const desk_pairing_window_ops_t *ops)
{
    if (!window || !pairing_ops_valid(ops)) return;
    pairing_lock(ops);
    if (window->active && window->expiry_deadline_us > 0 &&
        ops->monotonic_time_us(ops->context) >= window->expiry_deadline_us) {
        pairing_clear_locked(window, ops);
    }
    pairing_unlock(ops);
}
