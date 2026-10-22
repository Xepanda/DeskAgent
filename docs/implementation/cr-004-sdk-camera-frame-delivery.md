# CR-004：官方 SDK WebSocket 媒体到 Host 相机查看器的交付契约

- **提出方：** A0 架构与协议
- **范围：** A2 WSPK / 官方 Runtime / A3 Host SDK 接入 / A6 相机服务的数据交付边界
- **状态：** **Lead/A0 已批准接口边界；A3 官方 SDK camera adapter 与 managed Application 接线已有 L2 源码/测试证据；L3/L4 未通过**。本仓 Host adapter 已实现 camera lease 转发、SDK capture 结果校验/投递、单 capture 在途及断连清理；本轮 WSL 15 项定向测试通过。测试使用 fake transport/fixture JPEG，不证明真实 Runtime 部署或目标相机出图。
- **不在范围：** 修改 WSPK wire format、修改官方 SDK、改变相机硬件驱动或宣布实机 JPEG 已通过
- **关联：** `contracts/camera_frame_delivery.md`、CR-002、`docs/implementation/p0-3-official-sdk-device-contract.md`

## 1. 代码与 SDK 证据

### A2 固件 WebSocket

- `firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c::desk_sdk_ws_client_poll()` 收取 Runtime → 设备方向的 WebSocket binary message，校验完整 WSPK header/长度后直接丢弃；源码注释确认尚无 application ingress callback。它不是设备相机 JPEG 上行到 Host 的接收端。
- 相机响应方向由 `desk_sdk_bridge_send_media_frame()` 走 WebSocket binary sender：`desk_sdk_bridge.c` 的 `ctrl.camera.capture` 先返回带 `session_id` 的 `sys.ack`，再发送 `FRAME_IMAGE=3` WSPK，header 包含 `stream_id` 和 `sequence`。
- 当前 capture handler 只分配 **65536-byte** JPEG buffer（`size_t max_jpeg = 65536`）。因此尽管 WSPK 通用 payload 上限为 128 KiB、Host `MAX_CAMERA_JPEG_BYTES` 为 128 KiB，固件当前相机命令能产出的 JPEG 有效上限仍是 64 KiB。CR-002 的 WSPK 131088-byte 完整帧上限是传输帧通用边界，不等同当前相机采集上限。
- 当前图像发送使用 `flags=0`，作为单个未分片 WSPK `FRAME_IMAGE`。这被官方 SDK `_on_image_frame()` 当作完整 image 处理。

### 官方 SDK Runtime 与解析

- 锁定 SDK `runtime/daemon/connections/websocket_server.py` 接收设备 WebSocket binary frame；`RawFrameRouter.route_external()` 将 `str | bytes` 原样送往当前 Desktop 连接，或在 Application channel 活跃时送往 Application channel。
- 官方 `protocol.py::parse_wspk()` 解析 `<4sBBHII`，得到 `BinaryFrame(frame_type, flags, stream_id, sequence, payload)`。
- 官方 `WatcheRobot._capture_image()` 等待 `ctrl.camera.capture` 的 ACK `session_id`，再等待匹配 `stream_id` 的 `FRAME_IMAGE`。`_on_image_frame()` 接受 `flags=0` 完整帧；带 `FLAG_FRAGMENT` 时按 `FLAG_FIRST/FLAG_LAST`、`stream_id` 和连续 `sequence` 重组，超出 SDK `MAX_IMAGE_BYTES` 或序号不连续即丢弃。相机结果队列有界为 1，新的完整帧替换旧帧。
- SDK Runtime 的 WebSocket `serve()` 当前设 `max_size=None`。因此接入层不能把 TCP JSON 控制帧 `MAX_FRAME_SIZE=65536` 误作 binary 限长，也不能假设 Runtime 已替应用层执行 WSPK 媒体上限。

### Host A6 消费者

- Legacy TCP 模式下，`host/desk_agent/main.py` 将 `RobotAdapter.register_camera_frame_callback()` 注册到 `CameraViewerService.ingest_robot_frame()`；该 `RobotAdapter` 只对应自有 TCP 37020，不会自动收到官方 SDK Runtime 的 WebSocket binary media。**managed Application 模式已有独立接线：** `_run_managed_application()` 将既有 `ApplicationContext.robot` 与同一 `context._transport.send_command` 注入 `OfficialRobotCameraAdapter`，并把 frame sink 接到 `CameraViewerService.ingest_robot_frame()`；它不创建第二个 Runtime 或连接。
- `host/desk_agent/connectors/robot/official_camera_adapter.py::OfficialRobotCameraAdapter` 在设备 lease ACK 后启动单 capture worker，使用官方 `robot.camera.capture()` 接收其已关联的 `ImageFrame`，校验 JPEG 标记、content type、正 session ID 和默认 ≤64 KiB 上限后投递完整 JPEG bytes。它按 Host cadence 限制 capture 并发；capture 失败、lease 过期、release 或上游 SDK disconnect 会停止/清理本地 frame state。此为源码事实，实际 Runtime/设备行为仍未验证。
- `CameraViewerService.ingest_robot_frame(frame_bytes)` 当前接收**已剥掉 WSPK header 的 JPEG bytes**，再由 `push_frame()` 检查 `4..128 KiB` 大小和 JPEG SOI/EOI；仅活动租约内保存单张最新帧，过期帧由默认 2000 ms freshness 检查返回 unavailable，释放租约会清除帧。
- 现有 Host 资源接口和 firmware 已定义 `resource.camera.acquire/release`、`ctrl.camera.acquire/release`、`ctrl.camera.capture` 命令；Host `main.py` 在获取租约时 acquire 并请求 capture，在释放/临时租约结束时 release。SDK 的高级 `camera.capture()` 管理 capture ACK/图像关联，但没有公开 camera lease 专用方法； pinned SDK 的通用 `send_command()` 可承载仓库已定义的 acquire/release 命令。

