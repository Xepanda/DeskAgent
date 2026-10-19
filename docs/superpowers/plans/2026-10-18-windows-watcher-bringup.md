# Windows SenseCAP Watcher Bring-up Implementation Plan

> **For agentic workers:** 本文件只定义工作包，不授权执行。每次只派发一个工作包并单独验收；步骤用 checkbox 跟踪。首轮依次派 W0、（仅环境缺失时 W0e）、W1、W2，完成 W2 后停止在 W3 的硬件操作授权关口。

**Goal:** 在保护原机恢复路径与授权数据的前提下，以 TClaw 为设备主框架、官方 WatcheRobot Runtime/Daemon 为唯一设备连接所有者，打通本产品所需硬件和业务能力。

**Architecture:** Watcher 设备端保留 TClaw、Tuya 云语音和硬件仲裁；官方 Runtime/Daemon 唯一负责设备连接，Host 作为受管 Application。Windows 运行 Codex Companion，长期生活服务和 HA 在泰山派；Windows 可用于临时联调。A0 管公共契约、依赖和构建入口，A1 管设备启动/板级驱动，A2 管 SDK bridge，A3–A7 按 ownership 矩阵完成各自模块。

**Tech Stack:** Seeed SenseCAP Watcher / WatcheRobot Dev Kit；TuyaOpen/TClaw；ESP32-S3 与 WatcheRobot HAL；ESP-IDF/TuyaOpen 构建链；官方 WatcheRobot Runtime/Daemon/Python SDK；本仓 C11 bridge、Python Host/Companion、SQLite、HA API、Codex app-server stdio。实际可用版本和 Windows 工具链由 W0 复核，锁文件旧路径不作为当前机器事实。

**Spec:** `docs/superpowers/plans/2026-10-11-desk-agent-system.md`、`docs/superpowers/plans/2026-10-16-desk-agent-real-integration-and-p1-remediation.md`、`docs/implementation/interfaces.md`、`docs/implementation/handoff-2026-10-17-windows-codex.md`、`contracts/dependency_lock.json`；当次总管家裁决和本计划的硬件安全边界优先。

## Global Constraints

- 本计划基线快照为 2026-10-17；每个执行包先报告主机/OS、仓库绝对路径、Git SHA 与 dirty 状态，再复核对应事实。
- Watcher 实物 USB 接在当前 Windows 主机；COM20/COM21 同属 CH342（VID `1A86`, PID `55D2`, MI00/MI02），具体角色未知。仅枚举端口已允许；不得凭 COM 号推断用途或读取 chip ID。
- 只使用能访问当前 Windows USB 设备的单一硬件执行者操作串口/烧录；远端 Linux 执行者可以做代码或构建工作，不能声称能访问本机 COM 设备。硬件执行者每次只允许一位。
- TClaw 是设备主框架和硬件仲裁所有者，Tuya 云语音留在设备。官方 WatcheRobot Runtime/Daemon 是唯一设备连接所有者；Host 走受管 Application。现有自定义 RobotAdapter 仅用于迁移/测试基线，不得形成第二套 Runtime。
- 优先适配可构建的 Watcher/TClaw 板级工程；不得把当前 standalone host executable 烧到板。若 TClaw 缺少 Watcher 支持，A0 提交带源码证据的 CR，由 W0 定义单独 A1 包实现适配；不得改用别板或移除 TClaw。
- W2 只证明 target build 与镜像清单，不授予烧录资格。若最小启动/板级胶水缺口阻塞构建，触发 W3b：A1 修改并集成自己的启动/板级文件，A0 仅处理公共构建并重试；完整板级适配由 W0 定义单独 A1 包。
- 首次烧录只验证安全启动：启动日志/心跳、无自动云台动作、无伪造 Tuya 云会话成功。必须先完成原机/分区/恢复基线，确认镜像目标并取得当次明确烧录授权。
- 先核分区布局、恢复路径、是否保留用户数据；不盲目重装 SDK、擦全片 Flash 或改工厂凭据。保护授权优先：不读取/修改 NVS 或工厂凭据；任何可能包含凭据的整片备份/读取、复位、擦写都需另有明确、具体授权且不得输出凭据内容。
- 不读取/写入生产 Secret、API Key、Wi-Fi 密码或 HA token；真实配网、License、云端操作、家居动作、Codex 真任务各自需要对应的明确授权/用户参与。计划本身不构成授权。
- Windows 临时联调不代表泰山派部署。Levels 1–4 严格区分；host 构建、模拟、Python SDK 安装均不代表 target、物理、云端或端到端通过。
- A1 独占设备启动/板级文件并自行集成；A2 仅交 SDK bridge 接口/patch；A0 仅管理公共 CMake、组件注册、依赖和镜像重建。只派一个包，不跨 owner 批量实现。
- 现有未决 CR-001（配对失败响应/配对码来源/会话 token 设计）和 CR-002（ACK 与物理完成事件关联）维持 OPEN；不得私造 wire 字段或把 accepted 说成物理完成。
- HA 不改用户现有集成/实体/自动化；不新增第二个云语音会话；Codex app-server 不暴露网络端口；相机仅单帧 JPEG 与租约，不录像、不采集监控音频、不绕过 motion owner。
- 本次两份文档任务不 commit/push。后续实现包可在核定的 `md/` 分支内本地 commit 交接；不得自动 push、建远端分支、开 PR 或 merge。不得执行 `git reset --hard`、`git clean -fd`、强制切分支或覆盖用户改动。本计划不授予烧录权限，仅后续 W4b 等具体获授权设备包可在授权范围内烧录，其它包禁止；本地代码修改/单元测试按派发包授权执行。公开官方文档/GitHub 可只读调研；真实业务云会话、设备控制、云写操作按包需明确授权。本轮两个规划文件是授权新增未提交文件，W0 应保留并记录；跨机执行者先同步两文档，GitHub main 尚不含它们。


