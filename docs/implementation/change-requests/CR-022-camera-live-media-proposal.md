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
- 锁定的 Python SDK commit `8bc13967a06ef073459be47e930ff43ef37ee8fb` 含 `ApplicationRtc` 与 `rtc.video.mjpeg.v1` capability。其 `sdk_media_lab` 文档说明，具备该 capability 的固件可通过 `watcher-rtc/1` 信令启动会话，并把 MJPEG 帧经 `mjpeg-data` WebRTC data channel 直接发往浏览器，按 latest-frame-wins 并在停止/断连时释放相机资源。该证据仅说明锁定 SDK 有此应用接口和一个测试应用路径；当前 DeskAgent 固件源码未发现 `ctrl.rtc.session.start` handler、该 capability 宣告或持续 MJPEG publisher。SDK 示例及其配套固件记录不是当前 DeskAgent 固件的实现或设备实测证据。
- 当前固件虽定义 WSPK `FRAME_VIDEO=2` 常量，官方 SDK 对 `FRAME_VIDEO` 的默认接收用途是 face tracking preview 回调；这不自动形成通用 video API、RTC session 或浏览器直播链路。该常量也不等于已实现视频编码器。

源码/契约依据：`docs/implementation/cr-004-sdk-camera-frame-delivery.md` §1–3；`docs/implementation/CR-006-host-mjpeg-camera-stream.md` §1–3；`contracts/camera_frame_delivery.md`；`firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c` capture handler；`firmware/components/desk_hardware/src/desk_watcher_camera.c` capture 函数；`firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h` frame type；锁定 SDK 的 `src/watcherobot/application/rtc.py`、`src/watcherobot/robot.py`、`examples/sdk_media_lab/README.md` 与 `docs/himax-media-stage-20260909.md`。

## 2. 可选路径与权衡

| 选项 | 语义 | 优点 | 代价/限制 |
|---|---|---|---|
| **A. 保持 CR-004/006 快照预览** | 按租约周期调用 capture；每次一个 JPEG/WSPK `FRAME_IMAGE`；Host multipart MJPEG 发送新鲜帧，当前上限 2 FPS。 | 复用已实现的控制、adapter、租约、Host latest-frame 与 HTTP viewer；无新 wire。 | 更新慢且受每次命令/采集延迟影响；仍是轮询快照。只有产品接受 ≤2 FPS 的画面更新时适用。不得标成连续相机视频或设备主动推流。 |
| **B. 采用锁定 SDK 的 `rtc.video.mjpeg.v1` 推流路径** | 设备在一个受管媒体 session 中持续产出 MJPEG；按已验证的 `watcher-rtc/1` 信令和 MJPEG data channel 发送最新帧。 | 与锁定 SDK 的 RTC/MJPEG 应用能力方向一致；可避免每帧创建一个 capture command；数据通道可采用 latest-frame-wins。 | 当前 DeskAgent 固件没有该 session handler/capability/publisher；HX 持续帧率与并发资源尚无本项目证据。若视频直达浏览器，现有 A6 Host `/api/camera/stream` 不是自动得到的代理；必须决定消费者与鉴权/租约由谁控制。需要新的 A0 设备契约、A1/A2 实现及 A3/A6 接线和实机测量。 |
| **C. 新增 WSPK `FRAME_VIDEO` 设备主动发送** | 在现有 WebSocket binary channel 自定义连续帧/流会话。 | 延续已有 WSPK media channel；当前 SDK parser 可识别 frame type 2。 | pinned SDK 对 `FRAME_VIDEO` 的默认消费者不是通用 camera stream；会话创建/结束、编码、时间戳、背压、浏览器交付和租约仍需另造契约。存在与 B 重复建设风险。除非 A0 证实 RTC API 无法承载目标，否则不推荐。 |

**不提出 H.264 或其他编码方案。** 当前本仓及锁定 SDK 对目标 HX6538 的证据是 PTL JPEG/SDK MJPEG 路径，没有目标设备硬件编码器或持续视频输出证明。任何其他编码须先有供应商/设备源码和目标硬件证据，再另行裁决。

## 3. A0 建议路径

若产品目标确为“实时媒体推流”，建议以**选项 B：RTC MJPEG**作为下一阶段可行性验证方向，复用锁定 SDK 的应用接口；但当前只作提案，不视为可直接接入或实现已批准。选择 B 前，A1 必须先从 HX PTL/目标固件证据确认持续取帧是否可行、是否需改传感器模式、重复 capture latency 是否能达到产品目标，并确认 SPI/SYNC/相机/显示/音频资源约束。若这项源端能力没有证据或不满足目标，CR-022 回到产品选择：接受选项 A 的低帧率预览，或另开硬件/传感器能力项目；不能通过给轮询快照改名来满足原生视频目标。

如果产品只需要远程看一张偶尔更新的现场画面，则选项 A 已是最小路径，应该继续明确使用“快照预览 / 最高 2 FPS”命名并验收端到端新鲜度，而不启动 RTC 项目。

### 3.1 新协议提案必须冻结的消息语义

选择 B 后，应先由 A0 核对 pinned SDK `ApplicationRtc` API 与当前设备 JSON envelope，再另立批准 CR/schema。至少明确：

