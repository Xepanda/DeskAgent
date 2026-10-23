# CR-033：ESP32-S3 结构化设备诊断事件 allowlist

- **提出方：** A0 架构与协议
- **状态：** **APPROVED — L1 最小结构化事件契约；各 owner 的实现与 L2 验收仍待独立交付**
- **目标：** 支持已配对设备的低敏日志读取、解析与实时监控，仅传输经源码确认的结构化事件。此契约不提供 raw UART、`ESP_LOG`、`printf` 文本流或完整系统日志归档。
- **关联：** [CR-009](../CR-009-esp32s3-network-log-source.md)（v1 结构和通道）；[CR-032](CR-032-cr009-tal-log-hook-startup-seam.md)（TAL 输出 hook 安全 seam）；CR-014（Host session generation）；CR-018（提醒存储，非本 CR 依赖）。
- **证据界限：** 本 CR 是 L1 文档/协议裁决，不代表实现、目标板验证或端到端服务部署成功。

## 1. 范围和兼容策略

CR-009 v1 只允许 `CAMERA_CAPTURE_EMPTY(fields={})`；CR-032 批准的 TAL observer 对普通格式化文本全部丢弃。本 CR 批准以下封闭的新增结构化事件，沿用 CR-009 的 `sys.log.event` 结构、序号/gap 语义、单一已配对 WebSocket 通道和 `log_schema_version: "1.0.0"`。这是对事件 allowlist 的 A0 明确增量裁决：envelope 和字段类型体系不变；仍严格校验 event allowlist 的旧接收端必须拒绝不认识的 event。事件只可由所列 source 函数直接提供 enum/标量，不可从 `printf`/TAL/ESP-IDF 字符串扫描或猜测生成。未来若改变 envelope 或既有字段/类型，须另行裁决并升级 schema version。

所有未列出事件、字段、状态、来源均拒绝。旧接收端遇到未知事件必须 fail closed；新接收端继续接受 CR-009 的 `CAMERA_CAPTURE_EMPTY`。任何改变事件结构、增加任意文本字段或扩大 allowlist 的修改必须另由 A0 裁决。

## 2. 批准的最小事件 allowlist

每个事件除 CR-009 v1 公共字段外，仅可带下表所列 `fields`。所有事件级别固定为 `WARN` 或 `ERROR`，不得携带原始错误码、自由文本或调用参数。字段名称及枚举大小写是 wire contract。

| `component` / `event_id` | 精确 `fields` | 真实来源和生成条件 | 限制 |
|---|---|---|---|
| `audio` / `AUDIO_DRIVER_STATE_SNAPSHOT` | `{ "state": enum }`；`state ∈ {READY,NOT_READY}` | A1 `firmware/components/desk_hardware/src/desk_hardware.c::desk_hardware_audio_get_status()` / `desk_watcher_audio_status()` 在有效日志订阅建立后读取当前 `ready` bool，并由 A1 source 发布一次快照。不得把 `desk_watcher_audio_init()` 早期输出重放。 | 每个 stream 最多一次。仅代表 shared audio driver readiness；不证明麦克风输入、扬声器输出或语音链路可用，不附带 frames/errors/peak 或硬件型号。 |
| `network` / `TUYA_MQTT_STATE` | `{ "state": enum }`；`state ∈ {CONNECTED,DISCONNECTED}` | A1 `firmware/main/src/app_main.c::tuya_event_handler_on()` 仅由现有 `TUYA_EVENT_MQTT_CONNECTED` / `TUYA_EVENT_MQTT_DISCONNECT` 事件映射。 | 仅状态边沿；重复相同状态抑制。不含 SSID、IP、MAC、broker、账户或凭据；不把 MQTT 状态称为 SDK DeviceSession 状态。 |
| `stm32` / `STM32_ACK_TIMEOUT` | `{}` | A1 `firmware/components/desk_hardware/src/desk_hardware.c` 中实际等待 UART2 STM32 ACK 超时并递增 `s_mcu_timeout_count` 的路径。只在 ACK deadline 到达时发出。 | 每 60 秒最多一条；不得包含运动/命令种类、sequence、角度、速度、duration、payload 或 UART 字节。只说明一次本地主机命令未在期限内得到 ACK，不证明 peer/offline 或物理状态。 |
| `camera` / `CAMERA_CAPTURE_EMPTY` | `{}` | 保留 CR-009 v1：A1 `desk_watcher_camera_capture()` 收到零长度 PTL header 并耗尽 retry limit。 | 维持 CR-009 定义；额外受统一 stream rate limit 约束，不增加图像、长度、帧数据或错误文本。 |

