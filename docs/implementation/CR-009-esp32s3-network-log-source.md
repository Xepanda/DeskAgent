# CR-009：ESP32-S3 已配对网络日志订阅与流

- **提出方：** A0（基于 A7 网络日志源缺口记录）
- **提出日期：** 2026-10-22
- **状态：** **PROPOSED — 契约草案；尚无实现或网络日志能力**
- **关联：** `docs/acceptance/network-log-source-gap-2026-10-22.md`、`docs/implementation/interfaces.md` §5 L-09 / §6.4、`firmware/components/desk_sdk_bridge/`
- **预期 owner：** A0 契约批准；A1 设备日志源与发送；A2 SDK Runtime / 协议桥互通；A7 采集器和验收

## 1. 目标与现状

目标是在已配对、已认证的设备连接上按需订阅 ESP32-S3 诊断日志。**不得新建未认证的 TCP/UDP/HTTP 服务或其他旁路，也不得借用 Tuya 云端语音会话上传日志。**

当前仓库没有网络日志订阅、日志流消息或固件日志 sink。`tools/verification/capture_esp32s3_logs.py` 当前只被动读取 UART。L-09 描述的官方 Runtime 拓扑为 Runtime 在 Host 侧监听 LAN WebSocket、ESP32 设备主动连接；配对发现使用 UDP 37021，现有自有 TCP 37020 是另一条控制桥。上述控制桥、配对 UDP 和已存在的普通业务事件都不是日志源。

本 CR 提议只在 L-09 的已配对 WebSocket 应用通道上增加**协商后、按需、只读**日志订阅。它不更改 TCP 37020、UDP 37021、WSPK 图像帧或既有命令含义。此文档是待评审的契约草案；通过源码搜索或本 CR 本身不能证明网络日志已实现。

## 2. 通道、方向与授权

1. **连接方向：** ESP32-S3 仍主动连接配对请求中指定的 Host WebSocket Runtime。Runtime 在该既有连接上发送订阅/取消订阅请求；设备只在该连接上返回确认与日志事件。不得创建设备侧日志 listener、额外端口或广播日志。
2. **发现：** 沿用 L-09 配对流程发现 Runtime；UDP 37021 只用于既有配对请求/响应，配对完成后不承载日志订阅或数据。
3. **配对身份状态：** pinned Runtime 的实际认证入口是 WebSocket 首个 `HardwareHello`。`ExternalWebSocketServer._handle_hello()` 调用 Runtime 的 `_authorize_hardware_hello()`，再由 `DevicePairingSession.connect_device()` 校验当前配对状态、`pair_request_id`、`daemon_instance_id`、`session_token`、`mode` 和预期 peer IP，成功后才进入 `CONNECTED`。不要把设备发送的 `sys.client.hello` capability 列表误作配对凭据或权限授权。
4. **双侧授权门：** pinned Runtime 当前没有把 capability 当授权策略。未来设备应在已认证且已连接的 paired WebSocket handler 内检查配对/会话仍有效、已连接状态和本地 log-read gate；Runtime/Application 侧必须再检查显式诊断 consumer 已启用且当前连接角色/配对状态为已授权设备，才转发或保存日志。单独的 `device.logs.read` capability 只用于兼容性发现，不是授权。任一侧未通过时 fail closed。不得在每条日志内携带 session token，也不得把 pairing code、session token 或认证头写入日志。
5. **隔离：** log subscription 是设备只读诊断操作，不得执行命令信封中的业务操作。Runtime 侧不得默认转发给普通 Desktop 消费者或公网；Application 默认 callback 也不是日志归档授权。
6. **传输保护边界：** pinned Runtime 使用明文 `ws://`，没有 TLS。所有经 LAN 传输的日志都视为 LAN 可见；只允许严格 allowlist、已脱敏、低敏诊断项。不得传输任意原始 ESP-IDF/printf 文本、个人内容、凭据、音视频、用户输入、业务参数或可识别设备的长期标识。此 CR 不提供链路机密性。

## 3. 版本化消息建议

