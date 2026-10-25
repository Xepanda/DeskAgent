# CR-019：Managed Reminder Outbox 到设备通知 Adapter 的 Host 接线

- **提出方：** A0 架构与协议
- **状态：** **APPROVED（仅 DeskAgent Host 接线、回执存储及 A3 对 `main.py` 的精确路径例外；不批准设备存储或 wire schema）**
- **范围：** A3 managed Host reminder outbox；A2/A1 仅依赖已批准的 CR-017/CR-018 消息与设备行为
- **关联：** [CR-017](CR-017-managed-reminder-delivery.md)、[CR-018](CR-018-device-notification-persistence-surface.md)、`host/desk_agent/core/managed_notification.py`、`host/desk_agent/services/life_service.py`
- **限制：** 本 CR 不定义或更改设备 wire schema，不授权设备存储、不批准 SDK 协议变化。A3 Host 接线已有 L2 实现证据，但不代表完整提醒功能或真实设备验收完成。依 CR-018，Host SQLite 是唯一 durable store，设备通知阶段仅为易失观测；transport ACK 不得推进显示、播放或用户确认阶段。

## 1. 当前实现与证据（源码 L1、测试 L2）

1. `DeskAgentHostApp.__init__()` 创建唯一 `LifeService`，默认 SQLite 文件为 `storage.db_path`（缺省 `/var/lib/desk-agent/life_service.db`）。`outbox` 继续作为提醒 payload/重试记录；A3 已在同一 SQLite 库中增加 `notification_receipts` 与 `notification_receipt_events`，持久化关联 ID、expiry、归并后的 delivery status、用户确认 source/time、事件 fingerprint 与协议异常计数。receipt 不修改 reminder/occurrence 生命周期。
2. `LifeService.build_reminder_notification()` 将现有 `reminder.alert` payload 映射为稳定 `notification_id = occurrence_id` 的 CR-017 allowlist payload，并从 `scheduled_at` 复用 24 小时 catch-up expiry 规则构造 `expires_at`。`ensure_notification_receipt()` 在发送前验证 outbox 关联及有效期并创建 durable `unknown` receipt；启动时 `get_active_notification_contexts()` 恢复未过期 ID/expiry 供 listener 关联。
3. `_delivery_loop()` 在 managed session 中 claim 现有 outbox、确保 receipt 后调用 managed adapter。无 adapter、错误或 transport 失败时留在 pending 并退避；成功 transport ACK 或设备易失 accepted 不证明呈现/播放。只有已持久化的终态或更高阶段 receipt 才按既有策略抑制重发；未知结果同 ID 重试，设备重启边界可能重复呈现/播放。legacy session 继续走既有 announcement channel。
4. `_run_managed_application()` 在唯一官方 SDK `ApplicationContext` 内注册 `ManagedNotificationAdapter`，使用当前 pinned transport 并把事件 callback 接到 `LifeService.record_managed_notification_event()`；adapter 预载有效通知上下文。`DeskAgentHostApp.close()` 先停止并等待 delivery worker，再注销 notification listener，之后才关闭 LifeService。
5. `ManagedNotificationAdapter` 对 pinned SDK 版本做 fail-closed guard；`deliver()` 区分 transport ACK、设备易失 accepted 和 durable Host outbox/receipt。Host durable acceptance 仅表示 SQLite 已提交受管投递记录，不表示设备 durable acceptance、呈现或播放。设备 `accepted` 只作当前运行期观测，不升格为 durable device phase；状态/ACK 事件关联有效期并送到事务化 receipt API。adapter 的内存去重仅是同进程优化，跨 Host 重启幂等依赖 SQLite event fingerprint；设备跨设备重启不持久去重。

上述源码证据为 L1。root 报告相关 managed notification、outbox、LifeService 与 managed SDK startup 测试共 **63 passed、7 subtests**，属 L2；没有实际设备或 SDK service 联调证据。

## 2. 最小 Host 接线裁定

### 2.1 Runtime 生命周期与 A3 路径例外

