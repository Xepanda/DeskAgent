from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TestTclawRuntimeContract(unittest.TestCase):
    def test_native_runtime_uses_official_modules_in_dependency_order(self) -> None:
        source = ROOT / "firmware/main/src/tclaw_runtime_adapter.c"
        adapter = source.read_text(encoding="utf-8")
        app = (ROOT / "firmware/main/src/app_main.c").read_text(encoding="utf-8")

        calls = [
            "sys_bus_init()",
            "tclaw_chat_init()",
            "app_im_init()",
            "ws_server_start()",
            "tool_registry_init()",
            "acp_client_init()",
            "agent_loop_init()",
        ]
        positions = [adapter.index(call) for call in calls]
        self.assertEqual(positions, sorted(positions), "official TClaw runtime startup order must be preserved")
        self.assertIn("desk_tclaw_runtime_init()", app, "production boot must call the native TClaw adapter")
        self.assertIn("#ifndef ESP_PLATFORM", app, "the local host dialogue stub must stay out of production")
        boot = app.split("int runtime_rc = desk_tclaw_runtime_init();", 1)[1]
        self.assertNotIn("desk_tclaw_start()", boot.split("#else", 1)[0],
                         "production must not start the local dialogue loop")
        self.assertNotIn("tuya_iot_init(", adapter, "native runtime must reuse the single provisioning client")
        self.assertNotIn("tuya_iot_start(", adapter, "native runtime must reuse the single provisioning client")
        self.assertNotIn("tuya_iot_yield(", adapter, "native runtime must reuse the single provisioning loop")
        self.assertNotIn("ai_agent_init(", adapter, "tclaw_chat_init owns lazy ai_agent initialization")

    def test_optional_profiles_and_native_init_failures_are_explicit(self) -> None:
        adapter = (ROOT / "firmware/main/src/tclaw_runtime_adapter.c").read_text(encoding="utf-8")
        for state in ("ready", "disabled", "failed", "blocked"):
            self.assertIn(f'"{state}"', adapter, f"runtime state {state} must be reportable")
        self.assertIn("IM_SECRET_FS_APP_ID", adapter)
        self.assertIn("OPENCLAW_GATEWAY_TOKEN", adapter)
        self.assertIn("CLAW_WS_AUTH_TOKEN", adapter)
        self.assertIn("TCLAW_RUNTIME", adapter)

    def test_bridge_response_requires_real_envelope_and_matching_request(self) -> None:
        app = (ROOT / "firmware/main/src/app_main.c").read_text(encoding="utf-8")
        adapter = (ROOT / "firmware/main/src/tclaw_runtime_adapter.c").read_text(encoding="utf-8")
        tclaw = (ROOT / "firmware/components/desk_tclaw/src/desk_tclaw_bridge.c").read_text(encoding="utf-8")
        self.assertIn("cJSON_Parse", adapter, "bridge response must use JSON parsing")
        for key in ("request_id", "status", "result", "error"):
            self.assertIn(f'"{key}"', adapter, f"response parser must inspect top-level {key}")
        self.assertNotIn('"res-bridge", "succeeded"', app, "response must not invent request ID or success")
        self.assertIn("desk_tclaw_get_pending_request_id", adapter)
        self.assertIn("strcmp(s_pending_request_id, request_id)", tclaw,
                      "TClaw response handling must reject mismatched request correlation")

    def test_native_mcp_tools_registration_and_bridge_dispatch(self) -> None:
        adapter = (ROOT / "firmware/main/src/tclaw_runtime_adapter.c").read_text(encoding="utf-8")
        header = (ROOT / "firmware/main/include/tclaw_runtime_adapter.h").read_text(encoding="utf-8")

        self.assertIn("desk_tclaw_runtime_register_mcp_tools", header)
        self.assertIn("desk_tclaw_runtime_register_mcp_tools", adapter)
        self.assertIn("EVENT_MQTT_CONNECTED", adapter)
        self.assertIn("desk_mcp_tools", adapter)

        # Check native tools registered
        for tool in ("ha_execute", "codex_submit_turn", "reminder_create"):
            self.assertIn(f'"{tool}"', adapter, f"Native tool {tool} must be registered")

        # Check operations wired to Host bridge
        for op in ("ha.execute", "codex.submit_turn", "reminder.create"):
            self.assertIn(f'"{op}"', adapter, f"Tool operation {op} must be dispatched")

        self.assertIn("desk_sdk_bridge_dispatch_tool_request", adapter,
                      "Native tool execution must dispatch request envelope to SDK bridge")


if __name__ == "__main__":
    unittest.main()
