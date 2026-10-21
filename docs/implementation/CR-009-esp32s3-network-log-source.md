# CR-009：ESP32-S3 已配对网络日志订阅与流

- **提出方：** A0（基于 A7 网络日志源缺口记录）
- **提出日期：** 2026-10-22
- **状态：** **APPROVED — 有限 L2 范围；实现暂缓，待完成授权 seam 源码测试与 A3 listener adapter/SHA 测试**
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

`min_level` 枚举 `ERROR | WARN | INFO | DEBUG`，缺省 `WARN`；`components` 必须是已批准 allowlist 的子集。每条 WebSocket connection 最多有一个活动 stream；已有活动 stream 时重复 subscribe 必须返回 `sys.nack`，`data.error` 为 `ALREADY_SUBSCRIBED`，`data.command_id` 关联新请求，并保留原 stream 不变；禁止创建第二个 producer/queue consumer。Host 生成 `command_id = uuid.uuid4().hex`，并通过 pinned SDK `watcherobot.protocol.build_command()` 构造 `{type, code, data}` envelope。设备返回的 `sys.ack` / `sys.nack` 必须在 `data.command_id` 原样关联请求；不得新设顶层 `request_id`。成功 ACK 的 `data` 含 `type`, `command_id`, `accepted`, `stream_id`, `boot_id`, 当前 `next_sequence` 和协商 filter；NACK 将稳定错误标识放在 SDK 可读取的 `data.error`。请求只从确认时的序列开始，不补发先前日志。

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

`sequence` 在单次 `boot_id` 内单调递增；序号缺口代表一个或多个事件丢失，序号不因订阅、重连或 filter 改变而复用。L2 初版候选仅包含两个事件，schema 暂定且须 A1 核实来源和 channel 映射后由 A0 冻结：`CAMERA_CAPTURE_EMPTY` 的 `fields` 必须为空对象；`INPUT_CHANNEL_INIT_ERROR` 的 `fields` 必须且只能包含 `channel`（封闭枚举 `MIC | SPEAKER`）。event_id 本身表达错误类别，不携带原始错误码。其他 event_id 延后新增 CR/裁决。`uptime_ms` 必填，避免未校准设备时钟造成假 UTC；设备时钟可信时才附加 RFC3339 `timestamp_utc`，否则省略。设备可发 `sys.log.gap` 控制事件，报告 `first_dropped_sequence`、`last_dropped_sequence`、`dropped_count` 和封闭枚举 `reason`；接收端也必须能从 sequence gap 发现丢失。gap 通知本身不回收或重用丢失序号。

SDK v0.1.11 没有专用 log event/consumer API。`RawFrameRouter.route_external()` 会把设备合法 JSON frame 原样路由到活动的 Application channel；`LocalWebSocketApplicationBridge` 再调用 `on_frame`，其缺省 callback 是 `_ignore_callback`。若无活动 Application，则原始 frame 路由到 Desktop；不得把此默认 callback 或通用 Desktop 路由称为安全归档。A2 必须增加显式、有界的 log consumer/handler，并在认证/授权 gate 后仅把 `sys.log.event/gap` 交给它；当前 PR 若只接入临时 Application handler，也必须明确仅当前实现范围，不得宣称 SDK 已提供持久或安全日志 API。

日志不是任意业务事件转发：媒体、ASR 转写、用户输入、工具参数/结果、提醒内容、HA 实体值、Codex 文件内容等均不得进入该流。设备只发送经过 A1 审核的严格 allowlist 组件和低敏结构化诊断项；`fields` 只允许已批准的有界标量字段，不允许任意嵌套对象或自由文本；事件必须由审核过的枚举 `event_id` 表示，并仅携带该事件 schema 允许的有界 scalar `fields`（仅数值、布尔值或封闭枚举，不允许自由字符串）。禁止把原始 ESP-IDF/printf/UART payload 送入网络流；不持久化任何日志事件或原始 payload。设备端先做字段级脱敏，Host consumer 再二次脱敏。

## 4. 级别、隐私与资源边界

