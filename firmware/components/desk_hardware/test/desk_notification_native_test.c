#include "desk_notification.h"
#include "desk_hardware.h"
#include "desk_arbiter.h"
#include <assert.h>
#include <stdio.h>
#include <string.h>

static int s_display_calls;
static int s_audio_calls;
static int s_status_calls;
static char s_expected_preset[32] = "alert";
static char s_expected_display[64] = "Late: Take medicine";
static desk_notification_status_event_t s_events[16];
/* Volatile in-process fixture only. This is not device persistence evidence. */
static uint8_t s_fake_store[128][512];
static size_t s_fake_store_len[128];
static int s_fake_store_fail_next_save;
static int s_fake_store_corrupt_next_readback;
static int s_fake_store_unavailable_next_load;
static int s_fake_store_unavailable_next_save;

static int fake_store_load(unsigned slot, void *out, size_t capacity, size_t *out_len, void *user_data)
{
    (void)user_data;
    if (slot >= 128 || !out || !out_len) return DESK_HW_ERR_INVALID_ARG;
    if (s_fake_store_unavailable_next_load) {
        s_fake_store_unavailable_next_load = 0;
        return DESK_HW_ERR_UNAVAILABLE;
    }
    if (!s_fake_store_len[slot]) return DESK_NOTIFICATION_STORE_SLOT_EMPTY;
    if (s_fake_store_len[slot] > capacity) return DESK_HW_ERR_INVALID_ARG;
    memcpy(out, s_fake_store[slot], s_fake_store_len[slot]);
    if (s_fake_store_corrupt_next_readback) {
        s_fake_store_corrupt_next_readback = 0;
        ((uint8_t *)out)[0] ^= 0xff;
    }
    *out_len = s_fake_store_len[slot];
    return DESK_HW_OK;
}
static int fake_store_save(unsigned slot, const void *record, size_t len, void *user_data)
{
    (void)user_data;
    if (slot >= 128 || !record || len > sizeof(s_fake_store[slot])) return DESK_HW_ERR_INVALID_ARG;
    if (s_fake_store_unavailable_next_save) {
        s_fake_store_unavailable_next_save = 0;
        return DESK_HW_ERR_UNAVAILABLE;
    }
    if (s_fake_store_fail_next_save) {
        s_fake_store_fail_next_save = 0;
        return DESK_HW_ERR_IO;
    }
    memcpy(s_fake_store[slot], record, len);
    s_fake_store_len[slot] = len;
    return DESK_HW_OK;
}
static int fake_store_erase(unsigned slot, void *user_data)
{
    (void)user_data;
    if (slot >= 128) return DESK_HW_ERR_INVALID_ARG;
    memset(s_fake_store[slot], 0, sizeof(s_fake_store[slot]));
    s_fake_store_len[slot] = 0;
    return DESK_HW_OK;
}

