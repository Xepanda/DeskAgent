#include "desk_notification.h"
#include "desk_arbiter.h"
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>

#define NOTIFICATION_RECORDS_MAX 16
#define NOTIFICATION_SOUND_SAMPLES 1600
#define NOTIFICATION_SOUND_RATE 16000u

typedef struct {
    uint8_t occupied;
    uint8_t is_summary;
    char notification_id[DESK_NOTIFICATION_SUMMARY_ID_MAX + 1];
    char reminder_id[DESK_NOTIFICATION_REMINDER_ID_MAX + 1];
    char occurrence_id[DESK_NOTIFICATION_OCCURRENCE_ID_MAX + 1];
    char title[DESK_NOTIFICATION_TITLE_MAX + 1];
    char display_time[DESK_NOTIFICATION_DISPLAY_TIME_MAX + 1];
    uint64_t scheduled_epoch;
    uint64_t expires_epoch;
    int32_t error_code;
    uint8_t status;
    uint8_t pending;
    uint8_t rendered;
    uint8_t audio_attempted;
    uint8_t acknowledged;
    uint8_t summary_acknowledged;
    uint8_t is_catchup;
    uint64_t summary_more_count;
    char ack_source[DESK_NOTIFICATION_ACK_SOURCE_MAX + 1];
} notification_record_t;

static notification_record_t s_records[NOTIFICATION_RECORDS_MAX];
static bool s_initialized;
static desk_notification_status_cb_t s_status_cb;
static void *s_status_user_data;
static desk_notification_summary_ack_cb_t s_summary_ack_cb;
static void *s_summary_ack_user_data;
static desk_hardware_ui_overlay_handle_t s_active_overlay_handle;
static char s_active_overlay_notification_id[DESK_NOTIFICATION_SUMMARY_ID_MAX + 1];

static bool valid_title_utf8(const char *text);

static void emit_status(const char *id, desk_notification_status_t status, int error,
                        const char *source)
{
    if (!s_status_cb) return;
    desk_notification_status_event_t event = {0};
    snprintf(event.notification_id, sizeof(event.notification_id), "%s", id ? id : "");
    event.status = status;
    event.error_code = error;
    event.acknowledged = (status == DESK_NOTIFICATION_ACKNOWLEDGED);
    if (source) snprintf(event.ack_source, sizeof(event.ack_source), "%s", source);
    s_status_cb(&event, s_status_user_data);
}

/* Parse UTC RFC3339 timestamps, including Python's optional microseconds. */
static bool parse_utc(const char *text, uint64_t *out_epoch_us)
{
    int year, month, day, hour, minute, second, consumed = 0;
    if (!text || !out_epoch_us || strlen(text) < 20 ||
        sscanf(text, "%4d-%2d-%2dT%2d:%2d:%2d%n", &year, &month, &day,
               &hour, &minute, &second, &consumed) != 6 || consumed != 19 ||
        text[4] != '-' || text[7] != '-' || text[10] != 'T' || text[13] != ':' ||
        text[16] != ':') return false;
    if (year < 1970 || month < 1 || month > 12 || day < 1 || day > 31 ||
        hour > 23 || minute > 59 || second > 59) return false;
    static const unsigned month_days[] = {31,28,31,30,31,30,31,31,30,31,30,31};
    bool leap = (year % 4 == 0 && year % 100 != 0) || year % 400 == 0;
    unsigned mdays = month_days[month - 1] + (month == 2 && leap ? 1u : 0u);
    if ((unsigned)day > mdays) return false;
    int y = year - (month <= 2);
    int era = (y >= 0 ? y : y - 399) / 400;
    unsigned yoe = (unsigned)(y - era * 400);
    int shifted_month = month + (month > 2 ? -3 : 9);
    unsigned doy = (153u * (unsigned)shifted_month + 2u) / 5u + (unsigned)day - 1u;
    unsigned doe = yoe * 365u + yoe / 4u - yoe / 100u + doy;
    int64_t days = (int64_t)era * 146097 + (int64_t)doe - 719468;
    if (days < 0) return false;
    uint32_t micros = 0;
    size_t pos = (size_t)consumed;
    if (text[pos] == '.') {
        ++pos;
        unsigned digits = 0;
        while (text[pos] >= '0' && text[pos] <= '9' && digits < 6) {
            micros = micros * 10u + (unsigned)(text[pos++] - '0');
            ++digits;
        }
        if (digits == 0 || (text[pos] >= '0' && text[pos] <= '9')) return false;
        while (digits++ < 6) micros *= 10u;
    }
    if (text[pos] != 'Z' || text[pos + 1] != '\0') return false;
    *out_epoch_us = (((uint64_t)days * 86400u + (uint64_t)hour * 3600u +
                      (uint64_t)minute * 60u + (uint64_t)second) * 1000000u) + micros;
    return true;
}

