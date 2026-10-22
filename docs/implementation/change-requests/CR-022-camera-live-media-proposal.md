# CR-022 Proposal：相机实时媒体目标与传输路径

- **提出方：** A0 架构与协议
- **状态：** **PROPOSAL / NOT APPROVED；不冻结 wire，不授权固件、SDK 或 Host 实现**
- **关联：** [CR-004](../cr-004-sdk-camera-frame-delivery.md)、[CR-006](../CR-006-host-mjpeg-camera-stream.md)、[CR-011](../CR-011-hx6538-transport-adjudication.md)、[CR-002](../cr-002-wspk-media-frame-limit.md)、`contracts/camera_frame_delivery.md`
- **目的：** 将“相机视频/实时媒体推流”拆成可选且可验收的目标。本文不把现有 2 FPS 单帧轮询称为原生视频，也不批准变更消息协议。

## 1. 当前实现与证据边界

当前链路是 **按需采集 JPEG 快照，再由 Host 持续输出最新帧**：

```text
Host camera lease
  -> repeated SDK camera.capture() commands
  -> firmware ctrl.camera.capture: one HX PTL JPEG capture per command
  -> one WSPK FRAME_IMAGE per command
  -> A3 official_camera_adapter
  -> A6 CameraViewerService latest-frame slot
  -> Host HTTP multipart MJPEG /api/camera/stream
```

- CR-004 与 `contracts/camera_frame_delivery.md` 将 SDK `camera.capture()` 定义为 request/response；firmware 每次回一个 `FRAME_IMAGE=3`。当前 SDK capture cadence 上限为 2 FPS，Host 的 `iter_mjpeg()` 也最多输出 2 FPS。HTTP multipart 连接可以持续，但图像仍来自重复快照命令。此为持续更新的低帧率预览，不代表传感器连续采集或设备主动推流。
- `desk_sdk_bridge.c` 的 capture handler 为每次命令分配 JPEG buffer，取得一次 `desk_hardware_camera_capture_jpeg()` 结果后发送一个 WSPK image。当前 handler 的 JPEG 上限为 64 KiB（CR-004 记录）；WSPK 通用 128 KiB 上限不是当前相机路径能力证明。
- `desk_watcher_camera.c::desk_watcher_camera_capture()` 按次切换 HX 模式、等待 SYNC、读取一帧 PTL JPEG 并退出/释放 SPI bus；默认 PTL capture timeout 为 500 ms。HX6538 的连续传感器输出、硬件编码器或可持续取帧能力不能从这段 host 侧 receiver 代码推出。
- CR-011 选择 WatcheRobot HX PTL 为本项目运行时相机 transport，但目标板 HX 固件身份和 PTL 兼容性仍未由当前源码证明；不能假定其他摄像头协议或传感器模式在目标设备可用。
- **更正 SDK README 与实际执行路径的冲突：**锁定的 Python SDK commit `8bc13967a06ef073459be47e930ff43ef37ee8fb` 有 `ApplicationRtc` 和 `rtc.video.mjpeg.v1`。README 的一段文字称 MJPEG 走名为 `mjpeg-data` 的 WebRTC data channel；但同一 pinned 安装包的实际浏览器代码 `sdk_media_lab/web/app.js::createMjpegVideoTransport()` 从 Runtime 状态读取 `mjpeg_websocket_url`，调用 `new WebSocket(url)`，连接成功后发文本 `ready`，并解析该 socket 收到的二进制 WJPG/JPEG。pinned `runtime/daemon/runtime.py::device_status()` 给出的 URL 是 `ws://<peer-ip>:82/ws/mjpeg`。纯视频模式不创建 RTCPeerConnection；AV 模式的 WebRTC 负责音频，视频仍走单独 MJPEG WebSocket。`ApplicationRtc`（`application/rtc.py`）通过 Device channel 发送 `ctrl.rtc.session.start/stop`、offer/candidate 和状态/能力/统计；它本身不是 JPEG 数据通道。故当前有证据的 SDK 示例媒体拓扑是 **SDK Application 会话控制 + 独立设备 MJPEG WebSocket + 浏览器**，不是 README 所称的 MJPEG WebRTC data channel。该 bundled app/endpoint 属 pinned SDK 随附示例实现，不等于通用 SDK 的公开视频传输 API，也不是当前 DeskAgent 的实现或实测。
- 当前已审固件更没有可供该拓扑复用的持续帧源：`desk_hardware_camera_stream_start()` 和 `desk_hardware_camera_stream_pump()` 在 `ESP_PLATFORM` 返回 `DESK_HW_ERR_UNAVAILABLE`；HX6538 板级路径暴露的是一次调用采集一张 PTL JPEG。A1/A2 源码审计未证明目标板可持续采集、循环 one-shot 的稳定帧率/延迟或相机供电/总线在长时间推流时可用。当前 Host capture adapter 限制为最多 2 FPS；重复快照只能是低帧率预览，不能满足用户明确要求的实时视频/媒体推流验收目标。
- pinned 示例的设备 `:82/ws/mjpeg` 直连浏览器路径未提供当前 DeskAgent 所需的 Host lease 集成证据；README/客户端可见的 `ready` 文本握手不是身份认证。不能据此声称该端点已认证或受现有官方 Runtime 配对保护。其是否监听、如何鉴权及其生命周期均需本项目固件实现和实测；当前固件未发现该 MJPEG server。
- 当前固件虽定义 WSPK `FRAME_VIDEO=2` 常量，官方 SDK 对 `FRAME_VIDEO` 的默认接收用途是 face tracking preview 回调；这不自动形成通用 video API、RTC session 或浏览器直播链路。该常量也不等于已实现视频编码器。

