# CR-014：Managed Runtime DeviceSession 生命周期证据

- **提出方：** A0
- **提出日期：** 2026-10-22
- **状态：** PROPOSED — 等待 Runtime/SDK owner 评审；未实现，未批准 Host 开启网络日志授权
- **关联：** [CR-009 ESP32-S3 网络日志源](../CR-009-esp32s3-network-log-source.md)、[CR-012 Managed SDK 生命周期观察器](CR-012-managed-sdk-lifecycle-observer.md)、`docs/implementation/p0-3-official-sdk-device-contract.md`
- **预期 owner：** Runtime/SDK owner 实现并维护接口；A0 裁决契约与兼容性；A3 消费接口并维持 fail-closed；A7 验收

## 1. 问题与目标

CR-009 的 Host consumer 必须证明日志消息来自**当前已认证的设备 WebSocket 会话**。pinned WatcheRobot Runtime v0.1.11（SHA `8bc13967a06ef073459be47e930ff43ef37ee8fb`）的静态审计显示：`DevicePairingSession.snapshot()` 提供状态、online 和 request ID，但不提供物理连接代次；`ApplicationContext` 没有 typed DeviceSession 属性或连接结束回调。可选 `WATCHER_APP_DEVICE_STATUS_URL` 不能为托管应用提供可靠的重连撤销证据。Desktop 的 `daemon.device.state` 事件也不构成托管应用会话生命周期证明。

源码证据范围：锁定源码副本 `.target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk` 的 `src/watcherobot/runtime/daemon/pairing/session.py::DevicePairingSession.snapshot()` 仅返回 `state`、`online`、`mode`、`request_id`、`last_error`；`src/watcherobot/application/context.py::ApplicationContext` 未公开 DeviceSession 查询/observer；`src/watcherobot/application/transport.py::DaemonApplicationTransport` 的 `_resource_generation` / `_snapshot_generation` 是 device metadata snapshot 一致性计数，不是物理连接代次或 session end 通知。`ExternalWebSocketServer` 的 device disconnect/session-end listener 是 Runtime 内部接缝，未发现向 Managed Application 公共注入的证据。该结论是针对 pinned SHA 的 Level 1 源码审计；部署变体扩展及其他 SDK 版本未验证。不得把 metadata resource generation 或 `request_id` 当作 session generation。

本 CR 提议一项 Runtime 管理的 DeviceSession Lifecycle API，使 Host 能够：

1. 获取当前通过 HardwareHello 配对认证的设备 WebSocket 会话快照；
2. 绑定授权租约到该**物理连接的唯一代次**；
3. 在会话断开、被替换或认证/配对上下文变化时及时撤销该租约；
4. 安全处理监听器注册与初始状态查询之间的竞态。

此接口是应用侧生命周期证据，不新增网络服务或设备协议，不暴露配对码、session token、peer credential，也不自行执行 HardwareHello 认证。Runtime 必须继续独占连接与配对认证。

## 2. 术语与状态

- **Device session：** Runtime 已接受 HardwareHello 并由 pairing session 认证成功的单个 ESP32-S3 WebSocket 连接。TCP 控制桥、UDP 发现、配对请求或仅存在 pairing record 均不是 Device session。
- **Generation：** Runtime 为一个已认证物理 WebSocket 连接分配的不可复用代次。断线重连即使沿用同一 pairing request、token 或 peer 地址，也必须得到新代次。
- **Runtime instance ID：** 每次 Runtime 进程启动生成的新随机非凭据标识，用于区分进程重启前后的计数器值。它不是设备长期身份。
- **Managed Application：** 由 Runtime 创建并注入受管上下文的 Application；独立运行的 Host 进程不能凭猜测或读取 Desktop 事件获得本 API。

API 至少表达以下状态：

| State | 含义 | Host 网络日志授权 |
|---|---|---|
| `DISCONNECTED` | 当前没有通过 HardwareHello 的设备连接 | deny |
| `CONNECTED` | 存在一个当前已认证的设备 WebSocket 连接及有效 generation | 可继续检查其余 CR-009 授权门 |
| `REVOKED` | 曾有 generation 的会话已断开、替换或认证上下文失效 | 立即 revoke 对应 generation |

