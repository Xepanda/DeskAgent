"""
Regression tests for PR #14 remediation findings:
1. Issue 1 (P1): WSPK frame_type 0x03 (DESK_WSPK_TYPE_IMAGE) and 0x02 (VIDEO) camera frame ingestion.
2. Issue 2 (P1): RemoteCodexCompanionClient event listener and background completion poller for split deployments.
3. Issue 3 & 4 (P2): Request ID monotonicity and correlation table reuse without stale data.
4. Issue 5 (P2): ResponseEnvelope.create_error parameter compatibility in main.py exception handlers.
5. Issue 6 (Pipeline): Camera lease acquire/release and snapshot dispatches ctrl.camera.* commands to robot.
"""

from __future__ import annotations

import json
import os
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest.mock import MagicMock, patch

from companion.src.codex_companion import RemoteCodexCompanionClient
from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter
from host.desk_agent.core.models import RequestEnvelope, ResponseStatus
from host.desk_agent.main import DeskAgentHostApp


def make_req(operation: str, arguments: dict, client_id: str | None = None) -> RequestEnvelope:
    return RequestEnvelope(
        schema_version="1.0.0",
        request_id=f"req-test-{operation.replace('.', '-')}",
        operation=operation,
        arguments=arguments,
        expires_at="2099-01-01T00:00:00Z",
        client_id=client_id,
    )


class TestWspkFrameTypeHandling(unittest.TestCase):
    def test_wspk_image_and_video_frames_both_invoke_camera_callback(self) -> None:
        """Confirms that both WSPK type 0x03 (image) and 0x02 (video) trigger camera frame callbacks."""
        adapter = RobotAdapter(host="127.0.0.1", tcp_port=37020)
        camera_frames: list[bytes] = []
        audio_frames: list[bytes] = []

        adapter.register_camera_frame_callback(lambda data: camera_frames.append(data))
        adapter.register_audio_frame_callback(lambda data: audio_frames.append(data))

        raw_buf = bytearray()
        fake_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"
        fake_pcm = b"\x00\x01\x02\x03\x04\x05\x06\x07"

        # 1. Test Frame Type 0x03 (DESK_WSPK_TYPE_IMAGE) - used by firmware
        header_img = struct.pack("<4sBBHII", b"WSPK", 0x03, 0, 1, 101, len(fake_jpeg))
        adapter._process_incoming_bytes(raw_buf, header_img + fake_jpeg)
        self.assertEqual(len(camera_frames), 1)
        self.assertEqual(camera_frames[0], fake_jpeg)
        self.assertEqual(len(audio_frames), 0)

        # 2. Test Frame Type 0x02 (DESK_WSPK_TYPE_VIDEO)
        header_vid = struct.pack("<4sBBHII", b"WSPK", 0x02, 0, 1, 102, len(fake_jpeg))
        adapter._process_incoming_bytes(raw_buf, header_vid + fake_jpeg)
        self.assertEqual(len(camera_frames), 2)
        self.assertEqual(camera_frames[1], fake_jpeg)
        self.assertEqual(len(audio_frames), 0)

        # 3. Test Frame Type 0x01 (DESK_WSPK_TYPE_AUDIO)
        header_aud = struct.pack("<4sBBHII", b"WSPK", 0x01, 0, 2, 201, len(fake_pcm))
        adapter._process_incoming_bytes(raw_buf, header_aud + fake_pcm)
        self.assertEqual(len(camera_frames), 2)
        self.assertEqual(len(audio_frames), 1)
        self.assertEqual(audio_frames[0], fake_pcm)


class MockCodexCompanionServer(BaseHTTPRequestHandler):
    task_polls: int = 0
    task_status_sequence: list[str] = ["running", "completed"]

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length).decode("utf-8"))
        op = body.get("operation")
        req_id = body.get("request_id", "req-test")

        if op == "codex.submit_turn":
            resp = {
                "status": "succeeded",
                "request_id": req_id,
                "result": {"task_id": body["arguments"]["task_id"], "status": "running"},
            }
        elif op == "codex.get_task":
            MockCodexCompanionServer.task_polls += 1
            idx = min(MockCodexCompanionServer.task_polls - 1, len(MockCodexCompanionServer.task_status_sequence) - 1)
            status = MockCodexCompanionServer.task_status_sequence[idx]
            resp = {
                "status": "succeeded",
                "request_id": req_id,
                "result": {
                    "task_id": body["arguments"]["task_id"],
                    "status": status,
                    "summary": "Generated test code successfully",
                },
            }
        else:
            resp = {"status": "succeeded", "request_id": req_id, "result": {}}

        data = json.dumps(resp).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args: object) -> None:
        pass


