# CR-028：恢复后错过提醒策略与摘要投递提案

- **提出方：** A0 架构与协议
- **状态：** **PROPOSED — NOT APPROVED；等待产品 owner 选择策略及确认摘要体验**
- **范围：** 定义 Host 恢复/重启后错过提醒的策略选择、恢复窗口归并、摘要通知语义及验收门槛。本文仅为提案，不授权生产代码、现有 wire/schema、数据库或设备存储变更。
- **关联：** V2.1 用户体验草案 §1、§9；contracts/schemas/domain_life.json；CR-017（单 occurrence 通知）；CR-018（设备持久化仍阻塞）；CR-019（Host outbox adapter）；A3 LifeService.get_due_occurrences()。

## 1. 问题与契约差异

V2.1 要求 Edge/Robot 恢复后按 missed_policy 汇总错过事项，不连续补播一串闹铃。当前 LifeService 默认只将不超过 24 小时的到期 occurrence 作为 catch-up 返回；Host scheduler 对返回的 occurrence 逐条建立提醒 outbox。CR-017/CR-019 冻结的通知语义也是单 occurrence：notification_id = occurrence_id，一条 notification.deliver 只含一个提醒标题及其计划时间。

因此“汇总错过项”不能由现契约直接表达。若只在 Host 改成批量投递，会与 CR-017 的 ID、payload allowlist、receipt/outbox 关联和设备去重语义冲突；若照现状逐条投递，则无法满足 V2.1 的“不补发一串闹铃”。本提案记录这项待裁决差异，不默认为任一方案已获产品批准。

## 2. 策略选项与建议

| missed_policy 选项 | 恢复后的行为 | 代价/影响 |
|---|---|---|
| skip | 将已错过 occurrence 记为 missed/expired，不产生设备通知。 | 最少打扰，但用户可能不知道错过了什么。 |
| summary_once **（A0 建议默认；待产品确认）** | 每个恢复批次、每个时区最多生成一条错过摘要；只提示摘要，不逐条响铃。 | 需要新增摘要 payload、稳定摘要 ID、receipt/outbox 归属与设备渲染支持。 |
| deliver_individually | 对仍在现有 24 小时窗口内的 occurrence 逐条按 CR-017 投递。 | 与 V2.1“不补发一串闹铃”的目标冲突；仅可作为显式 opt-in 兼容选项，不建议默认。 |

**待产品确认：** 是否采用 summary_once 作为全局默认；策略是否可按 reminder 覆盖；deliver_individually 是否保留；用户如何选择/修改策略。当前 domain_life.json 未定义 missed_policy，且 reminder 现有字段不含该值。不得将本表中的建议直接当成配置默认或用户已授权选择。

## 3. 建议的摘要批次与时间语义

以下内容是供产品裁决的推荐草案，不在本 CR 状态变更前成为已批准契约：

1. **恢复批次：** 每次 Host scheduler 从启动/失联恢复并完成一次 overdue 扫描，形成一个持久化 recovery batch。批次边界用 UTC recovery_started_at / recovery_completed_at 记录；相同 batch 的重试、进程重启和设备重连必须复用原 batch 与摘要 ID，不得产生第二条摘要。
2. **纳入范围：** 只纳入恢复扫描时已到期且尚未完成、取消或此前已归档的 occurrence；只针对其有效 missed_policy 为 summary_once 的项目。未来 occurrence 不纳入。保留当前 24 小时 catch-up 候选边界作为**提案基线**：超过 scheduled_at + 24h 的 occurrence 不播放，也不进入本摘要，按明确的 missed/expired 归档路径处理。若产品希望摘要包含更旧的错过项，须另行确认历史查询范围与隐私/数量上限。
3. **摘要粒度：** 每个 recovery batch 按 reminder 所属 IANA 时区分组，每个 (batch_id, timezone) 生成最多一条摘要；组内按 scheduled_at 升序，按 occurrence 去重。允许摘要显示提醒标题、该 occurrence 的本地计划日期/时间；日志不得记录标题。建议 payload 上限 20 个项目，超出时带 omitted_count；此上限需产品确认，且设备不得将截断理解为未处理/未过期。
4. **时区：** 排序、过期和幂等均使用 UTC 时间戳；面向用户的日期/时间使用 occurrence 所属 reminder 的 IANA timezone（现有默认 Asia/Shanghai）。不得使用 Host 当前系统时区，也不得用本地墙钟比较 TTL。跨时区 occurrence 分属不同摘要，避免一个摘要用单一时区误标其他项目。
5. **不连环补播：** 摘要是普通的一次性 notification，不启动持续响铃、不逐 item 播放、不为每个被汇总 occurrence 再发 CR-017 单条 notification.deliver。设备可以显示摘要卡片并按产品确认的交互播报一段摘要；声音播放次数/用户确认手势仍待产品与 A1 裁决。通知 ACK 只确认摘要被用户看到/收到，不表示任一提醒已完成。
6. **过期：** 被汇总 occurrence 的原始 catch-up 时限仍为 scheduled_at + 24h（除非产品另行批准更改）。摘要本身建议设置 delivery_expires_at = recovery_completed_at + 24h，以便设备离线短时恢复后仍可收到一次摘要；过期后不得补播。摘要投递有效期与 occurrence catch-up 有效期是两个概念，不可复用 expires_at 混淆。该 24h 摘要窗口及摘要生成时限待产品确认。

