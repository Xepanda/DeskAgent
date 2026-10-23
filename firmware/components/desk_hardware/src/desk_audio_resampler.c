#include "desk_audio_resampler.h"

#include <limits.h>
#include <string.h>

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
        state->pending[state->pending_count++] = input[i];
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
