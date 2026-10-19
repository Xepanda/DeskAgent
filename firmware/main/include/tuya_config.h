/**
 * @file tuya_config.h
 * @brief Tuya product configuration with production credentials kept local.
 */

#ifndef TUYA_CONFIG_H_
#define TUYA_CONFIG_H_

#define TUYA_PRODUCT_ID "rhbbg6sazsavynoj"

#ifdef ESP_PLATFORM
#include "tuya_config_secrets.h"
#if !defined(TUYA_OPENSDK_UUID) || !defined(TUYA_OPENSDK_AUTHKEY)
#error "ESP production builds require local Tuya credentials in tuya_config_secrets.h"
#endif
#else
/* Non-production host harness values. Never used by an ESP target build. */
#ifndef TUYA_OPENSDK_UUID
#define TUYA_OPENSDK_UUID "host-test-uuid"
#endif
#ifndef TUYA_OPENSDK_AUTHKEY
#define TUYA_OPENSDK_AUTHKEY "host-test-authkey"
#endif
#endif

#endif /* TUYA_CONFIG_H_ */
