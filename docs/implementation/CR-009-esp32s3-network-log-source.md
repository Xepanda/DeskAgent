# CR-009：ESP32-S3 已配对网络日志订阅与流

- **提出方：** A0（基于 A7 网络日志源缺口记录）
- **提出日期：** 2026-10-22
- **状态：** **APPROVED — 有限 L2 范围；A2 per-connection auth-latch seam 已实现并通过定向 L2 测试；subscribe/unsubscribe handler 在 dirty source 中待 review；设备 event producer/send 与 writer 尚未实现；A3 bounded consumer module 已提交但未接入 managed app，Runtime 未部署**
- **关联：** `docs/acceptance/network-log-source-gap-2026-10-22.md`、`docs/implementation/interfaces.md` §5 L-09 / §6.4、`firmware/components/desk_sdk_bridge/`
- **预期 owner：** A0 契约/依赖裁决；A1 设备日志 sink/queue；A2 固件协议桥 handler 与发送；A3 Host consumer、Runtime 部署/托管集成；A7 验收

## 1. 目标与现状

目标是在已配对、已认证的设备连接上按需订阅 ESP32-S3 诊断日志。**不得新建未认证的 TCP/UDP/HTTP 服务或其他旁路，也不得借用 Tuya 云端语音会话上传日志。**

当前仓库没有网络日志订阅、日志流消息或固件日志 sink。`tools/verification/capture_esp32s3_logs.py` 当前只被动读取 UART。L-09 描述的官方 Runtime 拓扑为 Runtime 在 Host 侧监听 LAN WebSocket、ESP32 设备主动连接；配对发现使用 UDP 37021，现有自有 TCP 37020 是另一条控制桥。上述控制桥、配对 UDP 和已存在的普通业务事件都不是日志源。

本 CR 提议只在 L-09 的已配对 WebSocket 应用通道上增加**协商后、按需、只读**日志订阅。它不更改 TCP 37020、UDP 37021、WSPK 图像帧或既有命令含义。此文档是待评审的契约草案；通过源码搜索或本 CR 本身不能证明网络日志已实现。

## 2. 通道、方向与授权

1. **连接方向：** ESP32-S3 仍主动连接配对请求中指定的 Host WebSocket Runtime。Runtime 在该既有连接上发送订阅/取消订阅请求；设备只在该连接上返回确认与日志事件。不得创建设备侧日志 listener、额外端口或广播日志。
2. **发现：** 沿用 L-09 配对流程发现 Runtime；UDP 37021 只用于既有配对请求/响应，配对完成后不承载日志订阅或数据。
3. **配对身份状态：** pinned Runtime 的实际认证入口是 WebSocket 首个 `HardwareHello`。`ExternalWebSocketServer._handle_hello()` 调用 Runtime 的 `_authorize_hardware_hello()`，再由 `DevicePairingSession.connect_device()` 校验当前配对状态、`pair_request_id`、`daemon_instance_id`、`session_token`、`mode` 和预期 peer IP，成功后才进入 `CONNECTED`。不要把 capability 列表误作配对凭据或权限授权。
4. **双侧授权门：** Host consumer 必须默认关闭，并要求本地显式启用及当前已配对设备会话；设备 handler 必须再要求当前 WebSocket session 的认证 latch、paired metadata 完整及设备本地 log gate。两侧任一 gate 未通过即 fail closed。不得在日志事件中携带 session token，也不得把 pairing code、session token 或认证头写入日志。当前尚无证据证明 `device.logs.read` capability 有正式 wire 注册和消费路径，因此 L2 不要求观察该 capability 后才订阅；若 A2 后续补齐正式 wire 证据，再由 A0 裁决是否纳入。
5. **隔离：** log subscription 是设备只读诊断操作，不得执行命令信封中的业务操作。Runtime 侧不得默认转发给普通 Desktop 消费者或公网；Application 默认 callback 也不是日志归档授权。
6. **传输保护边界：** pinned Runtime 使用明文 `ws://`，没有 TLS。所有经 LAN 传输的日志都视为 LAN 可见；只允许严格 allowlist、已脱敏、低敏诊断项。不得传输任意原始 ESP-IDF/printf 文本、个人内容、凭据、音视频、用户输入、业务参数或可识别设备的长期标识。此 CR 不提供链路机密性。

## 3. 版本化消息建议

所有新 `sys.log.*` 消息沿用已认证 WebSocket 上的**单个 JSON WebSocket text message**；WebSocket message boundary 是 framing，不附加 TCP 37020 的换行帧，不使用 UDP，也不使用 WSPK binary frame。每个 `sys.log.*` 消息必须带 `log_schema_version: "1.0.0"`；未知版本或未知必需字段不得按日志猜测解析。L2 初版 v1 的 `type` 和字段由 A0 在本 CR 中批准冻结；实现必须逐字段遵循下列契约，任何扩展另走 A0 裁决。`sys.log.event/gap` 使用本 CR 的专用日志事件结构，避免把诊断流伪装成已有业务 `EventEnvelope`。

### 3.1 订阅与确认

