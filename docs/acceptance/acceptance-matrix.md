# Desk Agent R1 全系统验收矩阵与证据清单 (Acceptance Matrix)

> 文档版本：V3.22.0
> 责任 Agent：A0 架构与协议（基线与证据校准） / A7 系统集成与交付验证
> 参照标准：`AGENTS.md` 架构治理规范、实施计划关口目标与 PR #2 审查意见
> 状态：**2026-10-22 最新 CLI 镜像和当前全仓测试结果见 §0。用户最新现场确认当前设备已可通过唤醒词唤醒并进行对话；该证据未附设备日志，不能据此确认 ASR/TTS 细节、多轮会话、云端会话状态或恢复能力。相机仍未取到 JPEG，STM32 move/stop 均超时且没有 ACK/物理反馈，动作状态未知。Level 3 仅按单项记录，Level 4 整机闭环仍未完成。**
> V3.22.0 变更：记录 HA polling subscriber callback isolation 回归，当前 HA connector 定向测试为 37 passed、4 subtests passed；仍为 L2 软件证据，无真实 HA 会话或物理设备动作。保留 V3.21.0 语音现场确认及其日志证据边界。
> V3.13.0 变更：更新 HA、Reminder/Core、Companion、SDK 与定向回归测试数字；记录提醒 fixture 使用动态近时值且符合 24h catch-up 窗口、SQLite 测试 teardown 显式关闭连接；校准只读状态页/旋钮按钮 UI 契约、无真实帧源时不产生合成相机帧，以及 OS RNG 字符串形状 smoke test 的证据边界。全仓 pytest 为 336 passed、193 errors、1 failed、15 subtests；errors 与 failed 均由 Windows 缺少 `gcc`、native C harness 无法启动导致。相机和 STM32 的 L3 阻塞仍未解除。
> V3.14.0 变更：Companion 增至 53 项 L2 测试，覆盖 app-server `thread/resume` 重启时恢复非终态任务、确认 `cancelling` 终态及 active-writer 下 pending 重试；新代码/测试无真实默认模型及 throwaway workspace restart smoke 证据，设备回传 L4 仍未验证。
> V3.15.0 变更：补录真实 Codex app-server 默认模型（未传 `model`）与 throwaway workspace 任务在 8.76 秒内完成、Companion 重建后本地 completed 状态/assistant 摘要恢复且未含原 prompt。由于重启时 DB 已为终态，只发生 initialize、未触发 `thread/resume`；此为任务执行和本地持久化 L3 smoke，不是自动 resume 的 L3 证据。自动 resume 仍只有 53 项 L2 测试覆盖，设备反馈 L4 未验。
> V3.16.0 变更：记录本轮生活服务提醒 outbox 取消/认领/重试的事务修复及 54 项定向测试、Host 相机注入源的 L2 测试边界、SDK C 测试桩链接真实 WebSocket 源与官方 SDK camera lease 序列修正；全仓 Linux/WSL Python 3.12 测试为 539 passed、2 skipped、44 subtests passed。两项跳过分别为未设置 `WATCHEROBOT_SDK_SRC` 的官方适配器集成和 Windows SQLite unlink 特例。上述均属 L2；未取到真机 JPEG，STM32 仍无 ACK，官方 Runtime 真机配对、HA 实控、设备结果反馈及恢复验收仍未完成。
> V3.17.0 变更：补录 A1 相机 SPI 最大传输长度与 PTL body 上限一致的修复（相机错误契约 5 项通过；真实全零 PTL 头根因和 JPEG 仍未解决）；A2 以 C bridge 生成 PairAccept/HardwareHello 驱动 pinned 官方 Runtime loopback server 并确认 sys.ack（A2 套件 89 passed、21 subtests；依赖 13.1 与 pinned SDK 声明的 14–15 不一致）；A4 增加 HA WebSocket 暂时不可用后的重连测试（36 passed、4 subtests）；A5 真实 app-server 发现 turn/interrupt no-active-turn 竞态并补权威 resume 恢复尝试（Companion 54 passed，真实默认模型/重启恢复 L3）。设置本地官方 SDK 源后完整 Linux/WSL Python 3.12 回归为 544 passed、1 skipped、44 subtests passed；唯一 skip 是 Windows SQLite unlink 特例。以上不代表设备 WebSocket、相机 JPEG、STM32 ACK/动作、HA 实控或设备端反馈通过。
> V3.18.0 变更：A0 对 CR-007 增补只读 STM32 源码对账，确认 Watcher `desk_watcher_motion.c` 与 `desk_hardware.c` 存在两套行为不同的 ACK parser，且角度拒绝/clamp 语义依入口而异；CR-007 仍 OPEN，当前动作状态未确认，未发动作命令。app-only ESP32-S3 构建在临时激活已有 ESP-IDF 5.4 后到达 CMake 依赖解析，但因 `desk_hardware` 的 `esp_codec_dev` component 未解析而退出，未编译/刷机；需补齐兼容组件依赖后再构建。
> V3.19.0 变更：A0 已为 `desk_hardware` 显式 pin IDF 5.4 下 `espressif/esp_codec_dev` 1.3.6；component resolver 现可解析该依赖，但后续 CMake 在缺少 Tuya 注入的 `tal_cli` component 时停止，仍未完成固件编译/刷机。A7 为只读 UART 工具增加 `--no-raw`，避免原始日志落盘；COM21 本次 L3 采集约 9.6 秒收到 205 bytes、解析 2 行（transport connected、memory event），无断线/重连；Ctrl-C 中断，未独立读取固件 hash。日志工具定向 8 项通过。全仓 Linux/WSL Python 3.12 回归为 545 passed、1 skipped、44 subtests passed；唯一 skip 是 Windows SQLite unlink 特例。A2 loopback 仍因本地可用 `websockets` 仅 13.1（官方 pinned SDK 声明 14–15，14/15 包获取失败）而未在声明依赖版本上验证。
> V3.20.0 变更：改用官方 `tuyaopen-cli firmware build` app-only 入口后 ESP32-S3 固件编译成功，产物 app 1,899,680 bytes、SHA256 `D0172BA00241BD007AB32A66AF157D27E6059E9BA9E1401BBD6F53FAF6C42502`。为防错刷，通过 COM21 esptool 只读读取设备分区表；设备 hash `C6F7A19234CB70704BDF73FBDD94EC44BBEE664444AB5FDC527CFD1BA003ADFD`，设备 OTA0=`0x20000/6M`、OTA1=`0x620000/6M`；新构建表 hash `DE7EA934A231F9D21EBAB6E28642D00D31145F722E4A921F11BA9BF094898F8D`，新 app 表起点 `0x10000`、分区约 14.4M，且分区标签不同。因此没有刷写；产物只保存在 `.target_logging/tuyaopen-build-20261022/`，原有 tracked `firmware/dist/desk_agent_firmware_1.0.0/` 已逐项恢复。编译成功是 L2，分区读取是 L3，当前镜像功能未由本次 build 验证。
> V3.12.0 变更：记录 CR-006 最终交叉测试为 52 passed、9 subtests passed，inline JS 与 diff 检查通过；复审无新增 Critical/Important。该项为 L2，真实相机 `rc=-25/out_len=0`，L3 失败、L4 未验证，不能称全契约实机通过。补充本轮 COM21 只读 ESP32-S3 日志采集的两条事件及证据范围。
> V3.9.0 变更：记录 `6255657` Host managed SDK session 的 Codex terminal summary→`ctrl.expression.set` 软件链路、outbox 幂等/退避与 UTF-8 截断行为及定向回归结果；明确该证据为 L2，未启动真实 `WATCHER_APP_*` Runtime，也没有当前设备/声音 L3 证据。
> V3.8.0 变更：说明 camera `READY` 不代表 HX 传感器就绪；记录同步命中后 SPI header read 返回 `ESP_OK` 但七字节全零并映射为 `rc=-25`；列出同一次 capture 的下一步同步示波测量点，并标记现场尚无波形证据。
> V3.7.0 变更：更新 2026-10-22 CLI 镜像 app-only COM21 烧录 SHA/校验与最新真机状态；明确 Wi-Fi、麦克风为单项观测，MQTT 授权读取失败，相机返回全零 PTL、STM32 无接收或动作证据。
> V3.6.0 变更：依据 2026-10-22 最新构建、设备刷写与串口日志，更新相机 `rc=-25`/7-byte PTL 全零失败，SDK fake parser 与真实 Runtime 边界、A5 本机真实 Codex 任务、A3 reminder teardown 修复、A4/STM32 状态及 A7 串口日志工具证据；所有结论按 L1-L4 分层，不将软件或单项实测扩写为整机通过。
> V3.5.0 历史变更：曾记录 `verify_all.sh` 在 Step 2 失败以及 Reminder E2E teardown `WinError 32`；提醒清理问题已由 V3.6.0 的显式关闭修复。该历史快照不是当前全量回归结果。
> V3.4.0 变更：加入 2026-10-21 真实硬件状态与按功能的证据级别；新增现场 Runbook，列出相机失败定位证据、逐项后续操作、通过证据及停止条件。旧版用例数为历史快照，不作为当前全量回归声明。
> V3.3.0 变更：新增 A7 **真实固件跨层链路**证据 `tests/e2e/test_real_firmware_motion_fault_chain.py`（在真实编译 C 驱动中注入故障，经真实 C 协议桥接 → 真实 `RobotAdapter` → 真实 `DeskAgentHostApp`，断言**最外层响应信封**；并显式区分 direct C / real C protocol / Python fake-device / 全链路四类证据）；修复 `tests/e2e/test_g1_official_sdk_and_tuya_interop.py` 的 TCP 分帧假设缺陷（新增 `_recv_line()` 跨 `recv()` 累积读取助手，替换三处单次 `recv()` 读整行）；`verify_all.sh` 阶段 5 纳入跨层链路；按实测校准全量用例数为 295 项、并按套件逐一校准（firmware_contract 30 / sdk_contract 73 / camera 9 / companion 40 / connectors 26 / core 61 / e2e 47 / 顶层 9）。  
> V3.2.0 变更：新增 A7 跨模块 P1 回归门禁 `tests/e2e/test_p1_regression_gates.py`（PR #11 整改：驱动故障绝不伪成功、无 ACK 超时绝不报完成、乱序并发 ACK 不串线、超时后迟到 ACK 不污染新请求、真实 socket 流分帧、配对 fail-closed、Companion 局域网启动门禁、既有链路无回归）；`verify_all.sh` 由 7 阶段扩展为 8 阶段并将回归门禁纳入总控执行；校准全量用例数为实测 268 项（并发安全整改后复测）