## 路径 Ownership 裁决（本计划专用）

| 路径 | Owner | 边界 |
|---|---|---|
| `host/desk_agent/main.py`、必要 Host Runtime bridge、`host/desk_agent/connectors/robot/` | A3 | 仅路由/连接迁移；不重写 HA、Codex、相机模块 |
| `host/desk_agent/web/` | A6 | Web 管理页面与 LAN 相机交互 |
| `firmware/main/src/app_main.c` | A1 | 设备启动入口及实际板级启动集成 |
| 公共 CMake、组件注册、依赖与镜像重建 | A0 | 不编辑/合入 A1 私有启动入口或板级文件 |
## Review Focus

1. `tool.response` 中 note/content 含设备命令子串时被全文匹配误判：W1 测 `note.content = "motion.stop"` 不触发硬件命令，并覆盖嵌套 type 与普通命令。
2. TClaw、官方 Runtime/Daemon 与旧 RobotAdapter 争抢同一连接/硬件：W7a 与 WG1 证明唯一 Runtime 和实际资源仲裁。
3. NVS/工厂数据、分区或恢复路径不明导致覆盖原机状态：W3/W4a/W4b 记录分区与回退证据，并在授权缺失时停下。
4. target link 成功或 `.bin` 存在被误认为可烧录/板级功能完成：W2/W3b/W4a/W4b 分别验收构建、启动安全、最终镜像、授权物理启动。
5. ACK、配对接受或云 API 返回被误称为执行完成：W7b0/b1、W7c0/c1/c2/c3、WG1 及 A4/A7 分开验证 accepted、物理状态与端到端反馈。

---

## 工作包和依赖

### W0 — A0：Windows 资产与接口复核（Level 1）

**范围：** 只读文档、Git、工程、工具链、外部仓库源码和端口枚举；允许查阅公开官方资料/GitHub 源码与版本，不打开串口会话、不做真实业务云会话/设备控制/写操作。

- [ ] 执行者先报告主机/OS、仓库绝对路径、SHA、dirty 状态；确认目标仍是指定 DeskAgent repo。
- [ ] 复核当前 SenseCAP 型号/板级来源、TuyaOpen/TClaw/WatcheRobot/官方 Python SDK 的实际位置、版本、dirty 状态；旧 Linux 绝对路径只作为待定位线索。
- [ ] 核对 Windows `tuyaopen-cli`、SDK、IDF/toolchain、`idf.py`、CMake 与项目识别；记录 `IDF_PATH` 和 target build 前置条件，不安装依赖。当前 Windows 无 host C compiler、固定 Python 缺 requirements 的快照须复查。
- [ ] 只枚举设备管理器/COM20/COM21 的 VID/PID/MI 和端口占用者；角色、chip ID、Flash 内容均标为未知。
- [ ] 对账 `dependency_lock.json`、`interfaces.md`、CR-001/002、分区文件、板级工程与当前代码；确认 CLI `project info` 情况。
- [ ] 验证 native C/SDK contract 测试环境：检查 Windows 对 POSIX headers/shared-library 的支持，并检查现有 Linux 节点是否能访问已同步 SHA 的源码；记录解释器、编译器、依赖和确切命令。
- [ ] 交付连接契约表，每条链路列客户端/服务端、源码证明的实际协议、配对/鉴权来源、控制/媒体/事件通道、唯一连接 owner、官方文件+行或锁定版本引用；核对自定义 TCP 与官方 Runtime 是否兼容。
- [ ] 找出官方可构建 Watcher/TClaw 工程和组件接入点；若完整板级适配超出最小启动胶水，定义独立 A1 包（目标、文件、测试）供审查；若证据不足输出 CR/阻断项，不擅选其它板。
- [ ] 记录真实已有构建和测试命令、host-only / target-only 的区分、单一 Windows 硬件执行者安排。
- [ ] 交付基线表、路径/版本/SHA/dirty 证据、未知项和逐包建议；不改文件、不触碰串口/云。

