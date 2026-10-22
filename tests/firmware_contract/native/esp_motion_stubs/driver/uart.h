#ifndef TEST_DRIVER_UART_H
#define TEST_DRIVER_UART_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
typedef int esp_err_t;
#define ESP_OK 0
#define UART_NUM_2 2
#define UART_DATA_8_BITS 8
#define UART_PARITY_DISABLE 0
#define UART_STOP_BITS_1 1
#define UART_HW_FLOWCTRL_DISABLE 0
#define UART_SCLK_DEFAULT 0
#define UART_PIN_NO_CHANGE (-1)
typedef struct {
    int baud_rate;
    int data_bits;
    int parity;
    int stop_bits;
    int flow_ctrl;
    int source_clk;
} uart_config_t;
bool uart_is_driver_installed(int port);
esp_err_t uart_param_config(int port, const uart_config_t *config);
esp_err_t uart_set_pin(int port, int tx, int rx, int rts, int cts);
esp_err_t uart_driver_install(int port, int rx_buf, int tx_buf, int queue_size,
                              void *queue, int flags);
esp_err_t uart_driver_delete(int port);
void uart_flush_input(int port);
int uart_write_bytes(int port, const char *data, size_t len);
int uart_read_bytes(int port, uint8_t *data, size_t len, uint32_t timeout_ticks);
#endif
