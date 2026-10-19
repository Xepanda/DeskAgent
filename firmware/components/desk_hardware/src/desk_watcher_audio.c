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
static esp_codec_dev_handle_t s_mic, s_speaker;
static i2s_chan_handle_t s_tx, s_rx;
static SemaphoreHandle_t s_read_lock, s_write_lock, s_stopped, s_lifecycle;
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
    if (xSemaphoreTake(s_write_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return DESK_HW_ERR_TIMEOUT;
    int rc = esp_codec_dev_write(s_speaker, (void *)pcm, samples * sizeof(*pcm));
    xSemaphoreGive(s_write_lock);
    return rc == 0 ? DESK_HW_OK : DESK_HW_ERR_IO;
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
    if (desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE, 100) != DESK_ARBITER_OK)
        return OPRT_RESOURCE_NOT_READY;
    int rc = desk_watcher_audio_play((int16_t *)data, len / sizeof(int16_t), RATE);
    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE);
    return rc == DESK_HW_OK ? OPRT_OK : OPRT_COM_ERROR;
}

static OPERATE_RET native_config(TDD_AUDIO_HANDLE_T h, TDD_AUDIO_CMD_E command, void *args)
{
    (void)h;
    if (!s_ready) return OPRT_RESOURCE_NOT_READY;
    if (command == TDD_AUDIO_CMD_PLAY_STOP) {
        /* Writes are synchronous, serialized by the output mutex. */
        if (xSemaphoreTake(s_write_lock, pdMS_TO_TICKS(1000)) != pdTRUE) return OPRT_RESOURCE_NOT_READY;
        xSemaphoreGive(s_write_lock); return OPRT_OK;
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

    /* Save previous volume and mute status to restore after test */
    int prev_vol = 50;
    bool prev_mute = false;
    esp_codec_dev_get_out_vol(s_speaker, &prev_vol);
    esp_codec_dev_get_out_mute(s_speaker, &prev_mute);

    /* Ensure unmuted and safe, audible volume */
    if (esp_codec_dev_set_out_mute(s_speaker, false) != 0 ||
        esp_codec_dev_set_out_vol(s_speaker, 65) != 0) {
        desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
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

    /* Restore previous volume and mute status */
    esp_codec_dev_set_out_vol(s_speaker, prev_vol);
    esp_codec_dev_set_out_mute(s_speaker, prev_mute);

    desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE);
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
    if (!s_read_lock || !s_write_lock || !s_stopped || !s_lifecycle) goto fail;
    i2s_chan_config_t channel = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_0, I2S_ROLE_MASTER);
    channel.auto_clear = true;
    if (i2s_new_channel(&channel, &s_tx, &s_rx) != ESP_OK) goto fail;
    i2s_std_config_t standard = {
        .clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(RATE),
        .slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
        .gpio_cfg = { .mclk=10, .bclk=11, .ws=12, .dout=16, .din=15 },
    };
    standard.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;
    if (i2s_channel_init_std_mode(s_tx, &standard) != ESP_OK || i2s_channel_enable(s_tx) != ESP_OK) goto fail;
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
