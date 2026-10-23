#!/usr/bin/env python3
"""Invoke the existing TuyaOpen bridge against staged inputs and original provenance."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def source_snapshot(repo: Path, sdk: Path, staged_firmware: Path) -> dict[str, object]:
    listing = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "firmware"],
        check=True, stdout=subprocess.PIPE,
    )
    excluded = ("firmware/.build/", "firmware/dist/", "firmware/.target_logging/")
    files = {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in listing.stdout.split(b"\0") if item
    }
    files = {item for item in files if not item.startswith(excluded)}
    files.update(("tools/build_watcher_firmware.ps1", "tools/build_watcher_tuyaopen.py"))
    records = []
    aggregate = hashlib.sha256()
    for relative in sorted(files):
        path = repo / Path(relative)
        if not path.is_file():
            raise RuntimeError(f"Original build input is missing: {relative}")
        record = {"path": relative, "sha256": _sha256(path)}
        records.append(record)
        aggregate.update(relative.encode("utf-8", errors="surrogateescape"))
        aggregate.update(b"\0")
        aggregate.update(record["sha256"].encode("ascii"))
        aggregate.update(b"\n")
        if relative.startswith("firmware/"):
            staged_input = staged_firmware / Path(relative).relative_to("firmware")
            if not staged_input.is_file() or _sha256(staged_input) != record["sha256"]:
                raise RuntimeError(f"Staged firmware input differs from repository source: {relative}")

    diff = subprocess.run(
        ["git", "-C", str(repo), "diff", "--binary", "HEAD", "--", "firmware", "tools/build_watcher_firmware.ps1", "tools/build_watcher_tuyaopen.py"],
        check=True, stdout=subprocess.PIPE,
    ).stdout
    head = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    sdk_head = subprocess.run(["git", "-C", str(sdk), "rev-parse", "HEAD"], check=True, text=True, stdout=subprocess.PIPE).stdout.strip()
    sdk_status = subprocess.run(["git", "-C", str(sdk), "status", "--short", "--untracked-files=no"], check=True, text=True, stdout=subprocess.PIPE).stdout
    return {
        "source_commit": head,
        "aggregate_sha256": aggregate.hexdigest().upper(),
        "diff_sha256": hashlib.sha256(diff).hexdigest().upper(),
        "diff_bytes": diff,
        "inputs": records,
        "external_sdk_state": {
            "head": sdk_head,
            "tracked_dirty": bool(sdk_status.strip()),
            "tracked_status_sha256": hashlib.sha256(sdk_status.encode()).hexdigest().upper(),
        },
    }


def main(argv: list[str] | None = None) -> int:
    del argv  # The standalone route has no runner-issued archive provenance token.
    raise RuntimeError(
        "BLOCKED: direct bridge invocation is disabled; use only a runner-authorized staged build."
    )


if __name__ == "__main__":
    raise SystemExit(main())
