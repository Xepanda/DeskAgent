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
        """Confirms that with no camera config block, allow_synthetic defaults to False and never invents frames."""
        cfg_default_path = os.path.join(self.tmpdir.name, "default_config.json")
        with open(cfg_default_path, "w", encoding="utf-8") as f:
            json.dump({"storage": {"db_path": self.db_path}}, f)
        app_default = DeskAgentHostApp(cfg_default_path)
        try:
            self.assertFalse(app_default.camera_service.allow_synthetic)
            self.assertFalse(app_default.camera_service.auto_start_pipeline)
            app_default.camera_service.acquire_lease("viewer-no-fake", lease_ms=5000)
            frame = app_default.camera_service.get_latest_frame("viewer-no-fake")
            self.assertIsNone(frame, "Must NOT produce synthetic frames when hardware is missing")
        finally:
            app_default.close()

    def test_synthetic_frame_never_overwrites_hardware_frame(self) -> None:
        """Verifies push_frame ignores synthetic frames if a non-stale hardware frame exists."""
        cam = self.app.camera_service
        cam.acquire_lease("test-hw-protection", lease_ms=5000)
        hw_jpeg = b"\xff\xd8\xff\xe0HW_FRAME\xff\xd9"
        cam.push_frame(hw_jpeg, is_hardware=True)
        self.assertEqual(cam.get_latest_frame("test-hw-protection")["jpeg_data"], hw_jpeg)

        fake_jpeg = b"\xff\xd8\xff\xe0SYNTHETIC\xff\xd9"
        cam.push_frame(fake_jpeg, is_hardware=False)
        self.assertEqual(cam.get_latest_frame("test-hw-protection")["jpeg_data"], hw_jpeg)

    def test_camera_acquire_rejection_from_robot_rolls_back_host_lease(self) -> None:
        """Confirms that when robot nacks camera acquire, host rolls back lease and does NOT capture."""
        self.mock_robot.send_command.return_value = {
            "type": "sys.nack",
            "code": 409,
            "error": {"code": "RESOURCE_BUSY", "message": "Device camera locked"},
        }
        req = make_req("resource.camera.acquire", {"client_id": "busy-client", "lease_duration_ms": 5000}, client_id="busy-client")
        resp = self.app._handle_camera_acquire(req)
        self.assertEqual(resp.status, ResponseStatus.FAILED)
        self.assertFalse(self.app.camera_service.is_lease_valid())
        commands_sent = [call.args[0]["type"] for call in self.mock_robot.send_command.call_args_list]
        self.assertIn("ctrl.camera.acquire", commands_sent)
        self.assertNotIn("ctrl.camera.capture", commands_sent)

    def test_snapshot_acquires_lease_before_capture_and_returns_real_frame(self) -> None:
        """Verifies snapshot acquires ephemeral lease on host and device before issuing capture."""
        from urllib.request import urlopen
        from host.desk_agent.main import make_request_handler, ThreadedHTTPServer

        real_frame = b"\xff\xd8\xff\xe0REAL_ROBOT_SNAPSHOT\xff\xd9"
        def mock_send(cmd, timeout=1.0):
            if cmd["type"] == "ctrl.camera.acquire":
                return {"type": "sys.ack", "code": 0}
            elif cmd["type"] == "ctrl.camera.capture":
                self.app.camera_service.ingest_robot_frame(real_frame)
                return {"type": "sys.ack", "code": 0}
            return {"type": "sys.ack", "code": 0}
        self.mock_robot.send_command.side_effect = mock_send

        server = ThreadedHTTPServer(("127.0.0.1", 0), make_request_handler(self.app))
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()
        port = server.server_address[1]
        try:
            with urlopen(f"http://127.0.0.1:{port}/snapshot") as resp:
                self.assertEqual(resp.status, 200)
                self.assertEqual(resp.headers.get("X-Frame-Hardware"), "1")
                data = resp.read()
                self.assertEqual(data, real_frame)
        finally:
            server.shutdown()
            server.server_close()


