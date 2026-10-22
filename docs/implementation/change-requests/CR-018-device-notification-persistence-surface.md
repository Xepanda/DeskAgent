# CR-018：设备通知去重与确认回执的持久化存储面

- **提出方：** A0 架构与协议
- **状态：** **BLOCKED：等待产品/存储 owner 决定 app-owned 持久区的归属与容量**
- **关联：** CR-017、`contracts/dependency_lock.json` 的 `partition_table` 与 `partition_safety_invariants`、`firmware/partitions.csv`
- **范围：** 仅定义 CR-017 所需设备端通知去重/阶段/确认回执的存储契约。本文不授权格式化、分区表变更、擦写任何 Flash 地址或创建新运行时存储实例。

## 1. 裁决：当前没有获批且适用的 app-owned 持久化面

当前固件的 `storage` SPIFFS 分区为 4 MiB（`0xD20000..0x111FFFF`），依赖锁和镜像清单将其归属 UI/动画静态资源。源码中没有该分区的 SPIFFS mount/provider，也没有 DeskAgent 自有的通用 durable KV provider。准确地说，锁定 TuyaOpen SDK 确有 `tal_kv_init/set/get/del` API；但其 ESP32 backend 将 LittleFS 挂载在 Tuya `tuya` 分区内，并在 mount 失败后调用 `lfs_format()`。当前启动代码调用 `tal_kv_init()`，该能力已用于 TClaw / WebSocket token 等 Tuya/TClaw 数据，不能据此视为 DeskAgent app-owned、隔离或可安全复用的通知存储。

固件还有 `desk_notification_store_ops_t` 形式的 A1 backend 接口和 16 槽记录缓存；`desk_hardware_notification_init()` 在没有注册的 load/save/erase provider 时 fail closed。当前生产启动路径没有调用 `desk_hardware_notification_register_store()`，所以这个接口只是可供未来实现的 seam，不是现成 provider。通知记录结构包含标题，而 store callback 对整条记录进行读写；因此在标题保留决策未定、provider 未实现前，不能把这个 seam 当作允许落盘标题的授权。

`desk_watcher_storage_init()` 注册的是 Flash 尾部 Tuya store（`tuya`）及 marker（`deskmeta`），不是 DeskAgent 应用 KV API；这段区域不得挪用。SDK `tal_kv` 与这个 Tuya store 共用同一物理 `tuya` partition，并不是独立空间。

### 1.1 已有接口/分区适用性审计（源码 L1）

| 发现 | 源码/配置证据 | 对 CR-018 的结论 |
|---|---|---|
| Tuya `tal_kv` 是持久接口，但其 backend 是 LittleFS；`kv_storage_init()` 先 `lfs_mount()`，失败后会 `lfs_format()`。 | 锁定 TuyaOpen SDK `src/tal_kv/src/tal_kv.c` 的 `kv_storage_init()`、`tal_kv_init/set/get/del()`；`src/tal_kv/port/lfs_config.h`。 | 不可用于通知记录：其 mount/format 生命周期不能与通知 fail-closed 要求及已有 Tuya 数据隔离。API 级可写不等于产品授权或耐久契约。 |
| `tkl_flash_get_one_type_info(TUYA_FLASH_TYPE_UF)` 返回 `tuya` partition 相对地址 `0x9000`、大小 `0x18000`（96 KiB）；`KV_DATA` 使用相对地址 `0x1000`、大小 `0x8000`。`tal_kv` LittleFS backend 使用 `UF`。 | 锁定 TuyaOpen SDK `platform/ESP32/tuya_open_sdk/tuyaos_adapter/src/drivers/tkl_flash.c` 的分区常量及 `tkl_flash_get_one_type_info()`；`src/tal_kv/src/tal_kv.c`。 | 这些是已有 Tuya 数据面的内部布局，不是 notification namespace/quota；96 KiB UF 空间本身低于本 CR 的 128 KiB 设计下限，且与 Tuya store 共用格式化/文件系统生命周期；禁止将其重命名、扩容或当作空闲区。 |
| 本项目 `watcher_storage.c` 将 `tuya` 注册在 32 MiB Flash 尾部 `0x1FC0000..0x1FFFFFF`（256 KiB），并要求相关范围为空白或已有 marker；依赖锁的 factory partition table 不含这两个外部注册项。 | `firmware/main/src/watcher_storage.c`；`contracts/dependency_lock.json` 的 `partition_table`、`partition_safety_invariants`。 | Tuya KV 的相对 offset 落在共享 Tuya store 内；没有为通知另分区或预留配额。不能从 256 KiB 总尺寸推断有 128 KiB 可供通知使用。 |
| `storage` 是 `0xD20000` 起的 4 MiB SPIFFS 分区，镜像/依赖锁将其描述为 UI/动画静态资源；固件源码未找到对此分区的 `esp_vfs_spiffs_register` / mount 或通知 provider。 | `firmware/partitions.csv`、`firmware/sdkconfig.defaults`、`docs/implementation/r1-build-and-image-manifest.md`；项目固件源码。 | 不能安全选择其作为现成 provider；共享资产供应物、格式化和配额/命名空间所有权没有获批。 |

