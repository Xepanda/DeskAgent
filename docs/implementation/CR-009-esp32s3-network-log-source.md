# CR-009：ESP32-S3 已配对网络日志订阅与流

- **提出方：** A0（基于 A7 网络日志源缺口记录）
- **提出日期：** 2026-10-22
- **状态：** **PROPOSED — 契约草案；尚无实现或网络日志能力**
- **关联：** `docs/acceptance/network-log-source-gap-2026-10-22.md`、`docs/implementation/interfaces.md` §5 L-09 / §6.4、`firmware/components/desk_sdk_bridge/`、`contracts/schemas/envelope_event.json`
- **预期 owner：** A0 契约批准；A1 设备日志源与发送；A2 SDK Runtime / 协议桥互通；A7 采集器和验收

## 1. 目标与现状

目标是在已配对、已认证的设备连接上按需订阅 ESP32-S3 诊断日志。**不得新建未认证的 TCP/UDP/HTTP 服务或其他旁路，也不得借用 Tuya 云端语音会话上传日志。**

当前仓库没有网络日志订阅、日志流消息或固件日志 sink。`tools/verification/capture_esp32s3_logs.py` 当前只被动读取 UART。L-09 描述的官方 Runtime 拓扑为 Runtime 在 Host 侧监听 LAN WebSocket、ESP32 设备主动连接；配对发现使用 UDP 37021，现有自有 TCP 37020 是另一条控制桥。上述控制桥、配对 UDP 和已存在的普通业务事件都不是日志源。

本 CR 提议只在 L-09 的已配对 WebSocket 应用通道上增加**协商后、按需、只读**日志订阅。它不更改 TCP 37020、UDP 37021、WSPK 图像帧或既有命令含义。此文档是待评审的契约草案；通过源码搜索或本 CR 本身不能证明网络日志已实现。

## 2. 通道、方向与授权

1. **连接方向：** ESP32-S3 仍主动连接配对请求中指定的 Host WebSocket Runtime。Runtime 在该既有连接上发送订阅/取消订阅请求；设备只在该连接上返回确认与日志事件。不得创建设备侧日志 listener、额外端口或广播日志。
2. **发现：** 沿用 L-09 配对流程发现 Runtime；UDP 37021 只用于既有配对请求/响应，配对完成后不承载日志订阅或数据。
3. **认证：** 订阅要求当前 WebSocket 会话完成现有配对 `HardwareHello` token binding，并通过 `sys.client.hello` 会话校验。支持该提案的设备在 hello capability 列表中声明 `device.logs.read`；Runtime 只有观察到该 capability、且本地诊断调用显式请求后才能发送订阅。设备只接受当前已认证配对会话上的订阅。未配对、无效/过期会话、设备未声明该 capability、会话撤销或鉴权状态未知时一律拒绝，fail closed。不得在每条日志内携带 session token，也不得把 pairing code、session token 或任何认证头写入日志。
4. **隔离：** log subscription 是设备只读诊断操作，不得执行命令信封中的业务操作。Runtime 侧仅为显式本地诊断调用订阅；不得默认向普通业务客户端或公网转发。
5. **传输保护边界：** 配对证明对端获准使用会话，不等于链路加密。现有 Runtime 若使用明文 WebSocket，日志内容必须按不可信 LAN 可见处理；禁止流出个人内容、凭据、音视频或其他敏感值。若部署要求日志机密性，须由 A0/A2另行确认既有通道的 TLS 能力与证书验证，不能在本 CR 中暗示已加密。

## 3. 版本化消息建议

所有新 `sys.log.*` 消息沿用已认证 WebSocket 上的**单个 JSON WebSocket text message**；WebSocket message boundary 是 framing，不附加 TCP 37020 的换行帧，不使用 UDP，也不使用 WSPK binary frame。每个 `sys.log.*` 消息必须带 `log_schema_version: "1.0.0"`；未知版本或未知必需字段不得按日志猜测解析。下列 `type` 和字段是本 CR 的提案，须由 A0/A1/A2评审后冻结。`sys.log.event/gap` 使用本 CR 的专用日志事件结构，避免把诊断流伪装成已有业务 `EventEnvelope`。

### 3.1 订阅与确认

Runtime → 设备：

```json
{
  "type": "sys.log.subscribe",
  "log_schema_version": "1.0.0",
  "request_id": "logsub_01",
  "min_level": "WARN",
  "components": [],
  "resume": {"boot_id": "boot-opaque-id", "after_sequence": 120}
}
```

