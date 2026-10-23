"""Source contract for the Watcher TX DMA drain fence (L2 only)."""

from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
SOURCE = ROOT / "firmware/components/desk_hardware/src/desk_watcher_audio.c"
HEADER = ROOT / "firmware/components/desk_hardware/include/desk_watcher_audio.h"
HARDWARE_SOURCE = ROOT / "firmware/components/desk_hardware/src/desk_hardware.c"
NOTIFICATION_SOURCE = ROOT / "firmware/components/desk_hardware/src/desk_notification.c"


class TestWatcherAudioTxDrainContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = SOURCE.read_text(encoding="utf-8")

    def test_explicit_mono_dma_ring_size_matches_final_watcher_tx_configuration(self) -> None:
        for expression in (
            r"#define\s+TX_DMA_DESC_NUM\s+6u",
            r"#define\s+TX_DMA_FRAME_NUM\s+240u",
            r"#define\s+TX_DMA_ACTIVE_SLOT_NUM\s+1u",
            r"#define\s+TX_DMA_BYTES_PER_SAMPLE\s+2u",
        ):
            with self.subTest(expression=expression):
                self.assertRegex(self.source, expression)
        self.assertIn("channel.dma_desc_num = TX_DMA_DESC_NUM;", self.source)
        self.assertIn("channel.dma_frame_num = TX_DMA_FRAME_NUM;", self.source)
        self.assertIn("tx_slot.slot_mask = I2S_STD_SLOT_BOTH;", self.source)
        self.assertIn("info.total_dma_buf_size != TX_DMA_DESC_NUM * TX_DMA_DESC_BYTES", self.source)

    def test_callback_is_tx_only_registered_before_enable_and_counts_valid_eof(self) -> None:
        self.assertRegex(self.source, r"\.on_sent\s*=\s*tx_on_sent")
        self.assertLess(self.source.index("i2s_channel_register_event_callback(s_tx"),
                        self.source.index("i2s_channel_enable(s_tx)"))
        callback = self.source.split("static bool IRAM_ATTR tx_on_sent", 1)[1].split("static uint32_t tx_eof_sequence_get", 1)[0]
        self.assertIn("event->size != TX_DMA_DESC_BYTES", callback)
        self.assertIn("++s_tx_eof_sequence", callback)
        self.assertIn("portENTER_CRITICAL_ISR", callback)

    def test_drain_writes_full_silence_ring_then_waits_for_new_ring_of_eofs(self) -> None:
        drain = self.source.split("int desk_watcher_audio_tx_drain", 1)[1].split("static void mic_task", 1)[0]
        self.assertIn("xSemaphoreTake(s_write_lock", drain)
        self.assertIn("static const uint8_t silence[TX_DMA_DRAIN_BYTES] = {0};", drain)
        self.assertIn("esp_codec_dev_write(s_speaker, (void *)silence, sizeof(silence))", drain)
        self.assertIn("uint32_t start = tx_eof_sequence_get", drain)
        self.assertIn("(uint32_t)(current - start) >= TX_DMA_DESC_NUM", drain)
        self.assertIn("TX_DMA_DRAIN_TIMEOUT_MS", self.source)
        self.assertNotIn("i2s_channel_disable", drain)
        self.assertIn("acoustic output is physically silent", self.source)
        self.assertIn("s_tx_eof_armed = true", self.source)

    def test_header_exposes_bounded_drain_operation(self) -> None:
        header = HEADER.read_text(encoding="utf-8")
        self.assertRegex(header, r"int\s+desk_watcher_audio_tx_drain\(uint32_t timeout_ms\)")

    def test_sequence_delta_is_wrap_safe_and_failed_init_is_not_retried_in_place(self) -> None:
        self.assertIn("(uint32_t)(current - start) >= TX_DMA_DESC_NUM", self.source)
        init = self.source.split("int desk_watcher_audio_init(void)", 1)[1]
        self.assertIn("if (s_attempted) return DESK_HW_ERR_UNAVAILABLE;", init)
        self.assertIn("tx_eof_arm_for_final_configuration();", init)
        self.assertLess(init.index("tx_eof_arm_for_final_configuration();"),
                        init.index("i2s_channel_enable(s_tx)", init.index("tx_eof_arm_for_final_configuration();")))
        self.assertIn("A reboot is required after a physical init failure", self.source)

    def test_normal_playback_checks_owner_and_serializes_tx_writes(self) -> None:
        play = self.source.split("int desk_watcher_audio_play(", 1)[1].split("static bool IRAM_ATTR tx_on_sent", 1)[0]
        self.assertIn("desk_arbiter_get_owner(DESK_RES_AUDIO_PLAYBACK)", play)
        self.assertIn("xSemaphoreTake(s_write_lock", play)
        self.assertIn("xTaskGetCurrentTaskHandle() != stream_task", play)

    def test_tclaw_owner_spans_all_play_chunks_until_play_stop_drain(self) -> None:
        native_play = self.source.split("static OPERATE_RET native_play(", 1)[1].split(
            "static OPERATE_RET native_config", 1)[0]
        self.assertIn("desk_arbiter_acquire(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE", native_play)
        self.assertNotIn("desk_arbiter_release", native_play)
        config = self.source.split("static OPERATE_RET native_config(", 1)[1].split(
            "static OPERATE_RET native_close", 1)[0]
        stop = config.split("if (command == TDD_AUDIO_CMD_PLAY_STOP)", 1)[1].split("if (command !=", 1)[0]
        self.assertIn("desk_watcher_audio_tx_drain", stop)
        self.assertLess(stop.index("desk_watcher_audio_tx_drain"),
                        stop.index("desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE)"))
        self.assertIn("if (drain_rc == DESK_HW_OK)", stop)
        self.assertIn("return OPRT_RESOURCE_NOT_READY", stop)
        self.assertLess(stop.index("if (!s_tclaw_playback_session_active)"),
                        stop.index("desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_TCLAW_VOICE)"))

    def test_alert_preemption_drains_tclaw_session_without_releasing_on_failure(self) -> None:
        preempt = self.source.split("static bool audio_preempt_quiesce(", 1)[1].split(
            "int desk_watcher_audio_play_stop", 1)[0]
        tclaw_alert = preempt.split("current_owner == DESK_OWNER_TCLAW_VOICE", 1)[1]
        self.assertIn("requester == DESK_OWNER_SYSTEM_ALERT", tclaw_alert)
        self.assertIn("desk_watcher_audio_tx_drain(timeout_ms)", tclaw_alert)
        self.assertIn("return drain_rc == DESK_HW_OK", tclaw_alert)
        self.assertNotIn("desk_arbiter_release", tclaw_alert)
        self.assertIn("return false", tclaw_alert)

    def test_one_shot_public_playback_drains_before_return_and_notification_release(self) -> None:
        hardware = HARDWARE_SOURCE.read_text(encoding="utf-8")
        play = hardware.split("int desk_hardware_audio_play_pcm(", 1)[1].split(
            "int desk_hardware_audio_play_stop", 1)[0]
        self.assertIn("desk_watcher_audio_play_one_shot", play)
        self.assertLess(play.index("desk_watcher_audio_play_one_shot"), play.index("s_audio_play_cb"))
        one_shot = self.source.split("int desk_watcher_audio_play_one_shot(", 1)[1].split(
            "static bool IRAM_ATTR tx_on_sent", 1)[0]
        self.assertLess(one_shot.index("desk_watcher_audio_play(pcm"),
                        one_shot.index("desk_watcher_audio_tx_drain"))
        notification = NOTIFICATION_SOURCE.read_text(encoding="utf-8")
        audio = notification.split("int audio_rc = desk_hardware_audio_play_pcm", 1)[1].split(
            "entry->pending = 0", 1)[0]
        self.assertIn("if (audio_rc == DESK_HW_OK)", audio)
        self.assertIn("desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE)", audio)

    def test_diagnostic_tone_keeps_sdk_owner_when_drain_fails(self) -> None:
        tone = self.source.split("int desk_watcher_audio_test_tone(void)", 1)[1].split(
            "int desk_watcher_audio_init(void)", 1)[0]
        self.assertLess(tone.index("desk_watcher_audio_tx_drain"),
                        tone.rindex("desk_arbiter_release(DESK_RES_AUDIO_PLAYBACK, DESK_OWNER_SDK_REMOTE)"))
        self.assertIn("if (drain_rc == DESK_HW_OK)", tone)
        self.assertIn("xSemaphoreTake(s_one_shot_lock", tone)


if __name__ == "__main__":
    unittest.main()
