/**
 * @file desk_watcher_camera.c
 * @brief SenseCAP Watcher Himax HX6538 SPI2 / PTL Camera Driver Implementation.
 *
 * Hardware interface:
 *   - SPI2_HOST: SCLK (GPIO 4), MOSI (GPIO 5), MISO (GPIO 6), CS (GPIO 21), 12 MHz, Mode 0.
 *   - SYNC: PCA9535 expander Port 0 bit 6 (Pin 6).
 *   - HX Power: PCA9535 expander Port 1 bit 3 (Pin 11).
 */

#include "desk_watcher_camera.h"
#include "desk_watcher_board.h"
#include <stdio.h>
#include <string.h>

#ifdef ESP_PLATFORM
#include "driver/spi_master.h"
#include "driver/gpio.h"
#include "driver/uart.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "freertos/semphr.h"
#include "esp_log.h"
#include "esp_timer.h"
#endif

#define TAG "desk_watcher_camera"

#define WATCHER_CAM_SPI_HOST      SPI2_HOST
#define WATCHER_CAM_PIN_SCLK      GPIO_NUM_4
#define WATCHER_CAM_PIN_MOSI      GPIO_NUM_5
#define WATCHER_CAM_PIN_MISO      GPIO_NUM_6
#define WATCHER_CAM_PIN_CS        GPIO_NUM_21
#define WATCHER_CAM_SPI_HZ        (12 * 1000 * 1000)
#define WATCHER_CAM_HX_UART       UART_NUM_1
#define WATCHER_CAM_HX_UART_TX    GPIO_NUM_17
#define WATCHER_CAM_HX_UART_RX    GPIO_NUM_18
#define WATCHER_CAM_HX_UART_BAUD  921600

/* PCA9535 registers and bitmasks */
#define PCA_REG_INPUT0            0x00
#define PCA_REG_INPUT1            0x01
#define PCA_REG_OUTPUT1           0x03
#define PCA_REG_CONFIG0           0x06
#define PCA_REG_CONFIG1           0x07

#define PCA_PIN_SYNC_PORT0_MASK   (1u << 6) /* Port 0, bit 6 (Pin 6) */
#define PCA_PIN_HX_PWR_PORT1_MASK (1u << 3) /* Port 1, bit 3 (Pin 11) */

#define CAM_SYNC_TIMEOUT_MS       300
#define CAM_HX_POWER_SETTLE_MS    20
#define CAM_SYNC_GUARD_MS         2
#define CAM_CONTROL_TIMEOUT_MS    700U
#define CAM_CONTROL_RETRIES       3U
#define CAM_CONTROL_MODE_TIMEOUT_MS 3500U
#define CAM_PTL_EMPTY_RETRY_LIMIT 3U
#define CAM_PTL_EMPTY_BACKOFF_MS  5U
#define CAM_PTL_RESYNC_TIMEOUT_MS 50U
#define CAM_PTL_RESYNC_CHUNK_SIZE 16U
#define CAM_PTL_RESYNC_MAX_CHUNKS 256U

/* PTL and Media Protocol definitions */
#define HX_PTL_BASE_HEADER_SIZE   7U
#define HX_PTL_MAGIC_0            0xC0U
#define HX_PTL_MAGIC_1            0x5AU
#define HX_PTL_DATA_TYPE_JPEG     0x01U

#define HX_MEDIA_HEADER_SIZE      16U
#define HX_MEDIA_MAGIC_0          0x57U /* 'W' */
#define HX_MEDIA_MAGIC_1          0x52U /* 'R' */
#define HX_MEDIA_VERSION          1U

#define WATCHER_HX_PTL_MAX_JPEG_SIZE (128U * 1024U)
#define WATCHER_HX_PTL_BODY_CAPACITY (HX_MEDIA_HEADER_SIZE + WATCHER_HX_PTL_MAX_JPEG_SIZE)
/* spi_bus_config_t.max_transfer_sz limits each SPI transaction, including the
 * PTL body read. The old 64 KiB limit was smaller than the accepted 128 KiB
 * JPEG body, so otherwise valid larger frames failed at the SPI driver. */
#define CAM_MAX_PACKET_SZ         WATCHER_HX_PTL_BODY_CAPACITY

#ifdef ESP_PLATFORM
#include "esp_attr.h"
#include "esp_memory_utils.h"
static EXT_RAM_BSS_ATTR uint8_t s_ptl_body[WATCHER_HX_PTL_BODY_CAPACITY];
#else
static uint8_t s_ptl_body[WATCHER_HX_PTL_BODY_CAPACITY];
#endif

