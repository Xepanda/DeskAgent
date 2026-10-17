/**
 * @file desk_arbiter.h
 * @brief Desk Agent R1 Unified Resource Arbiter Interface.
 *
 * Enforces single-owner arbitration for Audio, Camera, Motion, and Display.
 * Prevents race conditions between local TClaw dialogue, background tasks,
 * and external Python SDK / Companion requests.
 *
 * @copyright Copyright (c) 2026 DeskAgent Project. All rights reserved.
 */

#ifndef DESK_ARBITER_H
#define DESK_ARBITER_H

#ifdef __cplusplus
extern "C" {
#endif

#include <stdbool.h>
#include <stdint.h>

/**
 * @brief Resource owner identities.
 */
typedef enum {
    DESK_OWNER_NONE = 0,
    DESK_OWNER_IDLE,
    DESK_OWNER_TCLAW_VOICE,      /* Local TClaw / Tuya Cloud voice interaction (highest priority) */
    DESK_OWNER_SDK_REMOTE,       /* External Python SDK / Host application command */
    DESK_OWNER_LAN_CAMERA,       /* LAN Camera viewing session lease */
    DESK_OWNER_SYSTEM_ALERT      /* High-priority alarm or safety fault */
} desk_resource_owner_t;

/**
 * @brief Resource types subject to arbitration.
 */
typedef enum {
    DESK_RES_AUDIO_PLAYBACK = 0, /* Speaker DAC / I2S DMA output */
    DESK_RES_AUDIO_RECORD,       /* Mic ADC / I2S DMA input */
    DESK_RES_CAMERA_SENSOR,      /* HX6538 Camera / SSCMA JPEG capture */
    DESK_RES_MOTION_GIMBAL,      /* STM32 Body Pan/Tilt Servos */
    DESK_RES_DISPLAY_UI,         /* LCD Screen & LVGL drawing buffer */
    DESK_RES_MAX
} desk_resource_type_t;

#define DESK_OWNER_SDK_CLIENT   DESK_OWNER_SDK_REMOTE
#define DESK_RES_DISPLAY        DESK_RES_DISPLAY_UI

/**
 * @brief Arbitration status codes.
 */
typedef enum {
    DESK_ARBITER_OK = 0,
    DESK_ARBITER_ERR_BUSY,       /* Held by higher or equal priority owner */
    DESK_ARBITER_ERR_INVALID,    /* Invalid argument */
    DESK_ARBITER_ERR_TIMEOUT     /* Timed out waiting for resource */
} desk_arbiter_status_t;

/**
 * @brief Camera viewing lease configuration.
 */
typedef struct {
    char client_id[32];
    uint32_t lease_duration_ms;
    uint32_t expires_at_ms;
    bool active;
} desk_camera_lease_t;

/**
 * @brief Initialize the unified resource arbiter.
 */
desk_arbiter_status_t desk_arbiter_init(void);

/**
 * @brief Request exclusive or prioritized ownership of a hardware resource.
 *
 * @param resource Target resource type.
 * @param requester Demanding subsystem identity.
 * @param timeout_ms Max milliseconds to wait if currently busy.
 * @return DESK_ARBITER_OK on grant, DESK_ARBITER_ERR_BUSY if denied.
 */
desk_arbiter_status_t desk_arbiter_acquire(desk_resource_type_t resource,
                                           desk_resource_owner_t requester,
                                           uint32_t timeout_ms);

/**
 * @brief Release ownership of a resource.
 *
 * @param resource Target resource type.
 * @param owner Subsystem relinquishing ownership.
 */
void desk_arbiter_release(desk_resource_type_t resource, desk_resource_owner_t owner);

/**
 * @brief Query current owner of a resource.
 */
desk_resource_owner_t desk_arbiter_get_owner(desk_resource_type_t resource);

/**
 * @brief Acquire camera viewing lease (also locks gimbal auto-tracking).
 *
 * @param client_id Unique identifier of viewing client.
 * @param lease_ms Requested lease duration in milliseconds.
 * @return DESK_ARBITER_OK if granted.
 */
desk_arbiter_status_t desk_arbiter_camera_acquire_lease(const char *client_id, uint32_t lease_ms);

/**
 * @brief Renew or release camera viewing lease.
 */
desk_arbiter_status_t desk_arbiter_camera_renew_lease(const char *client_id, uint32_t lease_ms);
void desk_arbiter_camera_release_lease(const char *client_id);
bool desk_arbiter_camera_is_lease_active(void);

#ifdef __cplusplus
}
#endif

#endif /* DESK_ARBITER_H */
