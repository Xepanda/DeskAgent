"""
Desk Agent R1 Host Daemon Entrypoint.
Runs on TaishanPi RK3566 (Linux), hosting Life Services, HA Connector,
Codex Companion Router, Camera Leased Viewer, and Envelope RPC Dispatcher.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import sys
import threading
import time
import uuid
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Optional

from host.desk_agent.core.models import RequestEnvelope, ResponseEnvelope, ResponseStatus
from host.desk_agent.core.router import Router
from host.desk_agent.services.life_service import LifeService
from host.desk_agent.connectors.ha.ha_connector import HAConnector
from host.desk_agent.camera.camera_service import CameraViewerService
from companion.src.codex_companion import CodexCompanion

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

        codex_cfg = self.config.get("codex", {})
        self.companion = CodexCompanion(
            allowed_workspaces=codex_cfg.get("workspaces"),
            codex_bin=codex_cfg.get("codex_bin", "codex"),
        )

        # Router Setup
        self.router = Router()
        self._register_routes()

        # Background Reminder Delivery Scheduler
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="desk-reminder-scheduler", daemon=True
        )
        self._scheduler_thread.start()

    def _scheduler_loop(self) -> None:
        """Periodically polls for due reminder occurrences and triggers delivery."""
        while not self._scheduler_stop.is_set():
            try:
                if self._scheduler_stop.is_set():
                    break
                due = self.life_service.get_due_occurrences()
                for occ in due:
                    logger.info("Delivering due reminder: %s (id=%s)", occ.get("title"), occ.get("occurrence_id"))
                    self.life_service.mark_occurrence_ringing(occ["occurrence_id"])
                    self.life_service.queue_outbox_message(
                        topic="reminder.alert",
                        payload={
                            "occurrence_id": occ["occurrence_id"],
                            "reminder_id": occ["reminder_id"],
                            "title": occ.get("title", ""),
                        },
                    )
            except Exception as e:
                if not self._scheduler_stop.is_set():
                    logger.warning("Error in reminder scheduler: %s", e)
            self._scheduler_stop.wait(1.0)

    def close(self) -> None:
        self._scheduler_stop.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=2.0)
        self.life_service.close()

    def _load_config(self, config_path: Optional[str]) -> dict[str, Any]:
        default_cfg = {
            "device_id": "desk-agent-r1-host",
            "server": {"host": "127.0.0.1", "port": 8088},
            "auth_token": None,
            "storage": {"db_path": "life_service.db"},
            "home_assistant": {
                "base_url": "http://homeassistant.local:8123",
                "allowed_domains": ["light", "switch", "cover", "climate", "fan", "media_player", "scene", "input_boolean"],
            },
            "camera": {"default_lease_ms": 10000},
            "codex": {
                "workspaces": {
                    "epanda-desk-agent": os.path.realpath("."),
                    "desk-agent-core": os.path.realpath("."),
                }
            },
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
        self.router.register("reminder.update", self._handle_reminder_update)
        self.router.register("reminder.delete", self._handle_reminder_delete)
        self.router.register("reminder.complete", self._handle_reminder_complete)
        self.router.register("reminder.snooze", self._handle_reminder_snooze)
        self.router.register("reminder.stop_ringing", self._handle_reminder_stop_ringing)
        self.router.register("note.create", self._handle_note_create)
        self.router.register("note.get", self._handle_note_get)
        self.router.register("note.list", self._handle_note_list)
        self.router.register("note.delete", self._handle_note_delete)

        # Codex Companion
        self.router.register("codex.submit_turn", self._handle_codex_submit_turn)
        self.router.register("codex.get_task", self._handle_codex_get_task)
        self.router.register("codex.decide_approval", self._handle_codex_decide_approval)
        self.router.register("codex.list_workspaces", self._handle_codex_list_workspaces)

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

    def _handle_reminder_update(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        res = self.life_service.update_reminder(
            reminder_id=args["reminder_id"],
            title=args.get("title"),
            due_at=args.get("due_at"),
            status=args.get("status"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_reminder_delete(self, req: RequestEnvelope) -> ResponseEnvelope:
        reminder_id = req.arguments["reminder_id"]
        deleted = self.life_service.delete_reminder(reminder_id)
        return ResponseEnvelope.create_success(req.request_id, {"deleted": deleted})

    def _handle_reminder_complete(self, req: RequestEnvelope) -> ResponseEnvelope:
        item = self.life_service.complete_reminder(req.arguments["reminder_id"])
        return ResponseEnvelope.create_success(req.request_id, item)

    def _handle_reminder_snooze(self, req: RequestEnvelope) -> ResponseEnvelope:
        snooze_min = req.arguments.get("snooze_minutes", 10)
        item = self.life_service.snooze_reminder(req.arguments["reminder_id"], snooze_min)
        return ResponseEnvelope.create_success(req.request_id, item)

    def _handle_reminder_stop_ringing(self, req: RequestEnvelope) -> ResponseEnvelope:
        item = self.life_service.stop_ringing(req.arguments["occurrence_id"])
        return ResponseEnvelope.create_success(req.request_id, item)

    def _handle_note_create(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        res = self.life_service.create_note(
            note_id=args.get("note_id", req.request_id),
            title=args.get("title", "Untitled"),
            content=args["content"],
            tags=args.get("tags"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_note_list(self, req: RequestEnvelope) -> ResponseEnvelope:
        tag_filter = req.arguments.get("tag")
        query_filter = req.arguments.get("query")
        items = self.life_service.list_notes(query=query_filter, tag=tag_filter)
        return ResponseEnvelope.create_success(req.request_id, {"items": items})

    def _handle_note_get(self, req: RequestEnvelope) -> ResponseEnvelope:
        note_id = req.arguments["note_id"]
        item = self.life_service.get_note(note_id)
        if not item:
            return ResponseEnvelope.create_error(
                req.request_id,
                code="RESOURCE_NOT_FOUND",
                message=f"Note '{note_id}' not found",
            )
        return ResponseEnvelope.create_success(req.request_id, item)

    def _handle_note_delete(self, req: RequestEnvelope) -> ResponseEnvelope:
        note_id = req.arguments["note_id"]
        deleted = self.life_service.delete_note(note_id)
        return ResponseEnvelope.create_success(req.request_id, {"deleted": deleted})

    def _handle_codex_submit_turn(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        task_id = args.get("task_id") or f"tsk-{uuid.uuid4().hex[:8]}"
        workspace_alias = args.get("workspace_alias", "desk-agent-core")
        prompt = args.get("prompt", "")
        model = args.get("model")
        res = self.companion.submit_task(task_id, workspace_alias, prompt, model=model)
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_codex_get_task(self, req: RequestEnvelope) -> ResponseEnvelope:
        task_id = req.arguments.get("task_id", "")
        st = self.companion.get_task_status(task_id)
        if not st:
            return ResponseEnvelope.create_error(
                req.request_id,
                code="RESOURCE_NOT_FOUND",
                message=f"Task '{task_id}' not found",
            )
        return ResponseEnvelope.create_success(req.request_id, st)

    def _handle_codex_decide_approval(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        res = self.companion.respond_approval(
            approval_id=args["approval_id"],
            task_id=args["task_id"],
            decision=args["decision"],
            reason=args.get("reason"),
        )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_codex_list_workspaces(self, req: RequestEnvelope) -> ResponseEnvelope:
        return ResponseEnvelope.create_success(req.request_id, {"workspaces": list(self.companion.workspaces.keys())})

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

        # Inject initial valid JPEG frame so viewing begins immediately
        if self.camera_service._latest_jpeg is None:
            initial_jpeg = (
                b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x01\x00H\x00H\x00\x00"
                b"\xff\xdb\x00C\x00\x08\x06\x06\x07\x06\x05\x08\x07\x07\x07\t\t\x08"
                b"\xff\xc0\x00\x0b\x08\x00\x10\x00\x10\x01\x01\x11\x00\xff\xc4\x00"
                b"\x1f\x00\x00\x01\x05\x01\x01\x01\x01\x01\x01\x00\x00\x00\x00\x00"
                b"\x00\x00\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\xff\xda\x00"
                b"\x08\x01\x01\x00\x00?\x00\xbf\x80\xff\xd9"
            )
            self.camera_service.push_frame(initial_jpeg)

        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_camera_release(self, req: RequestEnvelope) -> ResponseEnvelope:
        client_id = req.client_id or req.arguments.get("client_id", "anonymous")
        released = self.camera_service.release_lease(client_id)
        return ResponseEnvelope.create_success(req.request_id, {"released": released})

    def _handle_motion_move(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        return ResponseEnvelope.create_success(
            req.request_id,
            {"acknowledged": True, "pan": args.get("pan"), "tilt": args.get("tilt")},
        )


def make_request_handler(app: DeskAgentHostApp):
    web_dir = Path(__file__).parent / "web"
    index_html_path = web_dir / "index.html"

    class DeskAgentHTTPHandler(BaseHTTPRequestHandler):
        def _check_auth(self) -> bool:
            auth_token = app.config.get("auth_token")
            if not auth_token:
                return True
            auth_hdr = self.headers.get("Authorization", "")
            tok_hdr = self.headers.get("X-Desk-Agent-Token", "")
            if auth_hdr.startswith("Bearer "):
                return auth_hdr[7:].strip() == auth_token
            if tok_hdr:
                return tok_hdr.strip() == auth_token
            return False

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
                if not self._check_auth():
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "Unauthorized"}')
                    return

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
            if not self._check_auth():
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error": "Unauthorized: invalid or missing bearer token"}')
                return

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
    host = app.config.get("server", {}).get("host", "127.0.0.1")

    handler_cls = make_request_handler(app)
    httpd = ThreadedHTTPServer((host, port), handler_cls)

    def handle_signal(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        app.close()
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
        app.close()
        httpd.server_close()
        logger.info("Host daemon terminated cleanly.")


if __name__ == "__main__":
    main()
