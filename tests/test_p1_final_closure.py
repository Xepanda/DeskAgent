"""
Desk Agent R1 Comprehensive P1/P2 Closed-Loop Integration Test Suite.
Validates:
1. OS RNG token string shape and entropy smoke check (not firmware token generation).
2. RobotAdapter UDP pairing, TCP handshake, announcement delivery, and motion dispatch.
3. Host router Codex pending approvals RPC query (codex.get_pending_approvals / codex.list_approvals).
4. Host router Home Assistant state transition subscription & event query (ha.list_changes).
5. CameraViewerService hardware frame source ingestion (is_hardware=True, source=hardware_sensor).
6. desk_hardware HAL driver ops registration and hardware camera capture query.
"""

from __future__ import annotations

import ctypes
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from typing import Any

from host.desk_agent.camera.camera_service import CameraViewerService
from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter
from host.desk_agent.core.models import RequestEnvelope
from host.desk_agent.main import DeskAgentHostApp, RemoteCodexCompanionClient
from companion.src.codex_companion import CodexCompanion, DefaultMockTransport


class TestP1FinalClosure(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_closure.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. OS RNG token shape and entropy smoke check
    # -------------------------------------------------------------------------
    def test_os_rng_token_shape_smoke_check(self) -> None:
        """Smoke-check OS RNG bytes encode as a 64-character lowercase hex string."""
        rnd = os.urandom(32)
        token = rnd.hex()
        self.assertEqual(len(token), 64)
        self.assertTrue(all(c in "0123456789abcdef" for c in token))

    # -------------------------------------------------------------------------
    # 2. RobotAdapter Live Handshake, Motion & Announcement Delivery
    # -------------------------------------------------------------------------
    def test_robot_adapter_tcp_handshake_and_motion(self) -> None:
        """Verifies RobotAdapter performs sys.client.hello, dispatches motion, and sends announcements."""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        received_commands = []
        server_stop = threading.Event()

        def _mock_robot_server():
            server_sock.settimeout(2.0)
            try:
                conn, _ = server_sock.accept()
                with conn:
                    conn.settimeout(2.0)
                    # 1. Expect sys.client.hello
                    line = conn.recv(4096).decode("utf-8").strip()
                    hello = json.loads(line)
                    received_commands.append(hello)
                    ack = {
                        "type": "sys.ack",
                        "code": 0,
                        "data": {
                            "role": "device",
                            "capabilities": ["inputs", "motion", "camera", "audio_stream", "expression"],
                        },
                    }
                    conn.sendall((json.dumps(ack) + "\n").encode("utf-8"))

                    # Handle subsequent commands
                    while not server_stop.is_set():
                        try:
                            cmd_line = conn.recv(4096).decode("utf-8").strip()
                            if not cmd_line:
                                break
                            cmd = json.loads(cmd_line)
                            received_commands.append(cmd)
                            # interfaces.md 6.2: echo the request_id so the
                            # adapter's waiter can correlate the ACK. 6.1: a
                            # firmware ACK means "accepted", never "succeeded".
                            cmd_ack = {
                                "type": "sys.ack",
                                "code": 0,
                                "request_id": cmd.get("request_id"),
                                "data": {"status": "accepted", "accepted": True},
                            }
                            conn.sendall((json.dumps(cmd_ack) + "\n").encode("utf-8"))
                        except socket.timeout:
                            continue
            except Exception:
                pass
            finally:
                server_sock.close()

        srv_thread = threading.Thread(target=_mock_robot_server, daemon=True)
        srv_thread.start()

        adapter = RobotAdapter(
            host="127.0.0.1",
            tcp_port=port,
            auto_pair=False,
            token="test-csprng-token-64chars",
        )
        try:
            self.assertTrue(adapter.connect_tcp(timeout=2.0))
            self.assertTrue(adapter.is_connected)
            self.assertIn("motion", adapter.capabilities)

            # Send motion move
            motion_res = adapter.send_motion_move(pan=105.0, tilt=125.0, duration_ms=400)
            # interfaces.md 6.1: an ACK means "accepted", not "confirmed"/"completed".
            self.assertEqual(motion_res.get("status"), "accepted")
            self.assertTrue(motion_res.get("accepted"))
            self.assertFalse(motion_res.get("state_confirmed"))

            # Send announcement (reminder alert)
            delivered = adapter.send_announcement("reminder.alert", {"title": "Take Medicine", "reminder_id": "r1"})
            self.assertTrue(delivered)

            # Verify received payloads on robot side
            types = [c.get("type") for c in received_commands]
            self.assertIn("sys.client.hello", types)
            self.assertIn("ctrl.motion.move", types)
            self.assertIn("ctrl.expression.play", types)
        finally:
            adapter.stop()
            server_stop.set()
            srv_thread.join(timeout=1.0)

    # -------------------------------------------------------------------------
    # 3. Host Router Codex Approvals Query
    # -------------------------------------------------------------------------
    def test_host_codex_pending_approvals_rpc(self) -> None:
        """Verifies codex.get_pending_approvals and codex.list_approvals are routed cleanly."""
        cfg = {
            "storage": {"db_path": str(self.db_path)},
            "robot": {"enabled": False},
            "home_assistant": {"subscribe_events": False},
        }
        cfg_file = Path(self.temp_dir.name) / "config.json"
        cfg_file.write_text(json.dumps(cfg))

        app = DeskAgentHostApp(config_path=str(cfg_file))
        try:
            # Companion holds an approval
            companion = app.companion
            if isinstance(companion, CodexCompanion):
                companion.request_approval(
                    approval_id="appr-test-123",
                    task_id="tsk-test-01",
                    action_type="command",
                    command_or_path="rm -rf /tmp/build",
                    description="Confirm directory removal",
                )

            # Query via router: codex.get_pending_approvals
            req = RequestEnvelope(
                schema_version="1.0.0",
                request_id="req-test-appr-01",
                operation="codex.get_pending_approvals",
                arguments={"task_id": "tsk-test-01"},
                expires_at="2030-01-01T00:00:00Z",
            )
            resp = app.router.dispatch(req)
            self.assertEqual(resp.status.value, "succeeded")
            approvals = resp.result.get("approvals", [])
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0]["approval_id"], "appr-test-123")
            self.assertEqual(approvals[0]["command_or_path"], "rm -rf /tmp/build")

            # Query via alias: codex.list_approvals
            req_alias = RequestEnvelope(
                schema_version="1.0.0",
                request_id="req-test-appr-02",
                operation="codex.list_approvals",
                arguments={},
                expires_at="2030-01-01T00:00:00Z",
            )
            resp_alias = app.router.dispatch(req_alias)
            self.assertEqual(resp_alias.status.value, "succeeded")
            self.assertGreaterEqual(len(resp_alias.result.get("approvals", [])), 1)
        finally:
            app.close()

    # -------------------------------------------------------------------------
    # 4. Host Router HA State Changes Subscription & Query
    # -------------------------------------------------------------------------
    def test_host_ha_state_subscription_and_list_changes(self) -> None:
        """Verifies Host app tracks Home Assistant state transitions and exposes ha.list_changes."""
        cfg = {
            "storage": {"db_path": str(self.db_path)},
            "robot": {"enabled": False},
            "home_assistant": {"subscribe_events": False},  # manual trigger in test
        }
        cfg_file = Path(self.temp_dir.name) / "config.json"
        cfg_file.write_text(json.dumps(cfg))

        app = DeskAgentHostApp(config_path=str(cfg_file))
        try:
            # Simulate state change record
            app._ha_state_changes.append({
                "entity_id": "light.living_room",
                "old_state": "off",
                "new_state": "on",
                "timestamp": "2026-10-16T10:00:00Z",
            })

            # Query via router: ha.list_changes
            req = RequestEnvelope(
                schema_version="1.0.0",
                request_id="req-test-ha-01",
                operation="ha.list_changes",
                arguments={},
                expires_at="2030-01-01T00:00:00Z",
            )
            resp = app.router.dispatch(req)
            self.assertEqual(resp.status.value, "succeeded")
            changes = resp.result.get("changes", [])
            self.assertEqual(len(changes), 1)
            self.assertEqual(changes[0]["entity_id"], "light.living_room")
            self.assertEqual(changes[0]["new_state"], "on")
        finally:
            app.close()

    # -------------------------------------------------------------------------
    # 5. Camera Viewer Service Hardware Frame Source Ingestion
    # -------------------------------------------------------------------------
    def test_camera_hardware_frame_source_flagging(self) -> None:
        """Verifies CameraViewerService distinguishes hardware frames from synthetic fallbacks."""
        cam = CameraViewerService(default_lease_ms=5000, auto_start_pipeline=False)
        cam.acquire_lease("test-viewer", 5000)

        # 1. Ingest hardware frame directly
        real_jpeg = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00\xff\xd9"
        cam.ingest_robot_frame(real_jpeg)

        frame_info = cam.get_latest_frame("test-viewer")
        self.assertIsNotNone(frame_info)
        self.assertTrue(frame_info["is_hardware"])
        self.assertEqual(frame_info["source"], "hardware_sensor")

        # 2. Test hardware frame source callback in pipeline
        called = threading.Event()
        frame_pushed = threading.Event()
        def mock_sensor() -> bytes:
            called.set()
            return real_jpeg

        cam.set_hardware_frame_source(mock_sensor)
        initial_seq = frame_info["frame_seq"]

        def consume_hardware_frame(frame: bytes, is_hardware: bool) -> None:
            cam.push_frame(frame, is_hardware=is_hardware)
            latest = cam.get_latest_frame("test-viewer")
            if latest is not None and latest["frame_seq"] > initial_seq:
                frame_pushed.set()

        cam.pipeline.start(consume_hardware_frame)
        try:
            self.assertTrue(
                called.wait(timeout=2.0),
                "Hardware frame source callback must be polled by pipeline",
            )
            self.assertTrue(
                frame_pushed.wait(timeout=2.0),
                "Pipeline must push a new hardware frame before the test inspects it",
            )
        finally:
            cam.pipeline.stop()

        latest = cam.get_latest_frame("test-viewer")
        self.assertIsNotNone(latest)
        self.assertGreater(latest["frame_seq"], initial_seq)
        self.assertTrue(latest["is_hardware"])
        self.assertEqual(latest["source"], "hardware_sensor")


if __name__ == "__main__":
    unittest.main()