- 日志等级限 `ERROR/WARN/INFO/DEBUG`；不支持 `VERBOSE` 网络订阅。缺省 `WARN`，`DEBUG` 需显式本地订阅，固件重启后不恢复。
- 仅允许低敏、固定/结构化诊断值；必须删除/遮蔽 pairing code、session token、Wi-Fi 密码、云端/API/HA/Codex 凭据、Authorization 字段、密钥、音视频内容及用户可识别内容。对未知字段、未审核组件/消息默认丢弃；禁止把敏感原值仅依赖 Host 脱敏。LAN 明文可观察性下，即使脱敏也不得传送自由文本、用户内容或高敏运行时数据。
- `boot_id` 使用每次启动生成的非凭据、非长期设备标识随机值；不输出完整 MAC、序列号或固定设备身份。时间、tag、event_id、fields 长度均设上限，非法 UTF-8/过大事件丢弃并计数。
- 日志入口必须是固定时延、非阻塞 sink，由后台 worker 做队列消费、协议编码和 WebSocket 发送；sink 内禁止网络 I/O、阻塞等待、动态大块分配及再次调用 `ESP_LOG`（避免递归）。它不得改变既有 UART 输出/格式/级别。
- TAL `PrintLogV()` 在持有 global log mutex 时同步执行输出 sink；故 TAL sink 只能把已格式化、经 allowlist/脱敏后的短诊断复制到固定队列后立即返回。ESP-IDF `esp_log_set_vprintf` hook 没有 tag 参数且可能并发/重入：需先以有界 buffer 格式化，再解析审核过的 tag/event；必须可重入/并发安全，不能在 hook 内调用 `ESP_LOG`。
- 资源限制：候选 16 × 192-byte 固定队列（3072 bytes）+ 约 2 KiB internal worker stack，合计约 5 KiB。该预算未在目标板测量；A1 必须在进入 L3 前测量队列高水位、worker stack HWM、heap/PSRAM 和控制/语音干扰，超预算时先回到 A0 裁决。
- 队列满时采用 **drop-new + counter**，保留已排队较早事件；日志序号继续推进并通过 `sys.log.gap` 报告丢弃范围。断线立即丢弃并清空旧队列；重连不回放断线期间旧日志，重新订阅只从 ACK 返回的当前序号开始。Runtime 接收端也须有界；消费变慢不得阻塞 WebSocket 心跳/业务命令，丢弃需可观测。
- Runtime 接收端也使用有界、默认关闭的 consumer 队列；消费变慢时暂停/降低日志读取或明确丢弃并记录 gap，不得反压阻塞 WebSocket 心跳和业务命令。consumer 不持久化日志。
- 不写 Flash/NVS/SD，也不保留跨断线日志 ring buffer。掉线时清空待发队列，重连后重新通过 HardwareHello 配对认证与双侧授权，再重新订阅并按新 stream_id 从当前序号开始；不重放离线期间的旧队列。已知队列丢弃通过计数/gap 报告；断连期间的日志不承诺回补。
- 网络日志是诊断旁路数据，对业务不可用；断线、鉴权失败或订阅拒绝不影响控制功能。A7 的 UART-only logger 保留为独立 fallback。

## 5. 兼容性和最小文件 ownership

1. **Capability 现状：** 当前未证明 `device.logs.read` 存在正式 wire 注册和 Runtime 消费路径，因此 L2 不要求该 capability，也不以它作为订阅前置条件。保留 Host 本地默认关闭 consumer 与设备侧 paired-session/local-log-gate 双重授权。A2 若补证实际 wire 路径，须另交 A0 裁决后方可增加兼容要求。