static bool s_camera_initialized = false;
static desk_watcher_camera_resync_t s_ptl_scanner;
static bool s_ptl_resynchronizing = false;

#ifdef ESP_PLATFORM
static spi_device_handle_t s_spi_dev = NULL;
static SemaphoreHandle_t s_cam_mutex = NULL;
static bool s_spi_bus_owned = false;
static bool s_hx_uart_owned = false;
static uint32_t s_hx_request_id = 0;
#endif

/* Forward declarations of expander helpers from desk_watcher_board */
extern int desk_watcher_board_pca_read(uint8_t reg, uint8_t *value);
extern int desk_watcher_board_pca_write(uint8_t reg, uint8_t value);

static int camera_power_enable(bool enable)
{
    uint8_t out1 = 0, cfg1 = 0;
    if (desk_watcher_board_pca_read(PCA_REG_OUTPUT1, &out1) != DESK_HW_OK ||
        desk_watcher_board_pca_read(PCA_REG_CONFIG1, &cfg1) != DESK_HW_OK) {
        return DESK_HW_ERR_IO;
    }

    uint8_t next_out = enable ? (uint8_t)(out1 | PCA_PIN_HX_PWR_PORT1_MASK)
                              : (uint8_t)(out1 & ~PCA_PIN_HX_PWR_PORT1_MASK);
    uint8_t next_cfg = (uint8_t)(cfg1 & ~PCA_PIN_HX_PWR_PORT1_MASK); /* Output mode */

    if (next_out != out1) {
        if (desk_watcher_board_pca_write(PCA_REG_OUTPUT1, next_out) != DESK_HW_OK) {
            return DESK_HW_ERR_IO;
        }
    }
    if (next_cfg != cfg1) {
        if (desk_watcher_board_pca_write(PCA_REG_CONFIG1, next_cfg) != DESK_HW_OK) {
            return DESK_HW_ERR_IO;
        }
    }
    return DESK_HW_OK;
}

static int camera_sync_configure(void)
{
    uint8_t cfg0 = 0;
    if (desk_watcher_board_pca_read(PCA_REG_CONFIG0, &cfg0) != DESK_HW_OK) {
        return DESK_HW_ERR_IO;
    }
    uint8_t next_cfg0 = (uint8_t)(cfg0 | PCA_PIN_SYNC_PORT0_MASK); /* Input mode */
    if (next_cfg0 != cfg0) {
        if (desk_watcher_board_pca_write(PCA_REG_CONFIG0, next_cfg0) != DESK_HW_OK) {
            return DESK_HW_ERR_IO;
        }
    }
    return DESK_HW_OK;
}

static int camera_sync_is_active(bool *active)
{
    uint8_t in0 = 0;
    if (!active) return DESK_HW_ERR_INVALID_ARG;
    int rc = desk_watcher_board_pca_read(PCA_REG_INPUT0, &in0);
    if (rc != DESK_HW_OK) {
        return rc;
    }
    *active = (in0 & PCA_PIN_SYNC_PORT0_MASK) != 0;
    return DESK_HW_OK;
}

#ifdef ESP_PLATFORM
static void camera_control_deinit(void);
static void camera_init_cleanup(void)
{
    s_camera_initialized = false;
    camera_control_deinit();
    if (s_spi_dev) {
        (void)spi_bus_remove_device(s_spi_dev);
        s_spi_dev = NULL;
    }
    if (s_spi_bus_owned) {
        (void)spi_bus_free(WATCHER_CAM_SPI_HOST);
        s_spi_bus_owned = false;
    }
    (void)camera_power_enable(false);
    if (s_cam_mutex) {
        vSemaphoreDelete(s_cam_mutex);
        s_cam_mutex = NULL;
    }
}