static uint64_t now_epoch_us(void)
{
    struct timeval now;
    if (gettimeofday(&now, NULL) != 0 || now.tv_sec < 1577836800) return 0;
    return (uint64_t)now.tv_sec * 1000000u + (uint64_t)now.tv_usec;
}

static bool valid_text(const char *value, size_t capacity)
{
    return value && value[0] && memchr(value, '\0', capacity) != NULL;
}

static bool valid_id(const char *id, size_t capacity)
{
    if (!valid_text(id, capacity)) return false;
    for (const unsigned char *p = (const unsigned char *)id; *p; ++p) {
        if (!((*p >= 'a' && *p <= 'z') || (*p >= 'A' && *p <= 'Z') ||
              (*p >= '0' && *p <= '9') || *p == '.' || *p == '_' || *p == ':' || *p == '-'))
            return false;
    }
    return true;
}

static bool valid_display_time(const char *value)
{
    if (!valid_text(value, DESK_NOTIFICATION_DISPLAY_TIME_MAX + 1) || strlen(value) != 26 ||
        value[4] != '-' || value[7] != '-' || value[10] != ' ' || value[13] != ':' ||
        value[16] != ' ' || memcmp(value + 17, "UTC", 3) != 0 ||
        (value[20] != '+' && value[20] != '-') || value[23] != ':') return false;
    static const unsigned digit_positions[] = {
        0,1,2,3,5,6,8,9,11,12,14,15,21,22,24,25
    };
    for (size_t i = 0; i < sizeof(digit_positions) / sizeof(digit_positions[0]); ++i) {
        unsigned pos = digit_positions[i];
        if (value[pos] < '0' || value[pos] > '9') return false;
    }
    char timestamp[21];
    memcpy(timestamp, value, 10);
    timestamp[10] = 'T';
    memcpy(timestamp + 11, value + 11, 5);
    memcpy(timestamp + 16, ":00Z", 5);
    uint64_t ignored;
    if (!parse_utc(timestamp, &ignored)) return false;
    unsigned offset_hour = (unsigned)(value[21] - '0') * 10u + (unsigned)(value[22] - '0');
    unsigned offset_minute = (unsigned)(value[24] - '0') * 10u + (unsigned)(value[25] - '0');
    return offset_hour <= 14 && offset_minute <= 59 &&
           (offset_hour < 14 || offset_minute == 0);
}

static bool valid_title_utf8(const char *text)
{
    const unsigned char *p = (const unsigned char *)text;
    while (*p) {
        unsigned char lead = *p++;
        if (lead < 0x80) {
            if (lead < 0x20 || lead == 0x7f) return false;
            continue;
        }
        size_t continuation_count;
        unsigned char second_min = 0x80, second_max = 0xbf;
        if (lead >= 0xc2 && lead <= 0xdf) continuation_count = 1;
        else if (lead >= 0xe0 && lead <= 0xef) {
            continuation_count = 2;
            if (lead == 0xe0) second_min = 0xa0;
            if (lead == 0xed) second_max = 0x9f;
        } else if (lead >= 0xf0 && lead <= 0xf4) {
            continuation_count = 3;
            if (lead == 0xf0) second_min = 0x90;
            if (lead == 0xf4) second_max = 0x8f;
        } else return false;
        for (size_t i = 0; i < continuation_count; ++i) {
            unsigned char next = *p++;
            if (!next || (next & 0xc0) != 0x80) return false;
            if (i == 0 && (next < second_min || next > second_max)) return false;
        }
    }
    return true;
}

static size_t utf8_prefix(const char *text, size_t max_bytes)
{
    size_t used = 0;
    while (text[used] && used < max_bytes) {
        unsigned char lead = (unsigned char)text[used];
        size_t width = lead < 0x80 ? 1 : ((lead & 0xE0) == 0xC0 ? 2 :
                      ((lead & 0xF0) == 0xE0 ? 3 : ((lead & 0xF8) == 0xF0 ? 4 : 1)));
        if (used + width > max_bytes) break;
        used += width;
    }
    return used;
}

