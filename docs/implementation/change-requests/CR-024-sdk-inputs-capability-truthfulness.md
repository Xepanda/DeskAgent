# CR-024 — 暂停宣告 SDK `inputs` capability

- **状态：** **L2 软件合同项已由 CR-025 验收并解除暂停；当前 capability 已按 `inputs={roller}` 恢复。设备 L3/L4 仍未验证。**
- **范围：** 本 CR 原批准 A2 从 `sys.client.hello`（桌面 JSON bridge capability ACK）和 `sys.sdk.ready.get` 移除 `inputs`，避免宣告尚无消费链路的输入能力。该暂停已按 CR-025 的恢复门槛解除。
- **依据版本：** `contracts/dependency_lock.json` 锁定 WatcheRobot Python SDK v0.1.11，commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`；pinned checkout 与此版本一致。
- **后续契约：** [CR-025](CR-025-sdk-roller-input-capability.md) 批准仅支持滚轮旋转的 `inputs={roller}` 子集及恢复条件。

## 原始暂停裁决与背景

CR-024 最初移除 `inputs`，因为固件只有本地输入采集/UI/TuyaOpen 处理，没有向 SDK 发布 `evt.sdk.input` 的生产路径。此项 truthfulness 修复完成后，A2/A1 按 CR-025 增加 roller observer、事件序列化和 Runtime 输入路径。以下恢复判定以 CR-025 为准；CR-024 原“保持缺席”的要求已由本次验收取代。

SDK v0.1.11 仅将 `type == "evt.sdk.input"` 的 `data` 交给 `parse_input_event()` 并推送至 `robot.inputs`。其 parser 接受 back touch、带坐标的 screen tap 和 roller rotate；当前设备只实现 roller rotate。设备 UI 导航与 TuyaOpen 旋钮短按/长按继续保持本地处理。screen touch、back touch 和按钮输入不属于本 capability。

## A2/A1 实施与 L2 验收结果

生产 `sys.client.hello` 与 `sys.sdk.ready.get` 两个 endpoint 当前都按相同顺序报告 `motion`, `camera`, `expression.audio_follow.v1`, `expression`, `mcp_tools`, `inputs`。此处的 `inputs` 仅表示 roller 旋转事件，不能解读为 SDK 支持其他输入源。

Root 在隔离 pinned venv 中独立复跑三个 SDK contract 模块，**127 tests passed**（Level 2）：

1. `tests/sdk_contract/test_sdk_bridge_protocol.py`：锁定 parser 正例/反例、生产 C roller observer、方向及本地 UI/按键回归、发送失败路径、capability 精确列表。
2. `tests/sdk_contract/test_official_sdk_runtime_compat.py`：pinned Runtime + 生产 C WebSocket client localhost loopback 接收正反方向 roller events；断开时发送失败，重连后旧事件不重放、新事件可达；真实 `InputDomain` 容量 64 队列两轮各接收 68 个事件，逐轮 `dropped_events` 增加 4，保留最新 64 项且顺序正确。
3. `tests/sdk_contract/test_mcp_tool_bridge_contract.py`：生产 C handler capability 精确列表，并确认 `inputs` 仅指 SDK roller events、button/touch 不包括在内，两个 endpoint 一致。

这些测试是静态、native harness 与 localhost Runtime 软件互通证据（L2）；observer 测试使用输入抽象，不证明真实 GPIO 或实体设备事件。不得将其描述为设备连接或现场输入验收。

## 当前契约与剩余验收

- **当前已恢复：** A2 可在两个 capability endpoint 一致宣告 `inputs`，但覆盖仅为 `inputs={roller}`。任何变化都须保持两个列表精确一致，并更新对应能力范围说明。
- **L2：** CR-025 四项恢复门槛已通过上述 127 项组合复跑，软件 capability truthfulness 项解除暂停。
- **L3 未验证：** 目标板实体旋钮多步输入及 SDK 收件；设备 UI 滚动；短按/长按仍触发 TuyaOpen 语义；目标设备连接官方 Runtime。
- **L4 未验证：** 设备/Host/Runtime 重启与恢复、断连期间不重放及恢复后新事件闭环、持续输入压力和长稳行为。

## 结论

CR-024 原暂停决定已按 CR-025 的 L2 门槛解除。当前固件可宣告 `inputs`，但只表示 roller 旋转；本结论不构成设备 L3 或系统 L4 验收。
