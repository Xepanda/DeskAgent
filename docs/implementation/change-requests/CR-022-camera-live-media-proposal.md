# CR-022：相机实时 MJPEG 媒体流协议裁定

- **裁定方：** A0 架构与协议
- **状态：** **A0 ADJUDICATED / APPROVED FOR IMPLEMENTATION**（协议与产品目标已冻结；尚未实现、构建或实机验收）
- **授权依据：** 用户明确要求继续实现相机真实采集及视频/实时媒体推流，并授权“按冻结协议或经 A0 裁定的变更实现”。本裁定据此解除“只允许 2 FPS 快照”对实时媒体目标的限制。
- **关联：** [CR-004](../cr-004-sdk-camera-frame-delivery.md)、[CR-006](../CR-006-host-mjpeg-camera-stream.md)、[CR-011](../CR-011-hx6538-transport-adjudication.md)、[CR-002](../cr-002-wspk-media-frame-limit.md)、`contracts/camera_frame_delivery.md`
- **范围：** 冻结设备到 Host 的实时相机帧协议、Host 到浏览器的媒体路径、最低验收指标及资源/安全语义。允许 A1/A2/A3/A6 按 ownership 实现；不表示 HX6538 已达到指标，也不代表任何 L3/L4 通过。
- **兼容性裁决：** 对 capability `camera.stream.mjpeg.wspk.v1`，本 CR 覆盖 CR-004/CR-006 中针对重复 `camera.capture()` 刷新的 2 FPS 上限与 2000 ms freshness。该上限/阈值仍只适用于单帧快照模式；实时 stream 必须满足本 CR 的 15 FPS 与 1000 ms 门槛。

## 1. A0 裁定

**目标路径：** 继续使用已配对 WatcheRobot Runtime 的现有设备 WebSocket/WSPK 通道，增加受相机租约和会话门控的主动 JPEG 帧序列；A3 通过 pinned SDK 的 `ApplicationChannels` 原始帧回调接收，交给 A6 现有 latest-frame sink；浏览器继续使用 CR-006 已定义的 Host 鉴权 multipart MJPEG `/api/camera/stream`。

```text
物理相机连续新鲜帧
  -> A1 唯一 camera/SPI owner
  -> A2 WSPK FRAME_IMAGE（每帧一个 WJPG v1 + JPEG）
  -> paired WatcheRobot Runtime / ApplicationChannels
  -> A3 校验 stream、sequence、timestamp、JPEG
  -> A6 有界 latest-frame slot / Host multipart MJPEG
  -> 已鉴权且持有 lease 的浏览器
```

此为设备主动持续发送的 MJPEG 视频流；它不依赖每帧调用 `camera.capture()`。Host HTTP multipart 仍是浏览器输出封装，不构成传感器连续性的证据。每个帧必须来自新的真实传感器采集；重发缓存 JPEG、循环测试图、重复 capture 结果都不能计入帧率或验收。

**明确取舍：**

| 路径 | SDK/仓库证据 | A0 裁定 |
|---|---|---|
| WSPK `FRAME_IMAGE=3` 单次 `camera.capture()` | pinned Python SDK 将 `FRAME_IMAGE` 放入容量为 1 的 latest-image 队列；`CameraDomain.capture()` 仍按请求等待对应图片。CR-004/006 当前最多 2 FPS，属于单帧快照刷新。 | 保留为拍照与明确标注的低帧率快照预览；不能满足本 CR 实时视频验收。 |
| WSPK `FRAME_VIDEO=2` | pinned SDK `Robot._on_binary()` 仅在 face-tracking preview 已打开时将该帧传给 `FaceTrackingPreview._push_image()`；没有通用相机视频订阅 API。 | 不选作通用直播 wire；不得仅因存在帧类型常量就声称兼容。 |
| pinned sample 独立设备 `:82/ws/mjpeg` | 固定 SDK 示例 `sdk_media_lab` 实际使用此 WebSocket 和 WJPG 包；Runtime 状态可给出 endpoint。浏览器发送的 `ready` 不是认证，且 DeskAgent 固件没有该 server。 | 不作为产品路径：新增裸设备端口绕开现有 paired WSPK 与 Host lease/auth 边界。示例 README 关于 `mjpeg-data` WebRTC data channel 的说法与其实际网页代码冲突，不据此选 WebRTC。 |
| **WSPK `FRAME_IMAGE` 主动序列 + Host MJPEG relay** | 同一 paired Runtime 通道提供配对会话边界；SDK `ApplicationChannels` 可将原始设备帧交给受管 Application；Host 已有 lease、鉴权和 multipart MJPEG sink。需要新增相机 stream 控制和 WJPG 子封装。 | **选定。** 保留现有 Runtime、WSPK 和 Host viewer 安全边界，新增 wire 语义明确区分直播帧与单次 capture。 |