Runtime → 设备；必须匹配 pinned SDK 的 `build_command()` 封套：

```json
{
  "type": "sys.log.subscribe",
  "code": 0,
  "data": {
    "command_id": "logsub_01",
    "log_schema_version": "1.0.0",
    "min_level": "WARN",
    "components": []
  }
}
```

`min_level` 枚举 `ERROR | WARN | INFO | DEBUG`，缺省 `WARN`；`components` 必须是已批准 allowlist 的子集；v1 中空列表表示当前 schema 的全部批准来源（仅 `camera`），不表示任意日志。每条 WebSocket connection 最多有一个活动 stream；已有活动 stream 时重复 subscribe 必须返回 `sys.nack`，`data.error` 为 `ALREADY_SUBSCRIBED`，`data.command_id` 关联新请求，并保留原 stream 不变；禁止创建第二个 producer/queue consumer。Host 生成 `command_id = uuid.uuid4().hex`，并通过 pinned SDK `watcherobot.protocol.build_command()` 构造 `{type, code, data}` envelope。设备返回的 `sys.ack` / `sys.nack` 必须在 `data.command_id` 原样关联请求；不得新设顶层 `request_id`。成功 ACK 的 `data` 含 `type`, `command_id`, `accepted`, `stream_id`, `boot_id`, 当前 `next_sequence` 和协商 filter；NACK 将稳定错误标识放在 SDK 可读取的 `data.error`。请求只从确认时的序列开始，不补发先前日志。

Runtime → 设备取消订阅也必须用 `build_command()` 封套：`{"type":"sys.log.unsubscribe","code":0,"data":{"command_id":"...","log_schema_version":"1.0.0","stream_id":"..."}}`。设备以 `data.command_id` 关联的 `sys.ack` 确认停止。该订阅租约仅绑定当前已认证 WebSocket session；断开、close、reconnect begin、reset、token 替换或 pairing metadata mutation 均撤销授权、停止 stream 并清空待发队列。重连必须重新完成 HardwareHello 并重新订阅。

### 3.2 日志事件与丢失通知

设备 → Runtime 日志事件建议采用以下专用结构：

```json
{
  "type": "sys.log.event",
  "data": {
    "log_schema_version": "1.0.0",
    "stream_id": "logstream_01",
    "boot_id": "boot-opaque-id",
    "sequence": 121,
    "uptime_ms": 123456,
    "level": "WARN",
    "component": "camera",
    "event_id": "CAMERA_CAPTURE_EMPTY",
    "fields": {}
  }
}
```

`sequence` 在单次 `boot_id` 内单调递增；序号缺口代表一个或多个事件丢失，序号不因订阅、重连或 filter 改变而复用。L2 初版 v1 冻结为单个事件 `CAMERA_CAPTURE_EMPTY`，其 `fields` 必须为空对象。A1 源码审计确认：`desk_watcher_camera_capture()` 收到零长度 7-byte PTL header，重试达到上限后返回 `rc=-25`，且 `out_len` 保持 0。`INPUT_CHANNEL_INIT_ERROR` 延后：当前音频 init 错误折叠为 generic unavailable，没有 MIC/SPEAKER 的 stage-specific mapping；须待音频来源可准确区分并经范围审查后另行提案。其他 event_id 延后新增 CR/裁决。`uptime_ms` 必填，避免未校准设备时钟造成假 UTC；设备时钟可信时才附加 RFC3339 `timestamp_utc`，否则省略。设备可发 `sys.log.gap` 控制事件，报告 `first_dropped_sequence`、`last_dropped_sequence`、`dropped_count` 和封闭枚举 `reason`。v1 仅定义 `reason: "QUEUE_FULL"`，适用于 A1 source queue 或 A2 bounded log-writer queue 因容量满丢弃的连续序号范围；断连即终止 stream 并清队列，不为断连期间未入队事件构造 gap，也不定义 `DISCONNECTED`。接收端也必须能从 sequence gap 发现丢失。gap 通知本身不回收或重用丢失序号。唯一有效的 v1 reason 是 `QUEUE_FULL`。示例：

```json
{
  "type": "sys.log.gap",
  "data": {
    "log_schema_version": "1.0.0",
    "stream_id": "logstream_01",
    "boot_id": "boot-opaque-id",
    "first_dropped_sequence": 17,
    "last_dropped_sequence": 18,
    "dropped_count": 2,
    "reason": "QUEUE_FULL"
  }
}
```

SDK v0.1.11 没有专用 log event/consumer API。`RawFrameRouter.route_external()` 会把设备合法 JSON frame 原样路由到活动的 Application channel；`LocalWebSocketApplicationBridge` 再调用 `on_frame`，其缺省 callback 是 `_ignore_callback`。若无活动 Application，则原始 frame 路由到 Desktop；不得把此默认 callback 或通用 Desktop 路由称为安全归档。A3 已有显式有界 consumer 模块；A3 仍须将其接入 managed Application message listener，并仅在授权 gate 后投递 `sys.log.event/gap`。默认 callback、Desktop raw route 不是日志归档。