static int find_record(const char *id)
{
    for (unsigned i = 0; i < NOTIFICATION_RECORDS_MAX; ++i)
        if (s_records[i].occupied && strcmp(s_records[i].notification_id, id) == 0) return (int)i;
    return -1;
}

static int allocate_record(uint64_t now)
{
    for (unsigned i = 0; i < NOTIFICATION_RECORDS_MAX; ++i) {
        if (!s_records[i].occupied || s_records[i].expires_epoch <= now) {
            memset(&s_records[i], 0, sizeof(s_records[i]));
            return (int)i;
        }
    }
    return -1;
}

static void dismiss_active_overlay_for_id(const char *id)
{
    if (!s_active_overlay_handle || strcmp(s_active_overlay_notification_id, id) != 0) return;
    int rc = desk_hardware_ui_overlay_dismiss(s_active_overlay_handle);
    s_active_overlay_handle = 0;
    s_active_overlay_notification_id[0] = '\0';
    if (rc != DESK_HW_OK && rc != DESK_HW_ERR_TIMEOUT && rc != DESK_HW_ERR_UNAVAILABLE)
        printf("[NOTIFICATION][WARN] stage=overlay_dismiss rc=%d\n", rc);
}

static void fill_status(const char *id, unsigned index, desk_notification_status_event_t *out)
{
    memset(out, 0, sizeof(*out));
    snprintf(out->notification_id, sizeof(out->notification_id), "%s", id);
    out->status = (desk_notification_status_t)s_records[index].status;
    out->error_code = s_records[index].error_code;
    out->acknowledged = s_records[index].acknowledged != 0;
    if (s_records[index].acknowledged)
        snprintf(out->ack_source, sizeof(out->ack_source), "%s", s_records[index].ack_source);
}

int desk_hardware_notification_init(desk_notification_status_cb_t callback, void *user_data)
{
    if (s_active_overlay_handle) (void)desk_hardware_ui_overlay_dismiss(s_active_overlay_handle);
    s_active_overlay_handle = 0;
    s_active_overlay_notification_id[0] = '\0';
    memset(s_records, 0, sizeof(s_records));
    s_status_cb = callback;
    s_status_user_data = user_data;
    s_initialized = true;
    return DESK_HW_OK;
}

int desk_hardware_notification_deliver(const desk_notification_input_t *input,
                                       desk_notification_status_event_t *out_status)
{
    if (!s_initialized) return DESK_HW_ERR_UNAVAILABLE;
    if (!input || !out_status ||
        !valid_text(input->notification_id, sizeof(input->notification_id)) ||
        !valid_text(input->reminder_id, sizeof(input->reminder_id)) ||
        !valid_text(input->occurrence_id, sizeof(input->occurrence_id)) ||
        !valid_text(input->title, sizeof(input->title)) ||
        !valid_text(input->scheduled_at, sizeof(input->scheduled_at)) ||
        !valid_text(input->expires_at, sizeof(input->expires_at)) ||
        !valid_title_utf8(input->title)) return DESK_HW_ERR_INVALID_ARG;
    uint64_t scheduled, expires, now = now_epoch_us();
    if (!parse_utc(input->scheduled_at, &scheduled) || !parse_utc(input->expires_at, &expires) ||
        expires <= scheduled || strcmp(input->notification_id, input->occurrence_id) != 0)
        return DESK_HW_ERR_INVALID_ARG;
    if (!now) return DESK_HW_ERR_UNAVAILABLE;
    int index = find_record(input->notification_id);
    if (index >= 0) {
        notification_record_t *record = &s_records[index];
        if (record->is_summary) return DESK_HW_ERR_INVALID_ARG;
        if (record->pending && record->expires_epoch <= now) {
            record->pending = 0;
            record->status = DESK_NOTIFICATION_EXPIRED;
            record->error_code = DESK_HW_ERR_TIMEOUT;
            emit_status(input->notification_id, DESK_NOTIFICATION_EXPIRED,
                        DESK_HW_ERR_TIMEOUT, NULL);
        }
        fill_status(input->notification_id, (unsigned)index, out_status);
        return out_status->status == DESK_NOTIFICATION_EXPIRED ? DESK_HW_ERR_TIMEOUT : DESK_HW_OK;
    }
    if (expires <= now) {
        memset(out_status, 0, sizeof(*out_status));
        snprintf(out_status->notification_id, sizeof(out_status->notification_id), "%s",
                 input->notification_id);
        out_status->status = DESK_NOTIFICATION_EXPIRED;
        out_status->error_code = DESK_HW_ERR_TIMEOUT;
        emit_status(input->notification_id, DESK_NOTIFICATION_EXPIRED, DESK_HW_ERR_TIMEOUT, NULL);
        return DESK_HW_ERR_TIMEOUT;
    }
    index = allocate_record(now);
    if (index < 0) return DESK_HW_ERR_BUSY;
    notification_record_t *record = &s_records[index];
    record->occupied = 1;
    snprintf(record->notification_id, sizeof(record->notification_id), "%s", input->notification_id);
    snprintf(record->reminder_id, sizeof(record->reminder_id), "%s", input->reminder_id);
    snprintf(record->occurrence_id, sizeof(record->occurrence_id), "%s", input->occurrence_id);
    snprintf(record->title, sizeof(record->title), "%s", input->title);
    record->scheduled_epoch = scheduled;
    record->expires_epoch = expires;
    record->status = DESK_NOTIFICATION_ACCEPTED;
    record->pending = 1;
    record->is_catchup = input->is_catchup ? 1 : 0;
    fill_status(input->notification_id, (unsigned)index, out_status);
    emit_status(input->notification_id, DESK_NOTIFICATION_ACCEPTED, DESK_HW_OK, NULL);
    return DESK_HW_OK;
}

