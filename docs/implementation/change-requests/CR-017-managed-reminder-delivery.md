# CR-017：Managed SDK 提醒通知投递与确认契约

- **提出方：** A0 架构与协议
- **状态：** **PROPOSED；待 Lead/A0 批准后由各 owner 实现**
- **范围：** A3 managed Host outbox、A2 SDK 应用命令/事件入口、A1 设备显示/提示音及用户交互
- **关联：** `contracts/schemas/domain_life.json`、`docs/implementation/interfaces.md` §4.1 / L-04、CR-005
- **限制：** 当前 pinned SDK/固件未提供完整闭环。本文定义目标契约，不代表现有 SDK 兼容或功能已实现；不得用 `sys.ack` 推断已显示、播放或用户已确认。

## 1. 问题与结论

Host 已有持久化 reminder outbox 和定时重试，但 managed SDK startup 跳过旧 TCP `RobotAdapter` 及 `register_announcement_channel()`，没有注册替代提醒通道。reminder alert 因而无法投递；pending 提醒超过 24 小时会过期。旧 TCP 路径虽能发 `ctrl.expression.play`，不能作为 managed Runtime 的兼容证据。

Pinned official Python SDK 有 `ApplicationContext.robot.audio.play_file()/play_pcm()` 等专用 API，以及私有 `_transport` 上的通用 `send_command()` 和通用 `add_message_listener()`；`ApplicationContext` 未公开 app-level 通用 announcement sender 或事件订阅入口。SDK/当前固件均未定义提醒状态/播放完成/用户确认事件。固件 `ctrl.expression.set` 可返回 accepted，但未发出播放完成事件；当前命令 ACK 不构成播报闭环。A0/A2 必须裁决是上游公开 API 还是对当前 private `_transport` 的窄范围适配；不得将其误称为 SDK 原生提醒支持，也不能回退旧 TCP。

## 2. 最小目标消息流（提案）

沿用现有 reminder occurrence 和 Host outbox，不新增云端提醒服务。设备投递命令及事件名称须由 A0/A2/A1 在实现前统一落入 schema；以下是建议名称，当前均 **未实现/未被 pinned SDK 支持**：

1. A3 持久化一个 occurrence 通知，使用稳定 `notification_id = occurrence_id`，并通过官方 managed Runtime 的受支持公开应用接口投递 `notification.deliver`。负载只包括 `notification_id`、`reminder_id`、`occurrence_id`、`title`、`scheduled_at`、`expires_at` 和必要的 catch-up 标志。
2. 设备按 ID 幂等接收，先返回 `accepted`（仅代表已接收/排队），再按 UI/音频仲裁显示并尝试播放。设备发送 `notification.status`：`rendered`、`played`、`failed` 或 `expired`，并关联相同 ID。`played` 仅可由设备音频播放接口报告成功结束后发出；硬件缺少可验证播放完成信号时必须标 `unknown`/不发 `played`，不能由 Host ACK 代替。
3. 用户通过明确交互确认“已看到/收到这条通知”时发送 `notification.acknowledged`，包含 `notification_id` 与确认来源。Host 持久化该通知回执，但**不得因此改变 reminder/occurrence 的完成或响铃状态**。停止响铃仍须走既有 `reminder.stop_ringing`；完成事项须由用户另行明确表达完成意图，再调用 `reminder.complete`。设备侧的短按/长按目前保留 TuyaOpen 语音语义、滚轮用于 UI 选择；本提案不擅自改变按键映射。确认手势/语音意图由 A1/A0 单独裁决，未实现前状态保持“未确认”。
4. `accepted`、`rendered`、`played`、`acknowledged` 是不同阶段；不允许把前一阶段升级推断为后一阶段。通知已确认只表示用户确认看到/收到；它不等于停止响铃或事项完成。无显式 ACK 时，Host 只能报告已排队/已受理/状态未知。

### 幂等与重复

- `notification_id` 对同一 occurrence 固定不变，重发不得生成新 ID。设备须保存足以覆盖有效期的去重状态；同 ID 重发时返回当前状态，不重复播放。设备重启后仍须保持此语义。若设备尚无该能力，不能在 Host 侧宣称 exactly-once；结果不明时保留 `delivery_unknown` 并避免自动制造新的提醒 ID。
- Host 按 `notification_id` 唯一关联 outbox、设备状态和通知确认事件。重复状态/确认事件幂等处理；迟到事件在 expiry 后不得复活过期 occurrence。`notification.acknowledged` 只更新通知回执，不得调用 `reminder.stop_ringing` 或 `reminder.complete`。
- 如果首次投递的 ACK 丢失，Host 用同 ID 重试；设备端持久去重是避免重复播音的必要条件，不能仅依赖 SDK `command_id`（它只是单次命令响应关联键）。

### 离线、过期与恢复