日志不是任意业务事件转发：媒体、ASR 转写、用户输入、工具参数/结果、提醒内容、HA 实体值、Codex 文件内容等均不得进入该流。设备只发送经过 A1 审核的严格 allowlist 组件和低敏结构化诊断项；`fields` 只允许已批准的有界标量字段，不允许任意嵌套对象或自由文本；事件必须由审核过的枚举 `event_id` 表示，并仅携带该事件 schema 允许的有界 scalar `fields`（仅数值、布尔值或封闭枚举，不允许自由字符串）。禁止把原始 ESP-IDF/printf/UART payload 送入网络流；不持久化任何日志事件或原始 payload。设备端先做字段级脱敏，Host consumer 再二次脱敏。

## 4. 级别、隐私与资源边界

- 日志等级限 `ERROR/WARN/INFO/DEBUG`；不支持 `VERBOSE` 网络订阅。缺省 `WARN`，`DEBUG` 需显式本地订阅，固件重启后不恢复。
- 仅允许低敏、固定/结构化诊断值；必须删除/遮蔽 pairing code、session token、Wi-Fi 密码、云端/API/HA/Codex 凭据、Authorization 字段、密钥、音视频内容及用户可识别内容。对未知字段、未审核组件/消息默认丢弃；禁止把敏感原值仅依赖 Host 脱敏。LAN 明文可观察性下，即使脱敏也不得传送自由文本、用户内容或高敏运行时数据。
- `boot_id` 使用每次启动生成的非凭据、非长期设备标识随机值；不输出完整 MAC、序列号或固定设备身份。时间、tag、event_id、fields 长度均设上限，非法 UTF-8/过大事件丢弃并计数。
- 日志入口必须是固定时延、非阻塞 sink，由后台 worker 做队列消费、协议编码和 WebSocket 发送；sink 内禁止网络 I/O、阻塞等待、动态大块分配及再次调用 `ESP_LOG`（避免递归）。它不得改变既有 UART 输出/格式/级别。
- TAL `PrintLogV()` 在持有 global log mutex 时同步执行输出 sink；故 TAL sink 只能把已格式化、经 allowlist/脱敏后的短诊断复制到固定队列后立即返回。ESP-IDF `esp_log_set_vprintf` hook 没有 tag 参数且可能并发/重入：需先以有界 buffer 格式化，再解析审核过的 tag/event；必须可重入/并发安全，不能在 hook 内调用 `ESP_LOG`。
- 资源限制：候选 16 × 192-byte 固定队列（3072 bytes）+ 约 2 KiB internal worker stack，合计约 5 KiB。该预算未在目标板测量；A1 必须在进入 L3 前测量队列高水位、worker stack HWM、heap/PSRAM 和控制/语音干扰，超预算时先回到 A0 裁决。
- A1 source queue 固定 16 个事件槽，每槽最多 192 bytes；当前约 5 KiB 候选预算只覆盖该 source queue 与现有 worker stack，若增加 writer buffering/worker 必须另行计入；采用 **drop-new + counter**，保留已排队较早事件。事件观测时先分配每 boot 单调递增 `sequence`，即使 queue 满也推进序号并记下连续 dropped range。A1 queue API 必须以 O(1) 有界 `try_enqueue/try_dequeue/take_gap` 形式提供事件及 `QUEUE_FULL` gap，不执行网络 I/O、不等待空间、不动态分配。A2 生成每 boot 不含设备身份的 `boot_id`，在事件帧补充 stream/boot id 与 A1 捕获时的 monotonic `uptime_ms`；subscribe ACK 的 `next_sequence` 必须取 A1 source 的当前 next sequence（当前 A2 handler 使用 `s_log_sequence+1`；接入 producer 时须改为共享 source counter，不能另起计数）。
- A2 log writer 必须直接消费 A1 固定容量 source queue（除非实现证据证明需要额外固定容量缓冲），不得增加无界队列；A2 必须记录实际有限 send deadline，并以故障注入测试证明 worker write 不超过该上限；A1 producer/相机回调绝不调用 `desk_sdk_ws_client_send_text()`。现有 `send_text()` 同步进入 `ws_send_frame()`，可能阻塞；A2 只能在专用发送 worker 中以有界/有限 deadline 写 socket。source queue 满时由 A1 drop-new 并记录 `QUEUE_FULL` gap；断连时停止 writer、清队列且不重放。若另加固定容量 writer buffer，必须有界并纳入资源预算；不得用无界 socket write 假称非阻塞。
- A3 Runtime 接收端使用有界、默认关闭的 consumer 队列；消费变慢时明确丢弃并记录本地 drop counter，不得反压阻塞 WebSocket 心跳和业务命令。consumer 不持久化日志。
- 不写 Flash/NVS/SD，也不保留跨断线日志 ring buffer。掉线时清空待发队列，重连后重新通过 HardwareHello 配对认证与双侧授权，再重新订阅并按新 stream_id 从当前序号开始；不重放离线期间的旧队列。已知队列丢弃通过计数/gap 报告；断连期间的日志不承诺回补。
- 网络日志是诊断旁路数据，对业务不可用；断线、鉴权失败或订阅拒绝不影响控制功能。A7 的 UART-only logger 保留为独立 fallback。

