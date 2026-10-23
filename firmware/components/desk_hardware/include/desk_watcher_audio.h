#ifndef DESK_WATCHER_AUDIO_H
#define DESK_WATCHER_AUDIO_H
#include "desk_hardware.h"
/* One physical I2S0 pair shared by HAL diagnostics and the native TDL driver. */
int desk_watcher_audio_init(void);
int desk_watcher_audio_read(int16_t *pcm, size_t samples, size_t *read_samples);
int desk_watcher_audio_play(const int16_t *pcm, size_t samples, uint32_t rate);
/* One-shot helper keeps owner stable across PCM write and DMA EOF fence. */
int desk_watcher_audio_play_one_shot(const int16_t *pcm, size_t samples, uint32_t rate);
int desk_watcher_audio_play_stop(void);
/* Wait for ordered TX DMA EOF completion after writing one ring of silence. */
int desk_watcher_audio_tx_drain(uint32_t timeout_ms);
int desk_watcher_audio_stream_begin(uint64_t handle, uint32_t connection_generation,
                                    uint32_t stream_id, uint32_t sample_rate_hz,
                                    uint8_t channels, uint8_t sample_width_bytes,
                                    uint32_t total_bytes);
int desk_watcher_audio_stream_write(uint64_t handle, uint32_t connection_generation,
                                    uint32_t stream_id, uint32_t sequence,
                                    const uint8_t *pcm, size_t bytes,
                                    desk_audio_stream_status_t *out_status);
int desk_watcher_audio_stream_finish(uint64_t handle);
int desk_watcher_audio_stream_stop(uint64_t handle, uint32_t timeout_ms);
int desk_watcher_audio_stream_get_status(uint64_t handle,
                                         desk_audio_stream_status_t *out_status);
void desk_watcher_audio_status(bool *ready, bool *running, uint32_t *frames,
                               uint32_t *errors, uint32_t *peak);
int desk_watcher_audio_test_tone(void);
#endif
