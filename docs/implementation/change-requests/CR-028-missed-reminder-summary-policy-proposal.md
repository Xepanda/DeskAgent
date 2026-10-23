# CR-028：恢复后错过提醒摘要契约

- **提出方：** A0 架构与协议
- **状态：** **APPROVED — R1 本地产品/协议契约；尚未授权各业务 owner 实现**
- **范围：** 冻结全局错过提醒策略、恢复批次、摘要消息及幂等/过期语义。A3 Host、A2 SDK bridge、A1 设备 handler/UI 后续按各自独立工作包实施。
- **关联：** V2.1 用户体验草案 §1、§9；`contracts/schemas/notification_summary.json`；CR-017（单 occurrence `notification.deliver`）；CR-018（Host SQLite canonical storage、设备易失呈现）；CR-019（Host outbox adapter）。
- **证据级别：** 本 CR 与 JSON Schema 是 L1 契约；其本身不代表代码实现、节点实测或端到端恢复通过。

## 1. 已批准决策

1. **全局策略：** R1 当前统一采用 `summary_once`。不提供 per-reminder 覆盖，也不保留 `skip` 或 `deliver_individually` 配置入口。后续若需改变策略，必须另行提交 A0 CR。该策略属于 Host 恢复行为，不新增 `domain_life.json` 的提醒创建/更新参数。
2. **候选 occurrence：** 每次 Host 启动或恢复后的 overdue 扫描，只纳入扫描时已经到期，且截至 `scheduled_at + 24h` 尚未完成、取消或归档的 occurrence。超过该 24 小时窗口的 occurrence 按现有 missed/expired 路径处理，不进入摘要。未来 occurrence 不纳入。
3. **批次：** 一次恢复扫描形成一个持久化 recovery batch。批次创建后冻结其候选 occurrence 成员和摘要内容；同一批次在进程重启、设备重连、发送重试或重复扫描时复用原 batch/summary ID 和内容。已纳入批次的 occurrence 不得进入后续摘要批次。
4. **单摘要与上限：** 每个 recovery batch 最多产生一条摘要。每条摘要最多携带 20 个 occurrence，按 `scheduled_at` UTC 时间升序排列；更多候选由 `omitted_count` 精确计数。每个 item 保留其 reminder 的 IANA timezone，设备按各自 timezone 格式化本地时间，避免跨时区误标。
5. **用户呈现：** 每个恢复批次只产生一条不响铃摘要，不逐条重播，也不为其中 occurrence 另发 CR-017 单条通知。设备可以按 A1 后续获批实现显示摘要；此 CR 不批准语音播报、声音、交互手势或 UI 形式。
6. **过期：** 摘要 outbox 自生成时起有效 24 小时，`expires_at = created_at + 24h`。过期后不再投递，迟到 ACK 不复活摘要。时间比较使用 UTC；每项本地时间显示由该 item 的 `timezone` 指定。
7. **确认：** 对有效且未过期摘要收到 ACK 时，Host 必须在一个事务中持久写入 summary receipt，并将对应 summary outbox 和 batch 标记为终态 acknowledged，停止该摘要后续投递/重试。ACK 只表示摘要通知已收到/看到，不表示其中任何提醒完成、停止响铃或已处理；ACK 不得调用 `reminder.complete` 或 `reminder.stop_ringing`，也不得改变任何原 occurrence 状态。迟到或已过期摘要的 ACK 忽略，不写入有效 receipt、不复活摘要；重复有效 ACK 幂等，不重复改变状态。
8. **幂等：** `summary_id` 是稳定且全局唯一的摘要通知 ID；同一批次重扫、Host 重启和发送重试必须复用。摘要 outbox、batch 终态和 ACK receipt 均以 `summary_id` 幂等并由 Host SQLite 持久化。设备侧仅可在当前运行期间按此 ID 做 RAM 去重；设备重启会清除此状态，未知结果按同 ID 重投可能重复显示/播报，不承诺 exactly-once。实现须以事务方式持久化 batch、成员关系、冻结 payload 与 outbox，避免 occurrence 已消费但摘要未创建或重复建摘要。

## 2. 摘要消息与 CR-017 兼容边界

CR-017 的 `notification.deliver` 保持原样：`notification_id = occurrence_id`，单条 occurrence、原有 payload allowlist、receipt 和阶段语义均不变。摘要使用独立 SDK 应用命令 `notification.summary.deliver`，其 `data` 由 `contracts/schemas/notification_summary.json` 定义；不得把摘要伪装成一个 occurrence 通知、复用任一 occurrence ID，或扩展 CR-017 payload。

摘要消息字段为：`summary_id`、`batch_id`、`created_at`、`expires_at`、`items`、`omitted_count`。`items` 每项含 `reminder_id`、`occurrence_id`、`title`、`scheduled_at`、`timezone`；`scheduled_at` 为 UTC RFC 3339 时间，设备按 item 的 IANA timezone 格式化显示。SDK transport 可注入单次命令相关字段 `command_id`；它不是稳定通知 ID，重试时可以变化。

