from __future__ import annotations

import unittest

from host.desk_agent.services.network_log_consumer import NetworkLogConsumer


def camera_empty(sequence: int = 1) -> dict:
    return {
        "type": "sys.log.event",
        "data": {
            "log_schema_version": "1.0.0",
            "stream_id": "logstream_01",
            "boot_id": "boot-opaque-id",
            "sequence": sequence,
            "uptime_ms": 123456,
            "level": "WARN",
            "component": "camera",
            "event_id": "CAMERA_CAPTURE_EMPTY",
            "fields": {},
        },
    }


class NetworkLogConsumerTests(unittest.TestCase):
    def test_disabled_by_default_and_requires_authorized_delivery(self) -> None:
        consumer = NetworkLogConsumer()
        self.assertFalse(consumer.ingest(camera_empty(), authorized=True))
        self.assertEqual(consumer.snapshot()["queued"], 0)

        enabled = NetworkLogConsumer(enabled=True)
        self.assertFalse(enabled.ingest(camera_empty(), authorized=False))
        self.assertFalse(enabled.ingest(camera_empty(), authorized="yes"))
        self.assertEqual(enabled.snapshot()["unauthorized"], 2)
        self.assertEqual(enabled.drain(), [])

    def test_accepts_only_exact_camera_empty_schema_and_scrubs_unknown_fields(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        msg = camera_empty()
        msg["data"]["private"] = "must-not-be-retained"
        self.assertFalse(consumer.ingest(msg, authorized=True))

        self.assertTrue(consumer.ingest(camera_empty(), authorized=True))
        stored = consumer.drain()
        self.assertEqual(stored, [camera_empty()])
        self.assertNotIn("private", str(stored))

    def test_rejects_unknown_event_version_level_component_and_nonempty_fields(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        invalid = []
        for key, value in (
            ("log_schema_version", "2.0.0"),
            ("level", "TRACE"),
            ("component", "audio"),
            ("event_id", "INPUT_CHANNEL_INIT_ERROR"),
            ("fields", {"message": "raw log"}),
        ):
            msg = camera_empty()
            msg["data"][key] = value
            invalid.append(msg)
        invalid.extend([
            {"type": "tool.request", "data": {"secret": "x"}},
            {"type": "sys.log.event", "data": {"log_schema_version": "1.0.0"}},
        ])
        for msg in invalid:
            with self.subTest(message=msg):
                self.assertFalse(consumer.ingest(msg, authorized=True))
        self.assertEqual(consumer.snapshot()["rejected"], len(invalid))

    def test_gap_is_rejected_until_contract_reason_enum_is_frozen(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        gap = {
            "type": "sys.log.gap",
            "data": {
                "log_schema_version": "1.0.0",
                "stream_id": "logstream_01",
                "boot_id": "boot-opaque-id",
                "first_dropped_sequence": 3,
                "last_dropped_sequence": 5,
                "dropped_count": 3,
                "reason": "TEST_REASON",
            },
        }
        # CR-009 requires a closed reason enum but does not list its members.
        # The consumer therefore fails closed until A0 supplies that allowlist.
        self.assertFalse(consumer.ingest(gap, authorized=True))
        self.assertEqual(consumer.drain(), [])

        for reason in ("TEST_REASON", "QUEUE_FULL", "buffer overflow", "x" * 100):
            with self.subTest(reason=reason):
                candidate = {**gap, "data": {**gap["data"], "reason": reason}}
                self.assertFalse(consumer.ingest(candidate, authorized=True))
        inconsistent = {**gap, "data": {**gap["data"], "dropped_count": 4}}
        self.assertFalse(consumer.ingest(inconsistent, authorized=True))

    def test_queue_drops_newest_and_close_discards_buffer(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        for sequence in range(1, 17):
            self.assertTrue(consumer.ingest(camera_empty(sequence), authorized=True))
        self.assertFalse(consumer.ingest(camera_empty(17), authorized=True))
        self.assertEqual(consumer.snapshot()["dropped"], 1)
        self.assertEqual(len(consumer.drain()), 16)

        consumer.ingest(camera_empty(18), authorized=True)
        consumer.close()
        self.assertEqual(consumer.drain(), [])
        self.assertFalse(consumer.ingest(camera_empty(19), authorized=True))
        self.assertTrue(consumer.snapshot()["closed"])

    def test_session_reset_discards_old_events(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        consumer.ingest(camera_empty(), authorized=True)
        consumer.reset_session()
        self.assertEqual(consumer.drain(), [])


if __name__ == "__main__":
    unittest.main()
