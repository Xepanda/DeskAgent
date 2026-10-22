# CR-005：Managed SDK 模式下 Codex 任务终态反馈到设备

- **提出方：** A0 架构与协议
- **范围：** A3 Host SDK 装配、A5 Codex 终态摘要、A1 固件现有表达/提示音能力
- **状态：** **Lead/A0 已接受；实现已授权**
- **关联：** `contracts/schemas/domain_codex.json`、`contracts/schemas/domain_resource.json`、`docs/implementation/interfaces.md` §4.2、CR-004
- **限制：** 不增加 wire schema，不修改官方 SDK，不改变固件命令；不把 ACK 宣称为声音已被听到

## 1. 结论

设备侧已有适用的 wire command：`ctrl.expression.set`（也接受 `ctrl.expression.play`）。它可设置表情和屏幕状态文字，并通过现有 `play_audio` / `preset=alert` 触发提示音。Host TCP `RobotAdapter.send_announcement()` 目前使用 `ctrl.expression.play`，但 managed official SDK 会话停用该 TCP adapter，因此 `_on_codex_event` 和 outbox 目前没有可用的 managed-session 投递通道。

Pinned SDK 的 `ApplicationContext` 只公开 `robot`、`desktop`、`rtc` 等对象，没有公开 `transport` 或通用 `send_command()`；`context.robot` 也没有公开的通用命令发送方法。`DaemonApplicationTransport.send_command()` 本身是公开方法，但只能经 `context._transport` 取得。Lead/A0 已裁决：**当前使用 `context._transport.send_command()` 作为受控入口**，因为该 pinned transport 的发送方法是 public，且 `main.py` 已通过同一个私有字段给 camera adapter 注入命令发送能力。A3 在 composition root 复用这一入口，将受控 sender 注入终态反馈 adapter；保持唯一 managed Runtime，不开启第二连接。该访问绑定 `contracts/dependency_lock.json` 中 WatcheRobot Python SDK commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`。每次升级 SDK 必须复核 `ApplicationContext` transport 暴露方式及 `send_command()` ACK 行为，并优先切换到 SDK 新增的公开入口；在此之前不得默默沿用未核对的私有字段。

这是**传输接入缺口**，不是新 wire 契约：复用 `ctrl.expression.set`，不要发明 `codex.result` 等新 command，也不要把旧 TCP adapter 接回 managed 模式。

## 2. 现有字段和映射

终态事件沿用 `codex.turn_completed`，其现有字段为 `task_id`、`thread_id`、`turn_id`、`resolution`（`completed | interrupted | failed`）和可选 `summary`。A5 已负责产生并净化终态摘要。设备命令复用现有 envelope：`type: "ctrl.expression.set"`，`data` 内使用固件已解析的 `preset`、`text`、`event_type`、`play_audio`；`request_id` 沿用 SDK command transport 关联 ACK。

建议状态映射如下（只映射已有字段/命令）：

| Codex resolution | `event_type` | `preset` | `text` / 屏幕文案 | 提示音 |
| --- | --- | --- | --- | --- |
| `completed` | `codex.completed` | `happy` | 完成状态 + 摘要 | `play_audio=true` |
| `failed` | `codex.failed` | `alert` | 失败状态 + 摘要 | `play_audio=true` |
| `interrupted` | `codex.interrupted` | `curious` | 中断状态 + 摘要 | `play_audio=true` |

`ctrl.expression.set` 固件处理会在显示仲裁失败/显示驱动失败时返回 `sys.nack`；`sys.ack` 的 `data.status=accepted` 仅表示显示更新已受理。音频仲裁繁忙时当前实现仍返回成功 ACK，但会跳过提示音。因此 Host ACK 只能代表命令被固件接受，不能证明提示音已执行或用户听见。

## 3. 关联、去重、隐私和长度

- `task_id` / `turn_id` 是事件和 outbox 中已有的关联字段。A5 应确保每个 turn 只发布一次终态；A3 不得根据摘要文本推断终态。ACK 使用 SDK command request ID 关联该次发送。
- 现有固件没有按 `task_id` / `turn_id` 去重的持久化接收记录；request ID 只用于 ACK 关联，不能视为幂等键。A3 应在 Host outbox 中以已有 Codex 终态事件身份去重，保留已有 dispatched 状态；发送结果不确定或断线重连时不得宣称设备已显示。若采用重试，可能重复显示/提示音，需把此限制记录在投递状态中。不得新增固件去重字段或假设 ACK 丢失后安全重放。
- 仅转发 A5 的 `summary`，不得转发原始 prompt、完整 Codex 输出、diff、路径、审批内容或凭据。A5 既有 `_safe_terminal_summary()` 会移除原始 prompt 的原样回显，但不等价于通用秘密检测；A3 不应声称摘要已做秘密扫描。
- 固件将文本读入 `char text[64]`，最大可用内容为 **63 UTF-8 字节加 NUL**。在 A5/A3 边界按 UTF-8 字节安全截断，确保不截断多字节字符；控制字符规范化为空格。状态前缀与摘要合并后总长不得超过 63 字节。JSON Schema 的 `status_text.maxLength=64` 是字符数，不足以描述该 C 字节缓冲限制；固件实际处理可使用 `text`，也支持 `status_text`/`title` 回退。

## 4. 断线与投递状态

- 设备 session 未连接、SDK command 超时、`sys.nack` 或传输异常：保持 outbox pending，按既有退避重试；不能调用已停用的旧 TCP `RobotAdapter` 作为 fallback。
- Host 收到匹配 request ID 的 `sys.ack` 且 `accepted=true` 后，可将该 outbox 项标记 dispatched；该状态名称只描述设备接受命令，不描述音频完成或用户确认。
- SDK session 断开会使在途 command 失败；不保留旧 session 对象或旧 ACK waiter。重连后用当前 managed session 投递尚 pending 的终态。发送结果不确定时可能有重复提示音，遵循上节限制。
- 用户侧 UI 如需声称“已播报/已听到”，还需另行定义设备音频完成/用户确认事件；本 CR 不定义该协议。

## 5. Owner 与实现边界

- **A5 — `companion/`：** 生成符合 `codex.turn_completed` 的 resolution、task/thread/turn 关联和 assistant summary；过滤原 prompt 的原样回显；不直接发送设备命令，不改 wire schema。
- **A3 — `host/desk_agent/connectors/robot/` 与最小 composition-root 接线：** 新增/维护 managed SDK 终态反馈 adapter，接收现有 Host Codex outbox 投递回调，经当前官方 SDK Application 会话发送已有 `ctrl.expression.set` 命令。仅允许在 `host/desk_agent/main.py` 增加初始化和依赖装配，把 `context._transport.send_command` 注入 adapter 并接入 Codex outbox；不修改 A5 Companion、A1 固件或 A6 viewer。该私有字段访问只对锁定 SDK commit `8bc13967a06ef073459be47e930ff43ef37ee8fb` 授权；不得自建第二连接。升级 SDK 时先复核/测试并改用可用的公开入口。
- **A1 — 固件：** 当前 `desk_sdk_bridge.c` 已实现 `ctrl.expression.set` 的显示更新、资源仲裁、`play_audio` 提示音及 ACK/NACK。此包不要求固件改动；任何显示/提示音真实设备验收仍由固件 owner/集成验收另行执行。
- **A0 / Lead：** 已接受上述版本绑定方案并授权实现；outbox `dispatched` 仍只表示设备接受命令，不代表提示音完成或用户听到。

## 6. 证据与验收门槛

源码核对（Level 1）：

1. `host/desk_agent/main.py::_on_codex_event` 将 Codex terminal event 持久化到 outbox，但立即投递和 `_delivery_loop` 都只检查 `self.robot_adapter`；managed setup 构造 `DeskAgentHostApp(..., managed_sdk_session=True)` 后只注册 camera adapter。
2. 锁定的 SDK 源码 `.target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk/src/watcherobot/application/context.py` 中 `ApplicationContext` 公共属性包括 `robot`、`desktop`、`rtc`，transport 存在私有 `_transport`；`watcherobot/application/transport.py::DaemonApplicationTransport.send_command()` 是通用命令入口。
3. SDK `watcherobot/robot.py` 暴露设备特定接口，但没有公开的通用 `send_command`/expression setter；其 `_command()` 是私有实现。
4. `host/desk_agent/connectors/robot/robot_adapter.py::send_announcement()` 使用 `ctrl.expression.play`；固件 `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c` 的 expression handler 读 `preset`、`text`、`event_type`、`play_audio`，执行显示仲裁，按条件执行提示音并返回 ACK/NACK。
5. `contracts/schemas/domain_codex.json::codex_turn_completed_payload` 已含终态类型和关联 ID；`contracts/schemas/domain_resource.json::expression_set_args` 已定义 expression preset、status_text；`interfaces.md` §4.2 只概述 `resource.expression.set`，没有规定 Codex feedback delivery、长度字节数、speaker completion 或 managed SDK sender。
6. `tests/sdk_contract/test_sdk_bridge_protocol.py::test_ctrl_expression_play_and_set_success` 验证当前 firmware command 的显示、ACK 与 `reminder.alert` 提示音路径；并未验证 Codex terminal event 经 managed SDK 到设备的端到端路径。

实现验收需至少覆盖：三种 resolution 的命令字段映射；经锁定 SDK public `DaemonApplicationTransport.send_command()` 调用的 command ACK request ID 关联、`sys.ack`、`sys.nack` 和超时；摘要 UTF-8 63-byte 边界和 prompt 不回显；outbox 重启/断线重试及重复风险；managed SDK session 下 Codex terminal event 实际抵达设备显示并听到提示音。SDK 升级门槛还须确认 `context._transport` 生命周期仍由 `ApplicationContext` 管理、`send_command()` 仍是 public API、响应仍匹配 request ID，并验证无第二连接。日志须分别记录命令接受与真实音频/用户听感证据，后者不能由 ACK 替代。