## 5. 兼容性和最小文件 ownership

1. **Capability 现状：** 当前未证明 `device.logs.read` 存在正式 wire 注册和 Runtime 消费路径，因此 L2 不要求该 capability，也不以它作为订阅前置条件。保留 Host 本地默认关闭 consumer 与设备侧 paired-session/local-log-gate 双重授权。A2 若补证实际 wire 路径，须另交 A0 裁决后方可增加兼容要求。

2. **A2 — SDK bridge 与传输端（既有 ownership）：** 在 `firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h`、`src/desk_sdk_bridge.c`、`src/desk_sdk_websocket.c` 处理新增命令类型、paired-session 授权 gate、WebSocket JSON send、ACK/NACK `data.command_id` 关联与 disconnect 清理。不得用 `desk_sdk_bridge_is_connected()` 作为授权证据，因为它也会把 legacy TCP 连接计为在线。subscribe handler 必须只在当前 WebSocket receive loop dispatch，并绑定该连接上 `HardwareHello` ACK `code == 0` 的成功态。A2 增加 per-connection auth latch：候选设置点为 `desk_sdk_websocket.c::desk_sdk_ws_client_connect_paired()` 中观察到 HardwareHello ACK `code == 0` 后。该 ACK 当前只做 substring 匹配，latch 仅代表协议成功 ACK 已观察到，不代表 TLS/密码学验证过 Runtime 对端身份（当前 `ws://` 明文）。中央清除点为 `ws_transport_close`；并须在 token/auth-policy/成功 pairing metadata replacement 之前主动 invalidate。disconnect、close、reconnect begin、bridge reset 均须清除。置位后仍须检查 paired 状态、有效 64-hex token、完整 pairing metadata 和设备 local log gate（默认关闭）；不能只依赖默认关闭状态可能为 false 的 `s_auth_required`。未知命令现落入 `UNKNOWN_OPERATION`/404，必须新增显式 handler。A2 从 A1 `try_dequeue/take_gap` 消费，在 wire 层严格序列化上述 event/gap，`sequence/uptime_ms/boot_id/stream_id` 必须与 subscribe ACK 和 A1 record 一致。现有 `desk_sdk_ws_client_send_text()` 同步调用 `ws_send_frame()`，可能阻塞；A1 producer/相机回调绝不可调用。A2 必须由独立 log writer task 发送，且 socket write 具有有限 deadline/非阻塞行为；不能无限等待或持有会阻塞语音、心跳、业务命令的共享锁。write deadline 到期应关闭/重置当前 stream、清队列并报告可观测错误；不得无限重试旧日志。队列满由 bounded A1 queue drop-new 并生成 `QUEUE_FULL` gap，不额外设无界 TX queue。验收边界：故障注入阻塞/短写 socket 时，producer enqueue 立即返回且不触发网络调用；worker 写等待有上限；语音任务、WebSocket receive、ping/heartbeat 和业务命令在约定时限内继续运行；A2 报告具体 deadline 数值，并证明故障注入实测不超时。若 `send_text` 无法满足此边界，必须新增有界 writer adapter 后才能宣称符合。对应桥接契约测试归 `tests/sdk_contract/`。A2 不修改 `desk_hardware`、`desk_tclaw`、`firmware/main/src/app_main.c`、产品 Host 核心或 `.target_logging/**/vendor` 中的 pinned SDK 源码。

3. **A1→A2 结构化生产接口（A1/A2 边界）：** A1 在 `desk_hardware/` / `desk_tclaw/` 提供固定记录和 queue API；v1 唯一事件为 `CAMERA_CAPTURE_EMPTY`，`fields={}`，来源为 `desk_watcher_camera_capture()` 收到零长度 7-byte PTL header、重试耗尽后 `rc=-25/out_len=0`。记录字段为 `{sequence, uptime_ms, event_id}`；`event_id` 是 enum（仅 CAMERA_CAPTURE_EMPTY），fields 由事件类型固定为空；`uptime_ms` 是事件观察时的单调运行毫秒数，`sequence` 由 A1 在每个启用状态的事件尝试时先分配，64-bit unsigned 严格递增且不因队列丢弃回退；A2 生成每 boot 不含设备身份的 opaque `boot_id` 并附加当前 stream_id。Subscribe ACK 的 `next_sequence` 必须来自 A1 queue 的 next sequence。A1 固定 queue capacity 为 16 records、每 record 最大 192 bytes；API 为 O(1)、非阻塞 `try_enqueue/try_dequeue/take_gap`，不等待空间、不动态分配、不做网络 I/O。满时 drop-new、推进 sequence、累计连续 dropped range/count；`take_gap` 仅可产生 `reason=QUEUE_FULL`；A2 按序合并 event 与 gap，不能让后续 sequence 越过未发送 gap。无活动订阅/本地 gate 关闭时不得收集事件。断连立即 disable source 并清空 queue；清除的旧 stream 数据不回放、不伪装为 `QUEUE_FULL`，新 subscribe 从 ACK 的 next sequence 开始。TAL/ESP-IDF hook 保留 UART 语义。A1 不修改 `desk_sdk_bridge/` 或 `app_main.c`。

