/* Watcher ES8311 + ES7243/E audio, following Seeed BSP 8e37f7c.
 * The TDL microphone worker is the sole reader while native AI is open.
 */
#if defined(ESP_PLATFORM) && defined(DESK_BOARD_SENSECAP_WATCHER)
#include "desk_watcher_audio.h"
#include "desk_watcher_board.h"
#include "desk_arbiter.h"
#include "driver/i2s_std.h"
#include "esp_codec_dev.h"
#include "esp_codec_dev_defaults.h"
#include "es8311_codec.h"
#include "es7243_adc.h"
#include "es7243e_adc.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/semphr.h"
#include "freertos/task.h"
#include "freertos/idf_additions.h"
#include "esp_heap_caps.h"
#include "tdl_audio_driver.h"
#include "audio_afe.h"
#include <limits.h>

#define RATE 16000
#define FRAME_SAMPLES 320
#define TX_DMA_DESC_NUM 6u
#define TX_DMA_FRAME_NUM 240u
#define TX_DMA_ACTIVE_SLOT_NUM 1u
#define TX_DMA_BYTES_PER_SAMPLE 2u
#define TX_DMA_DESC_BYTES (TX_DMA_FRAME_NUM * TX_DMA_ACTIVE_SLOT_NUM * TX_DMA_BYTES_PER_SAMPLE)
#define TX_DMA_DRAIN_BYTES (TX_DMA_DESC_NUM * TX_DMA_DESC_BYTES)
#define TX_DMA_DRAIN_TIMEOUT_MS 1000u
static esp_codec_dev_handle_t s_mic, s_speaker;
static i2s_chan_handle_t s_tx, s_rx;
static SemaphoreHandle_t s_read_lock, s_write_lock, s_stopped, s_lifecycle;
static SemaphoreHandle_t s_tclaw_play_lock;
static SemaphoreHandle_t s_one_shot_lock;
static SemaphoreHandle_t s_stream_lock, s_stream_done;
static desk_audio_stream_ctx_t *s_stream_ctx;
static TaskHandle_t s_stream_task;
static bool s_stream_worker_running;
static bool s_tclaw_playback_session_active;
static uint32_t s_stream_drain_timeout_ms = TX_DMA_DRAIN_TIMEOUT_MS;
static portMUX_TYPE s_tx_eof_mux = portMUX_INITIALIZER_UNLOCKED;
static volatile uint32_t s_tx_eof_sequence;
static volatile bool s_tx_eof_invalid;
static volatile bool s_tx_eof_armed;
static bool s_ready, s_attempted;
static volatile bool s_running;
static volatile bool s_worker_exists;
static volatile uint32_t s_frames, s_errors, s_peak;
static TDL_AUDIO_MIC_CB s_mic_cb;

void desk_watcher_audio_status(bool *ready, bool *running, uint32_t *frames, uint32_t *errors, uint32_t *peak)
{
    if (ready) *ready=s_ready;
    if (running) *running=s_running;
    if (frames) *frames=s_frames;
    if (errors) *errors=s_errors;
    if (peak) *peak=s_peak;
}

int desk_watcher_audio_read(int16_t *pcm, size_t samples, size_t *count)
{
    if (count) *count = 0;
    if (!pcm || !count || !samples || samples > INT_MAX / sizeof(*pcm)) return DESK_HW_ERR_INVALID_ARG;
    if (!s_ready) return DESK_HW_ERR_UNAVAILABLE;
    if (xSemaphoreTake(s_read_lock, pdMS_TO_TICKS(100)) != pdTRUE) return DESK_HW_ERR_TIMEOUT;
    /* Never steal samples from TClaw's active input stream. */
    if (s_running || s_worker_exists) { xSemaphoreGive(s_read_lock); return DESK_HW_ERR_UNAVAILABLE; }
    int rc = esp_codec_dev_read(s_mic, pcm, samples * sizeof(*pcm));
    xSemaphoreGive(s_read_lock);
    if (rc != 0) return DESK_HW_ERR_IO;
    *count = samples;
    return DESK_HW_OK;
}

int desk_watcher_audio_play(const int16_t *pcm, size_t samples, uint32_t rate)
{
    if (!pcm || !samples || samples > INT_MAX / sizeof(*pcm) || rate != RATE) return DESK_HW_ERR_INVALID_ARG;
    if (!s_ready) return DESK_HW_ERR_UNAVAILABLE;
    desk_resource_owner_t owner = desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK);
    if (owner == DESK_OWNER_NONE) return DESK_HW_ERR_BUSY;
    if (owner == DESK_OWNER_SDK_REMOTE && s_stream_lock) {
        if (xSemaphoreTake(s_stream_lock, pdMS_TO_TICKS(100)) != pdTRUE) return DESK_HW_ERR_TIMEOUT;
        bool stream_active = s_stream_ctx && s_stream_ctx->owner_held;
        TaskHandle_t stream_task = s_stream_task;
        xSemaphoreGive(s_stream_lock);
        if (stream_active && xTaskGetCurrentTaskHandle() != stream_task) return DESK_HW_ERR_BUSY;
    }
    if (xSemaphoreTake(s_write_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return DESK_HW_ERR_TIMEOUT;
    /* Ownership may have changed while waiting for the serialized TX writer. */
    if (desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK) != owner) {
        xSemaphoreGive(s_write_lock);
        return DESK_HW_ERR_BUSY;
    }
    int rc = esp_codec_dev_write(s_speaker, (void *)pcm, samples * sizeof(*pcm));
    xSemaphoreGive(s_write_lock);
    return rc == 0 ? DESK_HW_OK : DESK_HW_ERR_IO;
}

