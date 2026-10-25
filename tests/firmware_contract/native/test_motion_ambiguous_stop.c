#include <assert.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "desk_hardware.h"
#include "desk_watcher_motion.h"
#include "driver/uart.h"
#include "freertos/semphr.h"
#include "freertos/task.h"

/* Count HAL release calls while still using the production arbiter logic. */
#define desk_arbiter_release desk_arbiter_release_impl
#include "../../../firmware/components/desk_hardware/src/desk_arbiter.c"
#undef desk_arbiter_release

static unsigned s_release_calls;
void desk_arbiter_release(desk_resource_type_t resource, desk_resource_owner_t owner)
{
    ++s_release_calls;
    desk_arbiter_release_impl(resource, owner);
}

typedef enum {
    TX_FULL_NO_RESPONSE,
    TX_ZERO,
    TX_PARTIAL,
    TX_NEGATIVE_WRITE,
    TX_MOVE_NACK,
    TX_MOVE_ACK,
    TX_WRONG_SEQ,
    TX_BAD_CRC,
    TX_NO_MOTION_CAPABILITY,
    TX_NO_HELLO,
} tx_mode_t;

static tx_mode_t s_mode;
static bool s_uart_installed;
static int64_t s_now_us;
static uint8_t s_response[32];
static size_t s_response_len;
static size_t s_response_offset;
static uint8_t s_frames[12][64];
static size_t s_frame_lens[12];
static unsigned s_write_calls;
static unsigned s_hello_write_calls;
static unsigned s_stop_owner_checks;

static uint16_t crc16(const uint8_t *data, size_t len)
{
    uint16_t crc = 0xFFFF;
    for (size_t i = 0; i < len; ++i) {
        crc ^= (uint16_t)data[i] << 8;
        for (unsigned bit = 0; bit < 8; ++bit) {
            crc = (crc & 0x8000) ? (uint16_t)((crc << 1) ^ 0x1021)
                                 : (uint16_t)(crc << 1);
        }
    }
    return crc;
}

static size_t cobs_encode(const uint8_t *src, size_t len, uint8_t *dst, size_t cap)
{
    size_t read = 0, write = 1, code_at = 0;
    uint8_t code = 1;
    if (cap < len + len / 254 + 1) return 0;
    while (read < len) {
        if (src[read] == 0) {
            dst[code_at] = code;
            code_at = write++;
            code = 1;
        } else {
            dst[write++] = src[read];
            if (++code == 0xFF) {
                dst[code_at] = code;
                code_at = write++;
                code = 1;
            }
        }
        ++read;
    }
    dst[code_at] = code;
    return write;
}

static size_t cobs_decode(const uint8_t *src, size_t len, uint8_t *dst, size_t cap)
{
    size_t read = 0, write = 0;
    while (read < len) {
        uint8_t code = src[read++];
        if (code == 0 || read + code - 1 > len) return 0;
        for (uint8_t i = 1; i < code; ++i) {
            if (write >= cap) return 0;
            dst[write++] = src[read++];
        }
        if (code != 0xFF && read < len) {
            if (write >= cap) return 0;
            dst[write++] = 0;
        }
    }
    return write;
}

static void make_response(uint8_t msg_id, uint32_t ref_seq,
                          const uint8_t *payload, uint16_t payload_len)
{
    uint8_t raw[32] = {0};
    raw[0] = 0xA5;
    raw[1] = 0x5A;
    raw[2] = 1;
    raw[3] = 1; /* official SYS ACK/NACK */
    raw[4] = msg_id;
    raw[5] = 2; /* RESPONSE */
    raw[6] = 0xAA; /* header seq is deliberately independent of ref_seq */
    raw[7] = 0xBB;
    raw[8] = 0xCC;
    raw[9] = 0xDD;
    raw[10] = (uint8_t)payload_len;
    raw[11] = (uint8_t)(payload_len >> 8);
    memcpy(raw + 12, payload, payload_len);
    uint16_t crc = crc16(raw, 12 + payload_len);
    raw[12 + payload_len] = (uint8_t)crc;
    raw[13 + payload_len] = (uint8_t)(crc >> 8);
    size_t encoded = cobs_encode(raw, 14 + payload_len, s_response, sizeof(s_response) - 1);
    assert(encoded > 0 && encoded < sizeof(s_response));
    s_response[encoded] = 0;
    s_response_len = encoded + 1;
    s_response_offset = 0;
    (void)ref_seq;
}