**停止关口：** W0 未确认正确工程/版本/OS与职责时，不派 W1/W2；硬件角色未知不阻止纯软件包。

### W0e — A0：项目局部验证环境准备（条件包）

**触发：** W0 证明选定环境不能运行 W1 所需 native C contract tests 时才派；可用则跳过。

- [ ] 仅使用已激活的 SDK/现有 Linux 环境，或项目局部 venv/已批准 compiler 路径；不改业务代码。
- [ ] 记录源码 SHA 同步方式和仓根，确保测试运行于基线代码。
- [ ] 如需 Python 依赖，仅按 W0 确认的 requirements 建项目局部 venv；不全局安装或重装 SDK。
- [ ] 大型下载先提交来源、体积和替代方案，等待用户决定；不擅自下载。
- [ ] 运行仓库现存的正常协议用例作单项 smoke（不依赖 W1 新增回归测试）；交环境路径/版本/命令/退出码。此项只验证环境/编译链，不要求先修复现存缺陷。

**依赖：** W0 明确证明环境缺口。**停止关口：** smoke 未通过则不派 W1。

### W1 — A2：顶层 type 路由安全修复（Level 2）

**文件：** `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c`、`tests/sdk_contract/test_sdk_bridge_protocol.py`。

- [ ] 新增 `test_tool_response_note_motion_stop_is_not_dispatched_as_command`：顶层 `type=tool.response` 且 `note.content="motion.stop"` 时原样按工具响应流程处理，不调用 motion stop driver。
- [ ] 新增覆盖 `tool.response` 的嵌套字段含 `sys.ping`/`motion.move`/`camera.acquire` 子串，均不得落入设备命令分支。
- [ ] 新增/保留正向测试：顶层真实 `ctrl.motion.stop` 和 `sys.ping` 仍走各自分支；异常/未知顶层 type fail-closed。
- [ ] 按**顶层 type**精确分派；不得继续以整段 JSON 的 `strstr` 作为命令身份判断。若现有 parser 不支持顶层读取，A2 可在本包内实现/替换内部 helper 并测试；只有涉及公共依赖、wire 字段或跨模块接口时才交 A0。
- [ ] 在 W0 选定的 POSIX/Linux native C 环境、仓根和同一源码 SHA 下运行 `PYTHONPATH=. python3 -m unittest tests.sdk_contract.test_sdk_bridge_protocol`；报告解释器/编译器和结果。不能假设 MSVC 即可运行 POSIX C tests。
- [ ] 检查 diff 仅在 A2 ownership 路径及该测试文件；交付变更、命令/结果、失败残项和 Level 2 证据。

**依赖：** W0 的源码/测试入口确认；W0e 若触发须先通过。**停止关口：** 新回归未通过则不进入 target build 或动作联调。

### W2 — A0：Watcher target build 与镜像清单（Level 1/2）

**文件范围：** A0 经裁决的公共构建入口/组件注册/依赖清单；A1 可提供 patch，但本包由 A0 只改公共 CMake/组件注册/依赖。A1 启动入口及板级文件由 A1 自行集成；精确文件在 W0 冻结。

- [ ] 先确认当前项目源树和 IDF/TuyaOpen 版本，不运行 host standalone 产物冒充设备固件。
- [ ] 根据 W0 已验证的官方 Watcher/TClaw 工程，确定最小、可复现 target 配置；若支持缺失，提交 A0 CR 和源码证据，暂停。
- [ ] 将 bridge/hardware/TClaw 组件接到官方 target build 入口；公共 CMake、依赖、组件注册由 A0 改/合；板级驱动由 A1 自行修改并集成；若只有最小启动/板级胶水缺口，触发 W3b，A1 集成后由 A0 重试 W2。完整板级适配必须由 W0 定义独立 A1 包，不得假编译成功。
- [ ] 运行确切 target build 命令，记录工具链版本、target、配置来源、完整摘要、退出码。
- [ ] 列出 bootloader、partition table、app `.bin` 的路径、大小、SHA256、分区偏移及目标板；构建日志中不得含凭据。
- [ ] 单独标注真实 target 编译与 host 仿真；不把编译通过当作 BSP 完成或烧录许可。
- [ ] 交付公共构建变更、可审镜像清单与可复现命令；不烧录、不接触设备、不改用户 NVS。

**依赖：** W0 工程路线和连接契约；确认自定义 bridge wire 与官方 Runtime 路线兼容，或由 A0 裁决适配 CR；W1 路由修复进入 target build 前。**停止关口：** 只有构建证据，不可烧录。

