# CR-003：局域网配对码生产注入与生命周期

- **提出方：** A0 架构与协议
- **关联：** `docs/implementation/change-requests/CR-001-pairing-code-validation.md`、`docs/implementation/interfaces.md` §6.4
- **状态：** 契约已修订；A1/A2 代码实施部分存在，物理 UI 操作与官方 SDK 真机闭环仍 OPEN（本 CR 关闭条件见 §4）
- **影响模块：** A1（本机入口、配对窗口、随机码展示、生命周期）；A2（验证、限速、拒绝路径及 A1 UI 通知 seam 调用）

## 1. 现状与只读证据

当前代码已提供 A1 配对窗口实现：`firmware/components/desk_cli/src/desk_cli.c` 注册 `pairing <open|close>`，`open` 使用 `esp_random()` 32-bit rejection sampling 生成六位码、注入 `desk_sdk_bridge_set_pairing_code()`、显示并启动 5 分钟单调计时；`close`、超时和开新窗口会清码。该文件路径不在 AGENTS.md A1 ownership 表列出的路径中，因此本 CR 将其明确纳入本工作包的 A1 责任范围；公共 ownership 表若后续治理更新，应由 A0 单独提交。

`desk_sdk_bridge_set_pairing_code()` 已有生产调用点（上述 CLI 窗口路径）。Bridge 在接受合法 pair request 时会于锁内消费码；`desk_tclaw_bridge.{h,c}` 另有 A1-only `desk_tclaw_pairing_ui_on_pairing_consumed()` 与 `desk_tclaw_pairing_ui_on_sdk_session_reset()` 显示清理 seam，但当前没有 A2 调用方。设备固件启动后仍须由操作者显式打开窗口；没有活动码时 fail-closed。

当前启动流程 `firmware/main/src/app_main.c` 初始化 NVS/TAL KV，再通过 `tuya_authorize_init()` / `tuya_authorize_read()` 读取 Tuya 授权 license，读取失败时回退到本地构建配置中的 UUID/AuthKey。该 license 面向 Tuya 云绑定，不是六位数字的 WatcheRobot `pairing_code`；不得把 UUID、AuthKey、`session_token` 或它们的派生值作为设备配对码。

`desk_watcher_storage_init()` 只核验/注册 Tuya 存储区域并保护未知分区；没有定义 DeskAgent 配对码 KV schema。现有 `tal_kv_init()` 不能证明本项目已有安全、可轮换的配对码持久化实现。`tuya_config.h` 的本地构建配置也是静态固件配置入口，不应承载共享或长期固定的六位码。

既有契约 `interfaces.md` §6.4 和 CR-001 要求六位数字精确匹配、未配置时不配对、不发 `pair.accept`，并禁止在日志中输出配对码或会话 token。锁定的官方 PairRequest 字段要求 `pairing_code` 为六位数字；错误码响应语义沿用静默拒绝，不新增 wire 消息。CR-001 的历史状态现由本 CR 更新。

## 2. A0 推荐裁决：设备生成、现场展示、短时 RAM 注入

采用**本机操作者显式打开限时配对窗口，由设备生成一次性六位码并显示；A1 将码注入 A2 的现有 setter；不从 Tuya provisioning 或持久化配置读取**。

### 配置来源与调用顺序