bool uart_is_driver_installed(int port) { (void)port; return s_uart_installed; }
esp_err_t uart_param_config(int port, const uart_config_t *config)
{ (void)port; (void)config; return ESP_OK; }
esp_err_t uart_set_pin(int port, int tx, int rx, int rts, int cts)
{ (void)port; (void)tx; (void)rx; (void)rts; (void)cts; return ESP_OK; }
esp_err_t uart_driver_install(int port, int rx_buf, int tx_buf, int queue_size,
                              void *queue, int flags)
{
    (void)port; (void)rx_buf; (void)tx_buf; (void)queue_size; (void)queue; (void)flags;
    s_uart_installed = true;
    return ESP_OK;
}
esp_err_t uart_driver_delete(int port) { (void)port; s_uart_installed = false; return ESP_OK; }
void uart_flush_input(int port) { (void)port; }
int uart_write_bytes(int port, const char *data, size_t len)
{
    (void)port;
    uint8_t raw[64];
    size_t raw_len = cobs_decode((const uint8_t *)data, len - 1, raw, sizeof(raw));
    assert(len > 1 && ((const uint8_t *)data)[len - 1] == 0 && raw_len >= 14);
    if (raw[3] == 1 && raw[4] == 1) { /* HELLO probe */
        ++s_hello_write_calls;
        if (s_mode == TX_NO_HELLO) return (int)len;
        const uint8_t hello[] = {0, 1, 0, 0, 1,
                                 (uint8_t)(s_mode == TX_NO_MOTION_CAPABILITY ? 0 : 1),
                                 0, 0, 1};
        uint32_t req_seq = (uint32_t)raw[6] | ((uint32_t)raw[7] << 8) |
                           ((uint32_t)raw[8] << 16) | ((uint32_t)raw[9] << 24);
        make_response(2, req_seq, hello, sizeof(hello));
        return (int)len;
    }
    assert(s_write_calls < 12);
    memcpy(s_frames[s_write_calls], data, len);
    s_frame_lens[s_write_calls] = len;
    unsigned index = s_write_calls++;
    if (raw[4] == MCU_MOTION_MSG_SERVO_STOP) {
        assert(desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL) ==
               DESK_OWNER_SDK_REMOTE);
        ++s_stop_owner_checks;
    }
    uint32_t seq = (uint32_t)raw[6] | ((uint32_t)raw[7] << 8) |
                   ((uint32_t)raw[8] << 16) | ((uint32_t)raw[9] << 24);
    if (index == 0 && s_mode == TX_MOVE_NACK) {
        uint8_t payload[8] = {(uint8_t)seq, (uint8_t)(seq >> 8), (uint8_t)(seq >> 16), (uint8_t)(seq >> 24), 0, 0, 3, 0};
        make_response(MCU_SYS_MSG_NACK, seq, payload, sizeof(payload));
    } else if (index == 0 && (s_mode == TX_MOVE_ACK || s_mode == TX_WRONG_SEQ || s_mode == TX_BAD_CRC)) {
        uint32_t ref = s_mode == TX_WRONG_SEQ ? seq + 1U : seq;
        uint8_t payload[6] = {(uint8_t)ref, (uint8_t)(ref >> 8), (uint8_t)(ref >> 16), (uint8_t)(ref >> 24), 0, 0};
        make_response(MCU_SYS_MSG_ACK, ref, payload, sizeof(payload));
        if (s_mode == TX_BAD_CRC) s_response[s_response_len - 2] ^= 1U;
    }
    if (s_mode == TX_ZERO && index == 0) return 0;
    if (s_mode == TX_PARTIAL && index == 0) return len > 1 ? (int)len - 1 : 1;
    if (s_mode == TX_NEGATIVE_WRITE && index == 0) return -1;
    return (int)len;
}
int uart_read_bytes(int port, uint8_t *data, size_t len, uint32_t timeout_ticks)
{
    (void)port; (void)timeout_ticks;
    if (s_response_offset < s_response_len) {
        *data = s_response[s_response_offset++];
        return 1;
    }
    (void)len;
    s_now_us += 10000;
    return 0;
}

