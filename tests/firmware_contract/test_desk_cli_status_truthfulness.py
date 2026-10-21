"""L2 source contracts for truthful hardware CLI status and body commands."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
CLI_C = ROOT / "firmware/components/desk_cli/src/desk_cli.c"
HARDWARE_C = ROOT / "firmware/components/desk_hardware/src/desk_hardware.c"


def function_body(source: str, signature: str, next_signature: str) -> str:
    definitions = source.split("static const desk_cli_command_t s_default_cmds[]", 1)[1]
    return definitions.split(signature, 1)[1].split(next_signature, 1)[0]


class TestDeskCliStatusTruthfulness(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = CLI_C.read_text(encoding="utf-8")

    def test_body_status_does_not_call_tx_length_receive_or_ack_evidence(self) -> None:
        body = function_body(self.source, "static void cmd_body(int argc, char *argv[])\n{",
                             "static void cmd_peripherals(int argc, char *argv[])\n{")
        self.assertNotIn("Last Packet Length", body)
        self.assertIn("Last formatted TX request length", body)
        self.assertIn("not an RX/ACK counter", body)
        self.assertIn("STM32 peer/ACK state: UNKNOWN", body)
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
        self.assertIn("Display status: UNKNOWN", display)
        self.assertIn("rc == 0", network)
        self.assertIn('link_name = "UNKNOWN"', network)
        self.assertIn("rc == 0", wifi)
        self.assertIn('link_name = "UNKNOWN"', wifi)
        self.assertIn("UNKNOWN (host simulation; no radio state)", wifi)

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
        self.assertIn("#ifdef ESP_PLATFORM\n        rc = desk_hardware_motion_move_to", body)
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

    def test_body_stop_remains_emergency_and_reports_driver_failure(self) -> None:
        body = function_body(self.source, "static void cmd_body(int argc, char *argv[])\n{",
                             "static void cmd_peripherals(int argc, char *argv[])\n{")
        self.assertIn('strcmp(argv[1], "stop")', body)
        self.assertIn("desk_hardware_motion_stop(true)", body)
        self.assertIn("#ifdef ESP_PLATFORM\n        rc = desk_hardware_motion_stop(true)", body)
        self.assertIn("Body emergency stop failed: rc=%d", body)
        self.assertIn("physical stop is unconfirmed", body)


if __name__ == "__main__":
    unittest.main()