所有新 `sys.log.*` 消息沿用已认证 WebSocket 上的**单个 JSON WebSocket text message**；WebSocket message boundary 是 framing，不附加 TCP 37020 的换行帧，不使用 UDP，也不使用 WSPK binary frame。每个 `sys.log.*` 消息必须带 `log_schema_version: "1.0.0"`；未知版本或未知必需字段不得按日志猜测解析。下列 `type` 和字段是本 CR 的提案，须由 A0/A1/A2 评审后冻结。`sys.log.event/gap` 使用本 CR 的专用日志事件结构，避免把诊断流伪装成已有业务 `EventEnvelope`。

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

`min_level` 枚举 `ERROR | WARN | INFO | DEBUG`，缺省 `WARN`；`components` 是显式 allowlist 的组件 tag 列表，空列表表示设备批准的诊断组件全集，不表示未过滤任意串流。设备返回的 `sys.ack` / `sys.nack` 必须在 `data.command_id` 原样关联请求 `data.command_id`，符合 SDK pending-command matcher；不得新设顶层 `request_id` 关联。成功 ACK 的 `data` 含 `type`, `command_id`, `accepted`, `stream_id`, `boot_id`, 当前 `next_sequence` 和协商 filter；NACK 在 `data.command_id` 外，还须把稳定错误标识放在 SDK可读取的 `data.error`，不能只写顶层 `error`。请求只从确认时的序列开始，不补发先前日志。

