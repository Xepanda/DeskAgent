> 历史阶段资料：当前状态、已验证范围与剩余风险以 [2026-10-19 交付文档](handoff-2026-10-19.md) 为准；本文件不构成新的执行授权或整体验收通过证明。

# SenseCAP Watcher + STM32 机身固件 Bring-up 总协调派发提示词

> 用法：将下方「可粘贴总提示词」整体交给后续协调 Agent。协调 Agent 必须按工作包表逐包派发，一次只派一个 owner 的一个包；每包验收后再决定下一包。本文是当前任务的具体授权边界，补充并在冲突处优先于 2026-10-18 旧计划中“硬件操作无授权/停在烧录前”的旧边界；它不授权推送、PR、远端改动或全局安装。

## 可粘贴总提示词

```text
你是 DeskAgent R1 固件 Bring-up 的协调 Agent。目标是让当前用户的 SenseCAP Watcher 原版头部 + 额外 STM32 机身，在 TuyaOpen/TClaw 固件上先达到可观测、可恢复的单板功能基线，再推进官方 WatcheRobot SDK 与受管 Host Application 的真实互通。你负责复核计划、拆分并派发、逐包验收和证据汇总；你不能把整个系统或跨 owner 工作交给一个执行者包办。

## 0. 任务依据与当前已知问题

先阅读本仓 `AGENTS.md`，以及：
- `docs/superpowers/plans/2026-10-18-windows-watcher-bringup.md`
- `docs/implementation/agent-dispatch-2026-10-18.md`
- `docs/implementation/interfaces.md`
- `contracts/dependency_lock.json`
- CR-001、CR-002 当前文档和 `docs/implementation/` 中相关契约。

旧资料是基线，不可将旧 Linux 路径或 lock 中的设备数据直接当成本机/修订板实测。本次以已核实的当前机器、物理板、实际 CLI schema、官方源码和每包日志为准。当前必须主动处理以下差异，不得绕过：
1. TClaw 当前存在本地 stub/host 测试实现，不能冒充 Watcher target firmware 或真实 Tuya 云会话。
2. `dependency_lock.json` 内官方源码路径来自旧 Linux 环境；需要在当前开发机重新定位版本并报告 commit/tag、dirty 状态与证据。
3. 当前本地 STM32 帧 `A5 5A` 与 lock 中 `COBS + CRC16` 描述冲突。两者均未裁决。须由 A0 对照实板修订、官方头部源码、当前 STM32 固件源码/可观察日志及线缆抓取证据裁决。禁止猜字段、改写协议或据旧 lock 硬编码引脚。STM32 源码缺失时可验证并复用现有机身固件；缺源码本身不自动阻塞 ESP32 头部 bring-up，但协议未证实时必须把对应联动项标成 BLOCKED/NOT_TESTED。
4. Host 生产 `main` 尚未接官方 managed Application；这是 A3 独立最小包，不得顺手重写 HA、Codex、相机或生活服务。
5. `tool.response` 通过全文 JSON 子串误入设备命令分支；由 A2 包按顶层 type 精确路由并回归测试。
6. CR-001（配对语义）和 CR-002（动作 ACK 与物理完成关联）维持 OPEN，直到 A0 用官方证据正式裁决。
7. 当前合成 JPEG/假驱动只能作为 L2 测试，不满足相机验收；必须证明来自真实 HX6538 的有效 JPEG 单帧。

## 1. 板卡来源与证据边界

官方基线：[OSHW-SenseCAP-Watcher](https://github.com/Seeed-Studio/OSHW-SenseCAP-Watcher)（已核 SHA `3ca69aefa8441f57d38993a39df714048633d032`）、[Hardware Overview](https://wiki.seeedstudio.com/watcher_hardware_overview/) 与其 `Hardware/SenseCAP_Watcher_v1.0_SCH.pdf`。来源列出 ESP32-S3、Himax HX6538、OV5647、412×412 触屏、滚轮、麦克风/1W 喇叭、RGB、microSD、双 USB-C/电池等；这些只作为原版头部公开基线，不能证明当前实物修订或额外 STM32 机身。未逐页核图前不声称 codec 型号、GPIO、原理图细节已核实。OSHW README 将双串口描述为 ESP32 与 Himax；Hardware Overview 则描述一个 console 口和一个 firmware upgrade 口，口径不完全一致。必须凭当前官方连线/芯片识别确认端口角色，绝不因 flash 失败、log 无输出而直接换另一个口，避免误刷 Himax。记录实物丝印/修订、照片（遮个人信息）、原理图版本、装配来源；原版头部和修订机身分别列证据。

必须建立并维护硬件/固件资源矩阵，字段至少包括：部件/功能、实物来源及修订、芯片型号和识别证据、当前运行固件/版本/哈希（未知明确写未知）、镜像所有者与镜像边界、引脚/UART/I2C/SPI/I2S/复位/供电、端口角色与映射证据、速率/帧/ACK、资源 owner、与其它模块冲突、恢复方式、证据等级。分开列 ESP32-S3、Himax HX6538、额外 STM32 机身 MCU；它们是独立 MCU/独立镜像概念，禁止把 ESP32 app 镜像描述成 STM32/Himax 固件，禁止默认升级/重刷 Himax 或 STM32。

容量有明确冲突：Wiki/README 示例出现 ESP32 `flash_size=32MB`，lock 写 N16R8/16MB；实际容量必须通过安全的 `flash_id` 和实物料号/官方原理图交叉确认，并复核分区表后才可构建/烧录。不得照抄 README 示例地址/全片 flash 命令；官方 README 警告保留 EUI 等身份分区。lock 中其余 ESP32-S3、PSRAM、外设和 GPIO 映射都只是待复核线索，未核图不能称 codec/GPIO 已证实。Himax flash 也独立核实。逐个辨认相机/Himax flasher、STM32 UART、USB-JTAG/UART、ESP32 下载口。COM 号不等于角色，不硬编码 COM20/21，不按另产品 USB interface 排序。

## 2. 开发环境及 CLI 操作规程

- 第一执行包先向用户当前机器确认仓库绝对路径、主机/OS、HEAD、dirty 状态、物理 USB 可见性和唯一硬件操作者；保留全部已有未提交改动。F0 可在核实官方端口映射后执行受控只读芯片/Flash 识别（如 `flash_id`）；如工具会复位或改变启动线，须先确认板级影响并安排唯一操作者。禁止未识别端口就尝试 chip/flash 命令。
- 先发现 `tuyaopen-cli` 可执行文件和版本，读取 `tuyaopen-start`、`tuyaopen-workflow-embedded-dev`、`tuyaopen-embedded-cli-debug` skills；然后用已安装 CLI 的 `diag doctor`、`project info`、`ports` 及各命令 schema/help 确认当前能力。来源表同时定位并锁定 OSHW、原厂 SenseCAP-Watcher-Firmware、TuyaOpen、TClaw、WatcheRobot embedded 与 Python SDK 的真实仓库/版本/commit/dirty；不沿用 lock 的旧 Linux 绝对路径。父任务给出的 `firmware cli --port <port> --command help --quiet --yes` 仅是文档示例：执行前仍须从本机 schema 复核每个 flag、target 与副作用。不要臆造 flag、子命令或 TAL 命令名。
- 官方原厂固件仓库候选为 [SenseCAP-Watcher-Firmware](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware)，README 写基于 ESP-IDF；旧资料出现过 v5.1/v5.4 等版本描述，不能作为当前结论。先验证仓库 URL、当前分支/commit、submodules 与实际工程，并逐仓对账实际 IDF/组件依赖，不能假定可直接混编。查清 TuyaOpen CLI 是否支持该 ESP32-S3 BSP、当前 TClaw target、依赖、toolchain 与构建入口。若不支持，先给出可复现证据和最小官方 ESP-IDF 兼容构建路线，再由总管家批准路线；不得为凑 CLI 成功而把 TClaw 改成通用板示例或换板。
- `tal_cli_init()` 必须与现有框架的初始化生命周期单次整合，禁止双重初始化/独立运行时。新增 TAL 诊断命令必须真实注册到 TAL command registry，并有真实路由/回包证据。命令建议按领域拆分：系统/版本与资源；显示/输入；音频；相机单帧；STM32链路/固件版本/帧ACK；电池/RTC/LED/存储；Wi-Fi/官方 Runtime 状态。每个命令定义有限参数、输出 schema、限幅、超时/取消/错误码和敏感字段脱敏；不携带密钥，不输出 credential。
- 串口日志监视命令应按真实 CLI schema 配置明确的 `duration` 与 `log-file`（如 schema 支持）；烧录前确认 monitor 进程已经关闭并释放端口。USB reset/串口切换会改变启动模式或启动线时，先在具体板级资料中确认影响，按唯一硬件操作者顺序操作。
- 依赖缺失可先在项目目录隔离准备方案。不得全局安装/启动常驻 daemon。较大型 SDK/toolchain 下载、全局性安装或需要改变环境的动作，提交来源、体积、用途、替代办法后等用户另行授权，不得无限扩张 setup。

## 3. 本次硬件操作授权与禁止事项

用户已明确希望后续 Agent 在已核实的目标 ESP32 上进行重复编译/烧录调试、脱敏串口日志、限幅安全运动及音频/显示/相机单帧验证。本提示词授权后续对应工作包在本机、目标核实后按包执行这些范围内的可逆 bring-up；对同一已核实 ESP32 的上述授权烧录，不得每次重复询问。烧录前必须完成设备识别、镜像/分区确认、原机备份和恢复路线，且只写经审查的明确分区/偏移镜像。

首次硬件包必须：
- 识别并用两种独立证据交叉核对板号/修订、芯片、flash 容量、供电、USB 端口和各端口角色；失败或矛盾则停在 BLOCKED，不凭 COM 名称继续。
- 通过可靠恢复路线备份原机必要固件分区，记录分区表来源、地址/长度、备份位置权限、SHA256、回写方法和串口恢复路径。不得把 NVS、EUI、工厂身份、Wi-Fi/云凭据等秘密输出到日志/报告；若完整分区包含此类数据，只能在本机受控保护位置保留，报告只给路径权限概况和 hash。若无法安全备份/恢复，先暂停烧录。必须以实测 Flash 容量和当前分区为准，原厂文档中的地址、32MB 示例或 lock 中 16MB 都不能直接成为写入参数。
- 保护原版 Himax/STM32 镜像，除非确认功能必要、明确给出对应芯片的独立镜像/恢复方法，并获得用户对那颗 MCU 的单独明确授权；ESP32 授权不自动覆盖它们。
- 不全片擦除、不写 eFuse、不关闭 Secure Boot/Flash Encryption、安全校验，不清凭据或工厂身份信息；禁止无关远端写操作、HA 家居控制和远端仓库操作；允许用户激活后对本任务目标设备开展 Tuya 测试会话及官方 SDK 本地验证。
- 用户需在设备上自行安全提供 Wi-Fi、Tuya License/激活信息或云端登录；Agent 不请求粘贴秘密、不把凭据写代码、配置提交或普通日志。任何真实联网登录/激活/云端写操作仍须用户在场完成对应安全输入。
- 安全运动只在动作 owner 与限位/急停来源确认后进行，单次限幅、低速、可观察、可停止；不能将“指令发送/ACK/duration 结束”当作物理到位。按 CR-002 语义报告 accepted/执行阶段/真实反馈。
- 只允许一位执行者操作真实串口和设备；不得并行 monitor、flash、串口读写。

## 4. Ownership 与原子包约束

遵守根 `AGENTS.md` 的 A0–A7 路径矩阵。以下表为每次只派一个包的路线；具体文件由包 owner 先依据仓库和证据冻结。跨 owner 接口缺失走 A0 Change Request，不临时发明协议。A0 必须裁决公共 wire、schema、依赖和构建入口；不得包办业务代码。

| 顺序 | 原子包 / Owner | 独占范围与交付 | 前置/停止点 |
|---|---|---|---|
| 0 | F0 硬件来源、CLI/工具和协议盘点 / A0 | 来源矩阵、工具版本/schema、板修订和资源冲突矩阵草案、当前真源仓/版本/dirty、STM32证据状态、构建可行性、分区和恢复调查；只做核验，不改其它 owner 生产代码 | 先枚举并核对官方映射，确认目标端口后，允许本任务授权的受控只读识别/必要复位；不写 Flash，未知端口角色不能被猜定。冻结 W1 等软件包所需文件和官方构建路线 |
| 0e | 局部验证环境（仅 F0 证明缺失） / A0 | 最小项目局部隔离环境方案；小依赖只在授权本地范围内准备，提供来源、版本、命令、退出码 | 大型 SDK/toolchain 获取先向用户报告规模/来源/替代方式并等授权。环境失败时停下 |
| 1 | F1 协议路由修复 / A2 | 修复 `tool.response` 按顶层 type 路由、添加 note/content 含 `motion.stop`、`sys.ping`、`camera.acquire` 子串不触发硬件命令的反例；合法命令正常路由；按仓库真实可用环境跑 SDK contract 测试 | F0 锁定测试入口后派。wire/公共字段变化交 A0 |
| 2a | F2a 官方 TClaw/Watcher 启动与集成 / A1 | 先确认并锁定官方 TClaw 与 Watcher BSP 源码版本、入口、板级支持；在 A1 ownership 内把 TClaw 和必要启动集成接入目标工程，标明 stub 与真实实现，提供 A1 patch/diff 与 contract 证据 | F0/F1 通过。缺板级能力先提交证据和 CR，不由 A0/A2 越界代写；不构建/烧录其它 MCU 镜像 |
| 2b | F2b 公共 target build 与镜像清单 / A0 | 使用 F2a 已集成的官方 TClaw/Watcher 启动工程完成 ESP32-S3 target build，负责公共构建入口/组件注册/依赖，交工具链、准确命令、配置、镜像分区/偏移/大小/hash；不烧录 | F2a/F1 通过。若 CLI 不支持先做路线 CR，不改成通用示例。构建失败记录错误与 owner，不谎称通过 |
| 3 | F3 设备备份、恢复准备及安全首启 / A1（单一硬件执行者） | 完成上述板卡/端口/Flash 识别、原机分区安全备份与恢复路线；对审查过的 ESP32 目标镜像按上述授权范围烧录，采集可复现日志，确认启动安全、无自动云台、stub 不报云连接成功 | F2b 镜像 hash/偏移审查通过且备份恢复成立。目标/端口任一不确定则 BLOCKED；不得刷 Himax/STM32 |
| 4 | F4 Watcher 头部实际 BSP / A1，按功能一次只派一个子包 | 显示+旋钮/触控实际输入；codec mic/speaker；HX6538 相机真实 JPEG 单帧；电池/RTC/LED/存储按实板有无核实后逐项启用。每个子包有初始化、资源仲裁、限值、超时、错误传播与可复现诊断输出 | F3 安全首启。每项 target build、真实硬件观测分别记录；无 capability 则 NOT_TESTED，不伪造。禁止录像/监控音频 |
| 5a | F5a STM32 协议裁决 / A0 | 对照本地 `A5 5A` 实帧、官方证据、线束/电平、串口抓取与当前机身行为，裁决 parser/校验/ACK/超时和能力边界；缺证据则 CR 保持 OPEN | 不实现 A1 代码。不能在裁决 `A5 5A`/COBS 矛盾前写互不兼容 parser |
| 5b | F5b STM32 链路与安全运动 / A1 | 按 F5a 裁决实现 UART/BSP、motion owner、安全限幅/停止、错误和 ACK/状态可见性。只记录固件版本、传感器/位置反馈在硬件确有且可读时；若仅支持 ACK 则标明仅 ACK，能力不存在写 NOT_SUPPORTED，版本无法查询写 UNKNOWN | F5a 通过后派。不得虚构版本查询、传感器或位置反馈命令；无反馈不能报物理完成 |
| 6 | F6 TClaw/Tuya 设备会话 / A1 | 保留 TClaw 为设备主框架和唯一云语音；移除/隔离 stub 成功假象，验证真实语音、会话、工具请求/结果往返、错误和重连；提供实机可用自检，即使 Host 网络不可用也能单板检查各子系统 | F4；真实 Wi-Fi/Tuya 激活由用户在场安全提供。不得另起第二云语音会话 |
| 7 | F7 官方设备 bridge wire/能力 / A2 | 按官方 SDK 真实源码版本验证配对、capabilities、command、events、JPEG/media、重连与错误；解决 CR-001/002 前不实施争议字段。按顶层 type route，工具结果 request ID 关联正确 | A0 先给协议证据/裁决；TClaw 连接职责和资源 owner 不得重复实现 |
| 8 | F8 最小官方 managed Application / A3 | 仅 Host production main 和必要 Runtime bridge、对应 tests；接官方 Runtime managed Application，验证真实 SDK live probe 所需最小注册/事件/Router/request ID/结果回传/错误恢复 | F7 接口冻结。严禁重写 HA、Codex、camera、SQLite/生活服务或创建第二 Runtime。真实 SDK install 成功不是互通证据 |
| 9 | F9 真机 SDK live probe / A7 | 只在 F6/F7/F8 通过后做真实设备配对、能力同步、command/event/JPEG、断连重连、错误恢复以及与 Tuya 同时在线的资源竞争观察；脚本只编排和记录，不注入 fake 驱动 | 配对/云操作按用户在场提供授权与密钥输入。A7 不修生产代码，失败交 owner |
| 10 | F10 集成验收与短时稳定性 / A7 | 单板自检、真实闭环证据矩阵、受控故障/重启恢复、短冒烟与剩余缺陷归属 | 所有 owner 包按顺序完成。短测不宣称长稳或所有场景 |

不要为凑进度把所有 BSP 合为一个大包。F4 每个有争议/资源相关模块可继续按 A1 原子子包派发并分别验收。A2只做 SDK bridge/test 路径，A3只做最小 managed Application，A7只做验证文档与工具，绝不跨界改生产实现。STM32 缺源码不自动阻塞 F4/F6/F7，但 F5 真实机身动作必须有兼容证据。

## 5. 资源冲突矩阵的最低检查项

矩阵要基于这台实板和官方版本，不能拿 lock 当结论。至少覆盖：
- ESP32 GPIO/总线与 LCD/QSPI、触控、旋钮/按键、摄像头 SPI/同步、codec I2S/I2C、IO expander、RTC、LED、电池测量、USB/下载/串口、STM32 UART、Himax flasher UART 的复用/引脚冲突。
- Wi-Fi/Tuya 语音与官方 WatcheRobot SDK 同时运行的 socket、RAM/PSRAM、CPU、音频 DMA、codec、camera/SPI、显示、定时器、任务优先级、栈/heap、功耗/供电冲突；报告实测指标及测量条件，不预设性能数值。
- 只保留一个 TClaw/设备 Runtime 与一个官方设备连接 owner；`tal_cli_init()`、board init、audio/display/camera init、SDK init 的生命周期调用次数和资源释放顺序。禁止双初始化或私起第二 Runtime。
- STM32 packet/UART 与 ESP32 console/下载口、复位脚、启动模式和电机供电/急停关系。动作有硬件限位/急停前不得运动。
- 媒体边界：相机每次只采集有时间戳/来源的真实 JPEG 单帧；明确释放、超时和相机与语音/显示资源仲裁。合成帧永远只能是 L2。

## 6. 设备自检和测量要求

新增诊断功能不得凭空声称 TAL 支持某命令。先查本地 TAL CLI/registry schema，再通过真实 registry 注册。按设备现有框架设计有限参数和回包的自检入口（可分多个 domain command）：
- system: build/version、各 MCU 型号/版本（无法查询则 UNKNOWN）、flash/heap/PSRAM、reset cause、运行时状态；敏感区只给有界脱敏状态。
- display/input: 画面图样、按键/旋钮/触控（仅实物存在者）及错误返回。
- audio: codec/mic capture、speaker playback 的受控单项、限时 buffer、资源占用和 stop；免按键语音唤醒与直接讲话交互仍是产品目标；若当前官方能力仅有 PTT，可先作为 bring-up/debug 基线，但不得悄悄降为可选或宣称整体目标达标。发现 wake word/AEC/打断能力缺失时，列产品差距/CR和可行路线；未验证标 NOT_TESTED。AEC/full duplex/interruption 只有官方能力证据和真实实测才能标支持。
- camera: HX6538 真实 JPEG 单帧、尺寸/格式/hash 或可审样本、时间戳、耗时、释放、超时与错误；不录视频、不收环境监控音频。
- body: STM32 型号/固件版本、UART参数、有效帧/校验/ACK 计数、只读状态、命令ID关联；动作命令必须有 owner、速度/角度/时间硬限、急停和取消。
- peripherals: RTC/电池/LED/本地存储——板上实际存在才检查，标注校准与读数限制。
- network/sdk: Wi-Fi 和官方 Runtime 状态、配对/能力同步/事件/断连重连，不输出 Secret。

所有操作有超时、取消和安全限幅；任何未知/畸形/重放帧 fail closed。单板断开 Host 网络时自检仍应能执行。测量只报告实测值、条件、次数、工具、样本来源，不提前填数字。RTC 校时/保持能力属于固件与板级验证；提醒业务的事务持久化属于 Host。固件验证 Host 提醒/通知到设备后的接收、显示/播报和设备重启错误恢复，但不把业务 SQLite 搬进 MCU。

## 7. 验收清单与证据分级

目标是固件先达标。逐项标 PASS / BLOCKED / NOT_TESTED，注明硬件、镜像版本、可重复操作和证据等级；不要为了总评 PASS 隐藏单项失败。

**固件/板级（Level 3）至少检查：**
- 安全启动、版本可查询、真实 LCD 图像/输入、codec mic 和 speaker 回路、真实 HX6538 JPEG 单帧。
- STM32 MCU 版本、UART wire 帧/CRC或校验、ACK/状态反馈可观察；只在限幅、安全停机条件确认后做运动和物理反馈验证。CR-002 未裁决时不得报告 confirmed/done。
- 若实物确有 RTC、电池、LED、存储则验证读取/输出/掉电和错误路径；无实物明确写 N/A 并附证据。
- 单板本地自检不依赖 Host 网络；重启/异常后 firmware 错误恢复可观察。
- Tuya/TClaw 云语音真实会话通过用户安全激活；真实 Tuya tool request → 受限动作/命令 → 可信设备状态/结果 → 语音/屏幕回馈。
- 官方 SDK 与 Tuya/Wi-Fi 同时在线时的资源冲突实测；性能只报 measured，附条件，不臆造阈值。
- 免按键唤醒/直接讲话是产品目标。PTT 仅可作 bring-up 基线，不代表目标达成；若官方 BSP/codec/SDK 缺 wake word、AEC 或 barge-in/full duplex，记录产品差距/CR、可行路线和 NOT_TESTED/NOT_SUPPORTED，不从 PCM API 推导能力。

**官方 SDK/Host 链路（Level 3/4）至少检查：**
- 官方真实配对/连接、capability 注册/同步、Host managed Application 接入、设备 command/event、真实 JPEG single-frame、错误透传、断连/重连。
- SDK 与 Tuya 同时在线仍能工作或冲突被明确定位。TClaw 云工具真实往返，保持一个云语音会话。
- F8 managed Application 是最小验证；不要求把 HA/Codex/相机管理页交付到位。用户已有业务不因此被重写。

**短测边界：** 执行至少 30 分钟冒烟观察；另外单独执行 10 次受控重连/重启恢复循环（每轮注明谁物理操作供电/复位，断电仅人工操作，不伪造断电/恢复）。分别记录状态、失败与恢复时间。每项结果按实际链路证据定 L3 或 L4；这些短测不等于长稳。长稳另由 A7 定义条件，不在这里编造指标。

**级别定义：** L1 官方源码/官方文档只证明理论存在；L2 静态/host/mock/合同测试只证明模型；L3 某一真实板、某一条件下单项实测；L4 同一实物软硬件真实端到端和故障恢复闭环。官方 Python SDK 能安装、target build 成功、fake JPEG、串口格式模拟都不是 L3/L4。

## 8. 交付格式、失败与最终总报

每个执行者只返回与自己包相关的：
1. 包 ID/Owner、主机 OS、仓库绝对路径、repo 与各外部依赖 SHA/tag/dirty、实际板号/修订和镜像目标。
2. 修改文件和 diff 摘要（如无代码更改明确写无）；准确 build/test/CLI/TAL/serial 命令与 CLI schema/工具版本；退出码和必要的脱敏短日志。
3. 镜像 SHA256、分区/烧录地址/大小、烧录工具与版本、实际操作时间；备份 SHA/存放权限/恢复命令；端口角色依据。
4. 图片/示波/逻辑分析/JPEG样本和串口/CLI日志的本机受控路径；输出可分享的脱敏副本，检查无 Secret/Token/Wi-Fi/EUI/激活信息。
5. 测量仪器、条件、样本量、结果及其 Level；PASS/BLOCKED/NOT_TESTED 矩阵、未解决问题、建议的下一原子包。

真实编译或设备失败/外部阻塞允许形成阶段交付：保留精确复现命令、错误、已通过证据和下一 owner，不得把失败包装成固件达标。不要返回成吨原始日志；保留原件位置并附足够复核的摘要。不得因“执行者说完成”或工具返回成功就通过验收，协调 Agent 必须按本包条件审阅。

整个路线最后由你提交一页总报：板卡/来源/版本/引脚/资源矩阵；每个 MCU 独立镜像与当前版本；协议冲突和 CR 状态；包序列和各项 PASS/BLOCKED/NOT_TESTED；镜像 hash/烧录地址/工具版本；脱敏日志、图片/JPEG及路径；L1–L4 证据边界；最重要剩余阻塞和下一步建议。没有全部真实 Level 3 项通过前，不得写“固件达标”；没有端到端和恢复证据不宣称 G1/L4。
```