不选择 H.264/WebRTC 视频编码。当前目标板源码只证明 HX PTL 单次 JPEG capture；没有可用的连续编码器证据。若未来要改为其他编码或独立设备媒体 socket，必须另行 A0 裁定。

## 2. 冻结的设备控制与媒体消息

以下是本 CR 批准的新相机流能力。它复用既有 WSPK 外层 framing、JSON 控制 envelope、camera lease 与官方配对 Runtime；不要求修改 pinned SDK 源码或启动第二个 Runtime。

### 2.1 Capability 和控制面

- 仅在固件能够启动真实 producer、完成授权/资源门控并发送下述 framing 时，才广告 capability **`camera.stream.mjpeg.wspk.v1`**。未广告或不支持时，Host 必须将直播标为 unavailable 并拒绝 start；不得静默回退到 2 FPS capture。
- 先使用 CR-004 的现有 `ctrl.camera.acquire` 建立唯一设备 camera lease。设备 Runtime 配对会话中的同一 `client_id`/lease owner 才可开始和停止流。无租约、owner 不匹配、已有拍照/直播 producer 或 camera 资源冲突时以既有 `sys.nack`/资源忙错误拒绝。
- 新增 JSON 控制类型 **`ctrl.camera.stream.start`**，沿用既有 envelope 的 `client_id`、`session_id`、`command_id` 和 ACK/NACK correlation。`data` 固定字段：`width=640`、`height=480`、`target_fps=15`。成功 ACK 的 `data` 必须含 `stream_id`（非零 uint16）和 `device_monotonic_ms`（设备单调时钟，uint32 ms）；只能在 producer 已启动且首帧能力就绪后 ACK。分辨率不支持或能力暂不可用须明确 NACK，不得上采样伪装 640×480。
- 新增 **`ctrl.camera.stream.stop`**，包含当前 `stream_id`。ACK 仅在 producer 已停、帧发送已停止且 camera/SPI/buffer owner 已释放后返回。重复 stop 对已终止 stream 幂等成功；未知 stream 返回既有无效会话错误。
- 新增 **`ctrl.camera.clock.ping`**：请求 `data={ping_id, host_send_monotonic_ms}`；`sys.ack` 回显 `ping_id` 并返回 `device_receive_monotonic_ms`、`device_send_monotonic_ms`。Host 收到 ACK 时记录 `host_receive_monotonic_ms`，与本地发送时间构成四时点样本；至少每 10 秒采样，忽略 RTT 最大样本并以低 RTT 样本估 offset。所有时间均为各端 monotonic uint32 ms，计算须处理回绕。offset 不确定度超过 100 ms 即停止报告延迟并使性能验收失败。
- 保留现有 `ctrl.camera.capture` 作为独立单帧操作。拍照与 stream 在设备端互斥；stream 活跃期间 capture 返回 busy，不得并行访问 HX/SPI2。

### 2.2 WSPK 帧与 WJPG v1 子封装