---

## 0. 当前现场验收状态（2026-10-22）

本节优先于文档中早期的“设备未接入/所有硬件待测”快照。只记录本轮已提供的实机日志和用户现场确认，不将软件测试扩写成硬件通过。

| 功能 | 当前证据层级与状态 | 已有证据 | 缺少的验收证据 / 下一步 |
| :--- | :--- | :--- | :--- |
| 固件 build / flash | **L2 build 通过；L3 app-only 烧录及 hash verify 通过** | **当前检查的是本轮新刷入的 CLI app 镜像**：构建成功，大小 1,899,680 bytes，SHA-256 `C09E84798654C8071875D04901213B8979BF2032F1B14F921CF911A6F857BE65`；COM21 写入 OTA0 `0x20000` 并 verify hash。实机分区布局与构建生成的 partition table 不一致，故未写 partition table、bootloader 或 model；COM20 未触碰 | 保留本次构建/烧录日志与实机分区读回记录；排除分区表差异后再考虑任何分区级变更。build/flash 通过不代表各功能通过 |
| Wi-Fi / provisioning | **L3 单项状态观测；云端授权未通过** | 新 CLI 镜像启动后 `wifi status` 为 `LINK_UP`；provisioning 阶段报 `mqtt_online`，但 `authorize_read rc=-1` | 需定位授权读取失败并核对真实 MQTT/AI 会话；`LINK_UP` 或 `mqtt_online` 单独不代表 Tuya 会话/语音云服务闭环 |
| 屏幕、基础提示音 | **L3 单项通过** | 用户现场确认屏幕显示文字、扬声器提示音可听 | 归档对应固件 SHA、完整启动日志和现场记录；尚未验证长稳、并发资源仲裁 |
| 麦克风采集 | **L3 单项通过（当前镜像观测）** | 新 CLI 镜像日志显示麦克风 READY、`running`，累计 895 frames，errors=0，peak=95 | 这是 PCM 采集状态，不等于唤醒/ASR/语音对话已在本镜像复测；需再次现场唤醒及多轮问答并归档识别、会话、播报证据 |
| 麦克风、唤醒与对话 | **L3 单项用户现场确认；详细链路未由日志验证** | 用户于 2026-10-22 最新确认“现在可以唤醒对话了”，即当前设备能响应唤醒词并进行对话。该确认没有附带本轮设备日志或固件 SHA；此前已有 PCM 采集观测，但不能把它当作本次对话的日志佐证 | 归档匹配当前固件 SHA 的启动、WakeNet/KWS、ASR、Tuya AI session/MQTT、NLG/TTS 与扬声器播放日志；量化重复唤醒并验证多轮上下文、断网恢复和长稳。当前用户确认不代表这些细项已通过 |
| 滚轮、按键与状态页 | **L2 源码契约检查；L3 仅有用户对滚轮/按键可用的确认** | UI 契约测试检查滚轮选择 HOME/AUDIO/CONNECTION/DEVICE 四个只读页、触摸打开/返回、短按/长按调用既有 TuyaOpen talk alert API；覆盖配对 overlay 时保留当前状态页显示。用户确认滚轮和按键可用，但无本轮逐项事件日志 | 归档真机滚轮、触摸、短按、长按输入事件和屏幕变化；源码契约测试不代表页面在当前镜像上的显示/交互已实机验收 |
| Himax 相机 | **L3 失败（当前 CLI 镜像观测）** | 最新 COM21 capture 日志显示 driver `READY`，但该状态仅证明初始化路径、I2C expander 配置及 SPI bus/device 创建成功；没有 HX ID/应答探测，不证明传感器就绪。capture 命中同步后，7-byte SPI header transfer 返回 `ESP_OK`，采样数据仍全零 (`raw=00000000000000`)，随后返回 `rc=-25`、`out_len=0`；未取得 JPEG。详见 `desk_watcher_camera.c:135-193,264-346`。现场尚无示波器/逻辑分析仪波形证据 | 下一次 capture 必须同步测量同一事务中的 CS GPIO21、SCLK GPIO4、MISO GPIO6、MOSI GPIO5、PCA SYNC bit 6 与 HX 电源轨；保存探点、时基和电压。确认总线数据前不盲目重复取帧；修复后导出并打开现场 JPEG，验证连续帧与资源释放 |
| 相机 SDK parser / Host adapter | **L2 fake 集成通过；L3/L4 未验证** | 测试调用固定版本官方 SDK 的 WSPK parser 和图片回调，以有效 JPEG fixture 验证适配路径；A3 Host adapter 代码/测试使用 fake managed context。`FrameCapturePipeline` 无 capture source 时必须不发出帧，即使传入 `allow_synthetic=True` 也忽略该兼容参数；该断言防止伪造 JPEG，不证明相机取帧成功。未连接官方 Runtime，也未从设备得到真实 JPEG | 真实 Runtime 完成发现/配对/会话后，由设备媒体帧经官方 parser、managed Application、Host camera service 到浏览器验证新鲜图；当前无此闭环 |
| CR-006 Host MJPEG stream | **L2 测试通过；最终复审无新增 Critical/Important；L3 失败、L4 未验证** | `tests/camera`、Host route、SDK camera adapter 与 managed SDK startup 交叉测试：52 passed、9 subtests passed；inline JS `node --check -`、`git diff --check` 通过。实现只消费真实硬件 JPEG，最高 2 FPS | 最新设备 capture 为 `rc=-25`、`out_len=0`，无真实 JPEG。以上仅为 L2 软件证据；需取得可打开的现场 JPEG，经 Host 到浏览器验证帧新鲜度与断线恢复，才能验收实机链路。不能称全契约实机通过 |
| STM32 / 机身动作 | **L3 命令发送尝试失败；当前动作状态未知，安全闭环未验** | 本轮 `move` 与 `stop` 均返回 `rc=-3` (`DESK_HW_ERR_TIMEOUT`)；没有 RX、ACK 或物理反馈，故 stop 是否执行无法确认，当前动作状态未知。只读源码诊断显示 UART `READY` 只代表 UART 初始化，不探测 STM32；23-byte last frame 是格式化帧缓存，不是 TX/RX 计数。驱动 UART write 完整后等待 ACK，最多 3×500ms，但未记录 RX 字节、坏 CRC、错序或 NACK 分类。COM21 已释放，本轮没有后续动作 | 先核对板卡原理图、STM32 固件协议及内部 UART 接线，再评估增加 RX 诊断；随后用可观察反馈验证 ACK 关联、物理动作和通信故障安全停止 |
| 官方 Python SDK | **L1 契约；L2 parser 测试通过；真实 Runtime L3 未验证** | 官方 SDK parser 对有效 fixture 的互操作测试通过；设备侧 pairing window 命令曾现场打开/取消。官方 Runtime 的真实 WebSocket 发现、配对、HardwareHello、能力注册、命令与媒体传输均未验 | 用未修改官方 SDK 连真实设备；错误配对码必须拒绝，正确配对后记录完整连接/命令/媒体/断线重连日志 |
| Home Assistant | **L2 软件测试；L3/L4 未验证** | HA connector 定向套件 **37 passed、4 subtests passed**，包含 polling subscriber callback isolation 回归；仅证明 connector 软件行为，没有证据表明已连接泰山派上的用户 HA 或执行物理设备 | 用真实 HA 和低风险设备验证发现、白名单服务调用、事件/状态订阅；分别记录 accepted、state changed、physical action |
| 提醒 / Host 服务 | **L2 软件测试通过；L3/L4 未验证** | 最新提醒/Core 定向套件报告 **61 passed、4 subtests passed**。两条 E2E due fixture 改为运行时近时值：crash recovery 用 90 分钟前（符合 24h catch-up 且超过 1h 延迟断言），delivery 用 5 分钟前（符合 catch-up 标记阈值及 24h 上限）；real-chain 提醒 fixture 也用运行时 5 分钟前。`test_p1_gaps_verification.py` 在清理临时目录前显式 `LifeService.close()`，Windows WinError 32 已消除。均为软件测试，不代表真机播报 | 在真实 Host/设备创建提醒，跨服务重启观察到期、播报、确认去重；另测强制进程终止后的 WAL 恢复、时区与离线错过策略 |
| Windows Companion / Codex | **真实 app-server 任务与本地持久化 L3 smoke 通过；自动 resume 仍为 L2；设备回传 L4 未完成** | Companion 定向套件 **53 passed（L2）**，覆盖 app-server `thread/resume` 重启时恢复非终态任务、对 `cancelling` 任务确认终态、active-writer 情况下 pending 重试。另以真实 Codex app-server 默认模型（未传 `model`）在 throwaway workspace 执行任务，8.76 秒内得到 `codex.turn_completed`；重建 Companion 后 completed 状态与 assistant 摘要一致，恢复内容不含原 prompt。重启时 DB 已是终态，只发送 initialize、没有 `thread/resume`，因此该 smoke 不证明自动 resume。`6255657` 的 terminal summary→`ctrl.expression.set` 软件路径和 outbox 逻辑仍未由真实设备 Runtime 验证，未验证设备显示/播报或用户听感 | 真实非终态任务进程重启后的 `thread/resume` 仍需单独 L3 验证；启动真实 managed Runtime 并连接设备，观察任务结果到设备反馈的完整链路并由用户确认听感 |
| ESP32-S3 串口日志读取 | **L2 工具测试通过；L3 只读日志观测** | A7 logger 7 项测试通过。本轮命令 `python tools/verification/capture_esp32s3_logs.py --capture --port COM21 --baud 115200 --output-dir .target_logging/esp32s3-live-20261022` 只读占用 COM21 约 4.85 秒，捕获 205 raw bytes、解析 2 行：transport connected、free heap 22,703 / PSRAM 7,074,164 bytes；offline replay exit 0 并复现事件。证据目录 `.target_logging/esp32s3-live-20261022/` | 仅覆盖这两条日志；不代表其他模块状态、语音/相机/STM32功能或长稳通过 |
| 整机闭环 / 恢复 | **L4 未完成** | 各能力证据分散；语音、屏幕、Codex 本机任务等是各自单项证据 | 完成语音→任务路由→真实服务/设备动作→真实结果→设备反馈，并验断网、服务退出、设备重启与重连；见 Runbook |

