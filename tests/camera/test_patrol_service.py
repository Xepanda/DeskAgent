"""Unit tests for PatrolService multi-point scanning and state machine."""

import time
import unittest
from typing import Optional

from host.desk_agent.camera.patrol_service import (
    DEFAULT_PATROL_WAYPOINTS,
    PatrolBusyError,
    PatrolReport,
    PatrolService,
    WaypointResult,
)
from host.desk_agent.camera.vision_service import MockVisionProvider, VisionService


class TestPatrolService(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_jpeg = b"\xff\xd8\x00\x01\x02\x03\xff\xd9"
        self.vision = VisionService(provider=MockVisionProvider(default_scene="test_office"))
        self.moves: list[tuple[float, float, int]] = []
        self.stopped = False

    def _mock_move(self, pan: float, tilt: float, dur: int) -> bool:
        self.moves.append((pan, tilt, dur))
        return True

    def _mock_stop(self) -> bool:
        self.stopped = True
        return True

    def _mock_snapshot(self) -> Optional[bytes]:
        return self.valid_jpeg

    def test_patrol_sequential_execution(self) -> None:
        service = PatrolService(
            vision_service=self.vision,
            move_fn=self._mock_move,
            stop_fn=self._mock_stop,
            snapshot_fn=self._mock_snapshot,
            settle_delay_s=0.01,
        )

        res = service.start_patrol(duration_ms=10)
        self.assertEqual(res["status"], "started")
        self.assertEqual(res["waypoint_count"], len(DEFAULT_PATROL_WAYPOINTS))

        # Wait for worker thread to complete
        for _ in range(50):
            if not service.is_running():
                break
            time.sleep(0.05)

        self.assertFalse(service.is_running())
        report = service.get_latest_report()
        self.assertIsNotNone(report)
        self.assertEqual(report["status"], "completed")
        self.assertEqual(len(report["waypoints"]), len(DEFAULT_PATROL_WAYPOINTS))
        self.assertEqual(len(self.moves), len(DEFAULT_PATROL_WAYPOINTS))

        # Check waypoints data
        wp0 = report["waypoints"][0]
        self.assertEqual(wp0["waypoint_id"], "wp_left")
        self.assertEqual(wp0["pan"], 45.0)
        self.assertIsNotNone(wp0["jpeg_base64"])
        self.assertTrue(wp0["analysis"]["success"])
        self.assertEqual(wp0["analysis"]["scene"], "test_office")

    def test_patrol_busy_error_on_concurrent_start(self) -> None:
        service = PatrolService(
            vision_service=self.vision,
            move_fn=self._mock_move,
            stop_fn=self._mock_stop,
            snapshot_fn=self._mock_snapshot,
            settle_delay_s=0.5,
        )

        service.start_patrol(duration_ms=200)
        self.assertTrue(service.is_running())

        with self.assertRaises(PatrolBusyError):
            service.start_patrol(duration_ms=200)

        # Clean up
        service.stop_patrol()

    def test_patrol_stop_and_cancellation(self) -> None:
        service = PatrolService(
            vision_service=self.vision,
            move_fn=self._mock_move,
            stop_fn=self._mock_stop,
            snapshot_fn=self._mock_snapshot,
            settle_delay_s=0.5,
        )

        service.start_patrol(duration_ms=500)
        self.assertTrue(service.is_running())

        # Stop immediately
        stop_res = service.stop_patrol(timeout=1.0)
        self.assertEqual(stop_res["status"], "cancelled")
        self.assertTrue(self.stopped)
        self.assertFalse(service.is_running())

        report = service.get_latest_report()
        self.assertIsNotNone(report)
        self.assertEqual(report["status"], "cancelled")

    def test_patrol_handles_missing_frame_gracefully(self) -> None:
        service = PatrolService(
            vision_service=self.vision,
            move_fn=self._mock_move,
            stop_fn=self._mock_stop,
            snapshot_fn=lambda: None,  # no frame
            settle_delay_s=0.01,
        )

        service.start_patrol(duration_ms=10)
        for _ in range(50):
            if not service.is_running():
                break
            time.sleep(0.05)

        self.assertFalse(service.is_running())
        report = service.get_latest_report()
        self.assertEqual(report["status"], "completed")
        self.assertEqual(len(report["waypoints"]), len(DEFAULT_PATROL_WAYPOINTS))
        # Waypoints should have capture error recorded
        for wp in report["waypoints"]:
            self.assertEqual(wp["error"], "NO_FRAME_RETURNED")
            self.assertIsNone(wp["analysis"])

    def test_patrol_history_bounded(self) -> None:
        service = PatrolService(
            vision_service=self.vision,
            move_fn=self._mock_move,
            stop_fn=self._mock_stop,
            snapshot_fn=self._mock_snapshot,
            settle_delay_s=0.01,
        )

        single_wp = [{"id": "wp1", "name": "W1", "pan": 90.0, "tilt": 120.0}]
        for _ in range(12):
            service.start_patrol(waypoints=single_wp, duration_ms=5)
            while service.is_running():
                time.sleep(0.01)

        history = service.get_history(limit=20)
        self.assertLessEqual(len(history), 10)


if __name__ == "__main__":
    unittest.main()