int desk_watcher_audio_play_one_shot(const int16_t *pcm, size_t samples, uint32_t rate)
{
    if (!s_one_shot_lock ||
        xSemaphoreTake(s_one_shot_lock, pdMS_TO_TICKS(TX_DMA_DRAIN_TIMEOUT_MS)) != pdTRUE)
        return DESK_HW_ERR_TIMEOUT;
    int rc = desk_watcher_audio_play(pcm, samples, rate);
    if (rc == DESK_HW_OK) rc = desk_watcher_audio_tx_drain(TX_DMA_DRAIN_TIMEOUT_MS);
    xSemaphoreGive(s_one_shot_lock);
    return rc;
}

static bool IRAM_ATTR tx_on_sent(i2s_chan_handle_t channel,
                                 i2s_event_data_t *event,
                                 void *user_data)
{
    (void)channel;
    (void)user_data;
    portENTER_CRITICAL_ISR(&s_tx_eof_mux);
    if (s_tx_eof_armed) {
        if (!event || event->size != TX_DMA_DESC_BYTES || !event->dma_buf) {
            s_tx_eof_invalid = true;
        } else {
            ++s_tx_eof_sequence;
        }
    }
    portEXIT_CRITICAL_ISR(&s_tx_eof_mux);
    return false;
}

static uint32_t tx_eof_sequence_get(bool *invalid)
{
    uint32_t sequence;
    portENTER_CRITICAL(&s_tx_eof_mux);
    sequence = s_tx_eof_sequence;
    if (invalid) *invalid = s_tx_eof_invalid;
    portEXIT_CRITICAL(&s_tx_eof_mux);
    return sequence;
}

static void tx_eof_arm_for_final_configuration(void)
{
    portENTER_CRITICAL(&s_tx_eof_mux);
    s_tx_eof_sequence = 0;
    s_tx_eof_invalid = false;
    s_tx_eof_armed = true;
    portEXIT_CRITICAL(&s_tx_eof_mux);
}

static bool audio_preempt_quiesce(desk_resource_type_t resource,
                                  desk_resource_owner_t current_owner,
                                  desk_resource_owner_t requester,
                                  uint32_t timeout_ms, void *user_data)
{
    (void)user_data;
    if (resource != DESK_RES_AUDIO_PLAYBACK) return false;
    if (!timeout_ms) timeout_ms = TX_DMA_DRAIN_TIMEOUT_MS;
    if (current_owner == DESK_OWNER_SDK_REMOTE) {
        uint64_t handle = 0;
        if (!s_stream_lock ||
            xSemaphoreTake(s_stream_lock, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) return false;
        bool stream_active = s_stream_ctx && s_stream_ctx->owner_held;
        if (stream_active) handle = s_stream_ctx->handle;
        xSemaphoreGive(s_stream_lock);
        if (stream_active) {
            return desk_watcher_audio_stream_stop(handle, timeout_ms) == DESK_HW_OK;
        }
        /* One-shot writers hold this lock across their payload and drain. */
        if (!s_one_shot_lock ||
            xSemaphoreTake(s_one_shot_lock, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) return false;
        int drain_rc = desk_watcher_audio_tx_drain(timeout_ms);
        xSemaphoreGive(s_one_shot_lock);
        return drain_rc == DESK_HW_OK;
    }
    if (current_owner == DESK_OWNER_TCLAW_VOICE &&
        requester == DESK_OWNER_SYSTEM_ALERT) {
        if (!s_tclaw_play_lock ||
            xSemaphoreTake(s_tclaw_play_lock, pdMS_TO_TICKS(timeout_ms)) != pdTRUE) return false;
        desk_resource_owner_t actual_owner = desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK);
        int drain_rc = DESK_HW_OK;
        if (actual_owner == DESK_OWNER_TCLAW_VOICE) {
            /* No per-chunk drain: the TDL stream stays contiguous until STOP
             * or an alert preempts it. Keep ownership if the EOF fence fails. */
            drain_rc = desk_watcher_audio_tx_drain(timeout_ms);
        } else if (actual_owner != DESK_OWNER_NONE && actual_owner != requester) {
            drain_rc = DESK_HW_ERR_BUSY;
        }
        if (drain_rc == DESK_HW_OK) s_tclaw_playback_session_active = false;
        xSemaphoreGive(s_tclaw_play_lock);
        return drain_rc == DESK_HW_OK;
    }
    return false;
}

int desk_watcher_audio_play_stop(void)
{
    desk_resource_owner_t owner = desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK);
    if (owner == DESK_OWNER_NONE) return DESK_HW_OK;
    if (owner == DESK_OWNER_SYSTEM_ALERT) return DESK_HW_ERR_BUSY;
    if (owner == DESK_OWNER_TCLAW_VOICE) {
        if (!s_tclaw_play_lock ||
            xSemaphoreTake(s_tclaw_play_lock, pdMS_TO_TICKS(TX_DMA_DRAIN_TIMEOUT_MS)) != pdTRUE)
            return DESK_HW_ERR_TIMEOUT;
        int rc = desk_watcher_audio_tx_drain(TX_DMA_DRAIN_TIMEOUT_MS);
        if (rc == DESK_HW_OK) {
            s_tclaw_playback_session_active = false;
            desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
        }
        xSemaphoreGive(s_tclaw_play_lock);
        return rc;
    }
    if (!audio_preempt_quiesce(DESK_RES_AUDIO_PLAYBACK, owner,
                               DESK_OWNER_SYSTEM_ALERT, TX_DMA_DRAIN_TIMEOUT_MS, NULL)) {
        return DESK_HW_ERR_TIMEOUT;
    }
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, owner);
    return DESK_HW_OK;
}