static int camera_control_exchange(const char *request, uint32_t request_id, uint32_t timeout_ms,
                                   desk_watcher_camera_control_state_t *state)
{
    if (uart_write_bytes(WATCHER_CAM_HX_UART, request, strlen(request)) != (int)strlen(request)) return -1;
    char line[128];
    size_t used = 0;
    int64_t deadline = esp_timer_get_time() + (int64_t)timeout_ms * 1000;
    while (esp_timer_get_time() < deadline) {
        uint8_t byte;
        if (uart_read_bytes(WATCHER_CAM_HX_UART, &byte, 1, pdMS_TO_TICKS(20)) != 1) continue;
        if (byte == '\r') continue;
        if (byte == '\n') {
            line[used] = '\0';
            if (desk_watcher_camera_parse_control_state(line, request_id, state) == 0) return 0;
            used = 0;
            continue;
        }
        if (byte < 0x20 || byte > 0x7e || used + 1 >= sizeof(line)) {
            used = 0;
            continue;
        }
        line[used++] = (char)byte;
    }
    return -1;
}

static int camera_control_status(desk_watcher_camera_control_state_t *state)
{
    char request[48];
    uint32_t id = ++s_hx_request_id;
    snprintf(request, sizeof(request), "WV1 STATUS %u\r\n", (unsigned)id);
    return camera_control_exchange(request, id, CAM_CONTROL_TIMEOUT_MS, state);
}

static int camera_control_mode(uint32_t mode, uint32_t model)
{
    desk_watcher_camera_control_state_t state;
    if (camera_control_status(&state) != 0 || state.result > 1U || state.error != 0U) return -1;
    if (state.mode == mode && state.model == model && state.debug == 0U) return 0;
    char request[80];
    uint32_t id = ++s_hx_request_id;
    snprintf(request, sizeof(request), "WV1 MODE %u %u %u %u 0\r\n", (unsigned)id,
             (unsigned)state.generation, (unsigned)mode, (unsigned)model);
    if (camera_control_exchange(request, id, CAM_CONTROL_TIMEOUT_MS, &state) != 0 || state.result > 1U) return -1;

    const uint32_t target_generation = state.generation;
    int64_t deadline = esp_timer_get_time() + (int64_t)CAM_CONTROL_MODE_TIMEOUT_MS * 1000;
    do {
        if (camera_control_status(&state) == 0 && state.generation == target_generation &&
            state.result == 0U && state.error == 0U &&
            state.mode == mode && state.model == model && state.debug == 0U) return 0;
        vTaskDelay(pdMS_TO_TICKS(5));
    } while (esp_timer_get_time() < deadline);
    return -1;
}

static int camera_control_init(void)
{
    if (uart_is_driver_installed(WATCHER_CAM_HX_UART)) return -1;
    if (uart_driver_install(WATCHER_CAM_HX_UART, 1024, 0, 0, NULL, 0) != ESP_OK) return -1;
    s_hx_uart_owned = true;
    const uart_config_t config = {
        .baud_rate = WATCHER_CAM_HX_UART_BAUD,
        .data_bits = UART_DATA_8_BITS,
        .parity = UART_PARITY_DISABLE,
        .stop_bits = UART_STOP_BITS_1,
        .flow_ctrl = UART_HW_FLOWCTRL_DISABLE,
        .source_clk = UART_SCLK_DEFAULT,
    };
    if (uart_param_config(WATCHER_CAM_HX_UART, &config) != ESP_OK ||
        uart_set_pin(WATCHER_CAM_HX_UART, WATCHER_CAM_HX_UART_TX, WATCHER_CAM_HX_UART_RX,
                     UART_PIN_NO_CHANGE, UART_PIN_NO_CHANGE) != ESP_OK) {
        camera_control_deinit();
        return -1;
    }
    for (unsigned attempt = 0; attempt < CAM_CONTROL_RETRIES; ++attempt) {
        desk_watcher_camera_control_state_t state;
        if (camera_control_status(&state) == 0 && state.result == 0U && state.error == 0U) return 0;
    }
    camera_control_deinit();
    return -1;
}

static void camera_control_deinit(void)
{
    if (s_hx_uart_owned) {
        (void)uart_driver_delete(WATCHER_CAM_HX_UART);
        s_hx_uart_owned = false;
    }
}

static int camera_capture_release(int result)
{
    int stop_rc = camera_control_mode(0U, 0U);
    if (s_cam_mutex) xSemaphoreGive(s_cam_mutex);
    return stop_rc != 0 && result == DESK_HW_OK ? DESK_WATCHER_CAMERA_ERR_CONTROL : result;
}