1. 当前批准的唯一入口为本机诊断 CLI 命令 `pairing open` / `pairing close`，由 A1 负责 `firmware/components/desk_cli/` 中的命令注册、处理与窗口生命周期。不得把该 CLI 说成已完成产品级物理 UI；不得新增或猜测触摸/旋钮/按键入口。滚轮和触摸现有 UI 导航由 A1 保持原语义，旋钮短按/长按继续 TuyaOpen `AT_KEY_TALK` / `AT_LONG_KEY_TALK`。若产品后续要求屏幕直接启动配对，须先另行确认具体导航动作，再由 A1 在其所有权路径实施。
2. ESP32-S3 侧使用 ESP-IDF `esp_random()` 32-bit 随机值拒绝采样生成六位十进制字符串并保留前导零；只接受小于 `floor(UINT32_MAX / 1,000,000) * 1,000,000` 的值，再取模并零填充。不使用 `rand()`、时间戳、MAC/UUID、固定默认值或构建期宏。生成或后续 setter/UI/timer 任一步失败均关闭窗口并清码。
3. A1 将此临时代码传给 `desk_sdk_bridge_set_pairing_code(code)`，仅在 setter 成功后显示。setter 失败则清零临时缓冲并不显示。显示内容只在本机配对页可见；不得广播、上传云端或写入 Host log。
4. SDK Runtime 用户从设备屏幕读取该码并在官方配对 UI 输入；随后 Runtime 的 `pair.request.pairing_code` 经现有配对路径做六位格式及常量时间精确比较。

### 有效期、清零与换码

- 配对窗口建议最长 **5 分钟**，并由单调时钟计时；不得因系统时间尚未同步而延长有效期。
- **成功消费/协议侧封锁：** A2 的 pairing handler 在合法请求成功或失败次数达到上限、并因此于 pairing mutex 内清除 RAM 码后，`desk_sdk_bridge_process_pairing_udp()` 在释放该锁后调用 `desk_tclaw_pairing_ui_on_pairing_consumed()`。通知表示“一次性码已失效”，不表示主机已收到 UDP 响应或 WebSocket/HardwareHello 已成功；A1 seam 只清显示，不再次修改 A2 凭据。若 A2 的单调期限先于 A1 窗口计时触发，也按该时序清显示。通知不得在 pairing mutex 内调用，避免 UI 渲染与 A2 锁耦合。
- **会话 reset：** A2 在 `desk_sdk_bridge_reset()` 清理配对态和码、释放 pairing mutex 后调用 `desk_tclaw_pairing_ui_on_sdk_session_reset()`；若初始化时也需要清理旧 UI 状态，应由 A1 自己在显示初始化完成后清 overlay，A2 不得在 UI 初始化前触发可能渲染的回调。A2 不得从锁内调用该 seam。
- **超时/取消/换码：** 由 A1 CLI 窗口 owner 在 timer 到期、`pairing close`、开新窗口前或注入/显示失败时，停止/重置 timer 并调用 `desk_sdk_bridge_set_pairing_code(NULL)` 与本地 UI clear；A2 无需回调。新窗口必须生成新码。
- **重启：** pairing code 仅在 RAM；任何掉电、看门狗或软件重启后 A2 初始化保持 fail-closed，A1 在显示初始化后清理 pairing overlay。不得持久化或恢复旧码；不要求在重启路径跨模块调用 UI seam。
- A2 应在活动配对窗口内限制失败尝试（建议最多 5 次，触发后立即清零并要求用户重新物理确认/生成新码），避免六位空间被在线穷举。尝试计数与过期状态必须由单调时间/窗口状态驱动。
- setter 对空值清零、拒绝格式错误、有效值只保存在 RAM 的当前语义可作为接口基础；setter 调用和配对状态切换应串行化或加锁，避免竞态读取半更新字符串。

### 机密与日志

- 六位码虽不是 Tuya 生产 Secret，仍属于短期配对凭据；不得写入 UART/CLI 日志、崩溃转储、持久化区、遥测、NVS/TAL KV、测试快照或 PR 文本。
- 诊断日志仅允许输出诸如 `pairing_window=active|expired`、拒绝原因、`pairing_code=[redacted]` 和非敏感 request ID；严禁输出实际配对码、`session_token`、UUID/AuthKey。
- 测试仅用生成的临时 fixture 值，不可复用现场码或真实设备凭据。

### 缺省与错误行为

