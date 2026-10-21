# CR-003：局域网配对码生产注入与生命周期

- **提出方：** A0 架构与协议
- **关联：** `docs/implementation/change-requests/CR-001-pairing-code-validation.md`、`docs/implementation/interfaces.md` §6.4
- **状态：** A0 推荐裁决已形成；A1/A2 实施与产品 UI 入口确认待办（本 CR 关闭条件见 §4）
- **影响模块：** A1（配对窗口、随机码展示、生命周期）；A2（验证、限速、拒绝路径）

## 1. 现状与只读证据

代码搜索确认 `desk_sdk_bridge_set_pairing_code()` 目前只在 `firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h` 声明、在 `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c` 实现，没有生产调用点。固件 pairing handler 在 `s_pairing_code_configured == false` 时拒绝请求并 fail-closed，因此当前启动后没有期望码可用，官方 SDK 配对不能成功。

当前启动流程 `firmware/main/src/app_main.c` 初始化 NVS/TAL KV，再通过 `tuya_authorize_init()` / `tuya_authorize_read()` 读取 Tuya 授权 license，读取失败时回退到本地构建配置中的 UUID/AuthKey。该 license 面向 Tuya 云绑定，不是六位数字的 WatcheRobot `pairing_code`；不得把 UUID、AuthKey、`session_token` 或它们的派生值作为设备配对码。

`desk_watcher_storage_init()` 只核验/注册 Tuya 存储区域并保护未知分区；没有定义 DeskAgent 配对码 KV schema。现有 `tal_kv_init()` 不能证明本项目已有安全、可轮换的配对码持久化实现。`tuya_config.h` 的本地构建配置也是静态固件配置入口，不应承载共享或长期固定的六位码。

既有契约 `interfaces.md` §6.4 和 CR-001 要求六位数字精确匹配、未配置时不配对、不发 `pair.accept`，并禁止在日志中输出配对码或会话 token。锁定的官方 PairRequest 字段要求 `pairing_code` 为六位数字；错误码响应语义沿用静默拒绝，不新增 wire 消息。CR-001 的历史状态现由本 CR 更新。

## 2. A0 推荐裁决：设备生成、现场展示、短时 RAM 注入

采用**物理确认后打开限时配对窗口，由设备本机 CSPRNG 生成一次性六位码并显示；A1 将码注入 A2 的现有 setter；不从 Tuya provisioning 或持久化配置读取**。

### 配置来源与调用顺序

1. A1 提供清楚的本机“开始配对”入口，要求用户现场主动确认。推荐通过状态页中的明确 UI 操作进入；不要占用用户已经指定给 TuyaOpen 短按/长按的按键语义。窗口不得默认永久打开。
2. ESP32-S3 侧使用 ESP-IDF CSPRNG（`esp_fill_random()`）生成六位十进制字符串并保留前导零。为消除模偏差，按 24-bit 随机值拒绝采样：仅接受小于 16,000,000 的值，再对 1,000,000 取模并零填充；不使用 `rand()`、时间戳、MAC/UUID、固定默认值或构建期宏。CSPRNG 失败时保持窗口关闭。
3. A1 将此临时代码传给 `desk_sdk_bridge_set_pairing_code(code)`，仅在 setter 成功后显示。setter 失败则清零临时缓冲并不显示。显示内容只在本机配对页可见；不得广播、上传云端或写入 Host log。
4. SDK Runtime 用户从设备屏幕读取该码并在官方配对 UI 输入；随后 Runtime 的 `pair.request.pairing_code` 经现有配对路径做六位格式及常量时间精确比较。

### 有效期、清零与换码

- 配对窗口建议最长 **5 分钟**，并由单调时钟计时；不得因系统时间尚未同步而延长有效期。
- 配对成功（设备发出 `pair.accept` 后码即一次性消费）、窗口超时、用户取消、网络会话重置或进入新的配对窗口时，A1 调用 `desk_sdk_bridge_set_pairing_code(NULL)` 清零旧值；重新打开窗口时生成全新码。设备重启后不恢复旧码；掉电、看门狗复位和软件重启均按未配对状态启动。
- A2 应在活动配对窗口内限制失败尝试（建议最多 5 次，触发后立即清零并要求用户重新物理确认/生成新码），避免六位空间被在线穷举。尝试计数与过期状态必须由单调时间/窗口状态驱动。
- setter 对空值清零、拒绝格式错误、有效值只保存在 RAM 的当前语义可作为接口基础；setter 调用和配对状态切换应串行化或加锁，避免竞态读取半更新字符串。

