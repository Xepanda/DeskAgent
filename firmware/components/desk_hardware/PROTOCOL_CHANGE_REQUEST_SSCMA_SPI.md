# CR: SSCMA SPI transport versus direct HX PTL capture

**Owner:** A1 (device hardware)  
**Decision requested from:** A0 (architecture and protocol)  
**Status:** Open; no transport change made  
**Scope:** Camera transport only. The existing deadline-based PTL retry remains in place.

## Finding

DeskAgent and Seeed's SenseCAP Watcher firmware use matching board wiring for
the HX interface (SPI2 GPIO 4/5/6, CS GPIO 21, PCA9535 SYNC bit 6, 12 MHz,
mode 0). The matching wiring does not establish transport compatibility.

DeskAgent's `desk_watcher_camera_capture` reads a seven-byte binary PTL base
header (`C0 5A`, type, body length), then reads a binary body containing the
HX media header, CRC, and JPEG. It separately switches HX mode through the
UART `WV1 STATUS` / `WV1 MODE` control path.

Seeed's `sscma_client_io_spi` is a transport for SSCMA's command/reply byte
stream. It sends transport packets identified by feature `0x10` and READ,
WRITE, or AVAILABLE operations; packet headers carry a length and the payload
is consumed by the SSCMA command parser as `AT+...` requests and JSON replies.
The image utility extracts the `data.image` string from an SSCMA reply. It is
not a direct PTL packet reader.

Relevant source locations in this component:

- `src/desk_watcher_camera.c`: PTL header parser, direct SPI read path, and
  UART mode control.
- `include/desk_hardware.h`: camera HAL board-driver callback.
- `src/desk_hardware.c`: forwards caller capture timeout to the board driver.

Upstream references:

- Seeed board wiring and `bsp_sscma_client_init`: `components/sensecap-watcher/include/sensecap-watcher.h` and `components/sensecap-watcher/sensecap-watcher.c` in [SenseCAP-Watcher-Firmware](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware).
- SSCMA SPI framing and transport operations: [`sscma_client_io_spi.c`](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware/blob/main/components/sscma_client/src/sscma_client_io_spi.c).
- SSCMA reply image extraction: [`sscma_client_ops.h`](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware/blob/main/components/sscma_client/include/sscma_client_ops.h) and `sscma_client_ops.c`.
- The Seeed repository declares Apache-2.0 licensing. No Seeed implementation code is copied by this change.

## A0 decisions required before an SSCMA implementation

1. Pin the Seeed repository revision and SSCMA component/dependency versions;
   confirm compatibility with the project's ESP-IDF version and build system.
2. Confirm which transports the installed HX firmware supports (direct binary
   PTL, SSCMA SPI command transport, or an explicit selectable mode) and how
   that mode is selected. Shared wiring alone is insufficient evidence.
3. Select one SPI2/HX transport owner. The current PTL driver and an SSCMA SPI
   client must not independently register or transact on the same device.
4. Define the camera HAL mapping from SSCMA command/reply to an image result,
   including the exact image encoding, decoding, size limits, timeout, and
   error mapping.
5. If upstream code is later incorporated, record Seeed Tech Co., Ltd.
   attribution and preserve the applicable Apache-2.0 license and notices.

After A0 resolves these points, A1 can implement the selected transport behind
the existing `desk_hardware_camera_capture_jpeg` entry point and validate it
on the target board. Until then, the PTL transport remains the only implemented
path; no image success or SSCMA/PTL interoperability is claimed.

## Validation still required

- Build the current PTL path with the selected ESP-IDF toolchain.
- On hardware, log several zero/empty PTL responses followed by a valid frame
  or deadline expiry; confirm bus release and camera mode restoration.
- Export and open a JPEG whose contents match the live scene.
- If SSCMA is selected, separately verify its command exchange, image encoding
  and decode, reset/mode behavior, timeout/error propagation, and single-owner
  SPI lifecycle on the actual HX firmware.