static void audio_stream_worker(void *arg)
{
    const uint64_t handle = (uint64_t)(uintptr_t)arg;
    int16_t *output = heap_caps_malloc(DESK_AUDIO_STREAM_CHUNK_BYTES,
                                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!output) {
        xSemaphoreTake(s_stream_lock, portMAX_DELAY);
        (void)desk_audio_stream_cancel(s_stream_ctx, handle, 6u);
        xSemaphoreGive(s_stream_lock);
    }

    for (;;) {
        size_t samples = 0;
        bool needs_drain = false;
        int next_rc = 0;
        xSemaphoreTake(s_stream_lock, portMAX_DELAY);
        if (!output) {
            needs_drain = true;
        } else {
            bool may_start = s_stream_ctx->state == DESK_AUDIO_STREAM_CANCELING ||
                s_stream_ctx->state == DESK_AUDIO_STREAM_DRAINING ||
                s_stream_ctx->queue_depth >= DESK_AUDIO_STREAM_START_BUFFER_FRAMES ||
                s_stream_ctx->finish_requested;
            if (may_start) {
                next_rc = desk_audio_stream_next_output(s_stream_ctx,
                    output, DESK_AUDIO_STREAM_CHUNK_BYTES / sizeof(*output),
                    &samples, &needs_drain);
            }
        }
        uint32_t drain_timeout = s_stream_drain_timeout_ms;
        xSemaphoreGive(s_stream_lock);

        if (samples) {
            int write_rc = desk_watcher_audio_play(output, samples, RATE);
            xSemaphoreTake(s_stream_lock, portMAX_DELAY);
            (void)desk_audio_stream_write_complete(s_stream_ctx, handle, write_rc);
            xSemaphoreGive(s_stream_lock);
            continue;
        }
        if (needs_drain || next_rc < 0) {
            int drain_rc = desk_watcher_audio_tx_drain(drain_timeout);
            bool release_owner = false;
            xSemaphoreTake(s_stream_lock, portMAX_DELAY);
            if (drain_rc == DESK_HW_OK &&
                desk_audio_stream_confirm_static(s_stream_ctx, handle, true) == 0) {
                release_owner = true;
            }
            s_stream_worker_running = false;
            s_stream_task = NULL;
            xSemaphoreGive(s_stream_lock);
            if (release_owner) desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
            if (output) heap_caps_free(output);
            xSemaphoreGive(s_stream_done);
            vTaskDeleteWithCaps(NULL);
            return;
        }
        (void)ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(100));
    }
}

static int audio_stream_start_worker(uint64_t handle)
{
    xSemaphoreTake(s_stream_done, 0);
    s_stream_worker_running = true;
    if (xTaskCreateWithCaps(audio_stream_worker, "watcher_audio_tx", 6144,
                            (void *)(uintptr_t)handle, 6, &s_stream_task,
                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        s_stream_worker_running = false;
        s_stream_task = NULL;
        return DESK_HW_ERR_UNAVAILABLE;
    }
    return DESK_HW_OK;
}

int desk_watcher_audio_stream_begin(uint64_t handle, uint32_t connection_generation,
                                    uint32_t stream_id, uint32_t sample_rate_hz,
                                    uint8_t channels, uint8_t sample_width_bytes,
                                    uint32_t total_bytes)
{
    if (!s_ready || !s_stream_ctx || !s_stream_lock || s_running || s_worker_exists)
        return DESK_HW_ERR_BUSY;
    xSemaphoreTake(s_stream_lock, portMAX_DELAY);
    bool capture_active = s_running || s_worker_exists;
    int rc = desk_audio_stream_begin(s_stream_ctx, handle, connection_generation,
        stream_id, sample_rate_hz, channels, sample_width_bytes, total_bytes,
        capture_active);
    if (rc != 0) {
        xSemaphoreGive(s_stream_lock);
        return rc == -2 ? DESK_HW_ERR_BUSY : DESK_HW_ERR_INVALID_ARG;
    }
    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE, 0) != DESK_ARBITER_OK) {
        s_stream_ctx->owner_held = false;
        s_stream_ctx->state = DESK_AUDIO_STREAM_IDLE;
        xSemaphoreGive(s_stream_lock);
        return DESK_HW_ERR_BUSY;
    }
    s_stream_drain_timeout_ms = TX_DMA_DRAIN_TIMEOUT_MS;
    rc = audio_stream_start_worker(handle);
    if (rc != DESK_HW_OK) {
        s_stream_ctx->owner_held = false;
        s_stream_ctx->state = DESK_AUDIO_STREAM_IDLE;
        desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
    }
    xSemaphoreGive(s_stream_lock);
    return rc;
}