### W3 — A1：原机信息、分区和恢复路线（Level 3 前置准备）

**文件范围：** A1 的只读实测记录/板级文档；不得改固件、写设备或读出凭据。

- [ ] 执行者报告本机 Windows、物理 USB 可见、DeskAgent SHA；确认自己是唯一硬件执行者。
- [ ] 先依据官方/本地板级文档确认原机分区表、Flash 容量/布局与恢复方法；软件定义的 `firmware/partitions.csv` 不等同于设备当前分区。
- [ ] 只进行当次明确授权的无凭据识别操作；默认不读 Flash/NVS/efuse、不读 chip ID、不复位、不擦写。
- [ ] 任何潜在含凭据的整片备份/读取、复位、擦写均要求单独具体授权；若授权不足则以“未执行”记录并停下。
- [ ] 记录原机版本/启动现象、端口映射（如可安全确认）、分区风险、恢复步骤、无法验证项；不记录或输出 Secret。
- [ ] 交付包含授权范围与准确操作日志的基线报告；不足以确保可恢复则阻断 W4b。

**依赖：** W0。**停止关口：** 未确认分区/恢复方案与明确授权，不得首次烧录。

### W3b — A1：最小安全启动 patch（仅当现入口不满足时）

**文件：** `firmware/main/src/app_main.c` 与 W0 冻结的 A1 板级路径；A1 修改并集成。禁止修改 SDK bridge、Host 或公共构建入口。

- [ ] 审阅现启动入口，列出自动云台、默认 stub、云会话状态上报和启动日志的实际行为。
- [ ] 仅实现安全启动验证所需 patch：可观察启动日志/心跳；不触发云台动作；不把 stub/未连接状态报告成 Tuya 云会话成功。
- [ ] 在 `tests/firmware_contract/test_safe_startup_contract.py`（新建）断言启动不下发 motion、stub 不报 session connected。
- [ ] 运行对应 host contract 测试；交付 A1 patch、测试证据、剩余启动风险和供 A0 合并的精确文件列表。
- [ ] 不改组件公共注册/构建入口；不接触硬件、不烧录、不配网。

**依赖：** 仅依赖 W0 冻结的启动/板级路径；不依赖 W2 或 W3。若 W2 仅被最小启动胶水阻塞则条件触发；A1 集成自身文件后 A0 重试 W2。完整板级适配另由 W0 定义 A1 包。W3 实测不阻塞此代码包；不得与 W4b 物理操作并行。

### W4a — A0：最终镜像集成与审查（Level 1/2）

**文件范围：** A0 仅改 W2 冻结的公共构建入口/组件注册/依赖并重建；不编辑或合入 A1 私有 app_main/板级文件。

- [ ] 输入 W1 修复、W2（含必要 W3b 后重试）target build、W3 分区/恢复报告及 A1 已自行集成的文件；检查 owner 和文件边界。
- [ ] 只处理公共构建变更并重新构建最终 target image；A1 私有启动/板级文件已由 A1 集成。
- [ ] 输出 bootloader/partition/app 镜像 SHA256、大小、目标、偏移和构建命令，确认无凭据进入日志。
- [ ] 用启动路径审查与已有静态/contract 测试确认镜像符合安全启动目标：启动日志/心跳、无自动云台、stub 不伪报云连接。
- [ ] 交付最终镜像审查记录和烧录可行性结论；本包不操作设备，也不授予 W4b。

**依赖：** W1、W2 成功（若 W3b 触发则由 A1 集成后 A0 重试）、W3 和必要的 W3b。此包仅 Level 1/2。

### W4b — A1：首次最小安全启动（Level 3）

**文件范围：** 无代码修改；当前 Windows 上一位硬件执行者操作核准板卡。

- [ ] 验收 W4a 最终 image 审查、W3 原机/分区/恢复方案，并核对目标板与镜像 SHA/偏移。
- [ ] 开始前重报本机 Windows、COM 身份证据和唯一硬件执行者；只在获得当次明确烧录授权后执行。
- [ ] 仅写入授权范围内的核准镜像/分区；不默认全片擦除，不读写 NVS/工厂凭据。
- [ ] 采集启动日志/心跳，验证无自动云台动作且未连接时不报告 Tuya 会话成功；不做配网或真实动作。
- [ ] 异常时按 W3 恢复路线停下并报告；任何复位/擦写须在授权范围内。
- [ ] 返回目标/镜像 SHA、工具/命令、端口角色、授权范围、脱敏日志和观测结果。

**依赖：** W4a 完成、W3 可恢复、明确烧录授权；无一可省略。

### W5a — A1：显示与实际输入 BSP（Level 2→3）

