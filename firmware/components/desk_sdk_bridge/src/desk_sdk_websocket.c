/* Minimal RFC6455 client for the official Python SDK Runtime. */
#include "desk_sdk_bridge.h"
#include <errno.h>
#include <fcntl.h>
#ifdef ESP_PLATFORM
#include <sys/poll.h>
#else
#include <poll.h>
#endif
#include <stdio.h>
#include <string.h>
#include <strings.h>
#include <time.h>
#include <sys/time.h>
#include <pthread.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>

#if !defined(ESP_PLATFORM) && defined(DESK_SDK_OPENSSL_TESTING)
#include <openssl/evp.h>
#include <openssl/sha.h>
#endif

#ifdef ESP_PLATFORM
#include "esp_random.h"
#include "esp_attr.h"
#include "tal_api.h"
#include "tal_hash.h"
#include "mix_method.h"
#define DESK_WS_BSS EXT_RAM_BSS_ATTR
#else
#define DESK_WS_BSS
#endif

#ifndef MSG_NOSIGNAL
#define MSG_NOSIGNAL 0
#endif

#define WS_RX_CAP  (DESK_WSPK_MAX_FRAME_SIZE + 14)
#define WS_HTTP_CAP 1536
#define WS_FRAME_CAP (DESK_WSPK_MAX_FRAME_SIZE + 1)
#define WS_GUID "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
#define WS_RECONNECT_BASE_DELAY_SEC 1U
#define WS_RECONNECT_MAX_DELAY_SEC 30U

static int s_ws_fd = -1;
static bool s_ws_ready = false;
/* A successful HardwareHello ACK observed on this socket, not cryptographic
 * peer authentication (the current transport is plaintext WebSocket). */
static bool s_ws_authenticated = false;
/* Serializes only the ACK latch and its generation. It is never held while
 * taking the pairing mutex or doing network I/O. */
static pthread_mutex_t s_ws_lifecycle_mutex = PTHREAD_MUTEX_INITIALIZER;
static uint32_t s_ws_generation = 0;
static desk_sdk_ws_state_t s_ws_state = DESK_SDK_WS_OFFLINE;
#ifndef ESP_PLATFORM
static uint32_t s_mask_counter = 0;
#endif
static DESK_WS_BSS uint8_t s_ws_rx[WS_RX_CAP];
static DESK_WS_BSS uint8_t s_ws_payload[WS_FRAME_CAP];
static DESK_WS_BSS uint8_t s_ws_ack[1024];
static DESK_WS_BSS char s_ws_response[DESK_SDK_MAX_FRAME_LEN + 1];
static DESK_WS_BSS char s_ws_http_response[WS_HTTP_CAP];
static size_t s_ws_rx_len = 0;
static bool s_pairing_target_valid = false;
static bool s_reconnect_attempt = false;
static unsigned s_reconnect_failures = 0;
static time_t s_ws_retry_at = 0;

static int ws_wait(int fd, short events, int timeout_ms)
{
    struct pollfd p = { .fd = fd, .events = events, .revents = 0 };
    int rc;
    do { rc = poll(&p, 1, timeout_ms); } while (rc < 0 && errno == EINTR);
    return rc > 0 && (p.revents & events) ? 0 : -1;
}

static int ws_send_all(int fd, const uint8_t *p, size_t n)
{
    size_t off = 0;
    while (off < n) {
        ssize_t sent = send(fd, p + off, n - off, MSG_NOSIGNAL);
        if (sent > 0) { off += (size_t)sent; continue; }
        if (sent < 0 && errno == EINTR) continue;
        if (sent < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) && ws_wait(fd, POLLOUT, 1000) == 0) continue;
        return -1;
    }
    return 0;
}

static void ws_random(uint8_t *out, size_t n)
{
#ifdef ESP_PLATFORM
    esp_fill_random(out, n);
#else
    uint32_t x = (uint32_t)getpid() ^ (uint32_t)time(NULL) ^ ++s_mask_counter;
    for (size_t i = 0; i < n; ++i) { x ^= x << 13; x ^= x >> 17; x ^= x << 5; out[i] = (uint8_t)x; }
#endif
}