- 没有操作者显式打开窗口、随机码生成/展示/注入失败、窗口已过期或重启恢复场景：均视为未配置，静默拒绝配对，不设置 `s_paired`、不生成/发放 token、不发送 `pair.accept`。
- 不得以固定开发码、host-test 常量、Tuya AuthKey、云端状态或“最近一次成功值”作为生产回退。
- 按 CR-001 既定临时契约，不新增错误响应类型；遇到格式错误/错误码仍静默拒绝并只记录脱敏诊断。

## 3. 职责边界

- **A1：** 持有当前批准的本机 CLI `pairing open|close` 入口（包括 `firmware/components/desk_cli/`，虽该路径未列入 AGENTS.md 矩阵）；维护 CSPRNG、显示、5 分钟计时及取消/超时/换码清理；实现 A1-only UI notification seams；不改 A2 生产代码。产品物理 UI 导航仍未定，不得擅自绑定现有按钮/触摸事件。
- **A2：** 持有 setter 与 pairing handler；校验活动码、失败尝试限速、并发安全、fail-closed 和日志脱敏；在消费成功和显式会话 reset 后按 §2 锁外调用相应 A1 UI seam；不读取 Tuya provisioning license，不在桥接锁内执行 UI。
- **A0：** 在 A1/A2 明确是否能跨所有固件重置与故障路径正确清码后更新公共 interfaces 裁决；不替代代码所有者实现。

## 4. 跨模块实施与验收门槛

1. 静态搜索证明只有 A1 配对窗口生命周期路径能设置/清空配对码；没有固定码、生产构建宏、Tuya license 复用或持久化读取路径。
2. 单元/固件契约测试覆盖：无配置拒绝；正确码可配对；错误/非六位码拒绝；最多 5 次失败后清码；成功、取消、超时、网络重置和重启都让旧码失效；新窗口生成新码；CSPRNG 失败不开放窗口；零前导码显示正确；并发更新不产生部分匹配。
3. 日志捕获断言：生成码与 `session_token` 不出现在日志，缺省/拒绝路径均不发 `pair.accept`。
4. 真机 Level 3：现场通过本机 CLI 打开配对窗口，屏幕码输入未修改的官方 Runtime 后真实配对成功；超时或取消后旧码失败、重新开窗后新码成功。只通过模拟/单测不能宣称官方 SDK 真实配对闭环。
5. **当前设备验收阻塞：** 现有 `pairing open|close` 是本机 CLI 操作者动作，不证明终端用户能通过产品物理 UI 发起配对。产品所需的屏幕导航动作仍未确认，且缺少设备连接官方 Runtime 的真机配对证据；不得将本 CR 标为 closed。A1/A2 可先按已定 CLI 与生命周期契约完成软件实现和 Level 2 覆盖；要解除物理 UI 阻塞，须由产品明确入口动作后再派发 A1 原子实现包。
6. 关闭本 CR 前，A1 交付 CLI 入口安全性、生成/显示/取消/超时清理测试及物理 UI 入口决策；A2 交付消费/reset seam 调用时序、限速/一次性消费和日志脱敏测试；A0 更新 `interfaces.md` 与官方 SDK 设备接入契约。A1/A2 分别按 §3 ownership 实施；若必须调整 setter 公共接口或协议字段，先发新 CR 由 A0 裁决。

**仍需产品确认的单点：** 若要求实体设备用户不经本机 CLI 发起配对，需确认屏幕导航动作与用户文案。不得默认占用长按、短按或其它硬件输入：现有滚轮/触摸导航与旋钮 TuyaOpen 语音事件均有既定语义。5 分钟窗口已由代码采用，仍需通过软件计时测试及设备验收验证。

**安全边界：** 六位配对码提供的是短时、现场辅助的局域网配对确认，不等同高熵长期凭据。该方案依赖用户物理确认、短 TTL、失败限速和不泄露日志共同降低风险；若未来产品要求无人值守配对，应另行定义受认证 provisioning，而不能沿用静态六位码。