实现可以采用等价内部状态，但不得把“配对资料存在”“设备 online 推测”“legacy TCP connected”转换为 `CONNECTED`。

## 3. API 契约

### 3.1 快照

Managed API 应提供等价于 `get_current_device_session()` 的 typed 查询。返回值至少包含：

```text
DeviceSessionSnapshot {
  state: DISCONNECTED | CONNECTED | REVOKED
  runtime_instance_id: opaque string
  generation: unsigned monotonic integer, present only for CONNECTED/REVOKED
  changed_at_monotonic: runtime-local monotonic timestamp
  reason: closed enum, present for REVOKED
}
```

约束：

- `generation` 在同一个 `runtime_instance_id` 内严格递增；同一物理连接生命周期内保持不变，永不复用。比较会话身份必须使用 `(runtime_instance_id, generation)`，不得只比较 pairing request ID、token、IP、设备名称或 `online`。
- Runtime 重启必须产生新的 `runtime_instance_id`。进程重启前的任何 Host lease 均无效；新进程不得将旧 generation 恢复为有效。
- `CONNECTED` 仅能在 Runtime 成功完成 HardwareHello 授权及 `DevicePairingSession.connect_device()` 后产生。状态必须绑定到当前 WebSocket connection object/session，而非可变的 pairing record 快照。
- 查询失败、API 不存在、字段缺失、状态过期或无法识别版本，调用方必须拒绝授权。不要用状态 URL 轮询补造代次。
- 不返回 pairing code、session token、认证头、peer IP 或其他可重放凭据。generation 是本地生命周期句柄，不可用于设备认证。

### 3.2 原子注册与状态观察

Managed API 应提供等价于 `subscribe_device_session(callback) -> (subscription, initial_snapshot)` 的注册操作，并满足：

1. 注册回调和取得初始快照在 Runtime 的同一同步边界完成；调用方不存在“先查询、后注册”导致漏过断线/重连事件的窗口。
2. 注册后，Runtime 按状态变更顺序发送 immutable snapshot。每个状态事件都携带完整 `(runtime_instance_id, generation)`（若适用）、单调 `event_sequence` 和 closed `reason`，不依赖 callback 接收前的可变全局状态。
3. 旧会话失效必须先于该 session 后续消息被 Runtime 接纳/路由。撤销后，旧 generation 的消息不得再进入 managed Application 的 message listener。异步 observer 可以用于通知清理，但不能是唯一的安全门；Runtime 的消息路由必须同时以当前 connection generation 作门控。
4. 会话被新物理连接替换时，先使旧 generation 无效并发出 `REVOKED`，再发布新连接的 `CONNECTED`。无论 callback 调度如何，`event_sequence` 和代次身份均可判定先后；新 CONNECTED 不能复活旧 generation 的 lease。
5. `unsubscribe()` 必须幂等并停止后续回调。Managed Application 销毁时 Runtime 自动注销；注销与正在执行的 callback 并发时，API 保证不发生 use-after-free，调用方仍须以 generation 校验自身状态。
6. 初始快照允许是 `DISCONNECTED`。任何 observer 断开、callback 异常或消费队列溢出都意味着无法证明最新状态，Host consumer 必须 fail closed，不能继续沿用最近一次 `CONNECTED`。

建议 closed reason 至少覆盖 `WEBSOCKET_CLOSED`、`CONNECTION_REPLACED`、`PAIRING_CONTEXT_CHANGED`、`AUTHORIZATION_REVOKED`、`RUNTIME_SHUTDOWN`。新增 reason 必须保持向前兼容：未知 reason 按 revoke 处理。

### 3.3 Managed 消息的代次绑定