- [ ] 依据 W0 官方板级证据确认显示能力与实际输入类型（旋钮/按键/触摸）；未证实的输入项不实现、不假测。
- [ ] 将已证实的显示/输入接入 A1 硬件抽象/仲裁；不改 A2 协议或启动公共入口。
- [ ] 在 `tests/firmware_contract/test_watcher_display_input.py` 新增 contract 测试，断言显示初始化、已证实输入事件和故障传播；缺失 capability 用例明确跳过/不宣称支持。
- [ ] 执行 target build；在单独授权后做屏幕图样和实际输入单项实测，不发起云端或动作。
- [ ] 返回板号、驱动版本、测试和观察证据、限制；失败交 A1 修复。

**依赖：** W4b 最小安全启动；该包可独立于其它 BSP 分次验收。

### W5b — A1：音频 BSP（Level 2→3）

- [ ] 核对 Watcher codec/麦克风/扬声器路径与官方 HAL，不假设接口和采样能力。
- [ ] 在 A1 ownership 封装采音/播音资源仲裁；不得声称仅 PCM 接口即具备唤醒/AEC/全双工。
- [ ] 增加真实存在的音频 contract 测试，注明 buffer/错误/资源冲突断言和执行命令。
- [ ] target build；获得独立明确授权后仅做本包规定的录放单项检查。
- [ ] 返回工具/配置、音频链路、实测观察和 Level 3 限制；不配网、不建重复云语音会话。

**依赖：** W4b；可与 W5a、W5c、W5d 分开派发。

### W5c — A1：云台/运动 BSP（Level 2→3）

- [ ] 核对板级电机/STM32 路径、资源 owner、机械限位和急停来源。
- [ ] 只通过 A1 motion owner 暴露受限动作/stop；不得由 A2/Host 绕过仲裁驱动。
- [ ] 运行 `tests/firmware_contract/test_desk_hardware_arbiter.py` 与 `tests/firmware_contract/test_desk_hardware_motion_fault_propagation.py`，断言仲裁、故障传播和停止。
- [ ] target build；获得单项动作的当次授权后，先空载/安全行程验证，再记录限位和 stop 结果。
- [ ] 返回驱动/板 revision/动作证据；ACK 仅 accepted，不代表物理到位；CR-002 未决不作确认承诺。

**依赖：** W4b；不要求其它 BSP 全部完成。

### W5d — A1：相机 BSP（Level 2→3）

- [ ] 依据 Watcher 板级证据接入单帧采集；不修改 A6 的 LAN 服务或页面。
- [ ] 运行/增加 A1 层采集、超时、释放和资源冲突测试，给出准确用例名称及断言。
- [ ] target build；经单独授权后采集一帧，记录尺寸/时间戳/成功或失败，不保存监控录像/音频。
- [ ] 返回 source path、测试结果、授权和单项硬件观察；不得称实时视频流。

**依赖：** W4b；独立于其他 BSP。

### W6 — A1：TClaw/Tuya 设备会话与语音/工具结果

- [ ] 在设备主框架内恢复真实 TClaw 对话路径，保持 Tuya 云语音在设备端；禁止 Python 再建云语音会话。
- [ ] 通过明确的用户操作完成 License/配网；凭据只使用安全配置途径，不写入仓库、日志或交付文档。
- [ ] 用现有 `tests/firmware_contract/test_tclaw_dialogue.py` 固定状态/错误断言；实际云会话需单独用户授权并记录脱敏证据。
- [ ] 验证一次真实语音请求、设备播报和工具结果回传各自状态；区分本地模型、云会话和模拟结果。
- [ ] 每项硬件依赖按 W5a–d 相应包验收，不因某 BSP 未完成伪报已接通。
- [ ] 返回固件 SHA、授权范围、对话证据、失败和恢复步骤；不得输出 license/token。

**依赖：** W4b、必要音频 BSP；Level 3 单项云会话不等于 G1。

### W7a — A2：官方 Runtime/Daemon/Application 唯一连接所有者

- [ ] 以 W0 锁定的官方 Runtime/Daemon 版本核验设备侧 SDK bridge 所需 wire/API 行为；不存在的接口标为 CR，不猜签名。
- [ ] A2 只改 `firmware/components/desk_sdk_bridge/` 与 `tests/sdk_contract/`，维持官方 Runtime/Daemon 唯一连接所有者；不实现 Host Application。
- [ ] 明确设备事件/命令/结果映射所需的官方接口证据；Host Application 与路由接线交 W7h。
- [ ] 跑 `tests/sdk_contract/test_official_sdk_runtime_compat.py`（外部 SDK 路径无效时如实说明）和 bridge contract 用例。
- [ ] 交付官方源码/API 引用、锁版本、设备协议映射和模拟/真实进程证据边界；不宣称真机已连通。