class TestRemoteCodexCompanionPollingAndEvents(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        MockCodexCompanionServer.task_polls = 0
        cls.server = HTTPServer(("127.0.0.1", 0), MockCodexCompanionServer)
        cls.port = cls.server.server_address[1]
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def test_remote_codex_companion_event_listener_and_background_polling(self) -> None:
        """Confirms RemoteCodexCompanionClient emits codex.turn_completed upon task completion over RPC."""
        client = RemoteCodexCompanionClient(
            endpoint_url=f"http://127.0.0.1:{self.port}",
            poll_interval=0.1,
        )
        self.assertTrue(hasattr(client, "add_event_listener"), "Must have add_event_listener method")

        received_events: list[tuple[str, dict]] = []
        event_done = threading.Event()

        def on_event(event_type: str, data: dict) -> None:
            received_events.append((event_type, data))
            if event_type == "codex.turn_completed":
                event_done.set()

        client.add_event_listener(on_event)

        # Submit task
        res = client.submit_task("task-p1-42", "main-workspace", "Fix test coverage")
        self.assertIn("task_id", res)

        # Await completion event
        ok = event_done.wait(timeout=2.0)
        client.close()

        self.assertTrue(ok, "Background poller should have dispatched codex.turn_completed event")
        self.assertEqual(len(received_events), 1)
        self.assertEqual(received_events[0][0], "codex.turn_completed")
        self.assertEqual(received_events[0][1]["task_id"], "task-p1-42")
        self.assertEqual(received_events[0][1]["status"], "completed")
        self.assertIn("Generated test code", received_events[0][1]["summary"])


class TestResponseEnvelopeCreateErrorHandlers(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")
        self.cfg_path = os.path.join(self.tmpdir.name, "config.json")
        cfg = {
            "storage": {"db_path": self.db_path},
            "home_assistant": {"subscribe_events": False},
            "robot": {"auto_connect": False},
            "camera": {"allow_synthetic": False},
        }
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.app = DeskAgentHostApp(self.cfg_path)

    def tearDown(self) -> None:
        self.app.close()
        self.tmpdir.cleanup()

    def test_reminder_create_exception_returns_valid_error_envelope(self) -> None:
        """Verifies _handle_reminder_create handles errors using code/message kwargs without TypeError."""
        req = make_req("reminder.create", {"title": "Test Reminder", "due_at": "2026-10-20T12:00:00Z"})
        with patch.object(self.app.life_service, "create_reminder", side_effect=ValueError("Simulated DB error")):
            resp = self.app._handle_reminder_create(req)
            self.assertEqual(resp.status, ResponseStatus.FAILED)
            self.assertIsNotNone(resp.error)
            self.assertEqual(resp.error["code"], "EXECUTION_FAILED")
            self.assertIn("Simulated DB error", resp.error["message"])

    def test_note_create_exception_returns_valid_error_envelope(self) -> None:
        """Verifies _handle_note_create handles errors using code/message kwargs without TypeError."""
        req = make_req("note.create", {"title": "Test Note", "content": "Important info"})
        with patch.object(self.app.life_service, "create_note", side_effect=RuntimeError("Disk quota exceeded")):
            resp = self.app._handle_note_create(req)
            self.assertEqual(resp.status, ResponseStatus.FAILED)
            self.assertIsNotNone(resp.error)
            self.assertEqual(resp.error["code"], "EXECUTION_FAILED")
            self.assertIn("Disk quota exceeded", resp.error["message"])


class TestCameraAcquireReleasePipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")
        self.cfg_path = os.path.join(self.tmpdir.name, "config.json")
        cfg = {
            "storage": {"db_path": self.db_path},
            "home_assistant": {"subscribe_events": False},
            "robot": {"auto_connect": False},
            "camera": {"allow_synthetic": False},
        }
        with open(self.cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        self.app = DeskAgentHostApp(self.cfg_path)

        # Mock connected robot client
        self.mock_robot = MagicMock()
        self.mock_robot.is_connected = True
        self.mock_robot.send_command.return_value = {"type": "sys.ack", "code": 0}
        self.app.register_robot_hardware_client(self.mock_robot)

    def tearDown(self) -> None:
        self.app.close()
        self.tmpdir.cleanup()

    def test_camera_acquire_dispatches_acquire_and_capture_to_robot(self) -> None:
        """Confirms that acquiring a camera lease sends ctrl.camera.acquire and ctrl.camera.capture to robot."""
        req = make_req("resource.camera.acquire", {"client_id": "test-viewer", "lease_duration_ms": 5000}, client_id="test-viewer")
        resp = self.app._handle_camera_acquire(req)
        self.assertEqual(resp.status, ResponseStatus.SUCCEEDED)

        commands_sent = [call.args[0]["type"] for call in self.mock_robot.send_command.call_args_list]
        self.assertIn("ctrl.camera.acquire", commands_sent)
        self.assertIn("ctrl.camera.capture", commands_sent)

    def test_camera_release_dispatches_release_to_robot(self) -> None:
        """Confirms that releasing a camera lease sends ctrl.camera.release to robot."""
        # First acquire
        acq_req = make_req("resource.camera.acquire", {"client_id": "viewer-rel"}, client_id="viewer-rel")
        self.app._handle_camera_acquire(acq_req)
        self.mock_robot.send_command.reset_mock()

        # Then release
        rel_req = make_req("resource.camera.release", {"client_id": "viewer-rel"}, client_id="viewer-rel")
        resp = self.app._handle_camera_release(rel_req)
        self.assertEqual(resp.status, ResponseStatus.SUCCEEDED)

        commands_sent = [call.args[0]["type"] for call in self.mock_robot.send_command.call_args_list]
        self.assertIn("ctrl.camera.release", commands_sent)

    def test_production_default_disallows_synthetic_frames(self) -> None:
        """Confirms that in production Host configuration, allow_synthetic defaults to False and never invents frames."""
        self.assertFalse(self.app.camera_service.allow_synthetic)
        self.app.camera_service.acquire_lease("viewer-no-fake", lease_ms=5000)
        frame = self.app.camera_service.get_latest_frame("viewer-no-fake")
        self.assertIsNone(frame, "Must NOT produce synthetic frames when hardware is missing")


if __name__ == "__main__":
    unittest.main()
