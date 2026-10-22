# ESP32-S3 Network Log Source Gap (A7)

**Status:** A7 evidence note; formal protocol decision requested from A0.

## Current verified boundary

The verification tool `tools/verification/capture_esp32s3_logs.py` currently reads the ESP32-S3 UART only. Its explicit `--capture` mode opens a read-only serial connection, parses arriving lines incrementally, and can emit redacted JSONL diagnostics with `--live-events PATH|stdout`. `--no-raw` processes UART bytes in memory and persists only redacted events and counts. It does not send bytes to the device. The logger tests exercise live event flushing, redaction, reconnect transitions, and no-write capture behavior.

The firmware interface catalog defines TCP 37020 as the device command/response bridge (`RequestEnvelope` / `ResponseEnvelope`) and UDP 37021 as pairing discovery (`docs/implementation/interfaces.md`, links L-02/L-03/L-09). The implementation in `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c` handles framed client requests and application events. Source search found no documented or implemented network log subscription, log stream message type, or ESP logging sink forwarded over these sockets. These existing business/control transports are not a network log source.

Therefore the supported log collection boundary is **UART-only**. No network port, packet fields, firmware endpoint, or interpretation of application responses as logs is inferred here. COM21/COM20 were not opened for this review; no credentials or raw capture data were read.

## A0 Change Request

Please define whether network log access is in scope and, if so, the authoritative source and contract before any A7 network adapter is implemented. The decision should specify the source/transport and connection direction, versioned message framing and event semantics, authentication/pairing and redaction requirements, bounded buffering/backpressure and reconnect behavior, supported firmware versions, and how diagnostics remain read-only. Do not reuse the command bridge as a log stream without an explicit contract.

Until A0 publishes that contract, A7 cannot add a network log client without inventing protocol behavior. This request does not change firmware or public interfaces.

## Evidence and checks

- `tests/e2e/test_esp32s3_log_capture.py`: 10 passed (L2 tool behavior only).
- `git diff --check` is currently blocked by pre-existing trailing whitespace throughout the tracked generated map `firmware/dist/desk_agent_firmware_1.0.0/desk_agent_firmware_1.0.0.map`; that generated artifact was not edited. A line-ending warning also appeared for another agent's dirty camera test.
- No live UART or network hardware validation was performed in this task.