**依赖：** W0/W1；不得早于 A0 对接口缺口的裁决。

### W7b0 — A0：CR-001 配对语义裁决

**文件：** `docs/implementation/change-requests/CR-001-pairing-code-validation.md`、必要时 `docs/implementation/interfaces.md`。

- [ ] A0 与产品/官方 SDK 方裁决配对码来源、错误响应和 token 生命周期；不猜官方 wire/API。
- [ ] 未裁决时维持 fail-closed，不授权 A2 协议实现；写清决议、证据和兼容边界。
- [ ] 返回 CR 状态和后续 A1/A2 工作拆分；不操作设备。

**依赖：** W7a 的官方 wire 证据；不涉及物理配对。

### W7b1 — A2：按 CR-001 裁决实现设备侧配对

- [ ] 仅在 W7b0 正式裁决后改 `firmware/components/desk_sdk_bridge/` 与 `tests/sdk_contract/test_sdk_bridge_protocol.py`。
- [ ] 覆盖配对码/失败响应/token 语义，fail-closed 与敏感日志脱敏；运行 `tests.sdk_contract.test_sdk_bridge_protocol`。
- [ ] 不硬编码/记录/读出凭据；无裁决则仅报告未执行，不改 wire。
- [ ] 返回裁决引用、diff、测试命令/结果及真实配对是否另需授权。

**依赖：** W7b0；真实配对需要额外明确授权。

### W7b2 — A1：配对码生成/展示（条件工作包）

- [ ] 仅当 W7b0 选定设备端生成/屏幕展示方案时派发；否则不执行。
- [ ] 在 `firmware/components/desk_tclaw/` / W5a 冻结的显示路径实现 A1 所有的生成与显示，不改 bridge 或 Runtime 协议。
- [ ] 明确随机来源、码过期/刷新和显示错误测试；具体输入输出由 W7b0 决议冻结，不自行补字段。
- [ ] 返回 A1 文件 diff、测试和用户可观察证据；不显示/记录长期 Secret。

**依赖：** W7b0 明确采用展示方案；需要真实配对时另有用户授权。

### W7c0 — A0：CR-002 完成关联裁决

**文件：** `docs/implementation/change-requests/CR-002-motion-confirmation-correlation.md`、必要时 `docs/implementation/interfaces.md`。

- [ ] 产品/A0/官方 SDK 确认 request/operation/job 关联字段、完成事件来源和超时语义；不按 duration 推断物理到位。
- [ ] 明确没有传感器/硬件反馈时是否维持 accepted + `state_confirmed:false`，不得伪造 succeeded/STATE_UNCONFIRMED。
- [ ] 返回决议、准确字段归属与 A1/A2/A3 后续包；CR 未关闭时不实现协议。

### W7c1 — A2：设备协议关联字段

- [ ] 仅在 W7c0 正式冻结 wire 后修改 `firmware/components/desk_sdk_bridge/` 与 `tests/sdk_contract/test_sdk_bridge_protocol.py`。
- [ ] ACK 与完成事件按裁决的稳定 ID 关联，覆盖重复/迟到/未知事件；不做 Host 等待逻辑。
- [ ] 无 A1 物理完成源时，不伪造事件；按裁决保留 accepted 语义。
- [ ] 返回协议 diff、contract 测试、Level 2 与真机限制。

### W7c2 — A1：物理完成状态源（条件工作包）

- [ ] 仅当 W7c0 确认 Watcher 存在可读物理状态/完成传感器时派发；无硬件反馈则明确不执行。
- [ ] 在 `firmware/components/desk_hardware/` 与 `tests/firmware_contract/` 暴露真实 owner 状态；不得用延时结束冒充到位。
- [ ] 按 W5c 校准/安全流程验证观测值；有硬件操作需另有明确授权。
- [ ] 返回传感器/驱动来源、观测条件和 Level 3 证据；没有传感器就报告 accepted 限制。

### W7c3 — A3：Host 物理确认窗口

- [ ] 仅在 W7c0、W7c1 和需要时 W7c2 完成后修改 `host/desk_agent/` 对应 A3 路径及 `tests/core/test_motion_ack_semantics.py`。
- [ ] 使用冻结关联 ID 等待真实硬件反馈；不能只等 `duration_ms`。超时/断连/无传感器时按裁决输出 accepted/unknown。
- [ ] 覆盖迟到、重复、无关联完成事件和 stop；运行对应 core tests。
- [ ] 返回 Host diff、测试和真机确认边界；未确认时不得报 succeeded/completed。

### W7h — A3：最小官方受管 Application 与工具/反馈接线

**文件：** W0 产出的 A3 Host 路径映射（预期 `host/desk_agent/main.py` / `host/desk_agent/core/`）与 `tests/core/test_host_daemon.py`；W0 若证实路径不同，先冻结替代路径。