static int s_mutex_token;
SemaphoreHandle_t xSemaphoreCreateMutex(void) { return &s_mutex_token; }
BaseType_t xSemaphoreTake(SemaphoreHandle_t semaphore, TickType_t ticks)
{ (void)semaphore; (void)ticks; return pdTRUE; }
BaseType_t xSemaphoreGive(SemaphoreHandle_t semaphore)
{ (void)semaphore; return pdTRUE; }
void vSemaphoreDelete(SemaphoreHandle_t semaphore) { (void)semaphore; }
void vTaskDelay(TickType_t ticks) { s_now_us += (int64_t)ticks * 1000; }
int64_t esp_timer_get_time(void) { return s_now_us; }

static int board_move(float pan, float tilt, uint32_t duration_ms)
{
    return desk_watcher_motion_move(pan, tilt, duration_ms);
}

static const desk_board_driver_ops_t s_ops = {
    .driver_name = "native_esp_motion_harness",
    .motion_move = board_move,
};

static void reset_transport(tx_mode_t mode)
{
    s_mode = mode;
    s_now_us = 0;
    s_response_len = 0;
    s_response_offset = 0;
    s_write_calls = 0;
    s_hello_write_calls = 0;
    s_stop_owner_checks = 0;
    memset(s_frames, 0, sizeof(s_frames));
    memset(s_frame_lens, 0, sizeof(s_frame_lens));
}

static uint32_t frame_seq(unsigned index)
{
    uint8_t raw[64];
    size_t n = cobs_decode(s_frames[index], s_frame_lens[index] - 1, raw, sizeof(raw));
    assert(n >= 14);
    return (uint32_t)raw[6] | ((uint32_t)raw[7] << 8) |
           ((uint32_t)raw[8] << 16) | ((uint32_t)raw[9] << 24);
}

static uint8_t frame_msg_id(unsigned index)
{
    uint8_t raw[64];
    size_t n = cobs_decode(s_frames[index], s_frame_lens[index] - 1, raw, sizeof(raw));
    assert(n >= 14);
    return raw[4];
}

static void assert_move_payload(unsigned index)
{
    uint8_t raw[64];
    size_t n = cobs_decode(s_frames[index], s_frame_lens[index] - 1, raw, sizeof(raw));
    assert(n == 23 && raw[0] == 0xA5 && raw[1] == 0x5A);
    assert(raw[3] == 2 && raw[4] == 1 && raw[5] == 1);
    assert(raw[10] == 9 && raw[11] == 0 && raw[12] == 3);
    assert(raw[13] == 0x84 && raw[14] == 0x03 && raw[15] == 0xB0 && raw[16] == 0x04);
    assert(raw[17] == 250 && raw[18] == 0 && raw[19] == 1 && raw[20] == 0);
    assert(((uint16_t)raw[21] | ((uint16_t)raw[22] << 8)) == crc16(raw, 21));
}

static void assert_stop_payload(unsigned index)
{
    uint8_t raw[64];
    size_t n = cobs_decode(s_frames[index], s_frame_lens[index] - 1, raw, sizeof(raw));
    assert(n == 16 && raw[3] == 2 && raw[4] == 2 && raw[5] == 1);
    assert(raw[10] == 2 && raw[11] == 0 && raw[12] == 1 && raw[13] == 0);
    assert(((uint16_t)raw[14] | ((uint16_t)raw[15] << 8)) == crc16(raw, 14));
}

static void begin_case(tx_mode_t mode)
{
    desk_hardware_init();
    assert(desk_watcher_motion_init() == DESK_HW_OK);
    assert(desk_hardware_register_driver_ops(&s_ops) == DESK_HW_OK);
    reset_transport(mode);
    desk_watcher_motion_peer_info_t peer;
    int probe_rc = desk_watcher_motion_probe(&peer, 20);
    assert(probe_rc == (mode == TX_NO_HELLO ? DESK_HW_ERR_TIMEOUT : DESK_HW_OK));
    if (probe_rc == DESK_HW_OK)
        assert((peer.capability_bitmap & 1u) == (mode == TX_NO_MOTION_CAPABILITY ? 0u : 1u));
    s_release_calls = 0;
}

static void assert_single_hal_release(void)
{
    assert(s_release_calls == 1);
    assert(desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL) == DESK_OWNER_NONE);
}