int desk_hardware_notification_deliver_summary(const desk_notification_summary_input_t *input)
{
    if (!s_initialized) return DESK_HW_ERR_UNAVAILABLE;
    if (!input || !valid_id(input->summary_id, DESK_NOTIFICATION_SUMMARY_ID_MAX + 1) ||
        !valid_id(input->batch_id, DESK_NOTIFICATION_SUMMARY_BATCH_ID_MAX + 1) ||
        !valid_text(input->created_at, DESK_NOTIFICATION_TIMESTAMP_MAX + 1) ||
        !valid_text(input->expires_at, DESK_NOTIFICATION_TIMESTAMP_MAX + 1) ||
        !input->items || input->item_count == 0 ||
        input->item_count > DESK_NOTIFICATION_SUMMARY_MAX_ITEMS) return DESK_HW_ERR_INVALID_ARG;
    uint64_t created, expires, now = now_epoch_us();
    if (!parse_utc(input->created_at, &created) || !parse_utc(input->expires_at, &expires) ||
        expires <= created || expires - created > 24ull * 60ull * 60ull * 1000000ull)
        return DESK_HW_ERR_INVALID_ARG;
    if (!now) return DESK_HW_ERR_UNAVAILABLE;
    int existing = find_record(input->summary_id);
    if (existing >= 0) {
        if (!s_records[existing].is_summary) return DESK_HW_ERR_INVALID_ARG;
        return s_records[existing].expires_epoch <= now ? DESK_HW_ERR_TIMEOUT : DESK_HW_OK;
    }
    if (expires <= now) return DESK_HW_ERR_TIMEOUT;
    for (size_t i = 0; i < input->item_count; ++i) {
        const desk_notification_summary_item_t *item = &input->items[i];
        uint64_t scheduled;
        if (!item || !valid_id(item->reminder_id, DESK_NOTIFICATION_REMINDER_ID_MAX + 1) ||
            !valid_id(item->occurrence_id, DESK_NOTIFICATION_OCCURRENCE_ID_MAX + 1) ||
            !valid_text(item->title, DESK_NOTIFICATION_SUMMARY_TITLE_MAX + 1) ||
            !valid_text(item->scheduled_at, DESK_NOTIFICATION_TIMESTAMP_MAX + 1) ||
            !valid_text(item->timezone, DESK_NOTIFICATION_TIMEZONE_MAX + 1) ||
            !valid_display_time(item->display_time) || !valid_title_utf8(item->title) ||
            !parse_utc(item->scheduled_at, &scheduled))
            return DESK_HW_ERR_INVALID_ARG;
    }
    int index = allocate_record(now);
    if (index < 0) return DESK_HW_ERR_BUSY;
    notification_record_t *record = &s_records[index];
    record->occupied = 1;
    record->is_summary = 1;
    record->pending = 1;
    record->expires_epoch = expires;
    snprintf(record->notification_id, sizeof(record->notification_id), "%s", input->summary_id);
    snprintf(record->title, sizeof(record->title), "%s", input->items[0].title);
    snprintf(record->display_time, sizeof(record->display_time), "%s",
             input->items[0].display_time);
    record->summary_more_count = (uint64_t)input->omitted_count + input->item_count - 1u;
    return DESK_HW_OK;
}

