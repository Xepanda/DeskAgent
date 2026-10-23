#!/usr/bin/env python3
"""Build DeskAgent firmware with all writable project state under .target_logging.

Only locked SDK source/toolchain inputs and reviewed project defaults may enter
the stage. Generated configuration is created from scratch inside the archive.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone


EXCLUDED_FIRMWARE_DIRS = {
    ".build", "build", "dist", ".target_logging", "__pycache__",
}
IDF_VERSION = "5.4"
IDF_TARGET = "esp32s3"
SDK_PROVENANCE_MANIFEST = Path("docs/acceptance/cr035-sdk-inputs.json")


def is_within(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


SUBST_CANDIDATES = tuple("ZYXWVUTSQPONMLKJIHGFECBA")


def _drive_letter_available(letter: str) -> bool:
    if os.name != "nt":
        return False
    import ctypes
    mask = ctypes.windll.kernel32.GetLogicalDrives()
    return not bool(mask & (1 << (ord(letter.upper()) - ord("A"))))


def _normalized_windows_path(path: str | Path) -> str:
    return ntpath.normcase(ntpath.normpath(str(path))).rstrip("\\/")


def _subst_targets(runner=subprocess.run) -> dict[str, str]:
    result = runner(["subst"], check=False, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if result.returncode:
        raise RuntimeError(f"Could not inspect temporary subst mappings: {result.stderr or result.stdout}")
    targets: dict[str, str] = {}
    for line in result.stdout.splitlines():
        match = re.match(r"^\s*([A-Za-z]):\\:\s*=>\s*(.*?)\s*$", line)
        if match:
            targets[match.group(1).upper()] = match.group(2)
    return targets


def _remove_archive_subst(letter: str, archive: Path, runner=subprocess.run) -> None:
    target = _subst_targets(runner).get(letter.upper())
    if target is None:
        return
    if _normalized_windows_path(target) != _normalized_windows_path(archive):
        raise RuntimeError(f"Temporary subst drive {letter}: changed to another target; leaving it untouched: {target}")
    removed = runner(["subst", f"{letter.upper()}:", "/D"], check=False,
                     capture_output=True, text=True, encoding="utf-8", errors="replace")
    remaining = _subst_targets(runner).get(letter.upper())
    if removed.returncode or remaining is not None:
        raise RuntimeError(f"Could not remove temporary subst drive {letter}: (exit={removed.returncode}, remaining={remaining})")


@contextmanager
def temporary_archive_mount(
    archive: Path,
    *,
    runner=subprocess.run,
    drive_available=None,
    candidates: tuple[str, ...] = SUBST_CANDIDATES,
):
    """Expose a single run archive through a temporary short drive and always unmount it."""
    if os.name != "nt" and drive_available is None:
        raise RuntimeError("Temporary archive subst mounts are supported only on Windows.")
    available = drive_available or _drive_letter_available
    letter = next((candidate.upper() for candidate in candidates if available(candidate.upper())), None)
    if letter is None:
        raise RuntimeError("No free drive letter is available for the isolated archive subst mount.")
    archive = Path(os.path.abspath(str(archive)))
    mounted = runner(["subst", f"{letter}:", str(archive)], check=False,
                     capture_output=True, text=True, encoding="utf-8", errors="replace")
    current_target = _subst_targets(runner).get(letter)
    expected_target = _normalized_windows_path(archive)
    if mounted.returncode:
        if current_target is not None and _normalized_windows_path(current_target) == expected_target:
            _remove_archive_subst(letter, archive, runner)
        raise RuntimeError(f"Could not create temporary subst drive {letter}: (exit={mounted.returncode}, {mounted.stderr or mounted.stdout})")
    if current_target is None or _normalized_windows_path(current_target) != expected_target:
        if current_target is not None and _normalized_windows_path(current_target) == expected_target:
            _remove_archive_subst(letter, archive, runner)
        raise RuntimeError(f"Temporary subst drive {letter}: does not map to the requested archive: {current_target}")
    try:
        yield Path(f"{letter}:\\")
    finally:
        _remove_archive_subst(letter, archive, runner)


def archive_stage_paths(mount_root: Path) -> dict[str, Path]:
    stage = mount_root / "stage"
    return {"archive": mount_root, "stage": stage, "sdk": stage / "sdk", "app": stage / "app"}


def mounted_bridge_wrapper_source() -> str:
    """Keep staged paths lexical when the existing bridge resolves Windows paths."""
    return '''#!/usr/bin/env python3
import os
from pathlib import Path
import runpy
import sys

bridge_path = Path(sys.argv[1])
mount_root = Path(os.path.abspath(sys.argv[2]))
bridge_args = sys.argv[3:]
_path_resolve = Path.resolve
_realpath = os.path.realpath

def _under_mount(path):
    try:
        Path(os.path.abspath(os.fspath(path))).relative_to(mount_root)
        return True
    except (ValueError, TypeError):
        return False

def _resolve(path, *args, **kwargs):
    if _under_mount(path):
        return Path(os.path.abspath(os.fspath(path)))
    return _path_resolve(path, *args, **kwargs)

def _resolve_realpath(path, *args, **kwargs):
    if _under_mount(path):
        return os.path.abspath(os.fspath(path))
    return _realpath(path, *args, **kwargs)

Path.resolve = _resolve
os.path.realpath = _resolve_realpath
sys.argv = [str(bridge_path), *bridge_args]
namespace = runpy.run_path(str(bridge_path), run_name="_deskagent_mounted_bridge")
raise SystemExit(namespace["main"]())
'''


def mounted_bridge_command(
    python: Path,
    wrapper: Path,
    bridge: Path,
    mount_root: Path,
    repo: Path,
    source_sdk: Path,
) -> list[str]:
    paths = archive_stage_paths(mount_root)
    return [
        str(python), str(wrapper), str(bridge), str(mount_root),
        str(paths["sdk"]), str(paths["app"]), str(paths["archive"]),
        "--repo", str(repo), "--source-sdk", str(source_sdk),
    ]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def _copy_firmware(source: Path, destination: Path) -> None:
    def ignore(directory: str, names: list[str]) -> set[str]:
        ignored = {name for name in names if name in EXCLUDED_FIRMWARE_DIRS}
        ignored.update(name for name in names if name.endswith(".pyc"))
        return ignored

    shutil.copytree(source, destination, ignore=ignore, symlinks=False)


def _idf_repo_version(idf_root: Path) -> str:
    version_header = idf_root / "components" / "esp_common" / "include" / "esp_idf_version.h"
    try:
        contents = version_header.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"Cannot read staged ESP-IDF version header: {version_header}") from exc
    major = re.search(r"(?m)^#define\s+ESP_IDF_VERSION_MAJOR\s+(\d+)\b", contents)
    minor = re.search(r"(?m)^#define\s+ESP_IDF_VERSION_MINOR\s+(\d+)\b", contents)
    if major is None or minor is None:
        raise RuntimeError(f"Staged ESP-IDF version is not identifiable from {version_header}")
    return f"{major.group(1)}.{minor.group(1)}"


def canonical_idf_registry_path(idf_root: Path) -> Path:
    """Match ESP-IDF activate.py, which canonicalizes its own script directory."""
    lexical_path = os.path.abspath(str(idf_root))
    return Path(os.path.realpath(lexical_path))


def register_staged_idf_target(idf_root: Path, espressif_root: Path, target: str) -> dict[str, object]:
    """Register this staged ESP-IDF repo in its staged-only idf-env.json."""
    idf_path = canonical_idf_registry_path(idf_root)
    idf_io_path = Path(os.path.abspath(str(idf_root)))
    espressif_path = Path(os.path.abspath(str(espressif_root)))
    version = _idf_repo_version(idf_io_path)
    if version != IDF_VERSION:
        raise RuntimeError(f"Expected staged ESP-IDF {IDF_VERSION}, found {version} at {idf_path}")
    if target != IDF_TARGET:
        raise RuntimeError(f"Only {IDF_TARGET} is supported by this isolated firmware runner, got {target}")

    env_path = espressif_path / "idf-env.json"
    try:
        payload = json.loads(env_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"Cannot read staged ESP-IDF environment registry: {env_path}") from exc
    installed = payload.get("idfInstalled") if isinstance(payload, dict) else None
    if not isinstance(installed, dict):
        raise RuntimeError(f"Staged ESP-IDF registry has no idfInstalled object: {env_path}")

    repo_id = f"{idf_path}-v{version}"
    record = {
        "version": version,
        "path": str(idf_path),
        "features": ["core"],
        "targets": [target],
    }
    installed[repo_id] = record
    temporary = env_path.with_suffix(env_path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=4) + "\n", encoding="utf-8")
    temporary.replace(env_path)
    return record


def assert_staged_idf_target(idf_root: Path, espressif_root: Path, target: str) -> dict[str, object]:
    """Fail closed before activation unless the exact staged repo target is registered."""
    idf_path = canonical_idf_registry_path(idf_root)
    idf_io_path = Path(os.path.abspath(str(idf_root)))
    version = _idf_repo_version(idf_io_path)
    if version != IDF_VERSION or target != IDF_TARGET:
        raise RuntimeError(f"Unexpected ESP-IDF activation request: version={version}, target={target}")
    env_path = Path(os.path.abspath(str(espressif_root))) / "idf-env.json"
    try:
        payload = json.loads(env_path.read_text(encoding="utf-8"))
        record = payload["idfInstalled"][f"{idf_path}-v{version}"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise RuntimeError(f"Staged ESP-IDF {idf_path} has no valid registration for {target}: {env_path}") from exc
    expected = {
        "version": version,
        "path": str(idf_path),
        "features": ["core"],
        "targets": [target],
    }
    if record != expected:
        raise RuntimeError(f"Staged ESP-IDF registration mismatch for {target}: expected {expected}, got {record}")
    return record


def validate_managed_component_lock(lock_path: Path, managed_root: Path) -> list[str]:
    """Require staged managed component directories to exactly match tracked lock entries."""
    if not lock_path.is_file():
        raise RuntimeError(f"Tracked component lock is missing: {lock_path}")
    lock_text = lock_path.read_text(encoding="utf-8")
    locked = sorted({f"{match.group(1)}__{match.group(2)}"
                     for match in re.finditer(r"(?m)^  ([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+):\s*$", lock_text)})
    if not locked:
        raise RuntimeError(f"Tracked component lock contains no registry component entries: {lock_path}")
    actual = sorted(item.name for item in managed_root.iterdir() if item.is_dir()) if managed_root.is_dir() else []
    if actual != locked:
        raise RuntimeError(
            "Managed component source set is not covered by the tracked lock; "
            f"locked={locked}, staged_source={actual}. Refusing to reuse generated component metadata."
        )
    return locked


def stage_sdk(
    sdk_source: Path,
    stage_root: Path,
    *,
    dependency_lock: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    """Fail closed until tracked SDK source and toolchain manifests exist.

    The local TuyaOpen checkout contains generated component, IDF, and tool
    installation trees. Copying or hashing those trees is not an approved
    source of build inputs, so this runner deliberately refuses to stage them.
    """
    if repo_root is None:
        raise RuntimeError(
            "BLOCKED: tracked SDK provenance manifest is missing; refusing all shared SDK access."
        )
    require_tracked_sdk_provenance(repo_root)
    raise RuntimeError("BLOCKED: no approved SDK source allowlist; refusing shared SDK access.")

def install_reviewed_sdkconfig_defaults(firmware_root: Path, staged_project: Path) -> Path:
    """Seed a fresh stage config from the reviewed tracked project defaults only."""
    defaults = firmware_root / "sdkconfig.defaults"
    if not defaults.is_file():
        raise RuntimeError(f"Reviewed firmware sdkconfig.defaults is missing: {defaults}")
    text = defaults.read_text(encoding="utf-8")
    required = {
        'CONFIG_IDF_TARGET="esp32s3"',
        "CONFIG_PARTITION_TABLE_CUSTOM=y",
        'CONFIG_ESPTOOLPY_FLASHSIZE="32MB"',
    }
    missing = sorted(item for item in required if item not in text.splitlines())
    if missing:
        raise RuntimeError("Reviewed defaults do not define required target/build settings: " + ", ".join(missing))
    staged_project.mkdir(parents=True, exist_ok=True)
    destination = staged_project / "sdkconfig.defaults"
    if destination.exists():
        raise FileExistsError(f"Refusing to reuse staged config defaults: {destination}")
    shutil.copy2(defaults, destination)
    return destination


def assert_non_sensitive_sdkconfig(config_path: Path) -> str:
    """Reject config entries that look credential-bearing before hashing/logging."""
    text = config_path.read_text(encoding="utf-8", errors="strict")
    required = {
        'CONFIG_IDF_TARGET="esp32s3"',
        "CONFIG_PARTITION_TABLE_CUSTOM=y",
        'CONFIG_ESPTOOLPY_FLASHSIZE="32MB"',
    }
    missing = sorted(item for item in required if item not in text.splitlines())
    if missing:
        raise RuntimeError("Generated sdkconfig is missing required build settings: " + ", ".join(missing))
    forbidden = re.compile(r"(?i)(?:PASSWORD|PASSWD|SECRET|TOKEN|API[_-]?KEY|PRIVATE[_-]?KEY)")
    matches = [line.split("=", 1)[0] for line in text.splitlines()
               if line.startswith("CONFIG_") and forbidden.search(line.split("=", 1)[0])]
    if matches:
        raise RuntimeError("Generated sdkconfig contains sensitive-looking symbols; refusing to log/hash it: "
                           + ", ".join(sorted(set(matches))))
    return hashlib.sha256(text.encode("utf-8")).hexdigest().upper()


def git_output(repo: Path, *args: str) -> bytes:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE).stdout


def require_tracked_sdk_provenance(repo: Path) -> None:
    """Refuse SDK access unless a tracked, reviewed SDK input manifest exists.

    No allowlist is currently approved. This gate therefore blocks before any
    SDK-root filesystem or process operation, even if a manifest is later added.
    """
    relative = SDK_PROVENANCE_MANIFEST.as_posix()
    tracked = subprocess.run(
        ["git", "-C", str(repo), "ls-files", "--error-unmatch", "--", relative],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if tracked.returncode != 0:
        raise RuntimeError(
            f"BLOCKED: tracked SDK provenance manifest is missing: {relative}; "
            "refusing all shared SDK and toolchain access."
        )
    manifest = repo / SDK_PROVENANCE_MANIFEST
    try:
        manifest.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(
            f"BLOCKED: tracked SDK provenance manifest cannot be read: {relative}; "
            "refusing all shared SDK and toolchain access."
        ) from exc
    raise RuntimeError(
        f"BLOCKED: SDK provenance manifest {relative} has no approved allowlist parser; "
        "refusing all shared SDK and toolchain access."
    )


def source_provenance(repo: Path) -> dict[str, object]:
    tracked_diff = git_output(repo, "diff", "--binary", "HEAD", "--", "firmware")
    staged_diff = git_output(repo, "diff", "--binary", "--cached", "HEAD", "--", "firmware")
    return {
        "source_head": git_output(repo, "rev-parse", "HEAD").decode().strip(),
        "firmware_worktree_diff_sha256": hashlib.sha256(tracked_diff).hexdigest().upper(),
        "firmware_index_diff_sha256": hashlib.sha256(staged_diff).hexdigest().upper(),
        "firmware_status": git_output(repo, "status", "--short", "--", "firmware").decode(errors="replace"),
    }


def find_idf_python(sdk_root: Path) -> tuple[Path, Path]:
    env_root = sdk_root / "platform" / "ESP32" / ".espressif" / "python_env"
    candidates = sorted(env_root.glob(f"idf{IDF_VERSION}_py*_env"),
                        key=lambda item: ("py3.12" not in item.name, item.name))
    for candidate in candidates:
        python = candidate / "Scripts" / "python.exe"
        version = candidate / "idf_version.txt"
        if not python.is_file() or not version.is_file() or version.read_text().strip() != IDF_VERSION:
            continue
        check = subprocess.run([str(python), "-c", "import rich"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if check.returncode == 0:
            return python, candidate / "Scripts"
    raise RuntimeError(f"No existing ESP-IDF {IDF_VERSION} Python env with rich under {env_root}; no install attempted.")


def staged_idf_tool_paths(sdk_root: Path, staged_sdk: Path, python: Path, scripts: Path) -> tuple[Path, Path]:
    """Map SDK IDF tool paths to the copied staging .espressif tree."""
    source_tools_root = sdk_root.resolve() / "platform" / "ESP32" / ".espressif"
    staged_tools_root = staged_sdk / "platform" / "ESP32" / ".espressif"
    try:
        staged_python = staged_tools_root / python.relative_to(source_tools_root)
        staged_scripts = staged_tools_root / scripts.relative_to(source_tools_root)
    except ValueError as exc:
        raise RuntimeError(f"IDF Python paths are outside the SDK .espressif tree: {exc}") from exc
    if not staged_python.is_file() or not staged_scripts.is_dir():
        raise RuntimeError(f"Staged IDF Python paths are unavailable: {staged_python}, {staged_scripts}")
    return staged_tools_root, staged_scripts


def gitpython_site_packages(sdk_root: Path) -> Path:
    """Locate existing SDK GitPython packages without changing any environment."""
    site_packages = sdk_root.resolve() / ".venv" / "Lib" / "site-packages"
    if not (site_packages / "git" / "__init__.py").is_file() or not (site_packages / "gitdb" / "__init__.py").is_file():
        raise RuntimeError(f"GitPython and GitDB are not available in SDK venv site-packages: {site_packages}")
    return site_packages.resolve()


def idf_export_prefix(idf_tools_root: Path, python_scripts: Path, export_bat: Path) -> str:
    """Build IDF activation commands without injecting third-party PYTHONPATH."""
    return (
        f'set "IDF_TOOLS_PATH={idf_tools_root}" && '
        f'set "PYTHONDONTWRITEBYTECODE=1" && '
        f'set "PATH={python_scripts};%PATH%" && '
        f'call "{export_bat}" && where idf.py'
    )


def configure_staged_partition_config(
    config_path: Path,
    csv_path: Path,
    *,
    repo: Path,
    authorization: dict[str, object] | None = None,
) -> None:
    """Keep partition mutation disabled until provenance and a safe design exist."""
    require_tracked_sdk_provenance(repo)
    raise RuntimeError(
        "BLOCKED: staged partition-config mutation is disabled pending A0-approved SDK provenance "
        "and a separately reviewed design."
    )


def _run_mounted_build(
    repo: Path,
    sdk_root: Path,
    archive: Path,
    mount_root: Path,
    python: Path,
    idf_python_scripts: Path,
) -> int:
    initial = source_provenance(repo)
    paths = archive_stage_paths(mount_root)
    stage = paths["stage"]
    staged_sdk = paths["sdk"]
    staged_app = paths["app"]
    stage.mkdir()
    _copy_firmware(repo / "firmware", staged_app)
    stage_sdk(sdk_root, staged_sdk, dependency_lock=repo / "firmware" / "dependencies.lock")
    idf_tools_root, idf_python_scripts = staged_idf_tool_paths(
        sdk_root, staged_sdk, python, idf_python_scripts
    )
    staged_target_registration = assert_staged_idf_target(
        staged_sdk / "platform" / "ESP32" / "esp-idf",
        staged_sdk / "platform" / "ESP32" / ".espressif",
        IDF_TARGET,
    )

    # The old builder writes generated target config into this project's
    # sdkconfig. Do so only in the staged project and bind the repository CSV
    # copied into the staged app.
    staged_project = staged_sdk / "platform" / "ESP32" / "tuya_open_sdk"
    defaults_path = install_reviewed_sdkconfig_defaults(repo / "firmware", staged_project)
    native_config = staged_project / "sdkconfig"
    (staged_project / "partitions.csv").write_bytes((staged_app / "partitions.csv").read_bytes())

    bridge = repo / "tools" / "verification" / "run_isolated_tuyaopen_bridge.py"
    idf_export = staged_sdk / "platform" / "ESP32" / "esp-idf" / "export.bat"
    sdkconfig_provenance = {
        "sdkconfig_path": str(native_config),
        "sdkconfig_defaults_path": str(defaults_path),
        "sdkconfig_defaults_sha256": sha256_file(defaults_path),
        "staged_sdkconfig_sha256": None,
        "partition_csv": str(staged_app / "partitions.csv"),
        "external_sdk_writes": "none intended; project/build/config state is staged under this archive",
    }
    (archive / "sdkconfig-provenance.json").write_text(
        json.dumps(sdkconfig_provenance, indent=2) + "\n", encoding="utf-8")

    wrapper = paths["archive"] / "run-isolated-bridge-mounted.py"
    wrapper.write_text(mounted_bridge_wrapper_source(), encoding="utf-8")
    bridge_command = mounted_bridge_command(python, wrapper, bridge, mount_root, repo, sdk_root)
    command = (
        f'{idf_export_prefix(idf_tools_root, idf_python_scripts, idf_export)} && '
        f'idf.py -C "{staged_project}" -B "{paths["archive"] / "build"}" set-target esp32s3 && '
        f'idf.py -C "{staged_project}" -B "{paths["archive"] / "build"}" reconfigure && '
        + subprocess.list2cmdline(bridge_command)
    )
    result = subprocess.run(command, shell=True, cwd=repo,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    log = result.stdout.decode("utf-8", errors="replace")
    if native_config.is_file():
        config_digest = assert_non_sensitive_sdkconfig(native_config)
        sdkconfig_provenance["staged_sdkconfig_sha256"] = config_digest
        sdkconfig_provenance["sdkconfig_generated_from_fresh_stage_defaults"] = True
        (archive / "sdkconfig-provenance.json").write_text(
            json.dumps(sdkconfig_provenance, indent=2) + "\n", encoding="utf-8")
    elif result.returncode == 0:
        raise RuntimeError("IDF completed but did not produce a fresh staged sdkconfig; refusing build evidence.")
    (archive / "build-console.log").write_text(
        f"COMMAND: {command}\nEXIT_CODE: {result.returncode}\n\n{log}", encoding="utf-8")

    final = source_provenance(repo)
    staged_sdk_manifest_path = stage / "staged-sdk-inputs.json"
    staged_sdk_manifest = json.loads(staged_sdk_manifest_path.read_text(encoding="utf-8"))
    provenance = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "source_before": initial,
        "source_after": final,
        "source_unchanged": initial == final,
        "staged_sdk": str(staged_sdk),
        "staged_sdk_inputs_manifest": str(staged_sdk_manifest_path),
        "staged_sdk_inputs_aggregate_sha256": staged_sdk_manifest["aggregate_sha256"],
        "idf_target_registration": {
            "repo_id": f"{staged_target_registration['path']}-v{staged_target_registration['version']}",
            **staged_target_registration,
        },
        "staged_app": str(staged_app),
        "project_build_outputs_staged_under_archive": True,
    }
    (archive / "checkout-provenance.json").write_text(
        json.dumps(provenance, indent=2) + "\n", encoding="utf-8")
    print(f"archive={archive}")
    print(f"build_exit_code={result.returncode}")
    print(f"source_unchanged={provenance['source_unchanged']}")
    return result.returncode


def run_build(repo: Path, sdk_root: Path, archive: Path) -> int:
    # First build operation: do not resolve, glob, stat, import from, or invoke
    # tools from sdk_root before the tracked, repo-local provenance gate.
    require_tracked_sdk_provenance(repo)
    archive_base = (repo / "firmware" / ".target_logging").resolve()
    archive = archive.resolve()
    if not is_within(archive, archive_base) or archive == archive_base:
        raise ValueError("Archive path must be a unique child of firmware/.target_logging.")
    if archive.exists():
        raise FileExistsError(f"Refusing to reuse existing archive: {archive}")
    free_bytes = shutil.disk_usage(archive_base).free
    if free_bytes < 8 * 1024 * 1024 * 1024:
        raise RuntimeError(f"Need at least 8 GiB free for isolated target artifacts; found {free_bytes} bytes.")
    python, idf_python_scripts = find_idf_python(sdk_root)
    gitpython_site_packages(sdk_root)
    archive.mkdir(parents=True)
    mount_evidence: dict[str, object] = {
        "archive": str(archive),
        "archive_mount": None,
        "cleanup_verified": False,
    }
    try:
        with temporary_archive_mount(archive) as mount_root:
            mount_evidence["archive_mount"] = str(mount_root)
            mount_evidence["mount_target"] = str(archive)
            result = _run_mounted_build(repo, sdk_root, archive, mount_root, python, idf_python_scripts)
        mount_evidence["cleanup_verified"] = True
        return result
    except BaseException as exc:
        mount_evidence["error"] = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        (archive / "archive-mount.json").write_text(
            json.dumps(mount_evidence, indent=2) + "\n", encoding="utf-8")


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--sdk", type=Path, default=Path.home() / "TuyaOpenIDE" / "TuyaOpenSDK")
    parser.add_argument("--archive", type=Path)
    args = parser.parse_args(argv)
    if args.archive is None:
        parser.error("--archive is required for a firmware build")
    return args


def main() -> int:
    args = parse_arguments()
    try:
        require_tracked_sdk_provenance(args.repo)
        return run_build(args.repo.resolve(), args.sdk, args.archive)
    except Exception as exc:
        print(f"isolated firmware build refused/failed before completion: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