1. 只有设备真实实现 video session 且 camera capability 就绪时才广告 `rtc.video.mjpeg.v1`；不支持时拒绝 start，不静默回落到 2 FPS 快照。
2. start/stop、offer/candidate、会话状态和统计按 session/client/command 关联；拒绝、超时、重复 start/stop、会话替换和设备重启均有终态。实际字段/事件沿用 SDK 已定义形状或由新 A0 契约明确，本文不发明 wire payload。
3. 每个媒体帧必须是完整且可解码的 JPEG，并有会话内单调序号及可用于新鲜度测量的采集时间语义；序号缺口允许表示背压丢弃，不能重复旧帧伪装新帧。若 SDK data channel 自身不能传递所需元数据，则需单独定义媒体封装。
4. 摄像头 lease 是唯一资源授权；会话未获 lease、lease 到期或 owner 不匹配时不发帧。stop、断线、应用关闭和设备重启清理 producer、sensor mode、SPI owner、buffers、WebRTC peer/data channel 和 Host/browser 状态；新连接不得复用旧 session/sequence 状态。
5. 新路径和现有 `camera.capture()` 的关系必须唯一明确：RTC video 活跃时是禁止 one-shot capture，还是由同一 session 仲裁；不得有两个同时操作 HX/SPI2 的 producer。
6. Host/browser 明确选择 media delivery owner：a) SDK media page 通过 WebRTC 直连设备，A6 HTTP stream 不作为其代理；或 b) A3 在授权且有界的媒体通道内向 Host/A6 relay。不得创建第二个设备 Runtime、绕开 Host lease，或默认把数据通道改成无限可靠缓存。

## 4. 待产品决策

产品需先给出这些可测目标，再批准下一步规格：

- “实时”的定义：首选预览帧率/最低持续帧率，以及采集到浏览器显示的最大或 P95 延迟。
- 画面目标：分辨率、JPEG 质量/单帧大小上限、是否需要音视频同步或纯视频即可。
- 出口路径：沿用 Host `/api/camera/stream` 可鉴权的 MJPEG HTTP endpoint，还是允许浏览器经 SDK WebRTC data channel 直连设备。直连能降低 relay，但要明确定义浏览器会话与租约/鉴权关系。
- 并发媒体：视频是否需与机器人声音、麦克风录音、屏幕动画同时运行；组合模式下可接受的帧率和音频质量下限。
- 退化行为：网络变慢时允许跳帧到什么程度；多长时间无帧应报 unavailable；是否接受短暂黑屏并自动重连。

这些指标当前没有被本仓产品契约裁决。本 proposal 不擅自设定目标 FPS、分辨率、码率或延迟数字。

## 5. Ownership 与未来验收

以下是方案边界建议，不构成实现授权：

- **A1 — `firmware/components/desk_hardware/`：** 核实目标 HX6538/PTL 源端能力，负责唯一 sensor/SPI owner、连续帧来源与缓冲生命周期；以现有 camera HAL/arbiter 为基础证明 sensor frame timing、释放/恢复及与音频/显示并发资源影响。不得从 PTL receiver 推断 HX 自身能力。
- **A2 — `firmware/components/desk_sdk_bridge/`：** 若选项 B 获批，负责设备 session/capability 门控和媒体帧进入 SDK 连接的固件协议侧适配；按 A0 批准的 SDK 消息语义实现序号、错误、stop/disconnect 清理与有界发送。不得仅因已有 `FRAME_VIDEO` 常量或 face tracking 收帧路径就声称 video 已实现。
- **A3 — `host/desk_agent/connectors/robot/` 与最小 Host composition root：** 负责复用 pinned SDK `ApplicationRtc`/device session、会话状态和租约回调；若产品选择 Host relay，负责单个有界媒体消费者和到 A6 sink 的接线。不得创建第二个 Runtime 或把未授权帧缓存到磁盘。
- **A6 — `host/desk_agent/camera/`、`host/desk_agent/web/`：** 负责浏览器 viewer、Host endpoint（若被选为交付路径）、鉴权和显示帧新鲜度；如果选择直连 WebRTC，明确该页的 token/session 获取与资源释放边界，不把直接 SDK demo 自动当作 Host 已集成。
- **A0：** 审批产品目标、最终 wire/schema、与 CR-004/006/011 的兼容性，以及是否保留两个明确模式。当前提案不批准改 `contracts/`。

验证必须分层：

1. **L1 源码/SDK 契约：** 记录 pinned SDK revision、capability gating 和设备实现实际覆盖的 API；用当前设备固件源码说明 HX 连续源是否存在。SDK 示例或另一个固件版本不能替代该证据。
2. **L2 软件验证：** session 生命周期、lease/capability gate、frame/session/sequence 校验、坏帧、并发 start、stop/timeout/disconnect 清理；慢客户端时只保留至多一张待发帧并明确丢帧计数。证明无旧帧冒充、无无限队列和无并行 sensor producer。
3. **L3 目标设备测量：** 真实 HX6538 与当前 ESP32 固件上记录源端帧率、发送/浏览器解码帧率、采集到显示延迟分布、JPEG size、丢帧/断流、内存峰值和释放后回收；在产品选择的并发 audio/animation 模式下重复。验证断 Wi-Fi、WebSocket/RTC 中断、浏览器关闭和设备重启后的资源释放/恢复。
4. **L4 场景验收：** 持续时长、网络质量、重连次数、帧率及延迟阈值从产品目标中取得；报告始终区分 source FPS、sent FPS、rendered FPS。截图、HTTP MJPEG 长连接、伪帧、SDK 回环测试不能单独证明真实连续视频体验。

**当前等级：** 本提案所引用的本仓契约/源码和锁定 SDK 源码为 L1。没有新增构建、设备、服务或浏览器闭环测试；当前 DeskAgent 实时视频推流能力仍未验证。
