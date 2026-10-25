# CR-036 — LAN reunite 请求方认证合同

- **提出方：** A0 架构与协议
- **状态：** **待 A0 裁决；本 CR 不授权生产代码实现、版本兼容承诺或硬件操作。**
- **范围：** 仅定义官方 Python SDK 与 DeskAgent 固件之间 `link.reunite.request` 的请求方认证合同及兼容验收条件。现有 PR #55 对当前无认证证明的 reunite 请求 fail-closed，应保持到本 CR 获批并由各 owner 完成实现。
- **关联：** [P0-3 官方 Python SDK 设备接入契约](../p0-3-official-sdk-device-contract.md)、PR #55、WatcheRobot Python SDK 当前协议实现。
- **证据级别：** 本 CR 是待裁决的 L1 协议提案；批准后仍需 L2 双端合同测试、L3 设备与官方 SDK 互通，以及适用时的 L4 重启恢复证据。

## 1. 问题与上游证据

经核对的未修改官方 SDK checkout 为 `https://github.com/orulink-ai/WatcheRobot_python_sdk.git`，HEAD `8bc13967a06ef073459be47e930ff43ef37ee8fb`（2026-10-10，merge #140）。该版本 `src/watcherobot/runtime/daemon/pairing/protocol.py`：

- 第 64–67 行允许 `link.reunite.request` 携带公共信封字段和 `nonce`、`target_mode`、`websocket_port`，但没有请求 MAC 或其它认证字段。
- 第 132–140 行的 `LinkReuniteRequest` 仅定义 `request_id`、`daemon_instance_id`、`nonce`、`target_mode`、`websocket_port`；第 477–485 行的序列化逻辑也只发送这些字段。
- 第 501–513 行定义由 `session_token` 派生 `binding_secret`；第 516–539 行的 `reunite_response_mac` 用于设备响应。`session.py` 第 307–316 行由 SDK 校验设备的响应 MAC。当前合同因此认证设备响应方，没有认证发出 reunite 请求的 daemon。

固件收到该 UDP 消息后会据此恢复配对并采用请求方地址作为 WebSocket 目标。若接受当前 v1.1 请求，局域网任意发送方可能触发 token 轮换或重定向设备连接。设备必须在任何配对状态、session token、目标 IP/端口或 WebSocket 连接变更前，认证请求方。

## 2. 待 A0 裁决的协议问题

批准实现前，A0 必须明确以下事项；未裁决前不得由 A2 自行选择 wire schema 或算法：

1. **版本与消息类型：** 是否为认证请求定义新协议版本（例如 `watcher-lan-pairing/1.2`），或使用独立消息类型/能力协商；明确旧 1.1 请求及所有未知版本必须拒绝，且不得静默降级到无认证 reunite。手动配对 1.0 是否保持不变也须写明。
2. **认证字段与 challenge 来源：** 认证字段名称、编码、长度、字符集及允许字段集。明确 `nonce` 是设备生成并预先发给已绑定 daemon 的一次性 challenge，还是客户端自行生成的随机数；若采用后者，说明设备如何验证请求新鲜度，以及为何窃取到的有效 MAC 请求不能在首次处理前被攻击者抢先转发。字段应表达 daemon 对本次 reunite challenge 的证明；不得传输原始 `session_token` 或 `binding_secret`。
3. **MAC 输入与对端绑定：** 明确 HMAC 算法、键编码、域分隔标签、字段顺序、字符编码、整数端口的规范表示及 MAC 输出格式。覆盖字段至少应绑定消息类型、协议/版本、`request_id`、`daemon_instance_id`、`nonce`、`target_mode` 和 `websocket_port`，避免跨会话、跨目标模式或端口替换。另须裁决认证如何绑定预期 daemon/网络对端：例如由设备保存的已配对 peer 身份或可验证的源地址约束；不得将无认证 UDP 源地址直接作为新 WebSocket 目标或 token 的接收方。说明首次收到被窃取有效请求的副本、且副本来自不同源地址时，设备为何不能向攻击者交付 token 或转移连接目标。规范化必须无歧义（例如定义严格长度前缀编码，或证明所选分隔格式无法产生歧义）；JSON 字段顺序和空白不得影响验签。
4. **Binding secret 派生与方向：** 明确沿用或升级 KDF。若沿用当前 SDK，记录它使用 `HMAC-SHA256(key=session_token, message=b"watcher-link-binding-v1")` 的派生结果；明确 daemon 与设备各自何时保存/恢复该 secret、它仅用于请求认证还是同时用于设备响应 MAC，以及两个方向是否使用独立的域分隔子密钥。派生材料只来自首次人工配对建立的 session token，不得由 reunite 响应中的新 token 反向更新长期 secret。
5. **设备响应合同：** 决定现有 `response_mac` 是否保持以及它对请求证明的关联方式；明确设备仅在请求 MAC 验证成功后才能返回新 session token，并且该 token 只发给已认证的当前请求。
6. **重放与重试：** 明确 nonce 的生成方、熵/唯一性要求、有效期、一次性 challenge 分发与消费方式、并发挑战处理、重启后的防重放状态边界，以及相同请求重传的幂等结果。必须定义 request_id/nonce 相同的重传是拒绝，还是仅向已验证的同一 peer 返回缓存的同一响应；不得向第二个源地址重发 session token。重复有效挑战不得产生多个不同 token、重复修改配对目标或建立多个连接。过期、重复冲突、格式错误和 MAC 不匹配请求必须拒绝且不得改变状态。
7. **官方 SDK 升级与兼容：** 明确最低兼容的官方 SDK/Runtime 版本、旧 SDK 的行为、固件/SDK 混合升级顺序、服务端与设备对新旧版本的处理矩阵，以及是否有安全的显式能力发现。兼容策略不得允许攻击者通过删字段或篡改版本触发 v1.1 无认证回退。若旧版 SDK 无法生成新认证证明，自动 reunite 应报告不支持并要求既有安全的人工配对升级路径。

