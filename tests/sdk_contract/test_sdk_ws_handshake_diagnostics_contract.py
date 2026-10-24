"""L2 source contract for redacted WebSocket handshake failure diagnostics."""

from __future__ import annotations

import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
WS_SOURCE = REPO_ROOT / "firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c"


def _function_body(source: str, signature: str, next_signature: str) -> str:
    start = source.index(signature)
    end = source.index(next_signature, start)
    return source[start:end]


class TestSdkWsHandshakeDiagnosticsContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = WS_SOURCE.read_text(encoding="utf-8")

    def test_connect_failure_reasons_are_distinguished_and_redacted(self) -> None:
        connect = _function_body(
            self.source,
            "int desk_sdk_ws_client_connect_paired(void)",
            "int desk_sdk_ws_client_poll(int timeout_ms)",
        )
        for reason in (
            "hello_frame_send_error",
            "hello_frame_read_timeout",
            "hello_frame_read_error",
            "hello_non_text_opcode",
            "hello_ack_schema_rejected",
            "hello_commit_rejected",
        ):
            with self.subTest(reason=reason):
                self.assertIn(reason, connect)

        self.assertRegex(
            connect,
            r"ws_send_frame\(0x1[\s\S]*?failure_reason\s*=\s*\"hello_frame_send_error\"[\s\S]*?failure_errno\s*=\s*errno",
        )
        self.assertRegex(
            connect,
            r"read_rc\s*==\s*1[\s\S]*?failure_reason\s*=\s*\"hello_frame_read_timeout\"[\s\S]*?failure_errno\s*=\s*0",
        )
        self.assertRegex(
            connect,
            r"read_rc\s*!=\s*0[\s\S]*?failure_reason\s*=\s*\"hello_frame_read_error\"[\s\S]*?failure_errno\s*=\s*errno",
        )
        self.assertRegex(
            connect,
            r"opcode\s*!=\s*0x1[\s\S]*?failure_reason\s*=\s*\"hello_non_text_opcode\"[\s\S]*?failure_errno\s*=\s*0",
        )
        self.assertRegex(
            connect,
            r"!ws_hardware_hello_ack_valid[\s\S]*?failure_reason\s*=\s*\"hello_ack_schema_rejected\"[\s\S]*?failure_errno\s*=\s*0",
        )
        self.assertRegex(
            connect,
            r"!ws_commit_hardware_hello_ack[\s\S]*?failure_reason\s*=\s*\"hello_commit_rejected\"[\s\S]*?failure_errno\s*=\s*0",
        )
        self.assertRegex(connect, r"failure_errno\s*!=\s*0")
        self.assertRegex(connect, r"errno=%d")
        self.assertNotRegex(connect, r"printf\([^;]*(?:hello|ack|token|payload)")
        self.assertNotRegex(connect, r"printf\([^;]*,\s*(?:hello|s_ws_ack|payload|session_token)")

    def test_read_timeout_is_distinct_from_syscall_or_frame_error(self) -> None:
        wait = _function_body(
            self.source,
            "static int ws_wait(int fd, short events, int timeout_ms)",
            "static int ws_wait_until(",
        )
        self.assertRegex(wait, r"errno\s*=\s*0\s*;\s*int rc\s*=\s*poll")
        self.assertRegex(wait, r"rc\s*==\s*0[^;]*return\s+1")

        send_all = _function_body(
            self.source,
            "static int ws_send_all(int fd, const uint8_t *p, size_t n)",
            "static void ws_random(",
        )
        self.assertRegex(send_all, r"errno\s*=\s*0\s*;\s*ssize_t sent\s*=\s*send")

        read_frame = _function_body(
            self.source,
            "static int ws_read_frame(int timeout_ms, uint8_t *opcode",
            "void desk_sdk_ws_client_close(void)",
        )
        self.assertRegex(read_frame, r"wait_rc\s*==\s*1")
        self.assertRegex(read_frame, r"errno\s*=\s*0\s*;\s*ssize_t r\s*=\s*recv")


if __name__ == "__main__":
    unittest.main()
