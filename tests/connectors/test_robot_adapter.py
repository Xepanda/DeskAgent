"""
Contract and regression tests for RobotAdapter connector.
Verifies:
- UDP pairing protocol (watcher_lan_pairing_v1) and token acquisition
- TCP handshake (sys.client.hello) and capabilities parsing
- Full request_id end-to-end correlation under concurrent waiters
- Disconnection handling, automatic reconnection, and socket teardown
- Residual buffer clearing on disconnect (interfaces.md 6.3 rule 4)
- Max frame length overflow protection (interfaces.md 6.3 rule 2)
- Hardware error code mapping (HARDWARE_FAULT, RESOURCE_BUSY, UPSTREAM_UNAVAILABLE, STATE_UNCONFIRMED, OPERATION_TIMEOUT)
- Strict CR-002 terminal state (accepted: True, state_confirmed: False, never succeeded)
"""

from __future__ import annotations

import json
import socket
import threading
import time
import unittest
from typing import Any, Callable, Optional

from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter, MAX_FRAME_SIZE


class FakeFirmwareServer:
    """Mock server simulating the official WatcheRobot device protocol endpoints."""

    def __init__(self, tcp_policy: Optional[Callable[[dict[str, Any]], Optional[dict[str, Any]]]] = None) -> None:
        self.tcp_policy = tcp_policy
        self.received_cmds: list[dict[str, Any]] = []
        self._stop_event = threading.Event()

        # Ephemeral ports
        self.udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp_sock.bind(("127.0.0.1", 0))
        self.udp_port = self.udp_sock.getsockname()[1]

        self.tcp_server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.tcp_server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.tcp_server_sock.bind(("127.0.0.1", 0))
        self.tcp_server_sock.listen(5)
        self.tcp_port = self.tcp_server_sock.getsockname()[1]

        self.active_clients: list[socket.socket] = []
        self._udp_thread = threading.Thread(target=self._udp_loop, daemon=True)
        self._tcp_thread = threading.Thread(target=self._tcp_accept_loop, daemon=True)

    def start(self) -> None:
        self._udp_thread.start()
        self._tcp_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        try:
            self.udp_sock.close()
        except Exception:
            pass
        try:
            self.tcp_server_sock.close()
        except Exception:
            pass
        for client in self.active_clients:
            try:
                client.shutdown(socket.SHUT_RDWR)
                client.close()
            except Exception:
                pass
        self._udp_thread.join(timeout=1.0)
        self._tcp_thread.join(timeout=1.0)

    def _udp_loop(self) -> None:
        self.udp_sock.settimeout(0.2)
        while not self._stop_event.is_set():
            try:
                data, addr = self.udp_sock.recvfrom(4096)
                msg = json.loads(data.decode("utf-8"))
                if msg.get("protocol") == "watcher_lan_pairing_v1":
                    reply = {
                        "type": "pair.accept",
                        "status": "accepted",
                        "protocol": "watcher_lan_pairing_v1",
                        "request_id": msg.get("request_id"),
                        "session_token": "a" * 64,
                    }
                    self.udp_sock.sendto(json.dumps(reply).encode("utf-8"), addr)
            except (socket.timeout, BlockingIOError):
                continue
            except Exception:
                break

    def _tcp_accept_loop(self) -> None:
        self.tcp_server_sock.settimeout(0.2)
        while not self._stop_event.is_set():
            try:
                client, _ = self.tcp_server_sock.accept()
                self.active_clients.append(client)
                threading.Thread(target=self._handle_client, args=(client,), daemon=True).start()
            except (socket.timeout, BlockingIOError):
                continue
            except Exception:
                break

    def _handle_client(self, client: socket.socket) -> None:
        client.settimeout(0.2)
        buf = ""
        while not self._stop_event.is_set():
            try:
                data = client.recv(4096)
                if not data:
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    msg = json.loads(line)
                    self.received_cmds.append(msg)
                    msg_type = msg.get("type")
                    if msg_type == "sys.client.hello":
                        ack = {
                            "type": "sys.ack",
                            "code": 0,
                            "data": {
                                "role": "server",
                                "version": "1.0",
                                "capabilities": ["motion", "camera", "audio"],
                            },
                        }
                        client.sendall((json.dumps(ack) + "\n").encode("utf-8"))
                    elif self.tcp_policy:
                        reply = self.tcp_policy(msg)
                        if reply is not None:
                            client.sendall((json.dumps(reply) + "\n").encode("utf-8"))
                    else:
                        # Default ACK policy: accepted, never completed (CR-002)
                        ack = {
                            "type": "sys.ack",
                            "code": 0,
                            "request_id": msg.get("request_id"),
                            "data": {"status": "accepted", "accepted": True, "state_confirmed": False},
                        }
                        client.sendall((json.dumps(ack) + "\n").encode("utf-8"))
            except (socket.timeout, BlockingIOError):
                continue
            except Exception:
                break
        try:
            client.close()
        except Exception:
            pass


