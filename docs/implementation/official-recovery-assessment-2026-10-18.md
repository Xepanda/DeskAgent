# Official firmware recovery assessment

User requested inspection of the official firmware before continuing DeskAgent
development. Tail initialization and further DeskAgent flashing are paused.
The authorized tail erase has NOT been executed.

## Independently checked

- Official repository: `orulink-ai/WatcheRobot-public`, inspected main
  `e89a5fd3ae9fa9115e5122e252ab90fe5dd0b72b`.
- The original local backup application descriptor identifies `WatcheRobot-S3`,
  `v0.4.4`, ESP-IDF `v6.0.2`.
- That backup is 2,293,760 bytes and is truncated: esptool cannot read a complete
  declared segment. It is not a validated restorable application.
- Downloaded official `WatcheRobot-S3-v0.4.4-PTL-paired.zip` from release
  `watche-v0.1.2`. SHA256 matches the published SHA256SUMS:
  `e6763e11c12f86b05f0633b43edebda36494402935aaa856f32a41519aac2d11`.
- Official complete application is 4,410,160 bytes, fits ota_0's 6 MiB slot at
  `0x20000`, and passes esptool checksum and validation-hash checks.
- Application SHA256:
  `91205c2b3aa447921745f29b86242fcbcf2fbf0e3c8be17a5917511b78ce76f0`.
- Official partition table equals the corresponding prefix of the original
  device partition-table backup. The current device table was also read this
  session and still contains the same ota_0 layout.
- The official v0.4.4 image and original backup have different ELF hashes.
  This is the same named version, not proven identical original build.
- Official v0.4.4 notes allow an ESP32 application-only update for an already
  paired PTL head. Full factory installation overwrites additional resources;
  it must not be silently substituted for preserving the existing device data.

## Proposed recovery scope

First restore the official v0.4.4 ESP32 application at `0x20000` while retaining
the existing partition table, NVS, agentcfg, model/storage, Himax, STM32 and SD
resources. Verify actual boot, screen, input, audio, camera and body connectivity
before calling the robot restored. Confirm actual Himax/body compatibility from
runtime evidence; matching version labels are not whole-device acceptance.

No official recovery image has been flashed during this assessment. Current
device firmware remains the first DeskAgent integration build tested earlier.
Official v0.5.3 paired assets are also available; latest OTA assets are not a
replacement for a complete first-install package. Upgrading versions is a
separate choice from restoring the prior named v0.4.4 baseline.

## Recovery execution — 2026-10-19

The user subsequently explicitly authorized restoring official firmware.
The earlier assessment-only statement above is superseded by this execution.

- Verified all 21 files in the downloaded v0.4.4 package against its provenance
  manifest before opening the device.
- Saved the exact currently flashed DeskAgent image, the current bootloader,
  and raw partition/configuration ranges to the private local backup directory
  `C:\Users\maida\.codex\private\deskagent\rollback-20261018-235715`.
  These backups may contain credentials and must not be committed or printed.
- Read the live partition table: all partition addresses, sizes and flags match
  the official package, but errlog/crasharc/stmota subtypes had been changed by
  previous integration work. Restored the official table to avoid mixing layouts.
- Wrote only COM21 / ESP32-S3: official bootloader at `0x0` (22,496 bytes),
  official table at `0x8000` (3,072 bytes), and official application at
  `0x20000` (4,410,160 bytes). Esptool reported `Hash of data verified` for
  each of the three images and exited successfully.
- Did not write NVS, agentcfg, model/storage, OTA selection data, Himax, STM32,
  SD contents or the proposed Tuya tail reservation. The tail initialization
  authorized earlier was not executed after the user changed direction.
- Real boot logs identify `WatcheRobot-S3` / `v0.4.4`, load ota_0 at `0x20000`,
  report knob_ready=1 and touch_ready=1, mount the SD card successfully at
  10 MHz after a conservative fallback, decode the first boot-animation frame,
  then reach `launcher_ready` and `Main control loop ready`.
- Runtime notes: the existing official SD resource marker is absent/stale;
  full resource verification is deferred by the application. RF calibration
  falls back to a full calibration. Neither is treated as whole-device failure
  or silently claimed resolved.
- Collected 100 seconds of real boot/runtime logs without an observed panic,
  assertion or repeated reboot.
- User physically confirmed: the previous interface is restored and screen/input
  operation is normal. Camera, microphone/speaker and body motion have not been
  individually reaccepted.

This verifies official image flashing and boot recovery. It does not establish
complete peripheral, networking or original-build byte-for-byte restoration.
