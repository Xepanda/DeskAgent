"""
Desk Agent R1 Camera Preset Patrol & Multi-Point Inspection Service.

Orchestrates sequential waypoint scanning:
- Commands motion rotation to predefined pan/tilt coordinates.
- Waits for servo settling.
- Captures genuine single-frame camera snapshots.
- Executes multimodal vision analysis at each angle.
- Aggregates multi-point inspection reports and maintains bounded history.
- Supports graceful mid-scan cancellation and emergency stop.
- Strictly adheres to single-frame capture rules (no continuous recording).
"""

from __future__ import annotations

import base64
import logging
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Optional

from host.desk_agent.camera.vision_service import VisionAnalysisResult, VisionService

logger = logging.getLogger(__name__)

DEFAULT_PATROL_WAYPOINTS: list[dict[str, Any]] = [
    {"id": "wp_left", "name": "左侧视角", "pan": 45.0, "tilt": 120.0},
    {"id": "wp_center", "name": "中央视角", "pan": 90.0, "tilt": 120.0},
    {"id": "wp_right", "name": "右侧视角", "pan": 135.0, "tilt": 120.0},
]

MAX_PATROL_HISTORY = 10


class PatrolError(RuntimeError):
    """Base patrol execution error."""
    pass


class PatrolBusyError(PatrolError):
    """Raised when starting a patrol while another scan is already running."""
    pass


