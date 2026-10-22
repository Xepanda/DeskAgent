#include "desk_sdk_bridge.h"
#include "desk_tclaw_bridge.h"

static unsigned s_pairing_consumed_notifications;
static unsigned s_sdk_reset_notifications;
static unsigned s_pairing_consumed_reentry_checks;
static unsigned s_sdk_reset_reentry_checks;

void desk_tclaw_pairing_ui_on_pairing_consumed(void)
{
    ++s_pairing_consumed_notifications;
    /* Reenter the bridge API to prove callbacks are invoked outside its mutex. */
    (void)desk_sdk_bridge_is_pairing_code_configured();
    ++s_pairing_consumed_reentry_checks;
}

void desk_tclaw_pairing_ui_on_sdk_session_reset(void)
{
    ++s_sdk_reset_notifications;
    (void)desk_sdk_bridge_is_pairing_code_configured();
    ++s_sdk_reset_reentry_checks;
}

void desk_sdk_test_pairing_ui_reset_counters(void)
{
    s_pairing_consumed_notifications = 0;
    s_sdk_reset_notifications = 0;
    s_pairing_consumed_reentry_checks = 0;
    s_sdk_reset_reentry_checks = 0;
}

unsigned desk_sdk_test_pairing_consumed_notifications(void)
{
    return s_pairing_consumed_notifications;
}

unsigned desk_sdk_test_sdk_reset_notifications(void)
{
    return s_sdk_reset_notifications;
}

unsigned desk_sdk_test_pairing_consumed_reentry_checks(void)
{
    return s_pairing_consumed_reentry_checks;
}

unsigned desk_sdk_test_sdk_reset_reentry_checks(void)
{
    return s_sdk_reset_reentry_checks;
}