## 2. A0 批准接口与当前接线

**数据路径（已实现 L2 adapter）：A2 发送原有 WSPK binary → 不修改的 pinned official SDK parser/camera API → A3 `OfficialRobotCameraAdapter` → A6 `CameraViewerService.ingest_robot_frame(jpeg_bytes)`。**

A3 adapter 复用本仓已定义的命令及 pinned SDK `send_command()` / `camera.capture()` 语义，不增加新的 wire message、第二套设备 Runtime，也不修改官方 SDK。A6 继续只消费完整 JPEG bytes 和维护 Host viewer lease / freshness；A2 继续只负责设备驱动采集、WSPK 完整帧发送及设备 lease，不直接跨设备调用 Host callback。

1. Host `resource.camera.acquire` 成功后，A3 adapter 使用 SDK 当前会话向设备发 `ctrl.camera.acquire`（同一 `client_id` 与剩余租期），设备拒绝则回滚 Host lease 并报告现有 `RESOURCE_BUSY` / 设备错误。
2. 仅在设备 lease 有效期间，A3 使用 pinned SDK `camera.capture()` 请求帧。该 SDK API 负责 ACK 和返回 image 的 `session_id` 关联及其定义的 image fragmentation 重组。A3 只把完成且匹配该 capture session 的 `FRAME_IMAGE` payload 交给 A6；不把 WSPK header 或音频帧传给相机服务。
3. 预览刷新按当前 Host `target_fps` 运行，单设备只允许一个 capture 在途；每个 lease 至多保留一个等待/最新完整 JPEG，落后帧丢弃，不累积无界队列。定时刷新达到 SDK/device capture 超时则停止本轮并显式报告 unavailable；不得把超时/断连前的旧帧作为新帧。
4. A6 现有 JPEG 校验、single latest frame、lease owner 和 2000 ms stale 检查继续生效。HTTP viewer 只可在当前 Host lease owner 下读取新鲜帧；无有效租约或没有新鲜帧时返回不可用。
5. Host lease release、expiry、SDK session disconnect、device reboot 均停止刷新并清空 Host latest frame；有连接时尽力发送现有 `ctrl.camera.release`。连接已断时记录 release 未确认，依赖设备租约超时清理，重连不得复用旧 session_id 或旧帧。

更细的 Python 内部回调定义见 `contracts/camera_frame_delivery.md`。该回调是进程内边界；WSPK 仍是唯一媒体 wire format。

## 3. 上限、背压和错误语义

| 边界 | 建议上限/行为 | 当前证据状态 |
| --- | --- | --- |
| WebSocket 完整 WSPK message | ≤ `DESK_WSPK_MAX_FRAME_SIZE` = 131088 bytes（128 KiB payload + 16-byte header），超出在 Host 接收层拒绝 | `_run_managed_application()` 包装既有 SDK binary callback，在 SDK camera consumer 处理前拒绝非 bytes/超上限 payload；对应 managed-startup contract test 为 L2。Pinned Runtime 部署后的实际 ingress 仍未测 |
| 单个相机 JPEG | 当前设备 source **≤65536 bytes**；A6 已接受至 128 KiB。若后续提高设备实际 capture 上限，须先另行核对 SRAM/PSRAM 并保持 WSPK payload ≤128 KiB | firmware `ctrl.camera.capture` 的 64 KiB allocation 是当前有效 source cap；不是 128 KiB |
| 队列/重组 | 单设备一个 capture 在途、Host 一个最新 JPEG；由 pinned SDK camera API 关联 capture 与 image；adapter 再限制完整 JPEG ≤64 KiB | A3 adapter 已接入 SDK `camera.capture()` 并校验完整 image；15 项 WSL L2 测试覆盖 parser/adapter/managed startup；未测 Runtime/真机 |
| Camera lease | Host 与设备使用现有 acquire/release 命令；租约 owner 与 duration 保持同一；设备 busy/无租约映射为既有错误，不创建第二种租约 | managed-mode adapter 已转发 acquire/release 并在 acquire 失败时回滚 Host lease；代码/fixture 测试为 L2 |
| 无帧/坏帧 | WSPK/header/size/sequence/JPEG 校验失败只丢弃当前帧并保留诊断；capture 超时、无租约、设备 busy 作为失败返回；不能透传前一张图冒充成功 | camera handler 当前把驱动失败映射为 `RESOURCE_BUSY`；`interfaces.md` 已记录需修正为硬件错误，此处不改错误字典 |
| 断线/释放 | 清理 Host latest frame、capture waiter、fragment assembly 和 lease owner；断线时 release 未确认必须记录；重连需新鲜 capture | managed SDK disconnect observer 已接线到非阻塞 camera invalidation，覆盖 L2；真实 Runtime 断线/重连与设备租约回收仍待 L3 验证 |