static int camera_read_resynchronized_header(uint8_t header[HX_PTL_BASE_HEADER_SIZE],
                                             uint32_t *body_size, size_t *prefetched)
{
    int64_t deadline = esp_timer_get_time() + (int64_t)CAM_PTL_RESYNC_TIMEOUT_MS * 1000;
    uint8_t chunk[CAM_PTL_RESYNC_CHUNK_SIZE];
    *prefetched = 0;
    for (unsigned n = 0; n < CAM_PTL_RESYNC_MAX_CHUNKS && esp_timer_get_time() < deadline; ++n) {
        spi_transaction_t trans = {
            .length = sizeof(chunk) * 8U, .rxlength = sizeof(chunk) * 8U,
            .rx_buffer = chunk, .flags = SPI_TRANS_CS_KEEP_ACTIVE,
        };
        esp_err_t err = spi_device_transmit(s_spi_dev, &trans);
        if (err != ESP_OK) return -1;
        for (size_t i = 0; i < sizeof(chunk); ++i) {
            if (desk_watcher_camera_resync_push(&s_ptl_scanner, chunk[i],
                                                WATCHER_HX_PTL_BODY_CAPACITY, body_size)) {
                memcpy(header, s_ptl_scanner.bytes, HX_PTL_BASE_HEADER_SIZE);
                *prefetched = i + 1U >= HX_PTL_BASE_HEADER_SIZE ? i + 1U - HX_PTL_BASE_HEADER_SIZE : 0U;
                if (*prefetched > *body_size) *prefetched = *body_size;
                if (*prefetched) memcpy(s_ptl_body, chunk + i + 1U - *prefetched, *prefetched);
                return 0;
            }
        }
    }
    return -1;
}
#endif

int desk_watcher_camera_init(void)
{
#ifdef ESP_PLATFORM
    if (s_camera_initialized) return DESK_HW_OK;

    if (desk_watcher_board_i2c_ensure() != DESK_HW_OK) {
        ESP_LOGE(TAG, "I2C0 master not available for camera expander");
        return DESK_HW_ERR_UNAVAILABLE;
    }

    if (!s_cam_mutex) {
        s_cam_mutex = xSemaphoreCreateMutex();
        if (!s_cam_mutex) return DESK_HW_ERR_UNAVAILABLE;
    }

    /* Configure SYNC pin and enable HX power. The upstream Watcher PTL
     * receiver gives HX6538 20 ms to boot before opening SPI2. */
    if (camera_sync_configure() != DESK_HW_OK || camera_power_enable(true) != DESK_HW_OK) {
        ESP_LOGW(TAG, "Failed to power up Himax HX6538 via PCA9535");
        camera_init_cleanup();
        return DESK_HW_ERR_UNAVAILABLE;
    }
    vTaskDelay(pdMS_TO_TICKS(CAM_HX_POWER_SETTLE_MS));

    /* Initialize SPI2 */
    const spi_bus_config_t buscfg = {
        .miso_io_num = WATCHER_CAM_PIN_MISO,
        .mosi_io_num = WATCHER_CAM_PIN_MOSI,
        .sclk_io_num = WATCHER_CAM_PIN_SCLK,
        .quadwp_io_num = -1,
        .quadhd_io_num = -1,
        .max_transfer_sz = CAM_MAX_PACKET_SZ,
    };

    esp_err_t err = spi_bus_initialize(WATCHER_CAM_SPI_HOST, &buscfg, SPI_DMA_CH_AUTO);
    s_spi_bus_owned = err == ESP_OK;
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
        ESP_LOGE(TAG, "SPI2 bus initialization failed: %s", esp_err_to_name(err));
        camera_init_cleanup();
        return DESK_HW_ERR_IO;
    }

    const spi_device_interface_config_t devcfg = {
        .clock_speed_hz = WATCHER_CAM_SPI_HZ,
        .mode = 0,
        .spics_io_num = WATCHER_CAM_PIN_CS,
        .queue_size = 3,
        .flags = 0,
    };

    err = spi_bus_add_device(WATCHER_CAM_SPI_HOST, &devcfg, &s_spi_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI2 device add failed: %s", esp_err_to_name(err));
        camera_init_cleanup();
        return DESK_HW_ERR_IO;
    }

    if (camera_control_init() != 0) {
        ESP_LOGE(TAG, "HX6538 UART1 STATUS handshake failed or UART1 is already owned");
        camera_init_cleanup();
        return DESK_WATCHER_CAMERA_ERR_CONTROL;
    }

    s_camera_initialized = true;
    ESP_LOGI(TAG, "Watcher Himax HX6538 camera ready (UART1 STATUS verified; SPI2: SCLK 4, MOSI 5, MISO 6, CS 21 @ 12MHz)");
    return DESK_HW_OK;
