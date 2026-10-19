#ifndef __QQBOT_CHANNEL_H__
#define __QQBOT_CHANNEL_H__

#include "im_platform.h"

#ifdef __cplusplus
extern "C" {
#endif

OPERATE_RET qqbot_channel_init(void);
OPERATE_RET qqbot_channel_start(void);
OPERATE_RET qqbot_send_message(const char *chat_id, const char *text);
OPERATE_RET qqbot_set_app_id(const char *app_id);
OPERATE_RET qqbot_set_client_secret(const char *secret);

#ifdef __cplusplus
}
#endif

#endif /* __QQBOT_CHANNEL_H__ */