源码/契约依据：`docs/implementation/cr-004-sdk-camera-frame-delivery.md` §1–3；`docs/implementation/CR-006-host-mjpeg-camera-stream.md` §1–3；`contracts/camera_frame_delivery.md`；`firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c` capture handler；`firmware/components/desk_hardware/src/desk_hardware.c` `desk_hardware_camera_stream_start/pump`；`firmware/components/desk_hardware/src/desk_watcher_camera.c` capture；`firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h` frame type。pinned vendor checkout `.target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk` HEAD `8bc13967a06ef073459be47e930ff43ef37ee8fb`：`examples/sdk_media_lab/README.md`（与下述执行源码冲突）、`examples/sdk_media_lab/web/app.js::createMjpegVideoTransport/parseWjpgPacket`、`web/mjpeg-transport.mjs`、`web/mjpeg-chunk-reassembly.mjs`、`service.py` RTC lifecycle and device-status use、`src/watcherobot/runtime/daemon/runtime.py::device_status`、`src/watcherobot/application/rtc.py::ApplicationRtc`。vendor checkout 是忽略路径下的 L1 参考，不是 DeskAgent 生产实现。

## 2. 可选路径与权衡

| 选项 | 语义 | 优点 | 代价/限制 |
|---|---|---|---|
| **A. 保持 CR-004/006 快照预览** | 按租约周期调用 capture；每次一个 JPEG/WSPK `FRAME_IMAGE`；Host multipart MJPEG 发送新鲜帧，当前上限 2 FPS。 | 复用已实现的控制、adapter、租约、Host latest-frame 与 HTTP viewer；无新 wire。 | 更新慢且受每次命令/采集延迟影响；仍是轮询快照。只有产品接受 ≤2 FPS 的画面更新时适用。不得标成连续相机视频或设备主动推流。 |
| **B. SDK 会话控制 + 有界 MJPEG WebSocket 媒体流（推荐进入可行性阶段）** | 复用 pinned SDK 的 `ApplicationRtc`/`rtc.video.mjpeg.v1` 做会话控制；媒体 source transport 参考 pinned `sdk_media_lab` 实际执行的独立设备 MJPEG WebSocket（设备 producer → MJPEG WS consumer → A6 viewer），再由 Host relay 到受控浏览器。Host relay 是本产品拟议适配，并非 SDK 示例已有能力；README 声称的 `mjpeg-data` WebRTC data channel 未被该网页执行代码使用。 | 方向上符合用户明确的实时推流目标；避免把每帧做成 SDK capture command；可由 Host 管 viewer、租约及 latest-frame sink。可以复用 pinned 示例的 WJPG/chunk source 格式作为协议审查输入。 | 当前 DeskAgent 没有 RTC session handler/capability、MJPEG WebSocket server、持续 JPEG producer 或 A6 relay 接线。sample `:82/ws/mjpeg` 的 `ready` 握手不认证；必须由 A0/安全审查冻结并验证授权，不能直接照搬为生产认证。A1 未证明 HX 连续/重复 capture 的吞吐、延迟和并发资源能力。需设备、SDK Application 与 Host/A6 协同实现和真机验收。 |
| **C. 新增 WSPK `FRAME_VIDEO` 设备主动发送** | 在现有 Runtime WebSocket binary channel 定义连续帧会话。 | 复用设备到 Runtime 的既有连接，避免额外的设备媒体监听端口。 | pinned SDK 对 `FRAME_VIDEO` 的默认消费者是 face-tracking preview callback，不是通用 live-camera sink；会话、帧封装、时间戳、背压、浏览器交付和授权仍需新增契约。除非验证 B 不适用，否则不推荐先走此路。 |

