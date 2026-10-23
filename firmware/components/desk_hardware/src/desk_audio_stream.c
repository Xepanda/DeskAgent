#include "desk_audio_stream.h"

#include <limits.h>
#include <string.h>

#define STREAM_ERR_INVALID 1u
#define STREAM_ERR_STALE 2u
#define STREAM_ERR_FORMAT 3u
#define STREAM_ERR_SEQUENCE 4u
#define STREAM_ERR_LENGTH 5u
#define STREAM_ERR_CODEC 6u

static int matches(const desk_audio_stream_ctx_t *ctx, uint64_t handle)
{
    return ctx && handle && ctx->handle == handle && ctx->state != DESK_AUDIO_STREAM_IDLE;
}

static void clear_queue(desk_audio_stream_ctx_t *ctx)
{
    ctx->queue_head = 0;
    ctx->queue_depth = 0;
}

static void fail_and_cancel(desk_audio_stream_ctx_t *ctx, uint32_t error)
{
    ctx->error_code = error;
    ctx->cancel_requested = true;
    ctx->finish_requested = false;
    ctx->state = DESK_AUDIO_STREAM_CANCELING;
    clear_queue(ctx);
}

int desk_audio_stream_begin(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                            uint32_t connection_generation, uint32_t stream_id,
                            uint32_t sample_rate_hz, uint8_t channels,
                            uint8_t sample_width_bytes, uint32_t total_bytes,
                            bool capture_active)
{
    if (!ctx || !handle) return -1;
    if (ctx->owner_held || ctx->playing ||
        (ctx->state == DESK_AUDIO_STREAM_RECEIVING ||
         ctx->state == DESK_AUDIO_STREAM_DRAINING ||
         ctx->state == DESK_AUDIO_STREAM_CANCELING)) return -2;
    if (!connection_generation || sample_rate_hz != 24000 || channels != 1 ||
        sample_width_bytes != 2 || total_bytes < 2 ||
        total_bytes > DESK_AUDIO_STREAM_MAX_BYTES || (total_bytes & 1u)) {
        return -1;
    }
    if (capture_active) return -2;

    memset(ctx, 0, sizeof(*ctx));
    ctx->handle = handle;
    ctx->connection_generation = connection_generation;
    ctx->stream_id = stream_id;
    ctx->total_bytes = total_bytes;
    ctx->owner_held = true;
    ctx->state = DESK_AUDIO_STREAM_RECEIVING;
    desk_audio_resampler_init(&ctx->resampler);
    return 0;
}

int desk_audio_stream_write(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                            uint32_t connection_generation, uint32_t stream_id,
                            uint32_t sequence, const uint8_t *pcm, size_t bytes)
{
    if (!matches(ctx, handle) || !ctx->owner_held ||
        ctx->state != DESK_AUDIO_STREAM_RECEIVING) return -1;
    if (connection_generation != ctx->connection_generation || stream_id != ctx->stream_id) {
        fail_and_cancel(ctx, STREAM_ERR_STALE);
        return -1;
    }
    if (!pcm || bytes < 2 || bytes > DESK_AUDIO_STREAM_CHUNK_BYTES || (bytes & 1u)) {
        fail_and_cancel(ctx, STREAM_ERR_FORMAT);
        return -1;
    }
    if (sequence != ctx->expected_rx_seq) {
        fail_and_cancel(ctx, STREAM_ERR_SEQUENCE);
        return -1;
    }
    if (bytes > ctx->total_bytes - ctx->accepted_bytes) {
        fail_and_cancel(ctx, STREAM_ERR_LENGTH);
        return -1;
    }
    if (ctx->queue_depth == DESK_AUDIO_STREAM_QUEUE_SLOTS) return -3; /* backpressure */

    uint8_t slot_ix = (uint8_t)((ctx->queue_head + ctx->queue_depth) % DESK_AUDIO_STREAM_QUEUE_SLOTS);
    desk_audio_stream_slot_t *slot = &ctx->queue[slot_ix];
    memcpy(slot->data, pcm, bytes);
    slot->bytes = (uint16_t)bytes;
    slot->sequence = sequence;
    ctx->queue_depth++;
    ctx->accepted_bytes += (uint32_t)bytes;
    ctx->expected_rx_seq++;
    return 0;
}

int desk_audio_stream_finish(desk_audio_stream_ctx_t *ctx, uint64_t handle)
{
    if (!matches(ctx, handle) || !ctx->owner_held ||
        ctx->state != DESK_AUDIO_STREAM_RECEIVING) return -1;
    if (ctx->accepted_bytes != ctx->total_bytes) {
        fail_and_cancel(ctx, STREAM_ERR_LENGTH);
        return -1;
    }
    ctx->finish_requested = true;
    return 0;
}

