#include "desk_notification.h"
#include "desk_arbiter.h"
#include <stdio.h>
#include <string.h>
#include <time.h>
#include <sys/time.h>

#define NOTIFICATION_STORE_MAGIC 0x444E4F54u
#define NOTIFICATION_STORE_VERSION 1u
#define NOTIFICATION_RECORDS_MAX 16
#define NOTIFICATION_SOUND_SAMPLES 1600
#define NOTIFICATION_SOUND_RATE 16000u

typedef struct {
    uint32_t magic;
    uint16_t version;
    uint8_t occupied;
    char notification_id[DESK_NOTIFICATION_ID_MAX + 1];
    char reminder_id[DESK_NOTIFICATION_REMINDER_ID_MAX + 1];
    char occurrence_id[DESK_NOTIFICATION_OCCURRENCE_ID_MAX + 1];
    char title[DESK_NOTIFICATION_TITLE_MAX + 1];
    uint64_t scheduled_epoch;
    uint64_t expires_epoch;
    int32_t error_code;
    uint8_t status;
    uint8_t pending;
    uint8_t rendered;
    uint8_t audio_attempted;
    uint8_t acknowledged;
    uint8_t is_catchup;
    char ack_source[DESK_NOTIFICATION_ACK_SOURCE_MAX + 1];
} notification_record_t;

static notification_record_t s_records[NOTIFICATION_RECORDS_MAX];
static bool s_initialized;
static desk_notification_store_ops_t s_store_ops;
static bool s_store_registered;
static desk_notification_status_cb_t s_status_cb;
static void *s_status_user_data;
static bool valid_title_utf8(const char *text);

static void emit_status(const char *id, desk_notification_status_t status, int error, const char *source)
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

