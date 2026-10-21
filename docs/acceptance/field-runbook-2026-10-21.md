# Desk Agent R1 现场验收 Runbook（2026-10-22 CLI 镜像增量基线）

本 runbook 供后续 A1/A2/A3/A4/A5/A6/A7 按模块执行。记录的现场设备为 SenseCAP Watcher，串口约定 COM21；COM20 是 Himax 端口，**禁止将 COM20 用作刷机/设备控制端口**。同一时刻只允许一个操作者占用 COM21。任何刷写必须有当次明确授权，且使用已核验的 OTA application image；不得用合并 QIO 镜像写 OTA 分区，也不得用 `firmware/dist` 的旧文件替代当前归档镜像。

## 1. 当前基线和验收判定

- **当前检查镜像（本轮新刷入 CLI app）**：2026-10-22 CLI 构建成功；OTA app 1,899,680 bytes，SHA-256 `C09E84798654C8071875D04901213B8979BF2032F1B14F921CF911A6F857BE65`。经 COM21 app-only 写入 OTA0 `0x20000` 并 verify hash 成功。此前 camera-diagnostic 镜像哈希 `BCDAFB90625F759C56FB8F69BBA1A9EF1CC255DDDF295107DAB19FEAE5D6598` 属于旧检查基线，不应再作为本轮 CLI 状态的镜像标识。请勿使用 `firmware/dist` 旧产物。
- **分区保护提示**：构建产出的 partition table 与实机分区表不一致；实机 OTA0 为 `0x20000`、6 MiB，model 为 `0xC20000`。本轮未写 partition table、bootloader 或 model；COM20 未触碰。除 app-only 且地址经核对的操作外，先解决差异再操作。
- 已恢复模型：`firmware/.build/bin/srmodels.bin`，SHA-256 `7C87DD7ADB5A7623907B6354D49BEA7F9289371262E6A57F15458FB8908C5814`。
- Wi-Fi / provisioning：当前 CLI 镜像 `wifi status` 为 `LINK_UP`；provisioning stage 为 `mqtt_online`，但 `authorize_read rc=-1`。这是 L3 单项状态观测，不代表云端授权或 Tuya AI 会话已可用。
- 麦克风：当前 CLI 镜像显示 READY、running、895 frames、errors=0、peak=95，属于 L3 PCM 采集观测。用户此前确认的唤醒/对话发生在旧镜像上；当前 CLI 镜像尚未重新验证 WakeNet、ASR、多轮会话或回复播报。
- 相机：L3 失败。当前 CLI 镜像日志显示 camera driver READY，但该状态只表示初始化路径、I2C expander 配置以及 SPI bus/device 创建成功；代码没有 HX ID/应答探测，因此 READY 不证明传感器已就绪。capture 命中同步后，7-byte SPI header 传输返回 `ESP_OK`，读数仍全零 (`raw=00000000000000`)，随后映射为 `rc=-25`、`out_len=0`，未取得 JPEG。来源：`firmware/components/desk_hardware/src/desk_watcher_camera.c:135-193,264-346`。现场无示波器/逻辑分析仪证据。
- A6 Host 相机页面：自动化页面测试是 L2；A3 SDK parser/managed adapter 使用 fake context 与有效 JPEG fixture 验证，也是 L2。设备没有真实 JPEG，且未连接官方 Runtime，故 Host 到浏览器的图像链路没有 L3/L4 证据。
- STM32 / body UART：本轮真实发送小幅命令 `body move 91 120 500`，返回 `rc=-3` (`DESK_HW_ERR_TIMEOUT`)，CLI `accepted=false`。随后 `body status` 显示 UART READY、cached setpoint 仍为 90/120、last formatted TX length=23 bytes。该 TX 长度只是发送帧记录，不是 STM32 RX/ACK；没有实物动作确认，动作未通过。日志：`.target_logging/com21-small-motion-test-20261022.log`。
- HA：当前仅 L2 connector 测试；没有用户现有 HA 会话或物理设备动作证据。
- 提醒：`LifeService` 23 项和定向 reminder catch-up 1 项通过；SQLite teardown 的 `WinError 32` 修复属 L2。强制 kill 崩溃恢复、真实 Host 重启、设备到期播报/确认未验。
- SDK bridge：5 项 source-contract 检查通过；依赖本机 GCC 的 10 项用例被 skipped，没有执行结果，不等于构建或官方 SDK 互通通过。
- 输入/UI：9 项软件 contract 测试；滚轮和按键已有用户现场确认，详细输入事件日志待归档。
- Codex Companion：真实 app-server 默认模型调用及隔离工作区修改/验证任务通过，任务记录跨 Companion 重启保留；属本机单项证据。审批/取消/失败恢复及设备触发和设备反馈未验。
- 官方 Python SDK 互通：官方 parser 对 fixture 的 10 项互操作测试通过（L2）；pairing window CLI 曾打开/取消。真实 Runtime WebSocket 发现、配对、HardwareHello、能力注册及命令/媒体闭环未验。
- ESP32-S3 日志：A7 logger 7 项测试通过（L2）；COM21 原始采集 350 bytes，捕获本轮相机失败日志（L3 单项观测）。采集成功不代表日志范围之外的模块正常。
- 系统验证脚本：最近一次 `verify_all.sh` 在 Step 2 失败，Step 3–8 未运行；本轮不得报告全套门禁通过。