#else
    s_camera_initialized = true;
    return DESK_HW_OK;
#endif
}

static uint32_t ptl_media_crc32(const void *data, size_t size)
{
    const uint8_t *bytes = (const uint8_t *)data;
    uint32_t crc = 0xFFFFFFFF;
    if (!bytes && size != 0) return 0;
    for (size_t i = 0; i < size; ++i) {
        crc ^= bytes[i];
        for (unsigned bit = 0; bit < 8; ++bit) {
            uint32_t mask = (uint32_t)-(int32_t)(crc & 1);
            crc = (crc >> 1) ^ (0xEDB88320 & mask);
        }
    }
    return ~crc;
}

int desk_watcher_camera_parse_ptl_header(const uint8_t *header, size_t header_len, uint32_t *out_body_size)
{
    if (!header || !out_body_size || header_len < HX_PTL_BASE_HEADER_SIZE) return -1;
    if (header[0] != HX_PTL_MAGIC_0 || header[1] != HX_PTL_MAGIC_1) return -2;
    if (header[2] != HX_PTL_DATA_TYPE_JPEG) return -3;
    uint32_t body_size = (uint32_t)header[3] |
                         ((uint32_t)header[4] << 8) |
                         ((uint32_t)header[5] << 16) |
                         ((uint32_t)header[6] << 24);
    if (body_size < HX_MEDIA_HEADER_SIZE || body_size > WATCHER_HX_PTL_BODY_CAPACITY) return -4;
    *out_body_size = body_size;
    return 0;
}

static int parse_control_number(const char **cursor, uint32_t *out_value)
{
    if (!cursor || !*cursor || !out_value || **cursor < '0' || **cursor > '9') return -1;
    uint32_t value = 0;
    do {
        uint32_t digit = (uint32_t)(**cursor - '0');
        if (value > (UINT32_MAX - digit) / 10U) return -1;
        value = value * 10U + digit;
        ++(*cursor);
    } while (**cursor >= '0' && **cursor <= '9');
    *out_value = value;
    return 0;
}

int desk_watcher_camera_parse_control_state(const char *line, uint32_t expected_id,
                                            desk_watcher_camera_control_state_t *out_state)
{
    static const char prefix[] = "WV1 STATE ";
    if (!line || !out_state || strncmp(line, prefix, sizeof(prefix) - 1U) != 0) return -1;
    const char *cursor = line + sizeof(prefix) - 1U;
    uint32_t values[7];
    for (size_t i = 0; i < 7U; ++i) {
        if (parse_control_number(&cursor, &values[i]) != 0) return -1;
        if (i != 6U) {
            if (*cursor != ' ') return -1;
            ++cursor;
        }
    }
    if (*cursor != '\0' || values[0] != expected_id || values[1] > 6U || values[3] > 4U ||
        values[4] > 4U || values[5] > 1U || values[6] > 6U) {
        return -1;
    }
    *out_state = (desk_watcher_camera_control_state_t){
        .result = values[1], .generation = values[2], .mode = values[3],
        .model = values[4], .debug = values[5], .error = values[6],
    };
    return 0;
}

bool desk_watcher_camera_resync_push(desk_watcher_camera_resync_t *scanner, uint8_t byte,
                                     uint32_t max_body_size, uint32_t *out_body_size)
{
    if (!scanner || !out_body_size) return false;
    if (scanner->used == sizeof(scanner->bytes)) {
        memmove(scanner->bytes, scanner->bytes + 1U, sizeof(scanner->bytes) - 1U);
        --scanner->used;
    }
    scanner->bytes[scanner->used++] = byte;
    return scanner->used == sizeof(scanner->bytes) &&
           desk_watcher_camera_parse_ptl_header(scanner->bytes, sizeof(scanner->bytes), out_body_size) == 0 &&
           *out_body_size <= max_body_size;
}