4. **`firmware/main/src/app_main.c` 接线判定：当前不预设必须修改。** 当前启动路径已调用 `desk_hardware_init()` 与 `desk_tclaw_init()`；若 sink 可从 A1 已有初始化/生命周期注册，避免改主入口。若静态/设备证据证明必须在 `app_main.c` 增加 hook 或启动调用，该文件未列入 A1/A2 当前独占路径；须先由 A0 明确归属（建议 A0 处理公共启动/组件注册，或批准一次性窄路径例外给指定 owner），并单独记录相应 CR。不得把该修改默认为 A1 或 A2 的权限。`deploy/edge/` 则已明确归 A3，不需要 ownership 例外；Runtime 部署使用 §7 已批准的锁定版本。

5. **Host consumer 与 Runtime 部署：区分产品与验证用途。** A7 拥有 `tools/verification/` 网络日志采集工具及 `tests/e2e/` 验证；A3 拥有 `host/desk_agent/core/` 产品 consumer（及对应测试）、`deploy/edge/` 安装/服务配置和 Host 生命周期集成。A3 已有默认关闭、有界、显式授权的内存 consumer 与 listener adapter，负责将其接通官方 Runtime managed Application callback；普通 Desktop raw route 和默认 `_ignore_callback` 不得作为日志归档。A3 consumer 必须只接受 `CAMERA_CAPTURE_EMPTY` event 与 `QUEUE_FULL` gap allowlist，未授权不接收，重连 reset queue；不持久化。A2 只维护固件 bridge/设备事件协议，不改产品 Host 核心或 Runtime 安装源码。

   **前置依赖：当前 edge 部署没有安装或启动 pinned official Runtime。** `deploy/edge/install.sh` 安装普通 Host/Companion 并安装 `desk-agent.service`；该 unit 直接运行 `host.desk_agent.main` 普通配置入口。Host 只有在 Runtime 提供 managed-app 环境时，才会进入 `_run_managed_application()` 并创建 `ApplicationContext.from_environment()`。Host 已有默认关闭、有界的内存 consumer（commit `84ded9e`）及 pinned listener adapter（commit `2aefe7a`），但当前 edge 部署仍没有 Runtime managed-app consumer wiring 或 Runtime 部署；因此尚无可运行的受管 Host 消费链路。A3 后续须在 `host/desk_agent/core/` + `deploy/edge/` 将 consumer 接入 managed Application，并单独部署/托管锁定版本官方 Runtime。consumer 当前对 gap 一律拒绝；A3 follow-up 必须将 closed allowlist 限定为 `QUEUE_FULL`，并测试字段范围、序号区间和计数一致性。只有本地显式启用且设备配对授权通过才消费；当前不宣称 TaiShanPi 可用。

   Runtime 候选已由本地 pinned source 核验：v0.1.11，commit SHA `8bc13967a06ef073459be47e930ff43ef37ee8fb`，许可证 Apache-2.0（见 §7）。A0 已批准此版本作为部署锁定版本；部署清单必须固定该版本并保留来源/许可证记录。`deploy/edge/` 已属于 A3 ownership，不需路径例外；如需改 pinned SDK 安装/第三方源码，当前不在 A3/A2 ownership 内，须先提出 A0 窄范围 ownership/path exception CR，明确 tracked path、owner、来源/许可证、升级回合及测试边界；ignored vendor checkout 保持只读。若 Application callback 可通过现有受支持接口接入，则不需要 SDK 源码例外。

6. **A0 — 公共契约与注册：** 批准本 CR 后，A0 更新公共接口/Schema/capability contract；如新增组件需变更公用构建注册，按 AGENTS 由 A0 合入。任何 ownership 例外先记录后实施。

7. A7 为网络采集消费者实现 L2 协议 fixtures/测试与 L3 设备验收；不修改固件生产代码或绕过 A1/A2。既有 UART capture 不删除、不转成网络 fallback；UART 和网络证据分别标注来源、固件 hash、boot_id 与证据等级。

## 6. 验收门槛

### L2 契约与实现测试

