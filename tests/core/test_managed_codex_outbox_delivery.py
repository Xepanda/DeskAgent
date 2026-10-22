from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from host.desk_agent.main import DeskAgentHostApp
from host.desk_agent.services.life_service import LifeService


class _Companion:
    def __init__(self) -> None:
        self.acknowledged: list[str] = []

    def acknowledge_task(self, task_id: str) -> None:
        self.acknowledged.append(task_id)


class ManagedCodexOutboxDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.service = LifeService(Path(self.temp.name) / "life.db")
        self.app = DeskAgentHostApp.__new__(DeskAgentHostApp)
        self.app.life_service = self.service
        self.app.managed_sdk_session = True
        self.app.managed_codex_feedback_adapter = None
        self.app.robot_adapter = None
        self.app.companion = _Companion()
        self.app._scheduler_stop = threading.Event()

    def tearDown(self) -> None:
        self.service.close()
        self.temp.cleanup()

    def test_terminal_event_is_deduplicated_and_payload_is_minimal(self) -> None:
        event = {
            "task_id": "task-1", "thread_id": "thread-1", "turn_id": "turn-1",
            "resolution": "completed", "summary": "assistant summary",
            "prompt": "do not send", "diff": "private diff", "path": "secret/path",
        }
        self.assertFalse(self.app._on_codex_event("codex.turn_completed", event))
        self.assertFalse(self.app._on_codex_event("codex.turn_completed", event))
        pending = self.service.get_pending_outbox_messages()
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["topic"], "codex.completed")
        self.assertEqual(pending[0]["payload"], {
            "task_id": "task-1", "turn_id": "turn-1",
            "resolution": "completed", "summary": "assistant summary",
        })

    def test_managed_nack_keeps_pending_with_backoff_and_no_tcp_fallback(self) -> None:
        self.app._on_codex_event("codex.failed", {
            "task_id": "task-2", "turn_id": "turn-2", "summary": "safe summary",
        })

        class NackAdapter:
            def send_terminal(_self, _topic, _payload):
                self.app._scheduler_stop.set()
                raise RuntimeError("device NACK")

        self.app.managed_codex_feedback_adapter = NackAdapter()
        self.app._delivery_loop()
        pending = self.service.get_pending_outbox_messages(include_delayed=True)
        self.assertEqual(len(pending), 1)
        self.assertEqual(pending[0]["retry_count"], 1)
        self.assertIsNotNone(pending[0]["next_retry_at"])
        self.assertEqual(self.app.companion.acknowledged, [])
        self.assertIsNone(self.app.robot_adapter)

    def test_accepted_ack_marks_dispatched_without_claiming_audio_confirmation(self) -> None:
        self.app._on_codex_event("codex.turn_completed", {
            "task_id": "task-3", "turn_id": "turn-3", "status": "completed",
            "summary": "safe summary",
        })

        class AcceptedAdapter:
            def send_terminal(_self, _topic, _payload):
                self.app._scheduler_stop.set()
                return {"accepted": True, "state_confirmed": False, "audio_confirmed": False}

        self.app.managed_codex_feedback_adapter = AcceptedAdapter()
        self.app._delivery_loop()
        rows = self.service._get_connection().execute(
            "SELECT state FROM outbox WHERE event_type = 'codex.completed'"
        ).fetchall()
        self.assertEqual(rows[0]["state"], "dispatched")
        self.assertEqual(self.app.companion.acknowledged, ["task-3"])


if __name__ == "__main__":
    unittest.main()
