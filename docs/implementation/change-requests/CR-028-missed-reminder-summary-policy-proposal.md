# CR-028：恢复后错过提醒摘要契约

- **提出方：** A0 架构与协议
- **状态：** **APPROVED — 契约与 A3 Host、A2 SDK bridge、A1 volatile handler/UI 软件实现已交付；各实现达到 L2，设备实测 L3/L4 仍未完成**
- **范围：** 冻结全局错过提醒策略、恢复批次、摘要消息及幂等/过期语义；A3 Host、A2 SDK bridge、A1 设备 handler/UI 已按各自独立工作包实现并完成软件验证。
- **关联：** V2.1 用户体验草案 §1、§9；`contracts/schemas/notification_summary.json`；CR-017（单 occurrence `notification.deliver`）；CR-018（Host SQLite canonical storage、设备易失呈现）；CR-019（Host outbox adapter）。
- **证据级别：** 本 CR 与 JSON Schema 为 L1；Host/A1/A2 定向软件实现与测试达到 L2。真实设备摘要显示、重启/重连恢复与现场交互仍需 L3/L4 验收。

## 1. 已批准决策

1. **全局策略：** R1 当前统一采用 `summary_once`。不提供 per-reminder 覆盖，也不保留 `skip` 或 `deliver_individually` 配置入口。后续若需改变策略，必须另行提交 A0 CR。该策略属于 Host 恢复行为，不新增 `domain_life.json` 的提醒创建/更新参数。
2. **候选 occurrence：** 每次 Host 启动或恢复后的 overdue 扫描，只纳入扫描时已经到期，且截至 `scheduled_at + 24h` 尚未完成、取消或归档的 occurrence。超过该 24 小时窗口的 occurrence 按现有 missed/expired 路径处理，不进入摘要。未来 occurrence 不纳入。
3. **批次：** 一次恢复扫描形成一个持久化 recovery batch。批次创建后冻结其候选 occurrence 成员和摘要内容；同一批次在进程重启、设备重连、发送重试或重复扫描时复用原 batch/summary ID 和内容。已纳入批次的 occurrence 不得进入后续摘要批次。
4. **单摘要与上限：** 每个 recovery batch 最多产生一条摘要。每条摘要最多携带 20 个 occurrence，按 `scheduled_at` UTC 时间升序排列；更多候选由 `omitted_count` 精确计数。每个 item 保留其 reminder 的 IANA timezone；Host 计算有界的 `display_time` 本地时间标签并携带 UTC offset，设备按该标签显示，避免固件缺少 IANA 时区数据库时误标。
5. **用户呈现：** 每个恢复批次只产生一条不响铃摘要，不逐条重播，也不为其中 occurrence 另发 CR-017 单条通知。A1 已实现 volatile 摘要 UI 呈现；此 CR 不批准语音播报、声音或交互手势。
6. **过期：** 摘要 outbox 自生成时起有效 24 小时，`expires_at = created_at + 24h`。过期后不再投递，迟到 ACK 不复活摘要。时间比较使用 UTC；Host 按该 item 的 `timezone` 生成并冻结 `display_time`，设备按原值显示，不重新换算。
7. **确认：** 对有效且未过期摘要收到 ACK 时，Host 必须在一个事务中持久写入 summary receipt，并将对应 summary outbox 和 batch 标记为终态 acknowledged，停止该摘要后续投递/重试。ACK 只表示摘要通知已收到/看到，不表示其中任何提醒完成、停止响铃或已处理；ACK 不得调用 `reminder.complete` 或 `reminder.stop_ringing`，也不得改变任何原 occurrence 状态。迟到或已过期摘要的 ACK 忽略，不写入有效 receipt、不复活摘要；重复有效 ACK 幂等，不重复改变状态。
8. **幂等：** `summary_id` 是稳定且全局唯一的摘要通知 ID；同一批次重扫、Host 重启和发送重试必须复用。摘要 outbox、batch 终态和 ACK receipt 均以 `summary_id` 幂等并由 Host SQLite 持久化。设备侧仅可在当前运行期间按此 ID 做 RAM 去重；设备重启会清除此状态，未知结果按同 ID 重投可能重复显示/播报，不承诺 exactly-once。实现须以事务方式持久化 batch、成员关系、冻结 payload 与 outbox，避免 occurrence 已消费但摘要未创建或重复建摘要。