Runtime → 设备取消订阅也必须用 `build_command()` 封套：`{"type":"sys.log.unsubscribe","code":0,"data":{"command_id":"...","log_schema_version":"1.0.0","stream_id":"..."}}`。设备以 `data.command_id` 关联的 `sys.ack` 确认停止。会话断开时设备自动停止订阅，不保留订阅授权到新连接。

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
    "message": "capture returned hardware error",
    "fields": {}
  }
}
```

`sequence` 在单次 `boot_id` 内单调递增；序号缺口代表一个或多个事件丢失，序号不因订阅、重连或 filter 改变而复用。`uptime_ms` 必填，避免未校准设备时钟造成假 UTC；设备时钟可信时才附加 RFC3339 `timestamp_utc`，否则省略。设备可发 `sys.log.gap` 控制事件，报告 `first_dropped_sequence`、`last_dropped_sequence`、`dropped_count` 和 `reason`；接收端也必须能从 sequence gap 发现丢失。gap 通知本身不回收或重用丢失序号。

SDK v0.1.11 没有专用 log event/consumer API。`RawFrameRouter.route_external()` 会把设备合法 JSON frame 原样路由到活动的 Application channel；`LocalWebSocketApplicationBridge` 再调用 `on_frame`，其缺省 callback 是 `_ignore_callback`。若无活动 Application，则原始 frame 路由到 Desktop；不得把此默认 callback 或通用 Desktop 路由称为安全归档。A2 必须增加显式、有界的 log consumer/handler，并在认证/授权 gate 后仅把 `sys.log.event/gap` 交给它；当前 PR 若只接入临时 Application handler，也必须明确仅当前实现范围，不得宣称 SDK 已提供持久或安全日志 API。

日志不是任意业务事件转发：媒体、ASR 转写、用户输入、工具参数/结果、提醒内容、HA 实体值、Codex 文件内容等均不得进入该流。设备只发送经过 A1 审核的严格 allowlist 组件和低敏结构化诊断项；`fields` 只允许已批准的有界标量字段，不允许任意嵌套对象或任意日志文本。禁止把原始 ESP-IDF/printf/UART payload 送入网络流。设备端必须先做字段级脱敏，Host consumer 再二次脱敏；禁止把原始 payload 默认持久化，诊断存储只保留 allowlist 内的脱敏事件和明确的丢失统计。

## 4. 级别、隐私与资源边界

- 日志等级限 `ERROR/WARN/INFO/DEBUG`；不支持 `VERBOSE` 网络订阅。缺省 `WARN`，`DEBUG` 需显式本地订阅，固件重启后不恢复。
- 仅允许低敏、固定/结构化诊断值；必须删除/遮蔽 pairing code、session token、Wi-Fi 密码、云端/API/HA/Codex 凭据、Authorization 字段、密钥、音视频内容及用户可识别内容。对未知字段、未审核组件/消息默认丢弃；禁止把敏感原值仅依赖 Host 脱敏。LAN 明文可观察性下，即使脱敏也不得传送自由文本、用户内容或高敏运行时数据。
- `boot_id` 使用每次启动生成的非凭据、非长期设备标识随机值；不输出完整 MAC、序列号或固定设备身份。时间、tag、message、fields 长度均设上限，非法 UTF-8/过大事件丢弃并计数。
- 日志入口必须是固定时延、非阻塞 sink，由后台 worker 做队列消费、协议编码和 WebSocket 发送；sink 内禁止网络 I/O、阻塞等待、动态大块分配及再次调用 `ESP_LOG`（避免递归）。它不得改变既有 UART 输出/格式/级别。
- TAL `PrintLogV()` 在持有 global log mutex 时同步执行输出 sink；故 TAL sink 只能把已格式化、经 allowlist/脱敏后的短诊断复制到固定队列后立即返回。ESP-IDF `esp_log_set_vprintf` hook 没有 tag 参数且可能并发/重入：需先以有界 buffer 格式化，再解析审核过的 tag/event；必须可重入/并发安全，不能在 hook 内调用 `ESP_LOG`。
- 资源限制仍是**待 A0/A1 实测裁决**，不冻结 64 KiB/4096-byte 默认值。当前可供评估的候选是 16 × 192-byte 固定队列（3072 bytes）+ 约 2 KiB internal worker stack，合计约 5 KiB 目标预算，**未经目标板测量，不是已验证预算或强制实现常量**。A1 需测队列高水位、worker stack HWM、heap/PSRAM 和控制/语音干扰，再由 A0/A1冻结可接受值。
- 队列满时采用 **drop-new + counter**，保留已排队较早事件；日志序号继续推进并通过 `sys.log.gap` 报告丢弃范围。断线立即丢弃并清空旧队列；重连不回放断线期间旧日志，重新订阅只从 ACK 返回的当前序号开始。Runtime 接收端也须有界；消费变慢不得阻塞 WebSocket 心跳/业务命令，丢弃需可观测。
- Runtime 接收端也使用有界队列；消费变慢时暂停/降低日志读取或明确丢弃并记录 gap，不得反压阻塞 WebSocket 心跳和业务命令。
- 不写 Flash/NVS/SD，也不保留跨断线日志 ring buffer。掉线时清空待发队列，重连后重新通过配对认证与双侧授权，再按新 stream_id 从当前序号开始；不重放离线期间的旧队列。已知队列丢弃通过计数/gap 报告；断连期间的日志不承诺回补。
- 网络日志是诊断旁路数据，对业务不可用；断线、鉴权失败或订阅拒绝不影响控制功能。A7 的 UART-only logger 保留为独立 fallback。

## 5. 兼容性和 owner 实现点

1. `device.logs.read` 仅作为能力兼容提示，不构成权限授权；旧 Runtime 忽略未知 capability，旧设备不声明它。Runtime 未观察到能力时不发订阅；即使观察到，仍必须由本地显式开启的授权诊断 consumer 决定是否订阅。设备端 `sys.log.subscribe/unsubscribe` handler 也必须校验当前 paired session、已连接状态和设备侧 log-read gate。两侧任一 gate 关闭时均不传送日志。
2. A1 在固件 SDK bridge 的已配对 WebSocket receive/send path 增加 `sys.log.subscribe/unsubscribe` **显式 handler**、订阅状态、低敏 allowlist logger sink、固定有界队列、序列/gap 生成和设备端脱敏。当前 bridge 未知 type 落入 `UNKNOWN_OPERATION`/404 NACK 分支；只在此分支之前新增明确处理，不可误将其视为已实现。不能将 `printf`/任意 ESP-IDF 输出无条件转发；不得增加新端口或更改 Tuya 音频会话。
3. A2 核验并扩展 pinned official Runtime 的路由和显式有界 consumer：下行 subscribe/unsubscribe 使用 `build_command()` 的 `{type, code, data:{command_id,...}}` 封套；ACK/NACK 在 `data.command_id` 精确关联。上行 `sys.log.event/gap` 是合法 JSON frame，沿现有 raw-frame route 到 Application callback，但 SDK 无专用日志 API，且 callback 默认 `_ignore_callback`；A2 必须新增有界 handler/consumer并应用 Host 显式授权 gate。不得把默认 callback 或 Desktop 路由作为安全归档。若当前 PR 只提供临时、易失的 Application handler，需限定其能力，不得宣称持久归档。
4. A7 为网络采集消费者实现 L2 协议 fixtures/测试与 L3 设备验收；不修改固件协议或绕过 A1/A2。A0 批准并同步本 CR 后，相关 schema/接口目录及能力版本由 A0/A2 按 ownership 单独更新。
5. 既有 UART capture 不删除、不转成网络 fallback；UART 和网络证据分别标注来源、固件 hash、boot_id 与证据等级。

## 6. 验收门槛

### L2 契约与实现测试

- 正例：已配对连接、设备能力提示存在、设备与 Host 两侧 gate 均打开、订阅过滤正确、ACK/NACK 的 `data.command_id` 对应、序号递增、有效 event/gap 可解析；顶层 `request_id` 不被依赖。
- 鉴权负例：未配对、错误/已撤销会话、设备未声明 `device.logs.read`、设备侧 gate 关闭、Host consumer 未显式授权、重连后沿用旧会话均 fail closed，不发日志。
- 协议负例：未知版本、未知必需字段、坏 JSON、错误级别、畸形/超长帧、无效序列、错 stream_id 均拒绝或丢弃且不污染后续消息。
- 隐私：构造 pairing code、token、Wi-Fi/API 凭据、用户文本、音频/图像标记等测试值，断言这些数据不会进入设备网络输出或 Host consumer；非 allowlist 消息/字段丢弃。Runtime 明文 LAN 测试按任一同网观察者可见处理。
- 背压/恢复：TAL/ESP-IDF producer hook 在 log lock/并发条件下固定时延返回，无网络写入、无递归；队列到上限时 drop-new 计数并报告 gap，控制和心跳可继续；断线清空队列且不回放，重新配对/授权后从当前序号新建 stream。
- 兼容：无 capability 的旧设备/Runtime保持旧行为且没有日志流；Runtime 未接入显式 consumer 时不做归档；固件未实现新 handler 时预期命中 unknown-type NACK/404，新增 handler 后验证正常 ACK/NACK `data.command_id` 关联。
- 只读：抓取协议记录并证明唯一设备入站操作是 subscribe/unsubscribe，设备状态/动作/配置没有变化。

### L3 真机

在明确记录设备身份类别（不暴露唯一标识）、固件 hash、Runtime 版本、设备能力提示及设备/Host 两侧 gate 结果后，通过已配对 WebSocket 收到可解析、脱敏、带序列的真实设备日志；验证过滤、可控背压造成的 gap、断线重连后无旧队列回放并从当前序号恢复，以及 UART 输出对照。不得以 localhost loopback、模拟器或初始化成功替代真机证据。

### L4 集成

持续运行期间同时有语音/控制/日志流，证明日志不影响实时音频与命令；覆盖 Host/设备重启、断网、重新配对、权限撤销和队列满恢复。任何凭据或用户内容泄露均为验收失败。

## 7. 当前证据与待裁决项

- **A2 静态审计（pinned SDK v0.1.11，L1）：** `watcherobot/protocol.py::build_command()` 生成 `{type, code: 0, data: {..., command_id}}`；`watcherobot/desktop_session.py::_resolve_pending()` 只以 `data.command_id` 匹配 ACK/NACK。订阅控制必须遵循这套封套。
- **A2 静态审计（pinned SDK v0.1.11，L1）：** `runtime/daemon/connections/websocket_server.py::_handle_hello()` 解析首个 HardwareHello 并调用 authorizer；`runtime/daemon/runtime.py::_authorize_hardware_hello()` 调用 pairing session；`runtime/daemon/pairing/session.py::DevicePairingSession.connect_device()` 校验当前状态、pair request / daemon ID / token / mode / peer IP 后设置 CONNECTED。该 Runtime 未把 capability 用作权限授权。
- **A2 静态审计（pinned SDK v0.1.11，L1）：** `runtime/daemon/routing/raw.py::RawFrameRouter.route_external()` 对合法 JSON frame 作原样路由；`runtime/daemon/application/bridge.py::LocalWebSocketApplicationBridge` 的缺省 frame callback 为 `_ignore_callback`；没有专用 log consumer/archive API。SDK `watcherobot/robot.py::Robot._on_message()` 对未列入分支的消息不作日志消费。
- **A2 静态审计（pinned SDK v0.1.11，L1）：** `runtime/daemon/connections/websocket_server.py::ExternalWebSocketServer.start()` 未配置 TLS context，`url` 返回 `ws://`；LAN 日志可见。设备身份来自已验证 pairing session，不是 capability。
- **当前设备桥静态证据（L1）：** `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c::desk_sdk_bridge_process_command()` 对未知 type 返回 404 `UNKNOWN_OPERATION` NACK；新订阅必须有显式 handler。`firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c::desk_sdk_ws_client_poll()` 是设备 WebSocket 收帧入口，dispatch / paired-state gate 尚未实现为日志功能。
- **A1 日志入口静态审计（本地 TuyaOpen SDK snapshot，L1；目标硬件未测）：** `.target_logging/a1-currenthead-isolated-build-20261022/TuyaOpenSDK/src/tal_system/src/tal_log.c::PrintLogV()` 在 `tal_mutex_lock(pLogManage->mutex)` 与 `tal_mutex_unlock()` 范围内格式化并同步调用 `__output_logManage_buf()`；网络 sink 若阻塞会延长/死锁全局日志锁。A1 提醒 `esp_log_set_vprintf` hook 需支持并发/重入、无 tag 参数，应先 bounded-format 再 allowlist 解析，hook 内不能再次调用 `ESP_LOG`。计划采用非阻塞固定队列 + worker、drop-new + counter，保留 UART 输出；候选 16×192B + ~2KiB internal stack（约 5KiB）尚未真机测量。
- **日志来源覆盖范围未对账：** TAL/stdout、ESP-IDF logger、UART sink 以及 ROM/bootloader logger 是否分别可捕获、是否重复、是否能保持现有 UART 语义尚未核实。该方案只允许经过 tag/event allowlist 和脱敏的低敏诊断，不等同于转发全部串口/系统日志。
- **A7 缺口记录证据（工具层 L2、有限 UART L3）：** 当前 logger 只读取 UART；未见网络日志 stream/sink。
- **未实现/未证明：** pinned Runtime 无专用 log consumer；Host capability 不授权；A1 未接入新 command handler/sink；A2 未加有界 consumer；当前 WebSocket 无 TLS；目标镜像上的 logger hook、内存预算、设备端脱敏、真机网络接收/恢复/性能均未验证。
- **A0/A1/A2 合入前必须确认：** Runtime/Application consumer 如何有界消费并区分已授权设备；订阅权限开关默认关闭；设备侧 handler 如何绑定实际 authenticated paired state；`boot_id` 熵源和候选 ~5KiB 预算/事件尺寸是否符合实测资源；logger hook 的重入与 UART 输出保真；各日志源覆盖/重复关系；pairing token 生命周期对长连接的影响。任一不满足须先修订 CR，不得发明隐式行为。

**结论：** 提案选择已认证的 L-09 WebSocket 为唯一允许的候选日志通道，明确拒绝未认证旁路；其网络日志能力仍为未实现/未验证。A0 批准前不得开始协议实现或将能力宣称为可用。
