/**
 * @file tclaw_chat.h
 * @brief tclaw_chat module: AI stream event bridge for TClaw
 * @version 0.1
 * @date 2025-03-25
 */

#ifndef __TCLAW_CHAT_H__
#define __TCLAW_CHAT_H__

#include "tuya_cloud_types.h"
#include "ai_chat_main.h"

#ifdef __cplusplus
extern "C" {
#endif

/***********************************************************
************************macro define************************
***********************************************************/

/***********************************************************
***********************typedef define***********************
***********************************************************/

/***********************************************************
********************function declaration********************
***********************************************************/
OPERATE_RET tclaw_chat_init(void);

typedef void (*tclaw_chat_event_hook_t)(AI_NOTIFY_EVENT_T *event);
void tclaw_chat_register_event_hook(tclaw_chat_event_hook_t hook);

#ifdef __cplusplus
}
#endif

#endif /* __TCLAW_CHAT_H__ */