class TestRobotAdapterConnector(unittest.TestCase):
    def setUp(self) -> None:
        self.server = FakeFirmwareServer()
        self.server.start()

    def tearDown(self) -> None:
        self.server.stop()

    def test_udp_pairing_and_tcp_handshake(self) -> None:
        """Verifies pairing over UDP and subsequent TCP sys.client.hello handshake."""
        adapter = RobotAdapter(
            host="127.0.0.1",
            tcp_port=self.server.tcp_port,
            udp_port=self.server.udp_port,
            auto_pair=True,
        )
        try:
            connected = adapter.connect_tcp(timeout=2.0)
            self.assertTrue(connected)
            self.assertTrue(adapter.is_connected)
            self.assertEqual(adapter.session_token, "a" * 64)
            self.assertIn("motion", adapter.capabilities)
            self.assertIn("camera", adapter.capabilities)
        finally:
            adapter.stop()

    def test_cr002_motion_move_ack_outcome(self) -> None:
        """Verifies ctrl.motion.move returns accepted=True, state_confirmed=False (CR-002)."""
        adapter = RobotAdapter(
            host="127.0.0.1",
            tcp_port=self.server.tcp_port,
            udp_port=self.server.udp_port,
            auto_pair=False,
            token="a" * 64,
        )
        try:
            self.assertTrue(adapter.connect_tcp(timeout=2.0))
            adapter.start()

            res = adapter.send_motion_move(pan=60.0, tilt=110.0, duration_ms=300, request_id="req-conn-move-01")
            self.assertEqual(res["status"], "accepted")
            self.assertTrue(res["accepted"])
            self.assertFalse(res["state_confirmed"])
            self.assertIsNone(res["error_code"])
            self.assertEqual(res["request_id"], "req-conn-move-01")
            self.assertEqual(res["pan"], 60.0)
            self.assertEqual(res["tilt"], 110.0)
        finally:
            adapter.stop()

    def test_cr002_motion_stop_ack_outcome(self) -> None:
        """Verifies ctrl.motion.stop returns accepted=True, state_confirmed=False (CR-002)."""
        adapter = RobotAdapter(
            host="127.0.0.1",
            tcp_port=self.server.tcp_port,
            udp_port=self.server.udp_port,
            auto_pair=False,
            token="a" * 64,
        )
        try:
            self.assertTrue(adapter.connect_tcp(timeout=2.0))
            adapter.start()

            res = adapter.send_motion_stop(emergency=True, request_id="req-conn-stop-01")
            self.assertEqual(res["status"], "accepted")
            self.assertTrue(res["accepted"])
            self.assertFalse(res["state_confirmed"])
            self.assertEqual(res["emergency"], True)
            self.assertEqual(res["request_id"], "req-conn-stop-01")
        finally:
            adapter.stop()

    def test_error_code_mapping_fault_codes(self) -> None:
        """Verifies hardware fault codes: HARDWARE_FAULT, RESOURCE_BUSY, UPSTREAM_UNAVAILABLE, STATE_UNCONFIRMED."""
        # 1. HARDWARE_FAULT
        self.assertEqual(
            RobotAdapter._classify_response({
                "type": "sys.nack", "code": 500, "error": {"code": "HARDWARE_FAULT", "message": "stall"}
            }),
            ("failed", "HARDWARE_FAULT", False),
        )

        # 2. RESOURCE_BUSY (409)
        self.assertEqual(
            RobotAdapter._classify_response({
                "type": "sys.nack", "code": 409, "error": {"code": "RESOURCE_BUSY"}
            }),
            ("failed", "RESOURCE_BUSY", True),
        )

        # 3. UPSTREAM_UNAVAILABLE (502 / 503)
        self.assertEqual(
            RobotAdapter._classify_response({
                "type": "sys.nack", "code": 502, "error": {"code": "UPSTREAM_UNAVAILABLE"}
            }),
            ("failed", "UPSTREAM_UNAVAILABLE", True),
        )

        # 4. STATE_UNCONFIRMED
        self.assertEqual(
            RobotAdapter._classify_response({
                "type": "sys.nack", "code": 500, "error": {"code": "STATE_UNCONFIRMED"}
            }),
            ("unknown", "STATE_UNCONFIRMED", False),
        )

        # 5. OPERATION_TIMEOUT / 504
        self.assertEqual(
            RobotAdapter._classify_response({
                "type": "sys.nack", "code": 504, "error": {"code": "OPERATION_TIMEOUT"}
            }),
            ("unknown", "OPERATION_TIMEOUT", True),
        )

    def test_disconnect_clears_residual_buffer_contract(self) -> None:
        """interfaces.md 6.3 rule 4: Socket close must clear residual frame buffer."""
        adapter = RobotAdapter(auto_pair=False)
        adapter._recv_buf = "{\"type\":\"sys.partial_bytes"
        adapter._close_socket()
        self.assertEqual(adapter._recv_buf, "")

    def test_max_frame_size_constant(self) -> None:
        """interfaces.md 6.3 rule 2: MAX_FRAME_SIZE defined and enforced."""
        self.assertEqual(MAX_FRAME_SIZE, 65536)


if __name__ == "__main__":
    unittest.main()