### 2026-10-21 回归数字与构建状态（历史快照）

| 范围 | 本轮数字/结果 | 证据边界 |
| :--- | :--- | :--- |
| HA Connector | **30 项** | L2 软件测试；不证明真实 HA/实体动作 |
| 提醒与 Host daemon | **37 项合计** | L2 软件测试历史快照；早期 Reminder E2E teardown 有 `WinError 32`，现已在对应测试中显式 close `LifeService` 后再清理目录。最新定向数字见 §0 |
| SDK bridge | **5 项 source-contract 检查通过；本机 GCC 依赖的 10 项用例 skipped** | 仅 source-contract L2；跳过用例没有运行结果，不证明编译或官方 SDK 互通 |
| 输入/UI | **9 项** | L2 输入/UI contract；用户确认滚轮和按键可用，现场事件日志待归档 |
| STM32 raw UART | **2 项静态检查** | 有一次交叉构建成功并生成镜像，但该镜像未烧录；没有 STM32 接收 ACK 或实物动作证据 |
| A6 Host 相机页 | **13 项通过** | L2 Host/Web 测试；设备仍未产生可打开的真实 JPEG，相机能力 L3 仍失败 |
| `tools/verification/verify_all.sh` | **本轮在 Step 2 失败** | 后续 Step 3–8 未运行；不得写成八阶段全通过，也不得沿用旧 295/412 总数作为本轮结果 |

### 本轮可复核记录

- 固件应用镜像（OTA app）SHA-256：`A0ABD911C7DFD643B419674767E539DF0057CB15F5E09C1280F343BABCB375E3`，文件位于 `firmware/.target_logging/p0-2-camera-guards-20261021/desk_agent_firmware.bin`。
- 恢复后的 SR 模型文件 SHA-256：`7C87DD7ADB5A7623907B6354D49BEA7F9289371262E6A57F15458FB8908C5814`。
- 唤醒/对话证据：`.target_logging/com21-wake-after-fix-20261021.log`、`.target_logging/com21-wake-after-model-restore-20261021.log`。
- 最新相机复测：`.target_logging/com21-camera-after-guard-flash-20261021.log`。日志显示麦克风仍运行、PCM 错误数为 0，但相机未返回图像。
- 上述路径属于本地忽略日志归档；合入前需确认文件仍在工作站，不应假定它们会随 Git 提交或 PR 自动分发。

### 2026-10-22 camera-diagnostic 镜像记录（历史快照）

以下记录对应此前 camera-diagnostic app，不是当前已刷入的 CLI app；当前状态以 §0 和下方“CLI 镜像现场增量”记录为准。

| 工作包 | 本轮结果 | 证据层级 / 边界 |
| :--- | :--- | :--- |
| 固件 camera-diagnostic build | TuyaOpen 固件构建命令 exit code 0；归档 app 为 `.target_logging/camera-diagnostic-20261022/desk_agent_firmware.bin`，SHA-256 `BCDAFB90625F759C56FB8F69BBA1A9EF1CC2551DDDF295107DAB19FEAE5D6598` | L2 构建通过。输出 partition table 与设备实测表不同；本轮只写 app 到已核对 OTA0 `0x20000`，没有写分区表、bootloader 或 model。不可据此推断分区配置已统一 |
| 相机错误诊断 | 烧录后一次真实 capture：7-byte PTL header 全零，`rc=-25`，`out_len=0`；没有 JPEG | L3 失败。350-byte 原始采集可用于复核；具体电气根因未确认 |
| ESP32-S3 serial logger | `tests.e2e.test_esp32s3_log_capture`：7 tests passed；工具对 COM21 原始捕获 350 bytes 并解析相机错误事件 | 测试为 L2；串口采集本身为 L3 观测证据，不证明未出现的其他事件正常 |
| SDK 图像 parser | `tests.sdk_contract.test_official_sdk_firmware_wspk_interop` 与 `test_wspk_framing`：10 tests passed；使用官方 SDK parser、有效 JPEG fixture | L2 fake fixture/parser interoperability；没有设备 JPEG、WebSocket 或 Runtime 会话 |
| A3 reminder | `tests.core.test_life_service` 23 tests；crash-recovery/catchup 定向测试 1 test 通过 | L2。Windows DB teardown 修复已验证；强制 kill 的崩溃恢复及设备播报未验证 |
| A4 HA | 既有 HA connector 软件用例通过 | L2；无真实 HA 服务会话或物理动作证据 |
| STM32 | 现有静态/模拟协议证据 | L2；未观测到 STM32 接收、ACK 或实物动作 |
| A5 Codex | 真实本机 app-server 调用采用默认模型成功；隔离工作区修改/验证任务完成，Companion 重启后任务记录仍在 | 本机服务/Host L3 单项；设备触发和设备结果播报仍未完成，不能评 L4 |

