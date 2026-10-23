#!/usr/bin/env python3
"""Invoke the existing TuyaOpen bridge against staged inputs and original provenance."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys


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
    files.add("firmware/.build/build/build_param.json")
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
        if relative.startswith("firmware/") and relative != "firmware/.build/build/build_param.json":
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


def load_sdk_gitpython(sdk_root: Path) -> Path:
    """Load GitPython only in this bridge process, never via child env vars."""
    site_packages = sdk_root.resolve() / ".venv" / "Lib" / "site-packages"
    if not (site_packages / "git" / "__init__.py").is_file() or not (site_packages / "gitdb" / "__init__.py").is_file():
        raise RuntimeError(f"SDK GitPython/GitDB packages are missing: {site_packages}")
    resolved = str(site_packages.resolve())
    if resolved not in sys.path:
        sys.path.insert(0, resolved)
    try:
        import git  # noqa: F401
        import gitdb  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(f"Cannot import SDK GitPython/GitDB in bridge process: {exc}") from exc
    return site_packages.resolve()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("sdk_root", type=Path)
    parser.add_argument("app_root", type=Path)
    parser.add_argument("archive_root", type=Path)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--source-sdk", type=Path, required=True)
    args = parser.parse_args()

    repo = args.repo.resolve()
    source_sdk = args.source_sdk.resolve()
    load_sdk_gitpython(source_sdk)
    bridge_path = repo / "tools" / "build_watcher_tuyaopen.py"
    spec = importlib.util.spec_from_file_location("deskagent_existing_tuyaopen_bridge", bridge_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot load existing TuyaOpen bridge: {bridge_path}")
    bridge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge)
    bridge.capture_firmware_inputs = lambda _firmware, _project_root, _sdk_root: source_snapshot(
        repo, source_sdk, args.app_root.resolve()
    )
    sys.argv = [str(bridge_path), str(args.sdk_root.resolve()), str(args.app_root.resolve()), str(args.archive_root.resolve())]
    return int(bridge.main())


if __name__ == "__main__":
    raise SystemExit(main())
