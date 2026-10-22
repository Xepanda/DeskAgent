# CR-018：设备通知去重与确认回执的持久化存储面

- **提出方：** A0 架构与协议
- **状态：** **BLOCKED：等待产品/存储 owner 决定 app-owned 持久区的归属与容量**
- **关联：** CR-017、`contracts/dependency_lock.json` 的 `partition_table` 与 `partition_safety_invariants`、`firmware/partitions.csv`
- **范围：** 仅定义 CR-017 所需设备端通知去重/阶段/确认回执的存储契约。本文不授权格式化、分区表变更、擦写任何 Flash 地址或创建新运行时存储实例。

## 1. 裁决：当前没有获批的 app-owned 持久化面

当前固件的 `storage` SPIFFS 分区为 4 MiB（`0xD20000..0x111FFFF`），依赖锁和镜像清单将其归属 UI/动画静态资源。当前源码没有该分区的 SPIFFS mount，也没有 DeskAgent app-owned KV API。`desk_watcher_storage_init()` 注册的是 Flash 尾部 Tuya store（`tuya`）及 marker（`deskmeta`），不是 DeskAgent 应用 KV API；这段区域不得挪用。

`nvs`（`0x9000`）、`phy_init`（`0x11000`）、`agentcfg`（`0x12000`，Tuya 凭据/license）、`model`（`0xC20000`，模型）、Tuya tail，以及未明确所有权的其他分区均不属于本功能的候选写入面。不得修改 `firmware/partitions.csv`、生成式分区表、启动时 NVS 流程或现有资产分区内容来绕过此缺口。

因此，本 CR **不指定当前可写分区**，设备端 durable provider 尚未批准。A1 sink 必须在 provider 缺失、未挂载或不可用时 fail closed：不发送 `accepted`、不播放/展示会被误认作已持久接收的通知；Host 保持 pending/unknown 并按 CR-017 的有效期规则处理。RAM 缓存不能替代持久去重，也不得报告为 durable acceptance。

另有一项影响设备重启恢复的产品决策尚未作出：如果设备在持久记录 `accepted` 后、首次展示前重启，恢复呈现需要可用的提醒标题。当前产品契约要求设备能恢复同 occurrence 状态，但尚未决定是否允许将标题短期持久化。**不得默认把用户标题写入持久介质。**

## 2. 需要产品/存储 owner 作出的决定

在实现设备持久化前，产品 owner/存储 owner 必须书面选择且授权存储面以及以下重启语义之一：

1. **允许短期持久化待展示标题**：明确提醒标题可以在设备本地被短期保存，批准其保护方式和清理规则，并确认可用的加密密钥来源不使用受保护的 `nvs` / `agentcfg` / Tuya tail；若没有已批准的安全密钥来源，不得声称加密存储。标题在状态到达 `rendered` 或 `expired` 后立即清除，只保留 ID、阶段和确认回执 tombstone 至 TTL 结束。这样设备才能在重启后恢复尚未展示的提醒。
2. **不允许持久化标题**：设备只持久化去重 ID/阶段；若重启时记录仍为 `accepted` 且尚未 `rendered`，设备上报 `delivery_unknown`，不自行恢复展示/播报，Host 不得将其报告为已送达。接受该场景下用户可能错过提醒，并明确 Host 是否会在设备重连后用同一 ID 和原始 outbox payload 重发以恢复投递；这类重发必须继续按同 ID 去重。

存储位置另须选择以下之一：

1. **批准现有 `storage` SPIFFS 中一个 app-owned 子命名空间和固定配额**，同时确认 UI/动画供应物不会覆盖、格式化或回收该命名空间，并允许 A1 增加不带自动格式化的 mount/provider；或
2. **指定由产品维护的独立 app-owned 存储区域**，并另行批准相应分区表/镜像/升级迁移方案与工厂兼容性验证。

当前工作包及 CR-017 批准都不包含上述产品决定。不得把“storage 分区有 4 MiB”解释为它全部空闲或可以直接挂载、格式化或写入。若没有决策，通知 durable dedupe/ack persistence 保持阻塞，不能宣称设备重启恢复闭环。

## 3. 决策后必须遵守的逻辑存储契约

以下是 backend provider 的要求；**不是当前分区兼容声明，也不批准将候选空间落盘**。

### Namespace 与记录

- DeskAgent 逻辑 namespace 固定为 `deskagent.notification.v1`；物理文件名/目录由 A1 backend 适配产品批准的介质，不得与 UI/动画资产命名空间冲突。
- 每条记录只保存：`notification_id`（即 CR-017 的 `occurrence_id`）、投递状态（`accepted | rendered | played | failed | expired | unknown`）、独立的 `user_acknowledged` 布尔值和可选 `acknowledged_at`、`first_received_at`、`expires_at`、`retain_until`、单调 `sequence`、格式 `schema_version` 和 CRC32。通知回执与投递阶段必须可分别恢复；确认不能覆盖/降级已记录的 played/failed 阶段。
- 标题是否持久化由第 2 节产品决策控制：若批准短期持久化，只能保留恢复 pending presentation 所必需的标题，遵循经批准的保护方式和清理期限；否则记录不得包含标题，并采用 `delivery_unknown`/不恢复呈现语义。任何模式都不得持久化完整提醒正文、转写、凭据或其他业务上下文。
- 对同一个 ID 的重发返回已持久化状态，不重复触发呈现/播放；`notification.acknowledged` 仅更新通知回执，不得改变响铃/提醒完成状态。