**决策：** 当前可见接口和分区都不能安全解除 CR-018 阻塞。保持 durable accepted/provider 部署阻塞；继续执行 provider 缺失时的 fail-closed wire contract。无需为此变更分区表、`sdkconfig` 或 `contracts/dependency_lock.json`。不在本 CR 中推断 `storage` 有空闲容量，也不为产品选定标题持久化、配额、保留期限或擦除语义。

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

### 2.1 Provider 不可用时的冻结 wire 错误

为使 A2 可以在存储决策仍阻塞期间实现 fail-closed handler，本 CR 冻结 provider missing/unmounted/unavailable 的拒绝响应；这不解除 CR 状态，也不批准任何 provider、分区、擦写或存储实现。对合法的 `notification.deliver`，设备必须不执行呈现/播放、不发应用 `accepted` 或成功 `sys.ack`，并在 SDK WebSocket command envelope 中返回：

```json
{"type":"sys.nack","code":503,"data":{"type":"notification.deliver","command_id":"<原样回显请求 data.command_id>","error":"STORAGE_UNAVAILABLE"}}
```

该 shape 沿用已有 `desk_sdk_log_nack()` 与同套件 authenticated WebSocket tests：`data.type` 是被拒绝命令名，`data.command_id` 原样回显，`data.error` 是稳定错误码字符串。pinned SDK `DaemonApplicationTransport.send_command()` 生成 32 字符小写 hex `command_id`，放在请求 `data`，并用响应 `data.command_id` 完成该次 ACK/NACK waiter correlation。稳定通知幂等键仍是 `data.notification_id`（等于 `occurrence_id`）；重试复用通知 ID，不复用 SDK command_id。`data.error` 精确为大写 `STORAGE_UNAVAILABLE`，登记于 `contracts/error_codes.json`；registry `description` 提供固定通用 message `Notification storage provider is unavailable`，不含标题、提醒正文、token 或凭据，registry `retryable: true` 代表 provider 恢复后可按相同 notification_id 重试。既有 wire shape 不传 message/retryable 字段。不能把传输层 ACK 当作 durable accepted。

该线格式是 Watcherobot SDK Application WebSocket command response，不是 `contracts/schemas/envelope_response.json` 的 DeskAgent `ResponseEnvelope`。后者保持 `schema_version/request_id/status/timestamp` 与顶层 `error` 结构；Host 如需映射，使用 `status="failed"`、相同大写 domain code/message、`error.retryable=true`，但不得将 SDK `command_id` 冒充 DeskAgent `request_id`。单独 schema：`contracts/schemas/sdk_sys_nack.json`。`code:503` 适配现有 integer NACK code 字段；当前没有针对 storage provider 的 deployed handler/端到端证据，因此 A2 若发现具体已部署 SDK/runtime 对 503 拒绝或改写，必须提交 A2/A0 change request，不得宣称该响应已可直接部署。

#### `notification.deliver` 的同步 A1 返回值到 SDK NACK 映射