2. **A2 — SDK bridge 与传输端（既有 ownership）：** 在 `firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h`、`src/desk_sdk_bridge.c`、`src/desk_sdk_websocket.c` 处理新增命令类型、paired-session 授权 gate、WebSocket JSON send、ACK/NACK `data.command_id` 关联与 disconnect 清理。不得用 `desk_sdk_bridge_is_connected()` 作为授权证据，因为它也会把 legacy TCP 连接计为在线。subscribe handler 必须只在当前 WebSocket receive loop dispatch，并绑定该连接上 `HardwareHello` ACK `code == 0` 的成功态。A2 增加 per-connection auth latch：候选设置点为 `desk_sdk_websocket.c::desk_sdk_ws_client_connect_paired()` 中观察到 HardwareHello ACK `code == 0` 后。该 ACK 当前只做 substring 匹配，latch 仅代表协议成功 ACK 已观察到，不代表 TLS/密码学验证过 Runtime 对端身份（当前 `ws://` 明文）。中央清除点为 `ws_transport_close`；并须在 token/auth-policy/成功 pairing metadata replacement 之前主动 invalidate。disconnect、close、reconnect begin、bridge reset 均须清除。置位后仍须检查 paired 状态、有效 64-hex token、完整 pairing metadata 和设备 local log gate（默认关闭）；不能只依赖默认关闭状态可能为 false 的 `s_auth_required`。未知命令现落入 `UNKNOWN_OPERATION`/404，必须新增显式 handler。对应桥接契约测试归 `tests/sdk_contract/`。A2 不修改 `desk_hardware`、`desk_tclaw`、`firmware/main/src/app_main.c`、产品 Host 核心或 `.target_logging/**/vendor` 中的 pinned SDK 源码。

3. **A1 — 日志源与有界 producer（既有 ownership）：** 在 `firmware/components/desk_hardware/` 新增/维护低敏 allowlist 日志 sink、脱敏和固定有界队列；优先接入现有 `desk_hardware_init()` 或 A1 自有 `desk_tclaw/` 初始化路径，TAL/ESP-IDF producer hook 只做固定时延 enqueue，UART 输出语义保持不变。通过 A2 提供的接口消费队列/发送。A1 不修改 `desk_sdk_bridge/` 或 `app_main.c`。

4. **`firmware/main/src/app_main.c` 接线判定：当前不预设必须修改。** 当前启动路径已调用 `desk_hardware_init()` 与 `desk_tclaw_init()`；若 sink 可从 A1 已有初始化/生命周期注册，避免改主入口。若静态/设备证据证明必须在 `app_main.c` 增加 hook 或启动调用，该文件未列入 A1/A2 当前独占路径；须先由 A0 明确归属（建议 A0 处理公共启动/组件注册，或批准一次性窄路径例外给指定 owner），并单独记录相应 CR。不得把该修改默认为 A1 或 A2 的权限。`deploy/edge/` 则已明确归 A3，不需要 ownership 例外；Runtime 部署使用 §7 已批准的锁定版本。

5. **Host consumer 与 Runtime 部署：区分产品与验证用途。** A7 拥有 `tools/verification/` 网络日志采集工具及 `tests/e2e/` 验证；A3 拥有 `host/desk_agent/core/` 产品 consumer（及对应测试）、`deploy/edge/` 安装/服务配置和 Host 生命周期集成。A3 负责新增默认关闭、有界、显式授权的产品 consumer，并接通由官方 Runtime 提供的 managed Application callback；普通 Desktop raw route 和默认 `_ignore_callback` 不得作为日志归档。A2 只维护固件 bridge/设备事件协议，不改产品 Host 核心或 Runtime 安装源码。

   **前置依赖：当前 edge 部署没有安装或启动 pinned official Runtime。** `deploy/edge/install.sh` 安装普通 Host/Companion 并安装 `desk-agent.service`；该 unit 直接运行 `host.desk_agent.main` 普通配置入口。Host 只有在 Runtime 提供 managed-app 环境时，才会进入 `_run_managed_application()` 并创建 `ApplicationContext.from_environment()`。当前没有日志 consumer、consumer 授权 gate 或有界接收队列。因此，即使固件先发送 `sys.log.event/gap`，当前部署也没有可用的受管 Host 消费链路。A3 在 `host/desk_agent/core/` + `deploy/edge/` 单独部署/托管该锁定版本官方 Runtime 与 managed app，并提供默认关闭、有界 consumer；可通过集中封装的 `private context._transport` listener 接收日志，但必须增加绑定该 Runtime SHA 的契约测试。只有本地显式启用且设备配对授权通过才消费。先做 L2 本机/模拟集成，不宣称 TaiShanPi 可用。

   Runtime 候选已由本地 pinned source 核验：v0.1.11，commit SHA `8bc13967a06ef073459be47e930ff43ef37ee8fb`，许可证 Apache-2.0（见 §7）。A0 已批准此版本作为部署锁定版本；部署清单必须固定该版本并保留来源/许可证记录。`deploy/edge/` 已属于 A3 ownership，不需路径例外；如需改 pinned SDK 安装/第三方源码，当前不在 A3/A2 ownership 内，须先提出 A0 窄范围 ownership/path exception CR，明确 tracked path、owner、来源/许可证、升级回合及测试边界；ignored vendor checkout 保持只读。若 Application callback 可通过现有受支持接口接入，则不需要 SDK 源码例外。

