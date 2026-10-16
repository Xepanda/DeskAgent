"""
End-to-End Fault Injection and Disaster Recovery Test Suite for Desk Agent R1.

Covers:
1. Network Disconnect & Reconnect Recovery:
   - WebSocket abrupt disconnect and automatic exponential backoff reconnection.
   - State reconciliation following reconnect to eliminate offline state drift.
   - Fallback REST polling continuity during WebSocket downtime.
2. Sudden Crash & SQLite WAL Durability:
   - Abrupt crash/termination simulation.
   - SQLite WAL state preservation across reboots.
   - Catch-up occurrence generation for missed reminders during downtime gap.
   - Outbox queue durability and backoff state preservation.
3. High-Concurrency Resource Contention & Arbitration:
   - Multi-client race for exclusive camera viewing lease (RESOURCE_BUSY).
   - Explicit release vs automatic expiration handoff.
4. Idempotency & Deduplication Caching:
   - Repeated dispatch of identical request_ids across router calls.
   - Strict deduplication guaranteeing exactly-once execution.
"""

from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Mapping, Optional

from host.desk_agent.camera.camera_service import CameraViewerService
from host.desk_agent.connectors.ha.ha_connector import HAConnector
from host.desk_agent.core.models import RequestEnvelope, ResponseEnvelope, ResponseStatus
from host.desk_agent.core.router import Router
from host.desk_agent.services.life_service import LifeService


class MockFaultyWebSocket:
    """Mock WebSocket that can simulate abrupt disconnections and reconnects."""

    def __init__(self, script: list[str]) -> None:
        self.script = list(script)
        self.sent: list[str] = []
        self.closed = False
        self._cond = threading.Condition()

    def recv(self, timeout: Optional[float] = None) -> str:
        with self._cond:
            deadline = time.time() + (timeout if timeout is not None else 1.0)
            while not self.script and not self.closed:
                rem = deadline - time.time()
                if rem <= 0:
                    raise TimeoutError("WebSocket recv timeout")
                self._cond.wait(timeout=rem)
            if self.script:
                return self.script.pop(0)
            raise ConnectionError("Abrupt network disconnection injected")

    def send(self, data: str) -> None:
        self.sent.append(data)

    def push(self, data: str) -> None:
        with self._cond:
            self.script.append(data)
            self._cond.notify_all()

    def close(self) -> None:
        self.closed = True
        with self._cond:
            self._cond.notify_all()