- 正例：已配对连接、当前 WebSocket HardwareHello 成功态、paired metadata/token/local gate 与 Host gate 均有效；subscribe ACK `next_sequence` 与 A1 source queue counter 一致；收到 CAMERA event 与 `reason=QUEUE_FULL` gap；ACK/NACK `data.command_id` 对应，sequence 单调，顶层 `request_id` 不依赖。
- 鉴权负例：未配对、错误/已撤销会话、设备 local gate 关闭、Host consumer 未显式授权、重连后沿用旧 latch/订阅均 fail closed，不发日志；仅 legacy TCP 在线也必须拒绝。
- 协议负例：未知版本、未知必需字段、坏 JSON、错误级别、畸形/超长帧、无效序列、错 stream_id 均拒绝或丢弃且不污染后续消息。
- 隐私：构造 pairing code、token、Wi-Fi/API 凭据、用户文本、音频/图像标记等测试值，断言这些数据不会进入设备网络输出或 Host consumer；非 allowlist 消息/字段丢弃。Runtime 明文 LAN 测试按任一同网观察者可见处理。
- 背压/恢复：A1 queue 恰为 16 records，塞满后第 17 项 drop-new、计数/sequence gap 正确，原有 16 项顺序保留；断连清空且不回放，不产生 `DISCONNECTED` gap。TAL/ESP-IDF producer hook 在 log lock/并发条件下固定时延返回，无网络写入、无递归。A2 writer stall/short-write 注入时 producer enqueue 立即返回；专用 worker write 必须在文档/测试记录的有限 deadline 内返回，且语音任务、WebSocket receive/ping/heartbeat、业务命令在约定时限内继续运行。
- 兼容：未实现本扩展的旧设备/Runtime保持旧行为且没有日志流；不要求未经 wire 证实的 capability；Runtime 未接入显式 consumer 时不做归档；A2 subscribe/unsubscribe handler 仍待 source review，未知命令应命中 unknown-type NACK/404，正例验证 ACK/NACK `data.command_id` 关联及重复 subscribe 原 stream 保留。
- 只读：抓取协议记录并证明唯一设备入站操作是 subscribe/unsubscribe，设备状态/动作/配置没有变化。

### L3 真机

在明确记录固件 hash、Runtime 版本、HardwareHello/paired session 与设备/Host 两侧 gate 结果后，通过已配对 WebSocket 收到可解析、脱敏、带序列的真实设备日志；验证过滤、可控背压造成的 gap、断线重连后无旧队列回放并从当前序号恢复，以及 UART 输出对照。不得以 localhost loopback、模拟器或初始化成功替代真机证据。

### L4 集成

持续运行期间同时有语音/控制/日志流，证明日志不影响实时音频与命令；覆盖 Host/设备重启、断网、重新配对、权限撤销和队列满恢复。任何凭据或用户内容泄露均为验收失败。

## 7. 当前证据与待裁决项

