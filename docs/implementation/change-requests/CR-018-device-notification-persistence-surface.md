# CR-018：提醒通知的 Host 持久化与设备易失呈现

- **提出方：** A0 架构与协议
- **状态：** **APPROVED（R1 存储归属与重启语义；实现及设备/服务验收仍未完成）**
- **关联：** [系统集成计划](../superpowers/plans/2026-10-11-desk-agent-system.md)、[CR-017](CR-017-managed-reminder-delivery.md)、[CR-019](CR-019-managed-reminder-host-outbox-adapter.md)、[CR-028](CR-028-missed-reminder-summary-policy-proposal.md)、`host/desk_agent/services/life_service.py`
- **范围：** 冻结提醒/通知的持久化 owner、事务边界、重启与设备接收语义。不授权分区表变更、固件 Flash/NVS 写入或自动格式化。

## 1. 裁决

**Host `LifeService` 使用的 SQLite 数据库是提醒 occurrence、reminder outbox、notification receipt、恢复摘要及其 ACK/事件去重的唯一 canonical durable store。** 默认数据库路径为 `/var/lib/desk-agent/life_service.db`，实际路径由 Host `storage.db_path` 配置决定。Host 是提醒调度和恢复的常驻服务；设备不是 durable store。

ESP32-S3 仅接收 schema allowlist 约束的有界通知/摘要字段，用于当前运行期间的显示和语音/提示播放。设备不得将标题、通知 ID、投递阶段、去重记录、ACK 或摘要写入 Flash、NVS、SPIFFS、Tuya KV、分区尾部或其他持久介质；不得为通知创建或复用固件持久化 provider。设备 RAM 中可做当前运行期的限额缓存，但该缓存不构成 durable acceptance，重启后清空。

因此，本 CR 的标题问题不再是固件存储选择：通知标题仅保存在 Host 已授权的 reminder/outbox 数据中，传给设备时服从现有 payload/schema 上限；设备掉电后由 Host SQLite 中仍有效的 outbox 按原稳定 ID 重试。此策略避免对分区表、资产 SPIFFS、Tuya 数据区或凭据区提出任何写入需求。

## 2. Host SQLite 契约

### 数据所有权和事务

- Reminder、occurrence、`reminder.alert` outbox、`notification_receipts`、`notification_receipt_events`、recovery batch、summary outbox 与 ACK receipt 均归属于同一个 `LifeService` SQLite 数据库；禁止另建 JSON/第二数据库或用进程内状态替代持久状态。
- 现有源码在文件数据库启用 SQLite WAL；同一 `LifeService` 的锁与 SQLite transaction 负责串行化访问。不要将 WAL 本身当作备份。
- 创建/更新 occurrence 及对应 outbox 必须在单一事务中提交；事务失败不得向调用方报告已保存或已排队。
- 投递前以稳定 `notification_id = occurrence_id` 幂等创建 receipt，验证其关联的 reminder/outbox 与 expiry。设备状态事件和 ACK 必须在 SQLite 事务中先写入唯一 fingerprint，再归并 receipt 状态；重复 fingerprint 幂等。回执不得改变提醒完成/响铃状态。
- Host `accepted/queued` 只可表示 Host 已持久保存并接管投递，不得表述为设备已持久接收、已显示、已播放或用户已确认。设备 transport ACK 只表示当前命令/连接的传输应答。

### Host 重启、故障和恢复

- Host 正常关闭或崩溃重启后，从同一 SQLite 文件恢复尚未完成且未过期的 occurrence/outbox、通知回执和恢复摘要；重试沿用原 occurrence/notification/summary ID，不创建新 ID 来规避未知结果。
- SQLite 打不开、事务失败、磁盘只读/满或数据库完整性失败时，提醒创建/更新与投递状态变更均 fail closed：不得声称写入成功或将 outbox 标记为 dispatched；保留可恢复的数据库文件，报告可诊断的 Host 存储错误，不自动删除、重建或清空数据库。
- 备份/恢复必须覆盖 SQLite 数据库及一致的 WAL 状态，使用 SQLite 支持的一致性备份机制；恢复后按 ID 幂等继续。备份路径、频率和保留策略遵循 Host 部署配置，本 CR 不虚构一个未配置的备份周期。
- 每个 reminder 的有效期继续使用 CR-017 既有 24 小时 catch-up/expiry 规则；摘要继续使用 CR-028 的创建后 24 小时有效期。过期后不投递、不接受迟到设备事件/ACK 来复活提醒。
- 当前数据库没有经批准的固定容量配额，也没有 receipt/outbox 的 GC/retention 实现可作为容量依据。本 CR 不添加任意字节/行数上限或提前删除规则。容量监控、备份保留和安全 GC 若需要产品值，另立 Host 存储运维决策；在容量或写入错误时 fail closed。