- 每个完整媒体消息是现有 WSPK binary frame，`frame_type=FRAME_IMAGE (3)`、`flags=0`、`stream_id` 等于 start ACK 的值，WSPK `sequence` 为该 stream 内递增的 uint32 序号。只在一个 paired Device channel 上发送。
- WSPK payload 使用 pinned `sdk_media_lab` 已实现的 **WJPG v1** 20-byte little-endian header，紧接完整 JPEG：
  - byte 0..3：ASCII `WJPG`；byte 4：version `1`；byte 5：reserved `0`；
  - byte 6..7：header size `20`；byte 8..11：sequence；
  - byte 12..15：capture timestamp，设备 monotonic 毫秒 uint32；
  - byte 16..19：JPEG 长度；余下字节为 JPEG。
- WJPG sequence 必须等于外层 WSPK sequence，且同一 stream 严格递增（允许 uint32 回绕）；timestamp 单调递增（允许 uint32 回绕）。JPEG 长度必须精确匹配 payload，JPEG marker/解码必须通过验证。`FRAME_IMAGE` stream **禁止 WSPK fragmentation**；完整 WSPK frame 不超过 CR-002 的 128 KiB payload，上述限制使单张 JPEG 最大为 `128 KiB - 20 bytes`。超限帧丢弃并计数，不拆成无界缓存。
- 设备 monotonic 时间与 Host 单调时间不得直接相减。A3 在 start ACK 后按至少每 10 秒一次的四时点 ping/ack 估算时钟 offset，使用 RTT 最小样本并记录不确定度；offset 不确定度超过 100 ms 时显示 latency 未知/验收失败，不能报告伪精确延迟。Host 每帧保留 capture timestamp、Host receive time、browser rendered time及 stream/session generation，以计算采集到渲染延迟。

## 3. 产品目标与验收门槛

以下是实时媒体的最低合格线，不是性能预测或可下调的默认值：

| 指标 | 必须达到 |
|---|---|
| 物理画面 | 实际相机传感器的新鲜 JPEG；不允许 mock、合成画面、缓存帧、上采样替代目标分辨率。 |
| 输出分辨率 | 每帧至少 **640×480**。设备不支持时该目标失败，不能靠缩放较低分辨率帧达标。 |
| 持续帧率 | source 采集帧率与浏览器成功渲染帧率均至少 **15 FPS**；以每个 60 秒窗口计，不只看短时峰值。 |
| 延迟 | 帧采集到浏览器显示的 **P95 ≤500 ms**，P99 ≤1000 ms；报告时钟 offset 不确定度和 source/sent/rendered FPS。 |
| 新鲜度 | 最新帧年龄超过 **1000 ms** 即清空旧图并显示 unavailable/reconnecting；禁止继续呈现旧画面为 live。 |
| 连续性/时长 | 目标设备端到端连续运行 **10 分钟**；每个 60 秒窗口至少 15 FPS，且不得发生超过 1000 ms 的无新帧间隔。 |
| 帧上限与完整性 | WSPK payload ≤128 KiB；JPEG 必须完整且可解码；记录大小分布和丢帧率。 |

若 HX6538/PTL、ESP32-S3 内存、总线或 Runtime 网络不能达到以上值，应将“实时视频推流”验收判为失败并修复源端/链路；不得通过把指标改成 ≤2 FPS、放大 freshness timeout、重复旧 JPEG 或只测 Host HTTP 连接来关闭目标。用户可另行使用名称明确的快照模式，但快照通过不等于本 CR 通过。

## 4. 租约、安全、资源仲裁和降级