## 4. 建议的摘要身份、持久化与重启语义

1. 每个摘要使用独立 notification_id，不等于任一 occurrence_id。建议由 Host 事务化分配不可变 batch_id 和摘要记录，并以 summary_id 作为通知幂等键；摘要内容的 occurrence ID 列表在该记录创建后冻结。相同恢复批次的重跑必须查询原记录并复用同一 summary_id。不要仅凭当前时间或排序后的内容临时生成 ID，因为重启、时区库更新或并发扫描可能改变输入集合。
2. 在一个 LifeService 事务中冻结候选 occurrence 集合、写入摘要 outbox 与摘要 receipt 上下文，再推进 scheduler 扫描游标；事务失败则整体重试，不得出现“occurrence 已被消费但摘要不存在”或重复生成摘要。原始 occurrence 的状态仍逐个保留；摘要 ACK 不改 occurrence 完成/取消状态，也不调用 reminder.complete / reminder.stop_ringing。
3. Host 重启恢复时恢复未过期摘要 outbox，保持同一个 summary_id、冻结内容和原 delivery_expires_at；设备重连重发沿用该 ID。重复摘要按 ID 幂等，不重复播放。到期后标 expired，迟到状态/ACK 不复活摘要。
4. 当前 CR-018 仍未批准设备 durable provider。即使摘要 wire 契约将来获批，设备不得在无获批持久 provider 时宣称 durable accepted；Host 保持 unknown/pending 并遵循届时批准的 fail-closed 与过期语义。此 CR 不选择 flash 分区、不授权标题持久化，也不解除 CR-018 阻塞。
5. 重复执行扫描时，已归入某个摘要的 occurrence 不得再次归入新 batch；新增 occurrence 可进入后续 batch。需要由 A3 保证 DB 唯一约束/事务隔离和可恢复批次标识，A7 覆盖并发扫描及崩溃注入。

## 5. 与现有契约冲突及受影响 owners

本提案未更改下列文件；批准后应拆成各 owner 的实现包和必要子 CR：

| 现有项 | 冲突/影响 | Owner（批准后） |
|---|---|---|
| contracts/schemas/domain_life.json reminder schema | 缺 missed_policy；需要确定字段为全局默认、reminder 属性或两者及默认值/兼容规则。 | A0 冻结字段契约；A3 实现存取和迁移。 |
| contracts/schemas/domain_life.json occurrence/响应模型 | 目前没有 recovery batch 或 summary 查询/策略字段；需决定是否公开策略和摘要状态。 | A0/A3 |
| CR-017 notification_id = occurrence_id 与单标题 payload allowlist | 摘要 ID 不对应单 occurrence，且 payload 需要多 item 和 batch/timezone/window 元数据；须扩展 notification kind/payload/status 关联语义，或单独批准新通知类型。不得静默复用旧字段。 | A0 裁决本地协议；A2 bridge；A1 device handler/UI/audio。 |
| CR-017/CR-019 occurrence outbox 与 receipt | 当前一 occurrence 对应一 notification；摘要需 batch 摘要实体、摘要级 receipt 和 occurrence membership 关联。旧 receipt 不得被重解释为摘要 receipt。 | A3 |
| CR-018 设备端持久化 | 摘要的 ID 去重和重启语义仍需设备持久层；标题/items 是否可持久化须由产品/存储 owner 另行批准。 | 产品/存储 owner、A0；A1 实现获批 provider 后接线。 |
| reminder/occurrence API 和产品交互 | 需确定策略设置入口、摘要查看/确认、长摘要截断和关机跨期规则。用户确认摘要不自动完成提醒。 | 产品 owner、A3；A1 对接设备 UI。 |
| 设备 SDK 能力与消息通道 | pinned SDK 当前合同未证明摘要消息或多 item notification 支持。 | A2/A1；依真实 SDK/设备证据评估。 |