- A3 已在 `_run_managed_application()` 创建 `ApplicationContext` 后，为**同一个** pinned transport 创建并注册 `ManagedNotificationAdapter`，receipt callback 接到当前 `LifeService`；未创建第二个 SDK Runtime、独立 TCP 连接或绕开 managed session。
- 已落实对 A3 ownership 的窄范围例外：`host/desk_agent/main.py` 中 `_run_managed_application()` 的注册/清理调用、`DeskAgentHostApp._delivery_loop()` 的 `reminder.alert` 分支，以及 `DeskAgentHostApp.close()` 的 adapter 关闭。该例外不授权修改其他 main.py 行为；其余业务状态、SQLite API 和测试仍限 A3 所属 core/services 路径。
- 注册顺序必须保证事件 listener 在首次发送之前安装。关闭时先停止/等待 delivery worker，再注销 listener/关闭 adapter，最后关闭 `LifeService`；幂等 close。managed app startup 失败时清理已创建 listener 与 Host，不得留下回调引用。
- adapter 未注册或 pinned SDK guard 失败时保持现有 fail-closed 行为：outbox 保持可重试，不得 fallback 到旧 TCP announcement channel，也不得标 dispatched。

### 2.2 从 occurrence 到命令

- A3 已消费现有 `reminder.alert` outbox，并以稳定 `notification_id = occurrence_id` 建立 CR-017 allowlist payload；`expires_at` 由现有 scheduled 时间和 24 小时 catch-up 规则确定，未引入第二套 TTL。
- reminder 保留 `claim_reminder_alert()` 的互斥与恢复行为。发送、超时、断线和进程重启均复用同一 occurrence/notification ID，不新建 occurrence 规避不确定结果。
- outbox 的 Host 持久状态只表示 SQLite 中的投递记录状态；不得把 `dispatched` 或 transport `sys.ack` 解释为设备已持久接收、已显示、已播放或用户已确认。设备阶段与确认单独存入 Host SQLite receipt。设备没有 durable accepted 状态；Host 是否结束重试由已落库的设备呈现/播放状态、错误和 expiry 规则决定。

## 3. 回执存储与 ACK 语义

- 继续使用当前 `LifeService` 管理的同一 SQLite 数据库和其事务/锁边界；不得另建 JSON 文件、第二数据库或进程内状态层作为持久状态来源。现有 outbox API 足够承载待投递 payload 与重试，但**不足以**保存设备阶段和用户确认。
- A3 已在 `LifeService` 中增加事务化 notification receipt schema/API：以稳定 `notification_id`（等于 occurrence ID）关联 reminder/outbox；保存归并后的设备阶段、独立的用户确认标志/source/time 与持久 event fingerprint。回执不改变 `occurrences.state`、不停止响铃、不完成 reminder。设备 `accepted` 仅作易失接收观测，Host 归并为 unknown，不提升到显示/播放阶段。
- 不新增提醒标题副本；payload 继续留在现有 outbox，回执表只留事件所需 ID、阶段/确认元数据及必要 expiry/去重元数据。遵守 CR-017 日志隐私要求。
- 三种证据必须区分：
  1. SDK `sys.ack`：仅证明该 command 的 transport-level ACK/correlation 成功；不证明设备应用已接收、持久化、呈现或播放。
  2. command response 中显式的应用 `accepted`：设备只允许将其用于当前运行期易失接收；它不构成 durable device accepted。Host 的持久排队/receipt 由 SQLite 事务保障，与设备是否呈现或播放分开。
  3. `notification.status` 与 `notification.acknowledged`：独立设备事件；Host 必须先在 LifeService 事务中持久记录，才能对上层报告该状态。设备 ACK 不能映射为提醒停止或完成。
- adapter 收到事件时没有稳定 wire event ID/sequence。A3 已在不改 wire schema 前提下于 Host 使用规范化事件内容 fingerprint 做跨 Host 重启幂等，并按下表维护单调阶段。`accepted` 只代表设备本次运行期收下命令，Host 保留为 unknown/诊断性观测，不推进设备呈现/播放阶段。