- **唯一授权路径：** paired WatcheRobot Device channel 是设备媒体出口；不增设未认证 `:82` listener。A3 只接受当前 paired `session_id`、当前有效 Host lease owner、活动 `stream_id` 的帧。A6 只向持有 Bearer 鉴权且 lease owner 相同的 viewer relay。浏览器不直连设备端口；token 不进 URL、HTML、帧头或日志。
- **单一 owner：** 每设备同时只允许一个 camera producer、一个 active stream generation、一个 Host lease owner 和一个浏览器 subscriber。Host latest-frame 槽容量为 1；设备发送队列和 A3/A6 待处理队列也各最多保留 1 张待发/最新帧。慢消费者采用 latest-frame-wins 丢弃旧帧，暴露 source/transport/host drop counters；不积压历史视频。
- **资源所有权：** A1 是唯一 HX6538/SPI2 camera owner，stream 与单次 capture 互斥。每帧完成后释放 PTL/SPI 临时资源，producer 在 stop/lease expiry/disconnect 释放 sensor mode、SPI、DMA/JPEG buffers。与 audio/display/motion 并发只有在资源 arbiter 明确准入且同样达到上述帧率/延迟门槛时才允许；冲突返回 busy 或停止视频，不可饿死语音/显示或绕过现有 motion owner。
- **续租和停止：** Host lease 续租仍用现有规则；lease 失效、显式 release、viewer/Host shutdown 或 session stop 时先停止采集并发送 stream.stop。设备 Runtime channel 断开、Host process 崩溃或设备重启时 producer 必须本地 fail-safe 停止，清除 stream generation/帧缓存；Host 超过 1000 ms 无新帧立即清空 viewer。未收到 stop ACK 标为未确认，等待设备租约 TTL；不得马上在同一设备上假定已释放并启动第二个 producer。
- **重连/重放：** 新连接必须重新配对/恢复 Runtime session、取得新 lease 并 start 新 stream；新的 stream_id/generation 与旧 session 绑定。丢弃迟到的旧 session、旧 stream 或非递增 sequence；禁止从 Host 快照、SDK 队列或浏览器 canvas 恢复旧图。
- **降级：** 实时 capability 缺失、source FPS 不达标、frame age 超限、鉴权失败、资源 busy、解析错误、断线或同步误差超过 100 ms 时显示明确 unavailable/error。不得自动切到 2 FPS snapshot 并仍标记 LIVE。若产品启用快照入口，必须由用户显式选择并显示“快照预览，最高 2 FPS”。

## 5. Ownership 与验证

- **A0（本裁定）：** 冻结 capability、消息、帧格式、实时目标与安全/资源语义；任何改变外层 WSPK、WJPG 字段、指标或路径的变更回到 A0。
- **A1 `firmware/components/desk_hardware/`：** 实现真实新鲜帧 producer 和唯一相机仲裁；先证明 640×480、15 FPS/10 分钟、内存峰值与并发可行。当前 `desk_hardware_camera_stream_start/pump()` 在 ESP 路径返回 unavailable，`desk_watcher_camera_capture()` 为单次 PTL JPEG，因此源端实现是明确依赖，不得用循环 mock pump 声称完成。
- **A2 `firmware/components/desk_sdk_bridge/`：** 实现 `camera.stream.mjpeg.wspk.v1` capability、上述 start/stop ACK、WSPK `FRAME_IMAGE` WJPG v1 frame、授权/session gate、序号、背压/错误计数和 disconnect teardown；保持原 `camera.capture` 语义并互斥。
- **A3 `host/desk_agent/connectors/robot/`：** 使用锁定 WatcheRobot SDK `ApplicationChannels` 的原始 binary callback，不创建第二个 Runtime；解析并关联 WSPK/WJPG、同步时钟、丢弃旧 generation、在有效 lease 内将 JPEG 交给 A6。既有 Robot `camera.capture()` adapter 只服务单帧模式。
- **A6 `host/desk_agent/camera/` 与 `web/`：** 沿用 CR-006 的 Host multipart MJPEG、Bearer/lease owner 校验和 one-slot viewer；输出帧龄/序号/渲染速率指标、freshness timeout 清图、断线 unavailable 与重新连接状态。
- **A7 `tests/e2e/`、`tools/verification/`、`docs/acceptance/`：** 做软件负例/背压/会话重放和故障恢复验证；设备可用后按表中数值完成真实源帧到浏览器闭环及 10 分钟连续测量。软件 fixture 只能算 L2。