上述是旧镜像阶段的增量记录，不是本分支全量回归结果；当前 CLI 镜像的现场状态记录如下。

### 2026-10-22 CLI 镜像现场增量（当前基线）

| 检查项 | 结果 | 证据层级 / 边界 |
| :--- | :--- | :--- |
| CLI 固件构建与烧录 | 构建成功；app-only bin 1,899,680 bytes，SHA-256 `C09E84798654C8071875D04901213B8979BF2032F1B14F921CF911A6F857BE65`；COM21 写入 OTA0 `0x20000` 并 verify hash。因构建 partition table 与实机读取布局不一致，未写 table/bootloader/model；COM20 未触碰 | L2 build + L3 app flash/hash verification；不是整机功能通过 |
| 网络 / provisioning | `wifi status LINK_UP`；provisioning `mqtt_online`，但 `authorize_read rc=-1` | L3 单项状态观测；云端授权与 Tuya AI 会话未验 |
| 音频输入 | 麦克风 READY/running，895 frames、errors=0、peak=95；用户最新确认当前设备可唤醒并对话 | PCM 帧数据是 L3 单项采集观测；唤醒/对话是 2026-10-22 用户现场确认，未附本轮设备日志或固件 SHA。未验证带日志的 WakeNet/KWS、ASR、Tuya 会话、TTS/扬声器细节、多轮上下文或恢复 |
| 相机 | Driver READY；PTL empty response `raw=00000000000000`，`rc=-25`，`out_len=0`，无 JPEG。READY 只表明初始化路径、expander 配置及 SPI bus/device 创建成功；未探测 HX ID/应答。同步命中后 7-byte SPI header read 返回 ESP_OK 但全零，才映射为 `rc=-25` | L3 失败；无真实图片。现场没有 scope/logic-analyzer 波形证据；下次必须在同一次 capture 同步观测 CS GPIO21、SCLK GPIO4、MISO GPIO6、MOSI GPIO5、PCA SYNC bit 6 和 HX 电源轨 |
| STM32 / body UART | 本轮 move 与 stop 均 `rc=-3`；UART READY 是初始化状态，23-byte last frame 是格式化缓存；stop 是否执行无法确认 | L3 命令尝试超时；没有 RX/ACK/物理反馈，当前动作状态未知、安全闭环未验。驱动等待 ACK 最多 3×500ms，但未提供 RX 内容/CRC/序列/NACK 分类。COM21 已释放。后续先核对原理图、固件协议和内部 UART 接线，再决定是否加入 RX 诊断 |
| Codex terminal summary 设备反馈 | `6255657` 在 Host managed SDK session 将 Codex terminal summary 编为 `ctrl.expression.set`；outbox 去重/幂等、退避和 UTF-8 63-byte 截断行为纳入实现 | combined 定向回归报告 166 passed、6 subtests passed，证据为 L2；测试中的 ACK 只代表 `accepted`。`WATCHER_APP_*` real managed Runtime 未配置/启动，无当前设备显示/声音或用户听感 L3 证据 |

本次为指定真机状态增量，不代表全量回归或端到端通过；整机 L4 仍未完成。

### 2026-10-22 定向回归补充（L2）

| 范围 | 最新定向结果 | 证据边界 |
| :--- | :--- | :--- |
| HA Connector | **37 passed、4 subtests passed** | 包含 polling subscriber callback isolation 回归；软件 connector 测试仍为 L2，没有真实 HA 会话或物理设备动作 |
| Reminder / Core | **61 passed，4 subtests passed** | 动态提醒 fixture 覆盖 24h catch-up 策略；DB teardown 先关闭 LifeService。仍是软件验证，不代表设备播报 |
| A5 Companion | **53 passed + 真实任务 smoke** | 53 项为 L2，覆盖 `thread/resume` restart recovery、`cancelling` terminal confirmation、active-writer pending retry。另有真实默认模型/throwaway workspace 任务和已完成任务本地持久化重建 smoke（L3）；该任务重启时已终态，未触发 `thread/resume`，不作为自动恢复 L3 证据，也不代表设备结果回传 |
| A2 SDK | **6 passed** | SDK 定向契约/互操作软件检查；不代表官方 SDK Runtime 已连接真机 |
| 更新后的跨模块定向回归 | **19 passed** | L2 软件集成；不升级为 Level 3/4 设备闭环 |
| 全仓 pytest | **336 passed、193 errors、1 failed、15 subtests** | 193 errors 与 1 failed 均因当前 Windows 运行环境缺少 `gcc`，native C harness 无法启动；这些结果不是功能断言失败，但全仓回归不能标为通过 |

本轮相机测试只断言未接入真实 capture source 时不输出帧；产品实现明确禁止将合成/占位 JPEG 当作硬件画面。屏幕输入/UI contract 是源码级检查：滚轮选择四个只读页，短按和长按沿用 TuyaOpen talk alert 事件；真机页面变化日志仍待归档。

`tests/test_p1_final_closure.py::test_os_rng_token_shape_smoke_check` 仅使用 Python `os.urandom(32)` 检查 OS RNG 字节可编码成 64 字符小写十六进制字符串。这是跨平台的字符串形状/熵 smoke check，**不调用固件 C token 生成实现，不构成固件 CSPRNG 验证**。

---

## 1. 证据分层标准与总体状态 (Evidence Layering)

根据 `AGENTS.md` 第 4 节定义，严格执行证据分层核验，坚决杜绝用假数据、内存状态或模拟输出夸大真实物理交付：

| 证据层级 | 验收范围与含义 | 当前结论 | 关键依据与测试输出 | 绝对不能代表什么 |
| :--- | :--- | :---: | :--- | :--- |
| **Level 1：公开源码 / 契约标准** | JSON Schema (Draft 2020-12)、25 个 fixtures、13 个稳定错误码、动态 ISO 8601 时钟契约 | **契约检查通过** | `contracts/validator/validate.py` 深度校验通过 | 不能代表物理板卡具备该 capability 或在线部署成功 |
| **Level 2：模拟 PoC / 协议对齐** | C 固件驱动报文、SDK 协议、RobotAdapter、真实 Host 进程与伪设备组成的 TCP 链路、跨模块回归、C 驱动故障跨层、Python 核心、HA/Codex 软件接口、故障注入与压力测试 | **此前快照 295 项通过；不是当前分支的全量结果** | 下方 §2 记录的是当时命令及输出；本次后续改动需重跑对应套件，必要时重跑全量 | 无法证明真机、真实 HA/SDK/Codex 业务和物理动作 |
| **Level 3：单项硬件 / 节点实测** | ESP32-S3 语音/屏幕/输入、Himax 相机、STM32 动作、Windows Companion、真实 HA 与 Host | **部分单项通过；相机失败；STM32/真实 HA 等未验证** | 见 §0 和 [现场 Runbook](field-runbook-2026-10-21.md)；未实测项仍存在 | 单项实测不能代表系统端到端或长稳 |
| **Level 4：端到端闭环与恢复验收** | 真实语音唤醒→外部工具/服务执行→真实结果→设备反馈，断网/服务退出/重启后恢复 | **未完成** | 当前缺少贯通真实服务动作、设备反馈及故障恢复的一条完整关联证据 | 不能代表所有未知硬件版本或生产环境 |