int desk_audio_stream_cancel(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                             uint32_t reason_code)
{
    if (!matches(ctx, handle) || !ctx->owner_held) return -1;
    ctx->error_code = reason_code;
    ctx->cancel_requested = true;
    ctx->finish_requested = false;
    ctx->state = DESK_AUDIO_STREAM_CANCELING;
    clear_queue(ctx);
    return 0;
}

int desk_audio_stream_next_output(desk_audio_stream_ctx_t *ctx, int16_t *output,
                                  size_t output_capacity, size_t *output_samples,
                                  bool *needs_static_confirmation)
{
    if (output_samples) *output_samples = 0;
    if (needs_static_confirmation) *needs_static_confirmation = false;
    if (!ctx || !output_samples || !needs_static_confirmation || ctx->playing) return -1;
    if (ctx->state == DESK_AUDIO_STREAM_CANCELING) {
        *needs_static_confirmation = true;
        return 0;
    }
    if (ctx->state == DESK_AUDIO_STREAM_DRAINING) {
        *needs_static_confirmation = true;
        return 0;
    }
    if (ctx->state != DESK_AUDIO_STREAM_RECEIVING) return -1;

    while (ctx->queue_depth) {
        desk_audio_stream_slot_t *slot = &ctx->queue[ctx->queue_head];
        size_t output_count = 0;
        int rc = desk_audio_resampler_process(&ctx->resampler,
                                              (const int16_t *)slot->data,
                                              slot->bytes / sizeof(int16_t), output,
                                              output_capacity, &output_count);
        if (rc != 0) {
            fail_and_cancel(ctx, STREAM_ERR_CODEC);
            *needs_static_confirmation = true;
            return -1;
        }
        ctx->queue_head = (uint8_t)((ctx->queue_head + 1) % DESK_AUDIO_STREAM_QUEUE_SLOTS);
        ctx->queue_depth--;
        if (output_count) {
            ctx->playing = true;
            *output_samples = output_count;
            return 1;
        }
    }

    if (ctx->finish_requested) {
        size_t tail_samples = 0;
        if (desk_audio_resampler_finish(&ctx->resampler, output, output_capacity,
                                        &tail_samples) != 0) {
            fail_and_cancel(ctx, STREAM_ERR_CODEC);
            *needs_static_confirmation = true;
            return -1;
        }
        ctx->finish_requested = false;
        if (tail_samples) {
            ctx->playing = true;
            *output_samples = tail_samples;
            return 1;
        }
        ctx->state = DESK_AUDIO_STREAM_DRAINING;
        *needs_static_confirmation = true;
    }
    return 0;
}

int desk_audio_stream_write_complete(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                                     int write_result)
{
    if (!matches(ctx, handle) || !ctx->playing) return -1;
    ctx->playing = false;
    if (write_result != 0) fail_and_cancel(ctx, STREAM_ERR_CODEC);
    return 0;
}

int desk_audio_stream_confirm_static(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                                     bool static_confirmed)
{
    if (!matches(ctx, handle) || ctx->playing ||
        (ctx->state != DESK_AUDIO_STREAM_CANCELING && ctx->state != DESK_AUDIO_STREAM_DRAINING)) {
        return -1;
    }
    if (!static_confirmed) return -2;
    ctx->owner_held = false;
    ctx->cancel_requested = false;
    if (ctx->error_code) ctx->state = DESK_AUDIO_STREAM_FAILED;
    else ctx->state = ctx->state == DESK_AUDIO_STREAM_DRAINING ?
                      DESK_AUDIO_STREAM_COMPLETE : DESK_AUDIO_STREAM_CANCELED;
    return 0;
}

int desk_audio_stream_get_status(const desk_audio_stream_ctx_t *ctx, uint64_t handle,
                                 desk_audio_stream_status_t *out_status)
{
    if (!matches(ctx, handle) || !out_status) return -1;
    memset(out_status, 0, sizeof(*out_status));
    out_status->handle = ctx->handle;
    out_status->connection_generation = ctx->connection_generation;
    out_status->stream_id = ctx->stream_id;
    out_status->expected_rx_seq = ctx->expected_rx_seq;
    out_status->total_bytes = ctx->total_bytes;
    out_status->accepted_bytes = ctx->accepted_bytes;
    out_status->error_code = ctx->error_code;
    out_status->pending_frames = ctx->queue_depth + (ctx->playing ? 1 : 0);
    out_status->queue_depth = ctx->queue_depth;
    /* Playback in flight no longer occupies a software queue slot. */
    out_status->free_frames = DESK_AUDIO_STREAM_QUEUE_SLOTS - ctx->queue_depth;
    out_status->start_buffer_frames = DESK_AUDIO_STREAM_START_BUFFER_FRAMES;
    out_status->state = ctx->state;
    out_status->playing = ctx->playing;
    out_status->tx_drained = !ctx->owner_held &&
        (ctx->state == DESK_AUDIO_STREAM_COMPLETE || ctx->state == DESK_AUDIO_STREAM_CANCELED ||
         ctx->state == DESK_AUDIO_STREAM_FAILED);
    return 0;
}