**不提出 H.264 或其他编码方案。** 当前本仓及锁定 SDK 对目标 HX6538 的证据是 PTL JPEG/SDK MJPEG 路径，没有目标设备硬件编码器或持续视频输出证明。任何其他编码须先有供应商/设备源码和目标硬件证据，再另行裁决。

## 3. A0 建议路径

若产品目标确为用户要求的“相机视频/实时媒体推流”，建议以**选项 B：SDK 会话控制 + 独立 MJPEG WebSocket source + Host/A6 relay**作为下一阶段可行性验证方向。这是基于 pinned SDK 示例的媒体 source 形态，为本项目提议的 Host relay 适配；不是该 SDK 示例当前已实现的拓扑，也不表示已有可直接复用的 DeskAgent 接口。拟议顺序为：A6 viewer 通过 Host 鉴权并申请单一 camera lease；A3 使用现有唯一 SDK Runtime / `ApplicationRtc` 控制会话；A2 在获批的设备 session 内门控媒体 capability；A1 持有唯一 HX/SPI camera owner，产出完整 JPEG 到有界 latest-frame slot；设备媒体 WS 只向获授权的 Host consumer 发送冻结格式，A3/A6 校验后写入现有 latest-frame sink 并服务浏览器。不能让浏览器直连未认证的 `:82` 端点作为产品默认路径。若改用 pinned sample 的 browser-direct topology，则必须先由 A0 冻结可验证的会话绑定认证方案，并由 A6/A3 明确如何不绕过 Host lease。

在批准 B 前，A1 必须以目标 HX6538/目标固件证据确认真实持续帧源：当前 HAL stream start/pump 在 ESP 上明确不可用，板级 capture 是单次 PTL JPEG。需要实现或复用受仲裁的持续 producer，并实测持续帧率、帧间隔、P95 采集到显示延迟、资源回收及 SPI/SYNC/相机与语音/显示并发；暂不能假定循环 one-shot 达标。当前 ≤2 FPS SDK capture 路径仅适合作为低帧率快照预览，不满足实时直播验收。若源端能力无法达到产品指标，应选择接受 A 的明确低帧率预览，或开展 HX/传感器能力项目，不能将快照重新命名为直播。

如果产品只需要远程看一张偶尔更新的现场画面，则选项 A 已是最小路径，应该继续明确使用“快照预览 / 最高 2 FPS”命名并验收端到端新鲜度，而不启动 RTC 项目。

### 3.1 新协议提案必须冻结的消息语义

选择 B 后，应先由 A0 核对 pinned SDK `ApplicationRtc` API、bundled app 的 MJPEG socket/WJPG framing 与当前设备 JSON envelope，再另立批准 CR/schema。以下列出必须冻结的语义/字段，不在本 proposal 中创造新 wire payload：

