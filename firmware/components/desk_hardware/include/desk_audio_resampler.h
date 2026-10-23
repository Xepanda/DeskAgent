#ifndef DESK_AUDIO_RESAMPLER_H
#define DESK_AUDIO_RESAMPLER_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/* Stateful rational 24 kHz -> 16 kHz mono S16LE conversion. */
#define DESK_AUDIO_RESAMPLER_FIR_TAPS 19u
typedef struct {
    int16_t pending[3];
    uint8_t pending_count;
    uint64_t input_samples;
    uint64_t output_samples;
    bool finished;
    int16_t fir_history[DESK_AUDIO_RESAMPLER_FIR_TAPS];
    uint8_t fir_position;
} desk_audio_resampler_t;

/*
 * Streaming 24 kHz -> 16 kHz conversion with a 19-tap Q15 Hamming-windowed
 * low-pass FIR (7.6 kHz nominal cutoff) before the existing 2:3 interpolation.
 * FIR history is carried across calls. The conversion preserves output sample
 * count/duration; it adds 9 input samples (0.375 ms) of causal group delay.
 * This low-order filter is intended to suppress aliases, not to claim a
 * studio-grade passband/stopband response or hardware listening validation.
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