/* Convert UTC RFC3339 timestamps, including Python's optional microseconds. */
static bool parse_utc(const char *text, uint64_t *out_epoch_us)
{
    int year, month, day, hour, minute, second, consumed = 0;
    if (!text || !out_epoch_us || strlen(text) < 20 ||
        sscanf(text, "%4d-%2d-%2dT%2d:%2d:%2d%n", &year, &month, &day,
               &hour, &minute, &second, &consumed) != 6 || consumed != 19 ||
        text[4] != '-' || text[7] != '-' || text[10] != 'T' || text[13] != ':' || text[16] != ':') return false;
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

static int store_load(void)
{
    if (!s_store_registered || !s_store_ops.load) return DESK_HW_ERR_UNAVAILABLE;
    memset(s_records, 0, sizeof(s_records));
    for (unsigned i = 0; i < NOTIFICATION_RECORDS_MAX; ++i) {
        size_t size = 0;
        int rc = s_store_ops.load(i, &s_records[i], sizeof(notification_record_t), &size,
                                  s_store_ops.user_data);
        if (rc == DESK_NOTIFICATION_STORE_SLOT_EMPTY) continue;
        if (rc != DESK_HW_OK || size != sizeof(notification_record_t) ||
            s_records[i].magic != NOTIFICATION_STORE_MAGIC ||
            s_records[i].version != NOTIFICATION_STORE_VERSION ||
            !s_records[i].occupied ||
            !memchr(s_records[i].notification_id, '\0', sizeof(s_records[i].notification_id)) ||
            !memchr(s_records[i].title, '\0', sizeof(s_records[i].title)) ||
            !valid_title_utf8(s_records[i].title)) return DESK_HW_ERR_IO;
    }
    return DESK_HW_OK;
}

static int store_save(unsigned index)
{
    if (!s_store_registered || !s_store_ops.save) return DESK_HW_ERR_UNAVAILABLE;
    int rc = s_store_ops.save(index, &s_records[index], sizeof(notification_record_t),
                              s_store_ops.user_data);
    if (rc == DESK_HW_OK) {
        notification_record_t verified = {0};
        size_t verified_len = 0;
        rc = s_store_ops.load(index, &verified, sizeof(verified), &verified_len,
                              s_store_ops.user_data);
        if (rc == DESK_HW_OK &&
            (verified_len != sizeof(verified) ||
             memcmp(&verified, &s_records[index], sizeof(verified)) != 0)) {
            rc = DESK_HW_ERR_IO;
        } else if (rc == DESK_NOTIFICATION_STORE_SLOT_EMPTY) {
            rc = DESK_HW_ERR_IO;
        }
    }
    if (rc != DESK_HW_OK) {
        /* An uncertain commit must stop this instance from creating more effects. */
        s_initialized = false;
        return DESK_HW_ERR_IO;
    }
    return DESK_HW_OK;
}

static int store_erase(unsigned index)
{
    if (!s_store_registered || !s_store_ops.erase) return DESK_HW_ERR_UNAVAILABLE;
    return s_store_ops.erase(index, s_store_ops.user_data);
}

static int find_record(const char *id)
{
    for (unsigned i = 0; i < NOTIFICATION_RECORDS_MAX; ++i) {
        if (s_records[i].occupied && strcmp(s_records[i].notification_id, id) == 0) return (int)i;
    }
    return -1;
}

static void fill_status(const char *id, unsigned index, desk_notification_status_event_t *out)
{
    memset(out, 0, sizeof(*out));
    snprintf(out->notification_id, sizeof(out->notification_id), "%s", id);
    out->status = (desk_notification_status_t)s_records[index].status;
    out->error_code = s_records[index].error_code;
    if (s_records[index].acknowledged) {
        out->acknowledged = true;
        snprintf(out->ack_source, sizeof(out->ack_source), "%s", s_records[index].ack_source);
    }
}

static bool valid_text(const char *value, size_t capacity)
{
    return value && value[0] && memchr(value, '\0', capacity) != NULL;
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
        unsigned char second_min = 0x80;
        unsigned char second_max = 0xbf;
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

int desk_hardware_notification_init(desk_notification_status_cb_t callback, void *user_data)
{
    s_initialized = false;
    int rc = store_load();
    if (rc != DESK_HW_OK) return rc;
    s_status_cb = callback;
    s_status_user_data = user_data;
    /* Reboot after an audio attempt leaves completion unknowable. Do not replay it. */
    for (unsigned i = 0; i < NOTIFICATION_RECORDS_MAX; ++i) {
        notification_record_t *record = &s_records[i];
        if (!record->occupied || !record->pending || !record->audio_attempted) continue;
        record->pending = 0;
        record->status = DESK_NOTIFICATION_UNKNOWN;
        record->error_code = DESK_HW_ERR_UNAVAILABLE;
        if (store_save(i) != DESK_HW_OK) return DESK_HW_ERR_IO;
        emit_status(record->notification_id, DESK_NOTIFICATION_UNKNOWN, DESK_HW_ERR_UNAVAILABLE, NULL);
    }
    s_initialized = true;
    return DESK_HW_OK;
}

int desk_hardware_notification_register_store(const desk_notification_store_ops_t *ops)
{
    if (s_initialized || !ops || !ops->load || !ops->save || !ops->erase)
        return DESK_HW_ERR_INVALID_ARG;
    s_store_ops = *ops;
    s_store_registered = true;
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
        if (s_records[index].pending && s_records[index].expires_epoch <= now) {
            s_records[index].pending = 0;
            s_records[index].status = DESK_NOTIFICATION_EXPIRED;
            s_records[index].error_code = DESK_HW_ERR_TIMEOUT;
            if (store_save((unsigned)index) != DESK_HW_OK) return DESK_HW_ERR_IO;
            emit_status(input->notification_id, DESK_NOTIFICATION_EXPIRED, DESK_HW_ERR_TIMEOUT, NULL);
        }
        fill_status(input->notification_id, (unsigned)index, out_status);
        return out_status->status == DESK_NOTIFICATION_EXPIRED ? DESK_HW_ERR_TIMEOUT : DESK_HW_OK;
    }
    if (expires <= now) {
        memset(out_status, 0, sizeof(*out_status));
        snprintf(out_status->notification_id, sizeof(out_status->notification_id), "%s", input->notification_id);
        out_status->status = DESK_NOTIFICATION_EXPIRED;
        out_status->error_code = DESK_HW_ERR_TIMEOUT;
        emit_status(input->notification_id, DESK_NOTIFICATION_EXPIRED, DESK_HW_ERR_TIMEOUT, NULL);
        return DESK_HW_ERR_TIMEOUT;
    }

    index = -1;
    for (unsigned i = 0; i < NOTIFICATION_RECORDS_MAX; ++i) {
        if (!s_records[i].occupied || s_records[i].expires_epoch <= now) {
            index = (int)i;
            break;
        }
    }
    if (index < 0) return DESK_HW_ERR_BUSY;
    if (s_records[index].occupied && store_erase((unsigned)index) != DESK_HW_OK) return DESK_HW_ERR_IO;
    memset(&s_records[index], 0, sizeof(s_records[index]));
    s_records[index].magic = NOTIFICATION_STORE_MAGIC;
    s_records[index].version = NOTIFICATION_STORE_VERSION;
    s_records[index].occupied = 1;
    snprintf(s_records[index].notification_id, sizeof(s_records[index].notification_id), "%s", input->notification_id);
    snprintf(s_records[index].reminder_id, sizeof(s_records[index].reminder_id), "%s", input->reminder_id);
    snprintf(s_records[index].occurrence_id, sizeof(s_records[index].occurrence_id), "%s", input->occurrence_id);
    snprintf(s_records[index].title, sizeof(s_records[index].title), "%s", input->title);
    s_records[index].scheduled_epoch = scheduled;
    s_records[index].expires_epoch = expires;
    s_records[index].status = DESK_NOTIFICATION_ACCEPTED;
    s_records[index].pending = 1;
    s_records[index].is_catchup = input->is_catchup ? 1 : 0;
    if (store_save((unsigned)index) != DESK_HW_OK) {
        memset(&s_records[index], 0, sizeof(s_records[index]));
        return DESK_HW_ERR_IO;
    }
    fill_status(input->notification_id, (unsigned)index, out_status);
    emit_status(input->notification_id, DESK_NOTIFICATION_ACCEPTED, DESK_HW_OK, NULL);
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
            entry->status = DESK_NOTIFICATION_EXPIRED;
            entry->error_code = DESK_HW_ERR_TIMEOUT;
            if (store_save(i) != DESK_HW_OK) return DESK_HW_ERR_IO;
            emit_status(entry->notification_id, DESK_NOTIFICATION_EXPIRED, DESK_HW_ERR_TIMEOUT, NULL);
            return DESK_HW_ERR_TIMEOUT;
        }
        if (!entry->rendered) {
            if (desk_arbiter_acquire(DESK_RES_DISPLAY_UI, DESK_OWNER_SDK_REMOTE, 0) != DESK_ARBITER_OK)
                return DESK_HW_ERR_BUSY;
            char text[64];
            size_t offset = 0;
            if (entry->is_catchup) {
                memcpy(text, "Late: ", 6);
                offset = 6;
            }
            size_t available = sizeof(text) - offset - 1;
            size_t title_len = utf8_prefix(entry->title, available);
            memcpy(text + offset, entry->title, title_len);
            text[offset + title_len] = '\0';
            int display_rc = desk_hardware_display_set_expression("alert", text);
            desk_arbiter_release(DESK_RES_DISPLAY_UI, DESK_OWNER_SDK_REMOTE);
            if (display_rc != DESK_HW_OK) {
                entry->pending = 0;
                entry->status = DESK_NOTIFICATION_FAILED;
                entry->error_code = display_rc;
                if (store_save(i) != DESK_HW_OK) return DESK_HW_ERR_IO;
                emit_status(entry->notification_id, DESK_NOTIFICATION_FAILED, display_rc, NULL);
                return display_rc;
            }
            entry->rendered = 1;
            entry->status = DESK_NOTIFICATION_RENDERED;
            entry->error_code = DESK_HW_OK;
            if (store_save(i) != DESK_HW_OK) return DESK_HW_ERR_IO;
            emit_status(entry->notification_id, DESK_NOTIFICATION_RENDERED, DESK_HW_OK, NULL);
        }
        /* Do not interrupt Tuya/TClaw speech. The caller retries while busy. */
        if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE, 0) != DESK_ARBITER_OK)
            return DESK_HW_ERR_BUSY;
        entry->audio_attempted = 1;
        if (store_save(i) != DESK_HW_OK) {
            entry->audio_attempted = 0;
            desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
            return DESK_HW_ERR_IO;
        }
        static const int16_t tone_lut[16] = {0,3062,5657,7396,8000,7396,5657,3062,0,-3062,-5657,-7396,-8000,-7396,-5657,-3062};
        int16_t tone[NOTIFICATION_SOUND_SAMPLES];
        for (size_t sample = 0; sample < NOTIFICATION_SOUND_SAMPLES; ++sample) tone[sample] = tone_lut[sample % 16];
        int audio_rc = desk_hardware_audio_play_pcm(tone, NOTIFICATION_SOUND_SAMPLES, NOTIFICATION_SOUND_RATE);
        desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
        entry->pending = 0;
        entry->status = audio_rc == DESK_HW_OK ? DESK_NOTIFICATION_UNKNOWN : DESK_NOTIFICATION_FAILED;
        entry->error_code = audio_rc == DESK_HW_OK ? DESK_HW_ERR_UNAVAILABLE : audio_rc;
        if (store_save(i) != DESK_HW_OK) return DESK_HW_ERR_IO;
        emit_status(entry->notification_id, (desk_notification_status_t)entry->status,
                    audio_rc == DESK_HW_OK ? DESK_HW_ERR_UNAVAILABLE : audio_rc, NULL);
        return audio_rc == DESK_HW_OK ? DESK_HW_OK : audio_rc;
    }
    return DESK_HW_OK;
}

