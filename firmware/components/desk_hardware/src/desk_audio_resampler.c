#include "desk_audio_resampler.h"

#include <limits.h>
#include <string.h>

/*
 * 19-tap Hamming-windowed sinc, designed at 24 kHz with a 7.6 kHz cutoff.
 * Q15 coefficients sum exactly to 32768. Its measured coefficient response is
 * about -0.33 dB at 6 kHz, -23.2 dB at 9 kHz, and -55.9 dB at 10 kHz.
 */
static const int16_t s_fir_coefficients_q15[DESK_AUDIO_RESAMPLER_FIR_TAPS] = {
    -75, -29, 274, -318, -481, 1612, -830, -3469, 9292, 20816,
    9292, -3469, -830, 1612, -481, -318, 274, -29, -75,
};

static int16_t filter_sample(desk_audio_resampler_t *state, int16_t input)
{
    state->fir_history[state->fir_position] = input;
    int64_t accumulator = 0;
    for (size_t tap = 0; tap < DESK_AUDIO_RESAMPLER_FIR_TAPS; ++tap) {
        size_t history_index = (state->fir_position + DESK_AUDIO_RESAMPLER_FIR_TAPS - tap) %
                               DESK_AUDIO_RESAMPLER_FIR_TAPS;
        accumulator += (int64_t)s_fir_coefficients_q15[tap] * state->fir_history[history_index];
    }
    state->fir_position = (uint8_t)((state->fir_position + 1u) % DESK_AUDIO_RESAMPLER_FIR_TAPS);

    int64_t rounded = accumulator + (accumulator >= 0 ? 16384 : -16384);
    int64_t filtered = rounded / 32768;
    if (filtered > INT16_MAX) filtered = INT16_MAX;
    if (filtered < INT16_MIN) filtered = INT16_MIN;
    return (int16_t)filtered;
}

void desk_audio_resampler_init(desk_audio_resampler_t *state)
{
    if (state) memset(state, 0, sizeof(*state));
}

int desk_audio_resampler_process(desk_audio_resampler_t *state,
                                 const int16_t *input, size_t input_samples,
                                 int16_t *output, size_t output_capacity,
                                 size_t *output_samples)
{
    if (output_samples) *output_samples = 0;
    if (!state || !output_samples || (input_samples && !input) ||
        state->pending_count > 2 || state->fir_position >= DESK_AUDIO_RESAMPLER_FIR_TAPS ||
        (state->finished && input_samples) ||
        state->input_samples > UINT64_MAX - input_samples) {
        return -1;
    }

    size_t groups = ((size_t)state->pending_count + input_samples) / 3;
    if (groups > SIZE_MAX / 2) return -1;
    size_t required = groups * 2;
    if (required > output_capacity || (required && !output)) return -1;

    size_t written = 0;
    for (size_t i = 0; i < input_samples; ++i) {
        state->pending[state->pending_count++] = filter_sample(state, input[i]);
        state->input_samples++;
        if (state->pending_count == 3) {
            int32_t midpoint = (int32_t)state->pending[1] + (int32_t)state->pending[2];
            output[written++] = state->pending[0];
            output[written++] = (int16_t)(midpoint / 2);
            state->output_samples += 2;
            state->pending_count = 0;
        }
    }
    *output_samples = written;
    return 0;
}

int desk_audio_resampler_finish(desk_audio_resampler_t *state,
                                int16_t *output, size_t output_capacity,
                                size_t *output_samples)
{
    if (output_samples) *output_samples = 0;
    if (!state || !output_samples) return -1;
    if (state->finished) return 0;
    size_t required = state->pending_count ? 1 : 0;
    if (required > output_capacity || (required && !output)) return -1;
    if (required) {
        /* Nearest endpoint keeps duration error below one 16 kHz sample. */
        output[0] = state->pending[0];
        state->output_samples++;
        *output_samples = 1;
    }
    state->pending_count = 0;
    state->finished = true;
    return 0;
}
