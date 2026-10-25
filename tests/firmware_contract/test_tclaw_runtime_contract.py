from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TestTclawRuntimeContract(unittest.TestCase):
    def test_native_boot_tolerates_transient_display_ui_ownership(self) -> None:
        app = (ROOT / "firmware/main/src/app_main.c").read_text(encoding="utf-8")
        checks = app.split("for (int r = 0; r < DESK_RES_MAX; r++)", 1)[1].split(
            'printf("[READY]', 1
        )[0]
        native_checks = checks.split("#ifdef ESP_PLATFORM", 1)[1].split("#endif", 1)[0]

        self.assertIn(
            "r == DESK_RES_DISPLAY_UI",
            native_checks,
            "display drawing can briefly own the arbiter during native runtime startup",
        )
        self.assertIn(
            "desk_arbiter_get_owner((desk_resource_type_t)r) != DESK_OWNER_NONE",
            checks,
            "the boot check must continue rejecting other leaked resource owners",
        )

    def test_only_wakeup_or_vad_starts_listening_expression(self) -> None:
        adapter = (ROOT / "firmware/main/src/tclaw_runtime_adapter.c").read_text(encoding="utf-8")
        handler = adapter.split("static void __desk_ai_chat_event_handler", 1)[1].split(
            "int desk_tclaw_runtime_init", 1
        )[0]
        listening_cases = handler.split('new_preset = "listening";', 1)[0]

        self.assertIn("case AI_USER_EVT_KEY_WAKEUP:", listening_cases)
        self.assertIn("case AI_USER_EVT_SERVER_VAD:", listening_cases)
        self.assertNotIn(
            "case AI_USER_EVT_MIC_DATA:",
            listening_cases,
            "raw PCM delivery is not a wakeup or voice activity event",
        )

    def test_cloud_disconnect_uses_offline_preset_and_gates_late_voice_events(self) -> None:
        adapter = (ROOT / "firmware/main/src/tclaw_runtime_adapter.c").read_text(encoding="utf-8")
        app = (ROOT / "firmware/main/src/app_main.c").read_text(encoding="utf-8")
        setter = adapter.split("void desk_tclaw_runtime_set_cloud_online", 1)[1].split(
            "static void __desk_ai_chat_event_handler", 1
        )[0]
        handler = adapter.split("static void __desk_ai_chat_event_handler", 1)[1].split(
            "int desk_tclaw_runtime_init", 1
        )[0]

        self.assertIn('"offline"', setter)
        self.assertIn('"Cloud Offline"', setter)
        self.assertIn("!s_cloud_online", handler,
                      "late TClaw events must not overwrite an authoritative offline state")
        self.assertIn("desk_tclaw_runtime_set_cloud_online(1)", app)
        self.assertIn("desk_tclaw_runtime_set_cloud_online(0)", app)

    def test_notification_alert_is_a_supported_board_expression(self) -> None:
        board = (ROOT / "firmware/components/desk_hardware/src/desk_watcher_board.c").read_text(
            encoding="utf-8"
        )
        self.assertIn('{ "alert",', board,
                      "notification delivery already requests alert; board adapter must accept it")
        self.assertIn('strcmp(preset_name, "alert") == 0', board,
                      "notification state must have an explicit renderer")

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

    def test_native_motion_tools_require_real_board_callbacks_and_use_builtin_dispatch(self) -> None:
        adapter = (ROOT / "firmware/main/src/tclaw_runtime_adapter.c").read_text(encoding="utf-8")
        registration = adapter.split("int desk_tclaw_runtime_register_mcp_tools(void)", 1)[1].split(
            "static OPERATE_RET __desk_mcp_tools_on_mqtt", 1
        )[0]
        motion_result = adapter.split("static OPERATE_RET __motion_set_response", 1)[1].split(
            "static OPERATE_RET __tool_motion_move", 1
        )[0]
        self.assertIn("static OPERATE_RET __tool_motion_move", adapter)
        self.assertIn("static OPERATE_RET __tool_motion_stop", adapter)
        move = adapter.split("static OPERATE_RET __tool_motion_move", 1)[1].split(
            "static OPERATE_RET __tool_motion_stop", 1
        )[0]
        stop = adapter.split("static OPERATE_RET __tool_motion_stop", 1)[1].split(
            "int desk_tclaw_runtime_register_mcp_tools", 1
        )[0]

        self.assertIn("desk_hardware_get_driver_ops()", registration)
        self.assertIn("if (ops && ops->motion_move)", registration)
        self.assertIn("if (ops && ops->motion_stop)", registration)
        self.assertIn("ops->motion_move", registration)
        self.assertIn('"motion_move"', registration)
        self.assertIn("ops->motion_stop", registration)
        self.assertIn('"motion_stop"', registration)
        self.assertIn('MCP_PROP_INT_RANGE("pan_deg"', registration)
        self.assertIn('MCP_PROP_INT_RANGE("tilt_deg"', registration)
        self.assertIn('MCP_PROP_INT_DEF_RANGE("duration_ms"', registration)
        self.assertNotIn('"servo_set"', registration)
        for field in ("pan_deg", "tilt_deg", "duration_ms"):
            self.assertIn(f'__get_int_prop(properties, "{field}"', move)
        self.assertIn("DESK_MOTION_PAN_MIN_DEG", move)
        self.assertIn("DESK_MOTION_PAN_MAX_DEG", move)
        self.assertIn("DESK_MOTION_TILT_MIN_DEG", move)
        self.assertIn("DESK_MOTION_TILT_MAX_DEG", move)
        self.assertIn('"ctrl.motion.move"', move)
        self.assertIn("desk_tclaw_execute_tool", move)
        self.assertIn("DESK_HW_ERR_BUSY", motion_result)
        self.assertIn("DESK_HW_ERR_TIMEOUT", motion_result)
        self.assertIn("physical completion is not confirmed", move)
        self.assertIn('__get_bool_prop(properties, "emergency"', stop)
        self.assertIn('"ctrl.motion.stop"', stop)
        self.assertIn("desk_tclaw_execute_tool", stop)
        self.assertIn("physical stop is not confirmed", stop)

        bridge = (ROOT / "firmware/components/desk_tclaw/src/desk_tclaw_bridge.c").read_text(
            encoding="utf-8"
        )
        builtin_move = bridge.split('if (strcmp(operation, "motion.move")', 1)[1].split(
            "/* 2. Motion Stop Tool */", 1
        )[0]
        builtin_stop = bridge.split('else if (strcmp(operation, "motion.stop")', 1)[1].split(
            "/* 3. Camera Capture Tool", 1
        )[0]
        self.assertIn("DESK_MOTION_PAN_MIN_DEG", builtin_move)
        self.assertIn("DESK_MOTION_PAN_MAX_DEG", builtin_move)
        self.assertIn("DESK_MOTION_TILT_MIN_DEG", builtin_move)
        self.assertIn("DESK_MOTION_TILT_MAX_DEG", builtin_move)
        self.assertIn('"INVALID_ARGUMENT"', builtin_move)
        self.assertLess(builtin_move.index('"INVALID_ARGUMENT"'),
                        builtin_move.index("desk_arbiter_acquire"))
        self.assertIn('"OPERATION_TIMEOUT"', builtin_move)
        self.assertIn('"OPERATION_TIMEOUT"', builtin_stop)
        self.assertIn('"RESOURCE_BUSY"', builtin_stop)
        dialogue_tests = (ROOT / "tests/firmware_contract/test_tclaw_dialogue.py").read_text(
            encoding="utf-8"
        )
        for case in ("test_builtin_motion_tool_cr002_compliance",
                     "test_builtin_motion_arbitration_rejection",
                     "test_builtin_motion_rejects_out_of_range_target"):
            self.assertIn(case, dialogue_tests)
        stm32_tests = (ROOT / "tests/firmware_contract/test_stm32_body_protocol.py").read_text(
            encoding="utf-8"
        )
        self.assertIn("test_07_timeout_and_sequence_preservation_invariant", stm32_tests)


if __name__ == "__main__":
    unittest.main()
