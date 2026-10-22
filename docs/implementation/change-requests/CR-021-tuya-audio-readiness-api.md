# CR-021: Tuya audio and wake readiness public API

- **提出方：** A0
- **提出日期：** 2026-10-22
- **状态：** PROPOSED — TuyaOpen SDK/API owner 裁决前，A1 的 CR-015 readiness 接入阻塞
- **关联：** [CR-015 audio input readiness gate](CR-015-audio-input-readiness-gate.md)
- **依赖基线：** `contracts/dependency_lock.json` 锁定的 TuyaOpen SDK commit `cdc064e78daeeae598157470244e351cd37cbbed`；CR-015 已记录其 clean pinned checkout 的 L1 审计结果。该 CR 未访问或修改 SDK/vendor 源码。
- **Owner：** TuyaOpen SDK owner 提供并维护公开接口；A0 维护契约与依赖 pin；A1 在 pin 更新并完成兼容审查后接入；A7 执行 L2/L3/L4 验收。

## 1. 决策背景与当前能力缺口

CR-015 已审计固定 pin 的公开 KWS 接口及调用路径：`tools/porting/adapter/kws/tkl_kws.h` 暴露 `tkl_kws_init()`、`tkl_kws_reg_wakeup_cb()`、`tkl_kws_enable()`、`tkl_kws_disable()`、`tkl_kws_deinit()` 操作，但没有读取实际模型名/ID、模型加载状态、启用状态或运行期错误的公开 getter/event。`ai_mode_wakeup.c` 对 `ai_audio_input_wakeup_mode_set()`、`tkl_kws_reg_wakeup_cb()` 和 `tkl_kws_enable()` 的结果未完整传播；`ai_chat_main.c` 仅检查 `tkl_kws_init()` 的返回值。因此现有接口不能由 A1 可靠报告并区分设备 open、AFE/VAD、WakeNet/KWS 模型与运行状态。以上为 CR-015 对依赖锁定 commit `cdc064e78daeeae598157470244e351cd37cbbed` 的既有 L1 审计结论，本 CR 未重新读取 vendor 源码。

DeskAgent 当前公开 HAL 的 `desk_hardware_audio_get_status()` / `desk_watcher_audio_status()` 只提供板级 `ready/running/frames/errors/peak`。这能作为本地硬件/HAL 观测，不能替代 Tuya AI 音频输入、AFE/VAD 或 KWS/WakeNet readiness。MQTT connected、agent/session 创建、回调注册成功或 PCM 计数也不能作为替代信号。

结论：固定 pin 的公开接口**不足以**满足 CR-015 的完整 readiness 契约。需要上游公开 API 与依赖 pin 更新；A1 不能靠私有符号、内部结构、推断模型文件或本地 vendor patch 补齐。

## 2. 请求的最小公开接口

请 TuyaOpen SDK owner 增加稳定、可在公开头文件中使用的 typed snapshot 与状态变更通知。以下名称仅描述契约，不限定 API 命名：

1. `audio_input` 状态：设备查找、设备 open、输入通路启动及关闭；返回状态、阶段和原始错误码。
2. `afe` / `vad` 状态：是否配置/启用、初始化/启动结果及失败阶段/错误码；未启用组件应明确报告 `not_configured`，不得误报 ready。
3. `kws` 状态：当前模型的实际公开名称或 ID、模型是否 loaded、KWS 是否 enabled/running，以及初始化或运行期失败阶段/错误码。必须能区分“模型已选择”与“模型已实际加载并启用”。
4. 单调递增的初始化 generation（或等价实例标识），用于识别重试、关闭及旧回调；snapshot API 与异步 callback 必须提供同一 generation。
5. 可查询当前 snapshot，并可订阅状态变化/运行期错误；错误不得只写日志后丢失。若使用单一 typed API，仍须能独立观察上述阶段。

上游接口必须定义生命周期与并发规则、callback 注册/注销、初始 snapshot 获取和状态通知之间的竞态处理。不得要求应用读取私有 backend 状态或解析自由格式日志。

## 3. 失败、恢复和兼容语义

- 同步初始化任一必需阶段失败时，公开调用返回失败及可归属的 stage/code；不得继续报告 mode/KWS ready。调用者可保留 provisioning/MQTT，但必须 fail closed 拒绝唤醒/对话入口。
- KWS/AFE/VAD 启动或运行期故障必须产生可查询且可订阅的 `failed` 状态；状态代次与错误需稳定关联。仅有 agent/session 或云端状态成功不能覆盖失败。
- stop/deinit、部分初始化失败及重新初始化必须定义资源回收；重试创建新 generation。旧 generation 的 callback 不得让新实例变为 ready。
- 恢复成功仅在该 generation 所有必需阶段重新确认后报告 ready；失败保持 failed/recovering 并保留最近错误。关闭或恢复中的状态不得显示 ready。
- SDK API 与行为变化必须更新依赖锁 pin；A0 在新 pin 上重核公开头文件、调用返回传播、兼容性和构建集成后，才可解除 CR-015 阻塞。禁止只对旧 pin 做本地 shim 后宣称满足 API 契约。

## 4. 验收门

**L2：源码/组件契约和测试**

1. 测试覆盖 device find/open、AFE、VAD、KWS model load、enable/start 各失败阶段，snapshot 与回调给出正确 generation/stage/code，且 ready 被拒绝。
2. 成功路径验证真实 model name/ID、loaded/enabled 状态均来自 SDK typed API；单独 MQTT/agent/session/PCM 成功不得使语音 ready。
3. 运行期故障会撤销 ready；关闭后恢复创建新 generation；旧 generation 事件不会覆盖新状态；重复注册/注销、部分失败回收与重新 init 可验证。
4. A1 仅通过公开头文件/API 集成；新 dependency pin 下构建与兼容性检查通过。不得改 vendor 源码规避失败。

**L3：目标硬件单项验证**

在目标 ESP32-S3 记录 audio device open、AFE/VAD、实际 WakeNet 模型标识、loaded/enabled、运行状态及失败码；覆盖启动失败、运行期故障、关闭和恢复。证据应来自 typed API snapshot/event 与设备日志交叉核对。

**L4：端到端闭环**

绑定固件 SHA 与配置，验证真实唤醒、语音上行、Tuya 会话和回复播放；注入或制造 audio open、AFE/KWS 初始化/运行失败，确认语音入口拒绝且状态原因准确；修复后重试并完成唤醒对话，证明新 generation 恢复。记录时间关联的设备/API/MQTT/session/播放证据及现场听感确认。

未达到 L2/L3/L4 前，CR-015 不得标记完成；源码中 API 存在或 L2 模拟通过不能代替设备及端到端证据。