为了避免通知延迟期间旧流入站，凡 Runtime 将来自设备的消息交给 managed Application，应通过 typed envelope 附带来源 `runtime_instance_id` 与 `generation`，或提供等价的 Runtime 原子验证能力。A3 consumer 对每条订阅 ACK、event、gap 都必须验证其 generation 等于当前 lease generation。不能证明来源代次的旧 raw callback 不得用于授权日志 consumer。

该要求只增加 Runtime→managed Application 的本地来源元数据，不修改设备 WebSocket wire schema；现有设备 `sys.log.*` JSON frame 保持不变。运行时重放/转发不得移除来源代次后仍声称具备当前会话授权。

## 4. Host 使用约束与 fail-closed

A3 可在本接口实现并经 pin 后，将 CR-009 Host 授权从常量 deny 升级为默认关闭的 generation-bound lease。Host 至少满足以下全部条件才允许当前日志 stream：

- 配置值为精确 JSON boolean `true`；缺失、字符串、整数、`null` 均为 deny；
- managed Application 已成功取得支持的 DeviceSession API 与原子 observer registration；
- 初始快照为 `CONNECTED`，且 HardwareHello 成功状态由 Runtime 提供；
- 本地 pairing/设备策略允许当前 session；该授权不把 capability 列表当凭据；
- 当前日志 stream 的来源 generation 与当前 `CONNECTED` generation 完全一致；
- subscribe ACK 成功且 `command_id`、`stream_id`、schema version 有效；
- 每条日志事件/gap均通过 CR-009 的精确 schema、allowlist、敏感字段过滤和队列限制。

以下任一情况必须同步 deny/revoke、停止 consumer、清空该代次队列，并拒绝之后到达的旧代次 ACK/event/gap：`REVOKED`、新 generation、Runtime instance 变化、observer 失效、状态/消息代次不匹配、管理 API 异常、应用停止、设备 channel 断开、unsubscribe 失败或序列/协议错误。Host 不得因旧 ACK 重建 lease；新 generation 必须重新 subscribe 并收到新 ACK。

当前 CR-009 的 `_network_log_authorized = False` 保持不变，直至 Runtime API 可用、pinned 消费者实现并通过 L2 负例。仅有 `WATCHER_APP_DEVICE_STATUS_URL`、Desktop `daemon.device.state`、pairing request ID、TCP 在线态或“最近一次 connected”缓存均不得解除 deny。

## 5. 兼容性与版本治理

- 本 CR 是 Runtime 本地 managed API 扩展，不更改现有设备 wire 协议、HardwareHello payload、配对码格式、命令信封或端口。
- API 应显式版本化，例如 `device_session_lifecycle` interface version `1`，并通过 Runtime 注入的 API capability/version 明确暴露。不得通过访问私有 transport/socket/session 成员推断兼容性。
- 未实现该接口的旧 Runtime 保持现有行为；Host 检测不到 v1 时保持 deny，不应静默降级到状态 URL 轮询。
- 若实现改动对既有 managed Application 向后兼容，可作 Runtime 次版本增加；若改变生命周期顺序、代次稳定性或消息来源 envelope 语义，必须升主版本/接口版本并同步更新 SDK pin 与兼容测试。
- 部署必须锁定明确 SDK/Runtime 版本和 source SHA。升级后需先通过本 CR 的 L2 contract/compatibility suite，再更新 lock/pin；版本号存在本身不是接口已实现的证据。
- Runtime shutdown 必须使全部 session generation 失效。进程重启后不得持久化/恢复授权 lease。

## 6. L2 验收用例

本 CR 实现后，Runtime/SDK owner 应提供不依赖真实硬件的单元/集成测试；测试只能证明 L2，不能证明部署或设备连通。

### 正向与顺序