6. **A0 — 公共契约与注册：** 批准本 CR 后，A0 更新公共接口/Schema/capability contract；如新增组件需变更公用构建注册，按 AGENTS 由 A0 合入。任何 ownership 例外先记录后实施。

7. A7 为网络采集消费者实现 L2 协议 fixtures/测试与 L3 设备验收；不修改固件生产代码或绕过 A1/A2。既有 UART capture 不删除、不转成网络 fallback；UART 和网络证据分别标注来源、固件 hash、boot_id 与证据等级。

## 6. 验收门槛

### L2 契约与实现测试

- 正例：已配对连接、当前 WebSocket HardwareHello 成功态、paired metadata/token/local gate 与 Host gate 均有效、订阅过滤正确、ACK/NACK 的 `data.command_id` 对应、序号递增、有效 event/gap 可解析；顶层 `request_id` 不被依赖。
- 鉴权负例：未配对、错误/已撤销会话、设备 local gate 关闭、Host consumer 未显式授权、重连后沿用旧 latch/订阅均 fail closed，不发日志；仅 legacy TCP 在线也必须拒绝。
- 协议负例：未知版本、未知必需字段、坏 JSON、错误级别、畸形/超长帧、无效序列、错 stream_id 均拒绝或丢弃且不污染后续消息。
- 隐私：构造 pairing code、token、Wi-Fi/API 凭据、用户文本、音频/图像标记等测试值，断言这些数据不会进入设备网络输出或 Host consumer；非 allowlist 消息/字段丢弃。Runtime 明文 LAN 测试按任一同网观察者可见处理。
- 背压/恢复：TAL/ESP-IDF producer hook 在 log lock/并发条件下固定时延返回，无网络写入、无递归；队列到上限时 drop-new 计数并报告 gap，控制和心跳可继续；断线清空队列且不回放，重新配对/授权后从当前序号新建 stream。
- 兼容：未实现本扩展的旧设备/Runtime保持旧行为且没有日志流；不要求未经 wire 证实的 capability；Runtime 未接入显式 consumer 时不做归档；固件未实现新 handler 时预期命中 unknown-type NACK/404，新增 handler 后验证正常 ACK/NACK `data.command_id` 关联。
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
- **A3 Host/deployment 静态审计（L1）：** `deploy/edge/install.sh` 安装普通 Host/Companion 并部署 `desk-agent.service`，没有安装或启动 pinned official Runtime；`deploy/edge/desk-agent.service` 直接执行 `python3 -m host.desk_agent.main --config ...`。`host/desk_agent/main.py::_run_managed_application()` 才调用 `ApplicationContext.from_environment()`；该路径依赖 Runtime 注入 managed-app 环境。当前部署无日志 consumer、授权 gate 或有界接收队列，故 Host/Runtime 部署与托管集成是网络日志端到端接收的前置依赖。
- **未实现/未证明：** edge 当前未部署/启动 pinned Runtime；Host 当前没有显式有界 log consumer/授权 gate；pinned Runtime 无专用 log consumer；Host capability 不授权；A1 未接入新 command handler/sink；A2 未加 handler/发送；当前 WebSocket 无 TLS；目标镜像上的 logger hook、内存预算、设备端脱敏、真机网络接收/恢复/性能均未验证。
- **实现前待解子项：** (a) A1 用源码确认两个候选 event 的真实来源与 `channel` 映射后，由 A0 冻结 schema；(b) A2 按候选设置点 `desk_sdk_websocket.c::desk_sdk_ws_client_connect_paired()`、清除点 `ws_transport_close` 及 token/auth-policy/pairing metadata replacement 前 invalidate 实现 per-connection latch，并覆盖源码测试：成功/失败 HardwareHello ACK、legacy TCP only、disconnect/close/reconnect/reset、token replacement、auth-policy replacement、pair metadata replacement；特别验证 substring ACK 不被当作 cryptographic peer authentication；(c) duplicate subscribe 固定返回 `ALREADY_SUBSCRIBED` 并保留原 stream；(d) A3 确认 pinned Runtime listener 的集中封装及 SHA 绑定测试。**这些来源核验/测试与 A3 adapter/SHA 测试完成前，不开始 L2 协议实现。** 后续还需设备 log gate 与 Host consumer 默认关闭且有界、logger hook 重入/UART 保真和来源覆盖；候选 ~5 KiB 预算在 L3 前测量；`app_main.c` 若确有需要先裁决 owner。

