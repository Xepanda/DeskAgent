# CR-002: WSPK 媒体帧上限与 Host binary 接收边界

**状态：** A0 兼容性裁决，待 A1/A2/A3 按归属落实并验证  
**范围：** WSPK binary 媒体帧上限；不改变 WSPK wire header 或 TCP JSON 控制帧契约。

## 问题与证据

当前实现中存在三处看起来不同的数值，但它们约束的对象并不相同：

| 位置 | 当前值 | 实际约束 |
| --- | ---: | --- |
| `firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h`：`DESK_WSPK_MAX_PAYLOAD_SIZE` | 131072 bytes | 固件 WSPK payload 上限，不含 16-byte header |
| `host/desk_agent/camera/camera_service.py`：`MAX_CAMERA_JPEG_BYTES` | 131072 bytes | Host camera service 保存/接受的 JPEG payload 上限，不含 WSPK header |
| `host/desk_agent/connectors/robot/robot_adapter.py`：`MAX_FRAME_SIZE` | 65536 bytes | TCP newline-delimited JSON 控制帧上限；其解析器处理文本帧，不处理 WSPK binary |

官方 SDK 固定使用 16-byte little-endian `<4sBBHII` WSPK header。SDK `watcherobot/protocol.py::build_wspk/parse_wspk` 按 header 中的 payload length 构帧/校验长度，不定义低于 128 KiB 的媒体 payload 上限。SDK WebSocket server 在 `runtime/daemon/connections/websocket_server.py::start()` 使用 `max_size=None`；`runtime/daemon/routing/raw.py::route_external()` 将 `str | bytes` 原样转发。因此 TCP JSON 的 64 KiB 上限目前不作用于官方 WebSocket binary 媒体路径，但 Host WebSocket binary 接收路径也没有自己的显式上限。

固件发送接口 `desk_sdk_bridge_send_media_frame()` 允许最大 131072-byte payload，并按需分配 `16 + payload_len` 的 PSRAM buffer；WebSocket sender 接收完整 WSPK packet，发送完整 binary message。camera 的 128 KiB JPEG 上限正好与固件 payload 上限一致。WSPK 与 camera 数据都不应把 header 算进 payload 上限。

主要关联契约/实现：

- `docs/implementation/interfaces.md` §6.3：TCP JSON 帧最大长度及 WSPK 格式不变的现有裁决。
- `docs/implementation/p0-3-official-sdk-device-contract.md`：媒体应在官方设备 WebSocket 上以 WSPK binary message 传输。
- `tests/sdk_contract/test_wspk_framing.py`、`tests/sdk_contract/test_sdk_bridge_protocol.py`：本仓格式、长度和固件封包测试。
- `tests/connectors/test_robot_adapter.py`：`MAX_FRAME_SIZE == 65536` 是 TCP JSON 控制帧契约测试。
- `tests/camera/test_camera_service.py`：相机 JPEG payload 上限测试。
- `.target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk/src/watcherobot/protocol.py`、`runtime/daemon/connections/websocket_server.py`、`runtime/daemon/routing/raw.py`：锁定的官方 SDK 解析、WebSocket 接收及 binary 原样路由实现。

## A0 裁决

**保留 WSPK 协议和 128 KiB payload 上限；不分片。为 Host 的 WebSocket 媒体 binary 路径单独增加并强制 131088-byte 完整 WSPK frame 上限（128 KiB payload + 16-byte header）。TCP JSON `MAX_FRAME_SIZE=65536` 原样保留并继续只约束换行 JSON 控制帧。**

这是“增大/明确 Host 媒体路径上限”的方案，不是把现有 JSON 控制帧上限改为 128 KiB。Host binary 限制必须在 WSPK 解码/媒体接收边界生效；不能因为 WebSocket 当前 `max_size=None` 就无限接收，也不能把 TCP 文本帧限制套用到 binary 帧。

选择该方案的理由：

1. 固件 JPEG 和 Host camera service 已使用同一 128 KiB payload 上限；较小统一上限会使合法的大 JPEG 在链路中被拒绝，且无现成证据证明现场 JPEG 能在更小上限内稳定编码。
2. 当前 WSPK parser 要求一条完整 packet 中包含 header 声明的完整 payload；协议没有 fragment index、总长度、重组或丢包恢复字段。拆分 WSPK 会改变 wire protocol、增加缓冲/状态/重连复杂度，且无法由现有官方 SDK `parse_wspk()` 直接消费。
3. JSON `MAX_FRAME_SIZE` 约束不同协议层，提升它不会修复 WebSocket binary 接收边界，反而会放大控制帧缓冲/解析的资源上限。

## 兼容性与资源影响

- **向后兼容：** WSPK header、frame type、stream/sequence 字段和已有 ≤128 KiB payload 均保持兼容。旧的较小 Host binary 接收器可能仍拒绝超过其实现上限的合法帧；更新后的 Host 对所有不超过 131088 bytes 的完整 WSPK binary message 接收。TCP JSON 限制保持 65536 bytes，因此现有控制协议行为不变。
- **资源：** 固件已有最大 131088-byte PSRAM 临时 buffer 上界；单条 WebSocket binary frame 需按完整帧处理。Host 应在读取/复制/解码之前检查完整 frame 长度并拒绝超限帧，以避免无界分配。由于 SDK server 当前 `max_size=None`，仅在后续 router 中检查会晚于 WebSocket 层接收/缓冲；实施时应在能看到设备角色和完整 binary frame 的最早层设置上限，或明确证明框架已将完整帧上限落实在接收分配前。
- **真实性限制：** 本裁决只统一协议/资源边界，不证明相机实际能产出 JPEG，也不证明官方 SDK 与设备已完成 Level 4 互通。

## 落地责任与验收门槛

1. **A1/A2 固件发送侧：** 在 WSPK payload `0`、`128 KiB` 边界分别验证；`128 KiB + 1` 必须拒绝，不能溢出或发送半帧。核对实际分配为 payload 加 16-byte header，并在 WebSocket binary path 发送一个完整 WSPK message。
2. **A3 Host/SDK 接收边界（必要时由 A0 先批准路径归属变更）：** 为 binary 媒体定义独立命名常量 `131088`，在接收/分发前拒绝 `131089` bytes；保留 TCP JSON `65536` 限制。验证超限关闭/拒绝策略不会污染后续连接或其他 client。
3. **官方 SDK 契约测试：** 经真实官方 WebSocket server/device-role connection 收到 16-byte header + 131072-byte payload，`parse_wspk()` 返回原始完整 payload；测试 131089-byte packet 被 Host 边界拒绝。另验证文本 JSON 65536/65537 边界仍按现有控制帧规则工作。
4. **设备实测：** 发送一张实际 JPEG，记录其 payload 长度、完整 WSPK frame 长度、SDK 解码结果和图片可打开证据；另跑连续采集以观察峰值内存、断连释放及重连。仅单测或构造的伪 JPEG 属 Level 2，不能代替真实相机证据。

**未决实现点：** Host 的正式 WebSocket 入口和媒体消费者由 SDK/runtime 负责，当前仓库 `RobotAdapter` 是 TCP JSON 适配器，不是该 binary 接收器。A2/A3 应在落地前指出具体由哪个 Host 层承担接收前限长；不得为了复用 `MAX_FRAME_SIZE` 而扩大 TCP JSON 控制帧限制。
