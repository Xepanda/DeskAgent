# CR-015：Tuya AI 音频输入就绪门禁

- **提出方：** A0
- **提出日期：** 2026-10-22
- **状态：** PROPOSED — 待 A1/A0 评审；未实现，语音链路 readiness 尚未验收
- **预期 owner：** A0 裁决跨层契约与依赖兼容性；A1 负责设备适配和固件接入；A7 验收
- **范围：** 只定义语音输入从初始化到可唤醒的就绪语义。不修改 TuyaOpen/TClaw vendor 文件，不将 MQTT、AI agent 或 session 状态单独视作音频 ready。

## 1. 固定依据与源码发现

本 CR 按仓库 `contracts/dependency_lock.json` 和 TClaw provenance 核对：

- 构建使用的 TuyaOpen SDK pin 为 `cdc064e78daeeae598157470244e351cd37cbbed`。**当前 `D:\tuya_proj\TuyaOpen` checkout 已不是该干净 pin**：工作区 dirty，HEAD 为 `752b14bb7dbcba703ee5bcb36b31c32b4fcbcb05`。本次只读使用其 Git object database 中的锁定 `cdc064e` commit（对象存在），未切换、修改、清理或拉取该外部仓库，也未以 dirty HEAD 作为锁定源码证据。
- TClaw 应用源码 pin 为 `88bd37378f1d106fcc1175e61b368222cc8fabda`，由 `firmware/third_party/tclaw/PROVENANCE.md` 记录。该 TClaw 上游仓库的 `TuyaOpen` 子模块 gitlink 是 `e82840dc9b629cb77fa922fb04e3f820dc2e78b4`；子模块内容未 vendored。**运行构建依赖以 dependency lock 中的 TuyaOpen SDK pin 为准**，gitlink 只说明 TClaw 上游来源时使用的 SDK 版本。
- 本文行号对应上列 SDK checkout 与仓库当前源码；后续 pin 改变时，必须重新核对行为和行号。

已确认的初始化路径：

1. `firmware/third_party/tclaw/src/tclaw_chat.c:239-248` 以 `AI_CHAT_MODE_WAKEUP` 为默认模式调用 `ai_chat_init()`，并传播其初始化错误。
2. SDK `src/ai_components/ai_main/src/ai_chat_main.c:404-415` 中，`ai_chat_init()` **先**订阅 `EVENT_MQTT_CONNECTED` 的 `__ai_mqtt_connected_evt`，再调用 `ai_audio_input_init()`。若之后的 audio input 初始化失败，函数错误返回；该路径没有对已成功注册的 MQTT listener 做撤销。
3. SDK `src/ai_components/ai_audio/src/ai_audio_input.c:357-389` 中，`ai_audio_input_init()` 依次查找音频设备、读取设备信息、创建 recorder、打开音频设备、按配置初始化 VAD（如启用）并启动输入；任何 `TUYA_CALL_ERR_RETURN` 失败都会中止并返回错误。返回成功是这些同步调用成功的证据，不是麦克风信号质量或后续模型长期运行的证明。
4. 由于 listener 在 audio init 之前注册，若 MQTT 后续连接且事件总线保留该订阅，`__ai_mqtt_connected_evt()` 仍可能执行。固定 SDK `ai_chat_main.c:317-329` 显示该回调调用 `ai_agent_init()` 并检查其错误，但**忽略** `ai_mode_init(mode)` 的返回值，随后无条件将 `sg_ai_agent_inited` 置为 `true`。`ai_manage_mode.c:94-110` 的 `ai_mode_init()` 会传播 mode handler `init()` 错误，因此该错误在 MQTT event callback 这一层丢失。
5. 该回调中的 `ai_agent_init()` 实现在 `src/ai_components/ai_agent/src/ai_agent.c:202-237`，进一步调用 `tuya_ai_agent_init()`，再到 `src/tuya_ai_service/svc_ai_agent/src/tuya_ai_input.c:567-591` 的 `tuya_ai_input_init()`；这段代码分配输入缓冲区、创建互斥量并启动 AI 输入线程。它不打开前述本地捕获设备，也不证明 AFE 或 KWS/WakeNet 已 ready。因此 AI agent 初始化成功不能覆盖先前 audio input 失败，也不能证明唤醒模式已成功初始化。
6. 语音编排层 `firmware/main/src/tclaw_runtime_adapter.c:666-680` 记录 `tclaw_chat_init()` 错误；但 `firmware/main/src/app_main.c:441-459` 对 `desk_tclaw_runtime_init()` 失败只输出 WARN，随后仍调用 `tuya_iot_start()`，使 provisioning/MQTT 可以继续运行。这样做有利于保留配置通道，但必须避免把“系统/MQTT/agent 可用”上报成“语音输入已就绪”。
7. 固定 SDK `tools/porting/adapter/kws/tkl_kws.h:21-43` 定义唤醒词 enum（含 `TKL_KWS_WAKEUP_NIHAO_XIAOZHI`）和 `tkl_kws_init/reg_wakeup_cb/enable/disable/deinit` 操作，但没有读取实际模型名/ID、KWS ready 状态或运行失败的查询 API。`ai_chat_init()` 在 `ai_chat_main.c:421` 检查 `tkl_kws_init()` 返回值；但 `ai_mode_wakeup.c:167-170` 忽略 `ai_audio_input_wakeup_mode_set()`、`tkl_kws_reg_wakeup_cb()` 和 `tkl_kws_enable()` 的返回值，调用者无法从 `ai_mode_init()` 得知这几项失败。对固定 commit 中 KWS adapter 与 AI main/mode 路径的符号检索未发现等价的 model/status getter。