摘要状态/ACK 可复用 CR-017 的 `notification.status` / `notification.acknowledged` 消息类型，但必须以 `summary_id` 关联；A3 receipt 必须区分 summary 与 occurrence，禁止因 ID 相似或形状相同混淆两者。ACK 仍只表达收到/看到。摘要不产生逐 occurrence 的 `played`、`rendered` 或完成状态。具体 SDK envelope、设备解析和呈现需 A2/A1 后续工作包核对与实现。

## 3. Owner 与授权边界

| Owner | 后续工作范围 | 当前授权 |
|---|---|---|
| A0 | 维护本 CR 与摘要 schema；裁决跨模块变更 | 本契约已批准 |
| A3 | `host/desk_agent/core/`、`services/` 及对应 Host 测试：恢复批次、事务化成员/摘要 outbox、稳定 ID、24h TTL、单次摘要与 receipt 区分 | **未授权业务实现**；需独立实施包，且不得改 wire/schema |
| A2 | `firmware/components/desk_sdk_bridge/`、`tests/sdk_contract/`：验证并解析独立摘要命令及状态/ACK 的 ID 关联 | **未授权代码实现**；需独立实施包 |
| A1 | `firmware/components/desk_hardware/`、`desk_tclaw/`、板级配置：获批设备 handler/UI 呈现，不响铃、不逐项补播 | **未授权代码或设备存储实现**；需独立实施包 |
| A7 | `tests/e2e/`、`tools/verification/`、`docs/acceptance/`：独立验收与证据矩阵 | 按其 ownership 执行；不得改生产代码 |

本 CR 不授权新增或修改 reminder schema 的用户配置字段，不授权实现数据库迁移，不授权修改 CR-017 的 occurrence 消息，不授权新增声音/语音策略，也不授权任何 owner 越过自身路径。

## 4. CR-018 Host 持久化与设备呈现

CR-018 已 **APPROVED**：Host `LifeService` SQLite 是摘要 batch、冻结 payload、outbox 与 receipt 的唯一 durable store；设备仅接收有界摘要用于当前运行期显示，不写 Flash/NVS，也不需要设备持久化 provider。Host 在 SQLite 中持久化摘要投递不等于设备已显示/播报。设备断线或重启导致结果不确定时，Host 在 24 小时有效期内按原 `summary_id` 重投；设备重启边界可能重复显示/播报，因此不承诺 exactly-once。Host 数据库事务不替代真实设备呈现验收。

不得将摘要标题写入未批准的设备存储面。Host 数据库中的摘要 payload/receipt 按 A3 已有数据治理和隐私约束处理；本 CR 不扩大日志内容。

## 5. 最低 L2 验收门槛

各实现工作包至少应以定向测试覆盖：

- 24 小时边界、完成/取消/归档过滤、未来 occurrence 排除及过期 occurrence 归档；
- 多 timezone 条目按 UTC 时刻排序、每批最多 20 项和准确 `omitted_count`，显示时保留各 item 的 IANA timezone；
- 每批恰好一个摘要，不逐条创建 CR-017 通知、不触发逐项响铃；
- batch、成员冻结、摘要 outbox 与扫描消费的事务原子性；重复扫描/并发扫描/进程重启后同一 batch 复用同一 `summary_id` 和 payload，且 occurrence 不重复归批；
- 同 ID 发送重试与 ACK 幂等；有效 ACK 事务化写 receipt 并终止 summary outbox/batch 后续投递；迟到/过期 ACK 忽略；ACK 不修改 occurrence 完成/响铃状态；
- 摘要生成后 24 小时过期，过期后不重发，迟到 ACK 不复活；
- schema validator 拒绝缺字段、额外字段、越界 `items`、负数 `omitted_count`、无效时间和不合法 ID。

这些 Host/bridge mock、schema 与定向测试最多证明 **L2**。当前无设备/服务凭据参与，也无真实节点验收证据。

## 6. 尚未验证与后续证据层级

- **L2：** 需由 A3/A2/A1 各自后续实施并通过上述定向软件测试；本契约批准不代表 L2 已通过。
- **L3：** 需在真实 Edge Host、设备与 pinned SDK Runtime 上验证摘要到达、UI 显示与无铃声行为；目前未验证。
- **L4：** 需验证真实断网、Host/设备崩溃重启、重连、同 ID 重投与过期后的端到端恢复；设备重启边界允许重复呈现/播报，不以“不重复”作为设备持久化验收前提。目前未验证。

Acceptance matrix 中既有 build failure 记录维持原样，由其负责 owner 另行更新；本 CR 不修改验收矩阵或构建状态。