只有结果、证据和设备反馈都满足表中门槛，才将对应能力标为通过。设备成功启动、函数返回成功、帧长度非零、HTTP 200 或 SDK 安装成功都不能单独作为该能力的通过证据。

## 2. 软件回归（不占串口、不构建固件）

在 PowerShell 仓库根目录执行，保存退出码与完整摘要：

```powershell
python -m unittest tests.core.test_life_service
python -m unittest tests.core.test_host_daemon
python -m unittest tests.connectors.test_ha_connector
python -m unittest tests.companion.test_codex_companion
python -m unittest tests.e2e.test_real_chain_tclaw_to_host
python -m unittest tests.e2e.test_p1_regression_gates
```

这些命令提供 L2 证据。最近一次 `tools/verification/verify_all.sh` 在 Step 2 失败，Step 3–8 未运行。该脚本会运行本机 C 构建、全量 Python 套件和部署预检；执行前确认没有其他 Agent 占用共享 `firmware/build`，并且当前环境具备脚本依赖。脚本通过仍不能自动升级任何 L3/L4 状态。

## 3. 语音与输入复测（COM21 单人占用）

### 操作

1. 先检查板卡启动日志包含预期 WakeNet 模型 `wn9_nihaoxiaozhi_tts`、AFE 初始化成功、Tuya AI session 有效；不要把本地 shim 的 `Cloud DISCONNECTED` 单独当云端断线结论，需和 provisioning/MQTT/AI session 日志交叉判断。
2. 在安静环境说两次唤醒词，每次等待设备反馈；随后进行至少三轮有上下文的问答。
3. 观察期间音频帧数继续增长、采样错误不增加；每轮应能关联 KWS（首轮）、ASR、会话状态、NLG/TTS 和实际扬声器输出。
4. 旋转滚轮正向/反向各数个 detent，确认只读状态页选择/滚动正确；短按、长按分别确认触发 TuyaOpen 既有短按语音和长按语音事件，不应互相串扰。
5. 用独立观察者记录唤醒成功率、听感、UI 变化；保存带时间戳原始日志，记录固件和模型 SHA。

### 通过证据

- 至少 2/2 唤醒成功、连续 3 轮问答都有 ASR 与真实播报；对应设备日志完整，无采集错误增长或会话失败。
- 滚轮事件与页面变化可对应；短按/长按各有正确事件日志和语音行为。
- 此处通过是语音和输入的单项 L3；只有加入真实外部任务/设备执行、真实结果和设备反馈后才可评 L4。

## 4. 相机取帧（L3 当前失败，先做信号定位）

### 先决步骤