所有同步失败使用同一 shape：`{"type":"sys.nack","code":<integer>,"data":{"type":"notification.deliver","command_id":"<echo>","error":"<UPPERCASE_CODE>"}}`。错误 message 和 retryability 属于 `contracts/error_codes.json` 的域语义，不增加 wire 字段。映射仅允许在识别失败后回 NACK；未知非零返回必须 fail closed，绝不能生成成功 `sys.ack`。

| A1 `desk_hardware_notification_deliver()` 返回/可确认条件 | SDK NACK `code` | `data.error` | Registry `retryable` | 固定通用 message / 处理 |
|---|---:|---|---|---|
| A1 专用 `DESK_NOTIFICATION_ERR_STORAGE_UNAVAILABLE` (`-100`)：provider missing/unmounted/unavailable 已可靠确认 | 503 | `STORAGE_UNAVAILABLE` | true | `Notification storage provider is unavailable`；按同一 `notification_id` 重试 |
| `DESK_HW_ERR_BUSY` 且来自 deliver 的记录槽容量耗尽 | 507 | `STORAGE_FULL` | false | `Notification storage is full`；空间未恢复前不自动重试 |
| `DESK_HW_ERR_IO`：保存、回读、校验或擦除失败，提交结果可能不确定 | 500 | `STORAGE_ERROR` | true | `Notification storage operation failed`；Host 保持 unknown 并仅以同一 ID 重试 |
| `DESK_HW_ERR_TIMEOUT` 表示 `expires_at` 已到期 | 410 | `EXPIRED` | false | `Notification has expired`；终止投递，不重试 |
| `DESK_HW_ERR_INVALID_ARG` 或 command/schema 校验失败 | 400 | `INVALID_ARGUMENT` | false | `Invalid notification request`；修正输入后由新请求处理 |
| Host 认证 gate 未通过（早于 A1 调用） | 401 | `AUTH_REQUIRED` | false | `Notification delivery requires an authenticated device session`；先完成认证，不重试同一会话 |
| 普通 `DESK_HW_ERR_UNAVAILABLE` (`-2`)：未初始化或系统时间无效，无法可靠归因为 provider 故障 | 503 | `NOTIFICATION_DELIVERY_FAILED` | true | `Notification delivery failed`；保守 fail closed，Host 保留 unknown/同 ID 重试，不伪称 provider 原因 |
| 任何未登记非零返回（含 `DESK_HW_ERR_STALL`） | 500 | `NOTIFICATION_DELIVERY_FAILED` | true | `Notification delivery failed`；保守 fail closed，Host 保留 unknown/同 ID 重试 |

A1 当前用专用 `DESK_NOTIFICATION_ERR_STORAGE_UNAVAILABLE (-100)` 表示 provider 未注册/不可用；普通 `DESK_HW_ERR_UNAVAILABLE (-2)` 仍表示 `!s_initialized`（但 provider 已注册）或 `now_epoch_us()==0`，所以 A2 将 `-100` 映射到 `STORAGE_UNAVAILABLE`、将 `-2` 映射到通用 `NOTIFICATION_DELIVERY_FAILED`。未认证 `401/AUTH_REQUIRED` 是 A2 认证 gate 错误，不是 A1 return code，但属于相同 SDK WebSocket NACK shape，纳入本 schema。当前同步映射由 A2 `desk_sdk_notification_result_nack()` 实现；所有非零返回都必须及时 NACK，不能以 `return -1` 让 SDK waiter 超时。

`desk_hardware_notification_process_pending()` 是 ACK 后独立的异步处理，不存在可安全回显的原命令 waiter。其 `DESK_HW_ERR_BUSY` 是显示/音频仲裁占用，应保留 pending 并稍后重试；超时/显示/音频结果按持久状态事件上报；IO 或未知错误 fail closed，绝不能为早已结束的 `notification.deliver` 伪造迟到 NACK。尤其是 `deliver` 返回 OK 后，表示已 durable accepted，可回 transport `sys.ack`；它不表示渲染/播放成功。

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