int desk_hardware_notification_register_summary_ack_callback(
    desk_notification_summary_ack_cb_t callback, void *user_data)
{
    s_summary_ack_cb = callback;
    s_summary_ack_user_data = user_data;
    return DESK_HW_OK;
}

int desk_hardware_notification_process_pending(void)
{
    if (!s_initialized) return DESK_HW_ERR_UNAVAILABLE;
    uint64_t now = now_epoch_us();
    if (!now) return DESK_HW_ERR_UNAVAILABLE;
    for (unsigned i = 0; i < NOTIFICATION_RECORDS_MAX; ++i) {
        notification_record_t *entry = &s_records[i];
        if (!entry->occupied || !entry->pending) continue;
        if (entry->expires_epoch <= now) {
            entry->pending = 0;
            if (!entry->is_summary) {
                entry->status = DESK_NOTIFICATION_EXPIRED;
                entry->error_code = DESK_HW_ERR_TIMEOUT;
                emit_status(entry->notification_id, DESK_NOTIFICATION_EXPIRED,
                            DESK_HW_ERR_TIMEOUT, NULL);
            }
            return DESK_HW_ERR_TIMEOUT;
        }
        if (!entry->rendered) {
            char text[64];
            size_t offset = 0;
            if (entry->is_summary) {
                memcpy(text, "Missed: ", 8);
                offset = 8;
                char suffix[16] = {0};
                if (entry->summary_more_count)
                    snprintf(suffix, sizeof(suffix), " +%llu",
                             (unsigned long long)entry->summary_more_count);
                size_t suffix_len = strlen(suffix);
                size_t available_title = sizeof(text) - offset - 1 - 1 -
                                         DESK_NOTIFICATION_DISPLAY_TIME_MAX - suffix_len;
                size_t title_len = utf8_prefix(entry->title, available_title);
                memcpy(text + offset, entry->title, title_len);
                offset += title_len;
                text[offset++] = ' ';
                memcpy(text + offset, entry->display_time, DESK_NOTIFICATION_DISPLAY_TIME_MAX);
                offset += DESK_NOTIFICATION_DISPLAY_TIME_MAX;
                memcpy(text + offset, suffix, suffix_len + 1);
            } else {
                if (entry->is_catchup) {
                    memcpy(text, "Late: ", 6);
                    offset = 6;
                }
                size_t title_len = utf8_prefix(entry->title, sizeof(text) - offset - 1);
                memcpy(text + offset, entry->title, title_len);
                text[offset + title_len] = '\0';
            }
            desk_hardware_ui_overlay_handle_t handle = 0;
            int rc = desk_hardware_ui_overlay_present(entry->notification_id, "alert", text,
                                                       entry->expires_epoch, &handle);
            if (rc != DESK_HW_OK) return rc;
            s_active_overlay_handle = handle;
            snprintf(s_active_overlay_notification_id, sizeof(s_active_overlay_notification_id),
                     "%s", entry->notification_id);
            entry->rendered = 1;
            if (entry->is_summary) {
                entry->pending = 0;
                return DESK_HW_OK;
            }
            entry->status = DESK_NOTIFICATION_RENDERED;
            entry->error_code = DESK_HW_OK;
            emit_status(entry->notification_id, DESK_NOTIFICATION_RENDERED, DESK_HW_OK, NULL);
        }
        if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE, 0) !=
            DESK_ARBITER_OK) return DESK_HW_ERR_BUSY;
        entry->audio_attempted = 1;
        static const int16_t tone_lut[16] = {
            0,3062,5657,7396,8000,7396,5657,3062,0,-3062,-5657,-7396,-8000,-7396,-5657,-3062
        };
        int16_t tone[NOTIFICATION_SOUND_SAMPLES];
        for (size_t sample = 0; sample < NOTIFICATION_SOUND_SAMPLES; ++sample)
            tone[sample] = tone_lut[sample % 16];
        int audio_rc = desk_hardware_audio_play_pcm(tone, NOTIFICATION_SOUND_SAMPLES,
                                                     NOTIFICATION_SOUND_RATE);
        desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
        entry->pending = 0;
        entry->status = audio_rc == DESK_HW_OK ? DESK_NOTIFICATION_UNKNOWN : DESK_NOTIFICATION_FAILED;
        entry->error_code = audio_rc == DESK_HW_OK ? DESK_HW_ERR_UNAVAILABLE : audio_rc;
        emit_status(entry->notification_id, (desk_notification_status_t)entry->status,
                    entry->error_code, NULL);
        return audio_rc == DESK_HW_OK ? DESK_HW_OK : audio_rc;
    }
    return DESK_HW_OK;
}

