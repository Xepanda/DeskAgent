# Desk Agent R1 现场验收 Runbook（2026-10-22 新镜像验收基线）

本 runbook 供后续 A1/A2/A3/A4/A5/A6/A7 按模块执行。记录的现场设备为 SenseCAP Watcher，串口约定 COM21；COM20 是 Himax 端口，**禁止将 COM20 用作刷机/设备控制端口**。同一时刻只允许一个操作者占用 COM21。任何刷写必须有当次明确授权，且使用已核验的 OTA application image；不得用合并 QIO 镜像写 OTA 分区，也不得用 `firmware/dist` 的旧文件替代当前归档镜像。

## 1. 当前基线和验收判定

- **当前设备镜像**：新 app-only 镜像 SHA-256 `72CAF9D437CFEEA604B4AC27559EC00772E567E35019DFD3CD80F204705B40CC`（1,898,288 bytes）已写入 COM21 OTA0 `0x20000`；esptool verify passed，随后从设备 `read_flash 0x20000/0x1cf730` 回读 SHA 完全一致。分区 SHA-256 `C6F7A19234CB70704BDF73FBDD94EC44BBEE664444AB5FDC527CFD1BA003ADFD` 与设备一致。仅写 app，未写 partition table、bootloader 或 model；COM20 未触碰。L3 仅证明 app 烧录/readback 与只读 CLI 响应，不证明语音、相机 JPEG 或 STM32 动作。
- **分区构建与构建入口边界**：隔离 DeskAgent worktree HEAD `0e4819e` 的 nested Ninja direct build 产出当前设备 app artifact；当前代码 HEAD 已前进至 `04cc8fb`，新增 A1 STM32 malformed NACK parser/contract regression 修复未包含于该 app、未实机验证。outer `tos.py build` 因 Windows 命令行过长失败，因此不能称标准 wrapper build 全流程成功。正确分区表 SHA-256 `C6F7A19234CB70704BDF73FBDD94EC44BBEE664444AB5FDC527CFD1BA003ADFD` 与设备一致，OTA0=`0x20000/0x600000`。标准 CLI app/partition 不匹配产物不得刷写。
- **2026-10-22 app-only flash/readback 与只读 smoke**：esptool v4.12.0 仅向 COM21 OTA0 `0x20000` 写入 1,898,288 bytes，SHA-256 `72CAF9D437CFEEA604B4AC27559EC00772E567E35019DFD3CD80F204705B40CC`，verify passed；设备 `read_flash 0x20000/0x1cf730` 回读文件 SHA 完全一致。没有 erase-all，也没有写 partition table、bootloader、model；COM20 未触碰。证据：`firmware/.target_logging/verified-firmware-20261022/flash-com21.log`、`readback-com21.log`。
- **本次 smoke 的证据边界**：当前设备 app 来源 HEAD `0e4819e`；当前代码 HEAD `04cc8fb` 中的 A1 malformed NACK parser/contract regression 修复晚于该镜像，未进入固件、未实机验证。该镜像 `body status` 返回 UART Driver State READY，但 STM32 peer/ACK UNKNOWN，未发送动作。相机 capture 三次 PTL FIFO poll 均全零、SYNC=1、最终 `rc=-25/out_len=0`，未取得 JPEG。证据：`firmware/.target_logging/verified-firmware-20261022/serial-cli-smoke.txt`。新镜像语音/唤醒现场复测待用户确认；旧 app 的用户语音确认不得外推。
- 已恢复模型：`firmware/.build/bin/srmodels.bin`，SHA-256 `7C87DD7ADB5A7623907B6354D49BEA7F9289371262E6A57F15458FB8908C5814`。
- Wi-Fi / provisioning：当前 CLI 镜像 `wifi status` 为 `LINK_UP`；provisioning stage 为 `mqtt_online`，但 `authorize_read rc=-1`。这是 L3 单项状态观测，不代表云端授权或 Tuya AI 会话已可用。
- 麦克风与语音：此前用户确认设备可唤醒对话属于旧 app；当前新镜像 SHA `72CAF9D...` 的现场唤醒与声音尚未由用户重新确认。COM21 脱敏日志未见 WakeNet/KWS/ASR/TTS，不能宣称新镜像语音通过；待现场复测。
- 相机：L3 失败。当前 CLI 镜像日志显示 camera driver READY，但该状态只表示初始化路径、I2C expander 配置以及 SPI bus/device 创建成功；代码没有 HX ID/应答探测，因此 READY 不证明传感器已就绪。capture 命中同步后，7-byte SPI header 传输返回 `ESP_OK`，读数仍全零 (`raw=00000000000000`)，随后映射为 `rc=-25`、`out_len=0`，未取得 JPEG。来源：`firmware/components/desk_hardware/src/desk_watcher_camera.c:135-193,264-346`。现场无示波器/逻辑分析仪证据。
- 当前新镜像直接 CLI 复测：COM21 `camera capture` 的三次 PTL FIFO poll 均返回全零 `raw=00000000000000`、SYNC=1、sync read `rc=0`，最后 `rc=-25`、`valid_frame=NO`、`out_len=0`。记录：`firmware/.target_logging/verified-firmware-20261022/serial-cli-smoke.txt`。没有 JPEG；不是 CRC 失败。
- CR-006 Host MJPEG：相机 service、浏览器 UI 与 `/api/camera/stream` 路由交叉测试为 L2（相机/Host 定向测试 43 passed、9 subtests passed）。PR #13 合入后修正了 3 项陈旧测试预期：成功路径注入真实帧源，同时保留无帧源时返回 unavailable、不输出帧的行为。实现只转发真实硬件 JPEG，最高 2 FPS，不合成或复用旧帧。当前设备最新真实 capture 仍为全零 PTL header、`rc=-25`、`out_len=0`，无 JPEG，因此相机 L3 仍失败，L4 未验证；这些定向测试仅是 L2 软件证据，不能称全契约实机通过。
- STM32 / body UART：本轮真实发送小幅命令 `body move 91 120 500`，返回 `rc=-3` (`DESK_HW_ERR_TIMEOUT`)，CLI `accepted=false`。随后 `body status` 显示 UART READY、cached setpoint 仍为 90/120、last formatted TX length=23 bytes。该 TX 长度只是发送帧记录，不是 STM32 RX/ACK；没有实物动作确认，动作未通过。日志：`.target_logging/com21-small-motion-test-20261022.log`。
- 当前代码/固件差异：HEAD `04cc8fb` 新增 STM32 malformed NACK parser 与 contract regression 修复；当前设备 app 由前一 HEAD `0e4819e` 构建，因此此修复不在已刷镜像中，尚无该修复的实机验证证据。
- 2026-10-22 后续只读状态诊断：COM21 上的 `body status` 尝试读到 ESP32 ROM `DOWNLOAD(USB/UART0)`，未出现应用 CLI 状态，也没有任何 STM32 响应/状态证据；因此不能将其记录为 STM32 状态。之后执行非写入 `esptool chip_id` 并指定 `--after hard_reset`，应用重新启动。随后命令 `python tools/verification/capture_esp32s3_logs.py --capture --no-raw --port COM21 --baud 115200 --output-dir .target_logging/esp32s3-live-20261022` 只读采集 33.7 秒：820 raw bytes、解析 8 行，包含 transport connected 与 4 条 heap/PSRAM 事件；原始字节仅在内存中处理，未写入文件。该记录为 ESP32-S3 应用 UART/内存的 L3 单项观测。没有发送运动命令；STM32 当前状态仍未知；COM20 未触碰。
- 应用恢复后，COM21 直接只读执行 `body status`，DTR/RTS 保持低电平。脱敏记录：`.target_logging/stm32-status-readonly/body-status-DTR-low-20261022.txt`。应用 CLI 返回 UART Driver State `READY`；cached setpoint Pan=90/Tilt=120 只是缓存值，不是物理反馈；last formatted TX length=0 bytes，不是 STM32 RX/ACK。该结果证明应用 CLI 有响应，但 STM32 peer/ACK 仍 UNKNOWN，物理状态未知。没有发送 move/stop。
- HA：当前仅 L2 connector 测试；没有用户现有 HA 会话或物理设备动作证据。
- 提醒 / 备忘（A3）：`LifeService` 30 tests passed。隔离服务生命周期 L3 smoke 使用三个独立 Python 子进程和临时 SQLite：create overdue-within-24h reminder + note → claim dispatching → process exit → restart 恢复唯一 pending、读取 note、进入 ringing → stop → restart 后确认 pending=0、status=`ringing_stopped`、occurrence=`dismissed`。该实验验证独立进程退出/重启间的 SQLite 持久化与状态恢复；不代表 Host 部署、设备播报/用户确认、离线补播策略或整机 L4。
- SDK bridge：5 项 source-contract 检查通过；依赖本机 GCC 的 10 项用例被 skipped，没有执行结果，不等于构建或官方 SDK 互通通过。
- 输入/UI：9 项软件 contract 测试；滚轮和按键已有用户现场确认，详细输入事件日志待归档。
- Codex Companion：既有 app-server 默认模型调用及隔离工作区修改任务有本机真实服务证据。`6255657` 新增 Host managed SDK session 的 terminal summary→`ctrl.expression.set` 结果回传、outbox 幂等/退避、UTF-8 63-byte 截断；combined 定向回归为 166 passed、6 subtests passed（L2）。测试 ACK 只代表 `accepted`。`WATCHER_APP_*` real managed Runtime 尚未配置/启动，当前设备显示/播报及用户听感没有 L3 证据。
- 官方 Python SDK 互通：此前 C client↔pinned Runtime loopback 21 passed 与最新 Runtime+C handler + localhost `ExternalWebSocketServer` suite 15 passed、2 subtests 是独立 L2 运行轮次，数字不应覆盖或合并；A0 CR-010 的双向音频 WSPK capability correction 仍进行中，真实 Runtime 真机发现、配对、HardwareHello、能力注册及命令/媒体闭环未验。
- ESP32-S3 日志：A7 logger 7 项测试通过（L2）。本轮命令 `python tools/verification/capture_esp32s3_logs.py --capture --port COM21 --baud 115200 --output-dir .target_logging/esp32s3-live-20261022` 只读占用 COM21 约 4.85 秒，原始采集 205 bytes、解析 2 行：transport connected、free heap 22,703 / PSRAM 7,074,164 bytes；offline replay exit 0 并复现事件。证据目录 `.target_logging/esp32s3-live-20261022/`。这是两条设备日志的 L3 单项观测，不覆盖其他模块，也不代表长稳通过。
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

