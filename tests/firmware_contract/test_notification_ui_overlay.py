"""Native contract tests for the volatile notification display overlay."""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone


ROOT = Path(__file__).resolve().parents[2]


class TestNotificationUiOverlay(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        compiler = os.environ.get("CC") or shutil.which("gcc")
        if not compiler:
            raise unittest.SkipTest("gcc is required for the native firmware contract harness")
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.lib_path = os.path.join(cls.temp_dir.name, "libdesk_ui_overlay_contract.so")
        sources = [
            ROOT / "firmware/components/desk_hardware/src/desk_arbiter.c",
            ROOT / "firmware/components/desk_hardware/src/desk_hardware.c",
            ROOT / "firmware/components/desk_hardware/src/desk_notification.c",
        ]
        command = [compiler, "-shared", "-fPIC", "-O2", "-Wall", "-Wextra",
                   "-D_POSIX_C_SOURCE=200809L",
                   f"-I{ROOT / 'firmware/components/desk_hardware/include'}",
                   *(str(source) for source in sources), "-lm", "-o", cls.lib_path]
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"native UI overlay contract compile failed: {result.stderr}")
        cls.lib = ctypes.CDLL(cls.lib_path)
        cls.lib.desk_arbiter_init.argtypes = []
        cls.lib.desk_arbiter_init.restype = ctypes.c_int
        cls.lib.desk_arbiter_acquire.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint32]
        cls.lib.desk_arbiter_acquire.restype = ctypes.c_int
        cls.lib.desk_arbiter_release.argtypes = [ctypes.c_int, ctypes.c_int]
        cls.lib.desk_arbiter_release.restype = None
        if cls.lib.desk_arbiter_init() != 0:
            raise RuntimeError("native display arbiter initialization failed")
        cls.lib.desk_hardware_set_screen_expression.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        cls.lib.desk_hardware_set_screen_expression.restype = ctypes.c_int
        cls.lib.desk_hardware_ui_overlay_present.argtypes = [
            ctypes.c_char_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint64,
            ctypes.POINTER(ctypes.c_uint32),
        ]
        cls.lib.desk_hardware_ui_overlay_present.restype = ctypes.c_int
        cls.lib.desk_hardware_ui_overlay_dismiss.argtypes = [ctypes.c_uint32]
        cls.lib.desk_hardware_ui_overlay_dismiss.restype = ctypes.c_int
        cls.lib.desk_hardware_ui_overlay_process_expiry.argtypes = []
        cls.lib.desk_hardware_ui_overlay_process_expiry.restype = ctypes.c_int
        cls.lib.desk_hardware_display_get_expression.argtypes = [
            ctypes.c_char_p, ctypes.c_size_t, ctypes.c_char_p, ctypes.c_size_t,
        ]
        cls.lib.desk_hardware_display_get_expression.restype = ctypes.c_int

        class NotificationInput(ctypes.Structure):
            _fields_ = [
                ("notification_id", ctypes.c_char * 65),
                ("reminder_id", ctypes.c_char * 65),
                ("occurrence_id", ctypes.c_char * 65),
                ("title", ctypes.c_char * 128),
                ("scheduled_at", ctypes.c_char * 32),
                ("expires_at", ctypes.c_char * 32),
                ("is_catchup", ctypes.c_bool),
            ]

        class NotificationStatus(ctypes.Structure):
            _fields_ = [
                ("notification_id", ctypes.c_char * 65),
                ("status", ctypes.c_int),
                ("error_code", ctypes.c_int),
                ("acknowledged", ctypes.c_bool),
                ("ack_source", ctypes.c_char * 32),
            ]

        class NotificationSummaryItem(ctypes.Structure):
            _fields_ = [
                ("reminder_id", ctypes.c_char_p),
                ("occurrence_id", ctypes.c_char_p),
                ("title", ctypes.c_char_p),
                ("scheduled_at", ctypes.c_char_p),
                ("timezone", ctypes.c_char_p),
                ("display_time", ctypes.c_char_p),
            ]

        class NotificationSummaryInput(ctypes.Structure):
            _fields_ = [
                ("summary_id", ctypes.c_char_p),
                ("batch_id", ctypes.c_char_p),
                ("created_at", ctypes.c_char_p),
                ("expires_at", ctypes.c_char_p),
                ("items", ctypes.POINTER(NotificationSummaryItem)),
                ("item_count", ctypes.c_size_t),
                ("omitted_count", ctypes.c_uint32),
            ]

        cls.NotificationInput = NotificationInput
        cls.NotificationStatus = NotificationStatus
        cls.NotificationSummaryItem = NotificationSummaryItem
        cls.NotificationSummaryInput = NotificationSummaryInput
        SummaryAck = ctypes.CFUNCTYPE(None, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_void_p)
        cls.summary_acks: list[tuple[bytes, bytes]] = []

        def summary_ack(summary_id: bytes, source: bytes, _user_data: int) -> None:
            cls.summary_acks.append((summary_id, source))

        cls.summary_ack_callback = SummaryAck(summary_ack)
        cls.lib.desk_hardware_notification_register_summary_ack_callback.argtypes = [
            SummaryAck, ctypes.c_void_p,
        ]
        cls.lib.desk_hardware_notification_register_summary_ack_callback.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_init.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        cls.lib.desk_hardware_notification_init.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_deliver.argtypes = [
            ctypes.POINTER(NotificationInput), ctypes.POINTER(NotificationStatus),
        ]
        cls.lib.desk_hardware_notification_deliver.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_process_pending.argtypes = []
        cls.lib.desk_hardware_notification_process_pending.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_acknowledge.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
        cls.lib.desk_hardware_notification_acknowledge.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_dismiss.argtypes = [ctypes.c_char_p]
        cls.lib.desk_hardware_notification_dismiss.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_get_status.argtypes = [
            ctypes.c_char_p, ctypes.POINTER(NotificationStatus),
        ]
        cls.lib.desk_hardware_notification_get_status.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_deliver_summary.argtypes = [
            ctypes.POINTER(NotificationSummaryInput),
        ]
        cls.lib.desk_hardware_notification_deliver_summary.restype = ctypes.c_int
        cls.lib.desk_hardware_notification_summary_acknowledge.argtypes = [ctypes.c_char_p,
                                                                           ctypes.c_char_p]
        cls.lib.desk_hardware_notification_summary_acknowledge.restype = ctypes.c_int

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.assertEqual(self.lib.desk_hardware_notification_init(None, None), 0)
        self.summary_acks.clear()
        self.assertEqual(self.lib.desk_hardware_notification_register_summary_ack_callback(
            self.summary_ack_callback, None), 0)

    @staticmethod
    def epoch_us() -> int:
        return int(time.time() * 1_000_000)

    def visible(self) -> tuple[str, str]:
        preset = ctypes.create_string_buffer(32)
        status = ctypes.create_string_buffer(64)
        self.assertEqual(self.lib.desk_hardware_display_get_expression(preset, 32, status, 64), 0)
        return preset.value.decode(), status.value.decode()

    def present(self, key: str, expiry_us: int | None = None) -> int:
        handle = ctypes.c_uint32()
        rc = self.lib.desk_hardware_ui_overlay_present(
            key.encode(), b"alert", b"Reminder", expiry_us or self.epoch_us() + 2_000_000,
            ctypes.byref(handle),
        )
        self.assertEqual(rc, 0)
        return handle.value

    def deliver_and_process_notification(self, key: str) -> None:
        now = datetime.now(timezone.utc)
        scheduled = (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        expires = (now + timedelta(seconds=5)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        item = self.NotificationInput(key.encode(), b"reminder", key.encode(), b"Take a break",
                                      scheduled.encode(), expires.encode(), False)
        status = self.NotificationStatus()
        self.assertEqual(self.lib.desk_hardware_notification_deliver(ctypes.byref(item),
                                                                     ctypes.byref(status)), 0)
        self.assertEqual(self.lib.desk_hardware_notification_process_pending(), 0)

    def test_base_updates_are_hidden_until_overlay_dismiss_and_latest_base_returns(self) -> None:
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"idle", b"Ready"), 0)
        handle = self.present("overlay-base")
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"thinking", b"Thinking"), 0)
        self.assertEqual(self.visible(), ("alert", "Reminder"))
        self.assertEqual(self.lib.desk_hardware_ui_overlay_dismiss(handle), 0)
        self.assertEqual(self.visible(), ("thinking", "Thinking"))

    def test_notification_init_is_ram_only_without_storage_provider(self) -> None:
        self.assertEqual(self.lib.desk_hardware_notification_init(None, None), 0)

    def test_summary_is_display_only_expiring_and_acknowledged_explicitly(self) -> None:
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"idle", b"Ready"), 0)
        now = datetime.now(timezone.utc)
        created = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode()
        expires = (now + timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode()
        display_time = b"2026-10-23 18:45 UTC+08:00"
        item = self.NotificationSummaryItem(b"reminder-1", b"occurrence-1", "吃药".encode(),
                                             created, b"Asia/Shanghai", display_time)
        items = (self.NotificationSummaryItem * 1)(item)
        summary_id = ("s:" + "x" * 126).encode()
        summary = self.NotificationSummaryInput(summary_id, b"batch-001", created,
                                                expires, items, 1, 2)
        self.assertEqual(self.lib.desk_arbiter_acquire(2, 4, 0), 0)
        self.assertEqual(self.lib.desk_hardware_notification_deliver_summary(
            ctypes.byref(summary)), 0)
        self.assertEqual(self.lib.desk_hardware_notification_deliver_summary(
            ctypes.byref(summary)), 0, "same summary id is idempotent in this boot session")
        self.assertEqual(self.lib.desk_hardware_notification_process_pending(), 0,
                         "summary presentation must not acquire/play audio")
        self.assertEqual(self.visible()[0], "alert")
        self.assertIn("Missed", self.visible()[1])
        self.assertIn(display_time.decode(), self.visible()[1],
                      "render the Host generated local display time verbatim")
        self.assertEqual(self.lib.desk_hardware_notification_summary_acknowledge(
            summary_id, b"explicit_ui"), 0)
        self.assertEqual(self.lib.desk_hardware_notification_summary_acknowledge(
            summary_id, b"explicit_ui"), 0)
        self.assertEqual(self.summary_acks, [(summary_id, b"explicit_ui")],
                         "only the first explicit ACK for this volatile ID emits the callback")
        self.assertEqual(self.visible(), ("idle", "Ready"))
        self.lib.desk_arbiter_release(2, 4)

    def test_summary_expiry_and_ram_reset_allow_same_id_redelivery(self) -> None:
        now = datetime.now(timezone.utc)
        created = (now - timedelta(hours=25)).strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode()
        expires = (now - timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode()
        item = self.NotificationSummaryItem(b"r", b"o", b"Old item", created, b"UTC",
                                             b"2026-10-23 18:45 UTC+00:00")
        items = (self.NotificationSummaryItem * 1)(item)
        summary = self.NotificationSummaryInput(b"summary:expired", b"batch", created,
                                                expires, items, 1, 0)
        self.assertEqual(self.lib.desk_hardware_notification_deliver_summary(
            ctypes.byref(summary)), -3)

    def test_summary_display_time_validation_and_utf8_safe_truncation(self) -> None:
        now = datetime.now(timezone.utc)
        created = now.strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode()
        expires = (now + timedelta(hours=23)).strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode()
        display_time = b"2026-10-23 18:45 UTC+08:00"
        item = self.NotificationSummaryItem(b"r", b"o", ("界" * 50).encode(), created,
                                             b"Asia/Shanghai", display_time)
        items = (self.NotificationSummaryItem * 1)(item)
        summary = self.NotificationSummaryInput(b"summary:utf8", b"batch", created, expires,
                                                items, 1, 2)
        self.assertEqual(self.lib.desk_hardware_notification_deliver_summary(
            ctypes.byref(summary)), 0)
        self.assertEqual(self.lib.desk_hardware_notification_process_pending(), 0)
        rendered = self.visible()[1]
        rendered.encode("utf-8").decode("utf-8")
        self.assertIn(display_time.decode(), rendered)
        self.assertIn(" +2", rendered)
        self.assertNotIn("�", rendered)

        malformed = self.NotificationSummaryItem(b"r2", b"o2", b"Invalid time", created,
                                                  b"Asia/Shanghai", b"2026-10-23 18:45 UTC+8")
        malformed_items = (self.NotificationSummaryItem * 1)(malformed)
        rejected = self.NotificationSummaryInput(b"summary:bad-time", b"batch", created,
                                                 expires, malformed_items, 1, 0)
        self.assertEqual(self.lib.desk_hardware_notification_deliver_summary(
            ctypes.byref(rejected)), -1)

    def test_occurrence_capacity_is_bounded_and_init_clears_session_dedupe(self) -> None:
        now = datetime.now(timezone.utc)
        scheduled = (now - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode()
        expires = (now + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.%fZ").encode()
        for i in range(16):
            key = f"bounded-{i}".encode()
            item = self.NotificationInput(key, b"reminder", key, b"Take a break",
                                          scheduled, expires, False)
            status = self.NotificationStatus()
            self.assertEqual(self.lib.desk_hardware_notification_deliver(
                ctypes.byref(item), ctypes.byref(status)), 0)
        key = b"bounded-overflow"
        item = self.NotificationInput(key, b"reminder", key, b"Take a break",
                                      scheduled, expires, False)
        status = self.NotificationStatus()
        self.assertEqual(self.lib.desk_hardware_notification_deliver(
            ctypes.byref(item), ctypes.byref(status)), -5)
        self.assertEqual(self.lib.desk_hardware_notification_init(None, None), 0)
        self.assertEqual(self.lib.desk_hardware_notification_deliver(
            ctypes.byref(item), ctypes.byref(status)), 0,
                         "init/reboot clears RAM dedupe and permits same-ID redelivery")

    def test_single_overlay_idempotency_busy_and_stale_generation(self) -> None:
        first = self.present("overlay-first")
        duplicate = ctypes.c_uint32()
        self.assertEqual(self.lib.desk_hardware_ui_overlay_present(
            b"overlay-first", b"alert", b"Changed", self.epoch_us() + 5_000_000,
            ctypes.byref(duplicate)), 0)
        self.assertEqual(duplicate.value, first)
        self.assertEqual(self.visible(), ("alert", "Reminder"),
                         "duplicate ID must preserve the current overlay payload")

        other = ctypes.c_uint32()
        self.assertEqual(self.lib.desk_hardware_ui_overlay_present(
            b"overlay-other", b"alert", b"Other", self.epoch_us() + 2_000_000,
            ctypes.byref(other)), -5)
        self.assertEqual(self.lib.desk_hardware_ui_overlay_dismiss(first), 0)
        second = self.present("overlay-other")
        self.assertNotEqual(second, first)
        self.assertEqual(self.lib.desk_hardware_ui_overlay_dismiss(first), -2)
        self.assertEqual(self.visible(), ("alert", "Reminder"))
        self.assertEqual(self.lib.desk_hardware_ui_overlay_dismiss(second), 0)

    def test_duplicate_does_not_extend_deadline_and_expiry_restores_latest_base(self) -> None:
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"idle", b"Ready"), 0)
        original_expiry = self.epoch_us() + 250_000
        first = self.present("overlay-expiry", original_expiry)
        duplicate = ctypes.c_uint32()
        self.assertEqual(self.lib.desk_hardware_ui_overlay_present(
            b"overlay-expiry", b"alert", b"Reminder", self.epoch_us() + 3_000_000,
            ctypes.byref(duplicate)), 0)
        self.assertEqual(duplicate.value, first)
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"speaking", b"Speaking"), 0)
        time.sleep(0.32)
        self.assertEqual(self.lib.desk_hardware_ui_overlay_process_expiry(), 0)
        self.assertEqual(self.visible(), ("speaking", "Speaking"))
        rejected = ctypes.c_uint32()
        self.assertEqual(self.lib.desk_hardware_ui_overlay_present(
            b"overlay-expiry", b"alert", b"Late duplicate", self.epoch_us() + 3_000_000,
            ctypes.byref(rejected)), -3,
        "an expired ID cannot be revived by a later duplicate with a longer deadline")
        self.assertEqual(self.visible(), ("speaking", "Speaking"))

    def test_dismiss_busy_keeps_base_render_dirty_for_existing_loop_retry(self) -> None:
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"idle", b"Ready"), 0)
        handle = self.present("overlay-dismiss-busy")
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"thinking", b"Latest base"), 0)
        # SYSTEM_ALERT outranks the base renderer (TCLAW_VOICE), forcing its
        # restoration render to return BUSY after dismiss closes the overlay.
        self.assertEqual(self.lib.desk_arbiter_acquire(4, 5, 0), 0)
        self.assertEqual(self.lib.desk_hardware_ui_overlay_dismiss(handle), -5)
        self.assertEqual(self.visible(), ("alert", "Reminder"))
        self.lib.desk_arbiter_release(4, 5)
        self.assertEqual(self.lib.desk_hardware_ui_overlay_process_expiry(), 0)
        self.assertEqual(self.visible(), ("thinking", "Latest base"))

    def test_expiry_busy_keeps_base_render_dirty_for_existing_loop_retry(self) -> None:
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"idle", b"Ready"), 0)
        handle = self.present("overlay-expiry-busy", self.epoch_us() + 220_000)
        self.assertTrue(handle)
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"speaking", b"Latest base"), 0)
        self.assertEqual(self.lib.desk_arbiter_acquire(4, 5, 0), 0)
        time.sleep(0.28)
        self.assertEqual(self.lib.desk_hardware_ui_overlay_process_expiry(), -5)
        self.assertEqual(self.visible(), ("alert", "Reminder"))
        self.lib.desk_arbiter_release(4, 5)
        self.assertEqual(self.lib.desk_hardware_ui_overlay_process_expiry(), 0)
        self.assertEqual(self.visible(), ("speaking", "Latest base"))

    def test_notification_ack_and_dismiss_close_only_overlay_and_keep_business_phase(self) -> None:
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"thinking", b"Thinking"), 0)
        self.deliver_and_process_notification("overlay-dismiss")
        self.assertEqual(self.visible(), ("alert", "Take a break"))
        self.assertEqual(self.lib.desk_hardware_notification_dismiss(b"overlay-dismiss"), 0)
        self.assertEqual(self.visible(), ("thinking", "Thinking"))
        status = self.NotificationStatus()
        self.assertEqual(self.lib.desk_hardware_notification_get_status(b"overlay-dismiss",
                                                                        ctypes.byref(status)), 0)
        self.assertFalse(status.acknowledged)
        self.assertEqual(status.status, 5, "display dismiss must leave delivery status UNKNOWN")

        self.deliver_and_process_notification("overlay-ack")
        self.assertEqual(self.visible(), ("alert", "Take a break"))
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"speaking", b"Speaking"), 0)
        self.assertEqual(self.lib.desk_hardware_notification_acknowledge(b"overlay-ack", b"explicit_ui"), 0)
        self.assertEqual(self.visible(), ("speaking", "Speaking"))
        status = self.NotificationStatus()
        self.assertEqual(self.lib.desk_hardware_notification_get_status(b"overlay-ack",
                                                                        ctypes.byref(status)), 0)
        self.assertTrue(status.acknowledged)
        self.assertEqual(status.status, 5, "ACK must not infer reminder completion or played")

    def test_notification_dismiss_propagates_busy_but_ack_is_session_local(self) -> None:
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"idle", b"Ready"), 0)
        self.deliver_and_process_notification("overlay-busy-dismiss")
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"thinking", b"Latest base"), 0)
        self.assertEqual(self.lib.desk_arbiter_acquire(4, 5, 0), 0)
        self.assertEqual(self.lib.desk_hardware_notification_dismiss(b"overlay-busy-dismiss"), -5)
        status = self.NotificationStatus()
        self.assertEqual(self.lib.desk_hardware_notification_get_status(b"overlay-busy-dismiss",
                                                                        ctypes.byref(status)), 0)
        self.assertFalse(status.acknowledged)
        self.lib.desk_arbiter_release(4, 5)
        self.assertEqual(self.lib.desk_hardware_ui_overlay_process_expiry(), 0)
        self.assertEqual(self.visible(), ("thinking", "Latest base"))

        self.deliver_and_process_notification("overlay-busy-ack")
        self.assertEqual(self.lib.desk_hardware_set_screen_expression(b"speaking", b"Latest ACK base"), 0)
        self.assertEqual(self.lib.desk_arbiter_acquire(4, 5, 0), 0)
        # ACK is only a volatile user action; a temporary display BUSY is retried
        # by the regular UI processing loop.
        self.assertEqual(self.lib.desk_hardware_notification_acknowledge(
            b"overlay-busy-ack", b"explicit_ui"), 0)
        status = self.NotificationStatus()
        self.assertEqual(self.lib.desk_hardware_notification_get_status(b"overlay-busy-ack",
                                                                        ctypes.byref(status)), 0)
        self.assertTrue(status.acknowledged)
        self.lib.desk_arbiter_release(4, 5)
        self.assertEqual(self.lib.desk_hardware_ui_overlay_process_expiry(), 0)
        self.assertEqual(self.visible(), ("speaking", "Latest ACK base"))

    def test_notification_wiring_uses_overlay_without_blending_ack_semantics(self) -> None:
        source = (ROOT / "firmware/components/desk_hardware/src/desk_notification.c").read_text(
            encoding="utf-8"
        )
        ack = source.split("int desk_hardware_notification_acknowledge", 1)[1].split(
            "int desk_hardware_notification_dismiss", 1
        )[0]
        ack_helper = source.split("static void dismiss_active_overlay_for_id", 1)[1].split(
            "static void fill_status", 1
        )[0]
        dismiss = source.split("int desk_hardware_notification_dismiss", 1)[1].split(
            "int desk_hardware_notification_get_status", 1
        )[0]
        processor = source.split("int desk_hardware_notification_process_pending", 1)[1].split(
            "int desk_hardware_notification_acknowledge", 1
        )[0]
        self.assertIn("desk_hardware_ui_overlay_present", processor)
        self.assertIn("dismiss_active_overlay_for_id(notification_id)", ack)
        self.assertIn("desk_hardware_ui_overlay_dismiss", ack_helper)
        self.assertIn("desk_hardware_ui_overlay_dismiss", dismiss)
        self.assertNotIn("pending = 0", dismiss,
                         "display dismiss must not imply delivery or reminder completion")
        self.assertNotIn("acknowledged = 1", dismiss,
                         "display dismiss must not create an ACK")


if __name__ == "__main__":
    unittest.main()