class TestCodexTurnStatusAndPersistence(unittest.TestCase):
    def test_codex_failed_and_interrupted_turn_notifications(self) -> None:
        from companion.src.codex_companion import CodexCompanion
        comp = CodexCompanion()
        events = []
        comp.add_event_listener(lambda ev, data: events.append((ev, data)))
        comp.workspaces["ws-1"] = "/tmp"
        comp._tasks["task-fail-01"] = {"task_id": "task-fail-01", "prompt": "Fail prompt"}
        comp._tasks["task-int-01"] = {"task_id": "task-int-01", "prompt": "Interrupt prompt"}
        comp._tasks["task-ok-01"] = {"task_id": "task-ok-01", "prompt": "Ok prompt"}

        # 1. Failed turn
        comp._handle_server_notification("turn/completed", {
            "task_id": "task-fail-01",
            "turn": {"id": "turn-1", "status": "failed", "error": {"message": "Compilation error"}},
        })
        self.assertEqual(events[0][0], "codex.failed")
        self.assertEqual(events[0][1]["status"], "failed")
        self.assertIn("Compilation error", events[0][1]["summary"])

        # 2. Interrupted turn
        comp._handle_server_notification("turn/completed", {
            "task_id": "task-int-01",
            "turn": {"id": "turn-2", "status": "interrupted"},
        })
        self.assertEqual(events[1][0], "codex.interrupted")
        self.assertEqual(events[1][1]["status"], "interrupted")

        # 3. Successful turn
        comp._handle_server_notification("turn/completed", {
            "task_id": "task-ok-01",
            "turn": {"id": "turn-3", "status": "completed", "summary": "Build succeeded"},
        })
        self.assertEqual(events[2][0], "codex.turn_completed")
        self.assertEqual(events[2][1]["status"], "completed")
        self.assertEqual(events[2][1]["summary"], "Build succeeded")

    def test_remote_codex_persistence_and_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, "remote_codex_tasks.json")
            client1 = RemoteCodexCompanionClient(
                endpoint_url="http://127.0.0.1:9999",
                state_file_path=state_file,
            )
            client1._pending_tasks["task-persist-01"] = {
                "task_id": "task-persist-01",
                "prompt": "Persistent task prompt",
                "workspace_alias": "ws-test",
            }
            client1._save_persisted_tasks()
            client1.close()

            # Client 2 restores on startup
            client2 = RemoteCodexCompanionClient(
                endpoint_url="http://127.0.0.1:9999",
                state_file_path=state_file,
            )
            self.assertIn("task-persist-01", client2._pending_tasks)
            self.assertEqual(client2._pending_tasks["task-persist-01"]["prompt"], "Persistent task prompt")
            client2.close()

    def test_main_codex_event_announcements(self) -> None:
        """Verifies DeskAgentHostApp maps codex completion, failure, and interrupt to appropriate announcements."""
        with tempfile.TemporaryDirectory() as td:
            db_p = os.path.join(td, "test.db")
            cfg_p = os.path.join(td, "cfg.json")
            with open(cfg_p, "w", encoding="utf-8") as f:
                json.dump({"storage": {"db_path": db_p}, "robot": {"auto_connect": False}}, f)
            app = DeskAgentHostApp(cfg_p)
            mock_robot = MagicMock()
            app.robot_adapter = mock_robot
            try:
                # Simulate completed
                app.companion._dispatch_event("codex.turn_completed", {"task_id": "t1", "summary": "Done job"})
                mock_robot.send_announcement.assert_called_with("codex.completed", {
                    "title": "Codex 任务完成",
                    "text": "Codex任务已完成：Done job",
                    "sound": "notify",
                    "task_id": "t1",
                })

                # Simulate failed
                app.companion._dispatch_event("codex.failed", {"task_id": "t2", "summary": "Syntax error"})
                mock_robot.send_announcement.assert_called_with("codex.failed", {
                    "title": "Codex 任务失败",
                    "text": "Codex任务执行失败：Syntax error",
                    "sound": "alert",
                    "task_id": "t2",
                })

                # Simulate interrupted
                app.companion._dispatch_event("codex.interrupted", {"task_id": "t3", "summary": "Cancelled by user"})
                mock_robot.send_announcement.assert_called_with("codex.interrupted", {
                    "title": "Codex 任务中断",
                    "text": "Codex任务已中断：Cancelled by user",
                    "sound": "alert",
                    "task_id": "t3",
                })
            finally:
                app.close()