## 3. 设备易失接收、重启和重复语义

- 设备只处理 allowlist 内的有限字段，执行当前会话可完成的呈现/播放并回报可证明的状态；内存不足、未初始化、仲裁忙、命令过期或显示/播放失败时返回明确失败/unknown，不得假成功。
- 设备不得发送“已持久接受”语义。设备应用层的 `accepted`（如果保留为协议阶段）只表示本次运行中已将命令放入易失处理队列，不能作为 Host outbox durable dispatch 的证据。transport `sys.ack` 更不表示呈现或播放完成。
- 设备重启会清除通知队列与内存去重状态；Host 根据 SQLite 中原 outbox/receipt 和原 expiry 恢复投递。设备端的 RAM 去重只能减少单次运行内重复呈现，不提供跨重启 exactly-once。
- ACK/状态事件已由 Host SQLite 持久记录时，Host 可按对应回执继续；若发送结果或状态事件丢失而 Host 无法证明呈现结果，Host 必须保留 unknown 并在有效期内用原 ID 重试。因设备不持久去重，此恢复策略可能造成重启/断线边界上的重复显示或播报；不得声称 exactly-once。需要避免该重复时，只能新增可靠的设备侧执行确认/恢复机制决策，不能默默写 Flash/NVS。
- Host 已持久记录 `rendered`/`played` 等更高阶段后，不因重连或重启自动重播。`played` 仅在设备实际播放完成信号可证明时使用；设备 ACK 不可替代。
- 单条通知继续遵守 CR-017 的 payload allowlist 和字段限制。错过提醒摘要继续遵守 CR-028：每个 batch 至多 20 项，摘要有效期 24 小时；设备只接收该有界摘要用于呈现，不持久化。不得把完整备忘正文、转写、凭据或任意日志发给设备。

## 4. 错误处理与安全边界

- 固件通知 handler 不再因“缺少持久 provider”拒绝命令；实现应移除该路径上的 `STORAGE_UNAVAILABLE` 前置条件。设备端错误仅表示输入/认证、过期、资源忙、硬件呈现/播放或运行期队列等实际问题，沿用已批准的 SDK NACK/event 语义。
- Host 在数据库不可用时不得向设备发新通知，也不得返回提醒已持久创建/已排队。设备在线并不允许绕过 Host canonical store 直接接受 durable 状态。
- 本裁决不允许修改 `firmware/partitions.csv`、生成式分区表、`sdkconfig`、启动 NVS/Tuya KV 流程或 UI/动画资产分区；不读取或写入生产凭据，不触碰 Flash。
- Host 数据库包含提醒标题、通知回执等用户数据，按 Host 文件权限和备份策略保护；本 CR 不授权将数据库复制到设备或传输完整备忘内容。

## 5. Owner 与实施拆分

- **A0：** 维护本契约及跨模块语义；不实现业务代码。
- **A3：** 以 `LifeService` SQLite 为唯一 durable source，实现并验证 occurrence/outbox/receipt/summary 事务、启动恢复、同 ID 幂等、数据库错误 fail-closed 和一致性备份/恢复；清除“等待设备持久存储”的 Host 阻塞条件。
- **A1：** 设备只做受限 RAM 队列、显示/播放和可证明状态上报；不注册 durable provider，不写固件持久介质。硬件呈现/播放仍需设备实测。
- **A2：** 转发 allowlist 命令与设备事件；不得把 transport ACK 或设备易失 `accepted` 升格为 Host durable device delivery。
- **A7：** 对 Host 进程/主机重启、数据库不可用、设备离线/重启、ACK 丢失、重复发送与过期执行独立验证；真实设备/服务证据分层记录。

## 6. 验收与证据等级

- **L1（本裁决）：** 系统计划明确提醒持久化由常驻 Host 服务承担；`LifeService` 源码已有 SQLite WAL、outbox、notification receipt/event 表及事务 API。该源码事实不等于完整恢复/运维验收。
- **L2（软件待验）：** 针对数据库事务中断、同 ID 重试、Host 关闭/重启恢复、重复事件 fingerprint、过期过滤、数据库只读/满/损坏与一致性备份恢复的测试全部通过；不得因 transport ACK 标记设备已显示/播放。
- **L3/L4（未验证）：** 真实设备显示/播放、用户确认、Host/设备重启、断网重连与结果不确定边界仍需真实硬件和服务验证。本 CR 不宣称该验收已完成。
