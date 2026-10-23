#ifndef DESK_AUDIO_RESAMPLER_H
#define DESK_AUDIO_RESAMPLER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Stateful rational 24 kHz -> 16 kHz mono S16LE conversion. */
typedef struct {
    int16_t pending[3];
    uint8_t pending_count;
    uint64_t input_samples;
    uint64_t output_samples;
    bool finished;
} desk_audio_resampler_t;

/*
 * Streaming 24 kHz -> 16 kHz linear interpolation. This preserves sample
 * duration and chunk continuity but is not an anti-aliasing low-pass filter;
 * callers must provide PCM band-limited to <= 8 kHz. Higher components can
 * fold into the output band. A production quality filter requires a separately
 * specified passband/stopband contract and hardware listening validation.
 */
void desk_audio_resampler_init(desk_audio_resampler_t *state);
int desk_audio_resampler_process(desk_audio_resampler_t *state,
                                 const int16_t *input, size_t input_samples,
                                 int16_t *output, size_t output_capacity,
                                 size_t *output_samples);
int desk_audio_resampler_finish(desk_audio_resampler_t *state,
                                int16_t *output, size_t output_capacity,
                                size_t *output_samples);

#endif
