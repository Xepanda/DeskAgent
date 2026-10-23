# CR-035 isolated firmware runner status

## Implemented

- The PowerShell entry point now delegates to the archive-only Python runner. It no longer inspects SDK-local generated configuration, build output, markers, or timestamps.
- SDK project staging excludes `sdkconfig*` and the SDK build tree. Managed component sources are accepted only when the directory set exactly matches the tracked lock. ESP-IDF environment registration is freshly generated in the archive; only the Python environment, toolchain files, and public IDF constraints are copied into that stage.
- A new staged `sdkconfig.defaults` is copied from tracked `firmware/sdkconfig.defaults`. Target setup must produce the generated config from this file; required target, flash-size, and custom-partition settings are checked before build evidence is recorded.
- The staged config is checked for credential-looking Kconfig symbols before its digest or build log is archived. Any match stops the run.

## Evidence

- TDD RED: `test_sdk_staging_excludes_generated_project_configuration` failed before implementation because the staged `sdkconfig` existed.
- TDD RED: `test_powershell_entrypoint_delegates_without_reading_generated_sdk_state` failed against the legacy wrapper, which referenced and read generated SDK state.
- GREEN: `python -m unittest tools.verification.test_build_isolated_firmware -v` passed 22 tests, including sentinel exclusion, fresh defaults, resolved target/flash validation, sensitive-symbol rejection, fresh IDF registry, lock-set validation, and PowerShell delegation.
- A read-only preflight against the approved `firmware/dependencies.lock` found one locked managed component (`espressif__esp_codec_dev`) while the selected SDK checkout has 15 managed component directories. The runner now rejects this mismatch before copying those components. No target build was run, and no lock file was changed.
- No target build was run in this change. This report does not claim L2 build, device, or end-to-end evidence. The build remains BLOCKED until A0 reconciles the tracked dependency lock with the approved SDK component set. Afterward the target build gate must also prove exact SDK/IDF versions, unique `tal_cli` resolution, and successful ELF/BIN/partition artifacts from a fresh archive.

## Remaining gate

Do not use any existing shared SDK configuration as a fallback. If a clean build requires Kconfig values not derived by `firmware/sdkconfig.defaults` plus the staged project's Kconfig inputs, stop and request a reviewed sanitized input as CR-035 specifies.
