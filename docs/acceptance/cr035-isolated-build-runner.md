# CR-035 isolated firmware runner status

## Implemented

- The PowerShell entry point now delegates to the archive-only Python runner. It no longer inspects SDK-local generated configuration, build output, markers, or timestamps.
- The bridge source snapshot includes tracked archive inputs only; it does not read or hash the shared `firmware/.build/build/build_param.json`.
- `run_build` now checks for the tracked repo-local SDK provenance manifest as its first build operation. The manifest is absent, so the runner blocks before resolving, globbing, stating, opening, importing from, or launching processes under the shared SDK path.
- `stage_sdk` checks that same provenance gate before any SDK-path access; direct calls without the repository context fail closed. No tracked provenance allowlist currently identifies safe SDK/toolchain files, so the runner cannot claim a target build is available.
- Direct execution of `run_isolated_tuyaopen_bridge.py` is disabled. It has no runner-issued archive provenance token and therefore cannot inspect SDK `.venv` or call the legacy bridge.
- The legacy `build_watcher_tuyaopen.py` CLI and its `capture_firmware_inputs` helper also require the tracked manifest before any SDK/config/build-path access. The manifest is absent and no runner authorization token format is approved, so both entry paths refuse. The isolated bridge no longer exports its old `source_snapshot` helper.
- The dependency lock currently names only `espressif__esp_codec_dev`, while the selected SDK checkout has 15 managed component directories. A0 confirmed the other 14 generated components lack tracked versions/hashes, including the `esp-sr` include dependency. The runner refuses the mismatch before copying dependencies; no lock file was changed.
- A tracked `firmware/sdkconfig.defaults` remains an input candidate, but it is not treated as sufficient proof that a complete target configuration can be safely generated. No generated config, target build, or artifact digest is produced while the SDK provenance gate is blocked.

## Evidence

- TDD RED: `test_sdk_staging_excludes_generated_project_configuration` failed before implementation because the staged `sdkconfig` existed.
- TDD RED: `test_powershell_entrypoint_delegates_without_reading_generated_sdk_state` failed against the legacy wrapper, which referenced and read generated SDK state.
- RED: instrumented tests exposed the legacy CLI resolving the SDK argument, its snapshot helper checking shared `build_param.json`, and the isolated bridge still exporting `source_snapshot`.
- GREEN: `python -m unittest tools.verification.test_build_isolated_firmware -v` passed 25 tests, including runner and legacy entrypoint guards for pathlib open/stat/exists/is_file/is_dir/resolve/glob/rglob/iterdir, `os.stat`/`os.scandir`, subprocesses, hashing, and copy operations under forbidden roots.
- The runner remains BLOCKED before target build and before any shared SDK/toolchain tree is traversed. A0 must supply reviewed, tracked provenance for the exact SDK, IDF, toolchain, and full managed-component set before this gate can be reopened. No target build was run, and no lock file was changed.
- This report does not claim L2 build, device, or end-to-end evidence. Once provenance exists, the target build gate must also prove exact SDK/IDF versions, unique `tal_cli` resolution, and successful ELF/BIN/partition artifacts from a fresh archive.

## Remaining gate

Do not use any existing shared SDK configuration, build output, Python environment, or tool cache as a fallback. If the exact source/toolchain provenance and all required configuration inputs cannot be derived from reviewed tracked sources, stop and retain the BLOCKED result; do not copy a machine cache or invent defaults.
