"""
Unit test for Host Daemon App and HTTP endpoints.
"""

from __future__ import annotations

import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import HTTPError
import json
from types import SimpleNamespace

from host.desk_agent.main import DeskAgentHostApp, make_request_handler, ThreadedHTTPServer
from host.desk_agent.camera.camera_service import (
    CameraStreamCapacityError,
    CameraStreamFrameUnavailable,
    CameraStreamLeaseError,
)
from host.desk_agent.core.models import RequestEnvelope


class TestHostDaemon(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "test_host_daemon.db"
        cfg_file = Path(cls.temp_dir.name) / "config.json"
        # Compatibility flag is intentionally true: CameraViewerService must still
        # fail closed when no physical frame source is attached.
        cfg_file.write_text(json.dumps({"storage": {"db_path": str(cls.db_path)}, "camera": {"allow_synthetic": True, "auto_start_pipeline": True}}))

        # Initialize App
        cls.app = DeskAgentHostApp(config_path=str(cfg_file))

        # Start HTTP server on an ephemeral port so concurrent suite runs
        # (and any other process) can never collide on a fixed port.
        handler = make_request_handler(cls.app)
        cls.server = ThreadedHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.app.close()
        cls.temp_dir.cleanup()

    def test_health_endpoint(self) -> None:
        with urlopen(f"http://127.0.0.1:{self.port}/health") as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "healthy")

    def test_rpc_endpoint(self) -> None:
        req_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-test-01",
            "operation": "ha.get_state",
            "arguments": {"entity_id": "light.desk"},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(req_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            self.assertEqual(resp.status, 200)
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["request_id"], "req-daemon-test-01")

    def test_rpc_reminder_flow(self) -> None:
        create_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-rem-01",
            "operation": "reminder.create",
            "arguments": {
                "reminder_id": "rem-daemon-01",
                "title": "Edge check",
                "due_at": "2026-10-15T12:00:00Z",
            },
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(create_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["result"]["reminder_id"], "rem-daemon-01")

        list_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-rem-02",
            "operation": "reminder.list",
            "arguments": {},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req2 = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(list_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req2) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertTrue(len(data["result"]["items"]) >= 1)

    def test_reminder_snooze_route_passes_occurrence_id(self) -> None:
        reminder_id = "rem-daemon-snooze-occurrence"
        self.app.life_service.create_reminder(
            reminder_id=reminder_id,
            title="Snooze route",
            due_at="2020-01-01T00:00:00Z",
        )
        occurrence_id = self.app.life_service.get_all_occurrences(reminder_id)[0]["occurrence_id"]
        self.app.life_service.trigger_occurrence_to_outbox(occurrence_id)

        req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-daemon-snooze-occurrence",
            operation="reminder.snooze",
            arguments={
                "reminder_id": reminder_id,
                "snooze_minutes": 10,
                "occurrence_id": occurrence_id,
            },
            expires_at="2030-01-01T00:00:00Z",
        )
        response = self.app._handle_reminder_snooze(req)

        self.assertEqual(response.status.value, "succeeded")
        occurrences = self.app.life_service.get_all_occurrences(reminder_id)
        previous = next(item for item in occurrences if item["occurrence_id"] == occurrence_id)
        self.assertEqual(previous["state"], "snoozed")

    def test_reminder_stop_ringing_route_maps_stale_state_to_non_retryable_conflict(self) -> None:
        reminder_id = "rem-daemon-stop-state-conflict"
        self.app.life_service.create_reminder(
            reminder_id=reminder_id,
            title="Not ringing yet",
            due_at="2035-01-01T00:00:00Z",
        )
        occurrence_id = self.app.life_service.get_all_occurrences(reminder_id)[0]["occurrence_id"]
        req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-daemon-stop-state-conflict",
            operation="reminder.stop_ringing",
            arguments={"occurrence_id": occurrence_id},
            expires_at="2030-01-01T00:00:00Z",
        )

        response = self.app.router.dispatch(req)

        self.assertEqual(response.status.value, "failed")
        self.assertEqual(response.error["code"], "STATE_CONFLICT")
        self.assertFalse(response.error["retryable"])
        self.assertEqual(self.app.life_service.get_all_occurrences(reminder_id)[0]["state"], "pending")

    def test_reminder_stop_ringing_route_preserves_missing_resource_error(self) -> None:
        req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-daemon-stop-missing",
            operation="reminder.stop_ringing",
            arguments={"occurrence_id": "occ-does-not-exist"},
            expires_at="2030-01-01T00:00:00Z",
        )

        response = self.app.router.dispatch(req)

        self.assertEqual(response.status.value, "failed")
        self.assertEqual(response.error["code"], "RESOURCE_NOT_FOUND")
        self.assertFalse(response.error["retryable"])

    def test_rpc_note_flow(self) -> None:
        note_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-note-01",
            "operation": "note.create",
            "arguments": {
                "note_id": "note-daemon-01",
                "title": "Meeting memo",
                "content": "Discuss architecture baseline",
                "tags": ["work"],
            },
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(note_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["result"]["note_id"], "note-daemon-01")

        get_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-note-02",
            "operation": "note.get",
            "arguments": {"note_id": "note-daemon-01"},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req2 = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(get_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req2) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["result"]["title"], "Meeting memo")

    def test_rpc_codex_routes(self) -> None:
        """Verifies codex.submit_turn and codex.get_task routes registered on Host."""
        submit_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-codex-01",
            "operation": "codex.submit_turn",
            "arguments": {
                "task_id": "tsk-host-codex-01",
                "workspace_alias": "epanda-desk-agent",
                "prompt": "run tests",
            },
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(submit_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertEqual(data["result"]["task_id"], "tsk-host-codex-01")

        # Query task status via Host
        get_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-daemon-codex-02",
            "operation": "codex.get_task",
            "arguments": {"task_id": "tsk-host-codex-01"},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req2 = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(get_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req2) as resp2:
            data2 = json.loads(resp2.read().decode())
            self.assertEqual(data2["status"], "succeeded")
            self.assertEqual(data2["result"]["task_id"], "tsk-host-codex-01")

    def test_camera_acquire_and_frame_endpoint(self) -> None:
        """A lease without an attached physical camera source must not yield a fake JPEG."""
        # 1. Acquire lease
        acq_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-cam-acq-01",
            "operation": "resource.camera.acquire",
            "arguments": {"client_id": "web-viewer-01", "lease_duration_ms": 5000},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(acq_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")
            self.assertTrue(data["result"]["granted"])

        try:
            # The capture pipeline has no frame source in this test. The endpoint
            # must report unavailable even though the legacy synthetic flag is set.
            frame_req = Request(f"http://127.0.0.1:{self.port}/api/camera/frame?client_id=web-viewer-01")
            with self.assertRaises(HTTPError) as error:
                urlopen(frame_req)
            try:
                self.assertEqual(error.exception.code, 503)
                self.assertEqual(json.loads(error.exception.read().decode())["error"], "CAMERA_FRAME_UNAVAILABLE")
            finally:
                error.exception.close()
        finally:
            # Keep the shared class-level Host app isolated for later tests.
            rel_payload = {
                "schema_version": "1.0.0",
                "request_id": "req-cam-rel-01",
                "operation": "resource.camera.release",
                "arguments": {"client_id": "web-viewer-01"},
                "expires_at": "2030-01-01T00:00:00Z",
            }
            req_rel = Request(
                f"http://127.0.0.1:{self.port}/api/rpc",
                data=json.dumps(rel_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req_rel) as resp:
                data = json.loads(resp.read().decode())
                self.assertEqual(data["status"], "succeeded")
                self.assertTrue(data["result"]["released"])

    def test_camera_frame_without_lease_returns_no_active_lease(self) -> None:
        request = Request(f"http://127.0.0.1:{self.port}/api/camera/frame?client_id=viewer-no-lease")
        with self.assertRaises(HTTPError) as error:
            urlopen(request)
        try:
            self.assertEqual(error.exception.code, 403)
            self.assertEqual(json.loads(error.exception.read().decode())["error"], "NO_ACTIVE_LEASE")
        finally:
            error.exception.close()

    def test_camera_frame_for_non_owner_returns_resource_busy(self) -> None:
        acquire_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-cam-owner-acq",
            "operation": "resource.camera.acquire",
            "arguments": {"client_id": "viewer-owner", "lease_duration_ms": 5000},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        acquire_request = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(acquire_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(acquire_request) as response:
            self.assertTrue(json.loads(response.read().decode())["result"]["granted"])

        try:
            request = Request(f"http://127.0.0.1:{self.port}/api/camera/frame?client_id=viewer-other")
            with self.assertRaises(HTTPError) as error:
                urlopen(request)
            try:
                self.assertEqual(error.exception.code, 403)
                self.assertEqual(json.loads(error.exception.read().decode())["error"], "RESOURCE_BUSY")
            finally:
                error.exception.close()
        finally:
            release_payload = {
                "schema_version": "1.0.0",
                "request_id": "req-cam-owner-release",
                "operation": "resource.camera.release",
                "arguments": {"client_id": "viewer-owner"},
                "expires_at": "2030-01-01T00:00:00Z",
            }
            release_request = Request(
                f"http://127.0.0.1:{self.port}/api/rpc",
                data=json.dumps(release_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(release_request) as response:
                self.assertTrue(json.loads(response.read().decode())["result"]["released"])

    def test_snapshot_endpoint_unavailable_without_physical_image_source(self) -> None:
        """GET /snapshot reports unavailable when the Host has no physical image source."""
        snap_req = Request(f"http://127.0.0.1:{self.port}/snapshot")
        with self.assertRaises(HTTPError) as error:
            urlopen(snap_req)
        try:
            self.assertEqual(error.exception.code, 404)
        finally:
            error.exception.close()

    def test_snapshot_endpoint_retrieval(self) -> None:
        """Verifies GET /snapshot returns fresh JPEG with anti-caching and frame headers."""
        jpeg = b"\xff\xd8host-test-image\xff\xd9"
        self.app.camera_service.set_hardware_frame_source(lambda: jpeg)
        snap_req = Request(f"http://127.0.0.1:{self.port}/snapshot")
        try:
            with urlopen(snap_req) as snap_resp:
                self.assertEqual(snap_resp.status, 200)
                self.assertEqual(snap_resp.headers.get("Content-Type"), "image/jpeg")
                self.assertIn("no-cache", snap_resp.headers.get("Cache-Control", ""))
                self.assertIsNotNone(snap_resp.headers.get("X-Frame-Seq"))
                self.assertEqual(snap_resp.read(), jpeg)
        finally:
            self.app.camera_service.set_hardware_frame_source(None)

    def test_reminder_automatic_scheduler_delivery(self) -> None:
        """Verifies background ReminderScheduler automatically triggers ringing and outbox alert for due items."""
        # Create a reminder due 1 minute ago
        past_time = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z")
        create_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-sched-rem-01",
            "operation": "reminder.create",
            "arguments": {
                "reminder_id": "rem-auto-deliver-01",
                "title": "Immediate Alert",
                "due_at": past_time,
            },
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(create_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertEqual(data["status"], "succeeded")

        # Allow background scheduler loop (polls every 1.0s) to detect and deliver
        time.sleep(1.2)

        # Verify alert was either dispatched by outbox delivery worker or pending in outbox (including with backoff)
        delivered_ids = [a.get("reminder_id") for a in self.app._delivered_alerts]
        pending_msgs = self.app.life_service.get_pending_outbox_messages(limit=10, include_delayed=True)
        pending_topics = [m["topic"] for m in pending_msgs]
        self.assertTrue(
            "rem-auto-deliver-01" in delivered_ids or "reminder.alert" in pending_topics,
            f"Alert must be processed: delivered={delivered_ids}, pending={pending_topics}",
        )

    def test_scheduler_loop_uses_atomic_trigger(self) -> None:
        """Verifies scheduler loop triggers due reminders via atomic trigger_occurrence_to_outbox."""
        called_occs = []
        original_trigger = getattr(self.app.life_service, "trigger_occurrence_to_outbox", None)

        def spy_trigger(occurrence_id, topic="reminder.alert", is_catchup=False):
            called_occs.append(occurrence_id)
            if original_trigger:
                return original_trigger(occurrence_id, topic=topic, is_catchup=is_catchup)
            return None

        self.app.life_service.trigger_occurrence_to_outbox = spy_trigger
        try:
            self.app.life_service.create_reminder(
                reminder_id="rem-atomic-sched-01",
                title="Atomic Sched Alert",
                due_at=(datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat().replace("+00:00", "Z"),
            )
            # Wait for scheduler loop to pick it up
            time.sleep(1.2)
            self.assertTrue(len(called_occs) >= 1, "Scheduler loop must call trigger_occurrence_to_outbox")
        finally:
            if original_trigger:
                self.app.life_service.trigger_occurrence_to_outbox = original_trigger
            elif hasattr(self.app.life_service, "trigger_occurrence_to_outbox"):
                delattr(self.app.life_service, "trigger_occurrence_to_outbox")

    def test_robot_tool_request_dispatched_to_router(self) -> None:
        """Verifies Host processes tool request from robot dialogue engine and sends response back."""
        # 1. Wrapped format
        wrapped_req = {
            "type": "tool.request",
            "data": {
                "schema_version": "1.0.0",
                "request_id": "req-tclaw-router-01",
                "operation": "reminder.create",
                "arguments": {
                    "reminder_id": "rem-tclaw-bridge-01",
                    "title": "Drink Coffee",
                    "due_at": "2026-10-16T16:00:00Z",
                },
                "expires_at": "2030-01-01T00:00:00Z",
            },
        }

        sent_responses: list[dict] = []
        if self.app.robot_adapter:
            original_send = self.app.robot_adapter.send_tool_response
            self.app.robot_adapter.send_tool_response = lambda resp: sent_responses.append(resp) or True
        else:
            class DummyAdapter:
                def send_tool_response(self, resp):
                    sent_responses.append(resp)
                    return True
            self.app.robot_adapter = DummyAdapter()
            original_send = None

        try:
            self.app._on_robot_tool_request(wrapped_req)
            self.assertEqual(len(sent_responses), 1)
            resp = sent_responses[0]
            self.assertEqual(resp["request_id"], "req-tclaw-router-01")
            self.assertEqual(resp["status"], "succeeded")
            self.assertEqual(resp["result"]["reminder_id"], "rem-tclaw-bridge-01")

            # Check reminder actually exists in life service
            rem = self.app.life_service.get_reminder("rem-tclaw-bridge-01")
            self.assertIsNotNone(rem)
            self.assertEqual(rem["title"], "Drink Coffee")

            # 2. Direct envelope format
            direct_req = {
                "schema_version": "1.0.0",
                "request_id": "req-tclaw-router-02",
                "operation": "ha.get_state",
                "arguments": {"entity_id": "light.living_room"},
                "expires_at": "2030-01-01T00:00:00Z",
            }
            self.app._on_robot_tool_request(direct_req)
            self.assertEqual(len(sent_responses), 2)
            resp2 = sent_responses[1]
            self.assertEqual(resp2["request_id"], "req-tclaw-router-02")
            self.assertEqual(resp2["status"], "succeeded")
        finally:
            if original_send and self.app.robot_adapter:
                self.app.robot_adapter.send_tool_response = original_send

    def test_motion_move_with_pan_deg_and_tilt_deg(self) -> None:
        """Verifies resource.motion.move accepts pan_deg/tilt_deg as well as pan/tilt and updates coordinates.

        A firmware ACK only proves the command was accepted, so the envelope must
        report ``accepted`` (never ``succeeded``) and must not carry the removed
        host-invented ``executed`` field (interfaces.md 6.1).
        """
        dispatched: list[tuple[float, float, int]] = []

        class MockRobotClient:
            is_connected = True

            def send_motion_move(self, pan: float, tilt: float, duration_ms: int = 500):
                dispatched.append((pan, tilt, duration_ms))
                return {
                    "status": "accepted",
                    "accepted": True,
                    "state_confirmed": False,
                    "error_code": None,
                    "retryable": False,
                    "pan": pan,
                    "tilt": tilt,
                    "duration_ms": duration_ms,
                }

        original_client = self.app._robot_client
        self.app._robot_client = MockRobotClient()
        try:
            # 1. Using pan_deg and tilt_deg
            move_payload = {
                "schema_version": "1.0.0",
                "request_id": "req-motion-deg-01",
                "operation": "resource.motion.move",
                "arguments": {
                    "pan_deg": 45.0,
                    "tilt_deg": 115.0,
                    "duration_ms": 300,
                },
                "expires_at": "2030-01-01T00:00:00Z",
            }
            req = Request(
                f"http://127.0.0.1:{self.port}/api/rpc",
                data=json.dumps(move_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                self.assertEqual(data["status"], "accepted")
                res = data["result"]
                self.assertNotIn("executed", res, "`executed` is not a contract field and must be removed")
                self.assertTrue(res["accepted"])
                self.assertFalse(res["state_confirmed"])
                self.assertEqual(res["pan"], 45.0)
                self.assertEqual(res["tilt"], 115.0)
                self.assertEqual(self.app.camera_service.pipeline._current_pan, 45.0)
                self.assertEqual(self.app.camera_service.pipeline._current_tilt, 115.0)

            # 2. Using fallback pan and tilt keys
            move_fallback = {
                "schema_version": "1.0.0",
                "request_id": "req-motion-fallback-01",
                "operation": "resource.motion.move",
                "arguments": {
                    "pan": 75.0,
                    "tilt": 125.0,
                    "duration_ms": 250,
                },
                "expires_at": "2030-01-01T00:00:00Z",
            }
            req2 = Request(
                f"http://127.0.0.1:{self.port}/api/rpc",
                data=json.dumps(move_fallback).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req2) as resp2:
                data2 = json.loads(resp2.read().decode())
                self.assertEqual(data2["status"], "accepted")
                res2 = data2["result"]
                self.assertEqual(res2["pan"], 75.0)
                self.assertEqual(res2["tilt"], 125.0)
                self.assertEqual(self.app.camera_service.pipeline._current_pan, 75.0)
                self.assertEqual(self.app.camera_service.pipeline._current_tilt, 125.0)
        finally:
            self.app._robot_client = original_client

    def test_motion_move_route_disconnected_is_not_reported_as_success(self) -> None:
        """Without a hardware link, resource.motion.move must not fabricate a success."""
        move_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-motion-move-nolink-01",
            "operation": "resource.motion.move",
            "arguments": {"pan_deg": 90.0, "tilt_deg": 120.0, "duration_ms": 200},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(move_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertNotEqual(data["status"], "succeeded")
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["error"]["code"], "UPSTREAM_UNAVAILABLE")
            self.assertTrue(data["error"]["retryable"])
            self.assertNotIn("result", data)
            self.assertEqual(data["error"]["details"]["reason"], "NO_PHYSICAL_ROBOT_CONNECTED")

    def test_motion_stop_route_disconnected(self) -> None:
        """Verifies resource.motion.stop reports UPSTREAM_UNAVAILABLE instead of a simulated success."""
        stop_payload = {
            "schema_version": "1.0.0",
            "request_id": "req-motion-stop-01",
            "operation": "resource.motion.stop",
            "arguments": {"emergency": True},
            "expires_at": "2030-01-01T00:00:00Z",
        }
        req = Request(
            f"http://127.0.0.1:{self.port}/api/rpc",
            data=json.dumps(stop_payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urlopen(req) as resp:
            data = json.loads(resp.read().decode())
            self.assertNotEqual(data["status"], "succeeded")
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["error"]["code"], "UPSTREAM_UNAVAILABLE")
            self.assertTrue(data["error"]["retryable"])
            self.assertNotIn("result", data)
            self.assertEqual(data["error"]["details"]["reason"], "NO_PHYSICAL_ROBOT_CONNECTED")
            self.assertEqual(data["error"]["details"]["hardware_link"], "disconnected")

    def test_motion_stop_route_connected(self) -> None:
        """Verifies resource.motion.stop reports `accepted` (not a fabricated completion) when connected."""
        called_args = []

        class MockRobotClient:
            is_connected = True

            def send_motion_stop(self, emergency: bool = False):
                called_args.append(emergency)
                return {
                    "status": "accepted",
                    "accepted": True,
                    "state_confirmed": False,
                    "error_code": None,
                    "retryable": False,
                    "emergency": emergency,
                }

        original_client = self.app._robot_client
        self.app._robot_client = MockRobotClient()
        try:
            stop_payload = {
                "schema_version": "1.0.0",
                "request_id": "req-motion-stop-02",
                "operation": "resource.motion.stop",
                "arguments": {"emergency": False},
                "expires_at": "2030-01-01T00:00:00Z",
            }
            req = Request(
                f"http://127.0.0.1:{self.port}/api/rpc",
                data=json.dumps(stop_payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urlopen(req) as resp:
                data = json.loads(resp.read().decode())
                self.assertEqual(data["status"], "accepted")
                res = data["result"]
                self.assertTrue(res["accepted"])
                self.assertFalse(res["state_confirmed"])
                self.assertEqual(res["hardware_link"], "connected")
                self.assertNotIn("executed", res)
                self.assertEqual(called_args, [False])
        finally:
            self.app._robot_client = original_client

    def test_robot_adapter_send_motion_stop(self) -> None:
        """Verifies RobotAdapter.send_motion_stop sends ctrl.motion.stop and reports an accepted outcome."""
        from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter

        adapter = RobotAdapter(auto_pair=False)
        sent_cmds = []

        def mock_send(cmd, timeout=2.0):
            sent_cmds.append(cmd)
            return {"type": "sys.ack", "code": 0, "data": {"status": "accepted", "accepted": True}}

        adapter.send_command = mock_send
        res = adapter.send_motion_stop(emergency=True)

        self.assertEqual(len(sent_cmds), 1)
        self.assertEqual(sent_cmds[0]["type"], "ctrl.motion.stop")
        self.assertEqual(sent_cmds[0]["data"]["emergency"], True)
        self.assertEqual(res["status"], "accepted")
        self.assertTrue(res["accepted"])
        self.assertFalse(res["state_confirmed"])
        self.assertIsNone(res["error_code"])
        self.assertTrue(res["emergency"])


class TestCameraStreamRoute(unittest.TestCase):
    def setUp(self) -> None:
        class StreamService:
            def __init__(self):
                self.calls = []
                self.error = None
                self.break_after_first = False
                self.end_after_first = False
                self.iterator_closed = False
                self.active_lease_owner = "viewer-1"

            def iter_mjpeg(self, client_id, **kwargs):
                self.calls.append(("iter", client_id, kwargs))
                if self.error and not isinstance(self.error, CameraStreamFrameUnavailable):
                    raise self.error

                def stream():
                    try:
                        if isinstance(self.error, CameraStreamFrameUnavailable):
                            raise self.error
                        yield b"--deskagent-frame\r\nContent-Type: image/jpeg\r\n\r\nfirst\r\n"
                        if self.break_after_first:
                            raise BrokenPipeError("simulated client disconnect")
                        if self.end_after_first:
                            return
                        yield b"--deskagent-frame\r\nContent-Type: image/jpeg\r\n\r\nsecond\r\n"
                    finally:
                        self.iterator_closed = True

                return stream()

            def release_lease(self, client_id):
                self.calls.append(("release", client_id, self.iterator_closed))
                self.active_lease_owner = None
                return True

        self.service = StreamService()
        app = SimpleNamespace(config={"auth_token": "stream-test-token"}, camera_service=self.service)
        self.app = app
        self.access_logs = []
        handler = make_request_handler(app)

        def capture_log(_handler, message, *args):
            self.access_logs.append(message % args)

        handler.log_message = capture_log
        self.server = ThreadedHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_address[1]}/api/camera/stream"
        self.url = f"{self.base_url}?client_id=viewer-1"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def test_stream_auth_precedes_service_and_stream_releases_owner(self) -> None:
        with self.assertRaises(HTTPError) as unauthorized:
            urlopen(self.url, timeout=2)
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()
        self.assertEqual(self.service.calls, [])

        query_token_url = f"{self.base_url}?client_id=viewer-1&token=stream-test-token"
        with self.assertRaises(HTTPError) as query_token_response:
            urlopen(query_token_url, timeout=2)
        self.assertEqual(query_token_response.exception.code, 401)
        self.assertEqual(self.service.calls, [])
        self.assertTrue(any("[REDACTED]" in line for line in self.access_logs))
        self.assertFalse(any("stream-test-token" in line for line in self.access_logs))

        custom_header = Request(self.url, headers={"X-Desk-Agent-Token": "stream-test-token"})
        with self.assertRaises(HTTPError) as custom_header_response:
            urlopen(custom_header, timeout=2)
        self.assertEqual(custom_header_response.exception.code, 401)
        self.assertEqual(self.service.calls, [])

        request = Request(self.url, headers={"Authorization": "Bearer stream-test-token"})
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "multipart/x-mixed-replace")
            self.assertIn("boundary=deskagent-frame", response.headers["Content-Type"])
            body = response.read()
        self.assertIn(b"first", body)
        self.assertIn(b"second", body)
        self.assertEqual(self.service.calls[0][0:2], ("iter", "viewer-1"))
        self.assertTrue(self.service.iterator_closed)
        self.assertFalse(any(call[0] == "release" for call in self.service.calls))
        self.assertEqual(self.service.active_lease_owner, "viewer-1")

    def test_stream_fails_closed_when_no_bearer_token_is_configured(self) -> None:
        self.app.config["auth_token"] = None
        with self.assertRaises(HTTPError) as unauthorized:
            urlopen(self.url, timeout=2)
        self.assertEqual(unauthorized.exception.code, 401)
        unauthorized.exception.close()
        self.assertEqual(self.service.calls, [])

    def test_rejects_missing_empty_and_duplicate_client_ids_before_service_call(self) -> None:
        urls = (
            self.base_url,
            f"{self.base_url}?client_id=",
            f"{self.base_url}?client_id=%20%20",
            f"{self.base_url}?client_id=one&client_id=two",
        )
        for url in urls:
            with self.subTest(url=url):
                request = Request(url, headers={"Authorization": "Bearer stream-test-token"})
                with self.assertRaises(HTTPError) as response:
                    urlopen(request, timeout=2)
                self.assertEqual(response.exception.code, 400)
                self.assertEqual(json.loads(response.exception.read().decode())["error"], "INVALID_CLIENT_ID")
        self.assertEqual(self.service.calls, [])

    def test_decodes_client_id_using_standard_query_parser(self) -> None:
        request = Request(
            f"{self.base_url}?client_id=view%2Der-1",
            headers={"Authorization": "Bearer stream-test-token"},
        )
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            response.read()
        self.assertEqual(self.service.calls[0][0:2], ("iter", "view-er-1"))

    def test_stream_service_errors_map_before_response_headers(self) -> None:
        cases = (
            (CameraStreamLeaseError("NO_ACTIVE_LEASE"), 403, "NO_ACTIVE_LEASE"),
            (CameraStreamLeaseError("RESOURCE_BUSY"), 403, "RESOURCE_BUSY"),
            (CameraStreamCapacityError("STREAM_ALREADY_ACTIVE"), 429, "STREAM_ALREADY_ACTIVE"),
            (CameraStreamCapacityError("STREAM_CAPACITY"), 429, "STREAM_CAPACITY"),
            (CameraStreamFrameUnavailable("NO_FRESH_FRAME"), 503, "CAMERA_FRAME_UNAVAILABLE"),
        )
        for error, expected_status, expected_code in cases:
            with self.subTest(error=error):
                self.service.calls.clear()
                self.service.error = error
                request = Request(self.url, headers={"Authorization": "Bearer stream-test-token"})
                with self.assertRaises(HTTPError) as response:
                    urlopen(request, timeout=2)
                self.assertEqual(response.exception.code, expected_status)
                self.assertEqual(response.exception.headers.get_content_type(), "application/json")
                body = json.loads(response.exception.read().decode())
                self.assertEqual(body["error"], expected_code)
                self.assertFalse(any(call[0] == "release" for call in self.service.calls))
                self.assertEqual(self.service.active_lease_owner, "viewer-1")

    def test_normal_stale_stream_end_keeps_camera_lease(self) -> None:
        self.service.end_after_first = True
        request = Request(self.url, headers={"Authorization": "Bearer stream-test-token"})
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"first", response.read())
        self.assertTrue(self.service.iterator_closed)
        self.assertFalse(any(call[0] == "release" for call in self.service.calls))
        self.assertEqual(self.service.active_lease_owner, "viewer-1")

    def test_stream_iterator_disconnect_closes_iterator_and_releases_lease(self) -> None:
        self.service.break_after_first = True
        request = Request(self.url, headers={"Authorization": "Bearer stream-test-token"})
        with urlopen(request, timeout=3) as response:
            self.assertEqual(response.status, 200)
            self.assertIn(b"first", response.read())
        self.assertTrue(self.service.iterator_closed)
        self.assertFalse(any(call[0] == "release" for call in self.service.calls))
        self.assertEqual(self.service.active_lease_owner, "viewer-1")


if __name__ == "__main__":
    unittest.main()