int desk_watcher_audio_stream_write(uint64_t handle, uint32_t connection_generation,
                                    uint32_t stream_id, uint32_t sequence,
                                    const uint8_t *pcm, size_t bytes,
                                    desk_audio_stream_status_t *out_status)
{
    if (!s_stream_ctx || !s_stream_lock) return DESK_HW_ERR_UNAVAILABLE;
    xSemaphoreTake(s_stream_lock, portMAX_DELAY);
    int rc = desk_audio_stream_write(s_stream_ctx, handle, connection_generation,
                                     stream_id, sequence, pcm, bytes);
    if (out_status) (void)desk_audio_stream_get_status(s_stream_ctx, handle, out_status);
    TaskHandle_t task = s_stream_task;
    xSemaphoreGive(s_stream_lock);
    if (task) xTaskNotifyGive(task);
    return rc == 0 ? DESK_HW_OK : rc == -3 ? DESK_HW_ERR_BUSY : DESK_HW_ERR_INVALID_ARG;
}

int desk_watcher_audio_stream_finish(uint64_t handle)
{
    if (!s_stream_ctx || !s_stream_lock) return DESK_HW_ERR_UNAVAILABLE;
    xSemaphoreTake(s_stream_lock, portMAX_DELAY);
    int rc = desk_audio_stream_finish(s_stream_ctx, handle);
    TaskHandle_t task = s_stream_task;
    xSemaphoreGive(s_stream_lock);
    if (task) xTaskNotifyGive(task);
    return rc == 0 ? DESK_HW_OK : DESK_HW_ERR_INVALID_ARG;
}

int desk_watcher_audio_stream_stop(uint64_t handle, uint32_t timeout_ms)
{
    if (!s_stream_ctx || !s_stream_lock) return DESK_HW_ERR_UNAVAILABLE;
    if (!timeout_ms) timeout_ms = TX_DMA_DRAIN_TIMEOUT_MS;
    xSemaphoreTake(s_stream_lock, portMAX_DELAY);
    if (s_stream_ctx->handle != handle || !s_stream_ctx->owner_held) {
        xSemaphoreGive(s_stream_lock);
        return DESK_HW_ERR_INVALID_ARG;
    }
    int rc = desk_audio_stream_cancel(s_stream_ctx, handle, 0);
    s_stream_drain_timeout_ms = timeout_ms;
    TaskHandle_t task = s_stream_task;
    bool worker_running = s_stream_worker_running;
    xSemaphoreGive(s_stream_lock);
    if (rc != 0) return DESK_HW_ERR_INVALID_ARG;
    if (worker_running && task) {
        xTaskNotifyGive(task);
        if (xSemaphoreTake(s_stream_done, pdMS_TO_TICKS(timeout_ms + 100u)) != pdTRUE)
            return DESK_HW_ERR_TIMEOUT;
        xSemaphoreTake(s_stream_lock, portMAX_DELAY);
        bool owner_held = s_stream_ctx->owner_held;
        xSemaphoreGive(s_stream_lock);
        return owner_held ? DESK_HW_ERR_TIMEOUT : DESK_HW_OK;
    }

    int drain_rc = desk_watcher_audio_tx_drain(timeout_ms);
    if (drain_rc != DESK_HW_OK) return drain_rc;
    xSemaphoreTake(s_stream_lock, portMAX_DELAY);
    rc = desk_audio_stream_confirm_static(s_stream_ctx, handle, true);
    xSemaphoreGive(s_stream_lock);
    if (rc == 0) desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
    return rc == 0 ? DESK_HW_OK : DESK_HW_ERR_IO;
}

int desk_watcher_audio_stream_get_status(uint64_t handle,
                                         desk_audio_stream_status_t *out_status)
{
    if (!s_stream_ctx || !s_stream_lock || !out_status) return DESK_HW_ERR_INVALID_ARG;
    xSemaphoreTake(s_stream_lock, portMAX_DELAY);
    int rc = desk_audio_stream_get_status(s_stream_ctx, handle, out_status);
    xSemaphoreGive(s_stream_lock);
    return rc == 0 ? DESK_HW_OK : DESK_HW_ERR_INVALID_ARG;
}

/*
 * I2S IDF 5.4 write() copies bytes into the TX descriptor ring and returns
 * after acceptance, not DMA completion. Append a whole ring of silence while
 * holding the sole writer mutex, then wait for one complete ring of later TX
 * EOF callbacks. This proves TX DMA ordering only; it does not prove that the
 * codec or acoustic output is physically silent.
 */
int desk_watcher_audio_tx_drain(uint32_t timeout_ms)
{
    if (!s_ready || !s_tx || !s_speaker) return DESK_HW_ERR_UNAVAILABLE;
    if (!timeout_ms) timeout_ms = TX_DMA_DRAIN_TIMEOUT_MS;
    if (xSemaphoreTake(s_write_lock, pdMS_TO_TICKS(timeout_ms)) != pdTRUE)
        return DESK_HW_ERR_TIMEOUT;

    i2s_chan_info_t info = {0};
    if (i2s_channel_get_info(s_tx, &info) != ESP_OK ||
        info.mode != I2S_COMM_MODE_STD ||
        info.total_dma_buf_size != TX_DMA_DESC_NUM * TX_DMA_DESC_BYTES) {
        xSemaphoreGive(s_write_lock);
        return DESK_HW_ERR_UNAVAILABLE;
    }

    static const uint8_t silence[TX_DMA_DRAIN_BYTES] = {0};
    int rc = esp_codec_dev_write(s_speaker, (void *)silence, sizeof(silence));
    if (rc != 0) {
        xSemaphoreGive(s_write_lock);
        return DESK_HW_ERR_IO;
    }

    bool callback_invalid = false;
    uint32_t start = tx_eof_sequence_get(&callback_invalid);
    if (callback_invalid) {
        xSemaphoreGive(s_write_lock);
        return DESK_HW_ERR_IO;
    }
    TickType_t start_tick = xTaskGetTickCount();
    TickType_t timeout_ticks = pdMS_TO_TICKS(timeout_ms);
    if (timeout_ticks == 0) timeout_ticks = 1;
    int result = DESK_HW_ERR_TIMEOUT;
    for (;;) {
        uint32_t current = tx_eof_sequence_get(&callback_invalid);
        if (callback_invalid) {
            result = DESK_HW_ERR_IO;
            break;
        }
        if ((uint32_t)(current - start) >= TX_DMA_DESC_NUM) {
            result = DESK_HW_OK;
            break;
        }
        if ((TickType_t)(xTaskGetTickCount() - start_tick) >= timeout_ticks) break;
        vTaskDelay(1);
    }
    xSemaphoreGive(s_write_lock);
    return result;
}

