from __future__ import annotations

import unittest

from host.desk_agent.connectors.robot.managed_codex_feedback_adapter import (
    ManagedCodexFeedbackAdapter,
)


class ManagedCodexFeedbackAdapterTests(unittest.TestCase):
    def test_maps_terminal_states_and_only_sends_safe_fields(self) -> None:
        sent: list[tuple[str, dict, float | None]] = []

        def sender(kind, data, timeout):
            sent.append((kind, data, timeout))
            return {"type": "sys.ack", "code": 0, "data": {"status": "accepted", "accepted": True}}

        adapter = ManagedCodexFeedbackAdapter(sender)
        cases = (
            ("codex.completed", "completed", "happy"),
            ("codex.failed", "failed", "alert"),
            ("codex.interrupted", "interrupted", "curious"),
        )
        for topic, resolution, preset in cases:
            result = adapter.send_terminal(topic, {
                "task_id": "task-1",
                "turn_id": "turn-1",
                "resolution": resolution,
                "summary": "safe assistant summary",
                "prompt": "secret prompt",
                "diff": "private diff",
                "path": "C:/private/file.py",
            })
            self.assertEqual(result, {"accepted": True, "state_confirmed": False, "audio_confirmed": False})
            kind, payload, timeout = sent[-1]
            self.assertEqual(kind, "ctrl.expression.set")
            self.assertEqual(payload, {
                "preset": preset,
                "text": {
                    "completed": "Codex任务已完成：safe assistant summary",
                    "failed": "Codex任务失败：safe assistant summary",
                    "interrupted": "Codex任务已中断：safe assistant summary",
                }[resolution],
                "event_type": topic,
                "play_audio": True,
            })
            self.assertEqual(timeout, 3.0)

    def test_normalizes_control_characters_and_truncates_at_utf8_boundary(self) -> None:
        captured = []

        def sender(_kind, payload, _timeout):
            captured.append(payload)
            return {"type": "sys.ack", "code": 0, "data": {"status": "accepted", "accepted": True}}

        summary = "\x00\n" + "汉" * 40
        ManagedCodexFeedbackAdapter(sender).send_terminal("codex.completed", {
            "task_id": "task-2", "resolution": "completed", "summary": summary,
        })
        text = captured[0]["text"]
        self.assertLessEqual(len(text.encode("utf-8")), 63)
        text.encode("utf-8").decode("utf-8")
        self.assertNotIn("\x00", text)
        self.assertNotIn("\n", text)
        self.assertTrue(text.startswith("Codex任务已完成："))

    def test_nack_and_nonaccepted_ack_fail_delivery(self) -> None:
        event = {"task_id": "task-3", "resolution": "failed", "summary": "safe"}
        replies = (
            {"type": "sys.nack", "code": 2, "data": {"reason": "busy"}},
            {"type": "sys.ack", "code": 0, "data": {"status": "accepted", "accepted": False}},
        )
        for reply in replies:
            with self.subTest(reply=reply), self.assertRaises(RuntimeError):
                ManagedCodexFeedbackAdapter(lambda *_args, response=reply: response).send_terminal(
                    "codex.failed", event
                )

    def test_rejects_topic_resolution_mismatch(self) -> None:
        adapter = ManagedCodexFeedbackAdapter(lambda *_: {})
        with self.assertRaises(ValueError):
            adapter.send_terminal("codex.failed", {
                "task_id": "task-4", "resolution": "completed", "summary": "safe",
            })


if __name__ == "__main__":
    unittest.main()