## 4. Ownership 与待决事项

- **A2 (`firmware/components/desk_sdk_bridge/`)：** 保持当前 WSPK 不变；camera capture 在 active lease 下执行并发 `FRAME_IMAGE`；明确 capture 64 KiB 限制与 WSPK 128 KiB 通用上限。A2 不负责 Host callback。
- **A3 (`host/desk_agent/connectors/robot/`)：** 唯一新增/维护的官方 SDK Robot/Camera adapter owner；从 pinned official Runtime/SDK 收取 image/capture 结果，执行 WSPK/session/序列校验与限流，映射现有 Host camera acquire/release/capture 命令，随后调用 A6 的 byte callback。Lead/A0 已批准将该目录纳入 A3 独占路径。
- **A3 的最小 composition-root 接线授权：** 允许仅修改 `host/desk_agent/main.py` 中 Robot/Camera adapter 的初始化和依赖装配，注册 adapter frame callback 到现有 `CameraViewerService.ingest_robot_frame`，并按需接通已有 camera lease/capture 调用。该授权不扩展到 `host/desk_agent/camera/` 或 `host/desk_agent/web/` 的实现；不得改变 CameraViewerService、HTTP/Web 页面、租约/新鲜度实现。
- **A6 (`host/desk_agent/camera/`, `web/`)：** 唯一 viewer/租约/新鲜度/JPEG consumer owner；保留 `ingest_robot_frame(bytes)` 输入，不解析 WebSocket/WSPK，不创建第二个设备连接。
- **A0：** 冻结跨模块适配边界、上限与验收证据；批准必要的 A3 ownership 修订。

**明确排除：** A4 的 `host/desk_agent/connectors/ha/` 与 HA 状态/服务接入不变；A6 的 viewer、管理页面及 CameraViewerService 所有权不变。A3 只增加 `connectors/robot/` 中的官方 SDK Robot/Camera transport adapter 与对应测试，并执行上述 `main.py` 最小接线；不得改 A4/A6 生产路径。

当前协议足以裁决 image transport、WSPK parsing、session correlation、已有 lease commands 和 Host sink；**没有待新增的 wire protocol 字段**。A3 adapter、managed Application composition-root 接线及相机 callback 已有 L2 源码/测试证据；本轮 WSL 指定 suite 为 **15 tests, OK**：`tests.connectors.test_official_robot_camera_adapter`、`tests.sdk_contract.test_official_sdk_host_media_ingress`、`tests.core.test_managed_sdk_startup`。它使用 fake SDK transport、合成/fixture JPEG 和本地 managed-startup seam，不是对未修改 Runtime 部署或物理设备的 L3 证明。剩余门包括：官方 Runtime 在目标 Host 的实际部署/配置、设备与 Runtime 的真实配对及 WebSocket 媒体收发，以及实体相机真实 JPEG 可读性。此前现场相机空头/无 JPEG 问题仍未关闭。A2 `desk_sdk_ws_client_poll()` 的下行 binary 丢弃不能作为 A6 source；旧 TCP `RobotAdapter` callback 也不能被宣称自动接入 WebSocket Runtime。

## 5. 验收门槛

1. 单测：接收有效 WSPK image（≤64 KiB JPEG）并注入正确 JPEG；错 `stream_id`、坏 header/length、非法 JPEG、超限帧、断序/不完整 fragment 都不更新 latest frame；音频与非 image frame 不进入 A6。
2. 资源/背压：capture 不并行；慢 Host 只保留一张 latest frame；lease busy、capture timeout、SDK disconnect、release 和 expiry 均清理等待者/帧且无旧图复用。
3. 官方 Runtime 闭环：未修改的 pinned Runtime/SDK 建立真实设备 WS session；Host SDK adapter 发 acquire/capture/release；收到匹配 `session_id` 的官方 `ImageFrame` 后 A6 `/api/camera/frame` 返回可解码、内容对应现场的 JPEG，并包含新鲜 seq/age；断开后页面显示 unavailable。
4. 实测证据需包含 WSPK type/flags/stream/sequence/length（不得记录敏感 pair token）、ACK/capture error、Host lease 状态、JPEG 可打开结果和恢复结果。mock、fixture JPEG 或仅 `parse_wspk()` 只能算 Level 2，不能证明真实 camera/Runtime/Host 闭环。

**真实相机仍是独立阻塞：** 本轮不触碰串口或设备；此前现场已观测 capture 返回空头/无 JPEG。即便接口实现完成，仍需 A1 在相机驱动关口先取得真实 JPEG，之后再做本 CR 的 Level 3/4 验收。
