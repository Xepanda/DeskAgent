"""Command-line client for the Host's existing health and motion RPC API."""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence, TextIO

from host.desk_agent.core.models import RequestEnvelope


DEFAULT_HOST = "http://127.0.0.1:8088"
TOKEN_ENV = "DESK_AGENT_AUTH_TOKEN"
_KNOWN_STATUSES = {"accepted", "succeeded", "failed", "unknown"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query or control hardware through the DeskAgent Host API.")
    _add_connection_options(parser, defaults=True)
    commands = parser.add_subparsers(dest="command", required=True)
    status = commands.add_parser("status", help="Show Host and robot connection status")
    _add_connection_options(status)
    move = commands.add_parser("move", help="Request a robot motion through Host arbitration")
    _add_connection_options(move)
    move.add_argument("--pan", type=float, required=True, help="Pan angle in degrees (0..180)")
    move.add_argument("--tilt", type=float, required=True, help="Tilt angle in degrees (100..140)")
    move.add_argument("--duration-ms", type=int, default=500, help="Transition duration (100..5000 ms)")
    stop = commands.add_parser("stop", help="Request a robot stop through Host arbitration")
    _add_connection_options(stop)
    stop.add_argument("--normal", dest="emergency", action="store_false", help="Request a non-emergency stop")
    stop.set_defaults(emergency=True)
    return parser


def _add_connection_options(parser: argparse.ArgumentParser, *, defaults: bool = False) -> None:
    default = argparse.SUPPRESS if not defaults else DEFAULT_HOST
    parser.add_argument("--host", default=default, help=f"Host base URL (default: {DEFAULT_HOST})")
    parser.add_argument("--token", default=argparse.SUPPRESS, help=f"Bearer token (or set {TOKEN_ENV})")
    parser.add_argument(
        "--timeout", type=float, default=argparse.SUPPRESS if not defaults else 5.0,
        help="HTTP timeout in seconds (default: 5)",
    )


def _request(
    url: str,
    *,
    method: str,
    token: str | None,
    timeout: float,
    payload: Mapping[str, Any] | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> tuple[int, bytes]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with opener(request, timeout=timeout) as response:
        return response.status, response.read()


def _safe_error(message: str, token: str | None) -> str:
    return message.replace(token, "[REDACTED]") if token else message


def _rpc_payload(operation: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    envelope = RequestEnvelope(
        schema_version="1.0.0",
        request_id=f"req-cli-{uuid.uuid4().hex}",
        operation=operation,
        arguments=arguments,
        expires_at=(now + timedelta(seconds=30)).isoformat().replace("+00:00", "Z"),
    )
    return envelope.to_dict()


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> int:
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    parser = _parser()
    args = parser.parse_args(argv)
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if not args.host.startswith(("http://", "https://")):
        parser.error("--host must start with http:// or https://")
    env = os.environ if environ is None else environ
    explicit_token = getattr(args, "token", None)
    token = explicit_token if explicit_token is not None else env.get(TOKEN_ENV)
    base_url = args.host.rstrip("/")

    try:
        if args.command == "status":
            _, body = _request(
                f"{base_url}/health", method="GET", token=token, timeout=args.timeout, opener=opener
            )
            health = json.loads(body.decode("utf-8"))
            healthy = health.get("status") == "healthy"
            connected = health.get("robot_connected") is True
            print(f"host_status={health.get('status', 'unknown')}", file=out)
            print(f"robot_connected={str(connected).lower()}", file=out)
            if not healthy:
                print("Host health endpoint reports an unhealthy or unknown state", file=err)
                return 1
            return 0

        if args.command == "move":
            if not 0 <= args.pan <= 180:
                parser.error("--pan must be between 0 and 180")
            if not 100 <= args.tilt <= 140:
                parser.error("--tilt must be between 100 and 140")
            if not 100 <= args.duration_ms <= 5000:
                parser.error("--duration-ms must be between 100 and 5000")
            operation = "resource.motion.move"
            arguments = {"pan_deg": args.pan, "tilt_deg": args.tilt, "duration_ms": args.duration_ms}
        else:
            operation = "resource.motion.stop"
            arguments = {"emergency": args.emergency}

        _, body = _request(
            f"{base_url}/api/rpc",
            method="POST",
            token=token,
            timeout=args.timeout,
            payload=_rpc_payload(operation, arguments),
            opener=opener,
        )
        response = json.loads(body.decode("utf-8"))
        raw_status = response.get("status")
        status = raw_status if raw_status in _KNOWN_STATUSES else "unknown"
        print(f"status={status}", file=out)
        if response.get("request_id"):
            print(f"request_id={response['request_id']}", file=out)
        if status == "accepted":
            print("dispatch accepted; physical completion not confirmed", file=out)
        if status in ("failed", "unknown"):
            error = response.get("error") or {}
            code = error.get("code") if isinstance(error, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            detail = ": ".join(str(part) for part in (code, message) if part)
            print(_safe_error(detail or "Host returned a failed or unrecognized response", token), file=err)
            return 1
        return 0
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code} {exc.reason}", file=err)
        return 1
    except (TimeoutError, socket.timeout):
        print("Request timed out", file=err)
        return 1
    except urllib.error.URLError as exc:
        reason = "connection failed" if isinstance(exc.reason, OSError) else str(exc.reason)
        print(_safe_error(f"Host request failed: {reason}", token), file=err)
        return 1
    except (UnicodeDecodeError, json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
        print(_safe_error(f"Invalid Host response: {exc}", token), file=err)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
