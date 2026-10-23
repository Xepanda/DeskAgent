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


def diagnostic_event(component: str, event_id: str, fields: dict, sequence: int = 1) -> dict:
    event = camera_empty(sequence)
    event["data"].update(component=component, event_id=event_id, fields=fields)
    return event


def queue_full_gap(first: int = 3, last: int = 5, count: int = 3) -> dict:
    return {
        "type": "sys.log.gap",
        "data": {
            "log_schema_version": "1.0.0",
            "stream_id": "logstream_01",
            "boot_id": "boot-opaque-id",
            "first_dropped_sequence": first,
            "last_dropped_sequence": last,
            "dropped_count": count,
            "reason": "QUEUE_FULL",
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

    def test_accepts_all_cr033_events_with_exact_fields(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        events = [
            camera_empty(),
            diagnostic_event("audio", "AUDIO_DRIVER_STATE_SNAPSHOT", {"state": "READY"}, 2),
            diagnostic_event("audio", "AUDIO_DRIVER_STATE_SNAPSHOT", {"state": "NOT_READY"}, 3),
            diagnostic_event("network", "TUYA_MQTT_STATE", {"state": "CONNECTED"}, 4),
            diagnostic_event("network", "TUYA_MQTT_STATE", {"state": "DISCONNECTED"}, 5),
            diagnostic_event("stm32", "STM32_ACK_TIMEOUT", {}, 6),
        ]
        for event in events:
            with self.subTest(event=event):
                self.assertTrue(consumer.ingest(event, authorized=True))
        self.assertEqual(consumer.drain(), events)

    def test_rejects_cr033_wrong_component_level_fields_and_state(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        invalid = [
            diagnostic_event("camera", "AUDIO_DRIVER_STATE_SNAPSHOT", {"state": "READY"}),
            diagnostic_event("audio", "AUDIO_DRIVER_STATE_SNAPSHOT", {"state": "READY", "extra": 1}),
            diagnostic_event("audio", "AUDIO_DRIVER_STATE_SNAPSHOT", {"state": "ready"}),
            diagnostic_event("audio", "AUDIO_DRIVER_STATE_SNAPSHOT", {"state": 1}),
            diagnostic_event("network", "TUYA_MQTT_STATE", {"state": "CONNECTING"}),
            diagnostic_event("network", "TUYA_MQTT_STATE", {"state": "CONNECTED", "ip": "192.0.2.1"}),
            diagnostic_event("stm32", "STM32_ACK_TIMEOUT", {"command": "move"}),
        ]
        for event in invalid:
            with self.subTest(event=event):
                self.assertFalse(consumer.ingest(event, authorized=True))
        self.assertEqual(consumer.snapshot()["rejected"], len(invalid))
        self.assertEqual(consumer.drain(), [])

    def test_cr033_events_remain_unaccepted_without_enable_and_authorization(self) -> None:
        event = diagnostic_event("network", "TUYA_MQTT_STATE", {"state": "CONNECTED"})
        default_off = NetworkLogConsumer()
        self.assertFalse(default_off.ingest(event, authorized=True))
        enabled = NetworkLogConsumer(enabled=True)
        self.assertFalse(enabled.ingest(event, authorized=False))
        self.assertFalse(enabled.ingest(event, authorized="true"))
        self.assertEqual(enabled.snapshot()["queued"], 0)

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

    def test_accepts_only_queue_full_gap_with_exact_contiguous_range(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        gap = queue_full_gap()
        self.assertTrue(consumer.ingest(gap, authorized=True))
        self.assertEqual(consumer.drain(), [gap])

    def test_rejects_unknown_gap_reason_schema_fields_and_inconsistent_ranges(self) -> None:
        consumer = NetworkLogConsumer(enabled=True)
        base = queue_full_gap()
        invalid = [
            {**base, "data": {**base["data"], "reason": "TEST_REASON"}},
            {**base, "data": {**base["data"], "reason": "DISCONNECTED"}},
            {**base, "data": {**base["data"], "log_schema_version": "2.0.0"}},
            {**base, "data": {**base["data"], "extra": "raw log"}},
            {**base, "data": {**base["data"], "dropped_count": 2}},
            {**base, "data": {**base["data"], "dropped_count": 4}},
            {**base, "data": {**base["data"], "last_dropped_sequence": 2}},
            {**base, "data": {**base["data"], "first_dropped_sequence": 0}},
            {**base, "data": {**base["data"], "dropped_count": True}},
        ]
        for msg in invalid:
            with self.subTest(message=msg):
                self.assertFalse(consumer.ingest(msg, authorized=True))
        self.assertEqual(consumer.snapshot()["rejected"], len(invalid))
        self.assertEqual(consumer.drain(), [])

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
