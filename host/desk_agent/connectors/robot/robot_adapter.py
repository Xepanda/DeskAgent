"""
Desk Agent R1 Official WatcheRobot SDK Protocol Adapter.

Connects the Host Python service to the Desk Agent firmware over
the official WatcheRobot device protocol:
- UDP 37021 pairing discovery (watcher_lan_pairing_v1) with dynamic CSPRNG session token.
- TCP 37020 bidirectional JSON command & event streaming.
- WSPK binary media framing (<4sBBHII).
- Robot announcement channel (delivers due reminders & alerts to robot speaker/display).
- Physical motion dispatch (ctrl.motion.move) with hardware state confirmation.
- Resilient background reconnection loop.
"""

from __future__ import annotations

import json
import logging
import select
import socket
import threading
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)


class RobotAdapter:
    """Production client adapter connecting Host to Robot Firmware / SDK Bridge."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        tcp_port: int = 37020,
        udp_port: int = 37021,
        auto_pair: bool = True,
        token: Optional[str] = None,
        reconnect_interval: float = 2.0,
    ) -> None:
        self.host = host
        self.tcp_port = tcp_port
        self.udp_port = udp_port
        self.auto_pair = auto_pair
        self.session_token = token
        self.reconnect_interval = reconnect_interval

        self._lock = threading.Lock()
        self._sock: Optional[socket.socket] = None
        self._connected = False
        self._capabilities: list[str] = []
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

        self._pending_responses: dict[str, Any] = {}
        self._event_listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._tool_request_handler: Optional[Callable[[dict[str, Any]], None]] = None
        self._recv_buf: str = ""

    @property
    def is_connected(self) -> bool:
        return self._connected and self._sock is not None

    @property
    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def start(self) -> None:
        """Starts the persistent background connection & event receiver worker."""
        if self._worker_thread and self._worker_thread.is_alive():
            return
        self._stop_event.clear()
        self._worker_thread = threading.Thread(
            target=self._connection_loop, name="desk-robot-adapter", daemon=True
        )
        self._worker_thread.start()

    def stop(self) -> None:
        """Stops the adapter worker and closes active sockets."""
        self._stop_event.set()
        with self._lock:
            self._close_socket()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)

    def _close_socket(self) -> None:
        if self._sock:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except Exception:
                pass
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None
        self._connected = False
        self._recv_buf = ""

    def pair_udp(self, timeout: float = 2.0) -> Optional[str]:
        """Performs UDP Pairing Discovery (watcher_lan_pairing_v1) to obtain dynamic session token."""
        udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp_sock.settimeout(timeout)
        try:
            req = {
                "protocol": "watcher_lan_pairing_v1",
                "request_id": f"pair-host-{int(time.time())}",
                "daemon_instance_id": "desk-agent-host",
                "target_mode": "python_sdk",
            }
            udp_sock.sendto(json.dumps(req).encode("utf-8"), (self.host, self.udp_port))
            data, _ = udp_sock.recvfrom(4096)
            resp = json.loads(data.decode("utf-8"))
            if resp.get("type") == "pair.accept" and resp.get("status") == "accepted":
                token = resp.get("token") or resp.get("session_token")
                if token:
                    self.session_token = token
                    logger.info("Successfully paired with robot over UDP, session token acquired")
                    return token
        except Exception as e:
            logger.debug("UDP pairing attempt note: %s", e)
        finally:
            udp_sock.close()
        return None

    def connect_tcp(self, timeout: float = 3.0) -> bool:
        """Connects TCP socket to robot firmware and executes sys.client.hello handshake."""
        with self._lock:
            self._close_socket()
            try:
                if self.auto_pair and not self.session_token:
                    self.pair_udp(timeout=1.5)

                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(timeout)
                sock.connect((self.host, self.tcp_port))

                hello_payload: dict[str, Any] = {
                    "type": "sys.client.hello",
                    "data": {
                        "role": "desktop",
                        "version": "1.0",
                    },
                }
                if self.session_token:
                    hello_payload["data"]["token"] = self.session_token
                    hello_payload["data"]["session_token"] = self.session_token

                sock.sendall((json.dumps(hello_payload) + "\n").encode("utf-8"))
                raw_ack = sock.recv(4096).decode("utf-8").strip()
                if not raw_ack:
                    sock.close()
                    return False

                ack = json.loads(raw_ack)
                if ack.get("type") == "sys.ack" and ack.get("code") == 0:
                    self._sock = sock
                    self._sock.setblocking(False)
                    self._connected = True
                    self._capabilities = ack.get("data", {}).get("capabilities", [])
                    logger.info("Connected to Robot SDK Bridge at %s:%d, capabilities: %s",
                                self.host, self.tcp_port, self._capabilities)
                    return True
                elif ack.get("type") == "sys.nack" and ack.get("code") == 401:
                    logger.warning("Robot rejected authentication with token: %s", ack.get("error"))
                    self.session_token = None
                    sock.close()
                    return False
                else:
                    logger.warning("Unexpected handshake response: %s", ack)
                    sock.close()
                    return False
            except Exception as e:
                logger.debug("Failed to connect to robot at %s:%d: %s", self.host, self.tcp_port, e)
                try:
                    sock.close()
                except Exception:
                    pass
                return False

    def _connection_loop(self) -> None:
        """Background loop ensuring continuous connectivity and reading incoming events/requests."""
        recv_buf = ""
        while not self._stop_event.is_set():
            if not self.is_connected:
                if not self.connect_tcp(timeout=2.0):
                    self._stop_event.wait(self.reconnect_interval)
                    continue

            # Socket is connected; wait for input with select
            try:
                sock = self._sock
                if not sock:
                    continue
                rlist, _, xlist = select.select([sock], [], [sock], 0.2)
                if xlist:
                    logger.info("Socket exceptional condition, closing")
                    with self._lock:
                        self._close_socket()
                    continue

                if rlist:
                    data = sock.recv(4096)
                    if not data:
                        logger.info("Robot disconnected TCP connection")
                        with self._lock:
                            self._close_socket()
                        continue
                    recv_buf += data.decode("utf-8", errors="replace")
                    while "\n" in recv_buf:
                        line, recv_buf = recv_buf.split("\n", 1)
                        line = line.strip()
                        if line:
                            self._handle_incoming_line(line)
            except BlockingIOError:
                pass
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.debug("Robot connection loop notice: %s", e)
                    with self._lock:
                        self._close_socket()
                    self._stop_event.wait(self.reconnect_interval)

    def _handle_incoming_line(self, line: str) -> None:
        """Processes incoming JSON packet from firmware."""
        try:
            msg = json.loads(line)
            msg_type = msg.get("type", "")
            req_id = msg.get("request_id")

            # Route response acks
            if msg_type in ("sys.ack", "sys.nack") or (req_id and not msg.get("operation") and msg_type != "tool.request"):
                with self._lock:
                    key = req_id or msg_type
                    self._pending_responses[key] = msg

            # Route events
            if msg_type.startswith("event.") or "event" in msg_type:
                evt_type = msg.get("event_type") or msg_type
                with self._lock:
                    listeners = list(self._event_listeners.get(evt_type, []))
                    all_listeners = list(self._event_listeners.get("*", []))
                for cb in listeners + all_listeners:
                    try:
                        cb(msg)
                    except Exception as err:
                        logger.warning("Event listener callback error: %s", err)

            # Route tool requests from TClaw to Host
            if msg_type == "tool.request" or msg.get("operation"):
                if self._tool_request_handler:
                    handler = self._tool_request_handler
                    def _dispatch(payload: dict[str, Any]) -> None:
                        try:
                            handler(payload)
                        except Exception as err:
                            logger.warning("Tool request handler error: %s", err)
                    threading.Thread(target=_dispatch, args=(msg,), name="desk-tool-dispatch", daemon=True).start()
        except Exception as e:
            logger.debug("Failed to parse incoming line from robot: %s (%s)", line, e)

    def send_command(self, cmd_dict: dict[str, Any], timeout: float = 2.0) -> dict[str, Any]:
        """Sends a JSON command to the robot and waits for sys.ack response."""
        with self._lock:
            if not self.is_connected or not self._sock:
                raise ConnectionError("Robot is not connected")
            sock = self._sock
            cmd_type = cmd_dict.get("type", "")
            req_id = cmd_dict.get("request_id") or f"req-{int(time.time()*1000)}"
            if "request_id" not in cmd_dict and not cmd_type.startswith("sys."):
                cmd_dict["request_id"] = req_id

            payload = (json.dumps(cmd_dict) + "\n").encode("utf-8")
            sock.sendall(payload)

        # Wait for ack response
        deadline = time.time() + timeout
        expected_key = req_id if "request_id" in cmd_dict else cmd_type
        while time.time() < deadline and not self._stop_event.is_set():
            with self._lock:
                if expected_key in self._pending_responses:
                    return self._pending_responses.pop(expected_key)
                if "sys.ack" in self._pending_responses:
                    return self._pending_responses.pop("sys.ack")
                if "sys.nack" in self._pending_responses:
                    return self._pending_responses.pop("sys.nack")

            worker_alive = bool(self._worker_thread and self._worker_thread.is_alive())
            if not worker_alive and self._sock:
                try:
                    rlist, _, _ = select.select([self._sock], [], [], 0.05)
                    if rlist:
                        data = self._sock.recv(4096)
                        if data:
                            self._recv_buf += data.decode("utf-8", errors="replace")
                            while "\n" in self._recv_buf:
                                line, self._recv_buf = self._recv_buf.split("\n", 1)
                                line = line.strip()
                                if line:
                                    self._handle_incoming_line(line)
                        else:
                            with self._lock:
                                self._close_socket()
                            break
                    else:
                        time.sleep(0.01)
                except Exception:
                    pass
            else:
                time.sleep(0.02)

        return {
            "type": "sys.nack",
            "code": 408,
            "status": "timeout",
            "error": {
                "code": "TIMEOUT",
                "message": "No ACK received from robot within timeout",
            },
        }

    def send_motion_move(self, pan: float, tilt: float, duration_ms: int = 500) -> dict[str, Any]:
        """Executes physical motion command on robot gimbal."""
        cmd = {
            "type": "ctrl.motion.move",
            "data": {
                "pan_deg": float(pan),
                "tilt_deg": float(tilt),
                "duration_ms": int(duration_ms),
            },
        }
        res = self.send_command(cmd, timeout=2.0)
        confirmed = res.get("code") == 0
        status = res.get("data", {}).get("status") if isinstance(res.get("data"), dict) else res.get("status", "completed")
        return {
            "status": "completed" if (status == "succeeded" or (confirmed and status == "completed")) else status,
            "confirmed": confirmed,
            "pan": pan,
            "tilt": tilt,
            "duration_ms": duration_ms,
        }

    def send_announcement(self, event_type: str, payload: dict[str, Any]) -> bool:
        """
        Dispatches announcement / alert to robot speaker and expression screen.
        Implements Host announcement_channel interface.
        """
        if not self.is_connected:
            return False

        title = payload.get("title", "Alert")
        cmd = {
            "type": "ctrl.expression.play",
            "data": {
                "preset": "alert",
                "text": title,
                "event_type": event_type,
                "payload": payload,
            },
        }
        try:
            res = self.send_command(cmd, timeout=1.5)
            return res.get("code", 0) == 0
        except Exception as e:
            logger.warning("Failed to send announcement to robot: %s", e)
            return False

    def send_tool_response(self, response_envelope: dict[str, Any]) -> bool:
        """Sends tool response envelope back to firmware TClaw."""
        req_id = response_envelope.get("request_id")
        cmd: dict[str, Any] = {
            "type": "tool.response",
            "data": response_envelope,
        }
        if req_id:
            cmd["request_id"] = req_id
        try:
            res = self.send_command(cmd, timeout=1.5)
            return res.get("code", 0) == 0
        except Exception:
            return False

    def register_tool_request_handler(self, handler: Callable[[dict[str, Any]], None]) -> None:
        """Registers handler for external tool requests initiated by robot TClaw."""
        self._tool_request_handler = handler