int desk_hardware_notification_acknowledge(const char *notification_id, const char *source)
{
    if (!s_initialized) return DESK_HW_ERR_UNAVAILABLE;
    if (!valid_text(notification_id, DESK_NOTIFICATION_ID_MAX + 1) ||
        !valid_text(source, DESK_NOTIFICATION_ACK_SOURCE_MAX + 1)) return DESK_HW_ERR_INVALID_ARG;
    int index = find_record(notification_id);
    if (index < 0) return DESK_HW_ERR_UNAVAILABLE;
    if (s_records[index].acknowledged) return DESK_HW_OK;
    s_records[index].acknowledged = 1;
    snprintf(s_records[index].ack_source, sizeof(s_records[index].ack_source), "%s", source);
    if (store_save((unsigned)index) != DESK_HW_OK) {
        s_records[index].acknowledged = 0;
        s_records[index].ack_source[0] = '\0';
        return DESK_HW_ERR_IO;
    }
    emit_status(notification_id, DESK_NOTIFICATION_ACKNOWLEDGED, DESK_HW_OK, source);
    return DESK_HW_OK;
}

int desk_hardware_notification_get_status(const char *notification_id,
                                          desk_notification_status_event_t *out_status)
{
    if (!s_initialized) return DESK_HW_ERR_UNAVAILABLE;
    if (!valid_text(notification_id, DESK_NOTIFICATION_ID_MAX + 1) || !out_status)
        return DESK_HW_ERR_INVALID_ARG;
    int index = find_record(notification_id);
    if (index < 0) return DESK_HW_ERR_UNAVAILABLE;
    fill_status(notification_id, (unsigned)index, out_status);
    return DESK_HW_OK;
}