- **pinned official Runtime 候选（L1）：** 本地 pinned source 核验版本 v0.1.11，commit SHA `8bc13967a06ef073459be47e930ff43ef37ee8fb`，许可证 Apache-2.0。此为部署锁定候选，不表示已部署或已在 TaiShanPi 验证。
- **A2 静态审计（pinned SDK v0.1.11，L1）：** `watcherobot/protocol.py::build_command()` 生成 `{type, code: 0, data: {..., command_id}}`；`watcherobot/desktop_session.py::_resolve_pending()` 只以 `data.command_id` 匹配 ACK/NACK。订阅控制必须遵循这套封套。
- **A2 pairing/token 静态审计（L1）：** session token 没有 expiry；5-minute TTL 仅用于 pairing code。日志授权是当前已认证 WebSocket session lease，重连需重新 HardwareHello 与 subscribe；不得声称 token 自身五分钟过期。
- **A2 bridge 静态审计（L1）：** `desk_sdk_bridge_is_connected()` 也将 legacy TCP 连接视为在线，不能用于日志授权。必须从当前 WebSocket receive loop 的 HardwareHello ACK `code == 0` 成功态设置 per-connection latch；断开、close、reconnect begin、reset、token replacement、pairing metadata mutation 均清除此 latch。handler 还需校验 paired state、完整 metadata、64-hex token 和默认关闭设备 local gate；不得只读 `s_auth_required`。
- **A2 静态审计（pinned SDK v0.1.11，L1）：** `runtime/daemon/connections/websocket_server.py::_handle_hello()` 解析首个 HardwareHello 并调用 authorizer；`runtime/daemon/runtime.py::_authorize_hardware_hello()` 调用 pairing session；`runtime/daemon/pairing/session.py::DevicePairingSession.connect_device()` 校验当前状态、pair request / daemon ID / token / mode / peer IP 后设置 CONNECTED。该 Runtime 未把 capability 用作权限授权；配对 session token 本身没有 expiry，5-minute TTL 只适用于 pairing code。日志订阅只租用当前已认证 WebSocket session，不宣称 token 自身 5 分钟过期。
- **A2 静态审计（pinned SDK v0.1.11，L1）：** `runtime/daemon/routing/raw.py::RawFrameRouter.route_external()` 对合法 JSON frame 作原样路由；`runtime/daemon/application/bridge.py::LocalWebSocketApplicationBridge` 的缺省 frame callback 为 `_ignore_callback`；没有专用 log consumer/archive API。SDK `watcherobot/robot.py::Robot._on_message()` 对未列入分支的消息不作日志消费。
- **A2 静态审计（pinned SDK v0.1.11，L1）：** `runtime/daemon/connections/websocket_server.py::ExternalWebSocketServer.start()` 未配置 TLS context，`url` 返回 `ws://`；LAN 日志可见。设备身份来自已验证 pairing session，不是 capability。
- **当前设备桥静态证据（L1）：** `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c::desk_sdk_bridge_process_command()` 对未知 type 返回 404 `UNKNOWN_OPERATION` NACK；新订阅必须有显式 handler。`firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c::desk_sdk_ws_client_poll()` 是设备 WebSocket 收帧入口，dispatch / paired-state gate 尚未实现为日志功能。
- **A1 日志入口静态审计（本地 TuyaOpen SDK snapshot，L1；目标硬件未测）：** `.target_logging/a1-currenthead-isolated-build-20261022/TuyaOpenSDK/src/tal_system/src/tal_log.c::PrintLogV()` 在 `tal_mutex_lock(pLogManage->mutex)` 与 `tal_mutex_unlock()` 范围内格式化并同步调用 `__output_logManage_buf()`；网络 sink 若阻塞会延长/死锁全局日志锁。A1 提醒 `esp_log_set_vprintf` hook 需支持并发/重入、无 tag 参数，应先 bounded-format 再 allowlist 解析，hook 内不能再次调用 `ESP_LOG`。计划采用非阻塞固定队列 + worker、drop-new + counter，保留 UART 输出；候选 16×192B + ~2KiB internal stack（约 5KiB）尚未真机测量。
- **日志来源覆盖范围未对账：** TAL/stdout、ESP-IDF logger、UART sink 以及 ROM/bootloader logger 是否分别可捕获、是否重复、是否能保持现有 UART 语义尚未核实。该方案只允许经过 tag/event allowlist 和脱敏的低敏诊断，不等同于转发全部串口/系统日志。
- **A1 CAMERA_CAPTURE_EMPTY 来源审计（L1）：** `firmware/components/desk_hardware/src/desk_watcher_camera.c::desk_watcher_camera_capture()` 在收到零长度 7-byte PTL header 并耗尽 empty-header retry limit 后返回 `rc=-25`，`out_len` 保持 0；这是 `CAMERA_CAPTURE_EMPTY(fields={})` 的真实可观察来源。`INPUT_CHANNEL_INIT_ERROR` 当前无真实 source：音频 init 错误折叠为 generic unavailable，且 `desk_watcher_input` 是 knob/button/touch 输入，不是 MIC/SPEAKER 音频通道；故本 CR v1 延后该事件，待 stage-specific mapping 与范围审查。
- **A3 Host L2 证据：** commit `2aefe7a` 提供集中封装的 `private context._transport` listener adapter，3 项绑定 pinned Runtime SHA 的聚焦测试通过；commit `84ded9e` 提供默认关闭、容量 16、drop-new、仅内存的 bounded consumer。当前 consumer 对所有 `sys.log.gap` 均拒绝，须跟进仅接受 `QUEUE_FULL` 并加正例/负例；未见 managed-app integration 或 Runtime 部署。
- **A3 Host/deployment 静态审计（L1）：** `deploy/edge/install.sh` 安装普通 Host/Companion 并部署 `desk-agent.service`，没有安装或启动 pinned official Runtime；`deploy/edge/desk-agent.service` 直接执行 `python3 -m host.desk_agent.main --config ...`。`host/desk_agent/main.py::_run_managed_application()` 才调用 `ApplicationContext.from_environment()`；该路径依赖 Runtime 注入 managed-app 环境。当前部署没有 managed-app consumer wiring 或 Runtime；尽管 A3 的 bounded consumer 模块已提交，Host/Runtime 部署与托管集成仍是网络日志端到端接收的前置依赖。
- **未实现/未证明：** edge 当前未部署/启动 pinned Runtime；A3 bounded consumer 模块尚未接入 managed app，且 gap allowlist 尚未放行 `QUEUE_FULL`；pinned Runtime 无专用 log consumer；A1 producer queue/sink 未实现；A2 当前 subscribe/unsubscribe handler 仅在 dirty source、待 review，event/gap sender 与 bounded writer 未实现；当前 WebSocket 无 TLS；目标镜像上的 logger hook、内存预算、设备端脱敏、真机网络接收/恢复/性能均未验证。
- **A2 dirty source / review evidence (L2 source only):** 4 个未提交文件 `firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h`、`src/desk_sdk_bridge.c`、`src/desk_sdk_websocket.c`、`tests/sdk_contract/test_official_sdk_runtime_compat.py` 含 subscribe/unsubscribe handler 与定向契约用例；这些仅作设计输入，尚未合入、尚无 producer/send、不可作为真机证据。ACK `next_sequence` 仍需切换到 A1 source counter。
- **剩余实现项：** (a) A1 queue/sink、logger hook、脱敏及 UART 保真尚未实现；唯一 v1 event 源已证实为 `CAMERA_CAPTURE_EMPTY(fields={})`；(b) A2 handler 仍待 review，须消费 A1 event/gap API、实现 wire serialization 和 bounded writer；其 `send_text()` 可能阻塞，必须通过 stall/short-write 测试与有限 deadline 验收；(c) A3 bounded consumer 已提交（`84ded9e`），但应将 gap reason allowlist 从“全部拒绝”改为仅 `QUEUE_FULL` 并覆盖正/负测试；managed-app wiring 与 Runtime deployment 尚未完成。L3 前须测量候选约 5 KiB 预算、审计 logger source 覆盖并确认目标固件 gate。