## 3. 获批后的实现边界

- **A0：** 冻结版本、schema、MAC/KDF、nonce/replay 和升级兼容合同；如需改动官方 SDK，应确认 SDK owner 与发布版本策略。
- **A2：** 仅在获批合同下实现设备端严格字段解析、常数时间 MAC 校验、防重放及状态变更顺序；在验证前不得更新 pairing token、binding state、peer IP、WebSocket 端口或建立连接。相应 C contract/runtime tests 归 `tests/sdk_contract/`。
- **官方 SDK owner/A3：** 按获批合同实现请求生成、secret 生命周期、重试/超时和升级兼容测试；不得把 mock 响应或 SDK 单测描述为设备互通。
- **A7：** 组织跨端定向验证和证据记录，不跨界修改生产模块。
- **范围限制：** 本 CR 不授权修改 UDP 传输架构、普通人工配对、媒体/相机路径或其它设备协议。

## 4. 验收条件

### L2：双端合同与状态机

1. 锁定的官方 SDK 与生产 C bridge 对获批版本的同一组 golden request/MAC 输入输出完全一致；至少覆盖字段篡改、字段缺失/额外字段、JSON 重排/空白、非法编码、错误版本、错误 MAC、过期 nonce 和重放请求。
2. 未认证、未知版本、显式删除/篡改版本以及旧 v1.1 reunite 请求均 fail-closed；测试断言 pairing token、持久化 binding、target peer、WebSocket endpoint 和连接状态全部保持不变。
3. 合法认证请求仅在完整验证成功后更新状态；同一有效 request 重传按批准合同幂等处理，不产生二次 token 轮换或重复连接，也不向不同源地址返回 token。捕获的有效 MAC 请求首次从不同源地址送达时，设备必须拒绝或证明该地址不可能获得配对 token/成为新的连接目标。
4. KDF、请求 MAC、响应 MAC、重启恢复与 nonce 处理的 golden vectors 来自获批规范，测试不得从生产代码临时复制实现来构造期望值。
5. SDK/固件支持矩阵验证无安全降级路径；旧客户端升级流程与不兼容错误可观察且不破坏已有人工配对数据。

L2 仅证明软件协议实现与状态逻辑符合合同，不证明设备或真实 SDK 网络环境已完成连接。

### L3/L4：实机与恢复

- **L3：** 使用获批版本的未修改官方 SDK 真实连接目标设备，验证人工配对后断线恢复、认证请求/响应、实际 WebSocket 重连、命令/结果往返；同时用未授权局域网请求证明设备拒绝且不更改连接目标或 pairing state。记录设备固件 SHA、SDK 版本、网络条件及原始脱敏结果。
- **L4：** 在进程重启、设备重启、重复广播、丢包/延迟及并发请求条件下复验恢复与 replay 行为；仅记录本 CR 实际覆盖的组合。

不得用协议解析测试、localhost loopback、初始化成功或“SDK 已安装”替代 L3 真实设备往返。

## 5. 当前决定与阻塞

在 A0 批准版本、认证 schema、MAC/KDF 方向、重放边界与兼容矩阵前：

- DeskAgent 当前 v1.1 `link.reunite.request` 必须继续 fail-closed；不得为追求断线恢复而重新开放无认证请求。
- 不得猜测 `request_mac` 字段名、wire version、规范化算法或过渡策略。
- PR #55 的安全修复可以保留，但不能声称官方 SDK reunite 已恢复或已完成真实断线恢复验证。