static void mic_task(void *arg)
{
    (void)arg;
    int16_t frame[FRAME_SAMPLES];
    while (s_running) {
        if (xSemaphoreTake(s_read_lock, pdMS_TO_TICKS(100)) != pdTRUE) continue;
        int rc = esp_codec_dev_read(s_mic, frame, sizeof(frame));
        xSemaphoreGive(s_read_lock);
        if (rc == 0 && s_running && s_mic_cb) {
            uint32_t peak=0;
            for (unsigned i=0; i<FRAME_SAMPLES; ++i) {
                int32_t sample=frame[i];
                uint32_t amplitude=sample < 0 ? -sample : sample;
                if (amplitude > peak) peak=amplitude;
            }
            s_peak=peak; s_frames++;
            s_mic_cb(TDL_AUDIO_FRAME_FORMAT_PCM, TDL_AUDIO_STATUS_RECEIVING, (uint8_t *)frame, sizeof(frame));
            auio_afe_processor_feed((uint8_t *)frame, sizeof(frame));
        } else if (rc != 0) {
            s_errors++;
            ESP_LOGW("watcher_audio", "ADC read failed rc=%d", rc);
            vTaskDelay(pdMS_TO_TICKS(100));
        }
    }
    s_mic_cb=NULL;
    desk_arbiter_release(DESK_RES_AUDIO_RECORD, DESK_OWNER_TCLAW_VOICE);
    s_worker_exists=false;
    xSemaphoreGive(s_stopped);
    vTaskDeleteWithCaps(NULL);
}

static OPERATE_RET native_open(TDD_AUDIO_HANDLE_T handle, TDL_AUDIO_MIC_CB callback)
{
    (void)handle;
    if (!s_ready || !callback) return OPRT_INVALID_PARM;
    /* Missing/incompatible speech models must not create invalid AFE tasks. */
    OPERATE_RET afe_rc=audio_afe_processor_init();
    if (afe_rc != OPRT_OK) return afe_rc;
    if (xSemaphoreTake(s_lifecycle, pdMS_TO_TICKS(1000)) != pdTRUE) return OPRT_RESOURCE_NOT_READY;
    if (xSemaphoreTake(s_read_lock, pdMS_TO_TICKS(1000)) != pdTRUE) {
        xSemaphoreGive(s_lifecycle); return OPRT_RESOURCE_NOT_READY;
    }
    if (s_running || s_worker_exists) {
        xSemaphoreGive(s_read_lock); xSemaphoreGive(s_lifecycle); return OPRT_RESOURCE_NOT_READY;
    }
    if (desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK) == DESK_OWNER_SDK_REMOTE &&
        !audio_preempt_quiesce(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE,
                               DESK_OWNER_TCLAW_VOICE, 1000, NULL)) {
        xSemaphoreGive(s_read_lock); xSemaphoreGive(s_lifecycle); return OPRT_RESOURCE_NOT_READY;
    }
    if (desk_arbiter_acquire(DESK_RES_AUDIO_RECORD, DESK_OWNER_TCLAW_VOICE, 0) != DESK_ARBITER_OK) {
        xSemaphoreGive(s_read_lock); xSemaphoreGive(s_lifecycle); return OPRT_RESOURCE_NOT_READY;
    }
    s_mic_cb = callback;
    s_running = true;
    s_worker_exists = true;
    xSemaphoreTake(s_stopped, 0);
    if (xTaskCreateWithCaps(mic_task, "watcher_mic", 4096, NULL, 5, NULL,
                            MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT) != pdPASS) {
        s_running = false; s_worker_exists = false; s_mic_cb = NULL;
        desk_arbiter_release(DESK_RES_AUDIO_RECORD, DESK_OWNER_TCLAW_VOICE);
        xSemaphoreGive(s_read_lock); xSemaphoreGive(s_lifecycle); return OPRT_MALLOC_FAILED;
    }
    xSemaphoreGive(s_read_lock);
    xSemaphoreGive(s_lifecycle);
    return OPRT_OK;
}