int desk_watcher_camera_validate_frame(const uint8_t *body, size_t body_size, const uint8_t **out_jpeg, size_t *out_jpeg_size)
{
    if (!body || !out_jpeg || !out_jpeg_size || body_size < HX_MEDIA_HEADER_SIZE + 4) return -1;
    /* Check Media Header magic 'W' 'R' */
    if (body[0] != HX_MEDIA_MAGIC_0 || body[1] != HX_MEDIA_MAGIC_1) return -2;
    if (body[2] != HX_MEDIA_VERSION) return -3;

    uint32_t expected_crc = (uint32_t)body[12] |
                            ((uint32_t)body[13] << 8) |
                            ((uint32_t)body[14] << 16) |
                            ((uint32_t)body[15] << 24);

    size_t payload_len = body_size - HX_MEDIA_HEADER_SIZE;
    const uint8_t *payload = &body[HX_MEDIA_HEADER_SIZE];

    /* CRC32 check on full payload */
    uint32_t calc_crc = ptl_media_crc32(payload, payload_len);
    if (calc_crc != expected_crc) return -4;

    /* Find JPEG boundaries */
    size_t jpeg_sz = payload_len;
    while (jpeg_sz >= 2 && !(payload[jpeg_sz - 2] == 0xFF && payload[jpeg_sz - 1] == 0xD9)) {
        --jpeg_sz;
    }
    if (jpeg_sz < 4 || payload[0] != 0xFF || payload[1] != 0xD8 ||
        payload[jpeg_sz - 2] != 0xFF || payload[jpeg_sz - 1] != 0xD9) {
        return -5;
    }

    *out_jpeg = payload;
    *out_jpeg_size = jpeg_sz;
    return 0;
}

