# CR-017：Managed SDK 提醒通知投递与确认契约

- **提出方：** A0 架构与协议
- **状态：** **APPROVED（本地契约；A3 managed Host 接线已实现并通过 L2 测试；设备存储、设备/服务 L3/L4 尚未验证）**
- **范围：** A3 managed Host outbox、A2 SDK 应用命令/事件入口、A1 设备显示/提示音及用户交互
- **关联：** `contracts/schemas/domain_life.json`、`docs/implementation/interfaces.md` §4.1 / L-04、CR-005、CR-018（设备持久存储面决策）、[CR-019（managed Host outbox 接线）](CR-019-managed-reminder-host-outbox-adapter.md)
- **限制：** Host 侧 adapter/outbox/SQLite receipt 接线已实现；当前 CR-018 仍 BLOCKED，设备 durable acceptance 尚不成立，设备/服务 L3/L4 闭环未验证。不得用 `sys.ack` 推断设备已持久接收、已显示、已播放或用户已确认。

## 1. 问题与结论

Host 已有持久化 reminder outbox 和定时重试。此前 managed SDK startup 跳过旧 TCP `RobotAdapter` 及 `register_announcement_channel()`，没有替代提醒通道；该 Host 接线缺口现已由 A3 按 CR-019 补齐：现有 occurrence/outbox 映射到隔离的 pinned-SDK notification adapter，并将 receipt 保存在同一 `LifeService` SQLite 数据库。CR-018 仍阻止设备 durable acceptance，因此 managed 投递当前只保留 pending/unknown 并按同一 ID 重试；这不代表设备真实收到了提醒。旧 TCP 路径不能作为 managed Runtime 的兼容证据。

Pinned official Python SDK 有 `ApplicationContext.robot.audio.play_file()/play_pcm()` 等专用 API，以及私有 `_transport` 上的通用 `send_command()` 和通用 `add_message_listener()`；`ApplicationContext` 未公开 app-level 通用 announcement sender 或事件订阅入口。SDK/当前固件均未定义提醒状态/播放完成/用户确认事件。固件 `ctrl.expression.set` 可返回 accepted，但未发出播放完成事件；当前命令 ACK 不构成播报闭环。Lead/A0 已批准仅由 A3 通过隔离 adapter 窄范围使用此 private `_transport` 兼容入口，且须满足精确 pin 与测试条件；不得将其误称为 SDK 原生提醒支持，也不能回退旧 TCP。

## 2. 批准的消息流及当前实现边界

沿用现有 reminder occurrence 和 Host outbox，不新增云端提醒服务。以下本地消息/状态名称已由 Lead/A0 批准，作为 DeskAgent 组件间契约。A3 Host 侧 adapter、outbox 调用及 receipt 持久化已完成 L2；设备 handler、A1/A2 状态事件产生与 CR-018 provider 仍未实现/验证，不能据此宣称 pinned SDK 或当前设备支持这些业务消息：

1. A3 持久化一个 occurrence 通知，使用稳定 `notification_id = occurrence_id`，并通过官方 managed Runtime 的受控命令发送入口投递 `notification.deliver`。负载只包括 `notification_id`、`reminder_id`、`occurrence_id`、`title`、`scheduled_at`、`expires_at` 和必要的 catch-up 标志。
2. **目标设备行为（未通过实机验证）：** 仅当 CR-018 批准的 provider 已原子写入并回读校验后，设备才可报告 durable `accepted`；当前 CR-018 BLOCKED，Host 不得把任何设备 `accepted` 观察归并为该阶段。后续设备按 ID 幂等处理，并按 UI/音频仲裁显示与尝试播放。设备发送 `notification.status`：`rendered`、`played`、`failed`、`expired` 或 `unknown`，并关联相同 ID。`played` 仅可由设备音频播放接口报告成功结束后发出；硬件缺少可验证播放完成信号时必须标 `unknown`/不发 `played`，不能由 Host ACK 代替。
3. 用户通过明确交互确认“已看到/收到这条通知”时发送 `notification.acknowledged`，包含 `notification_id` 与确认来源。Host 持久化该通知回执，但**不得因此改变 reminder/occurrence 的完成或响铃状态**。停止响铃仍须走既有 `reminder.stop_ringing`；完成事项须由用户另行明确表达完成意图，再调用 `reminder.complete`。设备侧的短按/长按目前保留 TuyaOpen 语音语义、滚轮用于 UI 选择；本 CR 不改变按键映射。确认手势/语音意图由 A1 在实现时裁定，未实现前状态保持“未确认”。
4. `accepted`、`rendered`、`played`、`unknown`、`acknowledged` 是不同阶段/回执；不允许把前一阶段升级推断为后一阶段。通知已确认只表示用户确认看到/收到；它不等于停止响铃或事项完成。无显式 ACK 时，Host 只能报告已排队/已受理/状态未知。