> [!IMPORTANT]
> **关于 G1 关口验收状态的严格校正（遵照 AGENTS.md 第 4 节）**：  
> `test_g1_official_sdk_and_tuya_interop.py` 属于 **Level 2（软件协议 PoC 与并发状态机验证）**，其实际执行的是 GCC 编译本项目 C 代码、ctypes 动态调用以及 Python 本地 Socket 模拟。**严禁将其宣称为实施计划定义的 G1 关口已完成**。  
> 真实的 **G1 关口通过标准** 为：**ESP32-S3 真机固件连接真实 Tuya 云端完成一轮语音对话 + 官方 WatcheRobot Runtime 接入 + 一个受控物理动作（如转头或表情）端到端闭环**。该工作包由下一阶段 A1（真实固件/驱动/Tuya云）与 A2（官方 Runtime/SDK 适配）联合实测完成。

---

## 2. 详细测试证据明细 (Level 1 & Level 2)

> 本节的固定用例数和历史命令输出来自 V3.3.0 快照；后续 A3/A4/A5/A2 等有代码变化。请按 [现场验收 Runbook](field-runbook-2026-10-21.md) 重新运行当前分支对应测试后再引用，不要把这里记录的历史数字当作当前测试结论。

### 2.1 Level 1: 契约规范与 Schema 深度校验
- **验证命令**：`python3 contracts/validator/validate.py`
- **实际检查结果**：
  ```text
  === Desk Agent R1 G0 Contract Verification (Deep Domain Checks) ===
  1. Verifying JSON Schemas...
    [OK] Schema valid: domain_codex.json, domain_ha.json, domain_life.json, domain_resource.json,
         envelope_event.json, envelope_request.json, envelope_response.json
  2. Verifying Positive Request Fixtures (8 positive fixtures checked)
  3. Verifying Negative Request Fixtures (3 negative fixtures correctly rejected)
  4. Verifying Response / Result Fixtures (8 responses verified)
  5. Verifying Event Fixtures (6 domain events verified)
  6. Verifying Error Catalog (13 stable error codes confirmed)
  7. Verifying Idempotency and Sequence Constraints (All unique)
  === ALL G0 CONTRACT & DEEP DOMAIN CHECKS PASSED (100% OK) ===
  ```

### 2.2 Level 2: 固件核心与 C 驱动原生仿真构建
- **验证命令**：
  ```bash
  /usr/bin/cmake -B firmware/build -S firmware && /usr/bin/cmake --build firmware/build
  ```
- **实际输出**：
  ```text
  -- Configuring done (0.1s)
  -- Generating done (0.1s)
  -- Build files have been written to: /home/share/samba/ePanda/DeskAgent/firmware/build
  [ 11%] Building C object CMakeFiles/desk_hardware.dir/components/desk_hardware/src/desk_hardware.c.o
  [ 22%] Linking C static library libdesk_hardware.a
  [ 33%] Built target desk_hardware
  [ 55%] Built target desk_tclaw
  [ 66%] Building C object CMakeFiles/desk_sdk_bridge.dir/components/desk_sdk_bridge/src/desk_sdk_bridge.c.o
  [ 77%] Linking C static library libdesk_sdk_bridge.a
  [ 77%] Built target desk_sdk_bridge
  [ 88%] Linking C executable desk_agent_firmware_test
  [100%] Built target desk_agent_firmware_test
  ```

### 2.3 Level 2: 全量自动化单元测试与集成测试 (295 项用例全部 PASS)
- **验证命令**：`PYTHONPATH=. python3 -m unittest discover -s tests -t . -p "test_*.py"`
- **实际执行结果**（串行复测 3 次均为同一结果）：
  ```text
  ----------------------------------------------------------------------
  Ran 295 tests in 44.118s

  OK
  ```
- **并发安全校验**：4 个全量套件并发执行 → 4/4 均为 `Ran 295 tests ... OK`，`Address already in use` 计数 0。
  > 注（已修复）：4 路并发**满载**下曾出现 `tests/e2e/test_g1_official_sdk_and_tuya_interop.py` 偶发 `JSONDecodeError`（**非** `Address already in use`）。**根因是该测试自身的 TCP 分帧假设缺陷**：固件 `desk_sdk_socket_send_line` 将 JSON 载荷与终止 `"\n"` 以**两次独立 `send()`** 发出，二者可能落在不同 TCP 段；该测试三处（旧 364/381/405 行）以**单次 `recv()` 即整行**的假设读取，当某次 `recv()` 只取到 `"\n"`（或其残留）时 `strip()` 得到空串并抛错。已在该文件内新增跨 `recv()` 累积、按 `"\n"` 切分并**前向携带余量**、带**有界超时**的 `_recv_line()` 读取助手，并替换全部三处调用（未使用 `sleep()`/异常重试/放宽超时等掩盖手段）。修复后按 3 轮 × 4 路并发复测 **12/12 OK**，`Address already in use` 计数 0。

#### 测试子模块明细表：