`min_level` 枚举 `ERROR | WARN | INFO | DEBUG`，缺省 `WARN`；`components` 是显式允许的组件 tag 列表，空列表表示设备批准的诊断组件全集，不表示未过滤任意串流。`resume` 可省略；只有相同 `boot_id` 且记录仍在有界 RAM 环形缓冲中时才允许补发。设备返回与 request_id 关联的 `sys.ack`（含 `accepted`, `stream_id`, `boot_id`, 当前 `next_sequence` 和协商的 filter），或 `sys.nack`（稳定错误码 `UNAUTHORIZED`、`INVALID_ARGUMENT`、`UNKNOWN_OPERATION` 或 `RESOURCE_BUSY`）。请求仅表示订阅获准，不保证历史事件完整。

Runtime → 设备取消订阅：`sys.log.unsubscribe`，携带 `log_schema_version`、`request_id`、`stream_id`；设备以关联 `sys.ack` 确认停止。会话断开时设备自动停止订阅，不保留订阅授权到新连接。

### 3.2 日志事件与丢失通知

设备 → Runtime 日志事件建议采用以下专用结构：

```json
{
  "type": "sys.log.event",
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
```

`sequence` 在单次 `boot_id` 内单调递增；序号缺口代表一个或多个事件丢失，序号不因订阅、重连或 filter 改变而复用。`uptime_ms` 必填，避免未校准设备时钟造成假 UTC；设备时钟可信时才附加 RFC3339 `timestamp_utc`，否则省略。设备可发 `sys.log.gap` 控制事件，报告 `first_dropped_sequence`、`last_dropped_sequence`、`dropped_count` 和 `reason`；接收端也必须能从 sequence gap 发现丢失。gap 通知本身不回收或重用丢失序号。

日志不是任意业务事件转发：媒体、ASR 转写、用户输入、工具参数/结果、提醒内容、HA 实体值、Codex 文件内容等均不得进入该流。设备只发送审核过的组件与结构化诊断项；`fields` 只允许已批准的有界标量字段，不允许任意嵌套对象。自由文本必须经过设备端脱敏，再经过 Host 侧二次脱敏。禁止把原始 UART/日志 payload 默认持久化；诊断存储只保留允许的脱敏事件和明确的丢失统计。

## 4. 级别、隐私与资源边界

- 日志等级限 `ERROR/WARN/INFO/DEBUG`；不支持 `VERBOSE` 网络订阅。缺省 `WARN`，`DEBUG` 需显式本地订阅，固件重启后不恢复。
- 必须删除/遮蔽 pairing code、session token、Wi-Fi 密码、云端/API/HA/Codex 凭据、Authorization 字段、密钥、音视频内容及用户可识别内容。对未知字段默认丢弃；禁止把敏感原值仅依赖 Host 脱敏。
- `boot_id` 使用每次启动生成的非凭据、非长期设备标识随机值；不输出完整 MAC、序列号或固定设备身份。时间、tag、message、fields 长度均设上限，非法 UTF-8/过大事件丢弃并计数。
- 队列必须有硬上限并且不阻塞音频、屏幕、相机、动作或控制命令。提案基线：每条编码后事件最多 4096 bytes、volatile queue 最多 64 KiB；满载时丢弃最旧日志并累计 sequence gap，控制/配对消息优先。A1 必须核实目标固件 SRAM/PSRAM 预算；若 64 KiB 不可接受，A0 与 A1 在实现前共同裁决更小上限，不能改为无界队列。
- Runtime 接收端也使用有界队列；消费变慢时暂停/降低日志读取或明确丢弃并记录 gap，不得反压阻塞 WebSocket 心跳和业务命令。
- 只保留当前启动周期的易失环形缓存，不写 Flash/NVS/SD。设备断开或重启后不承诺完整回放；重连先重新完成配对认证与能力协商，仅可按 `boot_id/after_sequence` 补发仍驻留 RAM 的事件，否则发 gap / reset 标记并从当前序列继续。
- 网络日志是诊断旁路数据，对业务不可用；断线、鉴权失败或订阅拒绝不影响控制功能。A7 的 UART-only logger 保留为独立 fallback。

## 5. 兼容性和 owner 实现点