### 机密与日志

- 六位码虽不是 Tuya 生产 Secret，仍属于短期配对凭据；不得写入 UART/CLI 日志、崩溃转储、持久化区、遥测、NVS/TAL KV、测试快照或 PR 文本。
- 诊断日志仅允许输出诸如 `pairing_window=active|expired`、拒绝原因、`pairing_code=[redacted]` 和非敏感 request ID；严禁输出实际配对码、`session_token`、UUID/AuthKey。
- 测试仅用生成的临时 fixture 值，不可复用现场码或真实设备凭据。

### 缺省与错误行为

- 没有用户物理确认、CSPRNG 失败、码生成/展示/注入失败、窗口已过期或重启恢复场景：均视为未配置，静默拒绝配对，不设置 `s_paired`、不生成/发放 token、不发送 `pair.accept`。
- 不得以固定开发码、host-test 常量、Tuya AuthKey、云端状态或“最近一次成功值”作为生产回退。
- 按 CR-001 既定临时契约，不新增错误响应类型；遇到格式错误/错误码仍静默拒绝并只记录脱敏诊断。

## 3. 职责边界

- **A1：** 物理配对入口、CSPRNG 调用、本机显示、5 分钟窗口计时、成功/取消/超时/重启时清码；不改 A2 生产代码。
- **A2：** 继续持有 `desk_sdk_bridge_set_pairing_code()` 和 pairing handler；校验活动码、失败尝试限速、并发安全、fail-closed 和日志脱敏；不读取 Tuya provisioning license。
- **A0：** 在 A1/A2 明确是否能跨所有固件重置与故障路径正确清码后更新公共 interfaces 裁决；不替代代码所有者实现。

## 4. 跨模块实施与验收门槛

1. 静态搜索证明只有 A1 配对窗口生命周期路径能设置/清空配对码；没有固定码、生产构建宏、Tuya license 复用或持久化读取路径。
2. 单元/固件契约测试覆盖：无配置拒绝；正确码可配对；错误/非六位码拒绝；最多 5 次失败后清码；成功、取消、超时、网络重置和重启都让旧码失效；新窗口生成新码；CSPRNG 失败不开放窗口；零前导码显示正确；并发更新不产生部分匹配。
3. 日志捕获断言：生成码与 `session_token` 不出现在日志，缺省/拒绝路径均不发 `pair.accept`。
4. 真机 Level 3：现场从 UI 打开配对页，屏幕码输入未修改的官方 Runtime 后真实配对成功；超时或取消后旧码失败、重新开窗后新码成功。只通过模拟/单测不能宣称官方 SDK 真实配对闭环。
5. 关闭本 CR 前，A1 确认不冲突的 UI 入口并交付生成、显示、清零测试；A2 交付配对尝试限速/一次性消费和日志脱敏测试；A0 更新 `interfaces.md` 与官方 SDK 设备接入契约。A1/A2 分别只修改自身 ownership 路径；若必须调整 setter 公共接口或协议字段，先发新 CR 由 A0 裁决。

**仍需产品确认的单点：** 配对 UI 的具体导航动作与用户文案。推荐使用屏幕状态页的明确“开始配对”操作；不得默认长按按键作为入口，因为长按已指定为 TuyaOpen 语音交互。窗口时长建议值为 5 分钟，A1/A2 实施时应按此参数测试；若产品要求不同，修改本 CR 后再实施。该 UI 细节不改变本 CR 的安全策略。

**安全边界：** 六位配对码提供的是短时、现场辅助的局域网配对确认，不等同高熵长期凭据。该方案依赖用户物理确认、短 TTL、失败限速和不泄露日志共同降低风险；若未来产品要求无人值守配对，应另行定义受认证 provisioning，而不能沿用静态六位码。
