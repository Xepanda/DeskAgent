from __future__ import annotations

import io
import struct
import unittest
from pathlib import Path
import sys
import threading
import time

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from host.desk_agent.connectors.robot.official_camera_stream_ingress import (  # noqa: E402
    OfficialCameraStreamIngress,
    _estimate_host_minus_device_offset,
    install_raw_device_frame_observer,
)
from host.desk_agent.main import DeskAgentHostApp  # noqa: E402


def jpeg_fixture() -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (640, 480), (19, 87, 143)).save(output, format="JPEG", quality=85)
    return output.getvalue()


def packet(jpeg: bytes, *, stream_id: int = 7, sequence: int = 1,
           timestamp_ms: int = 1000, flags: int = 0, frame_type: int = 3,
           inner_sequence: int | None = None) -> bytes:
    wjpg = struct.pack("<4sBBHIII", b"WJPG", 1, 0, 20,
                       sequence if inner_sequence is None else inner_sequence,
                       timestamp_ms, len(jpeg)) + jpeg
    return struct.pack("<4sBBHII", b"WSPK", frame_type, flags, stream_id,
                       sequence, len(wjpg)) + wjpg


class FakeSender:
    def __init__(self):
        self.commands: list[tuple[str, dict]] = []
        self.stream_id = 7
        self.device_ms = 1000
        self.fail_ping = False

    def __call__(self, command_type: str, data: dict, timeout: float | None):
        self.commands.append((command_type, dict(data)))
        if command_type == "ctrl.camera.stream.start":
            return {"type": "sys.ack", "code": 0,
                    "data": {"stream_id": self.stream_id,
                             "device_monotonic_ms": self.device_ms}}
        if command_type == "ctrl.camera.clock.ping":
            if self.fail_ping:
                return {"type": "sys.nack", "code": 1, "data": {}}
            host_ms = data["host_send_monotonic_ms"]
            time.sleep(0.01)
            return {"type": "sys.ack", "code": 0,
                    "data": {"ping_id": data["ping_id"],
                             "device_receive_monotonic_ms": (host_ms + 5) & 0xFFFFFFFF,
                             "device_send_monotonic_ms": (host_ms + 6) & 0xFFFFFFFF}}
        return {"type": "sys.ack", "code": 0, "data": {}}


