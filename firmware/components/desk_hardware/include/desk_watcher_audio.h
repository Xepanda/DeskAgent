#ifndef DESK_WATCHER_AUDIO_H
#define DESK_WATCHER_AUDIO_H
#include "desk_hardware.h"
/* One physical I2S0 pair shared by HAL diagnostics and the native TDL driver. */
int desk_watcher_audio_init(void);
int desk_watcher_audio_read(int16_t *pcm, size_t samples, size_t *read_samples);
int desk_watcher_audio_play(const int16_t *pcm, size_t samples, uint32_t rate);
void desk_watcher_audio_status(bool *ready, bool *running, uint32_t *frames,
                               uint32_t *errors, uint32_t *peak);
int desk_watcher_audio_test_tone(void);
#endif