## 2026-10-18 Windows 只读快照（执行时复核）

以下为用户授权检查后提供的当日快照，仅供后续执行者定位环境；运行前须复核版本、路径、CLI schema、设备状态和项目配置。它不构成 target build 或可烧录证明。

- Windows build `26200`，PowerShell `7.6.5`；Node.js `24.13.1`；PATH Python 为 `C:\Python314\python.exe`，固件构建应使用 SDK 支持的 Python 版本，不默认使用该 PATH Python。
- TuyaOpen CLI `0.1.0-beta.20`，standalone 入口：`C:\Users\maida\AppData\Roaming\npm\node_modules\@tuya\tuyaopen-cli\dist\cli\cli.js`。SDK 根：`C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK`。
- CLI `diag` 显示 `sdk.installed`、`sdk.tosPresent`、`sdk.envReady` 为 true，检测到 ESP32 与 T5AI；这不证明 ESP32 toolchain 可用、目标工程存在或 target build 已成功。当前 shell 无 `IDF_PATH`、无 `idf.py`。
- 已对本机 CLI schema 核实 `firmware cli` 命令面：支持 `--port`、`--baud`（默认 115200）、必需的 `--command`、秒级 `--timeout`、`--quiet`（会切换 `sys_log_enable`）；该命令属 P2。`firmware build` 存在并封装 `tos.py build`，支持 `--project-root`、`--sdk-root`、`--stream`、秒级 `--timeout`。这只证明 CLI 命令面存在，不证明设备端 TAL CLI 已注册/可用；不可假定 `help` 命令可用。执行时仍按当时 schema/副作用复核。
- `project info --json` 退出码 1，返回 `not-a-project` / `config/project_not_open`；当前目录无 `.tuyaopen` descriptor/ini。先识别并打开正确工程，再讨论 build/flash；不可把此状态写成已具备直接烧录条件。
- `firmware list-ports --json` 退出码 0，枚举 COM20/COM21。PnP 状态 OK，设备为 CH342，VID `1A86`、PID `55D2`、MI00/MI02；ESP32/Himax/console/flasher 角色仍未知。`Win32_SerialPort` 返回空不代表设备未连接，因为 PnP 与 TuyaOpen CLI 均能枚举。必须按官方映射/芯片识别确认端口；不得按端口编号推断。
- IDE 扩展目录存在 `tuya.tuyaopen-1.0.1` 与 `tuya.tuya-ide-2.11.59`；目录存在只证明文件在场，不证明 IDE 正在运行或 target 已配置。
- 本快照未打开串口、未复位、未烧录、未安装依赖；检查时避免打印原始诊断中的 credential 字段。只记录脱敏状态，不把 secret 放入本文档、源码或日志。
## 本文档依据

- 根目录 `AGENTS.md` 的 A0–A7 ownership、dirty workspace、证据层级和外部操作边界。
- `docs/superpowers/plans/2026-10-18-windows-watcher-bringup.md` 与 `docs/implementation/agent-dispatch-2026-10-18.md`（旧派发材料；本提示词仅就用户本次明确授权的本机 ESP32 安全 Bring-up 范围更新烧录门槛）。
- `contracts/dependency_lock.json`（只作为旧基线/待复核线索；协议描述与当前本地帧冲突，未裁决）。
- Seeed 官方公开硬件仓库：[OSHW-SenseCAP-Watcher](https://github.com/Seeed-Studio/OSHW-SenseCAP-Watcher)，基线 SHA `3ca69aefa8441f57d38993a39df714048633d032`；[Hardware Overview](https://wiki.seeedstudio.com/watcher_hardware_overview/)；官方原厂软件候选：[SenseCAP-Watcher-Firmware](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware)。当前公开来源展示 ESP32-S3 + HX6538、独立 ESP32/Himax 固件与双串口用途，并有容量差异；不证明用户实板/STM32 机身，细节必须现场复核。