### 幂等与重复

- `notification_id` 对同一 occurrence 固定不变，重发不得生成新 ID。设备须保存足以覆盖有效期的去重状态；同 ID 重发时返回当前状态，不重复播放。设备重启后仍须保持此语义。该持久面目前受 CR-018 阻塞；在产品/存储 owner 决定并完成 provider 前，不能在 Host 侧宣称 exactly-once；结果不明时保留 `delivery_unknown` 并避免自动制造新的提醒 ID。
- Host 按 `notification_id` 唯一关联 outbox、设备状态和通知确认事件。重复状态/确认事件幂等处理；迟到事件在 expiry 后不得复活过期 occurrence。`notification.acknowledged` 只更新通知回执，不得调用 `reminder.stop_ringing` 或 `reminder.complete`。
- 如果首次投递的 ACK 丢失，Host 用同 ID 重试；设备端持久去重是避免重复播音的必要条件，不能仅依赖 SDK `command_id`（它只是单次命令响应关联键）。

### 离线、过期与恢复

- Host outbox 在设备离线时持久保留并按既有退避策略重试。提醒 occurrence 的有效截止时间沿用当前 24 小时 catch-up/expiry 规则；超过 `expires_at` 后标记 missed/expired，不再播放过期内容。
- Host 重启后恢复 pending outbox；设备重启后依据稳定 ID 去重并重报最新可知状态。设备在 `accepted` 后、首次呈现前重启时能否恢复待展示内容，受 CR-018 的产品决策阻塞：若允许标题短期持久化则依同 ID 恢复；若不允许，须报告 `delivery_unknown` 且不得假报已展示/播报。通知确认事件需要持久化后再向用户呈现“已看到/收到”；提醒响铃状态与完成状态仍由各自既有生命周期操作维护。
- 若只有 transport ACK 而没有可验证设备状态，Host 持久化 `unknown` 并保留 pending，以同一 ID 按退避重试至现有 expiry；不能宣称已受理/已播放，也不得生成新 ID。状态查询/重报机制属于 A1/A2 后续接口。当前 pinned SDK 没有该通知状态 API。

### 隐私

- 只向设备发送用户创建的提醒标题和必要时间/关联 ID；不得附带原始对话、备忘录正文、Codex 内容、凭据或其他工具上下文。标题属于用户内容，只允许传给已配对目标设备并仅在提醒功能启用时传输。
- Host/设备日志默认只记录 ID、阶段、错误码和时间，不记录标题、语音转写或确认语句。若产品需要文本播报，设备收到标题后本地播放；不得为此新增外部 TTS 云端依赖。

## 3. 本地批准边界、接口依赖与 owner

- **A0 / Lead：** 已批准本地消息名、字段范围、阶段转换、去重和 expiry 语义。批准只对 DeskAgent 内部契约有效，不代表 upstream SDK、固件能力或验收结果。
- **A2：** 提供设备命令/事件通道的桥接和 session 生命周期支撑。可以在本地 schema 批准后使用现有 Runtime transport；不得另建连接 Runtime。当前 pinned Runtime/SDK 没有 `notification.*` 业务事件实现。
- **A1：** 仍需实现并实机验证设备接收、按 ID 幂等与重启恢复、显示/播放状态回报和资源仲裁；不能把显示成功等同于音频播放成功。**通知的显式用户确认输入来源由 A1 在实现时裁定**，不得由 CR-017 擅自重映射已有短按、长按或滚轮语义；确认输入未实现前不得声称用户确认闭环。
- **A3：** 已按 CR-019 完成 Host occurrence/outbox 接线、稳定 occurrence identity、24 小时 expiry 映射、同 ID pending/unknown 重试、LifeService SQLite receipt 与 ACK 状态分离；L2 测试通过。设备端 durable receipt 尚未获批，Host 不能报告设备 durable accepted、rendered、played 或用户确认作为已发生事实；不得将通知回执映射为 `reminder.stop_ringing` 或 `reminder.complete`。使用现有 managed Runtime，不回退旧 TCP adapter。

### A3 受控 Runtime 适配条件