### 3.1 Host 阶段迁移表（只规范接收与持久化，不定义 wire）

设备 delivery phase 的状态域沿用 CR-017：`unknown | accepted | rendered | played | failed | expired`。`notification.acknowledged` 是独立回执，不属于该阶段序列。

| 当前持久阶段 | 收到 `accepted` | 收到 `rendered` | 收到 `played` | 收到 `failed` | 收到 `expired` | 收到 `unknown` |
|---|---|---|---|---|---|---|
| `unknown` | 保持 `unknown`，仅记录本次运行期接收观测 | 接受该阶段作为已观察到的设备证据；不补写“曾观察到 accepted” | 接受该阶段作为已观察到的设备证据；不补写较低阶段 | 进入终态 `failed` | 进入终态 `expired` | 保持 `unknown` |
| `accepted` | 重复，幂等 | 推进到 `rendered` | 推进到 `played`（允许中间事件丢失；不声称观察到 `rendered`） | 进入终态 `failed` | 进入终态 `expired` | 保持 `accepted`，仅记录重复/不确定观察 |
| `rendered` | 迟到的较低阶段，忽略 | 重复，幂等 | 推进到 `played` | 进入终态 `failed` | 进入终态 `expired` | 保持 `rendered`，仅记录不确定观察 |
| `played` | 迟到的较低阶段，忽略 | 迟到的较低阶段，忽略 | 重复，幂等 | 冲突事件；保留 `played` 并记录协议异常 | 冲突/迟到事件；保留 `played` 并记录协议异常 | 保持 `played`，不得降级 |
| `failed` | 终态后事件，忽略并记录异常 | 终态后事件，忽略并记录异常 | 终态后事件，忽略并记录异常 | 重复，幂等 | 冲突终态，保留首次终态并记录异常 | 保持 `failed` |
| `expired` | 终态后/迟到事件，忽略 | 终态后/迟到事件，忽略 | 终态后/迟到事件，忽略 | 冲突终态，保留 `expired` 并记录异常 | 重复，幂等 | 保持 `expired` |

裁决规则：

- 只接受已关联到本地有效 notification ID 的合法枚举；同一规范化事件重复时幂等。无稳定 wire event ID 时，持久 fingerprint 仅用于去重完全相同的内容，不据此推断发送顺序。
- `accepted → rendered → played` 是单调阶段。较高阶段事件可越级推进，以容忍较低阶段事件丢失；这只证明收到的较高阶段，不反向伪造较低阶段的事件证据。较低阶段迟到不得回退当前阶段。
- `failed` 和 `expired` 是终态，第一次合法终态锁定；相互冲突时保留先落库的终态并记录异常，不靠到达顺序猜测实际发生顺序。Host 根据可信 `expires_at` 进入 expiry 后，所有迟到设备阶段事件都忽略，不得复活或覆盖 expired/missed。
- `unknown` 表示没有可证明的更高阶段或当前观测不确定；它不是终态，且永不降低已持久的 `accepted/rendered/played/failed/expired`。设备回报 unknown 时不覆盖已有阶段。
- `notification.acknowledged` 可在有效期内独立持久化，不推进/回退 delivery phase；重复确认幂等。过期后收到的 ACK 不采纳。ACK 不能触发停止响铃或完成 reminder。
- 上述表仅供 Host 对 CR-017 已批准状态作保守归并。它不授权 A1/A2 添加字段或消息，也不声称当前固件能生成任一状态事件。Host SQLite 持久化事件 receipt，设备阶段本身不在设备侧持久化。

## 4. 重复、重连、过期与关闭