当前集成回归记录：PR #13 变更合入后，WSL Python 3.12 全量套件 **550 passed、2 skipped、44 subtests passed，140.36 秒**；相机/Host 定向套件 **43 passed、9 subtests passed**。这两项均为 L2 软件证据。相机定向测试的成功路径使用注入帧源；无帧源仍须保持 unavailable，且不生成合成帧。设备侧仍未取到 JPEG。

## 3. 语音与输入复测（COM21 单人占用）

当前进展：用户已确认设备可唤醒并对话；此项记为用户现场确认，不能替代下述日志门槛，也不代表重复唤醒、多轮上下文、ASR/TTS 细节或恢复能力已验收。

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

已完成的隔离服务生命周期 L3 smoke：`LifeService` 30 tests passed；三个独立 Python 子进程 + 临时 SQLite 完成 create overdue-within-24h reminder + note → claim dispatching → process exit → restart 恢复唯一 pending/read note/ringing → stop → restart 后验证 pending=0、`ringing_stopped`、occurrence `dismissed`。此结果不覆盖真实 Host 部署、设备播报/确认或整机 L4。

1. 在目标时区创建一个短时提醒和一条备忘，记录 Host 数据库路径、创建时刻和 ID（不记录敏感内容）。
2. 重启 Host 服务，确认记录仍在；等待到期，确认 outbox occurrence 只产生一次并经真实设备显示/播报。
3. 用户确认提醒后检查状态和 outbox，确认同一 occurrence 不再播报；另测 snooze 后按规则再次触发。
4. 在隔离测试提醒上让设备离线跨过到期时刻，再恢复连接，确认系统按产品规则补播或明确标记错过；记录规则，不以“测试通过”替产品定义行为。

通过证据：DB 状态前后快照（脱敏）、Host 投递日志、设备播放/显示及用户确认、无重复播报；软件定时器测试仍仅为 L2。

## 8. Companion / Codex（L3 单服务与 L4 设备闭环）

当前代码已包含 `6255657` 的 managed-session terminal summary→`ctrl.expression.set` 发送逻辑以及 outbox 幂等、退避、UTF-8 63-byte 截断；combined 定向回归记录为 166 passed、6 subtests passed（L2）。这不表示真实 `WATCHER_APP_*` Runtime 已连接，测试 ACK 仅能说明 `accepted`，不能说明设备实际播报或用户听到。完成 L3 前须先配置并启动真实 Runtime，再在设备现场确认显示/声音。

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