**A0 Decision Log — 2026-10-22：** 批准 CR-009 进入有限 L2 实现阶段；仅允许本机/模拟构建、契约 fixtures 与集成验证，不批准实机验证、部署完成或产品交付宣称。范围固定为： (1) Runtime pin 为 watcherobot v0.1.11、SHA `8bc13967a06ef073459be47e930ff43ef37ee8fb`、Apache-2.0；(2) 仅传结构化枚举 `event_id` 与严格有界低敏 `fields`，不传原始日志或自由文本，不持久化；(3) A1 仅在 `desk_hardware/` / `desk_tclaw/` 提供 queue/sink API，A2 在 `desk_sdk_bridge/` 实现显式 paired command handler/发送，A3 在 `host/desk_agent/core/` + `deploy/edge/` 托管 Runtime/managed app 并实现默认关闭 bounded consumer，A0维护公开接口/schema，A7维护 L2 fixtures；(4) Runtime 无 TLS，LAN 仅允许低敏数据；候选约 5 KiB 预算必须在 L3 前测量；A2 审计补充：`desk_sdk_bridge_is_connected()` 包含 legacy TCP 在线态，不得作授权依据；仅接受当前 WebSocket HardwareHello ACK code=0 成功态的 per-connection auth latch，所有连接/配对元数据变更均须撤销。L2 不要求未经证实的 `device.logs.read` capability。两个初始事件 schema 保持候选，待 A1 核对来源/channel mapping 后由 A0 冻结。command_id 为 `uuid.uuid4().hex`，通过 `watcherobot.protocol.build_command()` 封套；单 WebSocket 单活动 stream，重复 subscribe NACK `ALREADY_SUBSCRIBED` 并保留原 stream。HardwareHello ACK latch 仅表明收到 code=0 的协议 ACK，不能证明 `ws://` 对端密码学身份；候选接入与撤销 seam 及测试按 §7 子项完成前，L2 实现保持 blocked。

**实现前 CR 子项：** (a) A1 核对两个候选事件来源与 channel mapping，A0 随后冻结 schema；(b) A2 实现并测试候选 per-connection auth latch：`desk_sdk_websocket.c::desk_sdk_ws_client_connect_paired()` 在 HardwareHello ACK code=0 后置位，`ws_transport_close` 集中清除，并在 token/auth-policy/pairing metadata 替换前 invalidate；覆盖成功/失败 ACK、legacy TCP only 和全部撤销场景，且不得把 substring ACK 误称密码学对端验证；(c) 重复 subscribe NACK `ALREADY_SUBSCRIBED` 且保留原 stream；(d) A3 提供集中 listener adapter 与 pinned SHA 绑定测试。所有子项/测试完成前保持 L2 实现 blocked。

**L3/L4 尚未通过：** 真实 Runtime service 在目标 Host 的部署、managed-app env 注入及 paired device 网络收发尚未验证；真实设备资源预算、日志源覆盖、TLS 缺失下的隐私边界和 UART 保真也未测。

**实机前置条件：** 完成上述实现前 CR 子项并通过 L2 实现/模拟验证；A3 在目标部署启动锁定 Runtime 并提供 managed-app environment；A1/A2 完成目标固件 sink/handler/配对 gate；A3 consumer 显式开启且双侧授权；测量资源预算并确认 logger source 覆盖、隐私过滤和 UART 保真后，才可进入 L3/L4 真机验收。

**结论：** 提案选择已认证的 L-09 WebSocket 为唯一允许的候选日志通道，明确拒绝未认证旁路；L2 实现获批，但网络日志能力仍未实现；真实 Runtime service/paired device 的 L3/L4 和产品交付未批准。
