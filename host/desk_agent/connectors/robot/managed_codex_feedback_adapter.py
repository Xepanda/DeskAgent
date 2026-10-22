"""Deliver sanitized Codex terminal summaries through an existing managed SDK session."""

from __future__ import annotations

import unicodedata
from typing import Any, Callable, Mapping


_TERMINAL_STATES = {
    "completed": ("codex.completed", "happy", "Codex任务已完成："),
    "failed": ("codex.failed", "alert", "Codex任务失败："),
    "interrupted": ("codex.interrupted", "curious", "Codex任务已中断："),
}


def _clean_text(value: str, prefix: str, max_bytes: int = 63) -> str:
    """Normalize control/whitespace characters and truncate on a UTF-8 boundary."""
    normalized = "".join(
        " " if unicodedata.category(char) == "Cc" else char
        for char in value
    )
    text = prefix + " ".join(normalized.split())
    result: list[str] = []
    size = 0
    for char in text:
        char_size = len(char.encode("utf-8"))
        if size + char_size > max_bytes:
            break
        result.append(char)
        size += char_size
    return "".join(result)


class ManagedCodexFeedbackAdapter:
    """Use the command sender from the already-running official SDK Application."""

    def __init__(self, command_sender: Callable[[str, dict[str, Any], float | None], dict[str, Any]]) -> None:
        self._send_command = command_sender

    def send_terminal(self, topic: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        resolution = payload.get("resolution")
        expected_topic, preset, prefix = _TERMINAL_STATES.get(str(resolution), (None, None, None))
        if expected_topic != topic:
            raise ValueError("Codex terminal topic and resolution do not match")
        task_id = payload.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("Codex terminal task_id is required")

        # The A5 summary is the only result content forwarded. Never copy any other
        # event fields (prompt, output, diff, paths, approval data) into the command.
        summary = payload.get("summary")
        if not isinstance(summary, str):
            summary = ""
        text = _clean_text(summary, prefix)
        ack = self._send_command(
            "ctrl.expression.set",
            {
                "preset": preset,
                "text": text,
                "event_type": topic,
                "play_audio": True,
            },
            3.0,
        )
        if not isinstance(ack, dict) or ack.get("type") != "sys.ack" or ack.get("code") != 0:
            raise RuntimeError(f"device rejected Codex terminal command: {ack!r}")
        data = ack.get("data")
        if not isinstance(data, dict) or data.get("status") != "accepted" or data.get("accepted") is not True:
            raise RuntimeError(f"device ACK did not confirm accepted status: {ack!r}")
        return {"accepted": True, "state_confirmed": False, "audio_confirmed": False}