int desk_hardware_notification_summary_acknowledge(const char *summary_id, const char *source)
{
    if (!s_initialized) return DESK_HW_ERR_UNAVAILABLE;
    if (!valid_id(summary_id, DESK_NOTIFICATION_SUMMARY_ID_MAX + 1) ||
        !valid_text(source, DESK_NOTIFICATION_ACK_SOURCE_MAX + 1)) return DESK_HW_ERR_INVALID_ARG;
    int index = find_record(summary_id);
    if (index < 0 || !s_records[index].is_summary) return DESK_HW_ERR_UNAVAILABLE;
    notification_record_t *record = &s_records[index];
    if (record->summary_acknowledged) return DESK_HW_OK;
    record->summary_acknowledged = 1;
    dismiss_active_overlay_for_id(summary_id);
    if (s_summary_ack_cb) s_summary_ack_cb(summary_id, source, s_summary_ack_user_data);
    return DESK_HW_OK;
}

int desk_hardware_notification_acknowledge(const char *notification_id, const char *source)
{
    if (!s_initialized) return DESK_HW_ERR_UNAVAILABLE;
    if (!valid_text(notification_id, DESK_NOTIFICATION_ID_MAX + 1) ||
        !valid_text(source, DESK_NOTIFICATION_ACK_SOURCE_MAX + 1)) return DESK_HW_ERR_INVALID_ARG;
    int index = find_record(notification_id);
    if (index < 0 || s_records[index].is_summary) return DESK_HW_ERR_UNAVAILABLE;
    notification_record_t *record = &s_records[index];
    if (record->acknowledged) {
        dismiss_active_overlay_for_id(notification_id);
        return DESK_HW_OK;
    }
    record->acknowledged = 1;
    snprintf(record->ack_source, sizeof(record->ack_source), "%s", source);
    dismiss_active_overlay_for_id(notification_id);
    emit_status(notification_id, DESK_NOTIFICATION_ACKNOWLEDGED, DESK_HW_OK, source);
    return DESK_HW_OK;
}

int desk_hardware_notification_dismiss(const char *notification_id)
{
    if (!s_initialized) return DESK_HW_ERR_UNAVAILABLE;
    if (!valid_text(notification_id, DESK_NOTIFICATION_SUMMARY_ID_MAX + 1))
        return DESK_HW_ERR_INVALID_ARG;
    if (!s_active_overlay_handle || strcmp(s_active_overlay_notification_id, notification_id) != 0)
        return DESK_HW_ERR_UNAVAILABLE;
    int rc = desk_hardware_ui_overlay_dismiss(s_active_overlay_handle);
    if (rc == DESK_HW_OK || rc == DESK_HW_ERR_TIMEOUT || rc == DESK_HW_ERR_UNAVAILABLE) {
        s_active_overlay_handle = 0;
        s_active_overlay_notification_id[0] = '\0';
    }
    return rc;
}

int desk_hardware_notification_get_status(const char *notification_id,
                                          desk_notification_status_event_t *out_status)
{
    if (!s_initialized) return DESK_HW_ERR_UNAVAILABLE;
    if (!valid_text(notification_id, DESK_NOTIFICATION_ID_MAX + 1) || !out_status)
        return DESK_HW_ERR_INVALID_ARG;
    int index = find_record(notification_id);
    if (index < 0 || s_records[index].is_summary) return DESK_HW_ERR_UNAVAILABLE;
    fill_status(notification_id, (unsigned)index, out_status);
    return DESK_HW_OK;
}
