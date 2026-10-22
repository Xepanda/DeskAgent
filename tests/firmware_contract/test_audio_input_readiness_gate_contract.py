from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class TestAudioInputReadinessGateContract(unittest.TestCase):
    def test_runtime_audio_init_failure_is_recorded_and_ai_events_are_gated(self) -> None:
        source = (ROOT / "firmware/main/src/tclaw_runtime_adapter.c").read_text(encoding="utf-8")
        event_handler = source.split("static void __desk_ai_chat_event_handler", 1)[1].split(
            "int desk_tclaw_runtime_init", 1
        )[0]
        init = source.split("int desk_tclaw_runtime_init", 1)[1].split(
            "int desk_tclaw_runtime_handle_bridge_response", 1
        )[0]

        self.assertIn("s_voice_event_ui_enabled", event_handler,
                      "late AI/MQTT events must not make a failed local audio path appear active")
        self.assertIn("STAGE_AUDIO_HAL", init)
        self.assertIn("desk_hardware_audio_get_status", init)
        self.assertIn("DESK_TCLAW_RUNTIME_FAILED", init)
        self.assertIn('"audio_hal"', source)
        self.assertIn("int audio_hal_rc = desk_hardware_audio_get_status(&audio_status);", init)
        self.assertNotIn("int audio_hal_rc = rc;", init,
                         "TClaw errors outside the HAL must not be attributed to the board audio HAL")
        self.assertIn("record_stage(STAGE_TCLAW_CHAT", init)
        self.assertIn("STAGE_AUDIO_HAL].state != DESK_TCLAW_RUNTIME_READY", init,
                      "agent loop must remain blocked unless the independent HAL stage is ready")
        self.assertIn("s_voice_event_ui_enabled = (rc == OPRT_OK && audio_hal_ready);", init)
        self.assertLess(init.index("s_voice_event_ui_enabled = (rc == OPRT_OK && audio_hal_ready);"),
                        init.index("tclaw_chat_register_event_hook"),
                        "only attach the UI event listener after TClaw and board HAL both pass")

    def test_audio_hal_stage_is_visible_and_explicitly_not_voice_readiness(self) -> None:
        header = (ROOT / "firmware/main/include/tclaw_runtime_adapter.h").read_text(encoding="utf-8")
        cli = (ROOT / "firmware/components/desk_cli/src/desk_cli.c").read_text(encoding="utf-8")
        source = (ROOT / "firmware/main/src/tclaw_runtime_adapter.c").read_text(encoding="utf-8")

        self.assertIn("audio_hal", source)
        self.assertIn("desk_tclaw_runtime_stages", header)
        self.assertIn("native[i].name", cli)
        self.assertNotIn('"voice_ready"', source,
                         "input device initialization alone does not prove AFE/WakeNet readiness")
        self.assertIn("HAL only", source,
                      "runtime comments must limit the claim to board HAL status")


if __name__ == "__main__":
    unittest.main()