static void ws_schedule_reconnect(void)
{
    unsigned delay = WS_RECONNECT_BASE_DELAY_SEC;
    unsigned shift = s_reconnect_failures < 5 ? s_reconnect_failures : 5;
    delay <<= shift;
    if (delay > WS_RECONNECT_MAX_DELAY_SEC) delay = WS_RECONNECT_MAX_DELAY_SEC;
    if (s_reconnect_failures < 31) ++s_reconnect_failures;
    s_ws_retry_at = time(NULL) + delay;
}

static void ws_transport_close(void)
{
    pthread_mutex_lock(&s_ws_lifecycle_mutex);
    ++s_ws_generation;
    s_ws_ready = false;
    s_ws_authenticated = false;
    if (s_ws_fd >= 0) { close(s_ws_fd); s_ws_fd = -1; }
    s_ws_state = DESK_SDK_WS_OFFLINE;
    s_ws_rx_len = 0;
    pthread_mutex_unlock(&s_ws_lifecycle_mutex);
    desk_sdk_bridge_log_session_closed();
}

static uint32_t ws_lifecycle_generation(void)
{
    pthread_mutex_lock(&s_ws_lifecycle_mutex);
    uint32_t generation = s_ws_generation;
    pthread_mutex_unlock(&s_ws_lifecycle_mutex);
    return generation;
}

static bool ws_commit_hardware_hello_ack(uint32_t generation)
{
    pthread_mutex_lock(&s_ws_lifecycle_mutex);
    bool current = generation == s_ws_generation;
    if (current) {
        /* This records protocol ACK observation, not cryptographic peer auth. */
        s_ws_authenticated = true;
        s_ws_ready = true;
        s_ws_state = DESK_SDK_WS_CONNECTED;
    }
    pthread_mutex_unlock(&s_ws_lifecycle_mutex);
    return current;
}

static void ws_disconnect_and_schedule(void)
{
    ws_transport_close();
    if (s_pairing_target_valid) ws_schedule_reconnect();
}