1. 只有设备真实实现 video session 且 camera capability 就绪时才广告 `rtc.video.mjpeg.v1`；不支持时拒绝 start，不静默回落到 2 FPS 快照。
2. Control plane 冻结 SDK 现有 `client_id`、`session_id`、`command_id`、`mode`、RTC protocol/capability gating、ACK/NACK/event correlation、重复请求和终态语义；复用 `ApplicationRtc`/`watcher-rtc/1` 的已定义形状，不因媒体 transport 更换而私造 RTC payload。
3. Media plane 明确 socket endpoint 所有者/端口、WS binary message 的封装、帧内 version/header length/sequence/capture timestamp/JPEG byte length 语义、chunk transfer identifier/index/count/offset/完整性以及上限。pinned bundled sample 的 `app.js` 解析 `WJPG` v1、20-byte little-endian header（sequence、capture timestamp ms、JPEG size）；chunk reassembly source 使用 `WDCH` magic。只有 A0 在确认来源/兼容和测试后，才能将这些 sample-defined bytes 选入 DeskAgent 契约；否则必须通过单独 A0 CR 定义，并明确不兼容该样例。此处不扩展或猜测 sample 之外的字段。
4. Session/lifecycle 冻结 host lease owner 与 device media session 的绑定键、start/stop ACK、租约续期/到期、session generation、每连接身份、断线/重连/替代 viewer 行为。未通过授权或 lease 失效不得发送；stop、断线、应用退出、browser close、设备重启需清理 producer、sensor mode、SPI owner、buffers、WebSocket、RTC peer（若音频使用）和 Host latest-frame/client state；旧 generation/sequence 不得进入新会话。
5. Resource ownership 冻结唯一 camera producer 与 `camera.capture()` 的互斥关系、camera lease 的唯一 authority、SPI/SYNC 占用窗口，以及与 audio/display/motion 的允许并发。不得由 RTC producer 和 capture command 同时操作 HX/SPI2。
6. Bounded delivery/backpressure 冻结 source queue、socket send buffer 和 Host/browser 各自上限、超限/慢消费者时 latest-frame-wins 丢弃策略及可观测 dropped counter、分片重组最大数量/总字节/TTL、单/多 viewer 数量，以及 freshness timeout。禁止无限队列、无界分片缓存和以重复旧图满足新帧状态。
7. Authentication/fanout 冻结媒体 socket 和 Host viewer 的认证主体、session/lease 绑定、授权撤销、重放隔离、LAN/TLS 假设和 token 生命周期；pinned sample 的 `ready` 握手不是认证机制。若暂时只能运行于受信隔离 LAN，须在产品决策中明确该范围，不能宣称生产鉴权完成。Host/A6 只向授权 viewer 分发当前租约内新鲜帧。
8. A6 当前 HTTP MJPEG API 与设备 MJPEG WebSocket 的 relay 关系必须明确：relay 采用何种有界单槽/订阅模型，释放时清空 frame 和订阅；或选择 sample 的 browser-direct topology，并另行证明其鉴权和 lease 控制。不得默认把 websocket、WebRTC data channel、WSPK `FRAME_VIDEO` 视为可互换的传输。

## 4. 待产品决策

产品需先给出这些可测目标，再批准下一步规格：

- 需量化实时目标：最低持续 source/rendered FPS、采集到浏览器显示的最大或 P95 延迟，以及验收持续时长。用户已明确目标是实时媒体推流；选项 A ≤2 FPS 只可作为另行接受的快照预览降级，不关闭直播需求。
- 画面目标：分辨率、JPEG 质量/单帧大小上限、是否需要音视频同步或纯视频即可。
- 观看路径二选一：沿用 Host `/api/camera/stream`，由 Host 从设备 MJPEG WebSocket 有界 relay 并统一做 viewer 鉴权/lease；或让浏览器按 pinned sample 直连 Runtime 提供的 `ws://<device>:82/ws/mjpeg`。后者不是 WebRTC data channel，且 sample `ready` 握手没有鉴权；只有先冻结 session/lease 绑定认证并验证不绕过 Host policy 后才可选用。
- 并发媒体：视频是否需与机器人声音、麦克风录音、屏幕动画同时运行；组合模式下可接受的帧率和音频质量下限。
- 退化行为：网络变慢时允许跳帧到什么程度；多长时间无帧应报 unavailable；是否接受短暂黑屏并自动重连。

这些指标当前没有被本仓产品契约裁决。本 proposal 不擅自设定目标 FPS、分辨率、码率或延迟数字。

## 5. Ownership 与未来验收