1. HardwareHello authorizer 接受配对后发布一个 `CONNECTED`，snapshot 和事件中的 runtime ID / generation 一致。
2. 同一 WebSocket 内查询多次 generation 不变；关闭后发布旧代次 `REVOKED`；物理重连即使配对 request ID/token/peer 相同，也产生更大 generation。
3. 将 observer 原子注册与并发 disconnect/reconnect 交错执行；最终调用方必然观察到注册时最新状态，不能漏掉 revoke。
4. 替换连接时旧 generation 先失效；旧消息在 Runtime 路由层被拒绝，新连接只携带新 generation。
5. Runtime 重启后 `runtime_instance_id` 变化，旧 generation 和 lease 无效。
6. `unsubscribe()` 重复调用安全；Application shutdown 清理 observer；正在执行的回调不造成生命周期错误。

### 负向与 fail-closed

1. 未认证/未配对 HardwareHello、仅 pairing pending、仅 Desktop channel、仅 legacy TCP online 均不发布 `CONNECTED`。
2. 不支持接口、未知接口版本、查询异常、observer 关闭或事件队列溢出均使 Host deny/revoke。
3. 旧 generation 的 ACK/event/gap、错误 runtime ID、过期 callback、未知 revoke reason 均被拒绝或按 revoke 处理。
4. Runtime 确认旧连接失效后，向 managed message listener 注入旧连接帧必须被拒绝；延迟的 observer callback 不能让该帧通过。
5. 状态变为 REVOKED 后注入旧 stream 消息；Host 清空并拒收，不能由旧 ACK 重新授权；新 generation 需重新 subscribe。
6. 查询/事件/日志 envelope 序列化不得包含 pairing code、session token、认证头、peer credential。
7. exact-bool opt-in 缺失/false/非 boolean 的 Host 集成都拒绝日志；打开 opt-in 但缺任一 CR-009 gate 仍拒绝。

### 兼容矩阵

| Host | Runtime | 预期行为 |
|---|---|---|
| 旧版 | 旧版 | 既有行为；日志授权 deny |
| 新版 | 旧版 / 无 API | API feature 缺失；日志授权 deny，不轮询回退 |
| 新版 | 支持 lifecycle v1 | 仅在所有 CR-009 gate 通过时可申请日志 lease |
| 旧版 | 新版 | 既有 managed-app API 行为保持兼容；不要求消费生命周期 API |

## 7. 依赖、未决问题与边界

- Runtime/SDK owner 需确认 public managed API 的语言/抽象边界、线程模型、回调重入/调度语义及支持的平台；不得由 A3 私自依赖 v0.1.11 私有 `_transport` 或 pairing internals。
- Runtime owner 需确认是否通过来源 envelope 标注每条 managed device message，或以等价同步 generation validator 拦截过期帧。两者必须满足 §3.2 第 3 项的先失效、后拒收顺序。
- Runtime owner 需说明 generation counter 的溢出处理。推荐 `uint64`；不得回绕复用。溢出时关闭会话并 fail closed，或更换 runtime instance ID 后重新认证。
- Runtime API 不替代设备侧默认关闭的 local log gate，也不替代 CR-009 的 HardwareHello latch、Host exact-bool consent、stream schema/privacy gate、有限队列/写期限或用户授权策略。
- 本 CR 不提供 TLS/机密性，不授权将原始日志发送到 LAN；CR-009 的严格结构化 allowlist 与脱敏约束持续生效。
- 本 CR 不宣称 v0.1.11 已有该接口，也不宣称其实现已完成。需等 Runtime/SDK owner 接受契约、提供实现版本/SHA 和 L2 证据后，才可进入 Host 消费者接入评审；真实 Runtime 部署和设备连通另属 L3/L4。

## 8. 提议决议

请 Runtime/SDK owner 评审并明确：

1. 是否接受 atomic snapshot + observer registration、不可复用 generation、Runtime 重启 instance ID、旧帧路由门控和无凭据输出这些语义；
2. 由哪个公共 managed API/SDK 接口提供该证据及最早可 pin 的版本；
3. 旧会话撤销相对 device message routing 的同步顺序如何保证；
4. 如何在 L2 测试中注入连接替换、observer 竞态、Runtime 重启和 stale message。

评审通过只批准 Runtime/SDK 接口实现，不自动打开 CR-009 Host authorization，也不构成 L3/L4 验收或部署授权。