Lead/A0 批准 A3 在隔离的 managed notification adapter 中，通过 `context._transport.send_command()` 发送本地批准的 command，并通过 `context._transport.add_message_listener()` 接收状态事件；这是对 pinned SDK transport 的窄范围兼容适配，不是 SDK 原生 reminder API。启用条件：

- `contracts/dependency_lock.json` 中 WatcheRobot Python SDK commit/hash 必须精确匹配本次已审计版本 `8bc13967a06ef073459be47e930ff43ef37ee8fb`；启动/测试发现版本不匹配时拒绝启用该 adapter。
- A3 必须以 contract tests 覆盖命令 ACK correlation、事件 listener 注册/注销、断线重连、重复事件及取消/关闭生命周期；不得绕过 `ApplicationContext` 创建第二 Runtime。
- SDK 升级时先重新审计 transport 入口、线程/回调生命周期及 ACK 关联行为；若接口变化，先更新依赖锁与本 CR，再恢复 adapter。
- 此私有字段授权仅限上述隔离 adapter；不得将它扩散为通用业务层调用约定，也不得 fallback 到 legacy TCP `RobotAdapter`。
- **A7：** 覆盖离线、ACK 丢失、设备/Host 重启、重复事件、过期、播放失败和真实用户确认的端到端验收。

## 4. 当前 pinned API 核验（L1）

核对本地锁定依赖 `contracts/dependency_lock.json` 中 `watcherobot_python_sdk` commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`；vendor checkout `git rev-parse HEAD` 与该 SHA 一致且 `git status --short` 为空：

1. `src/watcherobot/application/context.py::ApplicationContext` 只持有 `robot`、`desktop`、`rtc` 和私有 `_transport`，未公开通用应用命令/事件 API。
2. `src/watcherobot/application/transport.py::DaemonApplicationTransport.send_command()` 可按命令等待相关 ACK；`add_message_listener()` 可观察原始 device-channel message。二者不定义提醒语义，也没有将 ACK 映射为显示/播放完成；目前只能通过 `context._transport` 访问，缺少 `ApplicationContext` 的公开应用级入口。
3. `src/watcherobot/robot.py` 的公开 `audio.play_file()/play_pcm()` 是音频播放 API，`expression.play_official()` 是设备表情资源 API；这两类方法均不提供带提醒 ID 的跨应用状态事件与用户确认回执。
4. 当前 DeskAgent 固件 `desk_sdk_bridge.c` 对 `ctrl.expression.set` 的 ACK 是 accepted，音频提示音调用结果没有完成事件回传；固件 bridge 中也没有 `notification.*` 用户确认事件。故该命令不足以满足以上幂等提醒投递/播报/确认契约。

此核验只证明本地 pinned 源码公开的 API 能力边界（Level 1）；不证明部署 Runtime、设备固件或扬声器行为。vendor checkout 与锁定 SHA 相同且工作区干净，故本次源码引用与依赖锁一致。

## 5. 验收要求与未验证项

- **Host 静态/模拟验收（L2，已通过）：** root 报告 managed notification adapter、managed outbox 与相关 LifeService/SDK startup 测试共 **63 passed、7 subtests**，覆盖命令 ACK 与 durable acceptance 区分、同 ID pending 重试、持久 receipt/事件去重、expiry/迟到事件过滤、重启上下文和不因非 unknown 阶段重发。该证据不含设备或实际 SDK 服务联调。
- **设备节点验收（L3）：** 真实设备显示、实际扬声器播放完成反馈、用户确认事件与 occurrence 正确关联；仅收 command ACK 不通过。
- **端到端验收（L4）：** managed official Runtime 下提醒创建 → 到期 → 设备展示/播报 → 用户确认已看到/收到并持久化通知回执；另行验证停止响铃仍调用 `reminder.stop_ringing`，事项完成仅在用户明确完成意图后调用 `reminder.complete`。覆盖离线、ACK 丢失、重启、重复投递和超时过期。需真实 SDK Runtime/设备证据。
- **当前状态：** A3 Host 子链路已实现并达到 L2；设备侧通知命令 handler、CR-018 durable provider/去重、playback completion、真实状态/确认事件与 A1 用户确认输入映射仍未通过验证。CR-018 继续 BLOCKED；没有实际 managed SDK service/设备/扬声器的 L3 或端到端恢复 L4 证据。不得声称官方 SDK 原生支持提醒、设备已投递/播报、或整项功能完成。