1. 不重复盲目 capture。保持当前已知镜像和模型分区，禁止触碰 COM20、禁止写入未识别分区。
2. 根据当前板卡原理图/接线确认相机供电、地、排线方向及电平；不能仅凭 GPIO 名称猜测电源脚或总线脚。
3. 下一次只进行一次有仪器同步观测的 capture：同时探测 SPI CS GPIO21、SCLK GPIO4、MISO GPIO6、MOSI GPIO5、PCA expander SYNC bit 6，以及 HX 电源轨。记录探头位置、采样率、时间基准和电压；当前没有这些波形证据。
4. 对照同一次 capture 的波形判断：HX 电源轨稳定；PCA SYNC bit 6 与 CS 窗口/边沿顺序正确；SCLK 有预期脉冲；MOSI 有请求数据；MISO 是否返回非零/协议预期 PTL header。当前实现的 `READY` 只代表初始化、expander 配置和 SPI bus/device 创建成功，没有 HX ID/应答探测。已知失败路径是同步命中后 7-byte SPI read 返回 `ESP_OK`，但数据全零，再由代码返回 `rc=-25`；不可把它说成 SPI API 传输失败，也不可仅凭 READY 判断传感器就绪。若 MISO 仍全零，先定位电源、复位、连接、CS/SYNC 与总线方向，不改解析器来掩盖电气问题。

### 取帧和通过证据

5. 只有总线头可读后才连续采集至少 10 帧。每次保存完整底层错误码及阶段（等待同步、SPI、协议、CRC、JPEG 解码），记录长度和耗时。
6. 导出原始 JPEG 文件，检查 SOI/EOI 标记并用独立图像查看器打开；由现场操作者确认画面对应实际场景。
7. 重复采集后确认无死锁、内存/PSRAM 无持续泄漏、关闭租约可释放相机资源；无真实 JPEG 即不通过，不得使用合成画面。

### 门槛

- L3：10 帧均成功且 JPEG 可打开、画面正确，采集错误可归因，租约可释放。
- P1 局域网查看：再验证鉴权、帧新鲜度（断线明确不可用，不显示旧图冒充新图）、连接恢复和云台动作仲裁。单帧 JPEG 仅表示快照，不表示视频流。

## 5. STM32 与机身动作（L3 未验证）

1. 先从本板原理图和实际线束确认 UART TX/RX/GND、电平、波特率/校验/停止位；确认 STM32 固件的帧格式、CRC、序号/ACK 规则。保存这些基线，不更改未知接线。
2. 使用接收侧计数或逻辑分析仪独立证明 STM32 收到命令；固件 `Last Packet Length` 只是发送帧长度，不能计作接收。
3. 用低速、小幅度、无负载动作验证命令与 ACK 的 request/sequence 关联；记录发送/接收原始帧、CRC、超时和 ACK 状态。
4. 现场确认停止命令、限幅和动作所有权仲裁；拔除/屏蔽通信时动作必须超时并安全停止，不能显示成功。

通过证据：收到正确 ACK，实物动作方向/幅度与命令一致；故障注入后安全停机；至少覆盖正常、错误 ACK、超时三种记录。软件 C/Python 模拟只计 L2。

## 6. Home Assistant（L3/L4）

1. 使用已部署的泰山派 Host 配置连接用户现有 HA；凭据仅走安全配置，不写进日志、命令输出或仓库。
2. 发现目标低风险灯/插座，验证白名单；发送开/关一次，分别记录请求 accepted、HA 状态变更事件、现场设备实际动作。三者分开判定。
3. 订阅状态事件，手动从 HA UI 改变一次状态，确认 Host 更新；再断开 HA 或制造超时，确认返回明确失败/未知状态而非成功。
4. 恢复 HA，确认重连与状态对账；不得调用锁、安防、门禁等高风险服务。

通过证据：脱敏日志、实体 ID、HA 事件/状态时间戳、现场观察者确认；accepted 不能代替物理动作。

## 7. 提醒与备忘（L3/L4）

1. 在目标时区创建一个短时提醒和一条备忘，记录 Host 数据库路径、创建时刻和 ID（不记录敏感内容）。
2. 重启 Host 服务，确认记录仍在；等待到期，确认 outbox occurrence 只产生一次并经真实设备显示/播报。
3. 用户确认提醒后检查状态和 outbox，确认同一 occurrence 不再播报；另测 snooze 后按规则再次触发。
4. 在隔离测试提醒上让设备离线跨过到期时刻，再恢复连接，确认系统按产品规则补播或明确标记错过；记录规则，不以“测试通过”替产品定义行为。

