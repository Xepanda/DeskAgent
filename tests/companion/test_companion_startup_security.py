"""
WP5 / docs/implementation/interfaces.md §6.5 — Windows Companion startup security.

Ruling R5: default listen host is loopback; a non-loopback listener MUST be protected by an
explicitly configured, non-empty, non-placeholder auth_token, otherwise startup is refused
(fail closed). Empty/None tokens and repository-committed placeholder tokens are rejected.
Every startup entry point (library, CLI, PowerShell launchers) must share this validation.

These tests never bind a non-loopback interface: non-loopback cases are asserted on the
validation / resolution functions, and the live HTTP checks bind 127.0.0.1 ephemeral ports only.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

from companion.src.codex_companion import (
    DEFAULT_COMPANION_HOST,
    MIN_LAN_AUTH_TOKEN_LENGTH,
    CompanionConfigError,
    create_companion_server,
    is_loopback_host,
    is_placeholder_token,
    resolve_companion_settings,
    validate_companion_binding,
)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXAMPLE_CONFIG = os.path.join(REPO_ROOT, "deploy", "windows", "companion-config.example.json")

# The placeholder token literally committed in this repository and named by §6.5.
REPO_PLACEHOLDER_TOKEN = "REPLACE_WITH_STRONG_SHARED_TOKEN_MATCHING_TAISHANPI_CONFIG"
# An explicit, strong, non-placeholder token (test-only value, not a production secret).
STRONG_TOKEN = "wp5-strong-shared-token-9f3c1d7a4b2e"

NON_LOOPBACK_HOSTS = ("0.0.0.0", "192.168.1.50", "::", "10.0.0.7", "windows-pc.local")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def rpc_body() -> bytes:
    return json.dumps({"operation": "codex.get", "arguments": {"task_id": "wp5-t1"}}).encode("utf-8")


class TestLoopbackAndPlaceholderDetection(unittest.TestCase):
    def test_loopback_host_detection(self) -> None:
        for host in ("127.0.0.1", "127.0.0.5", "::1", "[::1]", "localhost", "LOCALHOST"):
            self.assertTrue(is_loopback_host(host), f"{host} should be loopback")
        for host in ("0.0.0.0", "::", "192.168.1.50", "10.0.0.7", "windows-pc.local", "", "   ", None):
            self.assertFalse(is_loopback_host(host), f"{host} should NOT be loopback")

    def test_default_host_is_loopback(self) -> None:
        self.assertEqual(DEFAULT_COMPANION_HOST, "127.0.0.1")

    def test_placeholder_token_detection(self) -> None:
        placeholders = (
            REPO_PLACEHOLDER_TOKEN,
            "REPLACE_WITH_STRONG_SHARED_TOKEN_OR_LEAVE_EMPTY_FOR_LOCAL_ONLY",
            "REPLACE_WITH_ANYTHING_ELSE",
            "replace_with_a_strong_token",
            "changeme",
            "CHANGEME-1234",
            "CHANGE_ME_PLEASE_1234567890",
            "your_token_here",
            "PLACEHOLDER",
            "example-token",
            "TODO_FILL_THIS_IN",
            "<token>",
            "",
            "    ",
            None,
        )
        for token in placeholders:
            self.assertTrue(is_placeholder_token(token), f"{token!r} should be a placeholder")
        self.assertFalse(is_placeholder_token(STRONG_TOKEN))
        self.assertFalse(is_placeholder_token("Zq7-kJ2mR9xT4pL6wY1nB3vC"))


class TestBindingValidation(unittest.TestCase):
    """Non-loopback host + unusable token must refuse to start (fail closed)."""

    def test_non_loopback_with_empty_or_none_token_refused(self) -> None:
        for host in NON_LOOPBACK_HOSTS:
            for token in ("", "   ", None):
                with self.assertRaises(CompanionConfigError) as ctx:
                    validate_companion_binding(host, token)
                self.assertIn("Refusing to start", str(ctx.exception))

    def test_non_loopback_with_repo_placeholder_token_refused(self) -> None:
        for host in NON_LOOPBACK_HOSTS:
            with self.assertRaises(CompanionConfigError) as ctx:
                validate_companion_binding(host, REPO_PLACEHOLDER_TOKEN)
            message = str(ctx.exception)
            self.assertIn("Refusing to start", message)
            self.assertIn("placeholder", message.lower())

    def test_non_loopback_with_short_token_refused(self) -> None:
        with self.assertRaises(CompanionConfigError) as ctx:
            validate_companion_binding("0.0.0.0", "abc123")
        self.assertIn(str(MIN_LAN_AUTH_TOKEN_LENGTH), str(ctx.exception))

    def test_non_loopback_with_non_string_token_refused(self) -> None:
        with self.assertRaises(CompanionConfigError):
            validate_companion_binding("0.0.0.0", 12345678901234567890)  # type: ignore[arg-type]

    def test_non_loopback_with_strong_token_allowed(self) -> None:
        for host in NON_LOOPBACK_HOSTS:
            validate_companion_binding(host, STRONG_TOKEN)  # must not raise

    def test_loopback_default_behaviour_unchanged(self) -> None:
        # Loopback with no token: local isolation, no auth. Still allowed.
        validate_companion_binding("127.0.0.1", None)
        validate_companion_binding("127.0.0.1", "")
        validate_companion_binding("localhost", None)
        # Loopback with an explicit token: allowed (and enforced by the handler).
        validate_companion_binding("127.0.0.1", STRONG_TOKEN)

    def test_loopback_with_placeholder_token_refused(self) -> None:
        with self.assertRaises(CompanionConfigError):
            validate_companion_binding("127.0.0.1", REPO_PLACEHOLDER_TOKEN)


class TestSettingsResolutionAndServerConstruction(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write_config(self, payload: dict) -> str:
        path = os.path.join(self.tmp.name, "companion-config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def test_create_server_refuses_non_loopback_without_token(self) -> None:
        for token in ("", None):
            with self.assertRaises(CompanionConfigError):
                create_companion_server(port=free_port(), host="0.0.0.0", auth_token=token)

    def test_create_server_refuses_non_loopback_with_placeholder_token(self) -> None:
        with self.assertRaises(CompanionConfigError):
            create_companion_server(
                port=free_port(), host="0.0.0.0", auth_token=REPO_PLACEHOLDER_TOKEN
            )

    def test_config_file_non_loopback_without_token_refused(self) -> None:
        for token in ("", REPO_PLACEHOLDER_TOKEN):
            path = self._write_config({"host": "0.0.0.0", "port": free_port(), "auth_token": token})
            with self.assertRaises(CompanionConfigError):
                create_companion_server(config_path=path)

        # auth_token key absent entirely -> falls back to None -> refused.
        path = self._write_config({"host": "0.0.0.0", "port": free_port()})
        with self.assertRaises(CompanionConfigError):
            create_companion_server(config_path=path)

    def test_config_file_non_loopback_with_strong_token_resolves(self) -> None:
        # Resolve only (never bind 0.0.0.0 in tests).
        path = self._write_config({"host": "0.0.0.0", "port": 18765, "auth_token": STRONG_TOKEN})
        settings = resolve_companion_settings(config_path=path)
        self.assertEqual(settings["host"], "0.0.0.0")
        self.assertEqual(settings["auth_token"], STRONG_TOKEN)

    def test_missing_config_file_refused(self) -> None:
        missing = os.path.join(self.tmp.name, "does-not-exist.json")
        with self.assertRaises(CompanionConfigError) as ctx:
            create_companion_server(config_path=missing)
        self.assertIn("not found", str(ctx.exception))

    def test_malformed_config_file_refused(self) -> None:
        path = os.path.join(self.tmp.name, "broken.json")
        with open(path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        with self.assertRaises(Exception):
            create_companion_server(config_path=path)

    def test_shipped_example_config_is_safe_by_default(self) -> None:
        """The repo-committed example must be loopback-only with no usable token."""
        with open(EXAMPLE_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        self.assertTrue(is_loopback_host(cfg.get("host")), "example must bind loopback")
        self.assertFalse(
            (cfg.get("auth_token") or "").strip(),
            "example must not ship a usable auth_token",
        )
        self.assertFalse(
            is_placeholder_token(cfg.get("auth_token")) and bool((cfg.get("auth_token") or "").strip()),
            "example must not ship a live placeholder token",
        )
        # Resolving the shipped example must succeed and require no auth (loopback only).
        settings = resolve_companion_settings(config_path=EXAMPLE_CONFIG)
        self.assertTrue(is_loopback_host(settings["host"]))
        self.assertFalse((settings["auth_token"] or "").strip())


class TestLiveServerAuth(unittest.TestCase):
    """Live HTTP checks, bound to 127.0.0.1 ephemeral ports only."""

    def _serve(self, **kwargs) -> tuple[object, threading.Thread, int]:
        server = create_companion_server(**kwargs)
        port = server.server_address[1]
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        time.sleep(0.1)
        return server, thread, port

    def _post_rpc(self, port: int, headers: dict | None = None):
        req = urllib.request.Request(f"http://127.0.0.1:{port}/rpc", data=rpc_body(), headers=headers or {})
        return urllib.request.urlopen(req, timeout=3.0)

    def test_loopback_default_server_starts_and_needs_no_token(self) -> None:
        server, _, port = self._serve(port=free_port(), host="127.0.0.1")
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)
            with self._post_rpc(port) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            server.shutdown()
            server.server_close()

    def test_strong_token_server_requires_token_on_rpc(self) -> None:
        server, _, port = self._serve(port=free_port(), host="127.0.0.1", auth_token=STRONG_TOKEN)
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3.0) as resp:
                self.assertEqual(resp.status, 200)

            # No token -> 401
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post_rpc(port)
            self.assertEqual(ctx.exception.code, 401)

            # Wrong token -> 401
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post_rpc(port, {"Authorization": "Bearer wrong-token-value-000"})
            self.assertEqual(ctx.exception.code, 401)

            # Correct token -> 200
            with self._post_rpc(port, {"Authorization": f"Bearer {STRONG_TOKEN}"}) as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(json.loads(resp.read().decode("utf-8")).get("success"))

            # X-Companion-Token header is also accepted
            with self._post_rpc(port, {"X-Companion-Token": STRONG_TOKEN}) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            server.shutdown()
            server.server_close()

    def test_lan_strong_token_configuration_still_works_over_http(self) -> None:
        """A valid LAN token keeps the TaishanPi -> Windows Companion path working."""
        # The non-loopback binding itself is validated (never bound here) ...
        settings = resolve_companion_settings(host="0.0.0.0", port=0, auth_token=STRONG_TOKEN)
        self.assertEqual(settings["host"], "0.0.0.0")
        self.assertTrue(is_loopback_host("127.0.0.1"))
        # ... and the same token is what protects the RPC surface.
        server, _, port = self._serve(port=free_port(), host="127.0.0.1", auth_token=settings["auth_token"])
        try:
            with self.assertRaises(urllib.error.HTTPError) as ctx:
                self._post_rpc(port)
            self.assertEqual(ctx.exception.code, 401)
            with self._post_rpc(port, {"Authorization": f"Bearer {STRONG_TOKEN}"}) as resp:
                self.assertEqual(resp.status, 200)
        finally:
            server.shutdown()
            server.server_close()


class TestCliEntryPointEnforcesSameRule(unittest.TestCase):
    """The CLI must enforce the exact same rule as the library function."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _write_config(self, payload: dict) -> str:
        path = os.path.join(self.tmp.name, "cli-companion-config.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return path

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONPATH"] = REPO_ROOT + os.pathsep + env.get("PYTHONPATH", "")
        return subprocess.run(
            [sys.executable, "-m", "companion.src.codex_companion", *args],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    def test_cli_refuses_non_loopback_without_token(self) -> None:
        path = self._write_config({"host": "0.0.0.0", "port": free_port(), "auth_token": ""})
        res = self._run_cli("--config", path)
        self.assertEqual(res.returncode, 2, f"stdout={res.stdout} stderr={res.stderr}")
        self.assertIn("REFUSING TO START", res.stderr)

    def test_cli_refuses_non_loopback_with_repo_placeholder_token(self) -> None:
        path = self._write_config(
            {"host": "0.0.0.0", "port": free_port(), "auth_token": REPO_PLACEHOLDER_TOKEN}
        )
        res = self._run_cli("--config", path)
        self.assertEqual(res.returncode, 2, f"stdout={res.stdout} stderr={res.stderr}")
        self.assertIn("REFUSING TO START", res.stderr)

    def test_cli_refuses_missing_config_file(self) -> None:
        res = self._run_cli("--config", os.path.join(self.tmp.name, "nope.json"))
        self.assertEqual(res.returncode, 2, f"stdout={res.stdout} stderr={res.stderr}")

    def test_cli_check_config_accepts_loopback_default(self) -> None:
        path = self._write_config({"host": "127.0.0.1", "port": free_port()})
        res = self._run_cli("--config", path, "--check-config")
        self.assertEqual(res.returncode, 0, f"stdout={res.stdout} stderr={res.stderr}")
        payload = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertTrue(payload["loopback_only"])
        self.assertFalse(payload["auth_required"])

    def test_cli_check_config_accepts_non_loopback_with_strong_token(self) -> None:
        """LAN reachability is preserved when a strong token is explicitly configured."""
        path = self._write_config({"host": "0.0.0.0", "port": free_port(), "auth_token": STRONG_TOKEN})
        res = self._run_cli("--config", path, "--check-config")
        self.assertEqual(res.returncode, 0, f"stdout={res.stdout} stderr={res.stderr}")
        payload = json.loads(res.stdout.strip().splitlines()[-1])
        self.assertEqual(payload["host"], "0.0.0.0")
        self.assertFalse(payload["loopback_only"])
        self.assertTrue(payload["auth_required"])
        # The CLI must never echo the token itself.
        self.assertNotIn(STRONG_TOKEN, res.stdout)

    def test_cli_check_config_rejects_placeholder_on_non_loopback(self) -> None:
        path = self._write_config(
            {"host": "0.0.0.0", "port": free_port(), "auth_token": REPO_PLACEHOLDER_TOKEN}
        )
        res = self._run_cli("--config", path, "--check-config")
        self.assertEqual(res.returncode, 2, f"stdout={res.stdout} stderr={res.stderr}")

    def test_launcher_scripts_do_not_copy_example_config_and_check_exit_code(self) -> None:
        """Static guard: the PowerShell launchers must not fall back to the example config."""
        for name in ("start-companion.ps1", "run-companion.ps1"):
            with open(os.path.join(REPO_ROOT, "deploy", "windows", name), "r", encoding="utf-8") as f:
                script = f.read()
            self.assertNotIn(
                "Copy-Item \"$PSScriptRoot\\companion-config.example.json\" $ConfigFile",
                script,
                f"{name} must not auto-copy the example config into a live config",
            )
            self.assertIn("--check-config", script, f"{name} must run the shared pre-flight validation")
            self.assertIn("exit $LASTEXITCODE", script, f"{name} must exit non-zero on refusal")


if __name__ == "__main__":
    unittest.main()