`AUDIO_DRIVER_STATE_SNAPSHOT` 的 `NOT_READY` 只表示读取时 `ready == false`，不得解释为初始化失败、麦克风故障或扬声器故障。`TUYA_MQTT_STATE` 只在有效 stream 存续期间观察到的实际状态回调时发布；不补发订阅前的历史状态。`STM32_ACK_TIMEOUT` 不等价于新建通用 STM32 peer health/heartbeat capability。

**候选但不纳入网络 allowlist：** `desk_boot_check()` 的启动失败在 Runtime/paired log stream 建立前会终止启动；按 CR-009 的无持久化、无离线 replay 规则，不能声称网络可观测，继续留在原本地 UART/CLI 诊断路径。A2 的 `desk_sdk_ws_state_t` 确有连接状态枚举，但尚未认证/订阅前不能发送日志，断开后也无法通过已断开的 stream 上报 offline；连接代次与结束通知由 CR-014 的 Runtime session 接口处理，因此本 CR 不把 SDK 连接状态转为设备日志事件。当前没有统一、校准过的资源压力阈值/采样 source，不增加资源告警。

## 3. 全局隐私、速率与默认关闭

1. 网络事件只通过 CR-009 已批准的 paired WebSocket structured event path 发送。设备 local log gate、Host exact-bool opt-in 和当前 session authorization 任一未成立时不收集或发送；当前 Host `_network_log_authorized` 仍 fail closed。SDK session 事件不得绕过该 gate。
2. 订阅默认关闭，设备重启后关闭，断连清除未发送队列；不写 Flash/NVS/SD，不保留跨断连日志，不重放。消费者/生产者慢时有界 drop-new，按 CR-009 `QUEUE_FULL` gap 语义报告，不阻塞业务。
3. 全局上限：单设备单活动 stream 每分钟最多 **30 条事件**；每个事件类型还遵守表内更严格的每 stream snapshot、状态边沿或 60 秒限制。重复状态和速率抑制的事件在序号分配前丢弃，不产生 `QUEUE_FULL` gap；只有已分配 sequence 后发生有界队列容量丢弃时才产生 queue gap。
4. `fields` 严格按表定义校验：不允许额外字段、字符串自由文本、嵌套对象、负值/越界数值、NaN/Inf 或任何凭据/用户内容。每条 event 总编码长度仍受 CR-009 v1 record/writer 上限约束；过长输入丢弃并计数，绝不截断后发送。
5. 禁止采集/传输 raw UART bytes、原始 TAL/`ESP_LOG`/`printf`、ASR/语音转写、PCM、图像/JPEG、IP/MAC/SSID、pairing/session token、认证头、license/密钥、命令参数、motion/camera 请求内容、提醒/HA/Codex 业务内容。CR-032 TAL observer 对自由文本仍全部丢弃，本 CR 不允许基于文本正则扩展 event source。
6. 不定义通用 heap/stack/CPU/队列水位 `RESOURCE_PRESSURE` 事件：当前代码没有统一、已校准的阈值与来源。CR-009 固定队列容量/丢弃 gap 只用于传输自身，不冒充系统资源健康告警。

## 4. 哪些可单独做 L2，哪些受阻

