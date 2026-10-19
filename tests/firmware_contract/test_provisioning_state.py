import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = os.path.realpath(os.path.join(os.path.dirname(__file__), "..", ".."))


class TestProvisioningState(unittest.TestCase):
    def test_events_report_only_observed_stages_and_keep_failure_code(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            exe = os.path.join(temp_dir, "provisioning_state_test")
            source = os.path.join(ROOT, "tests", "firmware_contract", "provisioning_state_test.c")
            implementation = os.path.join(ROOT, "firmware", "main", "src", "provisioning_state.c")
            header_dir = os.path.join(ROOT, "firmware", "main", "include")
            build = subprocess.run(
                ["gcc", "-std=c11", "-Wall", "-Wextra", "-Werror", f"-I{header_dir}", source, implementation, "-o", exe],
                capture_output=True,
                text=True,
            )
            self.assertEqual(build.returncode, 0, "provisioning state harness must compile")
            run = subprocess.run([exe], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, "provisioning event transitions must preserve observed state")

    def test_production_boot_has_no_sdk_or_secret_fallback_and_uses_real_netmgr(self) -> None:
        app = Path(os.path.join(ROOT, "firmware", "main", "src", "app_main.c")).read_text(encoding="utf-8")
        config = Path(os.path.join(ROOT, "firmware", "main", "include", "tuya_config.h")).read_text(encoding="utf-8")
        cli_path = Path(os.path.join(ROOT, "firmware", "components", "desk_cli", "src", "desk_cli.c"))
        cli = cli_path.read_text(encoding="utf-8")
        self.assertTrue("#ifdef ESP_PLATFORM\n#include \"tuya_iot.h\"" in app, "ESP target must require the Tuya SDK headers")
        self.assertTrue("netmgr_conn_get(NETCONN_AUTO, NETCONN_CMD_STATUS" in app, "network readiness must use SDK netmgr status")
        self.assertFalse("return true;" in app, "network callback must not claim link-up unconditionally")
        self.assertTrue("#include \"tuya_config_secrets.h\"" in config, "local credentials must come from ignored config")
        self.assertFalse("TUYA_OPENSDK_UUID_BACKUP" in config, "startup must not switch device identity")
        self.assertFalse("TUYA_OPENSDK_AUTHKEY_BACKUP" in config, "startup must not switch device identity")
        self.assertFalse("int tal_cli_init(" in cli, "DeskAgent must not override official TAL init")
        self.assertFalse("int tal_cli_cmd_register(" in cli, "DeskAgent must not override official TAL registry")
        self.assertTrue("tal_cli_cmd_register(s_sdk_commands" in cli, "diagnostic commands must use the SDK registry")
        self.assertFalse("xTaskCreate" in cli, "DeskAgent must not start a second CLI worker")
        self.assertFalse('"connect <ssid> [pass]"' in cli, "CLI must not accept or echo Wi-Fi credentials")
        self.assertFalse("esp_wifi_scan_start" in cli, "production network diagnostics must be read-only status")
        self.assertTrue("firmware/components/desk_cli" in cli_path.as_posix(), "local CLI component must not shadow TAL")
        self.assertTrue("desk_tuya_log_output" in app, "TAL output must pass through the sensitive-line filter")
        self.assertTrue("desk_log_line_has_sensitive_marker(line)" in app, "sensitive SDK log lines must be suppressed")
        self.assertFalse("(TAL_LOG_OUTPUT_CB)tkl_log_output" in app, "raw SDK logs must not bypass filtering")
        self.assertTrue("desk_yield_retry_step" in app, "yield errors must follow retry policy")
        self.assertFalse("if (yield_rc != OPRT_OK) return desk_boot_check" in app,
                         "transient SDK yield errors must not terminate the provisioning worker")


if __name__ == "__main__":
    unittest.main()
