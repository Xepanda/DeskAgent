# Camera Frame Delivery Contract (CR-004 proposal)

**Status:** Lead/A0 approved implementation interface boundary (CR-004). The owning agent may implement within the paths below.

This contract adds **no wire protocol**. Device media remains the pinned WatcheRobot WSPK binary frame `<4sBBHII`; this file defines only the in-process Host consumer boundary using the existing `CameraViewerService.ingest_robot_frame(frame_bytes)` method.

## Path and ownership

```text
ESP32 A2 WSPK FRAME_IMAGE
  -> pinned, unmodified official WatcheRobot WebSocket Runtime/SDK
  -> A3 official SDK Robot/Camera adapter (`host/desk_agent/connectors/robot/`)
  -> CameraViewerService.ingest_robot_frame(jpeg_bytes) [A6]
  -> GET /api/camera/frame [A6]
```

- A2 only captures on the device and sends the existing complete WSPK image frame on the paired device WebSocket. It does not call or know about Host Python callbacks.
- The pinned official Runtime/SDK is used as-is; do not modify its source or create a second device Runtime.
- A3 owns `host/desk_agent/connectors/robot/`, including the Runtime session, image/capture correlation, command forwarding, bounded receive/reassembly state, and callback into A6.
- A3 may make only the minimal composition-root change in `host/desk_agent/main.py` to initialize/wire this adapter and register its frame callback with the existing `CameraViewerService.ingest_robot_frame` sink. This does not transfer ownership of camera service or web implementation.
- A6 owns camera lease presentation, JPEG validation, one-slot latest-frame storage, freshness and HTTP output. A6 receives only JPEG payload bytes, with the WSPK header removed.
- A3 must not modify `host/desk_agent/camera/` or `host/desk_agent/web/`; A4's HA connector paths also remain outside this work.
- A2's Runtime-to-device binary receive path is not this camera source; its current implementation validates then discards binary media and has no Host-facing callback.
- A4 HA connector ownership and behavior are outside this boundary and remain unchanged.

## Media semantics

- For a request/response photo, accept only `FRAME_IMAGE=3` correlated to the capture ACK `session_id` through WSPK `stream_id`. The currently implemented device sends one unfragmented frame (`flags=0`); the official SDK also recognizes `FLAG_FRAGMENT` with `FLAG_FIRST`, `FLAG_LAST`, stream identity and consecutive sequence. If fragmented images are accepted, A3 must use those existing SDK semantics, reject gaps/incomplete assemblies, and cap the assembled JPEG at 128 KiB.
- Per complete WSPK binary message, enforce `DESK_WSPK_MAX_FRAME_SIZE=131088` bytes (128 KiB payload plus 16-byte header), consistent with CR-002. This is independent of the 65536-byte TCP newline-JSON limit.
- The current firmware camera handler allocates at most 65536 bytes for a JPEG. Thus its current effective image limit is 64 KiB even though the WSPK and Host camera service support larger payloads. Do not claim larger camera capture support until the firmware owner changes and validates that source limit.
- Before calling `ingest_robot_frame`, A3 must have a complete JPEG payload and ensure source type is image. A6 repeats JPEG marker and 4..128 KiB size validation and marks it as hardware-originated.

## Lease, cadence and backpressure

- Existing Host `resource.camera.acquire/release` and device `ctrl.camera.acquire/release` commands remain authoritative. A3 forwards them over the official SDK session with the same Host lease owner and duration; it starts capture only after device acquire succeeds.
- The SDK `camera.capture()` operation correlates `session_id` and returns the image. While a Host lease remains active, A3 may refresh at the requested `target_fps` (bounded by the existing Host resource contract); allow one device capture in flight. Do not enqueue an unbounded set of requests or images.
- A6 stores one latest JPEG. It does not store frames when there is no active lease. It reports no frame after the existing 2000 ms freshness threshold; it never replays a stale image as current.
- Host release or expiry stops scheduled capture, sends device release if connected, and clears A6's latest JPEG. Device/Runtime disconnect clears pending waiters and Host frame state; after reconnect a new lease/capture is required.

## Failure behavior

- Busy lease: return the existing `RESOURCE_BUSY` error and roll back the Host-side lease.
- Device `NO_ACTIVE_LEASE`: report capture failure; do not retry indefinitely or expose a previous JPEG as success.
- Capture timeout/no binary result: return unavailable/timeout using the existing Host request error behavior.
- Oversized or malformed WebSocket/WSPK/JPEG, mismatched session ID, invalid fragment ordering, or incomplete assembly: drop the frame with bounded diagnostic logging. Keep the previous image only until it naturally becomes stale; then return unavailable.
- Disconnect/restart: clear pending capture state, assembly buffers and latest frame; do not pair a new connection with old stream/session IDs.

## Verification boundary

Mocks and constructed JPEG fixtures validate only Level 2. Passing the end-to-end gate requires the unmodified pinned Runtime/SDK and target firmware to produce a real, decodable JPEG from the physical camera, deliver it to `ingest_robot_frame`, return it through `/api/camera/frame` with fresh metadata, and then report unavailable after disconnect or lease expiry.
