"""
Desk Agent R1 Host Daemon Entrypoint.
Runs on TaishanPi RK3566 (Linux), hosting Life Services, HA Connector,
Codex Companion Router, Camera Leased Viewer, and Envelope RPC Dispatcher.
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import ipaddress
import json
import logging
import os
import signal
import socket
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
from urllib.parse import parse_qs, urlsplit

from host.desk_agent.core.models import RequestEnvelope, ResponseEnvelope, ResponseStatus
from host.desk_agent.core.router import Router
from host.desk_agent.core.managed_sdk_listener import ManagedSdkMessageListener
from host.desk_agent.core.managed_sdk_lifecycle import ManagedSdkLifecycleObservers
from host.desk_agent.core.managed_notification import (
    ManagedNotificationAdapter,
    ManagedNotificationNackError,
)
from host.desk_agent.services.life_service import LifeService
from host.desk_agent.services.network_log_consumer import NetworkLogConsumer
from host.desk_agent.connectors.ha.ha_connector import HAConnector
from host.desk_agent.camera.camera_service import (
    CameraStreamCapacityError,
    CameraStreamFrameUnavailable,
    CameraStreamLeaseError,
    CameraViewerService,
)
from host.desk_agent.connectors.robot.robot_adapter import RobotAdapter
from host.desk_agent.connectors.robot.official_camera_adapter import (
    MAX_WSPK_FRAME_BYTES,
    OfficialRobotCameraAdapter,
)
from host.desk_agent.connectors.robot.managed_codex_feedback_adapter import ManagedCodexFeedbackAdapter
from companion.src.codex_companion import CodexCompanion

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("DeskAgentHost")


from companion.src.codex_companion import RemoteCodexCompanionClient


class ThreadedHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


def _address_is_loopback(address: Any) -> bool:
    """Return whether a bound socket address is loopback-only."""
    host = address[0] if isinstance(address, tuple) else address
    try:
        parsed = ipaddress.ip_address(str(host).split("%", 1)[0])
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            return parsed.ipv4_mapped.is_loopback
        return parsed.is_loopback
    except ValueError:
        return False


def _host_is_loopback(host: str) -> bool:
    """Resolve a configured bind host and require every result to be loopback."""
    if not host:
        return False  # HTTPServer treats an empty bind host as a wildcard.
    try:
        parsed = ipaddress.ip_address(host.split("%", 1)[0])
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped:
            return parsed.ipv4_mapped.is_loopback
        return parsed.is_loopback
    except ValueError:
        pass
    try:
        results = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
    except OSError:
        return False
    return bool(results) and all(_address_is_loopback(result[4]) for result in results)


def _validate_listener_auth(host: str, auth_token: Any) -> None:
    if not _host_is_loopback(host) and (not isinstance(auth_token, str) or not auth_token.strip()):
        raise ValueError("Non-loopback Host listener requires a non-empty auth_token")


def _load_host_config(config_path: Optional[str]) -> dict[str, Any]:
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
            with open(config_path, "r", encoding="utf-8") as config_file:
                user_cfg = json.load(config_file)
                default_cfg.update(user_cfg)
                logger.info("Loaded configuration from %s", config_path)
        except Exception as exc:
            logger.error("Failed to parse config file %s: %s; using defaults", config_path, exc)
    return default_cfg


class DeskAgentHostApp:
    def __init__(self, config_path: Optional[str] = None, *, managed_sdk_session: bool = False) -> None:
        self.managed_sdk_session = bool(managed_sdk_session)
        self.managed_codex_feedback_adapter: Optional[ManagedCodexFeedbackAdapter] = None
        self.managed_notification_adapter: Optional[ManagedNotificationAdapter] = None
        self.config = self._load_config(config_path)
        self.start_time = time.time()

        # Database and Services
        db_path = Path(self.config.get("storage", {}).get("db_path", "/var/lib/desk-agent/life_service.db"))
        self.life_service = LifeService(db_path)
        try:
            # One recovery scan per Host process start. Periodic reminder polling and
            # SDK reconnects reuse the resulting immutable outbox batch.
            self._startup_recovery_summary = self.life_service.create_recovery_summary_batch()
        except Exception as exc:
            self._startup_recovery_summary = None
            logger.warning("Startup missed-reminder recovery scan failed (%s)", type(exc).__name__)
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
            auto_start_pipeline=cam_cfg.get("auto_start_pipeline", False),
            allow_synthetic=cam_cfg.get("allow_synthetic", False),
        )
        network_log_cfg = self.config.get("network_logs", {})
        self.network_log_consumer = NetworkLogConsumer(
            enabled=bool(network_log_cfg.get("enabled", False))
        )
        self._managed_network_log_listener: Optional[ManagedSdkMessageListener] = None
        # CR-009 authorization/subscription is not implemented by this lifecycle hook.
        self._network_log_authorized = False
        self.official_camera_adapter: Optional[OfficialRobotCameraAdapter] = None

        codex_cfg = self.config.get("codex", {})
        companion_url = codex_cfg.get("companion_url") or self.config.get("codex_companion", {}).get("endpoint")
        if companion_url:
            logger.info("Configured remote Windows Codex Companion at %s", companion_url)
            state_file = str(db_path.parent / "remote_codex_tasks.json")
            self.companion = RemoteCodexCompanionClient(
                endpoint_url=companion_url,
                auth_token=codex_cfg.get("auth_token") or self.config.get("auth_token"),
                state_file_path=state_file,
            )
        else:
            self.companion = CodexCompanion(
                allowed_workspaces=codex_cfg.get("workspaces"),
                codex_bin=codex_cfg.get("codex_bin", "codex"),
            )

        if hasattr(self.companion, "add_event_listener"):
            self.companion.add_event_listener(self._on_codex_event)
            if hasattr(self.companion, "start"):
                self.companion.start()

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
        if robot_cfg.get("enabled", True) and not managed_sdk_session:
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
            self.robot_adapter.start()

        # HA State Subscription
        self._ha_state_changes: list[dict[str, Any]] = []
        ha_cfg = self.config.get("home_assistant", {})
        if ha_cfg.get("subscribe_events", True):
            self._ha_sub_stop: Optional[Callable[[], None]] = self.ha_connector.subscribe_state_changes(
                self._on_ha_state_change,
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

    def _on_codex_event(self, event_type: str, data: dict[str, Any]) -> bool:
        state_by_event = {
            "codex.completed": "completed",
            "codex.failed": "failed",
            "codex.error": "failed",
            "codex.interrupted": "interrupted",
            "codex.cancelled": "interrupted",
        }
        if event_type == "codex.turn_completed":
            resolution = data.get("resolution") or data.get("status") or "completed"
        else:
            resolution = state_by_event.get(event_type)
        if resolution not in ("completed", "failed", "interrupted"):
            return True

        task_id = data.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            logger.warning("Ignoring Codex terminal event without task_id")
            return False
        turn_id = data.get("turn_id")
        if not isinstance(turn_id, str):
            turn_id = ""
        topic = f"codex.{resolution}"
        summary = data.get("summary") if isinstance(data.get("summary"), str) else ""
        payload = {"task_id": task_id, "resolution": resolution, "summary": summary}
        if turn_id:
            payload["turn_id"] = turn_id

        # Stable outbox identity prevents repeated Companion notifications from
        # creating duplicate device prompts while preserving any pending retry.
        identity = f"{task_id}\0{turn_id or resolution}"
        outbox_id = self.life_service.enqueue_outbox_once(
            topic, payload, idempotency_key=identity
        )

        if self.managed_sdk_session:
            logger.info("Codex terminal event queued for managed SDK delivery (outbox=%s)", outbox_id)
            return False

        # Try immediate delivery if robot is currently online
        delivered = False
        if self.robot_adapter and getattr(self.robot_adapter, "is_connected", False):
            try:
                legacy_announcement = self._legacy_codex_announcement(topic, payload)
                delivered = bool(self.robot_adapter.send_announcement(topic, legacy_announcement))
                if delivered:
                    if hasattr(self.companion, "acknowledge_task"):
                        self.companion.acknowledge_task(task_id)
                    self.life_service.mark_outbox_dispatched(outbox_id)
                    logger.info("Codex announcement for task %s delivered immediately to robot", task_id)
            except Exception as e:
                logger.warning("Failed immediate dispatch of Codex announcement: %s", e)

        if delivered:
            return True
        else:
            logger.info("Robot not connected or announcement not confirmed for task %s; outbox %s queued for delivery retry", task_id, outbox_id)
            return False

    @staticmethod
    def _legacy_codex_announcement(topic: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        resolution = payload.get("resolution")
        title, prefix, sound = {
            "completed": ("Codex 任务完成", "Codex任务已完成：", "notify"),
            "failed": ("Codex 任务失败", "Codex任务执行失败：", "alert"),
            "interrupted": ("Codex 任务中断", "Codex任务已中断：", "alert"),
        }.get(str(resolution), ("Codex 任务状态", "Codex任务状态：", "notify"))
        return {
            "title": title,
            "text": f"{prefix}{payload.get('summary', '')}",
            "sound": sound,
            "task_id": payload.get("task_id", ""),
        }

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
                        if not self.life_service.claim_reminder_alert(outbox_id):
                            continue
                        if self.managed_sdk_session:
                            try:
                                if self.managed_notification_adapter is None:
                                    raise RuntimeError("managed notification adapter is not registered")
                                notification = self.life_service.build_reminder_notification(item)
                                receipt = self.life_service.ensure_notification_receipt(notification)
                                if receipt not in {"ready", "exists"}:
                                    raise RuntimeError("notification receipt context is unavailable")
                                persisted_receipt = self.life_service.get_notification_receipt(
                                    notification["notification_id"]
                                )
                                delivery_status = (
                                    persisted_receipt.get("delivery_status", "unknown")
                                    if persisted_receipt else "unknown"
                                )
                                if delivery_status != "unknown":
                                    backoff = min(300, 2 ** (item.get("retry_count", 0) + 1))
                                    self.life_service.record_outbox_failure(
                                        outbox_id,
                                        "Managed notification has a persisted device stage; awaiting expiry",
                                        backoff_seconds=backoff,
                                        keep_pending=True,
                                    )
                                    logger.info(
                                        "Managed reminder %s already has device stage %s; no replay sent",
                                        outbox_id,
                                        delivery_status,
                                    )
                                    continue
                                result = self.managed_notification_adapter.deliver(notification)
                                backoff = min(300, 2 ** (item.get("retry_count", 0) + 1))
                                # CR-018 is blocked: transport ACK and application accepted do not
                                # prove durable device acceptance. Keep this same occurrence pending.
                                self.life_service.record_outbox_failure(
                                    outbox_id,
                                    "Managed notification delivery remains unknown pending approved device persistence",
                                    backoff_seconds=backoff,
                                    keep_pending=True,
                                )
                                logger.info(
                                    "Managed reminder %s remains pending/unknown after transport attempt (state=%s)",
                                    outbox_id,
                                    result.get("delivery_state", "unknown"),
                                )
                            except ManagedNotificationNackError as exc:
                                if exc.retryable:
                                    backoff = min(300, 2 ** (item.get("retry_count", 0) + 1))
                                    self.life_service.record_outbox_failure(
                                        outbox_id,
                                        f"Managed notification NACK: {exc.error}",
                                        backoff_seconds=backoff,
                                        keep_pending=True,
                                    )
                                    logger.warning(
                                        "Managed reminder %s received retryable NACK %s; kept pending",
                                        outbox_id,
                                        exc.error,
                                    )
                                else:
                                    terminal = self.life_service.record_terminal_reminder_alert_failure(outbox_id)
                                    logger.warning(
                                        "Managed reminder %s received terminal NACK %s (outbox_failed=%s)",
                                        outbox_id,
                                        exc.error,
                                        terminal,
                                    )
                            except Exception as exc:
                                backoff = min(300, 2 ** (item.get("retry_count", 0) + 1))
                                self.life_service.record_outbox_failure(
                                    outbox_id,
                                    f"Managed notification delivery unavailable: {type(exc).__name__}",
                                    backoff_seconds=backoff,
                                    keep_pending=True,
                                )
                        else:
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
                    elif topic == "notification.summary.deliver":
                        if not self.managed_sdk_session or self.managed_notification_adapter is None:
                            backoff = min(300, 2 ** (item.get("retry_count", 0) + 1))
                            self.life_service.record_outbox_failure(
                                outbox_id,
                                "Managed SDK session is not ready for recovery summary",
                                backoff_seconds=backoff,
                                keep_pending=True,
                            )
                            continue
                        try:
                            result = self.managed_notification_adapter.deliver_summary(payload)
                            backoff = min(300, 2 ** (item.get("retry_count", 0) + 1))
                            # CR-018 is blocked: sys.ack/accepted is transport evidence only.
                            # Preserve the same summary outbox item as pending/unknown to TTL.
                            self.life_service.record_outbox_failure(
                                outbox_id,
                                "Recovery summary remains pending/unknown pending approved device persistence",
                                backoff_seconds=backoff,
                                keep_pending=True,
                            )
                            logger.info(
                                "Recovery summary %s remains pending/unknown after transport attempt (state=%s)",
                                payload.get("summary_id"), result.get("delivery_state", "unknown"),
                            )
                        except Exception as exc:
                            backoff = min(300, 2 ** (item.get("retry_count", 0) + 1))
                            self.life_service.record_outbox_failure(
                                outbox_id,
                                f"Managed recovery summary delivery unavailable: {type(exc).__name__}",
                                backoff_seconds=backoff,
                                keep_pending=True,
                            )
                    elif topic in ("codex.completed", "codex.failed", "codex.interrupted"):
                        if self.managed_codex_feedback_adapter is not None:
                            try:
                                result = self.managed_codex_feedback_adapter.send_terminal(topic, payload)
                                if result.get("accepted") is True:
                                    task_id = payload.get("task_id")
                                    if task_id and hasattr(self.companion, "acknowledge_task"):
                                        self.companion.acknowledge_task(task_id)
                                    self.life_service.mark_outbox_dispatched(outbox_id)
                                    logger.info(
                                        "Codex terminal command accepted by device (outbox=%s); audio completion is unconfirmed",
                                        outbox_id,
                                    )
                                else:
                                    raise RuntimeError("managed SDK feedback did not return accepted status")
                            except Exception as exc:
                                backoff = min(60, 2 ** (item.get("retry_count", 0) + 1))
                                self.life_service.record_outbox_failure(
                                    outbox_id,
                                    f"Managed SDK terminal delivery failed: {exc}",
                                    backoff_seconds=backoff,
                                    keep_pending=True,
                                )
                        elif self.robot_adapter and getattr(self.robot_adapter, "is_connected", False):
                            try:
                                delivered = bool(
                                    self.robot_adapter.send_announcement(
                                        topic, self._legacy_codex_announcement(topic, payload)
                                    )
                                )
                                if delivered:
                                    task_id = payload.get("task_id")
                                    if task_id and hasattr(self.companion, "acknowledge_task"):
                                        self.companion.acknowledge_task(task_id)
                                    self.life_service.mark_outbox_dispatched(outbox_id)
                                    logger.info("Outbox Codex announcement %s (%s) delivered to robot", outbox_id, topic)
                                else:
                                    backoff = min(60, 2 ** (item.get("retry_count", 0) + 1))
                                    self.life_service.record_outbox_failure(
                                        outbox_id, "Robot announcement channel rejected or busy", backoff_seconds=backoff
                                    )
                            except Exception as exc:
                                backoff = min(60, 2 ** (item.get("retry_count", 0) + 1))
                                self.life_service.record_outbox_failure(
                                    outbox_id, f"Robot delivery exception: {exc}", backoff_seconds=backoff
                                )
                        else:
                            backoff = min(30, 2 ** (item.get("retry_count", 0) + 1))
                            self.life_service.record_outbox_failure(
                                outbox_id,
                                "Managed SDK session is not ready" if self.managed_sdk_session else "Robot is disconnected",
                                backoff_seconds=backoff,
                                keep_pending=self.managed_sdk_session,
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

    def _record_managed_notification_event(self, event: Mapping[str, Any]) -> str:
        """Route CR-028 receipts by summary_id while preserving CR-017 behavior."""
        if isinstance(event.get("summary_id"), str):
            return self.life_service.record_recovery_summary_ack(event)
        return self.life_service.record_managed_notification_event(event)

    def close(self) -> None:
        self._scheduler_stop.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=2.0)
        if hasattr(self, "_delivery_thread") and self._delivery_thread.is_alive():
            # Managed send_command is bounded by the pinned SDK adapter timeout;
            # wait for its callback path before closing listeners or SQLite.
            self._delivery_thread.join()
        if self.official_camera_adapter:
            self.official_camera_adapter.close()
        if self._managed_network_log_listener is not None:
            try:
                self._managed_network_log_listener.remove(self._on_managed_sdk_message)
            except Exception as exc:
                logger.warning("Managed SDK message listener cleanup failed (%s)", type(exc).__name__)
            self._managed_network_log_listener = None
        if self.managed_notification_adapter is not None:
            try:
                self.managed_notification_adapter.close()
            except Exception as exc:
                logger.warning("Managed notification adapter cleanup failed (%s)", type(exc).__name__)
            self.managed_notification_adapter = None
        if getattr(self, "network_log_consumer", None) is not None:
            self.network_log_consumer.close()
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
        return _load_host_config(config_path)

    def _on_ha_state_change(
        self,
        entity_id: str,
        old_state: Optional[str],
        new_state: Optional[str],
        attributes: Optional[Mapping[str, Any]],
    ) -> None:
        logger.info("HA entity %s changed: %s -> %s", entity_id, old_state, new_state)
        change: dict[str, Any] = {
            "entity_id": entity_id,
            "old_state": old_state,
            "new_state": new_state,
            "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        }
        if new_state is not None:
            snapshot = dict(attributes) if isinstance(attributes, Mapping) else {}
            change["attributes"] = copy.deepcopy(snapshot)
        self._ha_state_changes.append(change)
        if len(self._ha_state_changes) > 100:
            self._ha_state_changes.pop(0)

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
            repeat_rule=args.get("repeat_rule"),
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
        item = self.life_service.snooze_reminder(
            req.arguments["reminder_id"], snooze_min, req.arguments.get("occurrence_id")
        )
        return ResponseEnvelope.create_success(req.request_id, item)

    def _handle_reminder_stop_ringing(self, req: RequestEnvelope) -> ResponseEnvelope:
        try:
            item = self.life_service.stop_ringing(req.arguments["occurrence_id"])
        except KeyError as exc:
            return ResponseEnvelope.create_error(
                req.request_id,
                "RESOURCE_NOT_FOUND",
                str(exc),
                retryable=False,
            )
        except ValueError as exc:
            return ResponseEnvelope.create_error(
                req.request_id,
                "STATE_CONFLICT",
                str(exc),
                retryable=False,
            )
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
        if self.official_camera_adapter is not None:
            device_result = self.official_camera_adapter.acquire(
                client_id, res.get("lease_duration_ms", 10000), target_fps=float(target_fps or res.get("target_fps", 2.0))
            )
            if not device_result.get("granted"):
                self.camera_service.release_lease(client_id)
                return ResponseEnvelope.create_error(
                    req.request_id,
                    code=device_result.get("reason", "DEVICE_ERROR"),
                    message="Official SDK camera lease was not granted",
                    details=device_result,
                )
        elif self.has_robot_hardware_connected() and hasattr(self._robot_client, "send_command"):
            try:
                ack = self._robot_client.send_command({
                    "type": "ctrl.camera.acquire",
                    "client_id": client_id,
                    "lease_duration_ms": res.get("lease_duration_ms", 10000),
                }, timeout=1.0)
                if not isinstance(ack, dict) or ack.get("type") != "sys.ack" or ack.get("code") != 0:
                    logger.warning("Robot rejected camera acquire: %s. Rolling back host lease.", ack)
                    self.camera_service.release_lease(client_id)
                    err_info = ack.get("error", {}) if isinstance(ack, dict) else {}
                    return ResponseEnvelope.create_error(
                        req.request_id,
                        code=err_info.get("code", "RESOURCE_BUSY"),
                        message=err_info.get("message", "Robot rejected camera acquire"),
                        details=ack if isinstance(ack, dict) else {},
                    )
                self._robot_client.send_command({
                    "type": "ctrl.camera.capture",
                }, timeout=1.0)
            except Exception as e:
                logger.warning("Failed to dispatch camera acquire/capture to robot: %s. Rolling back lease.", e)
                self.camera_service.release_lease(client_id)
                return ResponseEnvelope.create_error(
                    req.request_id,
                    code="DEVICE_ERROR",
                    message=f"Failed to communicate with robot camera: {e}",
                )
        return ResponseEnvelope.create_success(req.request_id, res)

    def _handle_camera_release(self, req: RequestEnvelope) -> ResponseEnvelope:
        client_id = req.client_id or req.arguments.get("client_id", "anonymous")
        released = self.camera_service.release_lease(client_id)
        if released and self.official_camera_adapter is None and self.has_robot_hardware_connected() and hasattr(self._robot_client, "send_command"):
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

    def register_official_sdk_camera_session(
        self,
        robot: Any,
        *,
        command_sender: Callable[[str, dict[str, Any], Optional[float]], dict[str, Any]],
        on_disconnect: Optional[Callable[[Callable[[], None]], None]] = None,
    ) -> OfficialRobotCameraAdapter:
        """Attach an existing managed-SDK session; this method never starts a Runtime or connection."""
        previous_teardown = self.camera_service.on_lease_released
        adapter = OfficialRobotCameraAdapter(
            robot,
            send_command=command_sender,
            ingest_frame=self.camera_service.ingest_robot_frame,
            clear_frame=self.camera_service.invalidate_owner_on_upstream_disconnect,
        )
        self.official_camera_adapter = adapter

        def release_device_lease(owner: str, reason: str) -> None:
            if previous_teardown:
                previous_teardown(owner, reason)
            adapter.release(owner)

        self.camera_service.on_lease_released = release_device_lease
        if on_disconnect is not None:
            on_disconnect(adapter.on_sdk_disconnect)
        return adapter

    def register_managed_sdk_codex_feedback_session(
        self,
        *,
        command_sender: Callable[[str, dict[str, Any], Optional[float]], dict[str, Any]],
    ) -> ManagedCodexFeedbackAdapter:
        """Attach Codex feedback to the existing managed SDK transport without opening a connection."""
        adapter = ManagedCodexFeedbackAdapter(command_sender)
        self.managed_codex_feedback_adapter = adapter
        return adapter

    def register_managed_sdk_network_log_session(self, context: object) -> None:
        """Route parsed Device JSON to the bounded consumer, fail closed absent CR-009 auth."""
        listener = ManagedSdkMessageListener(context)
        listener.add(self._on_managed_sdk_message)
        self._managed_network_log_listener = listener

    def _on_managed_sdk_message(self, message: dict[str, Any]) -> None:
        self.network_log_consumer.ingest(message, authorized=self._network_log_authorized)

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
            if not isinstance(auth_token, str) or not auth_token.strip():
                # Empty-token local-only mode remains available on loopback;
                # wildcard or LAN listeners fail closed even if startup checks
                # were bypassed by an embedding caller.
                return _address_is_loopback(self.server.server_address)
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

        def _check_stream_auth(self) -> bool:
            """Camera streams accept configured credentials only as a Bearer header."""
            auth_token = app.config.get("auth_token")
            if not isinstance(auth_token, str) or not auth_token:
                return False
            auth_hdr = self.headers.get("Authorization", "")
            return auth_hdr.startswith("Bearer ") and auth_hdr[7:].strip() == auth_token

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
                or self.path == "/api/camera/stream"
                or self.path.startswith("/api/camera/stream?")
            ):
                is_stream_request = (
                    self.path == "/api/camera/stream"
                    or self.path.startswith("/api/camera/stream?")
                )
                if is_stream_request and "token" in parse_qs(
                    urlsplit(self.path).query, keep_blank_values=True
                ):
                    request_parts = self.requestline.split(" ", 2)
                    if len(request_parts) == 3:
                        self.requestline = (
                            f"{request_parts[0]} {urlsplit(request_parts[1]).path}"
                            f"?token=[REDACTED] {request_parts[2]}"
                        )
                is_authorized = self._check_stream_auth() if is_stream_request else self._check_auth()
                if not is_authorized:
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

                is_stream_path = (
                    self.path == "/api/camera/stream"
                    or self.path.startswith("/api/camera/stream?")
                )

                if is_stream_path:
                    query_values = parse_qs(
                        urlsplit(self.path).query, keep_blank_values=True
                    ).get("client_id", [])
                    if len(query_values) != 1 or not query_values[0].strip():
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":"INVALID_CLIENT_ID"}')
                        return
                    client_id = query_values[0].strip()

                    stream = None
                    stream_admitted = False
                    stream_ready = False
                    try:
                        stream = iter(app.camera_service.iter_mjpeg(client_id))
                        stream_admitted = True
                        first_part = next(stream)
                        stream_ready = True
                    except CameraStreamCapacityError as exc:
                        self.send_response(429)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": exc.code}).encode())
                        return
                    except CameraStreamLeaseError as exc:
                        self.send_response(403)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": exc.code}).encode())
                        return
                    except CameraStreamFrameUnavailable as exc:
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({
                            "error": "CAMERA_FRAME_UNAVAILABLE",
                            "reason": exc.code,
                        }).encode())
                        return
                    except StopIteration:
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":"CAMERA_FRAME_UNAVAILABLE"}')
                        return
                    except Exception:
                        logger.exception("Camera stream could not start")
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(b'{"error":"CAMERA_STREAM_UNAVAILABLE"}')
                        return
                    finally:
                        # Failed first-frame attempts free only the subscriber slot;
                        # lease ownership is managed by the explicit lease API.
                        if not stream_ready and stream_admitted:
                            if stream is not None:
                                stream.close()

                    try:
                        self.send_response(200)
                        self.send_header(
                            "Content-Type",
                            "multipart/x-mixed-replace; boundary=deskagent-frame",
                        )
                        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                        self.send_header("Pragma", "no-cache")
                        self.send_header("Connection", "close")
                        self.end_headers()
                        self.wfile.write(first_part)
                        self.wfile.flush()
                        while True:
                            try:
                                part = next(stream)
                            except StopIteration:
                                break
                            self.wfile.write(part)
                            self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                        logger.debug("Camera stream client disconnected: %s", client_id)
                    finally:
                        if stream is not None:
                            stream.close()
                    return

                if is_snapshot_path:
                    ephemeral_lease_acquired = False
                    if app.camera_service.is_lease_valid():
                        if not client_id or app.camera_service._active_lease_owner != client_id:
                            self.send_response(403)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                            self.wfile.write(json.dumps({"error": "RESOURCE_BUSY"}).encode())
                            return
                        effective_client = client_id
                    else:
                        effective_client = client_id or "snapshot-viewer"
                        acq = app.camera_service.acquire_lease(effective_client, lease_ms=5000)
                        if not acq.get("granted"):
                            self.send_response(403)
                            self.send_header("Content-Type", "application/json")
                            self.end_headers()
                            self.wfile.write(json.dumps({"error": "RESOURCE_BUSY"}).encode())
                            return
                        ephemeral_lease_acquired = True
                        if app.official_camera_adapter is not None:
                            device_acq = app.official_camera_adapter.acquire(effective_client, 5000, target_fps=10.0)
                            if not device_acq.get("granted"):
                                app.camera_service.release_lease(effective_client)
                                self.send_response(503)
                                self.send_header("Content-Type", "application/json")
                                self.end_headers()
                                self.wfile.write(json.dumps({"error": device_acq.get("reason", "DEVICE_UNAVAILABLE")}).encode())
                                return
                        elif app.has_robot_hardware_connected() and hasattr(app._robot_client, "send_command"):
                            try:
                                ack = app._robot_client.send_command({
                                    "type": "ctrl.camera.acquire",
                                    "client_id": effective_client,
                                    "lease_duration_ms": 5000,
                                }, timeout=1.0)
                                if not isinstance(ack, dict) or ack.get("type") != "sys.ack" or ack.get("code") != 0:
                                    logger.warning("Robot rejected snapshot camera acquire: %s", ack)
                                    app.camera_service.release_lease(effective_client)
                                    self.send_response(403)
                                    self.send_header("Content-Type", "application/json")
                                    self.end_headers()
                                    self.wfile.write(json.dumps({"error": "RESOURCE_BUSY"}).encode())
                                    return
                            except Exception as exc:
                                logger.warning("Snapshot acquire dispatch error: %s", exc)
                                app.camera_service.release_lease(effective_client)
                                self.send_response(503)
                                self.send_header("Content-Type", "application/json")
                                self.end_headers()
                                self.wfile.write(json.dumps({"error": "DEVICE_UNAVAILABLE"}).encode())
                                return

                    try:
                        initial_seq = getattr(app.camera_service, "_frame_seq", 0)
                        if app.official_camera_adapter is not None:
                            frame_info = None
                            deadline = time.time() + 1.2
                            while time.time() < deadline:
                                candidate = app.camera_service.get_latest_frame(effective_client)
                                if candidate is not None and candidate.get("frame_seq", 0) > initial_seq:
                                    frame_info = candidate
                                    break
                                time.sleep(0.02)
                            if frame_info is None:
                                self.send_response(504)
                                self.send_header("Content-Type", "application/json")
                                self.end_headers()
                                self.wfile.write(json.dumps({"error": "CAPTURE_TIMEOUT"}).encode())
                                return
                        elif app.has_robot_hardware_connected() and hasattr(app._robot_client, "send_command"):
                            try:
                                cap_ack = app._robot_client.send_command({"type": "ctrl.camera.capture"}, timeout=1.0)
                                if not isinstance(cap_ack, dict) or cap_ack.get("type") != "sys.ack" or cap_ack.get("code") != 0:
                                    logger.warning("Robot rejected snapshot camera capture: %s", cap_ack)
                                    self.send_response(503)
                                    self.send_header("Content-Type", "application/json")
                                    self.end_headers()
                                    self.wfile.write(json.dumps({"error": "CAPTURE_FAILED"}).encode())
                                    return
                            except Exception as e:
                                logger.warning("Snapshot capture dispatch error: %s", e)
                                self.send_response(503)
                                self.send_header("Content-Type", "application/json")
                                self.end_headers()
                                self.wfile.write(json.dumps({"error": "CAPTURE_FAILED"}).encode())
                                return

                            frame_info = None
                            deadline = time.time() + 1.2
                            while time.time() < deadline:
                                candidate = app.camera_service.get_latest_frame(effective_client)
                                if candidate is not None and candidate.get("frame_seq", 0) > initial_seq:
                                    frame_info = candidate
                                    break
                                time.sleep(0.02)

                            if frame_info is None:
                                logger.warning("Timed out waiting for fresh robot frame (initial_seq=%d)", initial_seq)
                                self.send_response(504)
                                self.send_header("Content-Type", "application/json")
                                self.end_headers()
                                self.wfile.write(json.dumps({"error": "CAPTURE_TIMEOUT"}).encode())
                                return
                        else:
                            frame_info = None
                            deadline = time.time() + 1.2
                            while time.time() < deadline:
                                candidate = app.camera_service.get_latest_frame(effective_client)
                                if candidate is not None:
                                    frame_info = candidate
                                    break
                                time.sleep(0.02)

                            if frame_info is None:
                                frame_info = app.camera_service.get_latest_frame(effective_client)

                        if not frame_info:
                            self.send_response(404)
                            self.end_headers()
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
                    finally:
                        if ephemeral_lease_acquired:
                            app.camera_service.release_lease(effective_client)
                            if app.official_camera_adapter is None and app.has_robot_hardware_connected() and hasattr(app._robot_client, "send_command"):
                                try:
                                    app._robot_client.send_command({
                                        "type": "ctrl.camera.release",
                                        "client_id": effective_client,
                                    }, timeout=0.5)
                                except Exception:
                                    pass
                else:
                    # /api/camera/frame strictly enforces existing lease ownership
                    effective_id = client_id or "web-client"
                    lease_valid = app.camera_service.is_lease_valid()
                    if not lease_valid:
                        self.send_response(403)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "NO_ACTIVE_LEASE"}).encode())
                        return

                    if app.camera_service._active_lease_owner != effective_id:
                        self.send_response(403)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "RESOURCE_BUSY"}).encode())
                        return

                    frame_info = app.camera_service.get_latest_frame(effective_id)
                    if not frame_info:
                        self.send_response(503)
                        self.send_header("Content-Type", "application/json")
                        self.end_headers()
                        self.wfile.write(json.dumps({"error": "CAMERA_FRAME_UNAVAILABLE"}).encode())
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


def _serve_host(app: DeskAgentHostApp, host: str, port: int, *, shutdown_check: Optional[Callable[[], bool]] = None) -> None:
    _validate_listener_auth(host, app.config.get("auth_token"))
    handler_cls = make_request_handler(app)
    httpd = ThreadedHTTPServer((host, port), handler_cls)

    def handle_signal(sig, frame):
        logger.info("Received signal %s, shutting down...", sig)
        httpd.shutdown()

    if shutdown_check is None:
        signal.signal(signal.SIGINT, handle_signal)
        signal.signal(signal.SIGTERM, handle_signal)
    logger.info("Desk Agent Host Daemon running on http://%s:%d", host, port)
    try:
        if shutdown_check is None:
            httpd.serve_forever()
        else:
            thread = threading.Thread(target=httpd.serve_forever, name="desk-agent-http", daemon=True)
            thread.start()
            while thread.is_alive() and not shutdown_check():
                thread.join(timeout=0.25)
            if thread.is_alive():
                httpd.shutdown()
            thread.join(timeout=2.0)
    except KeyboardInterrupt:
        pass
    finally:
        app.close()
        httpd.server_close()
        logger.info("Host daemon terminated cleanly.")


async def _run_managed_application(config_path: Optional[str], host: str, port: int) -> None:
    """Run the Host inside the one Runtime-authorized SDK managed Application session."""
    launch_config = _load_host_config(config_path)
    _validate_listener_auth(host, launch_config.get("auth_token"))
    from watcherobot.application import ApplicationContext

    context = ApplicationContext.from_environment()
    async with context:
        app = DeskAgentHostApp(config_path=config_path, managed_sdk_session=True)
        lifecycle: Optional[ManagedSdkLifecycleObservers] = None
        host_loop_started = False
        try:
            # Pinned SDK currently keeps the command transport private on ApplicationContext.
            # This is a version-bound compatibility point: send_command itself is public on
            # DaemonApplicationTransport, but ApplicationContext exposes no transport property.
            transport = context._transport
            sdk_binary = getattr(transport, "_binary_callback", None)
            if not callable(sdk_binary):
                raise RuntimeError("pinned SDK transport has no binary callback")
            lifecycle = ManagedSdkLifecycleObservers(transport)

            def bounded_sdk_binary(frame: Any) -> None:
                payload = getattr(frame, "payload", None)
                if not isinstance(payload, bytes) or len(payload) + 16 > MAX_WSPK_FRAME_BYTES:
                    logger.warning("Dropped malformed or oversized official SDK WSPK frame")
                    return
                sdk_binary(frame)

            # The pinned Runtime uses max_size=None. Bound the parsed WSPK payload
            # before the SDK can queue or assemble it, while retaining its handler.
            transport._binary_callback = bounded_sdk_binary

            app.register_official_sdk_camera_session(
                context.robot,
                command_sender=transport.send_command,
                on_disconnect=lifecycle.add,
            )
            app.register_managed_sdk_network_log_session(context)
            lifecycle.add(app.network_log_consumer.on_disconnect)
            app.register_managed_sdk_codex_feedback_session(command_sender=transport.send_command)
            pending_summaries = app.life_service.get_pending_outbox_messages(limit=1000, include_delayed=True)
            known_summaries = {
                item["payload"]["summary_id"]: item["payload"]["expires_at"]
                for item in pending_summaries
                if item.get("topic") == "notification.summary.deliver"
                and isinstance(item.get("payload"), dict)
                and isinstance(item["payload"].get("summary_id"), str)
                and isinstance(item["payload"].get("expires_at"), str)
            }
            notification_adapter = ManagedNotificationAdapter(
                context,
                app._record_managed_notification_event,
                known_notifications=app.life_service.get_active_notification_contexts(),
                known_summaries=known_summaries,
            )
            app.managed_notification_adapter = notification_adapter
            host_loop_started = True
            await asyncio.to_thread(
                _serve_host,
                app,
                host,
                port,
                shutdown_check=lambda: context.shutdown_requested,
            )
        finally:
            try:
                if lifecycle is not None:
                    lifecycle.close()
            finally:
                if not host_loop_started:
                    app.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Desk Agent R1 Host Daemon")
    parser.add_argument("--config", "-c", type=str, help="Path to config.json")
    parser.add_argument("--port", "-p", type=int, help="Override HTTP server port")
    parser.add_argument("--host", "-H", type=str, help="Override HTTP server host")
    parser.add_argument("--check-config", action="store_true", help="Perform pre-flight configuration check and exit")
    args = parser.parse_args()

    launch_config = _load_host_config(args.config)
    port = args.port or launch_config.get("server", {}).get("port", 8088)
    host = args.host or launch_config.get("server", {}).get("host", "127.0.0.1")

    if args.check_config:
        try:
            _validate_listener_auth(host, launch_config.get("auth_token"))
            test_app = DeskAgentHostApp(config_path=args.config)
            test_app.close()
            print(json.dumps({"status": "valid", "config": args.config or "default"}))
            sys.exit(0)
        except Exception as exc:
            print(json.dumps({"status": "invalid", "error": str(exc)}), file=sys.stderr)
            sys.exit(1)

    _validate_listener_auth(host, launch_config.get("auth_token"))
    managed_env_names = (
        "WATCHER_APP_ID",
        "WATCHER_APP_RUN_CREDENTIAL",
        "WATCHER_APP_DESKTOP_WS_URL",
        "WATCHER_APP_DEVICE_WS_URL",
    )
    managed_environment_present = any(os.environ.get(name, "").strip() for name in managed_env_names)
    if managed_environment_present:
        asyncio.run(_run_managed_application(args.config, host, port))
        return
    app = DeskAgentHostApp(config_path=args.config)
    _serve_host(app, host, port)


if __name__ == "__main__":
    main()