1. 设备通过 `sys.client.hello` 的 capability 列表声明 `device.logs.read`。旧 Runtime 忽略未知 capability，旧设备不声明它；Runtime 未观察到该 capability 时不得订阅。未支持时双方继续既有功能，不得降级成未认证服务或把普通 `sys.event` 当日志。
2. A1 在固件 SDK bridge 的已配对 WebSocket receive/send path 增加订阅状态、允许列表日志 sink、易失有界 ring buffer、序列/gap 生成和脱敏。不能将 `printf`/任意 ESP-IDF 输出无条件转发；不得增加新端口或更改 Tuya 音频会话。
3. A2 核验并扩展 pinned official Runtime 的入站命令和事件路由：只从 authenticated paired WebSocket 发订阅命令，只向显式授权诊断 consumer 投递 `sys.log.event/gap`；忽略/安全处理旧版本未知类型。官方 SDK/Runtime 若不能兼容新增控制消息，先提交接口/兼容性方案，不私自 fork 第二个连接 Runtime。
4. A7 为网络采集消费者实现 L2 协议 fixtures/测试与 L3 设备验收；不修改固件协议或绕过 A1/A2。A0 批准并同步本 CR 后，相关 schema/接口目录及能力版本由 A0/A2 按 ownership 单独更新。
5. 既有 UART capture 不删除、不转成网络 fallback；UART 和网络证据分别标注来源、固件 hash、boot_id 与证据等级。

## 6. 验收门槛

### L2 契约与实现测试

- 正例：已配对连接、能力协商成功、订阅过滤正确、ack 与 request_id 对应、序号递增、有效 event/gap 可解析。
- 鉴权负例：未配对、错误/已撤销会话、设备未声明 `device.logs.read`、重连后沿用旧会话、无权限订阅均 fail closed，不发日志。
- 协议负例：未知版本、未知必需字段、坏 JSON、错误级别、畸形/超长帧、无效序列、错 stream_id 均拒绝或丢弃且不污染后续消息。
- 隐私：构造 pairing code、token、Wi-Fi/API 凭据、用户文本、音频/图像标记等测试值，断言设备输出和 Host 归档均不含原值；未知 fields 丢弃。
- 背压/恢复：队列到上限时设备任务无阻塞、控制和心跳可继续，事件被限量丢弃并报告 gap；断线/重连、相同 boot 游标仍在 ring、游标过期、重启 boot_id 改变都得到定义结果；没有旧订阅或无限回放。
- 兼容：无 capability 的旧设备/Runtime保持旧行为且没有日志流；未知消息不会触发命令或导致连接状态虚报。
- 只读：抓取协议记录并证明唯一设备入站操作是 subscribe/unsubscribe，设备状态/动作/配置没有变化。

### L3 真机

在明确记录设备身份类别（不暴露唯一标识）、固件 hash、Runtime 版本和 capability 协商结果后，通过已配对 WebSocket 收到可解析、脱敏、带序列的真实设备日志；验证过滤、可控背压造成的 gap、断线恢复和 UART 对照。不得以 localhost loopback、模拟器或初始化成功替代真机证据。

### L4 集成

持续运行期间同时有语音/控制/日志流，证明日志不影响实时音频与命令；覆盖 Host/设备重启、断网、重新配对、权限撤销和队列满恢复。任何凭据或用户内容泄露均为验收失败。

## 7. 当前证据与待裁决项

- **已由源码/文档确认（L1）：** L-09 配对发现 + Runtime-hosted WebSocket、设备 WebSocket client、pair token 首帧 HardwareHello、现有 `sys.client.hello` capability 协商、EventEnvelope 序列字段及 WSPK 的独立媒体 framing。
- **已由 A7 缺口记录确认（工具层 L2、有限 UART L3）：** 当前日志工具只读 UART；没有网络日志 stream/sink。
- **未证明：** 当前部署 Runtime 是否允许新增 `sys.log.subscribe`/`sys.log.event` 路由；TLS 是否启用；A1 目标镜像中可用的非阻塞 ESP-IDF logger hook、PSRAM/队列预算、设备端脱敏能力；任意真机网络日志接收、恢复或性能表现。
- **A0/A1/A2 合入前必须确认：** 现有 Runtime 能否以兼容扩展方式承载新的 `sys.*` 消息；`boot_id` 的熵源；A1 是否能在 ≤64 KiB 缓冲和 ≤4096-byte event 限额下不干扰音频/控制；当前配对 token 生命周期/R11 对 long-lived paired connection 的影响。若任一项不满足，先修订本 CR，不得实现未批准的隐式行为。

**结论：** 提案选择已认证的 L-09 WebSocket 为唯一允许的候选日志通道，明确拒绝未认证旁路；其网络日志能力仍为未实现/未验证。A0 批准前不得开始协议实现或将能力宣称为可用。