int main(void)
{
    desk_watcher_motion_diagnostics_t before;
    desk_watcher_motion_diagnostics_t after;
    /* A product command must not need a prior diagnostic CLI probe. */
    desk_hardware_init();
    assert(desk_watcher_motion_init() == DESK_HW_OK);
    assert(desk_hardware_register_driver_ops(&s_ops) == DESK_HW_OK);
    reset_transport(TX_MOVE_ACK);
    assert(desk_watcher_motion_move(-1.0f, 120.0f, 250) == DESK_HW_ERR_INVALID_ARG);
    assert(s_hello_write_calls == 0 && s_write_calls == 0);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_OK);
    assert(s_hello_write_calls == 1 && s_write_calls == 1);

    /* A known capable peer is reused until a command fails. */
    reset_transport(TX_MOVE_ACK);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_OK);
    assert(s_hello_write_calls == 0 && s_write_calls == 1);
    reset_transport(TX_FULL_NO_RESPONSE);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_TIMEOUT);
    reset_transport(TX_MOVE_ACK);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_OK);
    assert(s_hello_write_calls == 1 && s_write_calls == 1);

    /* No HELLO or no motion capability must still prohibit motion writes. */
    assert(desk_watcher_motion_deinit() == DESK_HW_OK);
    assert(desk_watcher_motion_init() == DESK_HW_OK);
    reset_transport(TX_NO_MOTION_CAPABILITY);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_UNAVAILABLE);
    assert(s_hello_write_calls == 1 && s_write_calls == 0);
    reset_transport(TX_NO_HELLO);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_UNAVAILABLE);
    assert(s_hello_write_calls == 1 && s_write_calls == 0);

    begin_case(TX_NO_MOTION_CAPABILITY);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_UNAVAILABLE);
    assert(s_write_calls == 0);
    assert_single_hal_release();
    begin_case(TX_NO_HELLO);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_UNAVAILABLE);
    assert(s_write_calls == 0);
    assert_single_hal_release();
    /* A provable zero-byte write is the sole transport-error no-STOP case. */
    begin_case(TX_ZERO);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_IO);
    assert(s_write_calls == 1);
    assert(s_stop_owner_checks == 0);
    assert(desk_watcher_motion_get_last_ambiguous_move_stop_result() == DESK_HW_ERR_UNAVAILABLE);
    assert_single_hal_release();

    /* Partial MOVE write is ambiguous: preserve MOVE IO error, emit one new-seq
     * STOP only once, do not retry STOP, and retain its timeout separately. */
    begin_case(TX_PARTIAL);
    desk_watcher_motion_get_diagnostics(&before);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_IO);
    assert(s_write_calls == 2);
    assert(s_stop_owner_checks == 1);
    assert(frame_msg_id(0) == 0x01 && frame_msg_id(1) == 0x02);
    assert(frame_seq(1) != frame_seq(0));
    assert(desk_watcher_motion_get_last_ambiguous_move_stop_result() == DESK_HW_ERR_TIMEOUT);
    desk_watcher_motion_get_diagnostics(&after);
    assert(after.tx_bytes - before.tx_bytes == (s_frame_lens[0] - 1U) + s_frame_lens[1]);
    assert(after.ack_count == before.ack_count);
    assert_single_hal_release();

    /* A negative/indeterminate UART write is not proof of zero-byte delivery:
     * preserve MOVE IO and issue one new-sequence STOP while owner is held. */
    begin_case(TX_NEGATIVE_WRITE);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_IO);
    assert(s_write_calls == 2);
    assert(frame_msg_id(0) == MCU_MOTION_MSG_SERVO_MOVE &&
           frame_msg_id(1) == MCU_MOTION_MSG_SERVO_STOP);
    assert(frame_seq(1) != frame_seq(0));
    assert(s_stop_owner_checks == 1);
    assert(desk_watcher_motion_get_last_ambiguous_move_stop_result() == DESK_HW_ERR_TIMEOUT);
    assert_single_hal_release();

    /* Full MOVE timeout retries MOVE with same sequence, then performs exactly
     * one STOP attempt with a new sequence and preserves the original timeout. */
    begin_case(TX_FULL_NO_RESPONSE);
    desk_watcher_motion_get_diagnostics(&before);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_TIMEOUT);
    assert(s_write_calls == 4);
    assert_move_payload(0);
    assert_move_payload(1);
    assert_move_payload(2);
    assert_stop_payload(3);
    assert(s_stop_owner_checks == 1);
    assert(frame_msg_id(0) == 0x01 && frame_msg_id(1) == 0x01 && frame_msg_id(2) == 0x01);
    assert(frame_seq(0) == frame_seq(1) && frame_seq(1) == frame_seq(2));
    assert(frame_msg_id(3) == 0x02 && frame_seq(3) != frame_seq(2));
    assert(desk_watcher_motion_get_last_ambiguous_move_stop_result() == DESK_HW_ERR_TIMEOUT);
    desk_watcher_motion_get_diagnostics(&after);
    assert(after.tx_bytes - before.tx_bytes == s_frame_lens[0] + s_frame_lens[1] + s_frame_lens[2] + s_frame_lens[3]);
    assert(after.rx_bytes == before.rx_bytes); /* no bytes received; never synthesize an ACK */
    assert(after.retry_count - before.retry_count == 2);
    assert(after.ack_count == before.ack_count);
    assert(after.timeout_count - before.timeout_count == 2); /* MOVE and best-effort STOP */
    assert(after.last_error == DESK_WATCHER_MOTION_ERROR_ACK_TIMEOUT);
    assert_single_hal_release();

    /* A correlated NACK remains ambiguous; append one STOP. */
    begin_case(TX_MOVE_NACK);
    desk_watcher_motion_get_diagnostics(&before);
    int nack_rc = desk_hardware_motion_move_to(90.0f, 120.0f, 250);
    assert(nack_rc == DESK_HW_ERR_IO);
    assert(s_write_calls == 2 && frame_msg_id(1) == 0x02);
    assert(s_stop_owner_checks == 1);
    assert(desk_watcher_motion_get_last_ambiguous_move_stop_result() == DESK_HW_ERR_TIMEOUT);
    desk_watcher_motion_get_diagnostics(&after);
    assert(after.nack_count - before.nack_count == 1);
    assert(after.ack_count == before.ack_count);
    assert_single_hal_release();

    /* A valid correlated ACK is accepted and does not provoke STOP. */
    begin_case(TX_MOVE_ACK);
    desk_watcher_motion_get_diagnostics(&before);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_OK);
    assert(s_write_calls == 1 && frame_msg_id(0) == 0x01);
    assert_move_payload(0);
    assert(s_stop_owner_checks == 0);
    assert(desk_watcher_motion_get_last_ambiguous_move_stop_result() == DESK_HW_ERR_UNAVAILABLE);
    desk_watcher_motion_get_diagnostics(&after);
    assert(after.tx_bytes - before.tx_bytes == s_frame_lens[0]);
    assert(after.rx_bytes - before.rx_bytes == s_response_len);
    assert(after.ack_count - before.ack_count == 1);
    assert(after.last_error == DESK_WATCHER_MOTION_ERROR_NONE);
    assert_single_hal_release();

    /* A CRC-valid ACK with an unrelated sequence is counted as RX evidence,
     * retried with the same request sequence, and never accepted as an ACK. */
    begin_case(TX_WRONG_SEQ);
    desk_watcher_motion_get_diagnostics(&before);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_TIMEOUT);
    desk_watcher_motion_get_diagnostics(&after);
    assert(after.ack_count == before.ack_count);
    assert(after.wrong_sequence_count - before.wrong_sequence_count >= 1);
    assert(after.retry_count - before.retry_count == 2);
    assert(after.last_error == DESK_WATCHER_MOTION_ERROR_ACK_TIMEOUT);
    assert(frame_msg_id(0) == MCU_MOTION_MSG_SERVO_MOVE && frame_seq(0) == frame_seq(1) && frame_seq(1) == frame_seq(2));
    assert_single_hal_release();

    /* A complete response with a bad CRC increments only the CRC diagnostic,
     * remains unaccepted, and falls through the ordinary retry/timeout path. */
    begin_case(TX_BAD_CRC);
    desk_watcher_motion_get_diagnostics(&before);
    assert(desk_hardware_motion_move_to(90.0f, 120.0f, 250) == DESK_HW_ERR_TIMEOUT);
    desk_watcher_motion_get_diagnostics(&after);
    assert(after.ack_count == before.ack_count);
    assert(after.bad_crc_count - before.bad_crc_count >= 1);
    assert(after.retry_count - before.retry_count == 2);
    assert_single_hal_release();

    puts("ambiguous STOP native ESP transaction harness: PASS");
    return 0;
}