上述事实是对固定源码的 **L1 源码推导**。事件订阅在失败路径上的实际保留、回调的真实调度、音频硬件行为以及 WakeNet/AFE 实际状态仍需 L2/L3 验证。

### A0 复核补记（2026-10-22，锁定对象只读审计）

本次使用 `git show cdc064e:<path>` 与 `git grep <symbol> cdc064e -- <path>` 审计锁定对象，确认完整 commit SHA 为 `cdc064e78daeeae598157470244e351cd37cbbed`；具体源码发现见上列第 4、7 项。dirty checkout 状态与 locked commit 的差异已明确分开记录，没有用当前 HEAD 推导上游行为。

**A0 结论：当前不需要新增独立 CR。** 返回值传播、ready 状态和模型身份均属于本 CR 的语音 readiness 范围。TuyaOpen AI mode/main owner 应在 `ai_mode_wakeup.c` 检查并传播 VAD mode、KWS callback registration 与 enable 错误，并在 MQTT callback 仅于 `ai_mode_init()` 成功后设置 agent/mode-ready 标志；TuyaOpen KWS adapter/平台实现 owner 应提供受支持的 ready/model identity 查询（至少实际模型名或 ID、loaded/enabled 状态和失败码），运行期故障撤销 ready 则需状态变化/错误通知。A1 只消费公开接口，不得以私有 backend、回调注册成功或 PCM 曾产出推定模型已加载。若上游确认无法扩展该 API，再由 A0 拆分新的窄接口 CR 并裁决依赖 pin；本次不修改 vendor/生产代码。该结论不证明目标板 WakeNet/AFE 当前 ready。

## 2. 风险与复现条件

当 `ai_chat_init()` 成功注册 MQTT listener 后，任一音频输入初始化步骤失败（例如音频设备未注册、open 失败、VAD/输入启动失败），随后网络连接触发 MQTT connected callback，系统可能出现以下不一致状态：

- `tclaw_chat_init()`/runtime 报初始化失败，但设备继续启动 MQTT；
- AI agent 输入缓冲区/线程或云侧 agent 可能继续初始化；
- `ai_mode_init()` 失败不会阻止 `sg_ai_agent_inited=true`；
- 系统、MQTT、agent 或 session 的成功状态可能被误读为麦克风、AFE 和唤醒链路已可用；
- 用户按键/唤醒词无响应，或者重试时遇到半初始化状态。

这描述的是源码允许的风险路径，不代表已在当前设备上复现，也不主张云 MQTT 必然连接或相关 callback 必然成功。

## 3. 契约要求与方案裁决

设备语音状态必须将传输/agent 状态与本地音频 readiness 分开。至少区分：

| 状态 | 含义 | 对外可报告语音 ready |
|---|---|---|
| `AUDIO_INPUT_INIT` | 正在完成设备查找、open、AFE/VAD/KWS 等初始化 | 否 |
| `AUDIO_INPUT_READY` | 当前会话代次所需输入链路与唤醒模式均确认初始化成功 | 是，可进入唤醒监听 |
| `AUDIO_INPUT_FAILED` | 某阶段初始化/运行失败，包含阶段与错误码 | 否；不得接受唤醒/对话启动 |
| `AUDIO_INPUT_RECOVERING` | 释放失败资源并重试或等待显式恢复 | 否 |

`MQTT_CONNECTED`、`ai_agent_init` 成功、AI session 创建成功、扬声器可播放提示音、PCM 曾经产出，都不能单独把状态升级为 `AUDIO_INPUT_READY`。设备端必须在音频路径关闭或异常后撤销 ready；MQTT 断开本身是否撤销本地唤醒监听由产品策略决定，但不得混淆本地唤醒 ready 与云端对话可用。

方案选项：