static OPERATE_RET native_play(TDD_AUDIO_HANDLE_T h, uint8_t *data, uint32_t len)
{
    (void)h;
    if (len % sizeof(int16_t) || !len) return OPRT_INVALID_PARM;
    if (!s_tclaw_play_lock ||
        xSemaphoreTake(s_tclaw_play_lock, pdMS_TO_TICKS(100)) != pdTRUE)
        return OPRT_RESOURCE_NOT_READY;
    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK) {
        xSemaphoreGive(s_tclaw_play_lock);
        return OPRT_RESOURCE_NOT_READY;
    }
    s_tclaw_playback_session_active = true;
    int rc = desk_watcher_audio_play((int16_t *)data, len / sizeof(int16_t), RATE);
    /* TDL sends many decoder chunks then issues PLAY_STOP. Do not release the
     * arbiter at write return: IDF only copied bytes into DMA descriptors. */
    xSemaphoreGive(s_tclaw_play_lock);
    return rc == DESK_HW_OK ? OPRT_OK : OPRT_COM_ERROR;
}

static OPERATE_RET native_config(TDD_AUDIO_HANDLE_T h, TDD_AUDIO_CMD_E command, void *args)
{
    (void)h;
    if (!s_ready) return OPRT_RESOURCE_NOT_READY;
    if (command == TDD_AUDIO_CMD_PLAY_STOP) {
        if (!s_tclaw_play_lock ||
            xSemaphoreTake(s_tclaw_play_lock, pdMS_TO_TICKS(TX_DMA_DRAIN_TIMEOUT_MS)) != pdTRUE)
            return OPRT_RESOURCE_NOT_READY;
        desk_resource_owner_t owner = desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK);
        if (owner == DESK_OWNER_NONE || owner == DESK_OWNER_SYSTEM_ALERT) {
            /* A completed alert preemption already drained and took ownership. */
            s_tclaw_playback_session_active = false;
            xSemaphoreGive(s_tclaw_play_lock);
            return OPRT_OK;
        }
        if (!s_tclaw_playback_session_active) {
            /* PLAY_STOP without a TClaw-owned session must not release a
             * different caller's shared audio owner. */
            xSemaphoreGive(s_tclaw_play_lock);
            return OPRT_OK;
        }
        if (owner != DESK_OWNER_TCLAW_VOICE) {
            xSemaphoreGive(s_tclaw_play_lock);
            return OPRT_RESOURCE_NOT_READY;
        }
        int drain_rc = desk_watcher_audio_tx_drain(TX_DMA_DRAIN_TIMEOUT_MS);
        if (drain_rc == DESK_HW_OK) {
            s_tclaw_playback_session_active = false;
            desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
        }
        xSemaphoreGive(s_tclaw_play_lock);
        return drain_rc == DESK_HW_OK ? OPRT_OK : OPRT_RESOURCE_NOT_READY;
    }
    if (command != TDD_AUDIO_CMD_SET_VOLUME || !args) return OPRT_INVALID_PARM;
    unsigned volume = *(uint8_t *)args;
    if (volume > 70) volume = 70;
    if (xSemaphoreTake(s_write_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return OPRT_RESOURCE_NOT_READY;
    int rc = esp_codec_dev_set_out_vol(s_speaker, volume);
    xSemaphoreGive(s_write_lock);
    return rc == 0 ? OPRT_OK : OPRT_COM_ERROR;
}

static OPERATE_RET native_close(TDD_AUDIO_HANDLE_T h)
{
    (void)h;
    if (xSemaphoreTake(s_lifecycle, pdMS_TO_TICKS(1000)) != pdTRUE) return OPRT_RESOURCE_NOT_READY;
    if (!s_running && !s_worker_exists) { xSemaphoreGive(s_lifecycle); return OPRT_OK; }
    s_running = false;
    bool stopped=xSemaphoreTake(s_stopped, pdMS_TO_TICKS(2000)) == pdTRUE;
    xSemaphoreGive(s_lifecycle);
    return stopped ? OPRT_OK : OPRT_RESOURCE_NOT_READY;
}

int desk_watcher_audio_test_tone(void)
{
    /* 1000 ms, 1000 Hz square wave at amplitude 12000 (~37% full scale) for audible verification. */
    int16_t frame[FRAME_SAMPLES];
    if (!s_ready) return DESK_HW_ERR_UNAVAILABLE;
    if (desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK) != DESK_OWNER_NONE)
        return DESK_HW_ERR_UNAVAILABLE;
    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE, 0) != DESK_ARBITER_OK)
        return DESK_HW_ERR_UNAVAILABLE;
    if (!s_one_shot_lock ||
        xSemaphoreTake(s_one_shot_lock, pdMS_TO_TICKS(TX_DMA_DRAIN_TIMEOUT_MS)) != pdTRUE) {
        desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
        return DESK_HW_ERR_TIMEOUT;
    }

    /* Save previous volume and mute status to restore after test */
    int prev_vol = 50;
    bool prev_mute = false;
    esp_codec_dev_get_out_vol(s_speaker, &prev_vol);
    esp_codec_dev_get_out_mute(s_speaker, &prev_mute);

    /* Ensure unmuted and safe, audible volume */
    if (esp_codec_dev_set_out_mute(s_speaker, false) != 0 ||
        esp_codec_dev_set_out_vol(s_speaker, 65) != 0) {
        desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
        xSemaphoreGive(s_one_shot_lock);
        return DESK_HW_ERR_IO;
    }

    ESP_LOGI("watcher_audio", "Starting 1000ms 1kHz audio test tone (vol=65, amp=12000)...");
    int rc = DESK_HW_OK;
    const unsigned total_samples = RATE; /* 1000 ms @ 16000 Hz = 16000 samples */
    for (unsigned offset = 0; offset < total_samples && rc == DESK_HW_OK; offset += FRAME_SAMPLES) {
        unsigned n = total_samples - offset < FRAME_SAMPLES ? total_samples - offset : FRAME_SAMPLES;
        /* 1000 Hz tone at 16000 Hz sample rate: period is 16 samples (8 high, 8 low) */
        for (unsigned i = 0; i < n; ++i) {
            frame[i] = ((offset + i) % 16 < 8) ? 12000 : -12000;
        }
        rc = desk_watcher_audio_play(frame, n, RATE);
    }
    ESP_LOGI("watcher_audio", "Audio test tone completed rc=%d", rc);

    int drain_rc = desk_watcher_audio_tx_drain(TX_DMA_DRAIN_TIMEOUT_MS);
    if (drain_rc == DESK_HW_OK) {
        /* Restore codec controls only after the final tone samples drained. */
        esp_codec_dev_set_out_vol(s_speaker, prev_vol);
        esp_codec_dev_set_out_mute(s_speaker, prev_mute);
        desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
    } else if (rc == DESK_HW_OK) {
        rc = drain_rc;
    }
    xSemaphoreGive(s_one_shot_lock);
    return rc;
}