int desk_watcher_camera_capture(uint8_t *out_buf, size_t max_len, size_t *out_len)
{
    if (out_len) *out_len = 0;
    if (!out_buf || !out_len || max_len == 0) return DESK_HW_ERR_INVALID_ARG;

#ifdef ESP_PLATFORM
    if (!s_camera_initialized || !s_spi_dev) {
        ESP_LOGE(TAG, "Capture unavailable: camera not ready initialized=%d spi_device=%s",
                 s_camera_initialized, s_spi_dev ? "present" : "missing");
        return DESK_WATCHER_CAMERA_ERR_NOT_READY;
    }

    if (!s_cam_mutex || xSemaphoreTake(s_cam_mutex, pdMS_TO_TICKS(1000)) != pdTRUE) {
        return DESK_HW_ERR_BUSY;
    }

    if (camera_control_mode(1U, 0U) != 0) {
        xSemaphoreGive(s_cam_mutex);
        ESP_LOGE(TAG, "HX6538 failed to enter PTL capture mode");
        return DESK_WATCHER_CAMERA_ERR_CONTROL;
    }

    /* SYNC is a readiness hint during aligned reads. During recovery, scan
     * the bounded stream even if the edge was lost with the malformed frame. */
    int64_t start = esp_timer_get_time();
    bool sync_ready = s_ptl_resynchronizing;
    while (!sync_ready && (esp_timer_get_time() - start) < (int64_t)(CAM_SYNC_TIMEOUT_MS * 1000)) {
        bool sync_active = false;
        int sync_rc = camera_sync_is_active(&sync_active);
        if (sync_rc != DESK_HW_OK) {
            ESP_LOGE(TAG, "SYNC read failed: PCA9535 INPUT0 rc=%d", sync_rc);
            return camera_capture_release(DESK_WATCHER_CAMERA_ERR_SYNC_IO);
        }
        if (sync_active) {
            sync_ready = true;
            break;
        }
        vTaskDelay(pdMS_TO_TICKS(1));
    }

    if (!sync_ready) {
        ESP_LOGE(TAG, "SYNC timeout after %d ms: ready signal stayed low", CAM_SYNC_TIMEOUT_MS);
        return camera_capture_release(DESK_HW_ERR_TIMEOUT);
    }

    /* SYNC is a FIFO readiness hint: upstream waits 2 ms after its rising
     * edge because the complete PTL header may not yet be in the slave FIFO. */
    if (!s_ptl_resynchronizing) vTaskDelay(pdMS_TO_TICKS(CAM_SYNC_GUARD_MS));

    /* Acquire bus for multi-step transaction */
    esp_err_t err = spi_device_acquire_bus(s_spi_dev, portMAX_DELAY);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI bus acquire failed: %s (%d)", esp_err_to_name(err), (int)err);
        return camera_capture_release(DESK_WATCHER_CAMERA_ERR_SPI);
    }

    size_t prefetched = 0;
    uint32_t body_size = 0;
    uint8_t base_header[HX_PTL_BASE_HEADER_SIZE];
    if (s_ptl_resynchronizing) {
        int scan_rc = camera_read_resynchronized_header(base_header, &body_size, &prefetched);
        if (scan_rc != 0) {
            uint8_t dummy = 0;
            spi_transaction_t close_trans = {.length = 8, .rxlength = 8, .rx_buffer = &dummy, .flags = 0};
            (void)spi_device_transmit(s_spi_dev, &close_trans);
            spi_device_release_bus(s_spi_dev);
            return camera_capture_release(DESK_WATCHER_CAMERA_ERR_PROTOCOL);
        }
        s_ptl_resynchronizing = false;
    } else {
    /* Empty FIFO is a transient state: close it, back off, and retry a bounded
     * number of times. SYNC remains a readiness hint for each fresh read. */
    for (unsigned empty_attempt = 0; ; ++empty_attempt) {
        spi_transaction_t hdr_trans = {
            .length = sizeof(base_header) * 8,
            .rxlength = sizeof(base_header) * 8,
            .rx_buffer = base_header,
            .flags = SPI_TRANS_CS_KEEP_ACTIVE,
        };
        err = spi_device_transmit(s_spi_dev, &hdr_trans);
        if (err != ESP_OK) {
            ESP_LOGE(TAG, "SPI PTL header transfer failed: %s (%d)", esp_err_to_name(err), (int)err);
            spi_device_release_bus(s_spi_dev);
            s_ptl_resynchronizing = true;
            memset(&s_ptl_scanner, 0, sizeof(s_ptl_scanner));
            return camera_capture_release(DESK_WATCHER_CAMERA_ERR_SPI);
        }
        uint8_t header_or = 0;
        for (size_t i = 0; i < sizeof(base_header); ++i) header_or |= base_header[i];
        if (header_or != 0) break;

        bool sync_active_after_read = false;
        int sync_read_rc = camera_sync_is_active(&sync_active_after_read);
        ESP_LOGW(TAG,
                 "PTL empty FIFO poll %u/%u: raw=%02x%02x%02x%02x%02x%02x%02x sync=%d sync read rc=%d",
                 empty_attempt + 1U, CAM_PTL_EMPTY_RETRY_LIMIT, base_header[0], base_header[1], base_header[2],
                 base_header[3], base_header[4], base_header[5], base_header[6], sync_active_after_read, sync_read_rc);
        uint8_t dummy = 0;
        spi_transaction_t close_trans = {.length = 8, .rxlength = 8, .rx_buffer = &dummy, .flags = 0};
        (void)spi_device_transmit(s_spi_dev, &close_trans);
        spi_device_release_bus(s_spi_dev);
        if (empty_attempt + 1U >= CAM_PTL_EMPTY_RETRY_LIMIT) {
            return camera_capture_release(DESK_WATCHER_CAMERA_ERR_EMPTY);
        }
        vTaskDelay(pdMS_TO_TICKS(CAM_PTL_EMPTY_BACKOFF_MS));
        int64_t retry_start = esp_timer_get_time();
        bool retry_sync = false;
        while (esp_timer_get_time() - retry_start < (int64_t)CAM_SYNC_TIMEOUT_MS * 1000) {
            bool active = false;
            if (camera_sync_is_active(&active) != DESK_HW_OK) return camera_capture_release(DESK_WATCHER_CAMERA_ERR_SYNC_IO);
            if (active) { retry_sync = true; break; }
            vTaskDelay(pdMS_TO_TICKS(1));
        }
        if (!retry_sync) return camera_capture_release(DESK_HW_ERR_TIMEOUT);
        vTaskDelay(pdMS_TO_TICKS(CAM_SYNC_GUARD_MS));
        err = spi_device_acquire_bus(s_spi_dev, portMAX_DELAY);
        if (err != ESP_OK) return camera_capture_release(DESK_WATCHER_CAMERA_ERR_SPI);
    }

    int ptl_rc = desk_watcher_camera_parse_ptl_header(base_header, sizeof(base_header), &body_size);
    if (ptl_rc != 0) {
        ESP_LOGE(TAG, "PTL header rejected: parser rc=%d raw=%02x%02x%02x%02x%02x%02x%02x size=%u",
                 ptl_rc, base_header[0], base_header[1], base_header[2], base_header[3],
                 base_header[4], base_header[5], base_header[6],
                 (unsigned)base_header[3] | ((unsigned)base_header[4] << 8) |
                 ((unsigned)base_header[5] << 16) | ((unsigned)base_header[6] << 24));
        /* Discard / close rejected packet by clocking 1 dummy byte without KEEP_ACTIVE */
        uint8_t dummy = 0;
        spi_transaction_t close_trans = {
            .length = 8,
            .rxlength = 8,
            .rx_buffer = &dummy,
            .flags = 0,
        };
        (void)spi_device_transmit(s_spi_dev, &close_trans);
        spi_device_release_bus(s_spi_dev);
        s_ptl_resynchronizing = true;
        memset(&s_ptl_scanner, 0, sizeof(s_ptl_scanner));
        return camera_capture_release(DESK_WATCHER_CAMERA_ERR_PROTOCOL);
    }
    }

    /* Step 2: Read complete body into PSRAM buffer s_ptl_body */
    spi_transaction_t body_trans = {
        .length = (body_size - prefetched) * 8,
        .rxlength = (body_size - prefetched) * 8,
        .rx_buffer = s_ptl_body + prefetched,
        .flags = esp_ptr_external_ram(s_ptl_body + prefetched) ? SPI_TRANS_DMA_USE_PSRAM : 0,
    };
    err = spi_device_transmit(s_spi_dev, &body_trans);
    spi_device_release_bus(s_spi_dev);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "SPI PTL body transfer failed (body_size=%u): %s (%d)",
                 (unsigned)body_size, esp_err_to_name(err), (int)err);
        s_ptl_resynchronizing = true;
        memset(&s_ptl_scanner, 0, sizeof(s_ptl_scanner));
        return camera_capture_release(DESK_WATCHER_CAMERA_ERR_SPI);
    }

    /* Step 3: Validate Media Header, CRC32, and extract JPEG */
    const uint8_t *jpeg_ptr = NULL;
    size_t jpeg_sz = 0;
    int frame_rc = desk_watcher_camera_validate_frame(s_ptl_body, body_size, &jpeg_ptr, &jpeg_sz);
    if (frame_rc != 0) {
        if (frame_rc == -4) {
            ESP_LOGE(TAG, "CRC mismatch: media payload size=%u", (unsigned)(body_size - HX_MEDIA_HEADER_SIZE));
            s_ptl_resynchronizing = true;
            memset(&s_ptl_scanner, 0, sizeof(s_ptl_scanner));
            return camera_capture_release(DESK_WATCHER_CAMERA_ERR_CRC);
        }
        ESP_LOGE(TAG, "Media frame rejected: validator rc=%d body_size=%u", frame_rc, (unsigned)body_size);
        s_ptl_resynchronizing = true;
        memset(&s_ptl_scanner, 0, sizeof(s_ptl_scanner));
        return camera_capture_release(DESK_WATCHER_CAMERA_ERR_PROTOCOL);
    }

    if (jpeg_sz > max_len) {
        ESP_LOGE(TAG, "JPEG output buffer too small: jpeg_size=%u max_len=%u",
                 (unsigned)jpeg_sz, (unsigned)max_len);
        return camera_capture_release(DESK_HW_ERR_INVALID_ARG);
    }

    memcpy(out_buf, jpeg_ptr, jpeg_sz);
    *out_len = jpeg_sz;

    s_ptl_resynchronizing = false;
    memset(&s_ptl_scanner, 0, sizeof(s_ptl_scanner));
    return camera_capture_release(DESK_HW_OK);
#else
    return DESK_HW_ERR_UNAVAILABLE;
#endif
}

int desk_watcher_camera_deinit(void)
{
#ifdef ESP_PLATFORM
    if (s_cam_mutex && xSemaphoreTake(s_cam_mutex, pdMS_TO_TICKS(1000)) != pdTRUE) return DESK_HW_ERR_BUSY;
    if (s_camera_initialized) {
        (void)camera_control_mode(0U, 0U);
        s_camera_initialized = false;
    }
    camera_control_deinit();
    if (s_spi_dev) {
        (void)spi_bus_remove_device(s_spi_dev);
        s_spi_dev = NULL;
    }
    if (s_spi_bus_owned) {
        (void)spi_bus_free(WATCHER_CAM_SPI_HOST);
        s_spi_bus_owned = false;
    }
    (void)camera_power_enable(false);
    if (s_cam_mutex) {
        xSemaphoreGive(s_cam_mutex);
        vSemaphoreDelete(s_cam_mutex);
        s_cam_mutex = NULL;
    }
#else
    s_camera_initialized = false;
#endif
    return DESK_HW_OK;
}

bool desk_watcher_camera_is_ready(void)
{
    return s_camera_initialized;
}