1. **优先：使用依赖 pin 支持的 typed readiness API/event。** API 应分别报告 audio-device open、AFE/VAD、KWS 模型加载与启动结果，以及失败阶段/错误码；A1 只消费公开接口并驱动设备状态。若当前 pin 没有该接口，由 A0 发起上游/API 或依赖升级裁决，更新 pin 后重做兼容验证。
2. **备选：由适配层传播明确的 audio-init failure。** 仅在现有公开回调/返回值足以覆盖初始化和后续 WakeNet/AFE 失败时采用；同步初始化返回错误必须阻止语音 ready，运行期错误必须撤销 ready。若 SDK/TClaw 未暴露必要 hook，应先提交接口变更请求，不得通过私有符号、内部结构或本地 vendor patch 猜测状态。
3. **不接受：** 仅用 MQTT、`ai_agent_init`、session 状态、输入线程创建成功或“未见错误日志”推定音频 ready；也不接受只在 `app_main` 忽略 runtime 错误的情况下继续显示/播报语音可用。

推荐裁决为方案 1；先由 A0 确认当前 pin 是否有覆盖完整音频/模型生命周期的公开 readiness 接口。若没有，则提出受支持的上游接口/升级请求，再由 A1 接入。`app_main` 可保留 provisioning/MQTT 的 best-effort 启动，但必须保持语音失败状态可观察、语音入口 fail-closed。

## 4. 诊断日志与指标

每次启动/恢复应有单调递增的 `audio_generation` 或等价实例标识，输出结构化且不含凭据的阶段日志：

```text
[AUDIO] generation=N stage=device_find state=ok|failed rc=...
[AUDIO] generation=N stage=device_open state=ok|failed rc=...
[AUDIO] generation=N stage=afe_init|afe_start state=ok|failed rc=...
[AUDIO] generation=N stage=wakenet_model_load|kws_start state=ok|failed rc=...
[AUDIO] generation=N stage=ready state=ready|rejected reason=... rc=...
```

同时记录 `mqtt_connected`、`ai_agent_init` 和 `ai_mode_init` 的结果以帮助对时序，但它们只能作为独立状态。至少保留各阶段最近错误码/失败计数、ready/revoked 次数、恢复尝试次数及最后一次成功 generation。不得记录配对码、token、Secret、原始音频或未经授权的敏感内容。实现可采用等价字段，但失败必须能定位到具体阶段，不能只留总的 `runtime_init rc`。

## 5. 验收门槛

### L2：主机/组件测试

1. 模拟 `ai_audio_input_init` 在设备查找、open、AFE/VAD 和输入启动各阶段失败；确认 runtime 保留失败阶段/错误码，语音状态为 `AUDIO_INPUT_FAILED`，唤醒/会话启动被拒绝。
2. 先成功注册 MQTT callback，再令 audio init 失败并触发 MQTT connected callback；确认 agent/session callback 即使运行也不能升级音频 ready，`ai_mode_init` 失败不得被吞掉或伪装成功。
3. audio open、AFE、WakeNet/KWS 初始化成功的正向路径中，只有所有必需阶段成功后才报告 `AUDIO_INPUT_READY`；WakeNet/AFE 任一未 ready 或返回失败必须拒绝启动并给出原因。
4. 模拟恢复：清理已打开/部分初始化资源、修复故障并重试；新 generation 完整初始化后可 ready，旧 generation 的错误/回调不能覆盖新状态。重复初始化、重试失败和取消路径不得泄漏资源或卡死。
5. `MQTT_CONNECTED`、AI agent 初始化、session 创建、扬声器播放或 PCM 计数单独成功的负例均不得通过 ready gate。

### L3/L4：真实设备

- **L3 单项硬件：** 在目标 ESP32-S3 上记录设备查找/open、AFE/VAD、实际 WakeNet 模型名/加载结果、KWS 启动和 PCM/错误计数；分别注入或制造 audio open 失败及模型/AFE 未 ready，确认 UI/按键拒绝开始语音并报告具体原因；恢复后确认状态正确转为 ready。
- **L4 端到端：** 在相同固件与配置上现场完成唤醒、至少一轮云端语音上行与回复播放；覆盖启动失败、网络晚于 audio init、恢复/重启后再次唤醒，并提供有时间关联的音频、MQTT、agent/session 与播放日志及用户听感确认。

源码审阅和 L2 测试不能代替 L3/L4。无真实硬件/服务证据前，不得标记语音唤醒或对话闭环已完成。

## 6. 未证实事项

- 当前 MQTT event bus 在 `ai_chat_init()` 后续初始化失败时，是否确实保留该订阅并调度 callback，须以 L2 故障注入确认。
- 本机锁定 SDK 的精确 `ai_chat_main.c` 路径已核对；本 CR 未从 TClaw 上游 gitlink `e82840d...` 单独检出并审计其源文件。构建实际依赖为锁定 SDK `cdc064e...`，升级或变更必须重新核对。
- 本 CR 未确认 DeskAgent 当前接入的 WakeNet 模型文件、模型名、SDK 某一板级配置下的 AFE 后端、AFE 初始化完成回调及运行期错误事件是否完整暴露给公开适配接口；这些是 A0/A1 方案裁决前置核验项。
- 当前设备是否存在上述失败症状、失败概率、是否曾在设备上成功唤醒/多轮对话，均不由本文源码静态分析证明。
