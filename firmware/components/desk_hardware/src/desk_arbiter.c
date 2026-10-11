/**
 * @file desk_arbiter.c
 * @brief Unified Resource Arbiter Implementation for Desk Agent R1.
 */

#include "desk_arbiter.h"
#include <string.h>
#include <stdio.h>

#ifdef ESP_PLATFORM
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "esp_timer.h"
#else
#include <time.h>
#endif

typedef struct {
    desk_resource_owner_t current_owner;
    uint32_t priority;
} desk_arbiter_entry_t;

static desk_arbiter_entry_t s_resources[DESK_RES_MAX];
static desk_camera_lease_t s_camera_lease;
static bool s_initialized = false;

static uint32_t get_owner_priority(desk_resource_owner_t owner)
{
    switch (owner) {
        case DESK_OWNER_SYSTEM_ALERT:   return 100;
        case DESK_OWNER_TCLAW_VOICE:    return 80;
        case DESK_OWNER_LAN_CAMERA:     return 60;
        case DESK_OWNER_SDK_REMOTE:     return 40;
        case DESK_OWNER_IDLE:           return 10;
        default:                        return 0;
    }
}

static uint32_t get_time_ms(void)
{
#ifdef ESP_PLATFORM
    return (uint32_t)(esp_timer_get_time() / 1000);
#else
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (uint32_t)(ts.tv_sec * 1000 + ts.tv_nsec / 1000000);
#endif
}

desk_arbiter_status_t desk_arbiter_init(void)
{
    for (int i = 0; i < DESK_RES_MAX; i++) {
        s_resources[i].current_owner = DESK_OWNER_NONE;
        s_resources[i].priority = 0;
    }
    memset(&s_camera_lease, 0, sizeof(s_camera_lease));
    s_initialized = true;
    return DESK_ARBITER_OK;
}

desk_arbiter_status_t desk_arbiter_acquire(desk_resource_type_t resource,
                                           desk_resource_owner_t requester,
                                           uint32_t timeout_ms)
{
    if (!s_initialized || resource >= DESK_RES_MAX) {
        return DESK_ARBITER_ERR_INVALID;
    }

    uint32_t req_prio = get_owner_priority(requester);
    uint32_t curr_prio = s_resources[resource].priority;

    /* If motion is requested while camera lease is active, check priority */
    if (resource == DESK_RES_MOTION_GIMBAL && s_camera_lease.active) {
        if (get_time_ms() < s_camera_lease.expires_at_ms) {
            /* Camera viewing is active. Only explicit manual remote control allowed;
             * automatic voice/expression tracking is locked out. */
            if (requester == DESK_OWNER_TCLAW_VOICE) {
                return DESK_ARBITER_ERR_BUSY;
            }
        } else {
            /* Lease expired */
            s_camera_lease.active = false;
        }
    }

    /* Check audio conflict: TClaw voice dialogue has priority over remote SDK playback */
    if (resource == DESK_RES_AUDIO_PLAYBACK || resource == DESK_RES_AUDIO_RECORD) {
        if (s_resources[resource].current_owner == DESK_OWNER_TCLAW_VOICE &&
            requester == DESK_OWNER_SDK_REMOTE) {
            return DESK_ARBITER_ERR_BUSY;
        }
    }

    /* Preemption if higher priority, or if resource is unowned */
    if (s_resources[resource].current_owner == DESK_OWNER_NONE ||
        req_prio >= curr_prio) {
        s_resources[resource].current_owner = requester;
        s_resources[resource].priority = req_prio;
        return DESK_ARBITER_OK;
    }

    return DESK_ARBITER_ERR_BUSY;
}

void desk_arbiter_release(desk_resource_type_t resource, desk_resource_owner_t owner)
{
    if (!s_initialized || resource >= DESK_RES_MAX) return;

    if (s_resources[resource].current_owner == owner) {
        s_resources[resource].current_owner = DESK_OWNER_NONE;
        s_resources[resource].priority = 0;
    }
}

desk_resource_owner_t desk_arbiter_get_owner(desk_resource_type_t resource)
{
    if (!s_initialized || resource >= DESK_RES_MAX) return DESK_OWNER_NONE;
    return s_resources[resource].current_owner;
}

desk_arbiter_status_t desk_arbiter_camera_acquire_lease(const char *client_id, uint32_t lease_ms)
{
    if (!client_id || lease_ms == 0) return DESK_ARBITER_ERR_INVALID;

    uint32_t now = get_time_ms();
    if (s_camera_lease.active && now < s_camera_lease.expires_at_ms) {
        if (strncmp(s_camera_lease.client_id, client_id, sizeof(s_camera_lease.client_id)) != 0) {
            return DESK_ARBITER_ERR_BUSY; /* Held by another client */
        }
    }

    strncpy(s_camera_lease.client_id, client_id, sizeof(s_camera_lease.client_id) - 1);
    s_camera_lease.lease_duration_ms = lease_ms;
    s_camera_lease.expires_at_ms = now + lease_ms;
    s_camera_lease.active = true;

    desk_arbiter_acquire(DESK_RES_CAMERA_SENSOR, DESK_OWNER_LAN_CAMERA, 0);
    return DESK_ARBITER_OK;
}

desk_arbiter_status_t desk_arbiter_camera_renew_lease(const char *client_id, uint32_t lease_ms)
{
    return desk_arbiter_camera_acquire_lease(client_id, lease_ms);
}

void desk_arbiter_camera_release_lease(const char *client_id)
{
    if (s_camera_lease.active &&
        strncmp(s_camera_lease.client_id, client_id, sizeof(s_camera_lease.client_id)) == 0) {
        s_camera_lease.active = false;
        desk_arbiter_release(DESK_RES_CAMERA_SENSOR, DESK_OWNER_LAN_CAMERA);
    }
}

bool desk_arbiter_camera_is_lease_active(void)
{
    if (!s_camera_lease.active) return false;
    if (get_time_ms() >= s_camera_lease.expires_at_ms) {
        s_camera_lease.active = false;
        desk_arbiter_release(DESK_RES_CAMERA_SENSOR, DESK_OWNER_LAN_CAMERA);
        return false;
    }
    return true;
}