### 验收分层

1. **L1 源码/契约：** pinned SDK HEAD `8bc13967a06ef073459be47e930ff43ef37ee8fb`；核验 `protocol.py` WSPK frame types、`Robot._on_binary()` 的 FRAME_VIDEO 特殊路由、`ApplicationChannels` 原始帧回调、示例 `parseWjpgPacket()`、Runtime 提供 `mjpeg_websocket_url` 的代码；本仓 CR-004/006、`desk_sdk_bridge.c` capture handler 与 `desk_watcher_camera.c` PTL 路径。此等级只证明接口/设计依据。
2. **L2 软件：** WSPK/WJPG malformed/oversized/sequence/timestamp/session 测试；无 lease、错 owner、并发 capture/start、重复 start/stop、慢 consumer 单槽丢帧、clock offset 不确定、断线/重启/旧 frame 重放、HTTP auth/freshness/clear/reconnect。必须验证 Host 分辨率、source/sent/rendered 计数分开；fixture 不算真实画面。
3. **L3 单项硬件：** 目标 HX6538 与 ESP32-S3 实测 640×480 真实内容，连续 10 分钟达到 source ≥15 FPS、资源回收、内存/队列有界；保存脱敏 WSPK/WJPG 序号/时戳/长度、错误/丢帧计数及可打开 JPEG。
4. **L4 端到端：** paired Runtime → A3 → A6 鉴权 Host `/api/camera/stream` → 浏览器的真实帧闭环，测量每 60 秒 source/rendered FPS 与 P95/P99 延迟；同时验证 lease release、慢客户端、断 Wi-Fi、Runtime 断开、浏览器关闭、设备重启后 1000 ms 内清图、资源释放和新 lease 恢复。截图或长连接本身不能证明实时流。

## 6. 依据与剩余硬件依赖

- CR-004 和 `contracts/camera_frame_delivery.md`：目前 `camera.capture()` 为 request/response，SDK adapter 最高 2 FPS，Host 单帧新鲜度槽；不能满足上述实时门槛。
- CR-006：已有 Host `/api/camera/stream` multipart relay、Bearer/lease owner 校验、单槽 latest-frame 和过期清图语义，可复用浏览器出口，不证明源端持续帧。
- 固定 WatcheRobot SDK commit：`src/watcherobot/protocol.py` 定义 `FRAME_VIDEO=2`、`FRAME_IMAGE=3`；`src/watcherobot/robot.py::_on_binary()` 将 FRAME_VIDEO 专用于已启动的 face-tracking preview，将 FRAME_IMAGE 放入容量 1 的 image queue；`application/channels.py` 暴露原始 Application frame；`examples/sdk_media_lab/web/app.js::parseWjpgPacket()` 解析 WJPG v1 20-byte header；`runtime/daemon/runtime.py::device_status()` 暴露示例 `:82/ws/mjpeg` URL。该 SDK 示例 URL/ready 不能替代本产品认证。
- 本仓 `firmware/components/desk_hardware/src/desk_hardware.c` 的 ESP stream start/pump 当前 unavailable；`desk_watcher_camera.c::desk_watcher_camera_capture()` 是带 SYNC/PTL deadline 的单次 JPEG 路径。**因此本裁定目前仅是 A0 协议决策（L1），连续物理 producer、FPS/分辨率/延迟、SPI/语音并发、实际 paired Runtime 收发和浏览器画面均待 A1/A2/A3/A6/A7 实现及硬件验收；不宣称直播已跑通。**

- 当前 desk_sdk_bridge.c 的 ctrl.camera.capture 仍是每条命令分配缓冲、调用一次 desk_hardware_camera_capture_jpeg() 并发送一条 FRAME_IMAGE；其现有捕获缓冲为 64 KiB。该单帧路径不等价于本 CR 主动流，A2 需实现独立 stream handler 并复核有界 JPEG buffer。