| 测试套件 | 用例数 | 覆盖核心能力与仿真要点 (Level 2) |
| :--- | :---: | :--- |
| **`tests/firmware_contract/`** | 30 | 1. 仲裁器 5 级优先级抢占与释放<br/>2. 相机租约锁定 TClaw 机头转动追踪<br/>3. 云台电机机械物理极限钳制 (0..180°, 100..140°)<br/>4. 屏幕表情与状态文本存取<br/>5. TClaw 状态机与音频独占生命周期<br/>6. 用户触摸/语音打断 (Interrupt/Barge-in)<br/>7. MCP 工具 RequestEnvelope 派发与动态结果接入<br/>8. 多轮对话上下文与动态流式 Token 回调<br/>9. Tuya Cloud 语音对话会话生命周期与音频流喂入仿真 |
| **`tests/sdk_contract/`** | 73 | 1. 100% 对齐官方 WSPK 小端序 `<4sBBHII` (16B) 封包解析与 14B 遗留读兼容<br/>2. UDP 37021 `watcher_lan_pairing_v1` 配对服务（CSPRNG 64B Hex Token 并强制认证）<br/>3. `sys.client.hello` 握手与 `role=hardware` Opus 音频协商<br/>4. 非阻塞 `0.0.0.0` TCP Socket 服务驱动与连接管理<br/>5. 云台动态角度入参解析与物理限位安全<br/>6. 资源仲裁忙时拒收并返回 409 `RESOURCE_BUSY`<br/>7. 相机租约互斥冲突拦截与释放<br/>8. 未知命令严格返回 404 `UNKNOWN_OPERATION`<br/>9. 触摸事件封装符合 `envelope_event.json` 标准 Schema<br/>10. ACK 超时严格返回 408 NACK 而非伪成功 `code: 0`<br/>11. **R3 TCP 持久缓冲分帧**：半包重组、粘包多消息、超长帧丢弃后重同步、断连残留缓冲清空<br/>12. **R4 配对码 fail-closed**：未配置/格式错/不匹配一律拒绝且不发 token<br/>13. **WP1b 驱动故障真实 C 协议测试（默认初始化 `strict_mode == false`）**：真实 `desk_sdk_bridge_process_command` 对驱动故障返回 `sys.nack`+`HARDWARE_FAULT`、回传 `request_id`、资源可再获取、成功 ACK 仅 `accepted`<br/>14. 官方 WatcheRobot Runtime 契约字段级对齐与机器人适配器端到端联调 |
| **`tests/camera/`** | 9 | 1. 单客户端互斥租约仲裁（并发抢占拒绝返回 `RESOURCE_BUSY`）<br/>2. 超时过期自动释放与黑帧遮盖（防旧画面冒充实时流）<br/>3. 租约释放回调触发底层资源注销<br/>4. 硬件真实传感器与合成画面源精准标注（`is_hardware` / `source` 字段标签）<br/>5. 自动续租租约延长机制<br/>6. Web 控制台 Token 鉴权（Bearer、Header、Query 三通道）及云台安全角度操作 |
| **`tests/companion/`** | 53 | 1. 生产模式缺失可执行文件真实报错（无伪成功）<br/>2. 工作区别名白名单与路径逃逸（Path Traversal）防御<br/>3. 真实 JSON-RPC stdio 客户端驱动与多线程按服务端请求 `id` 唤醒与响应<br/>4. 支持 Codex app-server 标准 `thread/start` 及 `thread/create` 映射<br/>5. 审批项目严格绑定与上下文一致性校验，消除 early-approval 竞态<br/>6. 默认监听 `127.0.0.1` 配合 Bearer Token 401 拦截<br/>7. `RemoteCodexCompanionClient` 远程 HTTP POST /rpc 代理调用及审批应答通道<br/>8. 对齐 codex-cli 0.161.0 审批枚举与 `turn/interrupt` 驼峰字段<br/>9. **R5 启动安全门禁**：非回环监听强制 ≥16 字符非占位强令牌，空/占位/短令牌一律拒绝启动（含所有启动入口统一校验路径）<br/>10. app-server `thread/resume` 重启恢复非终态任务、处理中断/cancelling 终态及 active-writer 条件下 pending 重试 |
| **`tests/connectors/`** | 26 | 1. 生产模式未配置凭据真实抛错（不伪造返回）<br/>2. 实体域与服务黑白名单多级校验（放行合规设备，拦截锁/安防/更新等敏感域）<br/>3. 两阶段状态确认机制（`accepted` 与 `state_confirmed` 解耦）<br/>4. 目标属性比对确认（亮度/色温等未达目标时 `state_confirmed=False`）<br/>5. 真实 HTTP 通信轮询与状态扭转<br/>6. WebSocket `/api/websocket` 客户端鉴权握手与状态流式订阅<br/>7. 指数退避断网重连与状态对账消除状态漂移<br/>8. 双通道容灾（WebSocket 中断时平滑降级 REST 轮询）与 HTTP 429 限流退避 |
| **`tests/core/`** | 61 | 1. SQLite WAL 事务持久化保证零丢失<br/>2. 提醒创建、推迟(Snooze)与完成状态彻底解耦<br/>3. 开机追赶与到期轮次精准筛选<br/>4. 持久化发件箱（Outbox）出入队与去重<br/>5. 便签创建、全文检索与分页<br/>6. 后台 `ReminderScheduler` 自动轮询到期 occurrence，触发 ringing 状态扭转并投递 `reminder.alert` outbox 消息<br/>7. 宿主守护进程 HTTP RPC 端到端分发（集成 Codex 路由、相机帧流抓取、默认回环绑定与 401 鉴权拦截）<br/>8. 单事务原子触发（occurrence ringing / reminder ringing / 周期排程 / outbox 入队绝不脱节）<br/>9. 时区感知 (`zoneinfo`) 跨天与跨夏令时重复计算<br/>10. **R1/R2 运动 ACK 语义与请求关联**：固件 ACK 只判 `accepted`、驱动失败→`HARDWARE_FAULT`、静默→`unknown`+`OPERATION_TIMEOUT`、乱序/迟到/无 `request_id` 的 ACK 一律丢弃、`request_id` 并发唯一<br/>11. **A3 Python 伪设备故障信封测试 (`test_hardware_fault_envelope.py`)**：给定伪设备上报硬件故障时，Host 最外层信封正确失败（纯 Python 伪设备，**不能**证明真实固件会传播驱动错误） |
| **`tests/e2e/`** | 47 | 1. 跨模块信封路由、幂等性去重与过期毫秒级拦截<br/>2. HA 领域指令执行与观察状态一致性闭环<br/>3. 生活服务持久化与服务重启恢复<br/>4. Codex 任务提交流程与敏感操作审批闭环<br/>5. 相机租约互斥与帧时效性检测<br/>6. 固件发起到 Host 路由与提醒后台到期投递全链路闭环<br/>7. **G1 软件协议仿真 (Level 2 PoC)**：`test_g1_official_sdk_and_tuya_interop.py`<br/>8. **A7 E2E 故障注入与网络自愈 (`test_e2e_fault_injection.py`)**：HA WebSocket 突发断连重连与状态对账自愈、进程异常退出与 SQLite WAL 崩溃恢复（到期提醒自动打上 `is_catchup=True` 追赶标记）、10 个高并发客户端争夺相机租约的绝对互斥仲裁、Router 高并发重复请求幂等缓存去重<br/>9. **A7 全系统长期稳定性压测 (`test_system_stability_stress.py`)**：200 轮跨域高频密集连续操作（50 提醒 + 50 HA 两阶段 + 50 Codex 任务/审批 + 50 相机拉流/续租），执行后校验 SQLite `PRAGMA integrity_check` 与 `foreign_key_check` 零损坏零外键违背<br/>10. **A7 真实链路端到端 (`test_real_chain_tclaw_to_host.py`)**：起真实 `DeskAgentHostApp`（真实 RobotAdapter / Router / LifeService / 后台调度器与发件箱 Worker），对端以忠实伪固件复现 `desk_sdk_bridge` TCP 服务与 TClaw 对话引擎，严格按 C 固件线格式收发。覆盖：工具请求经 TCP 贯通 Host 路由并在 SQLite 真实落库、固件 ACK 仅判 `accepted`（`state_confirmed=False`）、**固件扣发 ACK 时超时判定为 `unknown`+`OPERATION_TIMEOUT` 且绝不伪报成功**、到期提醒经调度器/发件箱回推 `ctrl.expression.play` 播报且 outbox 行为 `dispatched`（非退避）、未知操作与过期信封错误原路回传、脏数据不破坏链路、链路断开后自动重连并恢复链路<br/>11. **A7 跨模块 P1 回归门禁 (`test_p1_regression_gates.py`)**：见 §2.6<br/>12. **A7 真实固件跨层故障链路 (`test_real_firmware_motion_fault_chain.py`)**：见 §2.7 |
| **`tests/test_p1_gaps_verification.py`** | 4 | 1. 泰山派远程调用 Windows Codex Companion 客户端分发与审批确认<br/>2. 提醒发件箱后台消费 Worker 投递与重试机制<br/>3. HA 实体与服务白名单拦截、状态对账与状态变更订阅回调<br/>4. 无真实 capture source 时 FrameCapturePipeline 不输出合成/占位帧；不验证真实相机取帧 |
| **`tests/test_p1_final_closure.py`** | 5 | 1. Python OS RNG 字节的十六进制字符串形状 smoke check（不验证固件 C token 生成）<br/>2. RobotAdapter TCP/UDP 握手、舵机运动控制与到期提醒通道投递<br/>3. Host 路由器 Codex 待审批查询（`codex.get_pending_approvals` / `codex.list_approvals`）<br/>4. Host 路由器 HA 状态迁移事件查询（`ha.list_changes`）<br/>5. 相机硬件传感器帧源接入与 `hardware_sensor` 标签识别 |

### 2.4 验收脚本范围及本轮状态 (`tools/verification/verify_all.sh`)
为落实 `AGENTS.md` 规范第 4 节，提供自动化脚本对 Level 1 ~ Level 4 进行统一检查：
```bash
./tools/verification/verify_all.sh
```
- 脚本依序运行契约检查、本机 C 构建、Python 测试、真实 Host+伪设备链路、跨模块回归、部署预检和状态矩阵静态检查。它不连接硬件或真实外部服务。
- **本轮最近一次执行在 Step 2（C 固件原生 HAL / SDK Bridge 构建）失败并退出。Step 3–8 未运行，因此本轮没有全套结果。**
- 先前版本记录的 295 项或其他全量数均为历史快照，不作为当前分支的通过结果。2026-10-21 的分项数字见 §0 的“历史快照”表；2026-10-22 指定增量证据见同节“增量证据与复核边界”。
- 脚本成功结束也只能表示其配置的自动化门禁通过；L3 按单项证据记录，L4 仍需真实端到端与恢复验收。

### 2.5 A7 真实链路端到端套件证据明细 (`tests/e2e/test_real_chain_tclaw_to_host.py`)

- **验证命令**：`PYTHONPATH=. python3 -m unittest tests.e2e.test_real_chain_tclaw_to_host`
- **实际执行结果**：
  ```text
  ----------------------------------------------------------------------
  Ran 10 tests in 3.648s

  OK
  ```