- [ ] 按 W0/W7a 已证实的官方 Runtime Application API 建立最小受管 Application，不猜未证实 SDK 签名。
- [ ] 接入最小事件/工具请求、Host dispatch、结果回传；错误/超时不伪造成功，不包含提醒/HA/相机/Codex业务功能。
- [ ] 新增 `tests/core/test_managed_application_bridge.py`，断言工具请求进入既有 Router、结果按原 request ID 回传、异常不生成 succeeded。
- [ ] 运行上述新测试与 `tests/core/test_host_daemon.py` 对应用例；无真实 Runtime 时只报 Level 2。
- [ ] 返回 A3 文件范围、官方 API 引用、测试命令/结果、模拟与真机边界。

**依赖：** W7a 的官方接口证据及 A0 契约确认；必须先于 WG1。

### W7d — A2：官方 capability/media/input/error/reconnect 兼容矩阵

- [ ] 按 W0 锁定 Runtime/SDK 版本逐项列出能力发现、媒体/事件、错误、断线重连支持和官方依据。
- [ ] 通过 A1 owner API 接入能力；保持唯一连接所有者和媒体资源仲裁。
- [ ] 运行 `tests/sdk_contract/test_sdk_bridge_protocol.py`、`tests/sdk_contract/test_wspk_framing.py`、`tests/sdk_contract/test_official_sdk_runtime_compat.py`，缺 SDK 路径时标阻断。
- [ ] 对缺 API/字段提交 CR，不增加未对账 wire 字段；分开标记 Level 1、模拟 Level 2 与真实设备 Level 3。
- [ ] 返回逐能力表、官方出处、测试和限制；不把 SDK 安装成功说成固件互通。

**依赖：** W7a、对应 A1 BSP；pairing 与 motion confirmation 仍受 W7b0/W7b1 与 W7c0/W7c1 的 CR 门控。

### WG1 — A7：真实 G1 同时连通验收

- [ ] 锁定本仓、TClaw、WatcheRobot embedded/Python SDK、Runtime/Daemon、固件及目标硬件版本与 dirty state。
- [ ] 在同一真实 Watcher 上证明一轮 Tuya 对话期间官方 Runtime/Application 同时在线，接收事件并执行一个受控硬件动作。
- [ ] 验证动作从 A1 仲裁 owner 执行；ACK、物理状态、完成事件和机器人反馈分开记录。
- [ ] 现有 `tests/e2e/test_g1_official_sdk_and_tuya_interop.py` 是 native C/fake-driver 模拟，只作 Level 2 回归，不作为 live 证据。
- [ ] 新增 live-only 工具 `tools/verification/verify_g1_live.py`：默认不连设备；只有显式开关和对应用户授权后才连接真实 Watcher、官方 Runtime/Application 与 Tuya 会话。
- [ ] live 证据须来自真实进程/设备日志及人工观察；禁止 FakeFirmware、注入 driver、mock 或模拟脚本证明 live。人工可观察受控动作，但 Host 不得伪报传感器确认。
- [ ] 真实 Tuya、配网/License、受控动作各需明确用户参与/授权；无授权仅审阅 live 工具和 L2 回归。
- [ ] 返回脱敏日志、操作人/主机/时间/版本/授权、故障和恢复；只有全部真实闭环才标 G1 / Level 4 子项通过。

**依赖：** W5b、W5c、W6、W7a、W7h、W7d；若官方配对路径需要则 W7b0/W7b1/W7b2 按裁决完成。W7c 系列仅在 Host 需承诺物理完成状态时为前置；人工观察可证明受控动作发生，但不能让 Host 伪报 confirmed。

### W8 — A3：受管 Application、生活服务、持久化与泰山派

- [ ] 在 A3 路径实现 Runtime 注入的受管 Application 路由，不建立第二云语音会话。
- [ ] 生活服务使用 SQLite 事务/outbox、同 request ID 重试幂等、开机恢复；测试 `tests/core/test_life_service.py`、`tests/core/test_host_daemon.py`。
- [ ] Windows 仅作临时联调；泰山派安装/配置需另授权，不安装全局依赖或启动常驻服务。
- [ ] 覆盖落库/播报崩溃、断 WAN、重启和重复提醒；节点不可达不得报已播报。
- [ ] 返回服务端版本、脱敏日志、恢复测试和宿主资源；Level 3 与长期部署分开。

**依赖：** W7h 最小 Application 后进入业务扩展；不依赖 WG1。若只做离线单测可独立派发。

### W9 — A4：Home Assistant 状态、白名单与真实确认

