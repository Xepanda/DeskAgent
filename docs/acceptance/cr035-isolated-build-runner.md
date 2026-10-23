# CR-035 isolated firmware runner status

## Implemented

- The PowerShell entry point now delegates to the archive-only Python runner. It no longer inspects SDK-local generated configuration, build output, markers, or timestamps.
- The bridge source snapshot includes tracked archive inputs only; it does not read or hash the shared `firmware/.build/build/build_param.json`.
- SDK staging fails closed before traversing, copying, or hashing shared TuyaOpen, ESP-IDF, or `.espressif` trees. No tracked provenance allowlist currently identifies safe SDK/toolchain files, so the runner cannot claim a target build is available.
- The dependency lock currently names only `espressif__esp_codec_dev`, while the selected SDK checkout has 15 managed component directories. A0 confirmed the other 14 generated components lack tracked versions/hashes, including the `esp-sr` include dependency. The runner refuses the mismatch before copying dependencies; no lock file was changed.
- A tracked `firmware/sdkconfig.defaults` remains an input candidate, but it is not treated as sufficient proof that a complete target configuration can be safely generated. No generated config, target build, or artifact digest is produced while the SDK provenance gate is blocked.

## Evidence

- TDD RED: `test_sdk_staging_excludes_generated_project_configuration` failed before implementation because the staged `sdkconfig` existed.
- TDD RED: `test_powershell_entrypoint_delegates_without_reading_generated_sdk_state` failed against the legacy wrapper, which referenced and read generated SDK state.
- GREEN: `python -m unittest tools.verification.test_build_isolated_firmware -v` passed 23 tests, including instrumented guards that forbid opening/statting/hashing/copying shared generated config/build inputs and SDK/toolchain cache roots.
- The runner remains BLOCKED before target build and before any shared SDK/toolchain tree is traversed. A0 must supply reviewed, tracked provenance for the exact SDK, IDF, toolchain, and full managed-component set before this gate can be reopened. No target build was run, and no lock file was changed.
- This report does not claim L2 build, device, or end-to-end evidence. Once provenance exists, the target build gate must also prove exact SDK/IDF versions, unique `tal_cli` resolution, and successful ELF/BIN/partition artifacts from a fresh archive.

## Remaining gate

Do not use any existing shared SDK configuration, build output, Python environment, or tool cache as a fallback. If the exact source/toolchain provenance and all required configuration inputs cannot be derived from reviewed tracked sources, stop and retain the BLOCKED result; do not copy a machine cache or invent defaults.