- **链路构造（真实组件，非 Mock 旁路）**：本套件实例化**真实** `DeskAgentHostApp`，其中包含真实 `RobotAdapter`（真实 TCP 客户端与非阻塞全双工收发线程）、真实 `Router`、真实 `LifeService`（真实 SQLite WAL 落库）、真实后台 `ReminderScheduler` 与发件箱投递 Worker；对端为忠实伪固件 `FakeFirmwareTClaw`，按 C 固件线格式复现 `desk_sdk_bridge` TCP Socket 服务与 TClaw 对话引擎（`sys.client.hello` 握手、裸 RequestEnvelope + `\n` 下行、`tool.response` 上行并回 `sys.ack`）。
- **覆盖的真实链路断言**：
  1. **贯通与真实落库**：设备下发 `reminder.create` → TCP → RobotAdapter → Host Router → `LifeService` 真实写入 SQLite；回传信封携带真实 `reminder_id`，并二次查库确认非回显桩；
  2. **硬件通道确认**：`resource.motion.move` 经 RobotAdapter 下发 `ctrl.motion.move` 至固件，固件回 ACK 仅判定 `status=accepted` / `accepted=True` / `state_confirmed=False` 且 `hardware_link=connected`（ACK 语义为"已受理"，绝非"已完成"，interfaces.md §6.1）；
  3. **防伪造 ACK**：固件**扣发** ACK 时，Host 必须返回 `status=unknown` + `error.code=OPERATION_TIMEOUT`（`retryable=true`）且**不含 `result` 字段**，绝不得报 `succeeded`/`completed`（该用例真实等待 2s 超时窗口后判定，非空跑）；
  4. **主动播报回推**：到期提醒由真实调度器原子扭转 ringing 并入队 outbox，经投递 Worker 以 `ctrl.expression.play` 回推至设备播报；断言 outbox 行为 `state=dispatched`、`retry_count=0`、`dispatched_at` 非空——即**投递成功**而非退避（仅断言"无就绪 pending"会因退避而空过）；
  5. **错误原路回传**：未知操作返回 `failed` 信封、过期信封返回 `error.code=EXPIRED` 且绝不落库；
  6. **脏数据与断链韧性**：非 JSON 脏行不破坏链路；设备侧 TCP 突断后适配器自动重新握手并恢复链路。
- **⚠️ 证据层级声明**：以上全部为 **Level 2（软件协议与模拟 PoC）**。伪固件在**本机进程内**以 Python 复现固件线格式，**并未**运行 ESP32-S3 固件、未连接 Tuya 云端、未接入局域网 Home Assistant、未联调 Windows Codex。**本套件不得被宣称为 G1 关口通过或 Level 3/4 实测。**

### 2.6 A7 跨模块 P1 回归门禁套件证据明细 (`tests/e2e/test_p1_regression_gates.py`)

- **验证命令**：`PYTHONPATH=. python3 -m unittest tests.e2e.test_p1_regression_gates`
- **实际执行结果**：
  ```text
  ----------------------------------------------------------------------
  Ran 20 tests in 7.390s

  OK
  ```
- **门禁条目（全部断言在**出站响应信封**与**最终可观测状态**上，而非内部标志位）**：
  1. **驱动故障绝不伪成功**：固件驱动返回非 `DESK_HW_OK`（wire 上表现为 `sys.nack`+`HARDWARE_FAULT`）时，Host 信封为 `failed`、`error.code=HARDWARE_FAULT`、无 `result`、无 `executed`；`move` 与 `stop` 双分支覆盖；
  2. **无 ACK 超时绝不报完成**：固件静默 → 信封 `unknown` + `error.code=OPERATION_TIMEOUT`（`retryable=true`），且**不是** `succeeded`/`completed`/`accepted`；
  3. **乱序并发 ACK 不串线**：两个在途请求被**逆序**应答，各自等待者仅收到携带自身 `seq` 的响应，`request_id` 互异；
  4. **超时后迟到 ACK 不污染新请求**：请求 A 超时后其迟到 ACK 被丢弃；同样静默的新请求 B 必须**超时**（证明未被陈旧 ACK 满足）；随后新请求 C 由其**自身**响应满足；
  5. **真实 socket 流分帧**：消息被拆到多次 `send` 仍可重组（半包不得被当作完整帧）、一次 `send` 粘包多条消息各自处理、超长帧（> `DESK_SDK_MAX_FRAME_LEN`，从固件头文件读取）之后的有效帧仍被处理；
  6. **配对 fail-closed（真实 C 桥接）**：未配置 / 错误 / 缺失 / 格式非法的 `pairing_code` 一律返回拒绝且 `udp_out` 为空（无 `pair.accept`、无 `session_token`）；真实 UDP socket 上错误配对码**完全无回包**（超时）；对照组：正确配对码成功颁发 64 位 token；
  7. **Companion 局域网启动门禁（真实入口）**：`python3 -m companion.src.codex_companion --check-config` 在非回环 + 空/占位令牌时**退出码 2 并打印 REFUSING TO START**；对照组：强令牌（≥16 字符）与默认回环均正常通过；
  8. **既有链路无回归**：工具请求往返（`reminder.create` 真实落库）、到期提醒端到端投递、`RemoteCodexCompanionClient` 远程 RPC 均正常。
- **⚠️ 证据层级声明**：以上全部为 **Level 2**。伪固件以 Python 复现线格式；配对门禁编译真实 C 源码并调用真实 `desk_sdk_bridge_process_pairing_udp`/UDP 服务，但仍为**本机进程内**软件验证，**不得**宣称真机配对或 G1 通过。

### 2.7 A7 真实固件跨层故障链路套件证据明细 (`tests/e2e/test_real_firmware_motion_fault_chain.py`)

> 覆盖本轮最终 P1（`desk_hardware_motion_move_to` / `desk_hardware_motion_stop` 仅在 `s_strict_hardware_mode == true` 时传播驱动错误，默认配置下驱动失败被吞没为成功）的**最外层链路证据**。

- **验证命令**：`PYTHONPATH=. python3 -m unittest tests.e2e.test_real_firmware_motion_fault_chain`
- **实际执行结果**：
  ```text
  ----------------------------------------------------------------------
  Ran 5 tests in 1.325s

  OK
  ```
- **链路构造（真实组件，非 Mock 旁路）**：本套件在**真实编译的 C 硬件层**中通过 `desk_hardware_register_driver_ops` 注入故障驱动，经**真实编译的 C 协议桥接** `desk_sdk_bridge_process_command`（`desk_sdk_socket_server_start(0)` 真实 TCP 服务）→ 真实 `RobotAdapter`（真实 TCP 客户端 / 真实 ACK 关联）→ 真实 `DeskAgentHostApp`（真实 `Router` / `LifeService`），并断言 Host 产出的**最外层响应信封**（`ResponseEnvelope.to_dict()`）。故障**发端于真实 C 驱动**，逐跳存续。
- **断言项（对应任务 1-6）**：
  1. **move 驱动故障** → 外层信封 `status=failed` + `error.code=HARDWARE_FAULT`，**无** `result`、**无** `executed`，且**不是** `accepted`/`succeeded`/`completed`；
  2. **stop 驱动故障** → 同上；
  3. **`request_id` 端到端保持**：外层信封携带原始请求 `request_id`；
  4. **默认初始化 / `strict_mode == false`**：显式断言确为非严格模式（即此前出错的配置），故障前后均校验未被翻转；
  5. **资源可再获取**：故障后直接读取真实 C 仲裁器 owner 回到 `NONE`，随后同一链路再次运动返回 `accepted`（证明无泄漏的云台锁）；
  6. **CR-002 边界**：**成功**的运动 ACK 仅产出 `accepted` + `state_confirmed: false`，绝不 `succeeded`/`completed`；**不**断言任何物理完成语义。
  - **非空转保障**：断言注入的 C 驱动回调确实被调用（`driver_calls > 0`），证明故障源自真实 C 驱动而非其上层。
- **反证（negative control，一次性核验，未入库）**：将 `desk_hardware.c` 还原为 pre-926c82a 语义（驱动错误仅在严格模式传播）并重编译后，本套件 **3/5 用例失败**（move/stop 故障用例的外层信封退回 `accepted` 而非 `failed`）；A1 修复后 5/5 通过。证明本套件对被测缺陷具备判别力，非空过。
- **⚠️ 证据层级声明**：以上全部为 **Level 2（软件协议 / 模拟 PoC）**。C 驱动为**本机编译**而非 ESP32-S3 真机固件，**未**烧录、**未**连接 Tuya 云端、**未**接入局域网 HA 与 Windows Codex。**本套件不得被宣称为 G1 关口通过或 Level 3/4 实测。**

### 2.8 最终 P1 证据的四层区分（不得混淆）