class OfficialCameraStreamIngressTests(unittest.TestCase):
    def make_ingress(self, *, valid_owner=lambda owner: True, capability=True):
        sender = FakeSender()
        delivered: list[tuple[bytes, dict]] = []
        ingress = OfficialCameraStreamIngress(
            send_command=sender,
            ingest_frame=lambda jpeg, metadata: delivered.append((jpeg, metadata)),
            owner_is_valid=valid_owner,
            capabilities=["camera.stream.mjpeg.wspk.v1"] if capability else [],
            session_generation="paired-run-1",
            clock_ping_interval=3600,
        )
        self.addCleanup(ingress.close)
        return ingress, sender, delivered

    def test_valid_raw_image_frame_delivers_decodable_640x480_jpeg_and_metadata(self):
        ingress, _sender, delivered = self.make_ingress()
        self.assertTrue(ingress.start("viewer-a", 10000)["started"])
        jpeg = jpeg_fixture()
        ingress.on_application_frame("device", packet(jpeg))
        self.assertEqual(len(delivered), 1)
        self.assertEqual(delivered[0][0], jpeg)
        self.assertEqual(delivered[0][1]["sequence"], 1)
        self.assertEqual(delivered[0][1]["stream_id"], 7)
        self.assertEqual(delivered[0][1]["width"], 640)
        self.assertEqual(delivered[0][1]["height"], 480)
        with Image.open(io.BytesIO(delivered[0][0])) as decoded:
            decoded.verify()
        with Image.open(io.BytesIO(delivered[0][0])) as decoded:
            self.assertEqual(decoded.size, (640, 480))

    def test_malformed_and_oversized_frames_are_dropped(self):
        ingress, _sender, delivered = self.make_ingress()
        ingress.start("viewer-a", 10000)
        good = jpeg_fixture()
        ingress.on_application_frame("device", b"WSPK" + bytes(20))
        ingress.on_application_frame("device", packet(b"\xff\xd8bad\xff\xd9"))
        ingress.on_application_frame("device", packet(good, flags=1))
        oversized = struct.pack("<4sBBHII", b"WSPK", 3, 0, 7, 1, 131073)
        ingress.on_application_frame("device", oversized + bytes(16))
        self.assertEqual(delivered, [])
        self.assertGreaterEqual(ingress.drop_count, 4)

    def test_sequence_mismatch_or_rollback_and_timestamp_rollback_drop(self):
        ingress, _sender, delivered = self.make_ingress()
        ingress.start("viewer-a", 10000)
        jpeg = jpeg_fixture()
        ingress.on_application_frame("device", packet(jpeg, sequence=10, timestamp_ms=10))
        ingress.on_application_frame("device", packet(jpeg, sequence=11, timestamp_ms=11))
        ingress.on_application_frame("device", packet(jpeg, sequence=13, timestamp_ms=12))
        ingress.on_application_frame("device", packet(jpeg, sequence=9, timestamp_ms=13))
        ingress.on_application_frame("device", packet(jpeg, sequence=12, timestamp_ms=11))
        ingress.on_application_frame("device", packet(jpeg, sequence=14, timestamp_ms=14,
                                                       inner_sequence=15))
        self.assertEqual([m["sequence"] for _, m in delivered], [10, 11, 13])

    def test_lease_renewal_keeps_stream_generation_and_clock_ping_uses_four_timestamps(self):
        ingress, sender, _delivered = self.make_ingress()
        self.assertTrue(ingress.acquire("viewer-a", 10000)["granted"])
        self.assertTrue(ingress.acquire("viewer-a", 10000)["granted"])
        commands = [name for name, _ in sender.commands]
        self.assertEqual(commands.count("ctrl.camera.stream.start"), 1)
        self.assertEqual(commands.count("ctrl.camera.acquire"), 2)
        self.assertGreaterEqual(commands.count("ctrl.camera.clock.ping"), 1)

    def test_old_release_generation_cannot_stop_same_owner_new_stream(self):
        ingress, sender, _delivered = self.make_ingress()
        self.assertTrue(ingress.acquire("viewer-a", 10000, lease_generation=31)["granted"])
        self.assertTrue(ingress.acquire("viewer-a", 10000, lease_generation=32)["granted"])
        command_count = len(sender.commands)

        self.assertFalse(ingress.release("viewer-a", expected_generation=31))
        self.assertEqual(len(sender.commands), command_count)
        self.assertEqual(ingress._owner, "viewer-a")
        self.assertEqual(ingress._lease_generation, 32)
        self.assertTrue(ingress.release("viewer-a", expected_generation=32))

    def test_old_session_stream_and_wrong_owner_are_rejected(self):
        active = {"viewer-a"}
        ingress, _sender, delivered = self.make_ingress(
            valid_owner=lambda owner: owner in active
        )
        ingress.start("viewer-a", 10000)
        jpeg = jpeg_fixture()
        ingress.on_application_frame("device", packet(jpeg, stream_id=8),
                                     session_generation="old-run")
        ingress.on_application_frame("desktop", packet(jpeg))
        ingress.on_application_frame("device", packet(jpeg, stream_id=8))
        active.clear()
        ingress.on_application_frame("device", packet(jpeg, sequence=1))
        self.assertEqual(delivered, [])

    def test_start_requires_capability_and_valid_lease_owner(self):
        ingress, sender, _ = self.make_ingress(capability=False)
        self.assertFalse(ingress.start("viewer-a", 10000)["started"])
        self.assertEqual(sender.commands, [])
        no_lease, sender2, _ = self.make_ingress(valid_owner=lambda _owner: False)
        self.assertFalse(no_lease.start("viewer-a", 10000)["started"])
        self.assertEqual(sender2.commands, [])

    def test_sink_is_latest_only_and_cleanup_stops_stream_and_clears(self):
        sender = FakeSender()
        latest: dict[str, tuple[bytes, dict]] = {}
        ingress = OfficialCameraStreamIngress(
            send_command=sender,
            ingest_frame=lambda jpeg, metadata: latest.update(frame=(jpeg, metadata)),
            owner_is_valid=lambda _owner: True,
            capabilities=["camera.stream.mjpeg.wspk.v1"],
            clock_ping_interval=3600,
        )
        self.addCleanup(ingress.close)
        cleared: list[tuple[str, int | None]] = []
        ingress.set_clear_frame_callback(
            lambda owner, *, expected_generation=None: cleared.append((owner, expected_generation))
        )
        ingress.start("viewer-a", 10000)
        jpeg = jpeg_fixture()
        ingress.on_application_frame("device", packet(jpeg, sequence=1, timestamp_ms=1))
        ingress.on_application_frame("device", packet(jpeg, sequence=2, timestamp_ms=2))
        self.assertEqual(latest["frame"][1]["sequence"], 2)
        self.assertEqual(ingress.latest_frame_metadata["sequence"], 2)
        ingress.stop()
        names = [name for name, _ in sender.commands]
        self.assertIn("ctrl.camera.stream.stop", names)
        self.assertIn("ctrl.camera.release", names)
        self.assertEqual(cleared, [("viewer-a", None)])
        self.assertIsNone(ingress.latest_frame_metadata)

    def test_clock_offset_unknown_fails_closed_for_latency_metrics(self):
        ingress, sender, delivered = self.make_ingress()
        sender.fail_ping = True
        self.assertTrue(ingress.start("viewer-a", 10000)["started"])
        ingress.on_application_frame("device", packet(jpeg_fixture()))
        self.assertEqual(len(delivered), 1)
        self.assertIsNone(delivered[0][1]["latency_ms"])
        self.assertFalse(ingress.clock_offset_is_certain)

    def test_clock_offset_sign_and_uncertainty_follow_four_timestamp_formula(self):
        offset, uncertainty = _estimate_host_minus_device_offset(1000, 1105, 1110, 1025)
        self.assertEqual(offset, -95.0)
        self.assertEqual(uncertainty, 10.0)
        ingress, _sender, _delivered = self.make_ingress()
        ingress._clock_offset_ms = offset
        ingress._clock_uncertainty_ms = uncertainty
        self.assertEqual(ingress._latency_ms(1100, 1025), 20.0)

    def test_raw_observer_preserves_sdk_callback_and_observes_device_bytes(self):
        seen = []
        prior = []
        class Communicators:
            def __init__(self):
                self._on_frame = lambda channel, frame: prior.append((channel, frame))
        comms = Communicators()
        remove = install_raw_device_frame_observer(comms, lambda ch, frame: seen.append((ch, frame)))
        raw = packet(jpeg_fixture())
        comms._on_frame("device", raw)
        self.assertEqual(seen, [("device", raw)])
        self.assertEqual(prior, [("device", raw)])
        remove()

    def test_host_lifecycle_wires_existing_transport_release_and_disconnect(self):
        class CameraService:
            def __init__(self):
                self._lock = threading.RLock()
                self._active_lease_owner = None
                self._active_lease_generation = None
                self._lease_expires_at = 0.0
                self.on_lease_released = None
                self.frames = []

            def ingest_robot_frame(self, jpeg, metadata=None):
                self.frames.append((jpeg, metadata))

            def invalidate_owner_on_upstream_disconnect(self, owner, expected_generation=None):
                with self._lock:
                    if (self._active_lease_owner != owner
                            or (expected_generation is not None
                                and self._active_lease_generation != expected_generation)):
                        return False
                    self._active_lease_owner = None
                    self._active_lease_generation = None
                    return True

            def release_lease(self, owner, expected_generation=None):
                with self._lock:
                    if (self._active_lease_owner != owner
                            or (expected_generation is not None
                                and self._active_lease_generation != expected_generation)):
                        return False
                    generation = self._active_lease_generation
                    self._active_lease_owner = None
                    self._active_lease_generation = None
                    self._lease_expires_at = 0
                    callback = self.on_lease_released
                if callback:
                    callback(owner, "explicit_release", generation)
                return True

        class Robot:
            capabilities = ("camera.stream.mjpeg.wspk.v1",)

        class ExistingCommunicators:
            instances_created = 1

            def __init__(self):
                self.original_frames = []
                self._on_frame = lambda channel, frame: self.original_frames.append((channel, frame))

        app = DeskAgentHostApp.__new__(DeskAgentHostApp)
        app.camera_service = CameraService()
        app.official_camera_adapter = None
        app.official_camera_stream_ingress = None
        app._managed_camera_raw_observer_remove = None
        app._camera_stream_metadata_lock = threading.Lock()
        app._latest_camera_stream_metadata = None
        sender = FakeSender()
        communicators = ExistingCommunicators()
        original_callback = communicators._on_frame
        disconnect_callbacks = []
        app.register_official_sdk_camera_session(
            Robot(), command_sender=sender, communicators=communicators,
            on_disconnect=disconnect_callbacks.append,
        )
        observer = communicators._on_frame
        self.assertIsNot(observer, None)
        self.assertEqual(ExistingCommunicators.instances_created, 1)

        with app.camera_service._lock:
            app.camera_service._active_lease_owner = "viewer-a"
            app.camera_service._active_lease_generation = 1
            app.camera_service._lease_expires_at = time.time() + 10
        self.assertTrue(app.official_camera_stream_ingress.start(
            "viewer-a", 10000, lease_generation=1
        )["started"])
        jpeg = jpeg_fixture()
        raw = packet(jpeg)
        observer("device", raw)
        self.assertEqual(len(app.camera_service.frames), 1)
        self.assertEqual(app.camera_service.frames[0][0], jpeg)
        metadata = app.latest_camera_stream_metadata
        self.assertEqual(app.camera_service.frames[0][1], metadata)
        self.assertEqual(metadata["sequence"], 1)
        self.assertIn("capture_timestamp_ms", metadata)
        self.assertIn("host_receive_monotonic_ms", metadata)
        self.assertIn("session_generation", metadata)
        self.assertIn("clock_offset_ms", metadata)
        self.assertIn("clock_uncertainty_ms", metadata)
        self.assertEqual(communicators.original_frames, [("device", raw)])

        self.assertTrue(app.camera_service.release_lease("viewer-a"))
        command_names = [name for name, _ in sender.commands]
        self.assertIn("ctrl.camera.stream.stop", command_names)
        self.assertIsNone(app.latest_camera_stream_metadata)

        with app.camera_service._lock:
            app.camera_service._active_lease_owner = "viewer-b"
            app.camera_service._active_lease_generation = 2
            app.camera_service._lease_expires_at = time.time() + 10
        self.assertTrue(app.official_camera_stream_ingress.start(
            "viewer-b", 10000, lease_generation=2
        )["started"])
        disconnect_callbacks[-1]()
        self.assertIs(communicators._on_frame, original_callback)
        self.assertIsNone(app.latest_camera_stream_metadata)
        self.assertFalse(app.official_camera_stream_ingress.on_application_frame("device", raw))


if __name__ == "__main__":
    unittest.main()
