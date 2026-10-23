"""L2 source contracts for truthful hardware CLI status and body commands."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLI_C = ROOT / "firmware/components/desk_cli/src/desk_cli.c"
HARDWARE_C = ROOT / "firmware/components/desk_hardware/src/desk_hardware.c"


def function_body(source: str, signature: str, next_signature: str) -> str:
    definitions = source.split("static const desk_cli_command_t s_default_cmds[]", 1)[1]
    return definitions.split(signature, 1)[1].split(next_signature, 1)[0]


def conditional_body(source: str, condition: str) -> re.Match[str] | None:
    return re.search(
        rf"\bif\s*\(\s*{re.escape(condition)}\s*\)\s*\{{(?P<body>[^{{}}]*)\}}",
        source,
        re.DOTALL,
    )


class TestDeskCliStatusTruthfulness(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = CLI_C.read_text(encoding="utf-8")

    def test_body_status_reports_only_observed_uart_diagnostics(self) -> None:
        body = function_body(self.source, "static void cmd_body(int argc, char *argv[])\n{",
                             "static void cmd_peripherals(int argc, char *argv[])\n{")
        self.assertNotIn("Last Packet Length", body)
        self.assertIn("Last formatted TX request length", body)
        self.assertIn("desk_watcher_motion_get_diagnostics(&motion_diag)", body)
        self.assertIn("UART TX bytes (driver reported)", body)
        self.assertIn("RX bytes", body)
        self.assertIn("Valid correlated ACKs", body)
        self.assertIn("bad CRC", body)
        self.assertIn("wrong sequence", body)
        self.assertIn("last motion error", body)
        self.assertIn("no valid ACK observed", body)
        self.assertNotIn("desk_hardware_mcu_receive_bytes", body)
        self.assertIn("not physical feedback", body)

    def test_peripheral_placeholders_are_unknown_without_status_apis(self) -> None:
        body = function_body(self.source, "static void cmd_peripherals(int argc, char *argv[])\n{",
                             "static void cmd_network(int argc, char *argv[])\n{")
        self.assertIn("RTC state             : UNKNOWN", body)
        self.assertIn("RGB LED state         : UNKNOWN", body)
        self.assertIn("Battery state         : UNKNOWN", body)
        self.assertIn("IO Expander state     : UNKNOWN", body)
        self.assertNotIn(": READY", body)
        self.assertNotIn(": CONFIGURED", body)

    def test_status_commands_use_observable_sources_or_unknown(self) -> None:
        audio = function_body(self.source, "static void cmd_audio(int argc, char *argv[])\n{",
                              "static void cmd_camera(int argc, char *argv[])\n{")
        camera = function_body(self.source, "static void cmd_camera(int argc, char *argv[])\n{",
                               "static void cmd_body(int argc, char *argv[])\n{")
        display = function_body(self.source, "static void cmd_display(int argc, char *argv[])\n{",
                                "static void cmd_audio(int argc, char *argv[])\n{")
        network = function_body(self.source, "static void cmd_network(int argc, char *argv[])\n{",
                                "static void cmd_provisioning(int argc, char *argv[])\n{")
        wifi = function_body(self.source, "static void cmd_wifi(int argc, char *argv[])\n{",
                             "int desk_cli_exec(")

        self.assertIn("desk_hardware_audio_get_status", audio)
        self.assertIn("Audio status: UNKNOWN", audio)
        self.assertIn("desk_watcher_camera_is_ready", camera)
        self.assertIn("Capture Driver State  : UNKNOWN", camera)
        self.assertIn("desk_hardware_is_display_initialized", display)
        self.assertIn("desk_hardware_display_get_expression", display)
        self.assertIn("Display Software Shadow: UNKNOWN", display)
        self.assertIn("rc == 0", network)
        self.assertIn('link_name = "UNKNOWN"', network)
        self.assertIn("rc == 0", wifi)
        self.assertIn('link_name = "UNKNOWN"', wifi)
        self.assertIn("status == NETMGR_LINK_UP", wifi)
        self.assertIn("status == NETMGR_LINK_DOWN", wifi)
        self.assertNotIn("? \"LINK_UP\" : \"LINK_DOWN\"", wifi)
        self.assertIn("UNKNOWN (host simulation; no radio state)", wifi)

    def test_system_does_not_infer_arbiter_readiness_from_resource_owners(self) -> None:
        system = function_body(self.source, "static void cmd_system(int argc, char *argv[])\n{",
                               "static void cmd_display(int argc, char *argv[])\n{")

        self.assertIn("Hardware Arbiter Readiness: UNKNOWN (no readiness/status API)", system)
        self.assertIn("Display Driver Initialization: %s", system)
        self.assertIn("physical scanout not exposed", system)
        self.assertNotIn("Hardware Arbiter    : ACTIVE", system)
        self.assertNotIn("desk_arbiter_get_owner", system)

    def test_system_version_reports_runtime_app_descriptor_identity(self) -> None:
        version = function_body(self.source, "static void cmd_sys_version(int argc, char *argv[])\n{",
                                "extern uint32_t desk_app_task_stack_hwm(void);")
        component = (ROOT / "firmware/components/desk_cli/CMakeLists.txt").read_text(encoding="utf-8")

        self.assertIn('#include "esp_app_desc.h"', self.source)
        self.assertIn("esp_app_get_description()", version)
        for field in ("project_name", "version", "date", "time", "idf_ver", "app_elf_sha256"):
            self.assertIn(f"desc->{field}", version)
        self.assertIn("ELF SHA256", version)
        self.assertIn("not source SHA", version)
        self.assertNotIn("git", version.lower())
        self.assertIn("esp_app_format", component)

    def test_display_reports_initialization_shadow_and_scanout_independently(self) -> None:
        display = function_body(self.source, "static void cmd_display(int argc, char *argv[])\n{",
                                "static void cmd_audio(int argc, char *argv[])\n{")

        self.assertIn("Display Driver Initialization: %s", display)
        self.assertIn("Display Software Shadow: UNKNOWN (driver not initialized)", display)
        self.assertIn("Display Software Shadow: AVAILABLE (cached expression)", display)
        self.assertIn("Display Software Shadow: UNKNOWN (readback rc=%d)", display)
        self.assertIn("Display Physical Scanout: UNKNOWN (no physical scanout status API)", display)
        self.assertNotIn("Display status: READY", display)

    def test_display_status_branches_keep_errors_and_success_on_their_own_paths(self) -> None:
        display = function_body(self.source, "static void cmd_display(int argc, char *argv[])\n{",
                                "static void cmd_audio(int argc, char *argv[])\n{")

        self.assert_display_status_branch_contract(display)

    def assert_display_status_branch_contract(self, display: str) -> None:
        not_initialized = conditional_body(display, "!initialized")
        self.assertIsNotNone(not_initialized)
        not_initialized_body = not_initialized.group("body")
        self.assertIn('Display Software Shadow: UNKNOWN (driver not initialized)', not_initialized_body)
        self.assertRegex(not_initialized_body, r"\breturn\s*;")

        getter_failed = conditional_body(display, "rc != DESK_HW_OK")
        self.assertIsNotNone(getter_failed)
        getter_failed_body = getter_failed.group("body")
        self.assertIn('Display Software Shadow: UNKNOWN (readback rc=%d)', getter_failed_body)
        self.assertRegex(getter_failed_body, r"\breturn\s*;")

        available = re.search(
            r'Display Software Shadow: AVAILABLE \(cached expression\)', display
        )
        self.assertIsNotNone(available)
        self.assertGreater(available.start(), getter_failed.end())

    def test_display_status_contract_rejects_reversed_initialization_guard(self) -> None:
        display = function_body(self.source, "static void cmd_display(int argc, char *argv[])\n{",
                                "static void cmd_audio(int argc, char *argv[])\n{")
        wrong_control_flow = display.replace("if (!initialized)", "if (initialized)", 1)

        with self.assertRaises(AssertionError):
            self.assert_display_status_branch_contract(wrong_control_flow)

    def test_camera_capture_cli_names_specific_driver_failures(self) -> None:
        camera = function_body(self.source, "static void cmd_camera(int argc, char *argv[])\n{",
                               "static void cmd_body(int argc, char *argv[])\n{")
        names = self.source.split("static const char *cli_camera_capture_error_name(", 1)[1].split(
            "\n}", 1
        )[0]
        expected_labels = {
            "DESK_HW_ERR_UNAVAILABLE": "unavailable",
            "DESK_HW_ERR_TIMEOUT": "timeout",
            "DESK_HW_ERR_BUSY": "busy",
            "DESK_HW_ERR_INVALID_ARG": "invalid-arg",
            "DESK_WATCHER_CAMERA_ERR_PROTOCOL": "protocol",
            "DESK_WATCHER_CAMERA_ERR_CRC": "CRC",
            "DESK_WATCHER_CAMERA_ERR_SYNC_IO": "SYNC I/O",
            "DESK_WATCHER_CAMERA_ERR_NOT_READY": "not-ready",
            "DESK_WATCHER_CAMERA_ERR_SPI": "SPI",
            "DESK_WATCHER_CAMERA_ERR_EMPTY": "empty FIFO",
            "DESK_WATCHER_CAMERA_ERR_CONTROL": "HX6538 control",
            "DESK_WATCHER_CAMERA_ERR_BUFFER_TOO_SMALL": "buffer-too-small",
        }
        for error_code, reason in expected_labels.items():
            with self.subTest(error_code=error_code):
                self.assertIn(f'case {error_code}: return "{reason}";', names)
        self.assertIn('"unknown camera error"', names)
        self.assertIn('rc=%d (%s)', camera)
        self.assertIn("cli_camera_capture_error_name(rc)", camera)

    def test_body_move_parses_required_args_and_delegates_limits_and_result_to_hal(self) -> None:
        body = function_body(self.source, "static void cmd_body(int argc, char *argv[])\n{",
                             "static void cmd_peripherals(int argc, char *argv[])\n{")
        float_parser = self.source.split("static bool cli_parse_finite_float(", 1)[1].split(
            "static bool cli_parse_duration_ms(", 1
        )[0]
        duration_parser = self.source.split("static bool cli_parse_duration_ms(", 1)[1].split(
            "#ifdef ESP_PLATFORM", 1
        )[0]

        self.assertIn('strcmp(argv[1], "move")', body)
        self.assertIn("argc != 4 && argc != 5", body)
        self.assertIn("cli_parse_finite_float", body)
        self.assertIn("cli_parse_duration_ms", body)
        self.assertIn("desk_hardware_motion_move_to(pan, tilt, duration_ms)", body)
        self.assertIn("#ifdef ESP_PLATFORM\n        desk_arbiter_status_t arb", body)
        self.assertIn("desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE)", body)
        self.assertIn("Host HAL is a simulation", body)
        self.assertIn("accepted=true, state_confirmed=false", body)
        self.assertIn("Body move failed: rc=%d", body)
        self.assertIn("!isfinite(value)", float_parser)
        self.assertIn("*end != '\\0'", float_parser)
        self.assertIn("text[0] == '-'", duration_parser)
        self.assertIn("value > UINT32_MAX", duration_parser)

        # The CLI delegates the mechanical boundary to the existing HAL, which
        # clamps before invoking the board driver.
        hardware = HARDWARE_C.read_text(encoding="utf-8")
        motion = hardware.split("int desk_hardware_motion_move_to(", 1)[1].split(
            "int desk_hardware_motion_stop(", 1
        )[0]
        self.assertIn("DESK_MOTION_PAN_MIN_DEG", motion)
        self.assertIn("DESK_MOTION_PAN_MAX_DEG", motion)
        self.assertIn("DESK_MOTION_TILT_MIN_DEG", motion)
        self.assertIn("DESK_MOTION_TILT_MAX_DEG", motion)

    def test_body_move_uses_hal_internal_motion_arbitration(self) -> None:
        cli_body = function_body(self.source, "static void cmd_body(int argc, char *argv[])\n{",
                                 "static void cmd_peripherals(int argc, char *argv[])\n{")
        hardware = HARDWARE_C.read_text(encoding="utf-8")
        motion = hardware.split("int desk_hardware_motion_move_to(", 1)[1].split(
            "int desk_hardware_motion_stop(", 1
        )[0]

        self.assertIn("desk_hardware_motion_move_to(pan, tilt, duration_ms)", cli_body)
        self.assertIn("desk_arbiter_get_owner(DESK_RES_MOTION_GIMBAL)", motion)
        self.assertIn("desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE, 50)", motion)
        self.assertIn("current != DESK_OWNER_SDK_REMOTE && current != DESK_OWNER_TCLAW_VOICE", motion)
        self.assertIn("desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE)", motion)
        self.assertLess(motion.index("desk_arbiter_acquire("), motion.index("result = s_board_driver_ops->motion_move"))
        self.assertLess(motion.index("result = s_board_driver_ops->motion_move"), motion.index("if (need_release)"))

    def test_local_cli_move_cannot_borrow_the_voice_motion_owner(self) -> None:
        body = function_body(self.source, "static void cmd_body(int argc, char *argv[])\n{",
                             "static void cmd_peripherals(int argc, char *argv[])\n{")
        acquire = body.index("desk_arbiter_acquire(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE")
        move = body.index("desk_hardware_motion_move_to(pan, tilt, duration_ms)")
        release = body.index("desk_arbiter_release(DESK_RES_MOTION_GIMBAL, DESK_OWNER_SDK_REMOTE)")
        self.assertLess(acquire, move)
        self.assertLess(move, release)
        self.assertIn("if (arb != DESK_ARBITER_OK)", body)
        self.assertIn("RESOURCE_BUSY", body)

    def test_body_stop_remains_emergency_and_reports_driver_failure(self) -> None:
        body = function_body(self.source, "static void cmd_body(int argc, char *argv[])\n{",
                             "static void cmd_peripherals(int argc, char *argv[])\n{")
        self.assertIn('strcmp(argv[1], "stop")', body)
        self.assertIn("desk_hardware_motion_stop(true)", body)
        self.assertIn("#ifdef ESP_PLATFORM\n        rc = desk_hardware_motion_stop(true)", body)
        self.assertIn("Body emergency stop failed: rc=%d", body)
        self.assertIn("physical stop is unconfirmed", body)

    def test_body_rejects_extra_stop_args_and_unknown_subcommands(self) -> None:
        body = function_body(self.source, "static void cmd_body(int argc, char *argv[])\n{",
                             "static void cmd_peripherals(int argc, char *argv[])\n{")
        self.assertIn('argc == 2 && strcmp(argv[1], "stop") == 0', body)
        self.assertIn('argc == 1 || (argc == 2 && strcmp(argv[1], "status") == 0)', body)
        self.assertIn('Usage: body [status|move <pan_deg> <tilt_deg> [duration_ms]|stop]', body)
        self.assertIn('return;\n    }\n    printf("Usage: body', body)

    def test_cli_rejects_truncated_lines_and_ignored_extra_arguments(self) -> None:
        execute = self.source.split("int desk_cli_exec(const char *cmd_line)\n{", 1)[1].split(
            "void desk_cli_echo(", 1
        )[0]

        self.assertIn("command_length >= CLI_LINE_MAX_LEN", execute)
        self.assertIn("Command rejected: input exceeds %u bytes", execute)
        self.assertIn("maximum %u arguments supported", execute)
        self.assertIn("if (*p)", execute)
        self.assertLess(execute.index("command_length >= CLI_LINE_MAX_LEN"), execute.index("strncpy("))
        self.assertLess(execute.index("maximum %u arguments supported"), execute.index("/* Find command */"))


if __name__ == "__main__":
    unittest.main()