通过证据：DB 状态前后快照（脱敏）、Host 投递日志、设备播放/显示及用户确认、无重复播报；软件定时器测试仍仅为 L2。

## 8. Companion / Codex（L3 单服务与 L4 设备闭环）

1. 仅使用隔离测试工程和用户可销毁的测试分支/副本。先验证省略模型参数时真实 app-server 默认模型调用成功，并保存 thread/turn 标识和 assistant 输出。
2. 提交一项无副作用的文件修改任务，检查 diff 属于隔离目录且由 assistant 输出说明；运行目标验证并读取退出码。
3. 分别验证一次需要审批的操作（拒绝后无动作）、明确失败任务、取消任务；重启 Companion 后检查任务状态与结果摘要恢复。
4. 通过真实 Host 与机器人通道把完成/失败/取消的 assistant 结果传回设备屏幕/播报；用户核对失败未误报成功、用户输入未被当作结果。

通过证据：隔离目录 diff、实际验证命令/退出码、审批记录、状态持久化、Host 传输日志、设备显示/播报及现场确认。只读 app-server 调用仅说明服务集成，不证明编辑闭环。

## 9. 官方 SDK 与局域网相机页

### 官方 Python SDK

1. 运行未经修改的官方 SDK/runtime；记录版本与 SHA。设备错误配对码必须拒绝且不得产生授权 token。
2. 正确配对后记录设备主动发起 WebSocket 连接、HardwareHello、capability 注册；执行命令/结果双向收发和真实媒体收发。
3. 中断网络后恢复，验证自动重连、重新注册以及无旧请求串线。自定义 Host TCP 客户端不算官方 SDK 通过。

### 局域网相机页（依赖真实 JPEG）

1. 先完成 §4。用 Host 发布真实设备 JPEG；浏览器显示内容及帧时间戳要对应现场。
2. 验证无 token/错 token 被拒，正确 token 可看图；lease 释放后 capture 停止。
3. 断开设备后页面显示不可用，不沿用旧图；重连后新帧序号和时间戳前进。
4. 手动云台动作必须持有动作仲裁权；语音/其他 owner 持有时拒绝或排队。

## 10. L4 整机闭环及恢复

只有所有依赖项通过后，由 A7 主持并按串口独占规则执行：

1. 语音提出一个低风险、可观察的真实任务（优先 HA 灯开关；或在测试工作区创建 Codex 修改任务）。
2. 记录：KWS → ASR → Tuya AI session/MQTT 状态 → 工具调用 → Host 路由 → 外部服务真实结果 → assistant 回复 → 设备屏幕/语音反馈。
3. 将结果与 HA/文件实际状态和操作者观察相互核对，分清 accepted、state changed、physical action、task completed。
4. 依次注入 Wi-Fi/服务断开、Host 重启、设备重启；核对任务不重复执行、提醒不丢、重连后状态一致。
5. 并发运行音频、相机、动作和任务时观察资源仲裁、错误计数及内存水位；记录持续时长与样本，不宣称未覆盖的长稳时长。

通过 L4 的记录必须含全链路关联 ID 与时间戳、实际服务/设备观察、故障恢复前后状态、固件/模型/Host/Companion 版本 SHA。任何一跳只有模拟证据时，该场景不能标为 L4 通过。

## 11. 每次现场记录模板

```text
日期与时区：
硬件板卡/修订：
操作者与观察者：
固件 OTA app SHA-256：
模型 SHA-256：
Host / Companion / SDK 版本：
场景与前置状态：
步骤及时间戳：
请求 ID / 任务 ID / occurrence ID（如适用）：
原始日志/波形/JPEG 文件路径及 SHA-256：
可观察结果（HA 状态/物理动作/设备听感与显示）：
结果：PASS / FAIL / NOT RUN
证据层级：L1 / L2 / L3 / L4
失败原因、复现条件和恢复结果：
```

不得把 FAIL 或 NOT RUN 汇总成“部分完成”之外的笼统完成率，也不得用前一次固件的日志证明当前镜像。