@dataclass
class WaypointResult:
    waypoint_id: str
    name: str
    pan: float
    tilt: float
    timestamp: float
    jpeg_base64: Optional[str] = None
    analysis: Optional[dict[str, Any]] = None
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class PatrolReport:
    patrol_id: str
    status: str  # "running", "completed", "cancelled", "failed"
    started_at: float
    completed_at: Optional[float] = None
    waypoints: list[WaypointResult] = field(default_factory=list)
    summary: str = ""
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class PatrolService:
    """Manages multi-point camera patrol inspections."""

    def __init__(
        self,
        vision_service: VisionService,
        move_fn: Optional[Callable[[float, float, int], bool]] = None,
        stop_fn: Optional[Callable[[], bool]] = None,
        snapshot_fn: Optional[Callable[[], Optional[bytes]]] = None,
        settle_delay_s: float = 0.5,
    ) -> None:
        self.vision_service = vision_service
        self.move_fn = move_fn
        self.stop_fn = stop_fn
        self.snapshot_fn = snapshot_fn
        self.settle_delay_s = max(0.1, settle_delay_s)

        self._lock = threading.Lock()
        self._history: list[PatrolReport] = []
        self._active_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._current_report: Optional[PatrolReport] = None
        self._current_waypoint_idx: int = -1
        self._total_waypoints: int = 0

    def is_running(self) -> bool:
        with self._lock:
            return self._active_thread is not None and self._active_thread.is_alive()

    def start_patrol(
        self,
        waypoints: Optional[list[dict[str, Any]]] = None,
        duration_ms: int = 800,
    ) -> dict[str, Any]:
        """Initiates a new patrol scan in the background."""
        with self._lock:
            if self._active_thread is not None and self._active_thread.is_alive():
                raise PatrolBusyError("A patrol scan is already active")

            target_waypoints = waypoints or DEFAULT_PATROL_WAYPOINTS
            if not target_waypoints:
                raise ValueError("Waypoint list cannot be empty")

            patrol_id = f"patrol-{uuid.uuid4().hex[:8]}"
            report = PatrolReport(
                patrol_id=patrol_id,
                status="running",
                started_at=time.time(),
            )
            self._current_report = report
            self._current_waypoint_idx = 0
            self._total_waypoints = len(target_waypoints)
            self._stop_event.clear()

            thread = threading.Thread(
                target=self._run_patrol_worker,
                args=(report, target_waypoints, duration_ms),
                name=f"patrol-worker-{patrol_id}",
                daemon=True,
            )
            self._active_thread = thread
            thread.start()

            return {
                "patrol_id": patrol_id,
                "status": "started",
                "waypoint_count": len(target_waypoints),
            }

    def stop_patrol(self, timeout: float = 2.0) -> dict[str, Any]:
        """Signals active patrol to halt immediately and stops motion."""
        with self._lock:
            thread = self._active_thread
            report = self._current_report

        if not thread or not thread.is_alive():
            return {"status": "idle", "message": "No active patrol to stop"}

        self._stop_event.set()

        # Stop physical motion immediately
        if self.stop_fn:
            try:
                self.stop_fn()
            except Exception as e:
                logger.warning("Error invoking stop_fn during patrol cancellation: %s", e)

        thread.join(timeout=timeout)

        with self._lock:
            if report and report.status == "running":
                report.status = "cancelled"
                report.completed_at = time.time()
                report.summary = "Patrol cancelled by user request."
                self._record_history(report)

        return {"status": "cancelled", "patrol_id": report.patrol_id if report else None}

    def get_status(self) -> dict[str, Any]:
        with self._lock:
            running = self._active_thread is not None and self._active_thread.is_alive()
            curr = self._current_report
            return {
                "is_running": running,
                "current_waypoint_index": self._current_waypoint_idx if running else -1,
                "total_waypoints": self._total_waypoints if running else 0,
                "active_patrol": curr.to_dict() if curr else None,
                "latest_report": self._history[-1].to_dict() if self._history else None,
            }

    def get_latest_report(self) -> Optional[dict[str, Any]]:
        with self._lock:
            if self._history:
                return self._history[-1].to_dict()
            if self._current_report:
                return self._current_report.to_dict()
            return None

    def get_history(self, limit: int = MAX_PATROL_HISTORY) -> list[dict[str, Any]]:
        with self._lock:
            return [r.to_dict() for r in self._history[-limit:]]

    def _record_history(self, report: PatrolReport) -> None:
        if report not in self._history:
            self._history.append(report)
            if len(self._history) > MAX_PATROL_HISTORY:
                self._history.pop(0)

    def _run_patrol_worker(
        self,
        report: PatrolReport,
        waypoints: list[dict[str, Any]],
        duration_ms: int,
    ) -> None:
        try:
            for idx, wp in enumerate(waypoints):
                if self._stop_event.is_set():
                    report.status = "cancelled"
                    report.summary = f"Patrol stopped before waypoint {idx + 1}."
                    break

                with self._lock:
                    self._current_waypoint_idx = idx

                wp_id = wp.get("id", f"wp_{idx}")
                wp_name = wp.get("name", f"Waypoint {idx + 1}")
                pan = float(wp.get("pan", 90.0))
                tilt = float(wp.get("tilt", 120.0))

                logger.info("Patrol %s visiting %s (pan=%.1f, tilt=%.1f)", report.patrol_id, wp_name, pan, tilt)

                # 1. Dispatch motion to coordinate
                if self.move_fn:
                    try:
                        moved = self.move_fn(pan, tilt, duration_ms)
                        if not moved:
                            logger.warning("Move to waypoint %s rejected or failed", wp_name)
                    except Exception as err:
                        logger.warning("Motion move exception for %s: %s", wp_name, err)

                # Wait for move + mechanical settling (interruptible)
                move_delay = max(0.1, duration_ms / 1000.0) + self.settle_delay_s
                if self._stop_event.wait(timeout=move_delay):
                    report.status = "cancelled"
                    report.summary = f"Patrol cancelled during movement to {wp_name}."
                    break

                # 2. Capture single frame
                jpeg_data: Optional[bytes] = None
                capture_err: Optional[str] = None
                if self.snapshot_fn:
                    try:
                        jpeg_data = self.snapshot_fn()
                        if not jpeg_data:
                            capture_err = "NO_FRAME_RETURNED"
                    except Exception as err:
                        capture_err = f"CAPTURE_EXCEPTION_{err}"
                else:
                    capture_err = "NO_SNAPSHOT_FUNCTION"

                # 3. Vision analysis
                analysis_dict: Optional[dict[str, Any]] = None
                jpeg_b64: Optional[str] = None
                if jpeg_data:
                    jpeg_b64 = base64.b64encode(jpeg_data).decode("ascii")
                    vision_res = self.vision_service.analyze_jpeg(
                        jpeg_data,
                        prompt=f"Patrol observation at {wp_name} (Pan {pan:.1f} deg, Tilt {tilt:.1f} deg). Describe scene and objects.",
                    )
                    analysis_dict = vision_res.to_dict()

                wp_res = WaypointResult(
                    waypoint_id=wp_id,
                    name=wp_name,
                    pan=pan,
                    tilt=tilt,
                    timestamp=time.time(),
                    jpeg_base64=jpeg_b64,
                    analysis=analysis_dict,
                    error=capture_err,
                )
                report.waypoints.append(wp_res)

            if not self._stop_event.is_set():
                report.status = "completed"
                success_count = sum(1 for w in report.waypoints if w.analysis and w.analysis.get("success"))
                report.summary = f"Completed patrol across {len(report.waypoints)} waypoints ({success_count} analyzed successfully)."

        except Exception as e:
            logger.error("Patrol %s failed with exception: %s", report.patrol_id, e)
            report.status = "failed"
            report.error = str(e)
            report.summary = f"Patrol encountered an error: {e}"
        finally:
            report.completed_at = time.time()
            with self._lock:
                self._record_history(report)
                self._current_waypoint_idx = -1
                self._active_thread = None