- [ ] 只在 `host/desk_agent/connectors/ha/` 和对应测试内操作，不改用户 HA 集成/实体/自动化。
- [ ] 校验认证、WebSocket 事件、允许服务和目标属性差异；真实 URL/token 通过安全配置，不读出/提交。
- [ ] 测试 `tests/connectors/test_ha_connector.py`：未授权服务拒绝、状态无变化不称完成、断连/重连状态更新。
- [ ] 真实家居动作必须就目标设备/动作另行明确授权，记录服务响应与之后观察到的实体状态。
- [ ] 返回测试结果、脱敏日志和节点/物理证据等级。

**依赖：** A3 Runtime Application 路由稳定；真实动作另有授权。

### W10 — A5：Windows Codex Companion 真任务与恢复

- [ ] 只改 `companion/`、`deploy/windows/` 和 A5 测试；不暴露 app-server 网络端口。
- [ ] 按当前 app-server stdio JSON-RPC handshake、thread/turn/审批/取消/恢复契约实现；不承诺控制桌面正在进行的人工聊天。
- [ ] 运行 `tests/companion/test_codex_companion.py` 与 `tests/companion/test_companion_startup_security.py`，断言默认回环、跨机强 token、审批绑定真实请求。
- [ ] 真实 Codex 工程任务需另行明确授权；app-server turn 完成不等于工程改动已验证。
- [ ] 返回 Windows 主机/版本、测试、脱敏任务轨迹和重启恢复证据。

**依赖：** Companion 可独立开发；进入后续 W12 整机验收依赖 A3 路由。

### W11 — A6：相机 LAN 单帧、租约和管理页

- [ ] 只改 `host/desk_agent/camera/`、`host/desk_agent/web/` 与 A6 测试，不改底层相机驱动、不绕开 motion owner。
- [ ] 用 W5d 提供的真实单帧 source；帧带时间/来源，租约关闭后停止采集并遮盖旧帧。
- [ ] 运行 `tests/camera/test_camera_service.py`；验证请求失败、陈旧帧、到期释放、手动云台仲裁。
- [ ] 页面不得提供录像/回放/监控音频；LAN 访问和真实相机单项另需明确授权。
- [ ] 返回单帧、时间戳/租约与来源证据；不称实时视频流。

**依赖：** W5d；LAN/整机能力归入后续 A7。

### W12 — A7：部署、故障注入、长稳与验收矩阵

- [ ] 只改 `tests/e2e/`、`tools/verification/`、`docs/acceptance/`；生产缺陷交对应 owner。
- [ ] 固定软硬件版本、配置和 Level 证据；运行现有 `tests/e2e/test_e2e_fault_injection.py`、`test_system_stability_stress.py`、`tools/verification/verify_all.sh` 中适用阶段。
- [ ] 逐项覆盖断网、进程/设备重启、重复请求、关闭 LAN 页面、PC 关闭与泰山派恢复；标出真实环境缺口。
- [ ] 长稳时长按总管家/产品另行确定，不从演示时长推定性能指标；任何耗时门槛先经 A0/产品裁决。
- [ ] 更新验收矩阵仅基于可复现日志/版本/操作条件，不把 Level 1/2 写成 Level 3/4。
- [ ] 返回失败归属、重现步骤、故障恢复结果和无法验证事项；不直接跨模块改生产代码。

**依赖：** WG1、W8–W11 的单链路验收；缺模块可先跑局部故障测试，但不得宣称 G2/G3/G4 完成。

## 后续关口

- **首轮停止点：** W0→W1→W2 顺序完成后交总管家审查；未收到新派发前，不运行 W3 或任何硬件操作。
- **首次上电：** W3 分区/恢复核验 → W3b（需要时）→ A0 W4a 合构建审镜像 → A1 W4b 明确授权的安全启动。首次上电不等于任何 BSP 或 G1 完成。
- **G1：** WG1 要求真实 Tuya + 官方 Runtime/Application 同时连接 + 受控动作和可信反馈。
- **G2/G3/G4：** A3/A4/A5/A6 单链路和部署恢复逐步通过后，由 A7 做联调、故障恢复与验收证据矩阵；任何跨模块缺陷回到其 owner。

## 产品需求覆盖

| 用户需求 | 工作包 |
|---|---|
| 直接讲话、Tuya 对话与日常陪伴 | W5b、W6、W7a/W7d、WG1 |
| 表情与受限动作 | W5a、W5c、W6、W7d、WG1 |
| 语音派发 Codex、进度/结果播报 | W6、W8、W10、WG1 |
| HA 家居状态与受限控制 | W9 |
| 一次/重复提醒、闹钟、贪睡与取消 | W8 |
| 备忘 CRUD 与持久化 | W8 |
| LAN 真实单帧、手动云台 | W5c、W5d、W11 |
| Windows 联调、泰山派部署、断网/重启恢复 | W0、W8、W10、W12 |