static const char s_b64[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
static size_t ws_base64(const uint8_t *src, size_t len, char *dst, size_t cap)
{
    size_t need = ((len + 2) / 3) * 4;
    if (cap <= need) return 0;
    size_t i = 0, o = 0;
    while (i < len) {
        uint32_t v = (uint32_t)src[i++] << 16;
        bool b = i < len; if (b) v |= (uint32_t)src[i++] << 8;
        bool c = i < len; if (c) v |= src[i++];
        dst[o++] = s_b64[(v >> 18) & 63]; dst[o++] = s_b64[(v >> 12) & 63];
        dst[o++] = b ? s_b64[(v >> 6) & 63] : '='; dst[o++] = c ? s_b64[v & 63] : '=';
    }
    dst[o] = '\0'; return o;
}

#ifdef ESP_PLATFORM
static int ws_expected_accept(const char *key, char *out, size_t cap)
{
    char joined[128]; uint8_t digest[20];
    int n = snprintf(joined, sizeof(joined), "%s%s", key, WS_GUID);
    if (n <= 0 || (size_t)n >= sizeof(joined) || tal_sha1_ret((const uint8_t *)joined, (uint32_t)n, digest) != OPRT_OK) return -1;
    if (!tuya_base64_encode(digest, out, sizeof(digest)) || strlen(out) >= cap) return -1;
    return 0;
}
#elif defined(DESK_SDK_OPENSSL_TESTING)
static int ws_expected_accept(const char *key, char *out, size_t cap)
{
    char joined[128];
    uint8_t digest[SHA_DIGEST_LENGTH];
    if (!key || !out || cap < 29) return -1;
    int n = snprintf(joined, sizeof(joined), "%s%s", key, WS_GUID);
    if (n <= 0 || (size_t)n >= sizeof(joined)) return -1;
    if (!SHA1((const unsigned char *)joined, (size_t)n, digest)) return -1;
    int encoded = EVP_EncodeBlock((unsigned char *)out, digest, sizeof(digest));
    return encoded == 28 && (size_t)encoded < cap ? 0 : -1;
}
#else
static int ws_expected_accept(const char *key, char *out, size_t cap)
{
    /* Host builds exercise frame/state parsing; production crypto comes from Tuya TLS API. */
    (void)key; (void)out; (void)cap; return -1;
}
#endif

static bool ws_header_value(const char *headers, const char *name, const char *value)
{
    size_t nl = strlen(name), vl = strlen(value);
    for (const char *p = headers; *p; ++p) {
        if (p != headers && p[-1] != '\n') continue;
        if (strncasecmp(p, name, nl) != 0) continue;
        const char *v = p + nl; while (*v == ' ' || *v == '\t') ++v;
        if (strncasecmp(v, value, vl) == 0 && (v[vl] == '\r' || v[vl] == '\n' || v[vl] == '\0')) return true;
    }
    return false;
}

static int ws_send_frame(uint8_t opcode, const uint8_t *payload, size_t len)
{
    if (s_ws_fd < 0 || (!payload && len) || len > DESK_WSPK_MAX_PAYLOAD_SIZE + DESK_WSPK_HEADER_SIZE) return -1;
    uint8_t header[14], mask[4]; size_t h = 0;
    ws_random(mask, sizeof(mask)); header[h++] = (uint8_t)(0x80 | opcode);
    if (len < 126) header[h++] = (uint8_t)(0x80 | len);
    else if (len <= 65535) { header[h++] = 0x80 | 126; header[h++] = (uint8_t)(len >> 8); header[h++] = (uint8_t)len; }
    else { header[h++] = 0x80 | 127; uint64_t n = len; for (int i=7;i>=0;i--) header[h++] = (uint8_t)(n >> (i*8)); }
    memcpy(header + h, mask, sizeof(mask)); h += sizeof(mask);
    if (ws_send_all(s_ws_fd, header, h) != 0) return -1;
    uint8_t chunk[512]; size_t off = 0;
    while (off < len) {
        size_t n = len - off; if (n > sizeof(chunk)) n = sizeof(chunk);
        for (size_t i = 0; i < n; ++i) chunk[i] = payload[off + i] ^ mask[(off + i) & 3];
        if (ws_send_all(s_ws_fd, chunk, n) != 0) return -1;
        off += n;
    }
    return 0;
}

static int ws_read_frame(int timeout_ms, uint8_t *opcode, uint8_t *payload, size_t cap, size_t *out_len)
{
    if (s_ws_fd < 0 || !opcode || !payload || !out_len) return -1;
    for (;;) {
        if (s_ws_rx_len >= 2) {
            uint8_t b0 = s_ws_rx[0], b1 = s_ws_rx[1];
            uint8_t frame_opcode = b0 & 0x0f;
            size_t h = 2; uint64_t n = b1 & 0x7f; bool masked = (b1 & 0x80) != 0;
            if (n == 126) { if (s_ws_rx_len < h + 2) goto more; n = ((uint64_t)s_ws_rx[h] << 8) | s_ws_rx[h + 1]; h += 2; }
            else if (n == 127) { if (s_ws_rx_len < h + 8) goto more; n = 0; for (int i=0;i<8;i++) n=(n<<8)|s_ws_rx[h+i]; h+=8; }
            size_t frame_limit;
            if (frame_opcode == 0x1) frame_limit = DESK_SDK_MAX_FRAME_LEN;
            else if (frame_opcode == 0x2) frame_limit = DESK_WSPK_MAX_FRAME_SIZE;
            else if (frame_opcode == 0x8 || frame_opcode == 0x9 || frame_opcode == 0xA) frame_limit = 125;
            else return -1;
            if ((b0 & 0x80) == 0 || (b0 & 0x70) != 0 || masked || n > cap || n > frame_limit ||
                (n == 127 && (s_ws_rx[2] & 0x80) != 0)) return -1;
            if (frame_opcode >= 0x8 && (b0 & 0x80) == 0) return -1;
            if (s_ws_rx_len < h + (size_t)n) goto more;
            *opcode = b0 & 0x0f; *out_len = (size_t)n; memcpy(payload, s_ws_rx + h, (size_t)n);
            size_t used = h + (size_t)n; memmove(s_ws_rx, s_ws_rx + used, s_ws_rx_len - used); s_ws_rx_len -= used; return 0;
        }
more:
        if (ws_wait(s_ws_fd, POLLIN, timeout_ms) != 0) return 1;
        if (s_ws_rx_len == sizeof(s_ws_rx)) return -1;
        ssize_t r = recv(s_ws_fd, s_ws_rx + s_ws_rx_len, sizeof(s_ws_rx) - s_ws_rx_len, 0);
        if (r <= 0) return -1;
        s_ws_rx_len += (size_t)r;
    }
}

void desk_sdk_ws_client_close(void)
{
    ws_transport_close();
    s_pairing_target_valid = false;
    s_reconnect_failures = 0;
    s_ws_retry_at = 0;
}

bool desk_sdk_ws_client_is_connected(void)
{
    pthread_mutex_lock(&s_ws_lifecycle_mutex);
    bool ready = s_ws_ready;
    pthread_mutex_unlock(&s_ws_lifecycle_mutex);
    return ready;
}
bool desk_sdk_ws_client_is_authenticated(void)
{
    pthread_mutex_lock(&s_ws_lifecycle_mutex);
    bool authenticated = s_ws_ready && s_ws_authenticated;
    pthread_mutex_unlock(&s_ws_lifecycle_mutex);
    return authenticated;
}
desk_sdk_ws_state_t desk_sdk_ws_client_get_state(void) { return s_ws_state; }

int desk_sdk_ws_client_send_text(const char *text)
{
    if (!s_ws_ready || !text) return -1;
    if (ws_send_frame(0x1, (const uint8_t *)text, strlen(text)) != 0) { ws_disconnect_and_schedule(); return -1; }
    return 0;
}

int desk_sdk_ws_client_send_binary(const uint8_t *data, size_t len)
{
    if (!s_ws_ready || (!data && len)) return -1;
    if (len > DESK_WSPK_MAX_FRAME_SIZE) return -2;
    if (ws_send_frame(0x2, data, len) != 0) { ws_disconnect_and_schedule(); return -1; }
    return 0;
}

int desk_sdk_ws_client_connect_paired(void)
{
    char host[INET_ADDRSTRLEN], hello[512], key[32], expected[48];
    uint8_t key_raw[16];
    uint16_t port;
    ws_transport_close();
    uint32_t connect_generation = ws_lifecycle_generation();
    if (!s_reconnect_attempt) {
        s_reconnect_failures = 0;
        s_ws_retry_at = 0;
    }
    if (desk_sdk_bridge_get_pairing_target(host, sizeof(host), &port) != 0) goto fail;
    s_pairing_target_valid = true;
    if (desk_sdk_bridge_build_hardware_hello(hello, sizeof(hello)) < 0) goto fail;
    s_ws_state = DESK_SDK_WS_CONNECTING;
    ws_random(key_raw, sizeof(key_raw));
    if (!ws_base64(key_raw, sizeof(key_raw), key, sizeof(key)) || ws_expected_accept(key, expected, sizeof(expected)) != 0) goto fail;

    s_ws_fd = socket(AF_INET, SOCK_STREAM, 0);
    if (s_ws_fd < 0) goto fail;
    int flags = fcntl(s_ws_fd, F_GETFL, 0);
    if (flags < 0 || fcntl(s_ws_fd, F_SETFL, flags | O_NONBLOCK) < 0) goto fail;
    struct timeval tv = { .tv_sec = 3, .tv_usec = 0 };
    setsockopt(s_ws_fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(s_ws_fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    struct sockaddr_in addr; memset(&addr, 0, sizeof(addr)); addr.sin_family = AF_INET; addr.sin_port = htons(port);
    if (inet_pton(AF_INET, host, &addr.sin_addr) != 1) goto fail;
    int crc = connect(s_ws_fd, (struct sockaddr *)&addr, sizeof(addr));
    if (crc != 0 && errno != EINPROGRESS) goto fail;
    if (crc != 0) {
        int so_error = 0; socklen_t so_len = sizeof(so_error);
        if (ws_wait(s_ws_fd, POLLOUT, 3000) != 0 || getsockopt(s_ws_fd, SOL_SOCKET, SO_ERROR, &so_error, &so_len) != 0 || so_error != 0) goto fail;
    }
    s_ws_state = DESK_SDK_WS_HANDSHAKE;
    char request[512];
    int q = snprintf(request, sizeof(request), "GET / HTTP/1.1\r\nHost: %s:%u\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n", host, port, key);
    if (q <= 0 || (size_t)q >= sizeof(request) || ws_send_all(s_ws_fd, (const uint8_t *)request, (size_t)q) != 0) goto fail;
    char *response = s_ws_http_response; size_t used = 0; bool end = false;
    while (used + 1 < WS_HTTP_CAP) {
        if (ws_wait(s_ws_fd, POLLIN, 3000) != 0) goto fail;
        ssize_t n = recv(s_ws_fd, response + used, WS_HTTP_CAP - used - 1, 0); if (n <= 0) goto fail;
        used += (size_t)n; response[used] = '\0';
        if (strstr(response, "\r\n\r\n")) { end = true; break; }
    }
    if (!end || strncmp(response, "HTTP/1.1 101 ", 13) != 0 ||
        !ws_header_value(response, "Upgrade:", "websocket") ||
        !ws_header_value(response, "Connection:", "Upgrade") ||
        !ws_header_value(response, "Sec-WebSocket-Accept:", expected)) goto fail;
    const char *tail = strstr(response, "\r\n\r\n") + 4; size_t tail_len = used - (size_t)(tail - response);
    if (tail_len > sizeof(s_ws_rx)) goto fail;
    if (tail_len) { memcpy(s_ws_rx, tail, tail_len); s_ws_rx_len = tail_len; }
    if (ws_send_frame(0x1, (const uint8_t *)hello, strlen(hello)) != 0) goto fail;
    s_ws_state = DESK_SDK_WS_HELLO_SENT;
    uint8_t opcode; size_t ack_len;
    if (ws_read_frame(3000, &opcode, s_ws_ack, sizeof(s_ws_ack)-1, &ack_len) != 0 || opcode != 0x1) goto fail;
    s_ws_ack[ack_len] = 0;
    if (!strstr((char *)s_ws_ack, "\"type\":\"sys.ack\"") || !strstr((char *)s_ws_ack, "\"sys.client.hello\"") || !strstr((char *)s_ws_ack, "\"code\":0")) goto fail;
    /* Commit only if no close/credential/policy/identity invalidation happened
     * while this connection was in flight. This remains protocol ACK evidence,
     * not cryptographic Runtime peer authentication. */
    if (!ws_commit_hardware_hello_ack(connect_generation)) goto fail;
    s_reconnect_failures = 0;
    s_ws_retry_at = 0;
    return 0;
fail:
    ws_transport_close();
    if (s_pairing_target_valid) ws_schedule_reconnect();
    return -1;
}

int desk_sdk_ws_client_poll(int timeout_ms)
{
    if (!s_ws_ready) {
        if (s_pairing_target_valid && s_ws_retry_at != 0 && time(NULL) >= s_ws_retry_at) {
            s_reconnect_attempt = true;
            (void)desk_sdk_ws_client_connect_paired();
            s_reconnect_attempt = false;
        }
        return 0;
    }
    uint8_t op; uint8_t *payload = s_ws_payload; size_t len = 0;
    int rc = ws_read_frame(timeout_ms, &op, payload, WS_FRAME_CAP-1, &len);
    if (rc == 1) return 0;
    if (rc != 0) { ws_disconnect_and_schedule(); return -1; }
    if (op == 0x8) { (void)ws_send_frame(0x8, payload, len); ws_disconnect_and_schedule(); return 0; }
    if (op == 0x9) { if (ws_send_frame(0xA, payload, len) != 0) { ws_disconnect_and_schedule(); return -1; } return 0; }
    if (op == 0xA) return 0;
    if (op == 0x2) {
        desk_wspk_header_t media_header;
        if (len < DESK_WSPK_HEADER_SIZE || desk_wspk_unpack_header(payload, len, &media_header) != 0 ||
            media_header.payload_size != len - DESK_WSPK_HEADER_SIZE) {
            ws_disconnect_and_schedule();
            return -1;
        }
        /* Binary media ingress is validated here; no application ingress callback exists yet. */
        return 0;
    }
    if (op != 0x1) { ws_disconnect_and_schedule(); return -1; }
    if (memchr(payload, '\0', len) != NULL) { ws_disconnect_and_schedule(); return -1; }
    payload[len] = 0;
    char message_type[64] = "";
    if (json_parse_top_level_str((const char *)payload, "type", message_type, sizeof(message_type)) &&
        strcmp(message_type, "sys.client.hello") == 0) { ws_disconnect_and_schedule(); return -1; }
    if (desk_sdk_bridge_process_ws_command((const char *)payload, s_ws_response, sizeof(s_ws_response)) == 0)
        return desk_sdk_ws_client_send_text(s_ws_response);
    return 0;
}
