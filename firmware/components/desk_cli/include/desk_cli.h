#ifndef DESK_CLI_H_
#define DESK_CLI_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef void (*desk_cli_command_cb_t)(int argc, char *argv[]);

typedef struct {
    const char *name;
    const char *help;
    desk_cli_command_cb_t func;
} desk_cli_command_t;

/* Production registers diagnostics in the SDK TAL registry. The host build
 * retains a small local executor for contract tests; it never starts a worker. */
int desk_cli_register_commands(void);
int desk_cli_command_register(const desk_cli_command_t *commands, uint8_t count);
int desk_cli_exec(const char *command_line);
void desk_cli_echo(const char *string);
void desk_cli_update_provisioning_status(const char *stage, const char *error_stage, int error_code);
#ifndef ESP_PLATFORM
int desk_cli_init(void);
#endif

#endif
