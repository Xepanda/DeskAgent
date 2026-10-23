#ifndef DESK_AUDIO_STREAM_H
#define DESK_AUDIO_STREAM_H

#include "desk_audio_resampler.h"
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define DESK_AUDIO_STREAM_CHUNK_BYTES 4096u
#define DESK_AUDIO_STREAM_QUEUE_SLOTS 8u
#define DESK_AUDIO_STREAM_MAX_BYTES (4u * 1024u * 1024u)
#define DESK_AUDIO_STREAM_START_BUFFER_FRAMES 4u

typedef enum {
    DESK_AUDIO_STREAM_IDLE = 0,
    DESK_AUDIO_STREAM_RECEIVING,
    DESK_AUDIO_STREAM_DRAINING,
    DESK_AUDIO_STREAM_CANCELING,
    DESK_AUDIO_STREAM_COMPLETE,
    DESK_AUDIO_STREAM_CANCELED,
    DESK_AUDIO_STREAM_FAILED,
} desk_audio_stream_state_e;

typedef struct {
    uint8_t data[DESK_AUDIO_STREAM_CHUNK_BYTES];
    uint16_t bytes;
    uint32_t sequence;
} desk_audio_stream_slot_t;

typedef struct {
    uint64_t handle;
    uint32_t connection_generation;
    uint32_t stream_id;
    uint32_t expected_rx_seq;
    uint32_t total_bytes;
    uint32_t accepted_bytes;
    uint32_t error_code;
    uint8_t queue_head;
    uint8_t queue_depth;
    bool playing;
    bool owner_held;
    bool cancel_requested;
    bool finish_requested;
    desk_audio_stream_state_e state;
    desk_audio_resampler_t resampler;
    desk_audio_stream_slot_t queue[DESK_AUDIO_STREAM_QUEUE_SLOTS];
} desk_audio_stream_ctx_t;

typedef struct {
    uint64_t handle;
    uint32_t connection_generation;
    uint32_t stream_id;
    uint32_t expected_rx_seq;
    uint32_t total_bytes;
    uint32_t accepted_bytes;
    uint32_t error_code;
    uint16_t free_frames;
    uint16_t pending_frames;
    uint16_t queue_depth;
    uint16_t start_buffer_frames;
    desk_audio_stream_state_e state;
    bool playing;
    bool tx_drained;
} desk_audio_stream_status_t;

/* Caller serializes all state operations with one mutex around the shared context. */
int desk_audio_stream_begin(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                            uint32_t connection_generation, uint32_t stream_id,
                            uint32_t sample_rate_hz, uint8_t channels,
                            uint8_t sample_width_bytes, uint32_t total_bytes,
                            bool capture_active);
int desk_audio_stream_write(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                            uint32_t connection_generation, uint32_t stream_id,
                            uint32_t sequence, const uint8_t *pcm, size_t bytes);
int desk_audio_stream_finish(desk_audio_stream_ctx_t *ctx, uint64_t handle);
int desk_audio_stream_cancel(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                             uint32_t reason_code);
int desk_audio_stream_next_output(desk_audio_stream_ctx_t *ctx, int16_t *output,
                                  size_t output_capacity, size_t *output_samples,
                                  bool *needs_static_confirmation);
int desk_audio_stream_write_complete(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                                     int write_result);
int desk_audio_stream_confirm_static(desk_audio_stream_ctx_t *ctx, uint64_t handle,
                                     bool static_confirmed);
int desk_audio_stream_get_status(const desk_audio_stream_ctx_t *ctx, uint64_t handle,
                                 desk_audio_stream_status_t *out_status);

#endif