本轮最终 P1 由**四个互不等价**的证据层覆盖，各自证明不同事实，**不得互相替代或混为一谈**：

| 证据层 | 文件 | 证明什么 | **不能**代表什么 |
| :--- | :--- | :--- | :--- |
| **① 直接 C 函数测试（A1）** | `tests/firmware_contract/test_desk_hardware_motion_fault_propagation.py` | 直接调用 `desk_hardware_motion_move_to/_stop`，断言返回错误码 | 不代表 wire 协议或 Host 行为 |
| **② 真实 C 协议测试（A2）** | `tests/sdk_contract/test_sdk_bridge_protocol.py`（WP1b 段，默认 `strict=false`） | 真实 `desk_sdk_bridge_process_command` 在默认初始化下产出 `sys.nack`+`HARDWARE_FAULT` | 不代表 Host 端信封 |
| **③ Python 伪设备测试（A3）** | `tests/core/test_hardware_fault_envelope.py` | Host 收到（伪设备上报的）故障时产出正确契约信封 | 伪设备**无法**证明真实固件会传播驱动错误 |
| **④ 全跨层链路（A7，本套件）** | `tests/e2e/test_real_firmware_motion_fault_chain.py` | 故障源自**真实 C 驱动**，经**真实 C 协议 → 真实 Adapter → 真实 Host** 后最外层信封仍正确失败 | 仍是 **Level 2** 本机软件验证，不代表真机/云端 |

> ④ 是 ①②③ 的**交汇**，而非替代；任一层单独都不足以支撑"驱动错误经固件协议、Adapter、Host 后外层响应也正确失败"的完整结论。

---

## 3. 架构规范与去占位整改对照表

| 架构规范项 | 改造前问题 (占位/伪实现) | 改造后生产实现 (真实接入) |
| :--- | :--- | :--- |
| **A1 硬件驱动** | 录音不足时合成 440Hz 波形、扬声器无输出、云台只改内存、相机固定字节 | 移除伪波形；实现 `desk_board_driver_ops_t` 生产驱动钩子，支持 real UART 输出（`DESK_UART_DEVICE` 或 ESP32 `UART_NUM_1`）、I2S/FIFO 音频输出及硬件摄像头采集注入；STM32 二进制帧（0xA5/0x5A/CRC16）真实生成与发送 |
| **A1 Tuya 对话** | 固定返回 "Processed response" 或固定关键词匹配 | 真实 Tuya Cloud IoT 语音会话状态机（`desk_tclaw_tuya_session_start`, `desk_tclaw_tuya_handle_cloud_event`），多轮流式 Token 回调，LLM 动态 Tool Call 派发，并将结果组织回送云端继续合成 |
| **A2 配对 Token CSPRNG** | 使用伪随机 `rand()` 和时间戳简单异或生成 Token | 严格使用密码学安全伪随机数生成器（Linux/POSIX 读取 `/dev/urandom`，ESP32-S3 调用 `esp_fill_random()`），生成 64 字符高熵 Hex 字符串 |
| **A2 RobotAdapter 生产接入** | Host 启动入口未实例化或连接官方 SDK adapter，提醒无法送达，运动无真实通道 | 编写生产级 `RobotAdapter`（`host/desk_agent/connectors/robot/`），支持 UDP 37021 动态发现配对与 TCP 37020 全双工流；在 `DeskAgentHostApp` 启动入口自动注册为 `_robot_client` 及提醒播报通道，生命周期受管 |
| **A3 生活服务** | 内存字典易失，无后台调度器自动到期播报，无物理通道也标记送达 | SQLite WAL 事务表持久化、开机追赶自愈、`ReminderScheduler` 自动轮询到期扭转 ringing；`_deliver_robot_announcement` 严格核查机器人播报通道，通道离线时保留 pending 并持续重试，杜绝虚报送达 |
| **A4 HA 接入** | 固定返回 mock 数据；无事件订阅与对账 | 实体与服务级严格白名单校验、`reconcile_states()` 状态快照对账；`subscribe_state_changes()` 接入 Host 生命周期并在路由注册 `ha.list_changes` 查询接口 |
| **A5 Codex 审批流与关联竞态** | 审批早于任务映射时的关联竞态；无 Host 查询审批的 RPC 接口 | Companion 在 `turn/start` 前立即锁定 `_thread_to_task` 并自动补齐早期审批；新增 `codex.get_pending_approvals` 与 `codex.list_approvals` RPC 路由；`get_task_status` 嵌入待审批详情；RLock 保证重入安全 |
| **A6 相机真实帧源与 LAN 鉴权** | 默认纯合成画图无硬件挂钩；开启 Token 后跨机拉帧阻断 | `CameraViewerService` 提供 `set_hardware_frame_source` 与 `ingest_robot_frame`，接入真实 V4L2/机器人帧源并标注 `hardware_sensor`；Web 服务鉴权同时支持 Bearer 报头、`X-Desk-Agent-Token` 及 `token=` 查询参数，保证跨设备浏览闭环 |
| **固件启动顺序** | 固件启动先注册工具回调再调 init 清空回调，只轮询一次就退出 | 严格重构启动顺序：`desk_hardware_init` -> `desk_tclaw_init` -> `desk_sdk_bridge_init` -> 注册工具回调 -> 启动 TCP/UDP 服务 -> `desk_tclaw_start` -> 启动常驻 FreeRTOS / pthread 后台轮询 Worker (`desk_firmware_start_background_task`) |
| **动态时钟契约** | `expires_at` 写死 `2026-10-15T23:59:59Z` 导致请求一律判定超时 | 实现 `desk_tclaw_format_iso8601` 与 `desk_sdk_format_iso8601`，基于当前时钟动态计算 `now + ttl` 有效期与事件发生时间戳 |
| **ESP-IDF 构建** | 缺少标准组件注册与构建入口 | 补充 `firmware/main/CMakeLists.txt`、`sdkconfig.defaults` (ESP32-S3 octal PSRAM, 16MB Flash) 与 `partitions.csv`，CMake 编译全部通过 |

---

## 4. 现场物理硬件与在线环境缺口清单 (Level 3 & Level 4 Gaps)

根据 `AGENTS.md` 纪律，在未连接真实物理硬件与真实线上环境前，**明确记录以下待验证项，绝不宣称整机物理通过**：

| 验证项 | 依赖外部条件 | 待实操验证内容 | 成功判定指标 | 当前状态 |
| :--- | :--- | :--- | :--- | :---: |
| **ESP32-S3 固件烧录** | 物理板卡、USB-C 数据线、ESP-IDF 5.4 工具链环境 | `idf.py -p /dev/ttyUSB0 flash monitor` | 串口正常打印 Boot 日志，SRAM 空闲 > 30KB，PSRAM 空闲 > 2MB | 待实板烧录 |
| **物理麦克风与扬声器** | ES8311 / ES7210 硬件、喇叭与麦克风模块 | 远场语音唤醒，I2S DMA 采音与播报 | 无破音、无 I2S 挂死、AEC 回声消除正常、插话打断生效 | 待硬件接入 |
| **双轴舵机物理限位** | STM32 / 串口总线舵机、外部供电模块 | 下发全行程角度（Pan 0..180°, Tilt 100..140°） | 舵机平滑到位，无机械撞击，电流无异常激增 | 待硬件接入 |
| **涂鸦云端对话实通** | 真实涂鸦 Device License、Client ID 与凭据 | 发起真人一轮语音问答 | 涂鸦 AI 识别用户意图并下发 TTS 流及 MCP 工具指令 | 待云端配网 |
| **真实 Home Assistant** | 局域网物理 HA 实例、真实智能灯/插座 | 触发关灯指令 | 物理灯具熄灭，HA 状态机上报一致 | 待网络接入 |
| **Windows Codex 实机** | 真实 Windows 11 PC、OpenAI Codex 授权运行 | 提交自动化重构任务 | Companion stdio 正常驱动，真实生成代码 diff 并触发审批弹窗 | 待 PC 联调 |
| **G1 关口整机晋升** | A1 真实固件 + A2 官方 Runtime + 单项硬件动作 | 真实 Tuya 对话 + 官方 Runtime 连通 + 一个物理转头/表情 | 达成真机对话及动作闭环 | 待 A1+A2 联合交付 |