不得由任一实现 owner 私自修改 contracts/schemas/*、CR-017 wire payload 或设备命令来绕过本 CR 的产品决策。若选定摘要路线，A0 需先出具明确批准版本及兼容迁移方案；A1/A2/A3 随后按各自 ownership 分包实施。

## 6. 验收门槛

**产品/A0 决策门（进入实现前）：** 产品 owner 明确选择默认策略及覆盖范围；确定摘要纳入窗口、批次边界、最大条目数/截断、摘要 TTL、通知交互/语音行为、跨关机/跨日处理，并签署现有 CR-017 单 occurrence 契约的扩展方案。决策前，本 CR 保持 NOT APPROVED，现有实现行为不得被描述为满足摘要需求。

**L2 软件契约/模拟：** 批准后由 A3/A2/A1 补充 schema 与实现测试，至少覆盖 skip/summary_once（以及若保留则 deliver_individually）、UTC expiry、IANA 时区/夏令时显示、分组/排序/上限截断、并发扫描、DB 原子提交、崩溃前后恢复、重复批次/发送/ACK、过期迟到事件、同一 occurrence 不重复入摘要、ACK 不完成提醒、标题不写日志，以及无 provider 时 fail-closed。Schema validator、定向单测和故障注入模拟最多为 L2。

**L3 节点实测：** 在真实 Edge Host、设备与 pinned SDK Runtime 上创建多个一次性/重复提醒，制造 Host 关机、设备离线和跨时区/跨日恢复；观察摘要显示、实际播报次数、通知状态与同 ID 重连重试。证明没有逐条补播，并分别记录设备、Host、SDK 日志。单节点结果为 L3，不代表长期恢复可靠。

**L4 端到端恢复：** 执行 Host/设备在摘要创建、发送、设备接收、显示/播报和 ACK 前后的断网、进程崩溃、重启与重复投递；验证摘要稳定 ID/内容、occurrence membership、过期清理、用户确认与 reminder 完成状态分离、服务恢复无静默丢失/重复播报。记录配置、时区、固件/Host/SDK 版本与可复现证据。未完成这些真实场景前，不得宣称恢复后错过提醒闭环已通过。

## 7. 产品待选清单与当前状态

1. 是否采纳 summary_once 为全局默认；是否提供 per-reminder 覆盖；哪些提醒类别可设为 skip。
2. deliver_individually 是否作为用户显式 opt-in 保留；若保留，怎样限制连续声音打扰。
3. 24 小时 catch-up 窗口外的旧 occurrence 是仅归档、纳入摘要，还是另设更长历史窗口。
4. 摘要按恢复批次与时区分组是否合适；批次何时结束，连续 scheduler 扫描如何归并；跨多个本地日期是否合并。
5. 每摘要条目上限、截断呈现、摘要通知 TTL、设备离线超过摘要 TTL 的行为。
6. 设备显示/语音行为、摘要查看/确认手势、确认是否仅表示收到（A0 建议是），以及如何查看被截断项目。
7. 策略/时区修改对已生成、尚未发送摘要的生效规则；重复提醒 occurrence 的历史处理规则。
8. 批准摘要通知扩展现有 CR-017 还是新建通知类型；对应 SDK/固件兼容能力和 Host/设备 schema 版本策略。

**当前裁决：** 无产品选择已收到；推荐只作为讨论起点。本文不修改生产代码、现存 schema、分区/存储、CR-017/CR-019，也不证明 L2/L3/L4。