**A0 Decision Log — 2026-10-22：** 批准 CR-009 进入有限 L2 实现阶段；仅允许本机/模拟构建、契约 fixtures 与集成验证，不批准实机验证、部署完成或产品交付宣称。范围固定为： (1) Runtime pin 为 watcherobot v0.1.11、SHA `8bc13967a06ef073459be47e930ff43ef37ee8fb`、Apache-2.0；(2) 仅传结构化枚举 `event_id` 与严格有界低敏 `fields`，不传原始日志或自由文本，不持久化；(3) A1 仅在 `desk_hardware/` / `desk_tclaw/` 提供 queue/sink API，A2 在 `desk_sdk_bridge/` 实现显式 paired command handler/发送，A3 在 `host/desk_agent/core/` + `deploy/edge/` 托管 Runtime/managed app 并实现默认关闭 bounded consumer，A0维护公开接口/schema，A7维护 L2 fixtures；(4) Runtime 无 TLS，LAN 仅允许低敏数据；候选约 5 KiB 预算必须在 L3 前测量；A2 审计补充：`desk_sdk_bridge_is_connected()` 包含 legacy TCP 在线态，不得作授权依据；仅接受当前 WebSocket HardwareHello ACK code=0 成功态的 per-connection auth latch，所有连接/配对元数据变更均须撤销。L2 不要求未经证实的 `device.logs.read` capability。A1 已核实 `CAMERA_CAPTURE_EMPTY(fields={})` 的单一初始事件来源；`INPUT_CHANNEL_INIT_ERROR` 延后至音频 init 具备 stage-specific mapping 并完成范围审查。command_id 为 `uuid.uuid4().hex`，通过 `watcherobot.protocol.build_command()` 封套；单 WebSocket 单活动 stream，重复 subscribe NACK `ALREADY_SUBSCRIBED` 并保留原 stream。HardwareHello ACK latch 仅表明收到 code=0 的协议 ACK，不能证明 `ws://` 对端密码学身份。A2 于 2026-10-22 完成窄范围 lifecycle generation guard 和 4 项定向 L2 测试；此结果关闭 auth-latch seam 前置条件，可开始有限 L2 wire 实现，但不关闭全 socket 并发或 L3/L4 安全风险。

**A1/A2/A3 wire 对账：** 当前 A2 subscribe ACK 已使用 `stream_id`/`boot_id`/`next_sequence`，与 v1 event schema 形状可对齐；尚无 producer/send。A3 consumer 验证 CAMERA event 的 required fields 与 v1 schema 一致，但当前拒绝所有 gap；follow-up 只放行 `QUEUE_FULL`。A2 还须让 ACK `next_sequence` 使用 A1 source counter，并实现有限 writer。

**实现前 CR 子项：** (a) A1 已核实唯一初始事件 `CAMERA_CAPTURE_EMPTY(fields={})` 的源码条件；音频 init 事件延后，待其有 stage-specific mapping 并完成范围审查。(b) A2 per-connection auth-latch lifecycle/concurrency 子项已完成：connect 成功 ACK gate、close 清除、元数据更新撤销、generation guard 和定向源码测试；ACK 不可称为密码学认证，未覆盖的并发 socket API 仍需后续审计。(c) A2 subscribe/unsubscribe handler 在 dirty source 中已有 duplicate `ALREADY_SUBSCRIBED` NACK/原 stream 保留，但仍待 review；A1→A2 event/gap producer-send interface 与 bounded writer 尚未实现。(d) A3 listener adapter/SHA 测试已完成（commit `2aefe7a`，3 focused L2 tests），bounded consumer 已提交（`84ded9e`）；gap allowlist follow-up、managed-app integration 和 Runtime deployment 尚未完成。L2 wire handler 当前在 dirty source 中待 review；producer/send interface、bounded writer 与 A3 gap allowlist 对齐后才能构成可验收的 event path。

**L3/L4 尚未通过：** 真实 Runtime service 在目标 Host 的部署、managed-app env 注入及 paired device 网络收发尚未验证；真实设备资源预算、日志源覆盖、TLS 缺失下的隐私边界和 UART 保真也未测。

**实机前置条件：** 完成上述实现前 CR 子项并通过 L2 实现/模拟验证；A3 在目标部署启动锁定 Runtime 并提供 managed-app environment；A1/A2 完成目标固件 sink/handler/配对 gate 与 bounded writer；A3 consumer 显式开启且双侧授权；测量资源预算并确认 logger source 覆盖、隐私过滤和 UART 保真后，才可进入 L3/L4 真机验收。

**结论：** 提案选择已认证的 L-09 WebSocket 为唯一允许的候选日志通道，明确拒绝未认证旁路；L2 实现获批，但网络日志能力仍未实现；真实 Runtime service/paired device 的 L3/L4 和产品交付未批准。
