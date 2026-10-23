#!/usr/bin/env python3
"""Run the TuyaOpen target build and archive only verified artifacts."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SDK_PROVENANCE_MANIFEST = "docs/acceptance/cr035-sdk-inputs.json"


def require_runner_authorization(repo_root: Path) -> None:
    """Block legacy entrypoints before they can inspect shared SDK state."""
    tracked = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--error-unmatch", "--", SDK_PROVENANCE_MANIFEST],
        check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if tracked.returncode != 0:
        raise RuntimeError(
            f"BLOCKED: tracked SDK provenance manifest is missing: {SDK_PROVENANCE_MANIFEST}; "
            "legacy TuyaOpen builds are disabled before SDK access."
        )
    raise RuntimeError(
        "BLOCKED: no reviewed runner authorization token format exists; "
        "legacy TuyaOpen builds remain disabled."
    )


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest().upper()


def capture_firmware_inputs(firmware: Path, project_root: Path, sdk_root: Path) -> dict[str, object]:
    """Hash tracked and non-ignored untracked firmware inputs plus build entrypoints."""
    repo_root = firmware.parent
    require_runner_authorization(repo_root)
    listed = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "--cached", "--others", "--exclude-standard", "-z", "--", "firmware"],
        check=False,
        capture_output=True,
    )
    if listed.returncode:
        raise RuntimeError("Could not enumerate tracked and untracked firmware inputs.")

    excluded_prefixes = ("firmware/.build/", "firmware/dist/", "firmware/.target_logging/")
    input_paths = {
        item.decode("utf-8", errors="surrogateescape").replace("\\", "/")
        for item in listed.stdout.split(b"\0") if item
    }
    input_paths = {item for item in input_paths if not item.startswith(excluded_prefixes)}
    # These generated build parameters affect the SDK include/source selection
    # even though the project build cache is intentionally excluded wholesale.
    input_paths.add("firmware/.build/build/build_param.json")
    input_paths.update(("tools/build_watcher_firmware.ps1", "tools/build_watcher_tuyaopen.py"))

    records: list[dict[str, str]] = []
    for relative in sorted(input_paths):
        path = repo_root / Path(relative)
        if not path.is_file():
            raise RuntimeError(f"Firmware/build input disappeared before snapshot: {relative}")
        records.append({"path": relative, "sha256": sha256(path)})
    aggregate = hashlib.sha256()
    for record in records:
        aggregate.update(record["path"].encode("utf-8", errors="surrogateescape"))
        aggregate.update(b"\0")
        aggregate.update(record["sha256"].encode("ascii"))
        aggregate.update(b"\n")

    commit = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"], check=False, capture_output=True, text=True,
    )
    diff = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--binary", "HEAD", "--", "firmware", "tools/build_watcher_firmware.ps1", "tools/build_watcher_tuyaopen.py"],
        check=False,
        capture_output=True,
    )
    if diff.returncode:
        raise RuntimeError("Could not capture scoped firmware/build-script diff snapshot.")
    sdk_head = subprocess.run(
        ["git", "-C", str(sdk_root), "rev-parse", "HEAD"], check=False, capture_output=True, text=True,
    )
    sdk_tracked_status = subprocess.run(
        ["git", "-C", str(sdk_root), "status", "--short", "--untracked-files=no"],
        check=False, capture_output=True, text=True,
    )
    if sdk_head.returncode or sdk_tracked_status.returncode:
        raise RuntimeError("Could not read external TuyaOpen SDK revision and tracked dirty state.")
    sdk_status = sdk_tracked_status.stdout
    sdk_state = {
        "head": sdk_head.stdout.strip(),
        "tracked_dirty": bool(sdk_status.strip()),
        "tracked_status_sha256": hashlib.sha256(sdk_status.encode("utf-8")).hexdigest().upper(),
    }
    return {
        "source_commit": commit.stdout.strip() if commit.returncode == 0 else None,
        "aggregate_sha256": aggregate.hexdigest().upper(),
        "diff_sha256": hashlib.sha256(diff.stdout).hexdigest().upper(),
        "inputs": records,
        "diff_bytes": diff.stdout,
        "external_sdk_state": sdk_state,
    }


def main() -> int:
    # This public legacy CLI is not a runner-authorized build entrypoint.
    # Check only the tracked repository manifest before touching argv paths.
    require_runner_authorization(Path(__file__).resolve().parents[1])
    if len(sys.argv) not in (4, 5) or (len(sys.argv) == 5 and sys.argv[4] != "--partition-only"):
        print("usage: build_watcher_tuyaopen.py SDK_ROOT APP_ROOT ARCHIVE_ROOT [--partition-only]", file=sys.stderr)
        return 2
    sdk_root = Path(sys.argv[1]).resolve()
    app_root = Path(os.path.abspath(sys.argv[2]))
    archive = Path(sys.argv[3]).resolve()
    # app_root is source/embedded (a junction); resolve firmware from the real
    # checkout path so the manifest always identifies the repository source.
    firmware = Path(os.path.realpath(app_root))
    csv_path = firmware / "partitions.csv"
    idf_root = sdk_root / "platform" / "ESP32" / "tuya_open_sdk"
    idf_build = idf_root / "build"
    sdkconfig = idf_root / "sdkconfig"
    idf_source = sdk_root / "platform" / "ESP32" / "esp-idf"
    genpart = idf_source / "components" / "partition_table" / "gen_esp32part.py"
    python = Path(sys.executable)
    if not csv_path.is_file() or not sdkconfig.is_file() or not genpart.is_file():
        print("Required firmware CSV, generated sdkconfig, or ESP-IDF gen_esp32part.py is missing.", file=sys.stderr)
        return 2
    # The PowerShell entry point pre-creates this isolated run directory so it
    # can retain complete build logs and machine-state restoration evidence,
    # even when setup fails before this bridge starts.
    archive.mkdir(parents=True, exist_ok=True)
    project_root = firmware.parent
    source_start = capture_firmware_inputs(firmware, project_root, sdk_root)
    source_start_confirm = capture_firmware_inputs(firmware, project_root, sdk_root)
    if (source_start_confirm["aggregate_sha256"] != source_start["aggregate_sha256"]
            or source_start_confirm["diff_sha256"] != source_start["diff_sha256"]
            or source_start_confirm["external_sdk_state"] != source_start["external_sdk_state"]):
        raise RuntimeError("Firmware/build inputs changed while the initial provenance snapshot was being captured.")
    source_start = source_start_confirm
    sdkconfig_provenance_path = archive / "sdkconfig-provenance.json"
    if not sdkconfig_provenance_path.is_file():
        raise RuntimeError("PowerShell entrypoint did not archive the external SDK sdkconfig override provenance.")
    sdkconfig_provenance = json.loads(sdkconfig_provenance_path.read_text(encoding="utf-8-sig"))
    source_start_manifest = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_start["source_commit"],
        "input_set_sha256": source_start["aggregate_sha256"],
        "scoped_diff_sha256": source_start["diff_sha256"],
        "external_tuyaopen_sdk": source_start["external_sdk_state"],
        "external_sdkconfig": sdkconfig_provenance,
        "scope": "tracked and non-ignored untracked firmware files, excluding generated build/dist outputs, plus the two local build scripts",
        "inputs": source_start["inputs"],
    }
    source_start_bytes = (json.dumps(source_start_manifest, indent=2) + "\n").encode("utf-8")
    source_manifest_sha = hashlib.sha256(source_start_bytes).hexdigest().upper()
    (archive / "source-inputs-start.json").write_bytes(source_start_bytes)
    (archive / "source-diff-snapshot.patch").write_bytes(source_start["diff_bytes"])
    expected_csv = csv_path.read_bytes()
    expected_csv_sha = sha256(csv_path)
    csv_record = next((item for item in source_start["inputs"] if item["path"] == "firmware/partitions.csv"), None)
    if csv_record is None or csv_record["sha256"] != expected_csv_sha:
        raise RuntimeError("Partition CSV differs from the frozen firmware input snapshot.")
    expected_bin = archive / "expected-partition-table.bin"
    result = subprocess.run(
        [str(python), str(genpart), "--offset", "0x8000", "--flash-size", "32MB", str(csv_path), str(expected_bin)],
        cwd=str(idf_root), check=False,
    )
    if result.returncode:
        print("ESP-IDF partition CSV generation failed.", file=sys.stderr)
        return result.returncode
    expected_layout = subprocess.run(
        [str(python), str(genpart), str(expected_bin)], check=False,
        capture_output=True, text=True, encoding="utf-8",
    )
    if expected_layout.returncode:
        print("Could not decode the CSV-derived partition binary.", file=sys.stderr)
        return expected_layout.returncode
    required_layout = (
        "ota_0,app,ota_0,0x20000,6M,",
        "ota_1,app,ota_1,0x620000,6M,",
        "agentcfg,data,nvs,0x12000,56K,",
        "model,data,spiffs,0xc20000,1M,",
        "storage,data,spiffs,0xd20000,4M,",
    )
    if any(item not in expected_layout.stdout for item in required_layout):
        print("Repository CSV-derived partition layout does not match the required Watcher layout.", file=sys.stderr)
        return 1

    # Generate the real TuyaOpen ESP-IDF partition target before compiling
    # application sources. This both proves the resolved Kconfig input and
    # prevents an unrelated app compile error from hiding a stale table.
    export_bat = sdk_root / "platform" / "ESP32" / "esp-idf" / "export.bat"
    command = f"call {export_bat} && cd /d {idf_build} && ninja partition_table/partition-table.bin"
    partition_build = subprocess.run(["cmd.exe", "/c", command], check=False)
    if partition_build.returncode:
        print("ESP-IDF partition table target failed.", file=sys.stderr)
        return partition_build.returncode
    built_partition = idf_build / "partition_table" / "partition-table.bin"
    if not built_partition.is_file() or built_partition.read_bytes() != expected_bin.read_bytes():
        print("ESP-IDF generated partition table does not match firmware/partitions.csv.", file=sys.stderr)
        return 1
    actual_layout = subprocess.run(
        [str(python), str(genpart), str(built_partition)], check=False,
        capture_output=True, text=True, encoding="utf-8",
    )
    if actual_layout.returncode or actual_layout.stdout != expected_layout.stdout:
        print("Decoded ESP-IDF partition layout differs from the CSV-derived layout.", file=sys.stderr)
        return 1
    shutil.copy2(built_partition, archive / "partition-table.bin")
    (archive / "partition-layout.txt").write_text(actual_layout.stdout, encoding="utf-8")
    partition_manifest = {
        "source_commit": source_start["source_commit"],
        "source_dirty_diff_sha256": source_start["diff_sha256"],
        "source_input_set_sha256": source_start["aggregate_sha256"],
        "source_input_manifest_sha256": source_manifest_sha,
        "external_tuyaopen_sdk": source_start["external_sdk_state"],
        "build_script_sha256": sha256(Path(__file__).resolve()),
        "partition_csv_sha256": expected_csv_sha,
        "partition_table_expected_sha256": sha256(expected_bin),
        "partition_table_actual_sha256": sha256(built_partition),
        "partition_csv": str(csv_path),
        "validation": "PASS: IDF partition_table target output matches repository CSV-derived binary",
    }
    (archive / "partition-validation.json").write_text(json.dumps(partition_manifest, indent=2) + "\n", encoding="utf-8")
    if len(sys.argv) == 5:
        print(json.dumps(partition_manifest, indent=2))
        return 0

    # Read Kconfig's resolved values to ensure IDF is configured with the
    # repository input, rather than a same-named SDK-local CSV.
    config_text = sdkconfig.read_text(encoding="utf-8", errors="replace")
    requested = str(csv_path.resolve()).replace("\\", "/")
    for key in ("CONFIG_PARTITION_TABLE_CUSTOM_FILENAME", "CONFIG_PARTITION_TABLE_FILENAME"):
        if f'{key}="{requested}"' not in config_text:
            print(f"Temporary sdkconfig does not point {key} at the repository CSV.", file=sys.stderr)
            return 2

    sys.path.insert(0, str(sdk_root))
    tos_path = sdk_root / "tos.py"
    sys.argv = [str(tos_path), "build"]
    os.chdir(app_root)
    from tools.cli_command import cli_build, cli_config
    from tools.cli_command.util import get_global_params, set_global_params

    set_global_params()
    params = get_global_params()
    if os.path.normcase(os.path.normpath(params["app_root"])) != os.path.normcase(os.path.normpath(str(app_root))):
        print("TuyaOpen app root differs from requested build root.", file=sys.stderr)
        return 2
    params["dist_root"] = str(archive / "tuyaopen-package")
    cli_build.check_proj_dir()
    cli_config.init_using_config(force=False)
    using_data = cli_build.parse_config_file(params["using_config"])
    if using_data.get("CONFIG_PLATFORM_CHOICE") != "ESP32":
        print("Project configuration is not ESP32.", file=sys.stderr)
        return 2

    # The Tuya SDK's tracked platform/ESP32/toolchain_file.cmake hard-codes
    # IDF_TOOLS_PATH_RE to "~/.espressif" whenever IDF_PATH is set. CMake does
    # not expand that tilde, so its subsequent project() rejects the compiler
    # path even though ESP-IDF export.bat has installed the correct SDK-local
    # toolchain on PATH. Inject a project-scoped include after that toolchain
    # file and before project(opensdk), correcting only CMake's compiler vars;
    # do not edit the external SDK checkout.
    tools_root = Path(os.environ.get("IDF_TOOLS_PATH", "")).resolve()
    compiler_bin = (
        tools_root
        / "tools"
        / "xtensa-esp-elf"
        / "esp-14.2.0_20241119"
        / "xtensa-esp-elf"
        / "bin"
    )
    compiler_paths = {
        "CMAKE_C_COMPILER": compiler_bin / "xtensa-esp32-elf-gcc.exe",
        "CMAKE_CXX_COMPILER": compiler_bin / "xtensa-esp32-elf-g++.exe",
        "CMAKE_AR": compiler_bin / "xtensa-esp32-elf-ar.exe",
    }
    missing_compilers = [str(path) for path in compiler_paths.values() if not path.is_file()]
    if missing_compilers:
        print("SDK-local CMake compiler override files are missing: " + ", ".join(missing_compilers), file=sys.stderr)
        return 2

    def cmake_quote(path: Path) -> str:
        return path.resolve().as_posix().replace('"', '\\"')

    compiler_override = archive / "tuya-cmake-compiler-override.cmake"
    override_lines = [
        f'set({name} "{cmake_quote(path)}")'
        for name, path in compiler_paths.items()
    ]
    override_lines.append('message(STATUS "DeskAgent: using SDK-local ESP32 compiler paths")')
    compiler_override.write_text("\n".join(override_lines) + "\n", encoding="utf-8")

    # The app build cache may have been partially rewritten by a failed
    # configure. Preserve its original bytes/mtime and restore it if either
    # configure or compile fails; successful builds keep the resolved cache.
    app_cache = Path(params["app_build_path"]) / "CMakeCache.txt"
    cache_existed = app_cache.is_file()
    cache_bytes = app_cache.read_bytes() if cache_existed else None
    cache_mtime_ns = app_cache.stat().st_mtime_ns if cache_existed else None
    cache_restored: bool | None = None

    def restore_cache_after_failure() -> None:
        nonlocal cache_restored
        if cache_existed:
            app_cache.parent.mkdir(parents=True, exist_ok=True)
            app_cache.write_bytes(cache_bytes)
            os.utime(app_cache, ns=(cache_mtime_ns, cache_mtime_ns))
            cache_restored = app_cache.read_bytes() == cache_bytes and app_cache.stat().st_mtime_ns == cache_mtime_ns
        elif app_cache.exists():
            app_cache.unlink()
            cache_restored = not app_cache.exists()
        else:
            cache_restored = True

    def write_cache_evidence() -> None:
        (archive / "app-cmake-cache-restoration.json").write_text(
            json.dumps(
                {
                    "cache_path": str(app_cache),
                    "cache_existed_before": cache_existed,
                    "cache_sha256_before": hashlib.sha256(cache_bytes).hexdigest().upper() if cache_bytes is not None else None,
                    "cache_sha256_after": sha256(app_cache) if app_cache.is_file() else None,
                    "mtime_restored": bool(cache_restored is True and cache_existed and app_cache.is_file() and app_cache.stat().st_mtime_ns == cache_mtime_ns),
                    "cache_restored_after_failure": cache_restored,
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    def write_build_result(stage: str, success: bool, exit_code: int | None, error: str | None = None) -> None:
        (archive / "build-result.json").write_text(
            json.dumps(
                {
                    "stage": stage,
                    "success": success,
                    "exit_code": exit_code,
                    "error": error,
                },
                indent=2,
            ) + "\n",
            encoding="utf-8",
        )

    def idf_header_env() -> tuple[dict[str, str], Path]:
        # Match platform/ESP32/build_example.py:set_environment() exactly,
        # but apply it here before invoking the same IDF reconfigure action.
        # IDF's CMake project reads TUYAOS_HEADER_DIR only during configure.
        build_param_path = Path(params["build_param_root"]) / "build_param.json"
        if not build_param_path.is_file():
            raise RuntimeError(f"TuyaOpen build parameters were not generated: {build_param_path}")
        build_params = json.loads(build_param_path.read_text(encoding="utf-8"))
        header_dirs = str(build_params.get("OPEN_HEADER_DIR", ""))
        common_include = next(
            (Path(item).resolve() for item in header_dirs.split() if (Path(item) / "tuya_iot_config.h").is_file()),
            None,
        )
        if common_include is None:
            raise RuntimeError("OPEN_HEADER_DIR has no existing tuya_iot_config.h; refusing to synthesize a config header.")
        build_env = os.environ.copy()
        build_env["BUILD_PARAM_DIR"] = str(Path(params["build_param_root"]).resolve())
        build_env["TUYAOS_HEADER_DIR"] = header_dirs
        build_env["TUYAOS_LIBS_DIR"] = str(build_params.get("OPEN_LIBS_DIR", ""))
        build_env["TUYAOS_LIBS"] = str(build_params.get("PLATFORM_NEED_LIBS", ""))
        build_env["TUYAOS_BOARD_PATH"] = str(Path(build_params["OPEN_ROOT"]) / "boards" / "ESP32")
        return build_env, common_include

    def reconfigure_idf(build_env: dict[str, str], common_include: Path) -> tuple[bool, int, str]:
        idf_cli = shutil.which("idf.py")
        if not idf_cli:
            raise RuntimeError("idf.py is not available in the already-exported SDK-local ESP-IDF environment.")
        command = [idf_cli, "reconfigure"]
        print("Reconfiguring ESP-IDF with build_example.py TUYAOS_HEADER_DIR...")
        result = subprocess.run(
            command,
            cwd=str(idf_root),
            env=build_env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        reconfigure_log = archive / "idf-reconfigure.log"
        reconfigure_log.write_text(
            f"COMMAND: {subprocess.list2cmdline(command)}\nEXIT_CODE: {result.returncode}\n\n"
            + result.stdout
            + ("\n--- STDERR ---\n" + result.stderr if result.stderr else ""),
            encoding="utf-8",
        )
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        if result.returncode:
            (archive / "idf-reconfigure-validation.json").write_text(
                json.dumps(
                    {
                        "success": False,
                        "exit_code": result.returncode,
                        "tuyaos_header_dir": build_env["TUYAOS_HEADER_DIR"],
                        "required_header": str(common_include / "tuya_iot_config.h"),
                        "error": "idf.py reconfigure failed",
                    },
                    indent=2,
                ) + "\n",
                encoding="utf-8",
            )
            return False, result.returncode, "idf.py reconfigure failed"

        compile_db = idf_build / "compile_commands.json"
        ninja_file = idf_build / "build.ninja"
        normalized_header_dir = common_include.as_posix().lower()
        adapter_commands: list[str] = []
        if compile_db.is_file():
            commands = json.loads(compile_db.read_text(encoding="utf-8"))
            for entry in commands:
                if "tuyaos_adapter" not in str(entry.get("file", "")).replace("\\", "/").lower():
                    continue
                command_text = str(entry.get("command", ""))
                if not command_text and entry.get("arguments"):
                    command_text = subprocess.list2cmdline(entry["arguments"])
                adapter_commands.append(command_text.replace("\\", "/").lower())
        ninja_text = ninja_file.read_text(encoding="utf-8", errors="replace").replace("\\", "/").replace("$:", ":").lower() if ninja_file.is_file() else ""
        ninja_has_header = normalized_header_dir in ninja_text
        command_has_header = any(normalized_header_dir in command for command in adapter_commands)
        success = bool(adapter_commands and ninja_has_header and command_has_header)
        report = {
            "success": success,
            "exit_code": 0 if success else 1,
            "tuyaos_header_dir": build_env["TUYAOS_HEADER_DIR"],
            "required_header": str(common_include / "tuya_iot_config.h"),
            "adapter_compile_command_count": len(adapter_commands),
            "ninja_file": str(ninja_file),
            "compile_commands_file": str(compile_db),
            "ninja_rule_has_header_dir": bool(ninja_has_header),
            "adapter_compile_command_has_header_dir": bool(command_has_header),
        }
        (archive / "idf-reconfigure-validation.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        if not success:
            return False, 1, "tuyaos_adapter Ninja compile rules do not contain src/common/include"
        return True, 0, ""

    def snapshot_idf_logs() -> dict[Path, tuple[int, int]]:
        log_dir = idf_build / "log"
        if not log_dir.is_dir():
            return {}
        return {
            path: (path.stat().st_mtime_ns, path.stat().st_size)
            for path in log_dir.glob("idf_py_*_output_*") if path.is_file()
        }

    def archive_new_idf_logs(before: dict[Path, tuple[int, int]]) -> list[dict[str, object]]:
        log_dir = idf_build / "log"
        archived: list[dict[str, object]] = []
        if not log_dir.is_dir():
            return archived
        for source in sorted(log_dir.glob("idf_py_*_output_*")):
            if not source.is_file():
                continue
            current = (source.stat().st_mtime_ns, source.stat().st_size)
            if before.get(source) == current:
                continue
            destination = archive / f"idf-fullbuild-{source.name}"
            shutil.copy2(source, destination)
            archived.append({"source": str(source), "archive": str(destination), "bytes": destination.stat().st_size, "sha256": sha256(destination)})
        (archive / "idf-fullbuild-logs.json").write_text(json.dumps(archived, indent=2) + "\n", encoding="utf-8")
        return archived

    original_do_subprocess = cli_build.do_subprocess

    def do_subprocess_with_compiler_override(command, cwd=None):
        if isinstance(command, (list, tuple)) and command and Path(str(command[0])).name.lower() in ("cmake", "cmake.exe"):
            command = list(command) + [f"-DCMAKE_PROJECT_opensdk_INCLUDE_BEFORE={compiler_override.as_posix()}"]
        return original_do_subprocess(command, cwd=cwd)

    cli_build.do_subprocess = do_subprocess_with_compiler_override
    stage = "app_cmake_configure"
    failure_code: int | None = None
    failure_reason: str | None = None
    prior_idf_logs = snapshot_idf_logs()
    try:
        configured = cli_build.cmake_configure(using_data)
        if not configured:
            build_ok = False
            failure_code = 1
            failure_reason = "TuyaOpen app CMake configure failed"
        else:
            stage = "idf_reconfigure"
            build_env, common_include = idf_header_env()
            reconfigured, reconfigure_code, reconfigure_error = reconfigure_idf(build_env, common_include)
            if not reconfigured:
                build_ok = False
                failure_code = reconfigure_code
                failure_reason = reconfigure_error
            else:
                stage = "idf_full_build"
                build_ok = cli_build.ninja_build(params["app_build_path"])
                archive_new_idf_logs(prior_idf_logs)
                if not build_ok:
                    failure_code = 1
                    failure_reason = "TuyaOpen/ESP-IDF full build failed"
    except BaseException:
        archive_new_idf_logs(prior_idf_logs)
        restore_cache_after_failure()
        write_cache_evidence()
        write_build_result(stage, False, 1, "Unhandled exception; see build-console.log and stage logs")
        raise
    finally:
        cli_build.do_subprocess = original_do_subprocess

    if not build_ok:
        restore_cache_after_failure()
        write_cache_evidence()
    if not build_ok:
        write_build_result(stage, False, failure_code or 1, failure_reason or "TuyaOpen target build failed")
        (archive / "build-failure.txt").write_text(
            f"{failure_reason or 'TuyaOpen target build failed'}; verified partition table retained.\n",
            encoding="utf-8",
        )
        print(f"TuyaOpen target build failed during {stage}: {failure_reason or 'unknown error'}", file=sys.stderr)
        return 1

    # Verify that the CMake-resolved IDF input and built binary match the CSV.
    cmake_cfg = idf_build / "config" / "sdkconfig.cmake"
    if not cmake_cfg.is_file() or requested not in cmake_cfg.read_text(encoding="utf-8", errors="replace"):
        print("Generated IDF CMake config does not resolve the project partition CSV.", file=sys.stderr)
        return 1
    built_partition = idf_build / "partition_table" / "partition-table.bin"
    if not built_partition.is_file() or built_partition.read_bytes() != expected_bin.read_bytes():
        print("Generated partition-table.bin differs from the CSV-derived expected binary.", file=sys.stderr)
        return 1
    app_name = using_data.get("CONFIG_PROJECT_NAME", "")
    app_version = using_data.get("CONFIG_PROJECT_VERSION", "")
    app_bin = Path(params["app_bin_path"]) / f"{app_name}_QIO_{app_version}.bin"
    if not app_bin.is_file():
        print(f"Built app image missing: {app_bin}", file=sys.stderr)
        return 1
    shutil.copy2(app_bin, archive / "desk_agent_firmware.bin")
    shutil.copy2(built_partition, archive / "partition-table.bin")
    source_final = capture_firmware_inputs(firmware, project_root, sdk_root)
    source_final_manifest = {
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_final["source_commit"],
        "input_set_sha256": source_final["aggregate_sha256"],
        "scoped_diff_sha256": source_final["diff_sha256"],
        "external_tuyaopen_sdk": source_final["external_sdk_state"],
        "external_sdkconfig": sdkconfig_provenance,
        "inputs": source_final["inputs"],
    }
    (archive / "source-inputs-final.json").write_text(json.dumps(source_final_manifest, indent=2) + "\n", encoding="utf-8")
    provenance_ok = (
        source_final["aggregate_sha256"] == source_start["aggregate_sha256"]
        and source_final["diff_sha256"] == source_start["diff_sha256"]
        and source_final["source_commit"] == source_start["source_commit"]
        and source_final["external_sdk_state"] == source_start["external_sdk_state"]
    )
    manifest = {
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "source_commit": source_start["source_commit"],
        "source_dirty_diff_sha256": source_start["diff_sha256"],
        "source_input_set_sha256": source_start["aggregate_sha256"],
        "source_input_manifest_sha256": source_manifest_sha,
        "source_inputs_unchanged_during_build": provenance_ok,
        "source_input_final_set_sha256": source_final["aggregate_sha256"],
        "source_input_final_diff_sha256": source_final["diff_sha256"],
        "external_tuyaopen_sdk": source_start["external_sdk_state"],
        "build_script_sha256": sha256(Path(__file__).resolve()),
        "partition_csv_sha256": expected_csv_sha,
        "partition_table_expected_sha256": sha256(expected_bin),
        "partition_table_actual_sha256": sha256(built_partition),
        "app_sha256": sha256(archive / "desk_agent_firmware.bin"),
        "compiler_override_sha256": sha256(compiler_override),
        "compiler_override": str(compiler_override),
        "partition_csv": str(csv_path),
        "validation": "PASS: resolved IDF config and generated partition binary match repository CSV; firmware input snapshot unchanged during build" if provenance_ok else "FAIL: firmware/build inputs changed during build; artifact provenance is not verified",
    }
    (archive / "validation.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(manifest, indent=2))
    if not provenance_ok:
        write_cache_evidence()
        write_build_result("source_provenance_validation", False, 1, "Firmware/build inputs changed during build")
        return 1
    write_build_result("idf_full_build", True, 0)
    write_cache_evidence()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