int desk_watcher_audio_init(void)
{
    if (s_ready) return DESK_HW_OK;
    /* A failed partial setup retains its resources; do not allocate a second
     * I2S pair on a retry. A reboot is required after a physical init failure. */
    if (s_attempted) return DESK_HW_ERR_UNAVAILABLE;
    s_attempted = true;
    if (desk_watcher_board_i2c_ensure() != DESK_HW_OK ||
        desk_watcher_board_i2c_probe(0x18) != DESK_HW_OK) return DESK_HW_ERR_UNAVAILABLE;
    bool adc_e = desk_watcher_board_i2c_probe(0x13) != DESK_HW_OK;
    if (adc_e && desk_watcher_board_i2c_probe(0x14) != DESK_HW_OK) return DESK_HW_ERR_UNAVAILABLE;
    /* Keep the amplifier off throughout codec setup. */
    if (desk_watcher_board_audio_pa_set(false) != DESK_HW_OK) return DESK_HW_ERR_UNAVAILABLE;
    s_read_lock = xSemaphoreCreateMutex(); s_write_lock = xSemaphoreCreateMutex();
    s_stopped = xSemaphoreCreateBinary();
    s_lifecycle = xSemaphoreCreateMutex();
    s_tclaw_play_lock = xSemaphoreCreateMutex();
    s_one_shot_lock = xSemaphoreCreateMutex();
    s_stream_lock = xSemaphoreCreateMutex();
    s_stream_done = xSemaphoreCreateBinary();
    s_stream_ctx = heap_caps_calloc(1, sizeof(*s_stream_ctx), MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT);
    if (!s_read_lock || !s_write_lock || !s_stopped || !s_lifecycle || !s_tclaw_play_lock ||
        !s_one_shot_lock ||
        !s_stream_lock || !s_stream_done || !s_stream_ctx) goto fail;
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    channel.dma_desc_num = TX_DMA_DESC_NUM;
    channel.dma_frame_num = TX_DMA_FRAME_NUM;
    channel.auto_clear_after_cb = true;
    if (i2s_new_channel(&channel, &s_tx, &s_rx) != ESP_OK) goto fail;
    i2s_std_config_t standard = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = { .mclk=10, .bclk=11, .ws=12, .dout=16, .din=15 },
    };
    standard.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;
    if (i2s_channel_init_std_mode(s_tx, &standard) != ESP_OK) goto fail;
    static const i2s_event_callbacks_t tx_callbacks = { .on_sent = tx_on_sent };
    if (i2s_channel_register_event_callback(s_tx, &tx_callbacks, NULL) != ESP_OK ||
        i2s_channel_enable(s_tx) != ESP_OK) goto fail;
    standard.slot_cfg.slot_mask = I2S_STD_SLOT_RIGHT;
    if (i2s_channel_init_std_mode(s_rx, &standard) != ESP_OK || i2s_channel_enable(s_rx) != ESP_OK) goto fail;
    audio_codec_i2s_cfg_t bus = { .port=I2S_NUM_0, .rx_handle=s_rx, .tx_handle=s_tx };
    const audio_codec_data_if_t *data = audio_codec_new_i2s_data(&bus);
    audio_codec_i2c_cfg_t dac_bus = { .port=0, .addr=0x30 };
    const audio_codec_ctrl_if_t *dac_ctrl = audio_codec_new_i2c_ctrl(&dac_bus);
    const audio_codec_gpio_if_t *gpio = audio_codec_new_gpio();
    if (!data || !dac_ctrl || !gpio) goto fail;
    es8311_codec_cfg_t dac_config = {
        .ctrl_if=dac_ctrl, .gpio_if=gpio, .codec_mode=ESP_CODEC_DEV_WORK_MODE_DAC,
        .pa_pin=-1, .use_mclk=true, .hw_gain={ .pa_voltage=5.0, .codec_dac_voltage=3.3 },
    };
    const audio_codec_if_t *dac = es8311_codec_new(&dac_config);
    audio_codec_i2c_cfg_t adc_bus = { .port=0, .addr=(adc_e ? 0x14 : 0x13) << 1 };
    const audio_codec_ctrl_if_t *adc_ctrl = audio_codec_new_i2c_ctrl(&adc_bus);
    if (!dac || !adc_ctrl) goto fail;
    const audio_codec_if_t *adc;
    if (adc_e) { es7243e_codec_cfg_t cfg = { .ctrl_if=adc_ctrl }; adc=es7243e_codec_new(&cfg); }
    else { es7243_codec_cfg_t cfg = { .ctrl_if=adc_ctrl }; adc=es7243_codec_new(&cfg); }
    if (!adc) goto fail;
    esp_codec_dev_cfg_t out = { .dev_type=ESP_CODEC_DEV_TYPE_OUT, .codec_if=dac, .data_if=data };
    esp_codec_dev_cfg_t in = { .dev_type=ESP_CODEC_DEV_TYPE_IN, .codec_if=adc, .data_if=data };
    s_speaker=esp_codec_dev_new(&out); s_mic=esp_codec_dev_new(&in);
    if (!s_speaker || !s_mic) goto fail;
    esp_codec_dev_sample_info_t format = {
        .sample_rate = RATE,
        .channel = 2,
        .bits_per_sample = 16,
        .channel_mask = ESP_CODEC_DEV_MAKE_CHANNEL_MASK(0) | ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1),
    };
    if (esp_codec_dev_open(s_speaker, &format) != 0) goto fail;
    format.channel = 2;
    format.channel_mask = ESP_CODEC_DEV_MAKE_CHANNEL_MASK(1);
    if (esp_codec_dev_open(s_mic, &format) != 0 ||
        esp_codec_dev_set_out_vol(s_speaker, 65) != 0 ||
        esp_codec_dev_set_in_gain(s_mic, 27) != 0) goto fail;

    /* Re-assert Seeed Watcher hardware configuration on shared I2S0:
     * TX: MONO data duplicated to BOTH slots, 16-bit slot width, BCLK = 512kHz.
     * RX: MONO sampled from RIGHT slot (ES7243/E), 16-bit slot width.
     * Overrides any slot reconfiguration performed during esp_codec_dev_open. */
    esp_err_t err;
    if ((err = i2s_channel_disable(s_tx)) != ESP_OK) {
        ESP_LOGE("watcher_audio", "i2s disable tx failed: %d", err);
        goto fail;
    }
    i2s_std_slot_config_t tx_slot = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
    tx_slot.slot_mask = I2S_STD_SLOT_BOTH;
    if ((err = i2s_channel_reconfig_std_slot(s_tx, &tx_slot)) != ESP_OK) {
        ESP_LOGE("watcher_audio", "i2s reconfig tx slot failed: %d", err);
        goto fail;
    }
    i2s_std_clk_config_t tx_clk = I2S_STD_CLK_DEFAULT_CONFIG(RATE);
    if ((err = i2s_channel_reconfig_std_clock(s_tx, &tx_clk)) != ESP_OK) {
        ESP_LOGE("watcher_audio", "i2s reconfig tx clock failed: %d", err);
        goto fail;
    }
    tx_eof_arm_for_final_configuration();
    if ((err = i2s_channel_enable(s_tx)) != ESP_OK) {
        ESP_LOGE("watcher_audio", "i2s enable tx failed: %d", err);
        goto fail;
    }

    if ((err = i2s_channel_disable(s_rx)) != ESP_OK) {
        ESP_LOGE("watcher_audio", "i2s disable rx failed: %d", err);
        goto fail;
    }
    i2s_std_slot_config_t rx_slot = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO);
    rx_slot.slot_mask = I2S_STD_SLOT_RIGHT;
    if ((err = i2s_channel_reconfig_std_slot(s_rx, &rx_slot)) != ESP_OK) {
        ESP_LOGE("watcher_audio", "i2s reconfig rx slot failed: %d", err);
        goto fail;
    }
    i2s_std_clk_config_t rx_clk = I2S_STD_CLK_DEFAULT_CONFIG(RATE);
    if ((err = i2s_channel_reconfig_std_clock(s_rx, &rx_clk)) != ESP_OK) {
        ESP_LOGE("watcher_audio", "i2s reconfig rx clock failed: %d", err);
        goto fail;
    }
    if ((err = i2s_channel_enable(s_rx)) != ESP_OK) {
        ESP_LOGE("watcher_audio", "i2s enable rx failed: %d", err);
        goto fail;
    }

    static TDD_AUDIO_INTFS_T interfaces = { .open=native_open, .play=native_play, .config=native_config, .close=native_close };
    static TDD_AUDIO_INFO_T info = { .sample_rate=RATE, .sample_ch_num=1, .sample_bits=16, .sample_tm_ms=20 };
    if (tdl_audio_driver_register("watcher_audio", &s_ready, &interfaces, &info) != OPRT_OK) goto fail;
    if (desk_arbiter_register_preempt_handler(DESK_RES_AUDIO_PLAYBACK,
            audio_preempt_quiesce, NULL) != DESK_ARBITER_OK) goto fail;
    if (desk_watcher_board_audio_pa_set(true) != DESK_HW_OK) goto fail;
    s_ready=true;
    ESP_LOGI("watcher_audio", "ES8311 + %s ready; shared I2S0 16kHz PCM (vol=65, mic_gain=27dB)", adc_e ? "ES7243E" : "ES7243");
    return DESK_HW_OK;
fail:
    desk_watcher_board_audio_pa_set(false);
    ESP_LOGE("watcher_audio", "codec init failed; audio unavailable until reboot");
    return DESK_HW_ERR_UNAVAILABLE;
}
#endif