以下是方案边界建议，不构成实现授权：

- **A1 — `firmware/components/desk_hardware/`：** 核实目标 HX6538/PTL 源端能力并实现唯一 sensor/SPI producer（只有 capability 确证且 CR 批准后）；负责 capture scheduling、frame ownership/slot、timestamp、resource arbitration 和停止回收。当前 stream HAL 在 ESP 上明确 unavailable；不得把循环快照或 mock pump 写成已具备连续源。
- **A2 — `firmware/components/desk_sdk_bridge/`：** 获批后负责唯一 SDK device-session gate/capability、media socket lifecycle/authorization and bounded egress；实现 A0 冻结的已有/新增 framing、sequence/error/drop reporting、session generation 及 stop/disconnect teardown。不因 `ApplicationRtc` 存在、`:82/ws/mjpeg` 见于 sample 或 WSPK `FRAME_VIDEO` 常量而声称固件实现完成。
- **A3 — `host/desk_agent/connectors/robot/` 与最小 Host composition root：** 复用 pinned SDK 唯一 Runtime/Application session 和 RTC control API；按冻结方案管理 lease/capability、媒体 consumer 与 session generation。若 Host relay 被选，提供一个有界媒体 consumer 接入 A6 sink；不得创建第二个 Runtime、越过认证或持久化视频。
- **A6 — `host/desk_agent/camera/`、`host/desk_agent/web/`：** 负责现有 Host viewer 的会话/lease API、viewer 认证、帧新鲜度、断线 unavailable、relay bounded slot 与 release 清空。若选择 browser-direct sample topology，则必须新增并验证它与 Host authorization/lease 的安全绑定，不能把 `ready` 当认证，也不能把 SDK demo 当作 DeskAgent 已集成。
- **A0：** 审批产品目标、最终 wire/schema、与 CR-004/006/011 的兼容性，以及是否保留两个明确模式。当前提案不批准改 `contracts/`。

验证必须分层：

1. **L1 源码/SDK 契约：** 记录 pinned SDK revision、API/runtime endpoints、README 与执行代码的冲突、实际传输/帧封装、capability gating 和设备实现覆盖范围；用当前固件源码说明 stream API 在 ESP 上 unavailable、HX path 是按次 capture。SDK bundled sample/其它配套固件不能替代目标板 source evidence。
2. **L2 软件验证：** session/control lifecycle、authorization/lease/capability gate、frame/session/sequence/chunk validation、malformed/oversize/duplicate/stale frames、concurrent start/viewer、stop/timeout/disconnect/reconnect cleanup；慢客户端只允许冻结的有界队列/latest-frame slot 并有 dropped counter。证明旧会话帧不能进入新 viewer、无无限队列、无并行 producer，browser direct 与 relay 两种方案只测获选的一种。
3. **L3 目标设备测量：** 真实 HX6538 与当前 ESP32 固件上记录源端帧率、发送/浏览器解码帧率、采集到显示延迟分布、JPEG size、丢帧/断流、内存峰值和释放后回收；在产品选择的并发 audio/animation 模式下重复。验证断 Wi-Fi、WebSocket/RTC 中断、浏览器关闭和设备重启后的资源释放/恢复。
4. **L4 场景验收：** 持续时长、网络质量、重连次数、帧率及延迟阈值从产品目标中取得；报告始终区分 source FPS、sent FPS、rendered FPS。截图、HTTP MJPEG 长连接、伪帧、SDK 回环测试不能单独证明真实连续视频体验。

**当前等级：** 本提案所引用的本仓契约/源码和锁定 SDK source/package 为 L1。源码已更正：pinned sample 的媒体实际经独立 `:82/ws/mjpeg` WebSocket，而不是 README 声称的 `mjpeg-data` WebRTC data channel；此 sample 不证明 DeskAgent 具备该 server、鉴权或连续 HX source。DeskAgent ESP camera stream start/pump 当前返回 unavailable，现有最多 2 FPS capture 是快照预览，不满足用户要求的实时视频验收。没有新增构建、设备、服务或浏览器闭环测试；产品交付目标/观看路径仍待选择，CR-022 仍 PROPOSAL / NOT APPROVED。