## 2. 摘要消息与 CR-017 兼容边界

CR-017 的 `notification.deliver` 保持原样：`notification_id = occurrence_id`，单条 occurrence、原有 payload allowlist、receipt 和阶段语义均不变。摘要使用独立 SDK 应用命令 `notification.summary.deliver`，其 `data` 由 `contracts/schemas/notification_summary.json` 定义；不得把摘要伪装成一个 occurrence 通知、复用任一 occurrence ID，或扩展 CR-017 payload。

摘要消息字段为：`summary_id`、`batch_id`、`created_at`、`expires_at`、`items`、`omitted_count`。`items` 每项含 `reminder_id`、`occurrence_id`、`title`、`scheduled_at`、`timezone`、`display_time`；`scheduled_at` 为 UTC RFC 3339 时间，`timezone` 保留 IANA 标识，`display_time` 由 Host 以该 IANA 时区格式化。SDK transport 可注入单次命令相关字段 `command_id`；它不是稳定通知 ID，重试时可以变化。

### A0 实施可行性裁决：本地时间格式化边界

固件当前只解析并保存 `scheduled_at` 的 UTC epoch；通知摘要记录仅保存首项标题和汇总计数。固件源码没有可用的 IANA timezone database，也没有按 item 保留完整摘要列表的呈现路径。因此，不能要求设备端直接依据 IANA 字符串转换本地时间。

实现契约采用 Host 格式化：每个 item 增加必填 `display_time`，ASCII 格式固定为 `YYYY-MM-DD HH:MM UTC±HH:MM`，恰好 26 字节，包含本地 UTC offset 以区分 DST 重叠时间。Host 必须依据 `scheduled_at` 与 `timezone` 生成它；无法解析 IANA timezone 或无法计算 offset 时拒绝创建摘要，禁止静默退回设备本地时区或猜测。字段是冻结摘要 payload 的一部分，重投必须保持原值。`timezone` 和 `scheduled_at` 仍是规范数据；`display_time` 是设备只显示、不重新换算的呈现标签。

Host Python 运行时锁定 PyPI `tzdata==2026.5` 作为标准库 `zoneinfo` 的可移植 IANA 数据源。为避免 Linux 优先选用版本未知的系统目录，Host 的时间格式化组件应在构造 `ZoneInfo` 前将该进程的 `TZPATH` 设为空，使标准库使用已锁定的 `tzdata` 包；不得全局安装或在运行时自动升级。发布/部署仍须执行本地已安装版本与 lock 的一致性检查。

- **A3：** 负责生成并严格校验 `display_time`；Host 部署必须提供可信 IANA TZDB。变更冻结 payload 时同步增加 schema validator 与时区边界测试。
- **A2：** 解析新必填字段并透传；缺失、格式错误、额外字段均拒绝，不能自行生成或改写本地时间。
- **A1：** 不实现/打包 timezone database，不用设备当前时区换算；已实现首项标题与 `display_time` 原样呈现，并以 `+N` 显示其余及省略项总数，不逐项展开完整列表。

此项是对原 CR-028 第 4 节时间格式化责任的实现澄清，并不改变提醒候选、摘要/ACK 幂等、存储归属或设备易失状态决策。A3/A2/A1 已按各自 owner 边界完成 schema/wire/Host/firmware 软件兼容并通过定向 L2 验证；这不构成设备实测或端到端恢复验收。

摘要状态/ACK 可复用 CR-017 的 `notification.status` / `notification.acknowledged` 消息类型，但必须以 `summary_id` 关联；A3 receipt 必须区分 summary 与 occurrence，禁止因 ID 相似或形状相同混淆两者。ACK 仍只表达收到/看到。摘要不产生逐 occurrence 的 `played`、`rendered` 或完成状态。A2 SDK envelope、A1 设备解析与呈现的软件实现已交付并达到 L2；真实设备互通仍待实测。

## 3. Owner 与授权边界