class TestE2EFaultInjection(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "fault_test.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    # -------------------------------------------------------------------------
    # 1. Network Disconnect & Reconnect Recovery (Home Assistant Connector)
    # -------------------------------------------------------------------------
    def test_ha_abrupt_disconnect_and_reconnection_reconciliation(self) -> None:
        """Simulates HA WebSocket dropping mid-stream, verifying reconnection and state reconciliation."""
        ha_states = [{"entity_id": "light.desk", "state": "off"}]
        reconcile_call_count = 0

        def mock_http_transport(method: str, url: str, headers: Mapping[str, str], payload: Optional[Mapping[str, Any]], timeout: float):
            nonlocal reconcile_call_count
            if "/api/states" in url:
                reconcile_call_count += 1
                return 200, list(ha_states)
            return 200, {}

        # Connection 1: Auth OK, Subscribe OK, then sudden drop
        ws1_script = [
            json.dumps({"type": "auth_required", "ha_version": "2026.10"}),
            json.dumps({"type": "auth_ok", "ha_version": "2026.10"}),
            json.dumps({"id": 1, "type": "result", "success": True, "result": None}),
        ]
        ws1 = MockFaultyWebSocket(ws1_script)

        # Connection 2 (after reconnect): Auth OK, Subscribe OK, pushes new event
        ws2_script = [
            json.dumps({"type": "auth_required", "ha_version": "2026.10"}),
            json.dumps({"type": "auth_ok", "ha_version": "2026.10"}),
            json.dumps({"id": 2, "type": "result", "success": True, "result": None}),
            json.dumps({
                "id": 2,
                "type": "event",
                "event": {
                    "event_type": "state_changed",
                    "data": {
                        "entity_id": "light.desk",
                        "old_state": {"entity_id": "light.desk", "state": "off"},
                        "new_state": {"entity_id": "light.desk", "state": "on"},
                    },
                },
            }),
        ]
        ws2 = MockFaultyWebSocket(ws2_script)

        connections = [ws1, ws2]

        def ws_factory(url: str):
            if connections:
                return connections.pop(0)
            raise ConnectionError("No more connections configured")

        received_events = []
        event_received = threading.Event()

        def on_state_change(eid: str, old_st: Optional[str], new_st: Optional[str]) -> None:
            received_events.append((eid, old_st, new_st))
            if new_st == "on":
                event_received.set()

        ha = HAConnector(
            base_url="http://ha.fault-test:8123",
            token="test-token",
            transport=mock_http_transport,
            ws_factory=ws_factory,
        )

        stop_sub = ha.subscribe_state_changes(on_state_change)
        try:
            # 1. Verify initial connection
            deadline = time.time() + 1.0
            while not ha.is_ws_connected and time.time() < deadline:
                time.sleep(0.02)
            self.assertTrue(ha.is_ws_connected)

            # 2. Inject abrupt connection drop
            ha_states[0] = {"entity_id": "light.desk", "state": "on"}
            ws1.close()  # Drop socket

            # 3. Event should be captured by fallback poller or reconnect
            self.assertTrue(event_received.wait(timeout=3.0))

            # 4. Wait for WebSocket reconnection backoff (1s) to finish
            rec_deadline = time.time() + 3.0
            while not ha.is_ws_connected and time.time() < rec_deadline:
                time.sleep(0.05)

            self.assertTrue(ha.is_ws_connected, "WebSocket must reconnect after backoff")
            self.assertGreaterEqual(reconcile_call_count, 1)
            self.assertIn(("light.desk", "off", "on"), received_events)
        finally:
            stop_sub()
            ha.close()

    # -------------------------------------------------------------------------
    # 2. Sudden Crash & SQLite WAL Durability & Reminder Catch-up
    # -------------------------------------------------------------------------
    def test_crash_recovery_and_reminder_catchup(self) -> None:
        """Simulates abrupt process crash, verifying WAL durability and missed reminder catch-up on reboot."""
        # Phase 1: Service instance creates recurring reminder
        srv1 = LifeService(self.db_path)
        rem1 = srv1.create_reminder(
            reminder_id="rem-morning-tea",
            title="Drink morning tea",
            due_at="2026-10-16T08:00:00Z",
            repeat_rule="daily",
        )
        self.assertEqual(rem1["status"], "scheduled")

        srv1.queue_outbox_message("system.log", {"event": "service_started"})
        # Abrupt crash: terminate without clean close() or WAL checkpoint
        del srv1

        # Phase 2: Service reboots after downtime (current time is 09:30:00Z, 1.5h late)
        srv2 = LifeService(self.db_path)
        try:
            # Check WAL survived and reminder exists
            retrieved = srv2.get_reminder("rem-morning-tea")
            self.assertIsNotNone(retrieved)
            self.assertEqual(retrieved["title"], "Drink morning tea")

            # Outbox messages preserved
            outbox = srv2.get_pending_outbox_messages(limit=10)
            self.assertGreaterEqual(len(outbox), 1)

            # Detect missed occurrences with catchup flag
            due_occurrences = srv2.get_due_occurrences(now_iso="2026-10-16T09:30:00Z")
            self.assertEqual(len(due_occurrences), 1)
            occ = due_occurrences[0]
            self.assertTrue(occ["is_catchup"], "Missed reminder must be flagged as catchup")
            self.assertGreater(occ["delay_seconds"], 3600, "Delay must be over 1 hour")

            # Ring and automatically schedule next occurrence
            srv2.mark_occurrence_ringing(occ["occurrence_id"])
            all_occs = srv2.get_all_occurrences("rem-morning-tea")
            self.assertEqual(len(all_occs), 2)
            self.assertEqual(all_occs[1]["scheduled_at"], "2026-10-17T08:00:00Z")
        finally:
            srv2.close()

    # -------------------------------------------------------------------------
    # 3. High-Concurrency Resource Contention & Arbitration
    # -------------------------------------------------------------------------
    def test_concurrent_camera_lease_contention(self) -> None:
        """Fires 10 concurrent lease acquire requests; exactly 1 must succeed and 9 must receive RESOURCE_BUSY."""
        cam = CameraViewerService(default_lease_ms=5000, auto_start_pipeline=False)
        results: list[dict[str, Any]] = []
        lock = threading.Lock()

        def try_acquire(cid: str) -> None:
            res = cam.acquire_lease(cid, lease_ms=2000)
            with lock:
                results.append((cid, res))

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [executor.submit(try_acquire, f"client-{i}") for i in range(10)]
            for f in futures:
                f.result()

        granted = [r for r in results if r[1]["granted"]]
        busy = [r for r in results if not r[1]["granted"] and r[1]["reason"] == "RESOURCE_BUSY"]

        self.assertEqual(len(granted), 1, "Exactly one client must be granted exclusive lease")
        self.assertEqual(len(busy), 9, "All 9 concurrent clients must be rejected with RESOURCE_BUSY")

        winner_id = granted[0][0]
        # Winner can release lease
        self.assertTrue(cam.release_lease(winner_id))
        self.assertFalse(cam.is_lease_valid())

        # Now next client can acquire
        next_res = cam.acquire_lease("client-subsequent", lease_ms=2000)
        self.assertTrue(next_res["granted"])

    # -------------------------------------------------------------------------
    # 4. Duplicate Request Idempotency Caching
    # -------------------------------------------------------------------------
    def test_duplicate_request_idempotency_caching(self) -> None:
        """Verifies duplicate request_id returns cached response without re-executing handler."""
        router = Router()
        execution_count = 0

        def dummy_handler(req: RequestEnvelope) -> ResponseEnvelope:
            nonlocal execution_count
            execution_count += 1
            return ResponseEnvelope.create_success(req.request_id, {"execution": execution_count})

        router.register("test.compute", dummy_handler)

        identical_req = RequestEnvelope(
            schema_version="1.0.0",
            request_id="req-dedup-test-101",
            operation="test.compute",
            arguments={"val": 42},
            expires_at="2030-01-01T00:00:00Z",
        )

        resp1 = router.dispatch(identical_req)
        self.assertEqual(resp1.status, ResponseStatus.SUCCEEDED)
        self.assertEqual(resp1.result["execution"], 1)

        # Repeated dispatch of the same request_id must return cached response without re-executing
        for _ in range(10):
            resp = router.dispatch(identical_req)
            self.assertEqual(resp.status, ResponseStatus.SUCCEEDED)
            self.assertEqual(resp.result["execution"], 1)

        self.assertEqual(execution_count, 1, "Handler must be executed exactly once")


if __name__ == "__main__":
    unittest.main()