- Host outbox 在设备离线时持久保留并按既有退避策略重试。提醒 occurrence 的有效截止时间沿用当前 24 小时 catch-up/expiry 规则；超过 `expires_at` 后标记 missed/expired，不再播放过期内容。
- Host 重启后恢复 pending outbox；设备重启后依据稳定 ID 去重并重报最新可知状态。通知确认事件需要持久化后再向用户呈现“已看到/收到”；提醒响铃状态与完成状态仍由各自既有生命周期操作维护。
- 若投递 ACK 已返回但没有后续状态事件，Host 显示“已受理，播放状态未知”，不能无限把它当作发送失败重播；状态查询/重报机制属于 A2/A1 必需接口。当前 pinned SDK 没有该通知状态 API。

### 隐私

- 只向设备发送用户创建的提醒标题和必要时间/关联 ID；不得附带原始对话、备忘录正文、Codex 内容、凭据或其他工具上下文。标题属于用户内容，只允许传给已配对目标设备并仅在提醒功能启用时传输。
- Host/设备日志默认只记录 ID、阶段、错误码和时间，不记录标题、语音转写或确认语句。若产品需要文本播报，设备收到标题后本地播放；不得为此新增外部 TTS 云端依赖。

## 3. 接口依赖与 owner

- **A0 / Lead：** 批准消息名称、字段、状态转换、重复与过期语义；批准前 A3 不实现新 wire schema。
- **A2：** 为官方 managed SDK 应用提供受支持的公开命令发送和设备事件接收/订阅入口（当前 `ApplicationContext` 无公开通用 sender；当前 pinned Runtime/SDK 无通知状态/ACK 业务事件 API）。维护 command correlation 与 session 生命周期，不建立第二 Runtime。
- **A1：** 实现设备接收、按 ID 幂等与重启恢复、显示/播放状态回报、用户确认输入和资源仲裁；不能把显示成功等同于音频播放成功。
- **A3：** 接入现有 occurrence/outbox，稳定使用 occurrence identity，重试/expiry/recovery 并持久化设备状态和通知已读回执；不得将通知回执映射为 `reminder.stop_ringing` 或 `reminder.complete`。确保重连后使用同一 Runtime，不回退到旧 TCP adapter。
- **A7：** 覆盖离线、ACK 丢失、设备/Host 重启、重复事件、过期、播放失败和真实用户确认的端到端验收。

## 4. 当前 pinned API 核验（L1）

核对本地锁定依赖 `contracts/dependency_lock.json` 中 `watcherobot_python_sdk` commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`；vendor checkout `git rev-parse HEAD` 与该 SHA 一致且 `git status --short` 为空：

1. `src/watcherobot/application/context.py::ApplicationContext` 只持有 `robot`、`desktop`、`rtc` 和私有 `_transport`，未公开通用应用命令/事件 API。
2. `src/watcherobot/application/transport.py::DaemonApplicationTransport.send_command()` 可按命令等待相关 ACK；`add_message_listener()` 可观察原始 device-channel message。二者不定义提醒语义，也没有将 ACK 映射为显示/播放完成；目前只能通过 `context._transport` 访问，缺少 `ApplicationContext` 的公开应用级入口。
3. `src/watcherobot/robot.py` 的公开 `audio.play_file()/play_pcm()` 是音频播放 API，`expression.play_official()` 是设备表情资源 API；这两类方法均不提供带提醒 ID 的跨应用状态事件与用户确认回执。
4. 当前 DeskAgent 固件 `desk_sdk_bridge.c` 对 `ctrl.expression.set` 的 ACK 是 accepted，音频提示音调用结果没有完成事件回传；固件 bridge 中也没有 `notification.*` 用户确认事件。故该命令不足以满足以上幂等提醒投递/播报/确认契约。

此核验只证明本地 pinned 源码公开的 API 能力边界（Level 1）；不证明部署 Runtime、设备固件或扬声器行为。vendor checkout 与锁定 SHA 相同且工作区干净，故本次源码引用与依赖锁一致。

## 5. 验收要求与未验证项

- **契约静态验收（L1/L2）：** schema 校验必填字段、状态枚举、稳定 ID、expiry 和隐私约束；测试 duplicate delivery/state/ack、过期迟到事件、ACK 丢失后同 ID 重试、Host/设备重启恢复。
- **设备节点验收（L3）：** 真实设备显示、实际扬声器播放完成反馈、用户确认事件与 occurrence 正确关联；仅收 command ACK 不通过。
- **端到端验收（L4）：** managed official Runtime 下提醒创建 → 到期 → 设备展示/播报 → 用户确认已看到/收到并持久化通知回执；另行验证停止响铃仍调用 `reminder.stop_ringing`，事项完成仅在用户明确完成意图后调用 `reminder.complete`。覆盖离线、ACK 丢失、重启、重复投递和超时过期。需真实 SDK Runtime/设备证据。
- 当前缺少公开 managed application 命令/事件接口、device playback completion/user-ack event、设备端持久幂等存储及确定的用户确认映射。因此本 CR 仍为 proposed，功能未验证，不能声称兼容或完成。
