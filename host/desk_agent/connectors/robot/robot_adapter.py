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
import struct
import threading
import time
import uuid
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# Maximum allowed frame size in bytes (interfaces.md 6.3 rule 2)
MAX_FRAME_SIZE = 65536


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
        # Request ids that currently have a live waiter in send_command().
        # A response is only ever stored/delivered when its request_id belongs
        # to an active waiter (interfaces.md 6.2 rule 2 & 3).
        self._awaiting_ids: set[str] = set()
        # Serialises socket reads taken by send_command()'s worker-less fallback
        # so two waiters cannot split one coalesced ACK segment between them.
        self._fallback_recv_lock = threading.Lock()
        self._req_counter = 0
        self._req_counter_lock = threading.Lock()
        self._event_listeners: dict[str, list[Callable[[dict[str, Any]], None]]] = {}
        self._tool_request_handler: Optional[Callable[[dict[str, Any]], None]] = None
        self._recv_buf: str = ""
        self._raw_recv_buf = bytearray()
        self._camera_frame_callbacks: list[Callable[[bytes], None]] = []
        self._audio_frame_callbacks: list[Callable[[bytes], None]] = []

    def _new_request_id(self) -> str:
        """Generates a collision-free request id.

        Uses a uuid4 hex component plus a process-local monotonic counter so that
        concurrent callers within the same millisecond can never collide
        (interfaces.md 6.2 rule 4). Always matches ^[a-zA-Z0-9_-]+$ and is
        8..64 chars (interfaces.md 1.1).
        """
        with self._req_counter_lock:
            self._req_counter += 1
            seq = self._req_counter
        return f"req-{uuid.uuid4().hex[:16]}-{seq}"

    @property
    def is_connected(self) -> bool:
        return self._connected and self._sock is not None

    @property
    def capabilities(self) -> list[str]:
        return list(self._capabilities)

    def register_camera_frame_callback(self, cb: Callable[[bytes], None]) -> None:
        """Registers a callback to receive raw JPEG camera frames from WSPK binary stream."""
        with self._lock:
            if cb not in self._camera_frame_callbacks:
                self._camera_frame_callbacks.append(cb)

    def register_audio_frame_callback(self, cb: Callable[[bytes], None]) -> None:
        """Registers a callback to receive audio stream frames from WSPK binary stream."""
        with self._lock:
            if cb not in self._audio_frame_callbacks:
                self._audio_frame_callbacks.append(cb)

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

    def _process_incoming_bytes(self, raw_buf: bytearray, data: bytes) -> None:
        """Processes incoming bytes, unbundling WSPK binary media frames and newline-delimited JSON commands."""
        raw_buf.extend(data)
        while raw_buf:
            if raw_buf.startswith(b"WSPK"):
                # Header is 16 bytes: <4sBBHII
                if len(raw_buf) < 16:
                    break
                magic, frame_type, flags, stream_id, sequence, payload_size = struct.unpack("<4sBBHII", raw_buf[:16])
                if len(raw_buf) < 16 + payload_size:
                    break
                payload = bytes(raw_buf[16:16 + payload_size])
                del raw_buf[:16 + payload_size]
                if frame_type in (0x02, 0x03):  # Video (0x02) or Image (0x03) JPEG frame
                    with self._lock:
                        cbs = list(self._camera_frame_callbacks)
                    for cb in cbs:
                        try:
                            cb(payload)
                        except Exception as cb_err:
                            logger.warning("Camera frame callback error: %s", cb_err)
                elif frame_type == 0x01:  # Audio frame
                    with self._lock:
                        cbs = list(self._audio_frame_callbacks)
                    for cb in cbs:
                        try:
                            cb(payload)
                        except Exception as cb_err:
                            logger.warning("Audio frame callback error: %s", cb_err)
            elif b"\n" in raw_buf:
                nl_idx = raw_buf.find(b"\n")
                line_bytes = bytes(raw_buf[:nl_idx])
                del raw_buf[:nl_idx + 1]
                line_str = line_bytes.decode("utf-8", errors="replace").strip()
                if line_str:
                    self._handle_incoming_line(line_str)
            else:
                if len(raw_buf) > MAX_FRAME_SIZE:
                    logger.warning("Frame buffer exceeded MAX_FRAME_SIZE (%d bytes); discarding", MAX_FRAME_SIZE)
                    raw_buf.clear()
                break

    def _connection_loop(self) -> None:
        """Background loop ensuring continuous connectivity and reading incoming events/requests."""
        raw_buf = bytearray()
        while not self._stop_event.is_set():
            if not self.is_connected:
                raw_buf.clear()
                if not self.connect_tcp(timeout=2.0):
                    self._stop_event.wait(self.reconnect_interval)
                    continue

            try:
                sock = self._sock
                if not sock:
                    continue
                rlist, _, xlist = select.select([sock], [], [sock], 0.2)
                if xlist:
                    logger.info("Socket exceptional condition, closing")
                    raw_buf.clear()
                    with self._lock:
                        self._close_socket()
                    continue

                if rlist:
                    data = sock.recv(8192)
                    if not data:
                        logger.info("Robot disconnected TCP connection")
                        raw_buf.clear()
                        with self._lock:
                            self._close_socket()
                        continue
                    self._process_incoming_bytes(raw_buf, data)
            except BlockingIOError:
                pass
            except Exception as e:
                if not self._stop_event.is_set():
                    logger.debug("Robot connection loop notice: %s", e)
                    raw_buf.clear()
                    with self._lock:
                        self._close_socket()
                    self._stop_event.wait(self.reconnect_interval)

    def _handle_incoming_line(self, line: str) -> None:
        """Processes incoming JSON packet from firmware."""
        try:
            msg = json.loads(line)
            msg_type = msg.get("type", "")
            req_id = msg.get("request_id")

            # Route response acks.
            # interfaces.md 6.2: only ever deliver a response to the waiter that
            # owns its exact request_id. Responses without a request_id, or whose
            # waiter has already timed out, are discarded -- never handed to an
            # unrelated waiter and never silently consumed from a generic bucket.
            if msg_type in ("sys.ack", "sys.nack") or (req_id and not msg.get("operation") and msg_type != "tool.request"):
                if not req_id:
                    logger.debug(
                        "Discarding uncorrelated %s without request_id (interfaces.md 6.2)",
                        msg_type,
                    )
                else:
                    with self._lock:
                        if req_id in self._awaiting_ids:
                            self._pending_responses[req_id] = msg
                        else:
                            logger.debug(
                                "Discarding late/unsolicited %s for request_id=%s (no active waiter)",
                                msg_type,
                                req_id,
                            )

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
        """Sends a JSON command to the robot and waits for its correlated response.

        interfaces.md 6.2: the request_id is always present on the wire, the
        waiter only consumes the response whose request_id matches exactly, and
        the pending slot is always released -- on success, on timeout, and on
        send failure -- so a late ACK can never leak into a later request.
        """
        req_id = cmd_dict.get("request_id")
        if not req_id:
            req_id = self._new_request_id()
            cmd_dict["request_id"] = req_id

        with self._lock:
            if not self.is_connected or not self._sock:
                raise ConnectionError("Robot is not connected")
            sock = self._sock
            # Register the waiter BEFORE writing to the socket so a fast ACK
            # cannot be dropped as "unsolicited".
            self._awaiting_ids.add(req_id)
            try:
                payload = (json.dumps(cmd_dict) + "\n").encode("utf-8")
                sock.sendall(payload)
            except Exception:
                self._awaiting_ids.discard(req_id)
                self._pending_responses.pop(req_id, None)
                self._close_socket()
                raise

        try:
            # Wait for the response correlated to THIS request_id only.
            deadline = time.time() + timeout
            while time.time() < deadline and not self._stop_event.is_set():
                with self._lock:
                    if req_id in self._pending_responses:
                        return self._pending_responses.pop(req_id)

                worker_alive = bool(self._worker_thread and self._worker_thread.is_alive())
                if not worker_alive and self._sock:
                    # Fallback used only when the receiver worker thread is not
                    # running. It must stay deadline-respecting: never issue an
                    # unbounded blocking recv(), and serialise readers so two
                    # waiters cannot split one coalesced ACK segment between
                    # them (which would hang the loser past its deadline).
                    remaining = deadline - time.time()
                    if remaining <= 0:
                        break
                    if not self._fallback_recv_lock.acquire(timeout=min(remaining, 0.05)):
                        continue
                    try:
                        try:
                            sock = self._sock
                            if sock is None:
                                continue
                            slice_sec = max(0.01, min(remaining, 0.05))
                            try:
                                rlist, _, _ = select.select([sock], [], [], slice_sec)
                            except Exception:
                                rlist = []
                            if not rlist:
                                time.sleep(0.01)
                                continue
                            prev_timeout = sock.gettimeout()
                            try:
                                sock.settimeout(slice_sec)
                                data = sock.recv(4096)
                            except (socket.timeout, BlockingIOError):
                                continue
                            except Exception:
                                data = b""
                            finally:
                                try:
                                    sock.settimeout(prev_timeout)
                                except OSError:
                                    pass
                            if data:
                                self._process_incoming_bytes(self._raw_recv_buf, data)
                            else:
                                with self._lock:
                                    self._close_socket()
                                break
                        except Exception:
                            # A fallback read must never raise: the caller still
                            # owns its deadline and must receive the timeout shape.
                            time.sleep(0.01)
                    finally:
                        self._fallback_recv_lock.release()
                else:
                    time.sleep(0.02)
        finally:
            # Release the waiter and drop any response that raced in after the
            # deadline. It must never be served to a different request.
            with self._lock:
                self._awaiting_ids.discard(req_id)
                self._pending_responses.pop(req_id, None)

        return {
            "type": "sys.nack",
            "code": 408,
            "status": "timeout",
            "request_id": req_id,
            "error": {
                "code": "TIMEOUT",
                "message": "No ACK received from robot within timeout",
            },
        }

    @staticmethod
    def _classify_response(res: dict[str, Any]) -> tuple[str, Optional[str], bool]:
        """Maps a raw wire response to the frozen contract vocabulary.

        Returns (status, error_code, retryable) where status is one of
        "accepted" | "failed" | "unknown" (interfaces.md 6.1). A firmware ACK
        only ever means "accepted" -- it is never completion.
        """
        if not isinstance(res, dict):
            return "unknown", "OPERATION_TIMEOUT", True

        res_type = res.get("type")
        code = res.get("code")
        raw_error = res.get("error")
        err_code = raw_error.get("code") if isinstance(raw_error, dict) else (raw_error if isinstance(raw_error, str) else None)

        # No response at all / deadline elapsed.
        if res.get("status") == "timeout" or code in (408, 504) or err_code in ("TIMEOUT", "OPERATION_TIMEOUT"):
            return "unknown", "OPERATION_TIMEOUT", True

        if res_type == "sys.nack":
            if code == 409 or err_code == "RESOURCE_BUSY":
                return "failed", "RESOURCE_BUSY", True
            if code in (502, 503) or err_code == "UPSTREAM_UNAVAILABLE":
                return "failed", "UPSTREAM_UNAVAILABLE", True
            if err_code == "STATE_UNCONFIRMED":
                return "unknown", "STATE_UNCONFIRMED", False
            if err_code == "OPERATION_TIMEOUT":
                return "unknown", "OPERATION_TIMEOUT", True
            return "failed", "HARDWARE_FAULT", False

        if code in (None, 0):
            # sys.ack code:0 == accepted, never completed.
            return "accepted", None, False

        return "failed", "HARDWARE_FAULT", False

    def send_motion_move(self, pan: float, tilt: float, duration_ms: int = 500, request_id: Optional[str] = None) -> dict[str, Any]:
        """Dispatches a gimbal motion command and reports the contract outcome.

        A firmware `sys.ack` only proves the command was accepted, so this never
        returns "completed"/"succeeded". Physical completion is signalled
        separately by the `resource.motion.completed` event (interfaces.md 6.1).
        Under CR-002, motion is strictly ACK-only (state_confirmed: false).
        """
        cmd: dict[str, Any] = {
            "type": "ctrl.motion.move",
            "data": {
                "pan_deg": float(pan),
                "tilt_deg": float(tilt),
                "duration_ms": int(duration_ms),
            },
        }
        if request_id:
            cmd["request_id"] = request_id
        res = self.send_command(cmd, timeout=2.0)
        status, error_code, retryable = self._classify_response(res)
        return {
            "status": status,
            "accepted": status == "accepted",
            "state_confirmed": False,
            "error_code": error_code,
            "retryable": retryable,
            "pan": pan,
            "tilt": tilt,
            "duration_ms": duration_ms,
            "request_id": res.get("request_id") or cmd.get("request_id"),
        }

    def send_motion_stop(self, emergency: bool = False, request_id: Optional[str] = None) -> dict[str, Any]:
        """Dispatches a gimbal stop command and reports the contract outcome."""
        cmd: dict[str, Any] = {
            "type": "ctrl.motion.stop",
            "data": {
                "emergency": bool(emergency),
            },
        }
        if request_id:
            cmd["request_id"] = request_id
        res = self.send_command(cmd, timeout=2.0)
        status, error_code, retryable = self._classify_response(res)
        return {
            "status": status,
            "accepted": status == "accepted",
            "state_confirmed": False,
            "error_code": error_code,
            "retryable": retryable,
            "emergency": bool(emergency),
            "request_id": res.get("request_id") or cmd.get("request_id"),
        }

    def send_camera_acquire(self, client_id: str = "host", lease_ms: int = 10000, timeout: float = 2.0) -> dict[str, Any]:
        """Requests camera viewing lease from robot arbiter."""
        cmd = {
            "type": "ctrl.camera.acquire",
            "client_id": client_id,
            "lease_duration_ms": lease_ms,
        }
        return self.send_command(cmd, timeout=timeout)

    def send_camera_release(self, client_id: str = "host", timeout: float = 2.0) -> dict[str, Any]:
        """Releases camera viewing lease on robot arbiter."""
        cmd = {
            "type": "ctrl.camera.release",
            "client_id": client_id,
        }
        return self.send_command(cmd, timeout=timeout)

    def send_camera_capture(self, timeout: float = 2.0) -> dict[str, Any]:
        """Triggers single frame capture and WSPK media transmission from robot camera."""
        cmd = {
            "type": "ctrl.camera.capture",
        }
        return self.send_command(cmd, timeout=timeout)

    def send_announcement(self, event_type: str, payload: dict[str, Any]) -> bool:
        """
        Dispatches announcement / alert to robot speaker and expression screen.
        Implements Host announcement_channel interface.
        """
        if not self.is_connected:
            return False

        title = payload.get("title", "Alert")
        text = payload.get("text") or title
        cmd = {
            "type": "ctrl.expression.play",
            "data": {
                "preset": "alert",
                "text": text,
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
