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

        class NotificationStoreOps(ctypes.Structure):
            pass

        Load = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t,
                                ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p)
        Save = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint, ctypes.c_void_p, ctypes.c_size_t,
                                ctypes.c_void_p)
        Erase = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_uint, ctypes.c_void_p)
        NotificationStoreOps._fields_ = [
            ("load", Load), ("save", Save), ("erase", Erase), ("user_data", ctypes.c_void_p),
        ]

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

        cls.NotificationInput = NotificationInput
        cls.NotificationStatus = NotificationStatus
        cls.store_slots: list[bytes | None] = [None] * 16

        def load(slot: int, out_record: int, capacity: int, out_len: object, _user: int) -> int:
            data = cls.store_slots[slot]
            if data is None:
                out_len[0] = 0
                return 1
            if len(data) > capacity:
                return -4
            ctypes.memmove(out_record, data, len(data))
            out_len[0] = len(data)
            return 0

        def save(slot: int, record: int, record_len: int, _user: int) -> int:
            cls.store_slots[slot] = ctypes.string_at(record, record_len)
            return 0

        def erase(slot: int, _user: int) -> int:
            cls.store_slots[slot] = None
            return 0

        cls.store_callbacks = (Load(load), Save(save), Erase(erase))
        cls.store_ops = NotificationStoreOps(*cls.store_callbacks, None)
        cls.lib.desk_hardware_notification_register_store.argtypes = [
            ctypes.POINTER(NotificationStoreOps),
        ]
        cls.lib.desk_hardware_notification_register_store.restype = ctypes.c_int
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
        if cls.lib.desk_hardware_notification_register_store(ctypes.byref(cls.store_ops)) != 0:
            raise RuntimeError("native notification test store registration failed")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp_dir.cleanup()

    def setUp(self) -> None:
        self.store_slots[:] = [None] * len(self.store_slots)
        self.assertEqual(self.lib.desk_hardware_notification_init(None, None), 0)

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

    def test_notification_dismiss_propagates_busy_but_ack_reports_durable_success(self) -> None:
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
        # ACK success means its durable business update succeeded. A temporary
        # display BUSY is logged and retried by the regular UI processing loop.
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
