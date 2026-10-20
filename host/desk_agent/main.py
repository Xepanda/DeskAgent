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
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any, Callable, Mapping, Optional

from host.desk_agent.core.models import RequestEnvelope, ResponseEnvelope, ResponseStatus
from host.desk_agent.core.router import Router
from host.desk_agent.services.life_service import LifeService
from host.desk_agent.connectors.ha.ha_connector import HAConnector
from host.desk_agent.camera.camera_service import CameraViewerService
from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter
from companion.src.codex_companion import CodexCompanion

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("DeskAgentHost")


from companion.src.codex_companion import RemoteCodexCompanionClient


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


class DeskAgentHostApp:
    def __init__(self, config_path: Optional[str] = None) -> None:
        self.config = self._load_config(config_path)
        self.start_time = time.time()

        # Database and Services
        db_path = Path(self.config.get("storage", {}).get("db_path", "/var/lib/desk-agent/life_service.db"))
        self.life_service = LifeService(db_path)
        ha_cfg = self.config.get("home_assistant", {})
        self.ha_connector = HAConnector(
            base_url=ha_cfg.get("base_url", "http://homeassistant.local:8123"),
            access_token=ha_cfg.get("access_token"),
            allowed_domains=ha_cfg.get("allowed_domains"),
            allowed_entities=ha_cfg.get("allowed_entities"),
            denied_entities=ha_cfg.get("denied_entities"),
            allowed_services=ha_cfg.get("allowed_services"),
            denied_services=ha_cfg.get("denied_services"),
        )
        cam_cfg = self.config.get("camera", {})
        self.camera_service = CameraViewerService(
            default_lease_ms=cam_cfg.get("default_lease_ms", 10000),
            auto_start_pipeline=cam_cfg.get("auto_start_pipeline", True),
            allow_synthetic=cam_cfg.get("allow_synthetic", True),
        )

        codex_cfg = self.config.get("codex", {})
        companion_url = codex_cfg.get("companion_url") or self.config.get("codex_companion", {}).get("endpoint")
        if companion_url:
            logger.info("Configured remote Windows Codex Companion at %s", companion_url)
            self.companion = RemoteCodexCompanionClient(
                endpoint_url=companion_url,
                auth_token=codex_cfg.get("auth_token") or self.config.get("auth_token"),
            )
        else:
            self.companion = CodexCompanion(
                allowed_workspaces=codex_cfg.get("workspaces"),
                codex_bin=codex_cfg.get("codex_bin", "codex"),
            )

        # Router Setup
        self.router = Router()
        self._register_routes()

        # Robot Announcement / Audio Channel & Delivery Worker
        self._announcement_channel: Optional[Callable[[str, dict[str, Any]], bool]] = None
        self._delivered_alerts: list[dict[str, Any]] = []
        self._robot_client: Optional[Any] = None

        # Robot Hardware Link / SDK Bridge Adapter
        robot_cfg = self.config.get("robot", {})
        self.robot_adapter: Optional[RobotAdapter] = None
        if robot_cfg.get("enabled", True):
            self.robot_adapter = RobotAdapter(
                host=robot_cfg.get("host", "127.0.0.1"),
                tcp_port=robot_cfg.get("tcp_port", 37020),
                udp_port=robot_cfg.get("udp_port", 37021),
                auto_pair=robot_cfg.get("auto_pair", True),
                token=robot_cfg.get("token"),
                reconnect_interval=robot_cfg.get("reconnect_interval", 2.0),
            )
            self.register_robot_hardware_client(self.robot_adapter)
            self.register_announcement_channel(self.robot_adapter.send_announcement)
            self.robot_adapter.register_tool_request_handler(self._on_robot_tool_request)
            self.robot_adapter.register_camera_frame_callback(self.camera_service.ingest_robot_frame)

            def _on_codex_event(event_type: str, data: dict[str, Any]) -> None:
                if event_type in ("codex.turn_completed", "codex.completed"):
                    summary = data.get("summary") or f"Task {data.get('task_id', '')} finished"
                    announcement = {
                        "title": "Codex 任务完成",
                        "text": f"Codex任务已完成：{summary}",
                        "sound": "notify",
                        "task_id": data.get("task_id"),
                    }
                    if self.robot_adapter:
                        self.robot_adapter.send_announcement("codex.completed", announcement)

            if hasattr(self.companion, "add_event_listener"):
                self.companion.add_event_listener(_on_codex_event)

            self.robot_adapter.start()

        # HA State Subscription
        self._ha_state_changes: list[dict[str, Any]] = []
        def _on_ha_state_change(entity_id: str, old_state: Optional[str], new_state: Optional[str]) -> None:
            logger.info("HA entity %s changed: %s -> %s", entity_id, old_state, new_state)
            self._ha_state_changes.append({
                "entity_id": entity_id,
                "old_state": old_state,
                "new_state": new_state,
                "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            })
            if len(self._ha_state_changes) > 100:
                self._ha_state_changes.pop(0)

        ha_cfg = self.config.get("home_assistant", {})
        if ha_cfg.get("subscribe_events", True):
            self._ha_sub_stop: Optional[Callable[[], None]] = self.ha_connector.subscribe_state_changes(
                _on_ha_state_change,
                interval_sec=float(ha_cfg.get("poll_interval_sec", 2.0)),
            )
        else:
            self._ha_sub_stop = None

        # Background Reminder Scheduler & Outbox Delivery Worker
        self._scheduler_stop = threading.Event()
        self._scheduler_thread = threading.Thread(
            target=self._scheduler_loop, name="desk-reminder-scheduler", daemon=True
        )
        self._scheduler_thread.start()

        self._delivery_thread = threading.Thread(
            target=self._delivery_loop, name="desk-outbox-delivery", daemon=True
        )
        self._delivery_thread.start()

    def register_announcement_channel(self, channel: Callable[[str, dict[str, Any]], bool]) -> None:
        """Registers external robot audio / TTS speaker delivery channel."""
        self._announcement_channel = channel

    def _deliver_robot_announcement(
        self, title: str, reminder_id: str, occurrence_id: str, is_catchup: bool = False
    ) -> bool:
        """Dispatches reminder alert to robot announcement channel or event stream."""
        if not self._announcement_channel:
            logger.warning(
                "No active robot announcement channel registered; alert for reminder '%s' (id=%s) remains pending",
                title, reminder_id
            )
            return False

        logger.info(
            "Broadcasting robot audio reminder: '%s' (reminder_id=%s, is_catchup=%s)",
            title, reminder_id, is_catchup
        )
        alert_payload = {
            "reminder_id": reminder_id,
            "occurrence_id": occurrence_id,
            "title": title,
            "is_catchup": is_catchup,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }

        try:
            delivered = bool(self._announcement_channel("reminder.alert", alert_payload))
            if delivered:
                self._delivered_alerts.append(alert_payload)
            return delivered
        except Exception as e:
            logger.warning("Announcement channel returned error: %s", e)
            return False

    def _on_robot_tool_request(self, raw_msg: dict[str, Any]) -> None:
        """Handles tool requests from robot TClaw dialogue engine and routes them."""
        try:
            if not isinstance(raw_msg, dict):
                logger.warning("Invalid raw tool request format from robot: %s", raw_msg)
                return

            if raw_msg.get("type") == "tool.request" and isinstance(raw_msg.get("data"), dict):
                env_dict = dict(raw_msg["data"])
            else:
                env_dict = dict(raw_msg)

            if "request_id" not in env_dict or "operation" not in env_dict:
                logger.warning("Tool request missing required fields: %s", raw_msg)
                return

            if "expires_at" not in env_dict:
                # Default 60s expiration if omitted by firmware
                env_dict["expires_at"] = (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat().replace("+00:00", "Z")

            req = RequestEnvelope.from_dict(env_dict)
            resp = self.router.dispatch(req)
            if self.robot_adapter:
                self.robot_adapter.send_tool_response(resp.to_dict())
        except Exception as exc:
            logger.exception("Failed to process tool request from robot: %s", exc)
            if isinstance(raw_msg, dict):
                req_id = raw_msg.get("request_id") or (raw_msg.get("data", {}).get("request_id") if isinstance(raw_msg.get("data"), dict) else None)
                if req_id and self.robot_adapter:
                    err_resp = ResponseEnvelope.create_error(
                        request_id=req_id,
                        code="INTERNAL_ERROR",
                        message=str(exc),
                    )
                    self.robot_adapter.send_tool_response(err_resp.to_dict())

    def _delivery_loop(self) -> None:
        """Consumes outbox messages and reliably delivers to robot announcement channels with retry."""
        while not self._scheduler_stop.is_set():
            try:
                if self._scheduler_stop.is_set():
                    break
                pending = self.life_service.get_pending_outbox_messages(limit=10)
                for item in pending:
                    outbox_id = item["outbox_id"]
                    topic = item.get("topic") or item.get("event_type")
                    payload = item.get("payload", {})

                    if topic == "reminder.alert":
                        title = payload.get("title", "Reminder")
                        rem_id = payload.get("reminder_id", "")
                        occ_id = payload.get("occurrence_id", "")
                        is_catchup = bool(payload.get("is_catchup", False))
                        success = self._deliver_robot_announcement(title, rem_id, occ_id, is_catchup=is_catchup)
                        if success:
                            self.life_service.mark_outbox_dispatched(outbox_id)
                            logger.info("Outbox alert %s successfully dispatched to robot audio", outbox_id)
                        else:
                            backoff = min(300, 2 ** (item.get("retry_count", 0) + 1))
                            self.life_service.record_outbox_failure(
                                outbox_id, "Delivery channel busy or rejected", backoff_seconds=backoff
                            )
                    else:
                        self.life_service.mark_outbox_dispatched(outbox_id)
            except Exception as e:
                if not self._scheduler_stop.is_set():
                    logger.warning("Error in outbox delivery worker: %s", e)
            self._scheduler_stop.wait(0.5)

    def _scheduler_loop(self) -> None:
        """Periodically polls for due reminder occurrences and triggers delivery."""
        while not self._scheduler_stop.is_set():
            try:
                if self._scheduler_stop.is_set():
                    break
                due = self.life_service.get_due_occurrences()
                for occ in due:
                    is_catchup = bool(occ.get("is_catchup", False))
                    logger.info(
                        "Delivering due reminder: %s (id=%s, catchup=%s)",
                        occ.get("title"), occ.get("occurrence_id"), is_catchup
                    )
                    self.life_service.trigger_occurrence_to_outbox(
                        occurrence_id=occ["occurrence_id"],
                        topic="reminder.alert",
                        is_catchup=is_catchup,
                    )
            except Exception as e:
                if not self._scheduler_stop.is_set():
                    logger.warning("Error in reminder scheduler: %s", e)
            self._scheduler_stop.wait(1.0)

    def close(self) -> None:
        self._scheduler_stop.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=2.0)
        if hasattr(self, "_delivery_thread") and self._delivery_thread.is_alive():
            self._delivery_thread.join(timeout=2.0)
        if getattr(self, "robot_adapter", None):
            self.robot_adapter.stop()
        if getattr(self, "_ha_sub_stop", None):
            try:
                self._ha_sub_stop()
            except Exception:
                pass
        if getattr(self, "companion", None) and hasattr(self.companion, "close"):
            try:
                self.companion.close()
            except Exception:
                pass
        self.camera_service.stop_capture()
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
            "robot": {
                "enabled": True,
                "host": "127.0.0.1",
                "tcp_port": 37020,
                "udp_port": 37021,
                "auto_pair": True,
                "reconnect_interval": 2.0,
            },
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
        self.router.register("ha.list_changes", self._handle_ha_list_changes)

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
        self.router.register("codex.get_pending_approvals", self._handle_codex_get_pending_approvals)
        self.router.register("codex.list_approvals", self._handle_codex_get_pending_approvals)
        self.router.register("codex.list_workspaces", self._handle_codex_list_workspaces)

        # Camera & Hardware resources
        self.router.register("resource.camera.acquire", self._handle_camera_acquire)
        self.router.register("resource.camera.release", self._handle_camera_release)
        self.router.register("resource.motion.move", self._handle_motion_move)
        self.router.register("resource.motion.stop", self._handle_motion_stop)

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

    def _handle_ha_list_changes(self, req: RequestEnvelope) -> ResponseEnvelope:
        return ResponseEnvelope.create_success(req.request_id, {"changes": list(self._ha_state_changes)})

    def _handle_reminder_create(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        title = args.get("title") or args.get("text") or "Reminder"
        due_at = args.get("due_at") or args.get("due_time")
        if not due_at:
            now = datetime.now(timezone.utc)
            due_at = (now + timedelta(minutes=10)).isoformat()
        try:
            res = self.life_service.create_reminder(
                reminder_id=args.get("reminder_id", req.request_id),
                title=title,
                due_at=due_at,
                timezone_str=args.get("timezone", "Asia/Shanghai"),
                repeat_rule=args.get("repeat_rule"),
                priority=args.get("priority", "normal"),
            )
            return ResponseEnvelope.create_success(req.request_id, res)
        except Exception as e:
            return ResponseEnvelope.create_error(
                req.request_id,
                code="EXECUTION_FAILED",
                message=str(e),
            )

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
        title = args.get("title", "Untitled")
        content = args.get("content") or args.get("note") or title
        try:
            res = self.life_service.create_note(
                note_id=args.get("note_id", req.request_id),
                title=title,
                content=content,
                tags=args.get("tags"),
            )
            return ResponseEnvelope.create_success(req.request_id, res)
        except Exception as e:
            return ResponseEnvelope.create_error(
                req.request_id,
                code="EXECUTION_FAILED",
                message=str(e),
            )

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

    def _handle_codex_get_pending_approvals(self, req: RequestEnvelope) -> ResponseEnvelope:
        task_id = req.arguments.get("task_id")
        approvals = self.companion.get_pending_approvals(task_id=task_id)
        return ResponseEnvelope.create_success(req.request_id, {"approvals": approvals})

    def _handle_codex_list_workspaces(self, req: RequestEnvelope) -> ResponseEnvelope:
        return ResponseEnvelope.create_success(req.request_id, {"workspaces": list(self.companion.workspaces.keys())})

    def _handle_camera_acquire(self, req: RequestEnvelope) -> ResponseEnvelope:
        client_id = req.client_id or req.arguments.get("client_id", "anonymous")
        lease_ms = req.arguments.get("lease_duration_ms")
        target_fps = req.arguments.get("target_fps")
        res = self.camera_service.acquire_lease(client_id, lease_ms, target_fps=target_fps)
        if not res["granted"]:
            return ResponseEnvelope.create_error(
                req.request_id,
                code="RESOURCE_BUSY",
                message=f"Camera lease is currently held by '{res.get('current_owner')}'",
                details=res,
            )
        if self.has_robot_hardware_connected() and hasattr(self._robot_client, "send_command"):
            try:
                self._robot_client.send_command({
                    "type": "ctrl.camera.acquire",
                    "client_id": client_id,
                    "lease_duration_ms": res.get("lease_duration_ms", 10000),
                }, timeout=1.0)
                self._robot_client.send_command({
                    "type": "ctrl.camera.capture",
                }, timeout=1.0)
            except Exception as e:
                logger.debug("Failed to dispatch camera acquire/capture to robot: %s", e)
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_camera_release(self, req: RequestEnvelope) -> ResponseEnvelope:
        client_id = req.client_id or req.arguments.get("client_id", "anonymous")
        released = self.camera_service.release_lease(client_id)
        if released and self.has_robot_hardware_connected() and hasattr(self._robot_client, "send_command"):
            try:
                self._robot_client.send_command({
                    "type": "ctrl.camera.release",
                    "client_id": client_id,
                }, timeout=1.0)
            except Exception as e:
                logger.debug("Failed to dispatch camera release to robot: %s", e)
        return ResponseEnvelope.create_success(req.request_id, {"released": released})

    def register_robot_hardware_client(self, client: Any) -> None:
        """Registers the connected robot hardware / SDK bridge client."""
        self._robot_client = client

    def has_robot_hardware_connected(self) -> bool:
        """Checks if physical robot hardware link is actively connected."""
        client = getattr(self, "_robot_client", None)
        if client is None:
            return False
        if hasattr(client, "is_connected"):
            return bool(client.is_connected)
        return True

    # Frozen contract vocabulary for motion outcomes (contracts/error_codes.json,
    # interfaces.md 6.1). Firmware `sys.ack` means "accepted", never "completed".
    _MOTION_ERROR_CODES = frozenset({
        "HARDWARE_FAULT",
        "OPERATION_TIMEOUT",
        "RESOURCE_BUSY",
        "UPSTREAM_UNAVAILABLE",
        "STATE_UNCONFIRMED",
    })
    _MOTION_ERROR_MESSAGES = {
        "HARDWARE_FAULT": "Robot hardware driver reported a fault while executing the motion command",
        "OPERATION_TIMEOUT": "No response was received from the robot within the motion timeout window",
        "RESOURCE_BUSY": "Robot hardware resource is held by another owner; arbitration refused the command",
        "UPSTREAM_UNAVAILABLE": "Robot hardware link is disconnected or unreachable",
        "STATE_UNCONFIRMED": "Command was accepted but the physical state could not be confirmed",
    }

    @classmethod
    def _motion_outcome(cls, ack: Any) -> tuple[str, bool, bool, Optional[str], bool]:
        """Normalizes a robot adapter motion result into the contract vocabulary.

        Returns ``(status, accepted, state_confirmed, error_code, retryable)``
        where ``status`` is one of ``accepted`` | ``failed`` | ``unknown``.

        An ACK only ever proves acceptance, so a legacy ``completed``/``succeeded``
        claim from the hardware link is deliberately downgraded to ``accepted``
        and never surfaces as a success (interfaces.md 6.1 R1).
        Under CR-002 and R3a/R12 protocol adjudication, motion is strictly ACK-only
        (open-loop PWM without encoder feedback), so state_confirmed is ALWAYS False.
        """
        if not isinstance(ack, dict):
            return "unknown", False, False, "OPERATION_TIMEOUT", True

        raw_status = str(ack.get("status", "")).lower()
        accepted = bool(ack.get("accepted", False))
        error_code = ack.get("error_code")
        retryable = bool(ack.get("retryable", False))

        if error_code == "STATE_UNCONFIRMED":
            return "unknown", False, False, "STATE_UNCONFIRMED", False

        if raw_status in ("failed", "unknown"):
            if error_code not in cls._MOTION_ERROR_CODES:
                error_code = "HARDWARE_FAULT" if raw_status == "failed" else "OPERATION_TIMEOUT"
            return raw_status, False, False, error_code, retryable

        if raw_status in ("accepted", "completed", "succeeded", "in_progress") or accepted:
            return "accepted", True, False, None, False

        # Unrecognized shape: do not guess success.
        return "unknown", False, False, "OPERATION_TIMEOUT", True

    def _handle_motion_move(self, req: RequestEnvelope) -> ResponseEnvelope:
        args = req.arguments
        pan = float(args.get("pan_deg", args.get("pan", 90.0)))
        tilt = float(args.get("tilt_deg", args.get("tilt", 120.0)))
        duration_ms = int(args.get("duration_ms", 500))

        # Enforce physical mechanical limit clamping (Pan 0..180, Tilt 100..140)
        clamped_pan = max(0.0, min(180.0, pan))
        clamped_tilt = max(100.0, min(140.0, tilt))

        # Update coordinate model in camera service
        self.camera_service.update_motion_coordinates(clamped_pan, clamped_tilt)

        context = {
            "pan": clamped_pan,
            "tilt": clamped_tilt,
            "duration_ms": duration_ms,
        }

        if not self.has_robot_hardware_connected():
            # No hardware link: nothing was dispatched. Report the missing link
            # honestly instead of a fabricated success.
            return ResponseEnvelope.create_error(
                req.request_id,
                code="UPSTREAM_UNAVAILABLE",
                message="No physical robot hardware link is connected; motion was not dispatched",
                retryable=True,
                details={
                    **context,
                    "hardware_link": "disconnected",
                    "reason": "NO_PHYSICAL_ROBOT_CONNECTED",
                },
            )

        try:
            ack = self._robot_client.send_motion_move(clamped_pan, clamped_tilt, duration_ms)
        except Exception as exc:
            return ResponseEnvelope.create_error(
                req.request_id,
                code="UPSTREAM_UNAVAILABLE",
                message=f"Robot hardware link failed during motion dispatch: {exc}",
                retryable=True,
            )

        status, accepted, state_confirmed, error_code, retryable = self._motion_outcome(ack)

        if status in ("failed", "unknown"):
            code = error_code or "HARDWARE_FAULT"
            return ResponseEnvelope.create_error(
                req.request_id,
                code=code,
                message=self._MOTION_ERROR_MESSAGES.get(code, "Robot motion command was not accepted"),
                retryable=retryable,
                details={**context, "hardware_link": "connected"},
                status=ResponseStatus.UNKNOWN if status == "unknown" else ResponseStatus.FAILED,
            )

        # Firmware accepted the command. Physical completion is signalled
        # separately by the `resource.motion.completed` event, so the terminal
        # state here is `accepted` with state_confirmed left as reported.
        return ResponseEnvelope.create_accepted(
            req.request_id,
            {
                "status": "accepted",
                "accepted": accepted,
                "state_confirmed": state_confirmed,
                "hardware_link": "connected",
                **context,
            },
        )

    def _handle_motion_stop(self, req: RequestEnvelope) -> ResponseEnvelope:
        emergency = bool(req.arguments.get("emergency", False))

        if not self.has_robot_hardware_connected():
            return ResponseEnvelope.create_error(
                req.request_id,
                code="UPSTREAM_UNAVAILABLE",
                message="No physical robot hardware link is connected; stop was not dispatched",
                retryable=True,
                details={
                    "emergency": emergency,
                    "hardware_link": "disconnected",
                    "reason": "NO_PHYSICAL_ROBOT_CONNECTED",
                },
            )

        try:
            ack = self._robot_client.send_motion_stop(emergency)
        except Exception as exc:
            return ResponseEnvelope.create_error(
                req.request_id,
                code="UPSTREAM_UNAVAILABLE",
                message=f"Robot hardware link failed during motion stop: {exc}",
                retryable=True,
            )

        status, accepted, state_confirmed, error_code, retryable = self._motion_outcome(ack)

        if status in ("failed", "unknown"):
            code = error_code or "HARDWARE_FAULT"
            return ResponseEnvelope.create_error(
                req.request_id,
                code=code,
                message=self._MOTION_ERROR_MESSAGES.get(code, "Robot motion stop was not accepted"),
                retryable=retryable,
                details={"emergency": emergency, "hardware_link": "connected"},
                status=ResponseStatus.UNKNOWN if status == "unknown" else ResponseStatus.FAILED,
            )

        return ResponseEnvelope.create_accepted(
            req.request_id,
            {
                "status": "accepted",
                "accepted": accepted,
                "state_confirmed": state_confirmed,
                "hardware_link": "connected",
                "emergency": emergency,
            },
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
                if auth_hdr[7:].strip() == auth_token:
                    return True
            if tok_hdr and tok_hdr.strip() == auth_token:
                return True
            if "?" in self.path:
                query = self.path.split("?", 1)[1]
                for param in query.split("&"):
                    if param.startswith("token="):
                        if param.split("=", 1)[1] == auth_token:
                            return True
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
                resp = json.dumps({
                    "status": "healthy",
                    "uptime_s": uptime,
                    "robot_connected": app.has_robot_hardware_connected(),
                    "device_id": app.config.get("device_id", "desk-agent-r1-host"),
                }).encode()
                self.wfile.write(resp)
            elif (
                self.path == "/snapshot"
                or self.path.startswith("/snapshot?")
                or self.path == "/snapshot.jpg"
                or self.path.startswith("/snapshot.jpg?")
                or self.path.startswith("/api/camera/snapshot")
                or self.path.startswith("/api/camera/frame")
            ):
                if not self._check_auth():
                    self.send_response(401)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error": "Unauthorized"}')
                    return

                # Extract client_id from query params if present
                client_id = None
                if "?" in self.path:
                    query = self.path.split("?", 1)[1]
                    for param in query.split("&"):
                        if param.startswith("client_id="):
                            client_id = param.split("=", 1)[1]

                is_snapshot_path = (
                    self.path == "/snapshot"
                    or self.path.startswith("/snapshot?")
                    or self.path == "/snapshot.jpg"
                    or self.path.startswith("/snapshot.jpg?")
                    or self.path.startswith("/api/camera/snapshot")
                )

                if is_snapshot_path:
                    if app.has_robot_hardware_connected() and hasattr(app._robot_client, "send_command"):
                        try:
                            app._robot_client.send_command({"type": "ctrl.camera.capture"}, timeout=1.0)
                        except Exception as e:
                            logger.debug("Snapshot trigger to robot error: %s", e)
                    frame_info = app.camera_service.get_snapshot(client_id=client_id)
                else:
                    # /api/camera/frame strictly enforces existing lease ownership
                    effective_id = client_id or "web-client"
                    frame_info = app.camera_service.get_latest_frame(effective_id)

                if not frame_info:
                    is_busy = (
                        app.camera_service.is_lease_valid()
                        and client_id is not None
                        and app.camera_service._active_lease_owner != client_id
                    )
                    err_code = "RESOURCE_BUSY" if is_busy else "NO_ACTIVE_LEASE"
                    self.send_response(403)
                    self.send_header("Content-Type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"error": err_code}).encode())
                    return

                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.send_header("X-Frame-Seq", str(frame_info["frame_seq"]))
                self.send_header("X-Frame-Age-Ms", str(frame_info["frame_age_ms"]))
                self.send_header("X-Frame-Source", str(frame_info.get("source", "unknown")))
                self.send_header("X-Frame-Hardware", "1" if frame_info.get("is_hardware") else "0")
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
    parser.add_argument("--host", "-H", type=str, help="Override HTTP server host")
    parser.add_argument("--check-config", action="store_true", help="Perform pre-flight configuration check and exit")
    args = parser.parse_args()

    if args.check_config:
        try:
            test_app = DeskAgentHostApp(config_path=args.config)
            test_app.close()
            print(json.dumps({"status": "valid", "config": args.config or "default"}))
            sys.exit(0)
        except Exception as exc:
            print(json.dumps({"status": "invalid", "error": str(exc)}), file=sys.stderr)
            sys.exit(1)

    app = DeskAgentHostApp(config_path=args.config)
    port = args.port or app.config.get("server", {}).get("port", 8088)
    host = args.host or app.config.get("server", {}).get("host", "127.0.0.1")

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
