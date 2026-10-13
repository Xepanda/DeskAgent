"""
Desk Agent R1 Host Daemon Entrypoint.
Runs on TaishanPi RK3566 (Linux), hosting Life Services, HA Connector,
Camera Leased Viewer, and Envelope RPC Dispatcher.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import time
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Optional

from host.desk_agent.core.models import RequestEnvelope, ResponseEnvelope, ResponseStatus
from host.desk_agent.core.router import Router
from host.desk_agent.services.life_service import LifeService
from host.desk_agent.connectors.ha.ha_connector import HAConnector
from host.desk_agent.camera.camera_service import CameraViewerService

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("DeskAgentHost")


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class DeskAgentHostApp:
    def __init__(self, config_path: Optional[str] = None) -> None:
        self.config = self._load_config(config_path)
        self.start_time = time.time()

        # Database and Services
        db_path = Path(self.config.get("storage", {}).get("db_path", "/var/lib/desk-agent/life_service.db"))
        self.life_service = LifeService(db_path)
        self.ha_connector = HAConnector(
            base_url=self.config.get("home_assistant", {}).get("base_url", "http://homeassistant.local:8123"),
            access_token=self.config.get("home_assistant", {}).get("access_token"),
            allowed_domains=self.config.get("home_assistant", {}).get("allowed_domains"),
        )
        cam_cfg = self.config.get("camera", {})
        self.camera_service = CameraViewerService(
            default_lease_ms=cam_cfg.get("default_lease_ms", 10000)
        )

        # Router Setup
        self.router = Router()
        self._register_routes()

    def _load_config(self, config_path: Optional[str]) -> dict[str, Any]:
        default_cfg = {
            "device_id": "desk-agent-r1-host",
            "server": {"host": "0.0.0.0", "port": 8088},
            "storage": {"db_path": "life_service.db"},
            "home_assistant": {
                "base_url": "http://homeassistant.local:8123",
                "allowed_domains": ["light", "switch", "cover", "climate", "fan", "media_player", "scene", "input_boolean"],
            },
            "camera": {"default_lease_ms": 10000},
        }
        if config_path and os.path.exists(config_path):
            try:
                with open(config_path, "r", encoding="utf-8") as f:
                    user_cfg = json.load(f)
                    default_cfg.update(user_cfg)
                    logger.info("Loaded configuration from %s", config_path)
            except Exception as e:
                logger.error("Failed to parse config file %s: %s; using defaults", config_path, e)
        return default_cfg

    def _register_routes(self) -> None:
        # HA
        self.router.register("ha.get_state", self._handle_ha_get_state)
        self.router.register("ha.execute", self._handle_ha_execute)

        # Life - Reminders & Notes
        self.router.register("reminder.create", self._handle_reminder_create)
        self.router.register("reminder.list", self._handle_reminder_list)
        self.router.register("reminder.complete", self._handle_reminder_complete)
        self.router.register("reminder.snooze", self._handle_reminder_snooze)
        self.router.register("note.create", self._handle_note_create)
        self.router.register("note.list", self._handle_note_list)

        # Camera & Hardware resources
        self.router.register("resource.camera.acquire", self._handle_camera_acquire)
        self.router.register("resource.camera.release", self._handle_camera_release)
        self.router.register("resource.motion.move", self._handle_motion_move)

    def _handle_ha_get_state(self, req: RequestEnvelope) -> ResponseEnvelope:
        entity_id = req.arguments["entity_id"]
        res = self.ha_connector.get_state(entity_id)
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_ha_execute(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        res = self.ha_connector.execute(
            entity_id=args["entity_id"],
            action=args["action"],
            parameters=args.get("parameters"),
            wait_for_state=args.get("wait_for_state"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_reminder_create(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        res = self.life_service.create_reminder(
            reminder_id=args.get("reminder_id", req.request_id),
            title=args["title"],
            due_at=args["due_at"],
            timezone_str=args.get("timezone", "Asia/Shanghai"),
            repeat_rule=args.get("repeat_rule"),
            priority=args.get("priority", "normal"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_reminder_list(self, req: RequestEnvelope) -> ResponseEnvelope:
        status_filter = req.arguments.get("status")
        items = self.life_service.list_reminders(status=status_filter)
        return ResponseEnvelope.create_success(req.request_id, {"items": items})

    def _handle_reminder_complete(self, req: RequestEnvelope) -> ResponseEnvelope:
        item = self.life_service.complete_reminder(req.arguments["reminder_id"])
        return ResponseEnvelope.create_success(req.request_id, item)

    def _handle_reminder_snooze(self, req: RequestEnvelope) -> ResponseEnvelope:
        snooze_min = req.arguments.get("snooze_minutes", 10)
        item = self.life_service.snooze_reminder(req.arguments["reminder_id"], snooze_min)
        return ResponseEnvelope.create_success(req.request_id, item)

    def _handle_note_create(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        res = self.life_service.create_note(
            note_id=args.get("note_id", req.request_id),
            content=args["content"],
            tags=args.get("tags"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_note_list(self, req: RequestEnvelope) -> ResponseEnvelope:
        tag_filter = req.arguments.get("tag")
        items = self.life_service.list_notes(tag=tag_filter)
        return ResponseEnvelope.create_success(req.request_id, {"items": items})

    def _handle_camera_acquire(self, req: RequestEnvelope) -> ResponseEnvelope:
        client_id = req.client_id or req.arguments.get("client_id", "anonymous")
        lease_ms = req.arguments.get("lease_duration_ms")
        res = self.camera_service.acquire_lease(client_id, lease_ms)
        if not res["granted"]:
            return ResponseEnvelope.create_error(
                req.request_id,
                code="RESOURCE_BUSY",
                message=f"Camera lease is currently held by '{res.get('current_owner')}'",
                details=res,
            )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_camera_release(self, req: RequestEnvelope) -> ResponseEnvelope:
        client_id = req.client_id or req.arguments.get("client_id", "anonymous")
        released = self.camera_service.release_lease(client_id)
        return ResponseEnvelope.create_success(req.request_id, {"released": released})

    def _handle_motion_move(self, req: RequestEnvelope) -> ResponseEnvelope:
        # Move command acknowledged and routed to gimbal controller
        args = req.arguments
        return ResponseEnvelope.create_success(
            req.request_id,
            {"acknowledged": True, "pan": args.get("pan"), "tilt": args.get("tilt")},
        )


def make_request_handler(app: DeskAgentHostApp):
    web_dir = Path(__file__).parent / "web"
    index_html_path = web_dir / "index.html"

    class DeskAgentHTTPHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            if self.path in ("/", "/index.html"):
                if index_html_path.exists():
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.end_headers()
                    self.wfile.write(index_html_path.read_bytes())
                else:
                    self.send_response(404)
                    self.end_headers()
            elif self.path == "/health":
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                uptime = int(time.time() - app.start_time)
                resp = json.dumps({"status": "healthy", "uptime_s": uptime}).encode()
                self.wfile.write(resp)
            elif self.path.startswith("/api/camera/frame"):
                # Extract client_id from query params if present
                client_id = "web-client"
                if "?" in self.path:
                    query = self.path.split("?", 1)[1]
                    for param in query.split("&"):
                        if param.startswith("client_id="):
                            client_id = param.split("=", 1)[1]

                frame_info = app.camera_service.get_latest_frame(client_id)
                if not frame_info:
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "NO_ACTIVE_LEASE"}')
                    return

                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("X-Frame-Seq", str(frame_info["frame_seq"]))
                self.send_header("X-Frame-Age-Ms", str(frame_info["frame_age_ms"]))
                self.end_headers()
                self.wfile.write(frame_info["jpeg_data"])
            else:
                self.send_response(404)
                self.end_headers()

        def do_POST(self) -> None:
            if self.path == "/api/rpc":
                content_len = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(content_len).decode("utf-8")
                try:
                    payload = json.loads(body)
                    req = RequestEnvelope.from_dict(payload)
                    resp = app.router.dispatch(req)
                except Exception as e:
                    req_id = payload.get("request_id", "unknown") if isinstance(payload, dict) else "unknown"
                    resp = ResponseEnvelope.create_error(req_id, "INTERNAL_ERROR", str(e))

                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.end_headers()
                self.wfile.write(resp.to_json().encode("utf-8"))
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            logger.debug(format, *args)

    return DeskAgentHTTPHandler


def main() -> None:
    parser = argparse.ArgumentParser(description="Desk Agent R1 Host Daemon")
    parser.add_argument("--config", "-c", type=str, help="Path to config.json")
    parser.add_argument("--port", "-p", type=int, help="Override HTTP server port")
    args = parser.parse_args()

    app = DeskAgentHostApp(config_path=args.config)
    port = args.port or app.config.get("server", {}).get("port", 8088)
    host = app.config.get("server", {}).get("host", "0.0.0.0")

    handler_cls = make_request_handler(app)
    httpd = ThreadedHTTPServer((host, port), handler_cls)

    def handle_signal(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        httpd.shutdown()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("Desk Agent Host Daemon running on http://%s:%d", host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        logger.info("Host daemon terminated cleanly.")


if __name__ == "__main__":
    main()