- **可在当前软件单独实现并做 L2：** A1 对已有函数结果增加仅含表内字段的结构化 producer；A2 在已有 bridge formatter/allowlist 中序列化指定 event/schema 并保持现有单 writer/gap/gate；A3 扩展 Host consumer 的严格 schema/event allowlist 并继续默认关闭；A0 维护此契约。无需真实设备或服务凭据即可使用 fake source、socket shim 和 consumer fixture 验证模型、过滤、速率限制、序号/gap 与 fail-closed。
- **CR-014 阻塞的部分：** A3 不能据此开启 Host consumer 或宣称可实时收到设备日志。Host session generation/end notification 的安全授权仍由 CR-014 定义；在其接口实现/验收前，`_network_log_authorized=False` 必须保持。
- **CR-018：** 是提醒 durable storage 决策，不是本日志事件的依赖。本 CR 明确日志不持久化；不得用 CR-018 推导日志可写设备 Flash。
- **需要硬件/现场证据的部分：** 真实 ESP32-S3 各 logger/外设行为、音频 readiness 含义、STM32 UART ACK timeout 的 peer 相关性、实际网络收发、RAM/stack 高水位、UART 原输出保真和与语音/控制并发影响，均不能由 host shim/L2 推断。

## 5. Ownership 与定向 L2 验收

| Owner | 独占路径与任务边界 |
|---|---|
| **A0** | `docs/implementation/`、`contracts/`：冻结 event/schema contract 与 privacy gates；不实现生产 source。 |
| **A1** | `firmware/components/desk_hardware/`、`desk_tclaw/`；本 CR 对 `firmware/main/src/app_main.c` 仅授权在现有 `tuya_event_handler_on()` 的 MQTT 状态来源处调用 A1 structured producer，不改其他启动/业务行为。新增 source 必须只发本 CR 枚举；CR-032 `desk_tuya_log_output()` seam 仍不得把自由文本映射为事件。 |
| **A2** | `firmware/components/desk_sdk_bridge/`：只实现已批准 event enum/fields 序列化与单 writer 发送，不读取 UART/任意 logger 文本、不绕过 auth/lifecycle gate、不修改 A1 source。 |
| **A3** | `host/desk_agent/core/` 及对应测试：严格解析 allowlist、保持默认关闭、执行 per-session bounded consumer；CR-014 未闭环前不得 authorize。不得持久化日志。 |
| **A7** | `tests/e2e/`、`tools/verification/`、`docs/acceptance/`：独立验收工具和 evidence matrix；不得改生产实现。 |

各 owner 最小定向 L2 验收：

1. **A1 source tests**（如 `tests/firmware_contract/test_watcher_log_source_contract.py`）：每个允许来源只产生表中 enum 和精确 fields；启动失败/`license_unavailable`、音频错误细分、原始日志文本、伪造 `CAMERA_CAPTURE_EMPTY` 文本均不产生记录；音频 snapshot 仅报告 `ready` bool；边沿/每 stream/60s/全局 30-per-minute 限制正确；source disabled 不采集；并发、满队列和 gap 满足 CR-009。
2. **A2 bridge tests**（`tests/sdk_contract/test_official_sdk_runtime_compat.py`、`test_sdk_bridge_protocol.py`）：逐 event 验证 wire JSON、schema version、`component/event_id/fields` 精确 allowlist；缺字段/额外字段/错类型/越界值/未知事件 fail closed；认证/重复订阅/取消/断连清队列和单 writer deadline 的既有门槛不回退。
3. **A3 consumer tests**（`tests/core/test_network_log_consumer.py` 及 managed listener/lifecycle tests）：所有允许事件解析；raw 文本、凭据字段、用户内容、未知事件均拒绝；队列有界；默认关闭和断连清空；CR-014 unavailable/未授权时零事件被接受。
4. **A0 schema/doc check：** 机器可读 fixtures 或 schema validator 对每个事件正反例校验；确认所有记录只含有界 enum/标量，无日志落盘或新增日志原文。

上述是 L2 软件验收要求，不声称目标板有效。L3 至少需要真实 ESP32-S3 固件/Runtime 观察每种实际设备 source，并核对 UART 与设备资源；L4 还需实时并发、断线/重连/复位与持续运行恢复证据。CR-014 未完成前 Host live monitor 仍不成立。
