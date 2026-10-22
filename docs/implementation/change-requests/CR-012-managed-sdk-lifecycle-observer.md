# CR-012：Managed SDK Lifecycle Observer 接缝

- **提出方：** A0（响应 CR-009 Host NetworkLogConsumer managed-app 接入缺口）
- **提出日期：** 2026-10-22
- **状态：** **APPROVED — 仅 Host 软件/L2；不得据此宣称网络日志端到端可用**
- **关联：** [CR-009](../CR-009-esp32s3-network-log-source.md)、[managed SDK listener](../../../host/desk_agent/core/managed_sdk_listener.py)、`host/desk_agent/main.py::_run_managed_application`
- **Owner：** A3 实现 Host 接缝与日志 consumer 生命周期接入；A0 维护本契约；A7 验收

## 1. 缺口与现有能力

固定官方 Python SDK 为 `watcherobot` v0.1.11，commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`（依赖锁与本地 pinned source）。该版本 `DaemonApplicationTransport` 提供 `add_message_listener()` / `remove_message_listener()`，并在 Device channel 的普通 JSON frame 上调用监听器；Desktop channel 在此之前单独路由。它没有公开 disconnect observer 或连接状态 observer。

该版本使用一个私有 `_disconnect_callback`。`_run()` 在 communicators 结束且不是显式 stop 时调用该 callback；SDK 已通过 `set_callbacks()` 将 Robot 清理逻辑放在此处。直接替换会丢失 SDK/Robot 清理，多个 Host adapter 各自包装则会产生覆盖顺序和清理顺序竞态。`ApplicationContext` 也没有公开 transport 属性，当前 Host 组合入口已按 pinned SDK 版本访问 `context._transport`。

现有 CR-009 要求设备断线后撤销订阅、停止流并清空队列，重连后重新认证并显式重新订阅；但没有定义 Host 如何观察 managed Application 的断线。CR-012 只补这条本地生命周期接缝，不更改 CR-009 的 wire contract、订阅授权或事件 schema。

## 2. A0 裁决

1. **复用消息监听器：** A3 继续用 `DaemonApplicationTransport.add_message_listener()` 接收日志 JSON。监听器只接收 Device channel 的解析后消息；不得改用 Desktop raw route、默认 Application callback 或新增第二条网络通道。
2. **Host 侧共享 disconnect observer：** A3 在自有 Host 代码中提供单一 `ManagedSdkLifecycleObservers`（名称可调整但语义须一致），由 `_run_managed_application()` 组合根创建并安装。它将 SDK 原有 `_disconnect_callback` 保存为唯一 upstream cleanup，再向多个 Host observer 广播 disconnect。Camera 与 NetworkLogConsumer 等 adapter 通过 registry 注册/注销，禁止 adapter 直接读写或包装 `_disconnect_callback`。
3. **边界：** 仅允许 A3 修改 `host/desk_agent/core/`、NetworkLogConsumer 所属服务、相关 Host 测试，以及依 CR-004 已批准的最小 `host/desk_agent/main.py` managed-app composition wiring。不得修改 pinned SDK/vendor、SDK bridge/设备 wire protocol、A2/A1 生产代码或 Runtime。
4. **生命周期范围：** 本 CR 只批准 disconnect observer。不得把 SDK channel startup、`sys.sdk.ready` 命令 ACK 或普通业务消息推断为新的已配对/已认证连接 observer。任何未来需要显式 connect/ready observer 的功能须另行提 CR，或使用经 A0 核对的 SDK 公共 API。

## 3. Observer 语义与失败处理

- Registry 安装时必须验证 pinned transport 存在 callable `_disconnect_callback`；缺失时 managed mode fail closed 并给出清晰错误，不得静默跳过清理。不得假定其他 SDK 版本兼容。
- Registry 只包装一次原 callback。每次 SDK disconnect 时先调用原 callback，再按注册快照通知 Host observers；原 callback 抛错时仍须尝试通知 observers，最后保留/传播 SDK 原 callback 的异常语义。每个 observer 独立隔离：一个 observer 抛错不得阻止其他 observer；异常须以不含凭据/日志 payload 的诊断记录。
- 通知同步发生于 pinned SDK transport 生命周期路径。observer 必须短小、非阻塞，不得在回调内等待网络、发 unsubscribe command、join worker 或调用会等待 SDK transport 的同步命令。需要较慢清理时只设置状态/信号，由对应 owner 的 worker 完成。
- A3 NetworkLogConsumer 在 disconnect observer 中清空内存队列、活动 stream/boot/session 关联与待处理订阅状态，并保持 fail closed。不得持久化、重放或自动重订阅断线期间日志。Host consumer 队列清理不得改变设备侧 CR-009 授权 latch 语义。
- Camera observer 继续清除 lease/frame/waiter 状态；同一 registry 通知 Camera 与日志 consumer，不能互相覆盖。
- Registry 在 `ApplicationContext` 退出前注销所有 Host observers，并恢复原 `_disconnect_callback`（仅当 transport 当前 callback 仍是本 registry 安装的 wrapper 时）。若 transport callback 被非预期替换，应记录契约错误，不覆盖第三方状态。
- SDK 显式 `close()`/正常 context shutdown 可能不会触发 `_disconnect_callback`。组合根必须在 context teardown 中调用相同的 Host cleanup/close 路径一次；清理需幂等，避免 disconnect 与 teardown 双重通知导致异常。
- 该 observer 只表示 transport lifecycle 结束，不证明设备已断电、配对失效、云端状态或物理硬件状态。

## 4. A3 L2 验收条件

1. 在 pinned SDK SHA 下验证 Device-channel log event 可到达 listener，Desktop-channel 同形消息不会进入 NetworkLogConsumer。
2. 安装 registry 后模拟断线：原 Robot/SDK callback 恰执行一次；所有已注册 observer 各收到一次通知；某 observer 抛错不阻断其他 observer；记录中无事件 payload、token 或 pairing code。
3. Camera 和 NetworkLogConsumer 同时注册时，二者都完成清理；注册/注销重复调用不会重复通知或遗留 observer。
4. 正常 context shutdown、断线后 shutdown、重复 cleanup 均安全；callback 恢复/注销行为不会覆盖后续所有者的替换值。
5. 断线清理测试证明 consumer queue、stream/session state 均清空且没有自动重订阅；重连后的新日志必须等待新的有效授权/订阅路径，旧 stream 事件不得重新进入 consumer。
6. 定向 Host 测试与 `git diff --check` 通过。证据仅为 Level 2；Runtime 部署、设备日志 producer/send、真实配对连接和 L3/L4 仍按 CR-009 保持未验证。

## 5. 不在本 CR 范围内

- 新增/修改任何 wire message、配对/认证机制、`sys.log.*` schema、capability 或设备侧 subscribe/unsubscribe handler。
- 修改或 vendoring 官方 SDK；本 CR 通过 Host 内部 adapter 兼容 pinned v0.1.11 私有 transport seam。
- 宣称已完成 Host Runtime 部署、ESP32-S3 producer/send、TLS、日志真机传输、设备重连恢复或 CR-009 整体验收。