- **重复发送/ACK 丢失：** 仍用原 notification ID 重试。若只收到 transport ACK 而没有可证明的呈现/播放状态，Host 不得报告设备已呈现/播放；按 CR-017/occurrence expiry 决定同 ID 重试或标 unknown。设备端只作 RAM 去重，设备重启或结果不确定时可能重复播放；Host SQLite 不能代替设备侧 exactly-once。
- **断线/重连：** listener 绑定当前 Runtime。未到期 pending/unknown outbox 在 Host 恢复后按相同 ID 处理。adapter 建立时须能从 LifeService 装载仍有效的 ID/expiry 过滤上下文，或先将 listener 事件暂存至可持久关联；不得因 Host 进程重启而无声丢弃合法迟到事件。设备断线/重启会清除易失去重状态，因此未知通知重投可能再次呈现/播放。具体 transport 断线回调与重连次序须由 pinned SDK contract tests 验证。
- **过期：** `expires_at` 到期后不再发送/重试，迟到 device status/ack 不得复活 occurrence 或覆盖 expired/missed 结果。依赖 LifeService 当前 24 小时 catch-up/expiry 规则，并确保数据库状态和 adapter 的过滤窗口一致。
- **用户 ACK：** 仅记录“已看到/收到通知”的回执与来源；不能隐式触发 `reminder.stop_ringing`、`reminder.complete` 或 Companion task ACK。停止响铃/完成仍调用原有独立业务意图。
- **关闭：** delivery worker 停止并等待后移除 listener，避免 close 与回调并发写入已关闭的 `LifeService`。尚未有可证明设备结果的 outbox 留待 Host 重启恢复；不得因正常关闭将 unknown 变成已呈现/播放。

## 5. L2 验收条件及尚缺证据

A3 Host 实现的 L2 contract/unit tests 已由 root 复跑，报告 **63 passed、7 subtests**，覆盖：

- managed app 使用唯一 Context 注册并清理 adapter；delivery loop 会处理 reminder，但无 adapter/pin 不匹配时 fail closed。
- notification ID 稳定；pending、dispatching、ACK 丢失、设备离线、Host 重启与 expiry 后均不制造新 ID；重复事件跨进程/重启去重。
- transport ACK、Host SQLite durable outbox/receipt 与设备易失 accepted 分开；不得声称设备已持久接收，或由 ACK 推断已显示/播放。
- receipt 与 occurrence/outbox 使用同一 LifeService DB 事务边界，状态/ack 分离且不调用 stop/complete；ack source 保留；过期迟到事件不复活提醒。
- close 顺序覆盖 worker、listener、LifeService，并可重复调用。

上述测试最多是 **L2**。本 CR 和现有 adapter 均不证明设备固件实现了 wire handler、状态重报或真实 UI/音频播放。须在硬件与服务可用后依 CR-017/A7 完成 L3/L4：真实 managed Runtime 提醒到期、设备显示/播放完成或 unknown、用户确认，以及 ACK 丢失、断网、Host/设备重启、可能重复呈现/播放、过期和恢复场景。设备 durable storage 不属于验收项。当前没有设备或 SDK service 实测证据，不得声称 L3/L4。

## 6. 待裁决项

1. **已批准且已实现范围：** A3 对 `main.py` 的上述三处窄范围接线及 LifeService 同一 SQLite 库的 receipt schema/API 已完成，并有 L2 测试证据；不包括其他 main.py 行为、设备存储或 wire schema。
2. **CR-018 存储归属已裁决。** Host SQLite 是唯一 canonical durable store；设备只作易失呈现/播放。软件实现可继续推进；真实 reminder delivery 仍须设备/服务 L3/L4 验收，且设备重启边界不承诺 exactly-once。
3. **阶段迁移裁决已由本 CR 第 3.1 节完成**，仅约束 Host 侧归并；A1/A2 若需更改设备状态枚举或 wire 表达，必须另提 CR 并经 A0 裁决。
4. `reminder.alert` 原始 outbox payload 缺 `notification_id`/`expires_at`，A3 已在 Host mapper 中从 `occurrence_id`/`scheduled_at`/`is_catchup` 确定性构造，沿用当前 24 小时规则；任何更改产品 TTL/存储行为仍须另提决策。
