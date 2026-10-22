# CR-025 — 仅发布 SDK 滚轮旋转输入

- **状态：** 总控/A0 已批准部分覆盖语义，批准进入软件实现。`inputs` capability 必须等事件链路 L2 验收通过后才可恢复；当前尚未恢复，也不代表设备能力已验证。
- **范围：** 评估在保持本地 UI 导航和 TuyaOpen 按键语义不变的前提下，是否可将真实滚轮旋转事件发布给锁定的 WatcheRobot Python SDK。
- **来源 pin：** `contracts/dependency_lock.json` 中 WatcheRobot Python SDK `v0.1.11`，commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`。本地 pinned checkout `.target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk` 的 `git rev-parse HEAD` 与 pin 一致。
- **审议依据：** [CR-024](CR-024-sdk-inputs-capability-truthfulness.md) 当前决定从 capability 列表移除 `inputs`，直到 A0 再审并满足恢复门槛。

## A0 裁决

**批准 `inputs` 表示至少一种可用的 SDK 输入，并接受当前仅覆盖 `roller` 的部分能力及有界 best-effort 丢步语义。** 按钮短按/长按继续只走 TuyaOpen 语音语义，不纳入 SDK inputs；screen touch 与 back touch 均不支持、不纳入 capability。锁定 SDK 的 `parse_input_event()` 已接受 roller 这一标准输入种类，无需扩充 parser/API 即可解析该输入子集。

批准仅针对部分覆盖的产品语义和进入软件实现。实现完成前，capability 仍保持缺席；A2 仅可在事件链路 L2 验收通过、并按 `inputs={roller}` 明确记录覆盖范围后恢复 `sys.client.hello` 与 `sys.sdk.ready.get` 中的 capability。SDK `v0.1.11` 未提供机器可读的来源子类型列表；若未来要求 Runtime 自动发现具体来源，需另行提交 SDK/协议变更请求。

## 源码与接口证据

1. 锁定源码 `src/watcherobot/robot.py::_on_message()` 只将 `type == "evt.sdk.input"` 的 `data` 交给 `parse_input_event()`，解析成功后放入 `robot.inputs`。`robot.py::_on_message()` 不会把其他事件类型转换成输入事件。
2. `src/watcherobot/inputs.py::parse_input_event()` 的 roller 分支要求：
   ```json
   {"source":"roller","action":"rotate","delta":1,"timestamp_ms":123}
   ```
   `delta` 必须为非零整数、`timestamp_ms` 必须为非负整数；Python `bool` 虽是 `int` 子类，但 parser 明确拒绝 bool。负 delta 合法。
3. SDK `InputDomain` 使用容量 64 的有界队列。满载时丢弃最旧事件并递增 `robot.inputs.dropped_events`，不会向设备发送流控或溢出事件。连接断开时 `_close("disconnected")` 清空队列并关闭当前输入流；等待方得到 `WatcheRobotError`。因此输入事件语义是有界、尽力送达，不保证每个机械 detent exactly-once。
4. `firmware/components/desk_hardware/src/desk_watcher_input.c::watcher_knob_decode_step()` 已把正向 detent 映射到 `DESK_TOUCH_ACTION_SCROLL_UP`，负向 detent 映射到 `DESK_TOUCH_ACTION_SCROLL_DOWN`；`watcher_input_record_event()` 继续调用 `desk_hardware_feed_touch_event()`，其调用方 UI 导航应保持不变。事件来自硬件轮询/编码器状态变化，不含 SDK timestamp。
5. 当前 `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c::desk_sdk_bridge_send_touch_event()` 发出旧的 `resource.touch_event` 信封，字段为 target/action/墙上时间字符串；它不是 `evt.sdk.input`，不能直接供 `robot.inputs` 消费。不得将 SDK 事件塞进旧事件类型或将旧事件误称为 SDK 输入。
6. `desk_watcher_input.c` 中按钮短按/长按由 PCA9535 采样后仍送本地 `desk_hardware_feed_touch_event()`；`desk_tclaw_bridge.c` 继续把它们映射为 TuyaOpen `AT_KEY_TALK` / `AT_LONG_KEY_TALK`。本提案不向 SDK 映射按钮事件。
7. 当前屏幕触摸逻辑仅保留触摸起止 Y 并据此生成滑动/按压动作，不保留 tap 坐标；也没有独立 back-touch 采集源。不得从现有数据构造 `screen_touch/tap` 或 `back_touch`。

## 待批准的事件合同

当且仅当物理旋钮产生一个已解码 detent 时，固件应在不阻塞本地 UI/Tuya 处理的情况下尽力发布一个 SDK 输入事件：

| 本地采集动作 | SDK event `data` |
| --- | --- |
| `DESK_TOUCH_TARGET_ROLLER` + `DESK_TOUCH_ACTION_SCROLL_UP` | `{"source":"roller","action":"rotate","delta":1,"timestamp_ms":T}` |
| `DESK_TOUCH_TARGET_ROLLER` + `DESK_TOUCH_ACTION_SCROLL_DOWN` | `{"source":"roller","action":"rotate","delta":-1,"timestamp_ms":T}` |

- 外层消息类型必须为 `evt.sdk.input`，`data` 必须直接符合 SDK parser schema。
- `T` 是固件收到/确认该 detent 事件时的 monotonic 毫秒数（ESP-IDF `esp_timer_get_time()/1000` 或等价单调时钟），转换时不得回退或用 wall clock。它表示**Host 可见 parser 的事件接收时间字段**，不是编码器采样/传感器采样时间，也不承诺跨重启可比较。
- 每个被解码的 detent 单独映射为 `delta=+1` 或 `delta=-1`；不得将按钮或触屏动作伪装为 delta，也不得在丢步时合并/猜测 detent。
- 事件发布失败、连接未建立或发送队列满时，只丢 SDK 副本并记录可观察的错误/计数；不能阻塞、吞掉或改变本地 `desk_hardware_feed_touch_event()` 路径。是否需要设备端重传/去重是另一个协议决策；本提案不承诺可靠传输。
- SDK/parser 拒绝畸形或未知事件时，Runtime 不会入队。C 端必须保证 schema 与整数边界正确；禁止把发送尝试记为 Host 已消费。
- SDK 输入队列溢出时，Runtime 丢弃最旧事件并通过其本地 `dropped_events` 计数体现。产品接受此 best-effort 滚轮流语义是 capability 恢复的条件之一；若必须保证每一步滚动均可见，需另行设计背压/确认协议及 SDK 支持。
- 断连关闭当前 SDK 输入流并清空 Runtime 已排队事件；重连后事件只从新连接开始，不补发断连期间的旧输入。不得通过重放缓存让 UI 产生过时滚动。

## 所需接口与归属

1. **A1 硬件事件源：** 仅在 `firmware/components/desk_hardware/src/desk_watcher_input.c` 的成功 detent 分支提供一个不改变既有 `desk_hardware_feed_touch_event()` 语义的事件通知；若需跨组件 API，在 `firmware/components/desk_hardware/include/desk_hardware.h` 提交接口建议。禁止在 A1 中自行拼 SDK JSON 或修改 `desk_sdk_bridge`。
2. **A2 SDK bridge：** 在 `firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h` 与 `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c` 定义类型安全的 roller input 事件接口，发送 `evt.sdk.input`，明确 WebSocket Runtime 连接条件、序列化错误及发送失败返回值。现有 `desk_sdk_bridge_send_touch_event()` 保持旧协议用途，不复用、不改变其 `resource.touch_event` schema。
3. **A0/A2 capability：** 经本 CR 最终批准并完成事件路径后，A2 才可在 `sys.client.hello` 与 `sys.sdk.ready.get` 同时恢复 `inputs`；两处列表必须一致。若 SDK/Host capability schema 未能声明只支持 roller，则 companion 文档及项目能力清单必须同时注明 `inputs={roller}` 的实际覆盖范围，且先由总控确认此表达足以避免全量能力误解。

## 验收门槛

### L2 软件合同（恢复 capability 的必要条件）

1. 用锁定 SDK parser 做正例/反例：`delta=+1/-1` 与非负 monotonic 毫秒时间戳均生成 `RollerEvent`；零 delta、bool、字符串、缺字段、负时间戳和非 `roller/rotate` schema 均拒绝。
2. 生产 C 路径测试证明每个真实输入抽象 detent 只发出一个合法 `evt.sdk.input`；UP/DOWN 方向严格对应 `+1/-1`；本地 UI 仍收到原始事件；按键短按/长按仍只触发 TuyaOpen 语义，且没有 SDK 输入事件。
3. 用 pinned SDK Runtime + 生产 C WebSocket client localhost loopback，实际等待并收到 `RollerEvent`；覆盖断连、重连、发送失败和可重复验证的队列溢出行为。测试只能证明软件 L2 互通，不能升级为设备互通。
4. 两个 capability 端点精确一致；capability 恢复前，事件 parser/loopback 测试和上述局部覆盖描述必须先通过。

### L3/L4 设备门槛

- **L3：** 目标板实测旋钮正/反向多步，Host SDK `robot.inputs.wait()` 收到方向与次数对应的 roller events；同时设备 UI 滚动可用、按钮短按/长按仍走 TuyaOpen 语音。记录固件/SDK SHA、事件日志和用户观察。必须有设备接入与当次现场验收，当前未验证。
- **L4：** 在设备/Host 重连与 Runtime 重启条件下验证断连期间事件不重放、恢复后新 detent 可达且 UI/Tuya 行为保持；持续滚动压力下队列溢出可观测且不阻塞本地交互。当前未验证。

## 已裁决事项与下一步

- **产品语义已批准：** `inputs` 表示至少一种可用 SDK 输入；当前只支持 roller，接受有界 best-effort 丢步。按钮、screen touch、back touch 均不纳入此能力。
- **软件实现进行中条件：** A1/A2 可按本合同实现 roller 事件链路；在事件链路达到 L2 前，维持 CR-024 的 capability 缺席状态。L2 通过后，A2 才可同时在两个 capability 端点恢复 `inputs`，并标明覆盖仅为 roller。
- **上游 SDK API：** 仅实现 parser 已定义的 roller 事件不需要新增上游 API。若要求 SDK capability 查询明确返回受支持的输入来源集合，锁定 `v0.1.11` 未提供该表达；先向 SDK/A0 提交接口变更请求，不得伪造完整能力声明。
- **后续归属：** A2 实现 bridge/schema/localhost loopback；A1 在硬件输入采集处接入类型化事件通知并保持本地 UI/Tuya 处理；A7 补入设备验收证据。L3/L4 仍需后续目标硬件与 Runtime 实测，不由本次产品语义批准替代。

## 结论

总控/A0 已批准 `inputs` 表示至少一种可用 SDK 输入，当前仅覆盖 roller，并接受有界 best-effort 丢步。已批准进入软件实现；capability 必须等待事件链路 L2 验收通过后再恢复。按钮及 screen/back touch 均不纳入。此裁决不表示实现已完成、设备已支持或任何 L3/L4 通过。