class TestP2RemainingRemediations(unittest.TestCase):
    def test_remote_codex_restored_tasks_deferred_until_listener_added(self) -> None:
        """Issue 1 (P2): RemoteCodexCompanionClient does not start poller until event listener is registered,
        preventing lost notifications if task completed during restart."""
        with tempfile.TemporaryDirectory() as td:
            state_file = os.path.join(td, "remote_codex_tasks.json")
            with open(state_file, "w", encoding="utf-8") as f:
                json.dump({
                    "task-restart-01": {
                        "task_id": "task-restart-01",
                        "prompt": "Test prompt",
                        "status": "running",
                    }
                }, f)

            client = RemoteCodexCompanionClient(
                endpoint_url="http://127.0.0.1:9998",
                state_file_path=state_file,
            )
            self.assertIsNone(client._poll_thread)
            self.assertEqual(len(client._pending_tasks), 1)

            events: list[tuple[str, dict]] = []
            def mock_rpc(op, args):
                if op == "codex.get_task":
                    return {"status": "succeeded", "result": {"task_id": args.get("task_id"), "status": "completed", "output": "Done successfully"}}
                return {"status": "succeeded", "result": {}}

            with patch.object(client, "_rpc_call", side_effect=mock_rpc):
                client.add_event_listener(lambda ev, data: events.append((ev, data)))
                self.assertIsNotNone(client._poll_thread)

                deadline = time.time() + 2.0
                while time.time() < deadline and not events:
                    time.sleep(0.05)

                self.assertTrue(len(events) >= 1)
                self.assertEqual(events[0][0], "codex.turn_completed")
                self.assertEqual(events[0][1]["task_id"], "task-restart-01")
                self.assertEqual(events[0][1]["summary"], "Done successfully")

            client.close()

    def test_codex_summary_extracts_assistant_output_and_never_user_prompt(self) -> None:
        """Issue 2 (P2): Codex turn completion uses assistant's response or fallback, never slicing user prompt."""
        from companion.src.codex_companion import CodexCompanion
        comp = CodexCompanion()
        events: list[tuple[str, dict]] = []
        comp.add_event_listener(lambda ev, data: events.append((ev, data)))
        comp.workspaces["ws-1"] = "/tmp"

        user_prompt = "请帮我修复代码中的空指针异常并在第10行添加判空"
        comp._tasks["task-out-01"] = {"task_id": "task-out-01", "prompt": user_prompt}

        # 1. Assistant produces text via item/updated
        comp._handle_server_notification("item/updated", {
            "task_id": "task-out-01",
            "item": {"type": "message", "text": "已在第 10 行增加判空检查并修复 NPE"},
        })
        comp._handle_server_notification("turn/completed", {
            "task_id": "task-out-01",
            "turn": {"id": "turn-1", "status": "completed"},
        })
        self.assertEqual(events[0][1]["summary"], "已在第 10 行增加判空检查并修复 NPE")
        self.assertNotEqual(events[0][1]["summary"], user_prompt[:60])

        # 2. No output produced at all: defaults to neutral success message, NOT user prompt
        comp._tasks["task-noout-02"] = {"task_id": "task-noout-02", "prompt": user_prompt}
        comp._handle_server_notification("turn/completed", {
            "task_id": "task-noout-02",
            "turn": {"id": "turn-2", "status": "completed"},
        })
        self.assertEqual(events[1][1]["summary"], "任务已成功完成")
        self.assertNotEqual(events[1][1]["summary"], user_prompt[:60])

    def test_snapshot_rejects_anonymous_access_when_lease_is_held(self) -> None:
        """Issue 3 (P2): When another client holds active lease, snapshot without client_id cannot borrow owner."""
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        from host.desk_agent.main import make_request_handler, ThreadedHTTPServer

        with tempfile.TemporaryDirectory() as td:
            db_p = os.path.join(td, "test.db")
            cfg_p = os.path.join(td, "cfg.json")
            with open(cfg_p, "w", encoding="utf-8") as f:
                json.dump({"storage": {"db_path": db_p}, "robot": {"auto_connect": False}}, f)
            app = DeskAgentHostApp(cfg_p)
            server = ThreadedHTTPServer(("127.0.0.1", 0), make_request_handler(app))
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            port = server.server_address[1]

            try:
                # Active lease acquired by 'alice'
                app.camera_service.acquire_lease("alice", lease_ms=10000)

                # 1. Anonymous request (/snapshot without client_id) MUST be rejected with 403
                with self.assertRaises(HTTPError) as ctx:
                    with urlopen(f"http://127.0.0.1:{port}/snapshot") as resp:
                        pass
                self.assertEqual(ctx.exception.code, 403)

                # 2. Mismatched client_id ('bob') MUST be rejected with 403
                with self.assertRaises(HTTPError) as ctx:
                    with urlopen(f"http://127.0.0.1:{port}/snapshot?client_id=bob") as resp:
                        pass
                self.assertEqual(ctx.exception.code, 403)

                # 3. Matching client_id ('alice') is accepted
                app.camera_service.push_frame(b"\xff\xd8\xff\xe0ALICE_FRAME\xff\xd9", is_hardware=True)
                with urlopen(f"http://127.0.0.1:{port}/snapshot?client_id=alice") as resp:
                    self.assertEqual(resp.status, 200)
            finally:
                server.shutdown()
                server.server_close()
                app.close()

    def test_snapshot_capture_ack_failure_and_stale_hardware_rejection(self) -> None:
        """Issue 4 (P2): Snapshot rejects on capture NACK and strictly requires frame_seq > initial_seq."""
        from urllib.request import urlopen, Request
        from urllib.error import HTTPError
        from host.desk_agent.main import make_request_handler, ThreadedHTTPServer

        with tempfile.TemporaryDirectory() as td:
            db_p = os.path.join(td, "test.db")
            cfg_p = os.path.join(td, "cfg.json")
            with open(cfg_p, "w", encoding="utf-8") as f:
                json.dump({"storage": {"db_path": db_p}, "robot": {"auto_connect": False}}, f)
            app = DeskAgentHostApp(cfg_p)
            mock_robot = MagicMock()
            mock_robot.is_connected = True
            app.robot_adapter = mock_robot
            app._robot_client = mock_robot

            server = ThreadedHTTPServer(("127.0.0.1", 0), make_request_handler(app))
            t = threading.Thread(target=server.serve_forever, daemon=True)
            t.start()
            port = server.server_address[1]

            try:
                # Part A: Robot rejects ctrl.camera.capture with sys.nack
                mock_robot.send_command.side_effect = lambda cmd, timeout=1.0: (
                    {"type": "sys.ack", "code": 0} if cmd["type"] == "ctrl.camera.acquire"
                    else {"type": "sys.nack", "code": 500, "error": {"code": "SENSOR_FAULT", "message": "Camera HW fault"}}
                )
                with self.assertRaises(HTTPError) as ctx:
                    with urlopen(f"http://127.0.0.1:{port}/snapshot") as resp:
                        pass
                self.assertEqual(ctx.exception.code, 503)

                # Part B: Stale hardware frame rejection
                app.camera_service.ingest_robot_frame(b"\xff\xd8\xff\xe0OLD_STALE_FRAME\xff\xd9")
                initial_seq = app.camera_service._frame_seq

                # Robot ACKs capture, but does NOT ingest any new frame
                mock_robot.send_command.side_effect = lambda cmd, timeout=1.0: {"type": "sys.ack", "code": 0}

                with self.assertRaises(HTTPError) as ctx:
                    with urlopen(f"http://127.0.0.1:{port}/snapshot") as resp:
                        pass
                self.assertEqual(ctx.exception.code, 504)

                # Part C: Fresh frame arrives with frame_seq > initial_seq
                def mock_capture_and_ingest(cmd, timeout=1.0):
                    if cmd["type"] == "ctrl.camera.capture":
                        app.camera_service.ingest_robot_frame(b"\xff\xd8\xff\xe0BRAND_NEW_FRAME\xff\xd9")
                    return {"type": "sys.ack", "code": 0}

                mock_robot.send_command.side_effect = mock_capture_and_ingest
                with urlopen(f"http://127.0.0.1:{port}/snapshot") as resp:
                    self.assertEqual(resp.status, 200)
                    data = resp.read()
                    self.assertEqual(data, b"\xff\xd8\xff\xe0BRAND_NEW_FRAME\xff\xd9")
                    self.assertGreater(int(resp.headers.get("X-Frame-Seq")), initial_seq)
            finally:
                server.shutdown()
                server.server_close()
                app.close()

if __name__ == "__main__":
    unittest.main()
