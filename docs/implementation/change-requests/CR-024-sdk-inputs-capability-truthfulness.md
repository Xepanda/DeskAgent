# CR-024 — 暂停宣告 SDK `inputs` capability

- **状态：** A0 裁决已由 A2 实施；软件 L2 contract 验收通过。锁定 SDK runtime loopback 与目标设备 L3 尚未验证。
- **范围：** A2 在 `sys.client.hello`（桌面 JSON bridge 的 capability ACK）与 `sys.sdk.ready.get` 中移除 `inputs`。不改事件协议、不改设备输入采集、不改本地交互。
- **依据版本：** `contracts/dependency_lock.json` 锁定 WatcheRobot Python SDK v0.1.11，commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`；本地 pinned checkout `git rev-parse HEAD` 与之相同。

## 裁决

当前移除 `inputs` capability，直到固件完成可验收的 SDK 输入事件发布链路后再由 A0 单独恢复。设备不得向 SDK/Runtime 承诺目前无法消费的输入功能。A2 仅修改 capability 宣告，不删除/屏蔽硬件采集或 UI 行为。

## 证据与当前语义

1. 锁定 SDK `src/watcherobot/robot.py` 在 `_handle_message` 对 `type == "evt.sdk.input"` 才调用 `parse_input_event(data)` 并推送 `robot.inputs` 队列。没有该事件就没有 SDK 输入消费路径。
2. `src/watcherobot/inputs.py::parse_input_event` 接受三类精确数据：
   - `{source:"back_touch", action:"press"|"release"|"long_press", touch_id:0..255, timestamp_ms:非负整数}`；
   - `{source:"screen_touch", action:"tap", x:非负整数, y:非负整数, timestamp_ms:非负整数}`；
   - `{source:"roller", action:"rotate", delta:非零整数, timestamp_ms:非负整数}`。
3. 当前固件 `firmware/components/desk_hardware/src/desk_watcher_input.c` 将旋钮编码器步进映射为 `DESK_TOUCH_TARGET_ROLLER + SCROLL_UP/DOWN`，按键短按/长按映射为 roller + `SHORT_PRESS/LONG_PRESS`，触摸映射为 screentouch + 短按/长按/上下滚动。采样后仅调用 `desk_hardware_feed_touch_event()`，没有 SDK event 发布调用。
4. `firmware/components/desk_tclaw/src/desk_tclaw_bridge.c` 对旋钮滚动继续执行 UI 导航；旋钮短按/长按继续映射 TuyaOpen `AT_KEY_TALK` / `AT_LONG_KEY_TALK`。这些本地语义必须保留。
5. 当前事件采集没有保留屏幕触点 x/y；因此无法构造 SDK 所需 `screen_touch/tap`。固件亦未采集/提供独立背部触摸源；不能把屏幕触摸冒充 back_touch。SDK schema 没有 knob-button 短按/长按事件类型，不能擅自映射成 roller rotate。

## A2 实施结果（软件 L2 contract）

A2 已从 `sys.client.hello` 与 `sys.sdk.ready.get` 的 capability 列表移除 `inputs`，并删除不再使用的 `DESK_SDK_CAP_INPUTS` 常量；本地输入采集、UI 和 TuyaOpen 按键处理代码未改。两个响应的精确列表相同，顺序为：`motion`, `camera`, `expression.audio_follow.v1`, `expression`, `mcp_tools`。`tests/sdk_contract/test_sdk_bridge_protocol.py` 对两者做精确列表断言，`test_mcp_tool_bridge_contract.py` 对生产 C handler 的列表做精确断言，SDK compat 测试断言 `inputs` 缺席。

执行记录：merged WSL unittest suite 共 **119/119 passed**，包括 `test_mcp_tool_bridge_contract`、`test_sdk_bridge_protocol` 与 `test_official_sdk_runtime_compat`；其中官方 Runtime compatibility 的 34 项本次均实际运行并通过，使用仓库已有 `.target_logging/sdk-acceptance/sdk-venv` 与 pinned vendor SDK，经 `PYTHONPATH` 加载，未安装依赖。C harness 同时补入生产 `desk_notification.c` 链接源以完成构建。此结果是软件 L2 contract/测试证据；不据此声称目标设备互通或 L3/L4 通过。

## A2 实施要求

- 从 capability 列表移除 `inputs`，并确保 hello 与 `sys.sdk.ready.get` 完全一致；其他 capability 不在本 CR 范围。
- 不得新增 `evt.sdk.input` 发布，不得把输入转发至 Host，不得改输入/UI/Tuya 语义，不得以 hello 的硬件角色 ACK 为由扩张 capability。
- 不得删除 A1 输入驱动、本地 UI 导航、按键语音调用或诊断计数。

## 兼容性与恢复门槛

Capability 缺席意味着 SDK consumer 应将输入视为不可用；不得据此假定事件会到达。恢复前由 A0 再审并要求：

1. 明确并保留本地 UI/按键语义，决定哪些原始输入可同时发布；任何争用由 A1/A0 裁决，不得改变 TuyaOpen 短按/长按用途。
2. 对每个 SDK 事件源给出由真实采样数据到 schema 字段的无歧义映射；增加屏幕触点坐标采集后才可发 `screen_touch/tap`。未实现的 back touch 不得宣告支持。
3. 使用锁定 SDK parser 做正例/反例测试，覆盖 source/action、整数类型（bool 不可作为整数）、边界、毫秒时间戳、方向 delta；验证 SDK 未宣告时能力检查失败/域按设计不可依赖。
4. 加入生产 C 路径测试，证明输入样本产生严格合法 `evt.sdk.input` JSON，同时本地 `desk_hardware_feed_touch_event()` 仍收到并处理原事件；验证重复、非法、断连行为及事件不误路由为命令结果。
5. 通过 localhost pinned Runtime+C loopback 证明 SDK `robot.inputs.wait()` 收到对应事件。这是 Level 2 软件互通；若宣称设备互通，仍须另行完成目标板 Level 3，端到端场景另按 Level 4 验收。

## 验收（本 CR）

- 静态/合同测试断言 `sys.client.hello` 和 `sys.sdk.ready.get` 均不含 `inputs`，并确认列表除该项外保持既有值。
- 锁定 SDK 检查显示 capability 不存在时不会将 `robot.inputs` 描述为受支持输入能力；不得因空事件流误报成功。
- 回归测试确认本地滚轮 UI 导航与旋钮短按/长按既有映射未被修改（软件层）；此项不代表板上输入已实测。
- 本 CR 的 capability truthfulness 软件合同已通过；不要求当前实现 SDK 输入事件，因为 capability 已撤下。后续若恢复 `inputs`，须另行提交 A0 决策并满足上文恢复门槛。

## 验收限制

A2 capability 移除与一致性已有 Level 2 contract 测试证据，pinned SDK compatibility suite 的 34 项也已在本次 merged WSL suite 中实际运行通过。测试结果不构成目标设备上的 SDK 连接、输入硬件工作、L3/L4 或真实输入事件送达证明；目标硬件验收仍未验证。