### 原子提交与完整性

- `accepted` 只能在该 ID 的接收记录已原子提交并回读校验后返回。每个状态迁移（包括 acknowledgement）必须先持久提交，再对 Host 发出对应事件。
- 每条编码记录包含 schema version、单调 sequence 与 CRC32。写入必须采用具备恢复规则的双槽/追加日志事务：先写完整候选记录，再写提交标记；重启时只恢复 CRC 有效且提交完整的最新记录。provider 必须保证掉电中断不会让半条记录被解释为成功。
- 不支持可靠同步/原子提交的介质或 API 不符合 provider 契约。CRC 错误、版本未知、截断记录或提交标记不完整时，禁止默默重置、格式化、覆盖旧数据或丢弃去重状态；标记 provider degraded 并 fail closed。

### 容量与去重保留期

- 产品批准的 app-owned 配额至少 **128 KiB**，其中需容纳至少 **128 个 ID**、每条编码记录最多 **512 bytes**（64 KiB 记录空间，满足可选的短期标题字段），其余空间用于索引、双槽/日志和垃圾回收余量。此数字是设计下限，不代表现有 `storage` 具备可用配额。
- 去重记录最早只能在 `retain_until = max(expires_at + 1 hour, first_received_at + 25 hours)` 后回收，以覆盖 Host 的 24 小时 reminder catch-up 期限和迟到重试。时间不可信或不可用时，不得提前 GC；需要等待可信 UTC 或保留记录。
- 达到保留期后可做增量回收，但不得触发全分区格式化。写入放大/擦除寿命必须由 A1 对实际 backend 评估；超出容量预算时返回明确 `storage_full`，不可驱逐仍在保留期内的 ID。

### 缺失、满载与损坏

- Provider 缺失或未挂载：拒绝 durable `accepted`，不呈现/播放，向 Host 报可区分错误 `storage_unavailable`。
- 配额满：拒绝新的 ID 并返回 `storage_full`；已有 ID 查询/重试仍返回其已持久状态。不得静默淘汰尚在保留期内的记录。
- CRC/版本/日志损坏：进入持久化故障状态，阻止新通知确认/播放；保留原始数据供诊断，不自动格式化或擦除。恢复需明确的维护/迁移决策。
- 原子写入/回读失败：不返回 accepted，不发状态已落盘事件；若结果不确定，回报 `storage_error` 并 fail closed，Host 保留 unknown/retry 同一 ID。

## 4. Owner 与下一步

- **产品/存储 owner：** 决定是否允许本地短期持久化标题，明确加密/密钥与清理策略或接受 reboot 后 unknown、不恢复呈现；并决定是否允许在 UI/动画资产 SPIFFS 中划出独立 namespace/quota，或提出独立 app-owned 存储方案。未决期间不授权任何物理写入。
- **A0：** 在决定落地后更新依赖/分区所有权契约，复核是否触及工厂布局、OTA 和资产升级流程；若需改分区表，必须另立明确批准的变更与迁移方案。
- **A1：** 实现 durable provider callback，遵守 namespace、原子性、CRC、容量/TTL 和 fail-closed 规则；按产品决定实现标题短期加密持久化，或 reboot 后 `delivery_unknown` 且不恢复呈现。禁止写 NVS、agentcfg、model、Tuya tail，禁止初始化时自动 format。
- **A2：** 只转发设备状态事件，不将 command ACK 升格为存储持久化或播放完成证明。
- **A3：** 保持 Host outbox pending/unknown，按同一 `notification_id` 重试；不得因设备存储错误标记 dispatched。
- **A7：** 经产品批准的真实存储面可用后，验证掉电注入、部分写、CRC 损坏、满载、重启去重、过期回收和拒绝写保护区；当前无设备/存储验收结论。

## 5. 证据等级与限制

- `firmware/partitions.csv`、`contracts/dependency_lock.json`、`docs/implementation/r1-build-and-image-manifest.md` 和 `firmware/main/src/watcher_storage.c` 的本地源码核对为 **L1**：可证明已记录分区布局、保护声明和 Tuya-tail 注册逻辑。
- 当前无 SPIFFS mount / app KV provider 的源码路径证据；不代表所有未检索组件绝对不存在此类外部实现。
- 未访问真实设备 Flash、未格式化/写入、未做掉电或存储耐久验证；当前存储兼容性、介质磨损与实际剩余容量均 **未验证**。本文不授予 L3/L4。
