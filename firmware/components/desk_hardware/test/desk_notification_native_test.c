#include "desk_notification.h"
#include "desk_hardware.h"
#include "desk_arbiter.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>
#include <time.h>

static int s_display_calls;
static int s_audio_calls;
static int s_status_calls;
static int s_summary_ack_calls;
static char s_expected_preset[32] = "alert";
static char s_expected_display[64] = "Missed: Take medicine 2035-01-01 18:00 UTC+08:00 +2";
static desk_notification_status_event_t s_events[32];
static char s_ack_id[DESK_NOTIFICATION_SUMMARY_ID_MAX + 1];
static char s_ack_source[DESK_NOTIFICATION_ACK_SOURCE_MAX + 1];

static int mock_init(void) { return DESK_HW_OK; }
static int mock_display(const char *preset, const char *text)
{
    if (strcmp(preset, "alert") == 0) {
        assert(strcmp(preset, s_expected_preset) == 0);
        assert(strcmp(text, s_expected_display) == 0);
        ++s_display_calls;
    } else {
        assert(strcmp(preset, "idle") == 0 && strcmp(text, "Ready") == 0);
    }
    return DESK_HW_OK;
}
static int mock_audio(const int16_t *pcm, size_t samples, uint32_t rate)
{
    assert(pcm && samples && rate == 16000);
    ++s_audio_calls;
    return DESK_HW_OK;
}
static void on_status(const desk_notification_status_event_t *event, void *user_data)
{
    (void)user_data;
    assert(s_status_calls < (int)(sizeof(s_events) / sizeof(s_events[0])));
    s_events[s_status_calls++] = *event;
}
static void on_summary_ack(const char *id, const char *source, void *user_data)
{
    (void)user_data;
    ++s_summary_ack_calls;
    snprintf(s_ack_id, sizeof(s_ack_id), "%s", id);
    snprintf(s_ack_source, sizeof(s_ack_source), "%s", source);
}
static void timestamp(char *out, size_t capacity, time_t epoch)
{
    struct tm value;
    assert(gmtime_r(&epoch, &value));
    assert(strftime(out, capacity, "%Y-%m-%dT%H:%M:%SZ", &value) > 0);
}
static desk_notification_input_t notification(const char *id, const char *expires)
{
    desk_notification_input_t input = {0};
    snprintf(input.notification_id, sizeof(input.notification_id), "%s", id);
    snprintf(input.reminder_id, sizeof(input.reminder_id), "reminder-1");
    snprintf(input.occurrence_id, sizeof(input.occurrence_id), "%s", id);
    snprintf(input.title, sizeof(input.title), "Take medicine");
    snprintf(input.scheduled_at, sizeof(input.scheduled_at), "2035-01-01T10:00:00Z");
    snprintf(input.expires_at, sizeof(input.expires_at), "%s", expires);
    return input;
}

int main(void)
{
    const desk_board_driver_ops_t ops = {
        .driver_name = "notification-native-test",
        .init = mock_init,
        .audio_play_pcm = mock_audio,
        .display_expression = mock_display,
    };
    assert(desk_arbiter_init() == DESK_ARBITER_OK);
    assert(desk_hardware_register_driver_ops(&ops) == DESK_HW_OK);
    desk_hardware_set_strict_mode(false);
    assert(desk_hardware_set_screen_expression("idle", "Ready") == DESK_HW_OK);
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_HW_OK);
    assert(desk_hardware_notification_register_summary_ack_callback(on_summary_ack, NULL) == DESK_HW_OK);

    desk_notification_summary_item_t summary_item = {
        .reminder_id = "reminder-1",
        .occurrence_id = "occurrence-1",
        .title = "Take medicine",
        .scheduled_at = "2035-01-01T10:00:00Z",
        .timezone = "Asia/Shanghai",
        .display_time = "2035-01-01 18:00 UTC+08:00",
    };
    char created[32], expires[32];
    time_t now = time(NULL);
    timestamp(created, sizeof(created), now);
    timestamp(expires, sizeof(expires), now + 23 * 3600);
    desk_notification_summary_input_t summary = {
        .summary_id = "summary:stable-001",
        .batch_id = "batch-001",
        .created_at = created,
        .expires_at = expires,
        .items = &summary_item,
        .item_count = 1,
        .omitted_count = 2,
    };
    assert(desk_hardware_notification_deliver_summary(&summary) == DESK_HW_OK);
    assert(desk_hardware_notification_deliver_summary(&summary) == DESK_HW_OK);
    assert(desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 0) ==
           DESK_ARBITER_OK);
    assert(desk_hardware_notification_process_pending() == DESK_HW_OK);
    assert(s_display_calls == 1 && s_audio_calls == 0);
    snprintf(s_expected_preset, sizeof(s_expected_preset), "idle");
    snprintf(s_expected_display, sizeof(s_expected_display), "Ready");
    assert(desk_hardware_notification_summary_acknowledge(summary.summary_id, "explicit_ui") ==
           DESK_HW_OK);
    assert(desk_hardware_notification_summary_acknowledge(summary.summary_id, "explicit_ui") ==
           DESK_HW_OK);
    assert(s_summary_ack_calls == 1);
    assert(strcmp(s_ack_id, summary.summary_id) == 0);
    assert(strcmp(s_ack_source, "explicit_ui") == 0);
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);

    /* RAM reset clears dedupe; re-delivering the same stable ID may show again. */
    snprintf(s_expected_preset, sizeof(s_expected_preset), "idle");
    snprintf(s_expected_display, sizeof(s_expected_display), "Ready");
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_HW_OK);
    snprintf(s_expected_preset, sizeof(s_expected_preset), "alert");
    snprintf(s_expected_display, sizeof(s_expected_display),
             "Missed: Take medicine 2035-01-01 18:00 UTC+08:00 +2");
    assert(desk_hardware_notification_deliver_summary(&summary) == DESK_HW_OK);
    assert(desk_hardware_notification_process_pending() == DESK_HW_OK);
    assert(s_display_calls == 2 && s_audio_calls == 0);
    snprintf(s_expected_preset, sizeof(s_expected_preset), "idle");
    snprintf(s_expected_display, sizeof(s_expected_display), "Ready");
    assert(desk_hardware_notification_dismiss(summary.summary_id) == DESK_HW_OK);

    desk_notification_input_t item = notification("occurrence-001", "2035-01-02T10:00:00Z");
    desk_notification_status_event_t state = {0};
    snprintf(s_expected_preset, sizeof(s_expected_preset), "alert");
    snprintf(s_expected_display, sizeof(s_expected_display), "Take medicine");
    assert(desk_hardware_notification_deliver(&item, &state) == DESK_HW_OK);
    assert(state.status == DESK_NOTIFICATION_ACCEPTED);
    assert(desk_hardware_notification_deliver(&item, &state) == DESK_HW_OK);
    assert(s_status_calls == 1);
    assert(desk_hardware_notification_process_pending() == DESK_HW_OK);
    assert(s_audio_calls == 1);
    assert(desk_hardware_notification_get_status(item.notification_id, &state) == DESK_HW_OK);
    assert(state.status == DESK_NOTIFICATION_UNKNOWN);

    char expired[32];
    timestamp(created, sizeof(created), now - 2);
    timestamp(expired, sizeof(expired), now - 1);
    summary.summary_id = "summary:expired";
    summary.created_at = created;
    summary.expires_at = expired;
    assert(desk_hardware_notification_deliver_summary(&summary) == DESK_HW_ERR_TIMEOUT);
    puts("desk_notification_native_test: PASS");
    return 0;
}