static int mock_init(void) { return DESK_HW_OK; }
static int mock_display(const char *preset, const char *text)
{
    assert(strcmp(preset, s_expected_preset) == 0);
    assert(strcmp(text, s_expected_display) == 0);
    ++s_display_calls;
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

static desk_notification_input_t notification(const char *id, const char *expires)
{
    desk_notification_input_t input = {0};
    snprintf(input.notification_id, sizeof(input.notification_id), "%s", id);
    snprintf(input.reminder_id, sizeof(input.reminder_id), "reminder-1");
    snprintf(input.occurrence_id, sizeof(input.occurrence_id), "%s", id);
    snprintf(input.title, sizeof(input.title), "Take medicine");
    snprintf(input.scheduled_at, sizeof(input.scheduled_at), "2035-01-01T10:00:00.000000Z");
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
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_NOTIFICATION_ERR_STORAGE_UNAVAILABLE);
    assert(s_status_calls == 0); /* No provider means no accepted/status event. */
    const desk_notification_store_ops_t store_ops = {
        .load = fake_store_load,
        .save = fake_store_save,
        .erase = fake_store_erase,
    };
    assert(desk_hardware_notification_register_store(&store_ops) == DESK_HW_OK);
    desk_notification_input_t uninitialized = notification("uninitialized", "2035-01-02T10:00:00.000001Z");
    desk_notification_status_event_t state = {0};
    assert(desk_hardware_notification_deliver(&uninitialized, &state) == DESK_HW_ERR_UNAVAILABLE);
    s_fake_store_unavailable_next_load = 1;
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_NOTIFICATION_ERR_STORAGE_UNAVAILABLE);
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_HW_OK);

    /* A successful callback is not enough: readback must verify the committed record. */
    desk_notification_input_t readback_failure = notification("readback-fail", "2035-01-02T10:00:00.000001Z");
    s_fake_store_corrupt_next_readback = 1;
    assert(desk_hardware_notification_deliver(&readback_failure, &state) == DESK_HW_ERR_IO);
    assert(s_status_calls == 0);
    /* Reset the volatile fixture after this deliberately ambiguous commit. */
    assert(fake_store_erase(0, NULL) == DESK_HW_OK);
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_HW_OK);

    desk_notification_input_t save_failure = notification("save-fail", "2035-01-02T10:00:00.000001Z");
    s_fake_store_fail_next_save = 1;
    assert(desk_hardware_notification_deliver(&save_failure, &state) == DESK_HW_ERR_IO);
    assert(s_status_calls == 0);
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_HW_OK);
    desk_notification_input_t provider_down = notification("provider-down", "2035-01-02T10:00:00.000001Z");
    s_fake_store_unavailable_next_save = 1;
    assert(desk_hardware_notification_deliver(&provider_down, &state) == DESK_NOTIFICATION_ERR_STORAGE_UNAVAILABLE);
    assert(s_status_calls == 0);
    assert(desk_hardware_notification_process_pending() == DESK_HW_OK);
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_HW_OK);

    desk_notification_input_t input = notification("occurrence-001", "2035-01-02T10:00:00.000001Z");
    input.is_catchup = true;
    /* The display budget is 57 title bytes; do not split the 3-byte CJK codepoint. */
    memset(input.title, 'A', 56);
    memcpy(input.title + 56, "中", 4);
    memcpy(s_expected_display, "Late: ", 6);
    memset(s_expected_display + 6, 'A', 56);
    s_expected_display[62] = '\0';
    assert(desk_hardware_notification_deliver(&input, &state) == DESK_HW_OK);
    assert(state.status == DESK_NOTIFICATION_ACCEPTED);
    assert(s_status_calls == 1);

    desk_notification_input_t invalid_title = notification("bad-utf8", "2035-01-02T10:00:00.000001Z");
    invalid_title.title[0] = (char)0xc0; /* Invalid overlong UTF-8 lead byte. */
    invalid_title.title[1] = '\0';
    assert(desk_hardware_notification_deliver(&invalid_title, &state) == DESK_HW_ERR_INVALID_ARG);

    desk_notification_input_t wrong_id = input;
    snprintf(wrong_id.notification_id, sizeof(wrong_id.notification_id), "not-the-occurrence");
    assert(desk_hardware_notification_deliver(&wrong_id, &state) == DESK_HW_ERR_INVALID_ARG);

    /* Duplicate occurrence is reported but is not queued or played twice. */
    assert(desk_hardware_notification_deliver(&input, &state) == DESK_HW_OK);
    assert(state.status == DESK_NOTIFICATION_ACCEPTED);
    assert(s_status_calls == 1);

    assert(desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 0) == DESK_ARBITER_OK);
    assert(desk_hardware_notification_process_pending() == DESK_HW_ERR_BUSY);
    assert(s_display_calls == 1 && s_audio_calls == 0);
    assert(s_events[s_status_calls - 1].status == DESK_NOTIFICATION_RENDERED);
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
    assert(desk_hardware_notification_process_pending() == DESK_HW_OK);
    assert(s_display_calls == 1);
    assert(s_audio_calls == 1);
    assert(s_events[s_status_calls - 1].status == DESK_NOTIFICATION_UNKNOWN);
    assert(desk_hardware_notification_get_status("occurrence-001", &state) == DESK_HW_OK);
    assert(state.status == DESK_NOTIFICATION_UNKNOWN);

    /* Re-init reads the same volatile fixture; this is not reboot persistence proof. */
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_HW_OK);
    assert(desk_hardware_notification_deliver(&input, &state) == DESK_HW_OK);
    assert(state.status == DESK_NOTIFICATION_UNKNOWN);
    assert(desk_hardware_notification_process_pending() == DESK_HW_OK);
    assert(s_audio_calls == 1);

    desk_notification_input_t invalid = notification("bad-1", "2000-01-01T00:00:00Z");
    snprintf(invalid.scheduled_at, sizeof(invalid.scheduled_at), "1999-12-31T00:00:00Z");
    assert(desk_hardware_notification_deliver(&invalid, &state) == DESK_HW_ERR_TIMEOUT);
    assert(state.status == DESK_NOTIFICATION_EXPIRED);

    /* ACK dismisses the volatile overlay and restores the latest base state. */
    snprintf(s_expected_preset, sizeof(s_expected_preset), "idle");
    snprintf(s_expected_display, sizeof(s_expected_display), "Ready");
    assert(desk_hardware_notification_acknowledge("occurrence-001", "explicit_ui") == DESK_HW_OK);
    assert(s_events[s_status_calls - 1].status == DESK_NOTIFICATION_ACKNOWLEDGED);
    assert(s_events[s_status_calls - 1].acknowledged);
    assert(strcmp(s_events[s_status_calls - 1].ack_source, "explicit_ui") == 0);
    assert(desk_hardware_notification_get_status("occurrence-001", &state) == DESK_HW_OK);
    assert(state.status == DESK_NOTIFICATION_UNKNOWN && state.acknowledged);

    /* Corrupt persisted bytes fail initialization closed; no notification work resumes. */
    s_fake_store[0][0] ^= 0xff;
    assert(desk_hardware_notification_init(on_status, NULL) == DESK_HW_ERR_IO);
    assert(desk_hardware_notification_deliver(&input, &state) == DESK_HW_ERR_UNAVAILABLE);

    puts("desk_notification_native_test: PASS");
    return 0;
}