| Owner | 后续工作范围 | 当前授权 |
|---|---|---|
| A0 | 维护本 CR 与摘要 schema；裁决跨模块变更 | 本契约已批准 |
| A3 | `host/desk_agent/core/`、`services/` 及对应 Host 测试：恢复批次、事务化成员/摘要 outbox、稳定 ID、24h TTL、单次摘要与 receipt 区分 | **软件实现与定向 L2 验证已完成**；设备/服务端端到端验收仍开放 |
| A2 | `firmware/components/desk_sdk_bridge/`、`tests/sdk_contract/`：验证并解析独立摘要命令及状态/ACK 的 ID 关联 | **软件实现与 pinned Runtime/bridge 定向 L2 验证已完成**；真实设备互通仍开放 |
| A1 | `firmware/components/desk_hardware/`、`desk_tclaw/`、板级配置：设备 handler/UI 呈现，不响铃、不逐项补播 | **volatile 软件实现与 native/contract 定向 L2 验证已完成**；目标设备显示及恢复实测仍开放 |
| A7 | `tests/e2e/`、`tools/verification/`、`docs/acceptance/`：独立验收与证据矩阵 | 按其 ownership 执行；不得改生产代码 |

本 CR 不授权新增或修改 reminder schema 的用户配置字段，不授权实现数据库迁移，不授权修改 CR-017 的 occurrence 消息，不授权新增声音/语音策略，也不授权任何 owner 越过自身路径。

## 4. CR-018 Host 持久化与设备呈现

CR-018 已 **APPROVED**：Host `LifeService` SQLite 是摘要 batch、冻结 payload、outbox 与 receipt 的唯一 durable store；设备仅接收有界摘要用于当前运行期显示，不写 Flash/NVS，也不需要设备持久化 provider。Host 在 SQLite 中持久化摘要投递不等于设备已显示/播报。设备断线或重启导致结果不确定时，Host 在 24 小时有效期内按原 `summary_id` 重投；设备重启边界可能重复显示/播报，因此不承诺 exactly-once。Host 数据库事务不替代真实设备呈现验收。

不得将摘要标题写入未批准的设备存储面。Host 数据库中的摘要 payload/receipt 按 A3 已有数据治理和隐私约束处理；本 CR 不扩大日志内容。

## 5. 最低 L2 验收门槛

各实现工作包至少应以定向测试覆盖：

- 24 小时边界、完成/取消/归档过滤、未来 occurrence 排除及过期 occurrence 归档；
- 多 timezone 条目按 UTC 时刻排序、每批最多 20 项和准确 `omitted_count`，验证 `display_time` 与 `scheduled_at`/IANA timezone 及其 UTC offset 一致；
- 每批恰好一个摘要，不逐条创建 CR-017 通知、不触发逐项响铃；
- batch、成员冻结、摘要 outbox 与扫描消费的事务原子性；重复扫描/并发扫描/进程重启后同一 batch 复用同一 `summary_id` 和 payload，且 occurrence 不重复归批；
- 同 ID 发送重试与 ACK 幂等；有效 ACK 事务化写 receipt 并终止 summary outbox/batch 后续投递；迟到/过期 ACK 忽略；ACK 不修改 occurrence 完成/响铃状态；
- 摘要生成后 24 小时过期，过期后不重发，迟到 ACK 不复活；
- schema validator 拒绝缺字段、额外字段、越界 `items`、负数 `omitted_count`、无效时间和不合法 ID。

Host、bridge、schema 与 native/contract 定向测试证明的软件结果为 **L2**；这些结果不代表真实设备摘要呈现或 Host/设备恢复闭环。

## 6. 尚未验证与后续证据层级

- **L2：** A3 Host、A2 SDK bridge 与 A1 volatile UI 的软件实现已通过各自定向测试；独立测试结果见验收矩阵，不合并为全仓通过数。
- **L3：** 仍需在真实 Edge Host、设备与 pinned SDK Runtime 上验证摘要到达、屏幕显示与无铃声行为；尚未验证。
- **L4：** 仍需验证真实断网、Host/设备崩溃重启、重连、同 ID 重投与过期后的端到端恢复；设备重启边界允许重复呈现/播报，不以“不重复”作为设备持久化验收前提。目前未验证。

Acceptance matrix 中既有 build failure 记录维持原样，由其负责 owner 另行更新；本 CR 不修改验收矩阵或构建状态。
