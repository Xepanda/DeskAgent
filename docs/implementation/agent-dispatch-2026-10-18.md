> 历史阶段资料：当前状态、已验证范围与剩余风险以 [2026-10-19 交付文档](handoff-2026-10-19.md) 为准；本文件不构成新的执行授权或整体验收通过证明。

# Windows Watcher Bring-up 工作包派发卡

本文件供用户在后续工具中复制派发。它只包含计划，不授权执行。每次只发一个工作包给一个执行者；验收后由总管家决定下一包。首轮只发 **W0 → W0e（需要时）→ W1 → W2**；W2 回报后停止，等待新的硬件任务授权。

详细架构、依赖、测试和关口见 [`2026-10-18-windows-watcher-bringup.md`](../superpowers/plans/2026-10-18-windows-watcher-bringup.md)。跨主机派发时，先把两份计划文档及对应代码版本同步给执行者；不能假设另一台机器已有相同工作区。

## 所有工作包的统一派发约束

复制时将本节约束和一张单包派发卡一起发送。单包范围内的本地代码修改/局部测试已由派发授权；下文单列的设备/外部动作授权默认没有。

- 开始前返回主机/OS、仓库绝对路径、Git SHA、dirty 状态；不匹配时停止并报告，不覆盖用户文件。
- 遵守 A0–A7 ownership。一次一个包；A1 独占并集成设备启动/板级文件；A2 仅 SDK bridge 接口/patch；A0 仅公共 CMake、组件注册、依赖与镜像重建，不合入 A1 私有入口。
- 不执行 reset/clean/强制切分支，不全局安装依赖，不读写生产凭据。本计划不授予烧录权限，仅 W4b 等具体获授权设备包可在范围内烧录，其它包禁止；本地代码/单元测试按包授权执行。公开官方文档/GitHub 可只读调研；真实业务云会话、设备控制、云写操作按包需明确授权。本轮两个规划文件为授权新增未提交文件，W0 应保留并记录；跨机执行者先同步两文档，GitHub main 尚不含它们。本轮两份文档不 commit；后续实现包可在核定的 `md/` 分支本地 commit 交接，但不得自动 push/建远端分支/PR/merge。
- Watcher USB 在当前 Windows；COM20/COM21 角色未知，除端口枚举外，串口、复位、读取、烧录均需对应任务的明确授权。远端 Linux 不得声称能操作本机 COM。
- 不打开串口、不烧录、不配网、不访问云、不做家居动作、不跑真实 Codex 任务，除非当前单包明确写明已获授权范围。
- 交付变更文件和 diff、实际运行命令及结果、证据等级、未决项和权限范围；未运行写明原因，不以空泛“完成”代替证据。
- 不泄露任何 token、配对码、License、Wi-Fi/HA/云凭据；日志先脱敏。ACK/SDK安装/host构建不能证明物理完成或端到端。
- 主仓默认按顺序执行，不自动创建多份 review worktree；远端执行者须先确认工作区已同步且共享，否则返回补丁，不得声称已修改本机文件。


## 路径 Ownership 裁决（本计划专用）

| 路径 | Owner | 边界 |
|---|---|---|
| `host/desk_agent/main.py`、必要 Host Runtime bridge、`host/desk_agent/connectors/robot/` | A3 | 仅路由/连接迁移；不重写 HA、Codex、相机模块 |
| `host/desk_agent/web/` | A6 | Web 管理页面与 LAN 相机交互 |
| `firmware/main/src/app_main.c` | A1 | 设备启动入口及实际板级启动集成 |
| 公共 CMake、组件注册、依赖与镜像重建 | A0 | 不编辑/合入 A1 私有启动入口或板级文件 |
## 单包提示词

下列每项单独复制派发，不要把多个包合给一个执行者。设备/外部动作授权字段默认“无”；只有用户对该单项另作明确授权时才可改写。

### W0 — A0 资产/接口复核（首轮第 1 包）

> **目标：** 只读复核 Windows 上 SenseCAP Watcher bring-up 的真实工程、版本、接口和测试基线；允许查公开官方资料/GitHub 源码与版本，不做真实业务云会话/设备控制/写操作。项目 `D:\ePanda\DeskAgent`；基线预期 SHA `9472d70879ba027aa61c96aa5c73e0de9cb83f99`，已合 PR11。读取本计划、AGENTS、系统计划、remediation 计划、handoff、dependency_lock、interfaces、firmware 构建入口及关键测试；重查当前事实，不把 10/17 快照当当前结论。
>
> **范围：** 只读本仓/实际外部 SDK 源码和工作区状态、Windows toolchain/CLI、COM 枚举；不建立串口会话。
>
> **禁止：** 不改文件、不安装依赖、不读 chip ID/NVS/Flash/凭据，不复位/烧录/配网或做真实业务云会话/设备控制，不 commit/push。可查公开官方资料/GitHub源码。
>
> **验收：** 报告主机/OS、绝对路径、SHA、dirty；锁定 TClaw/Watcher/官方 SDK 版本和实路径，查明 target build 前置条件、端口硬件身份但不猜角色、当前测试命令、TClaw Watcher 支持证据和缺口。交连接契约表：每条链路列客户端/服务端、源码证明的实际协议、配对/鉴权来源、控制/媒体/事件通道、唯一连接 owner、官方文件+行或锁定版本；重点核对自定义 TCP 与官方 Runtime 的兼容性。不能确认就交 A0 CR。
>
> **返回证据：** 关键文件/行、命令及只读输出摘要、版本表、Level 1/2 边界、未知项和逐包建议。
>
> **设备/外部动作授权：无。** 用户仅已允许端口枚举；本包无代码修改授权。

### W0e — A0 项目局部验证环境准备（仅 W0 证明环境缺失时）

> **目标：** 为 W1 原生 C bridge 测试准备最小、可复现的本地验证环境。W0 已有可用环境则跳过。
>
> **范围：** 只使用现有 IDF/SDK 激活方式、项目局部 Python venv/依赖和经确认可用的 C compiler；不改业务代码或系统级 PATH。
>
> **禁止：** 不全局安装/重装 SDK，不擅自下载大型工具链，不碰硬件/云；大下载先列来源、大小、方案并等待用户决定。
>
> **验收：** 报告 host compiler、Python/依赖及激活命令；运行 repo 既有 bridge native test 的单项 smoke，记录源码 SHA 和环境路径。环境无法在本地合规准备则提交阻断项。
>
> **返回证据：** 主机/OS、SHA、局部环境目录、版本、命令/退出码、是否有外部下载。
>
> **设备/外部动作授权：无。** 仅限项目局部环境，不含系统级安装或大下载。


### W1 — A2 顶层 type 路由（首轮第 2 包）

> **目标：** 修复 `desk_sdk_bridge_process_command()` 对整段 JSON 使用 `strstr` 的分派风险。输入 `type=tool.response`、`note.content="motion.stop"` 必须保持工具响应路径，不能调用设备 stop；嵌套字段含 `sys.ping`/`motion.move`/`camera.acquire` 也不能误路由。真实顶层设备 type 仍正常。
>
> **范围：** 仅 `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c` 和 `tests/sdk_contract/test_sdk_bridge_protocol.py`。按顶层 type 精确分派；如现有 parser 不足，A2 可在本包内部实现并测试 parser helper；只有涉及公共依赖、wire 字段或跨模块接口才交 A0。
>
> **禁止：** 不改 A1/A0/Host 代码、不改 wire 契约、不操作硬件/云，不扩大格式化范围。
>
> **验收：** 增加 `test_tool_response_note_motion_stop_is_not_dispatched_as_command`、嵌套关键字和合法设备命令回归；在 W0 选定 POSIX/Linux native C 环境、仓根和相同源码 SHA 下运行 `PYTHONPATH=. python3 -m unittest tests.sdk_contract.test_sdk_bridge_protocol`。不能假设 MSVC 可运行 POSIX C tests。
>
> **返回证据：** diff、测试名和断言、命令/退出码、失败项、Level 2 声明、文件范围检查。
>
> **设备/外部动作授权：无。** 本包范围内代码修改和本地测试已由派发授权。

### W2 — A0 target build 与镜像清单（首轮第 3 包；之后停止）

> **目标：** 基于 W0 确认的官方 Watcher/TClaw 工程接入本仓组件并完成真实 ESP32-S3 target build。当前 standalone `desk_agent_firmware_test` 是 host executable，不能当固件烧录。
>
> **范围：** 仅 A0 公共 CMake/组件注册/依赖清单；A1 可提供板级 patch，由 A0 合公共入口。具体文件由 W0 冻结。
>
> **禁止：** 不烧录、不访问 COM/云、不擦写 Flash、不把有默认 stub 的现入口称为可烧录镜像，不换别板或移除 TClaw，不全局安装依赖。
>
> **验收：** 报告 target、IDF/TuyaOpen/toolchain 版本和配置来源；先审连接契约与官方 Runtime 兼容路线，再运行真实 target build；输出 bootloader/partition/app 镜像路径、大小、SHA256、偏移和 build 日志摘要。若 TClaw 无 Watcher 支持或 wire 不兼容，提交带源码证据的 A0 CR 并停止。
>
> **返回证据：** diff、实际命令/退出码、镜像清单、编译与 host/mock 证据区分、阻断和风险。
>
> **设备/外部动作授权：无。** 本包代码/构建已授权；不含烧录授权，W2 产物也不等于烧录资格。

### W3 — A1 原机/分区/恢复信息

> **目标：** 为未来首次安全启动建立可审阅的原机状态、设备分区和恢复路线。
>
> **范围：** 仅当前 Windows 的只读板级资料/获授权安全识别；单一 A1 硬件执行者。COM20/21 可枚举但角色须证据确认。
>
> **禁止：** 默认不读 Flash、NVS、efuse、chip ID 或任何可能含凭据的整片备份；不复位、不擦写、不烧录、不配网、不访问云。敏感读取/复位/擦写必须有单独、具体、当次授权，且不输出凭据内容。
>
> **验收：** 说明实物/板修订、分区布局依据、原机基线、恢复步骤和不能确认项；仅文件 `partitions.csv` 不能证明设备当前布局。恢复路线不成立即停在 W4b 前。
>
> **返回证据：** 主机/OS、仓库路径/SHA、授权原文范围、设备识别来源、脱敏日志、风险和未执行项。
>
> **设备/外部动作授权：默认无；用户明确补充后逐项填写，不可从计划推定。**

### W3b — A1 最小安全启动 patch（需要时）

> **目标：** 若当前启动入口不满足安全首启，提交最小 patch：有可观察启动日志/心跳、无自动云台动作、未连接时不伪报 Tuya 会话成功。
>
> **范围：** `firmware/main/src/app_main.c` 与 W0 冻结的 A1 板级路径及 A1 contract 测试；A1 修改并集成自己的文件。
>
> **禁止：** 不改 A2 bridge/Host/公共构建注册，不做配网/真实云会话、不接触硬件、不烧录。
>
> **验收：** 在 `tests/firmware_contract/test_safe_startup_contract.py`（新建）断言 boot 不下发 motion、stub 不报 cloud connected；运行 native contract test 并交 A1 已集成变更与风险。W3b 仅依赖 W0 冻结路径，不依赖 W2/W3；若 W2 被最小启动胶水阻塞，A1 集成后交 A0 重试 W2。完整板级适配须 W0 另列 A1 包。
>
> **返回证据：** diff、测试结果、startup code path 和供 A0 集成的文件清单。
>
> **设备/外部动作授权：无。**

### W4a — A0 最终镜像集成与审查

> **目标：** 仅处理公共构建项并重建最终可审镜像；A1 已自行集成安全启动文件。
>
> **范围：** A0 仅处理公共 CMake/组件注册/依赖及镜像重建；不编辑或合入 A1 私有启动/板级文件。
>
> **禁止：** 不烧录、不操作 COM、不使用 host executable 冒充固件、不默许默认 stub 镜像可烧录、不读写凭据。
>
> **验收：** 前置 W1、W2（若 W3b 触发则由 A1 集成后 A0 重试）、W3 和必要 W3b；重建 bootloader/partition/app 并审 SHA/偏移、启动源码与 contract 证据：启动日志/心跳、无自动云台、stub 不伪报 cloud connected。
>
> **返回证据：** A0 合入文件、目标/镜像 SHA、构建命令、镜像审查结果和限制。
>
> **设备/外部动作授权：无。** 本包只构建和审查，不包含物理启动授权。

### W4b — A1 首次最小安全启动

> **目标：** 只在核准镜像上验证启动日志/心跳、无自动云台动作、未连接时无伪造 Tuya 成功。
>
> **范围：** 当前 Windows 唯一硬件执行者操作核准板卡，不改代码。
>
> **禁止：** 不使用 host executable/default stub，不默认为全片擦除；不读写 NVS/工厂凭据，不配网/云请求/真实动作。任何复位擦写须在本包明确授权范围。
>
> **验收：** W4a 最终 image 审查、W3 分区/恢复、端口角色证据和当次明确烧录授权齐备；仅写核准范围并采集脱敏启动日志。
>
> **返回证据：** 主机/端口、镜像 SHA/偏移、授权范围、命令、心跳日志、动作/云状态观察、异常和恢复。
>
> **设备/外部动作授权：默认无；必须逐项写明目标板、写入及复位/擦写范围。**

### W5a — A1 显示与实际输入 BSP

> **目标：** 在目标 Watcher 上验证显示及 W0 官方板级证据确认的实际输入（旋钮/按键/触摸）。未证实的输入 capability 不实现、不假测。
>
> **范围：** `firmware/components/desk_hardware/`、W0 冻结的 A1 显示/输入文件、`tests/firmware_contract/test_watcher_display_input.py`（新建）。
>
> **禁止：** 不猜板修订/GPIO，不跨入 A2/Host、不绕仲裁；不测 W0 未证实的触摸/旋钮/按键。
>
> **验收：** 测试已证实输入事件、显示初始化和故障传播；target build；单项屏幕/输入实测须另获授权。
>
> **返回证据：** 板修订来源、input capability、diff、测试/build、授权和 Level 2/3 边界。
>
> **设备/外部动作授权：默认无；屏幕/输入硬件实测另行明确授权。**

### W5b — A1 音频 BSP

> **目标：** 核验并接通 Watcher 的真实麦克风/codec/扬声器路径与资源仲裁。
>
> **范围：** `firmware/components/desk_hardware/`、W0 冻结的 A1 audio driver 文件、`tests/firmware_contract/test_watcher_audio.py`（新建）。
>
> **禁止：** 不猜接口；不跨 owner；不得把 PCM 接口说成唤醒/AEC/全双工，不新增云语音会话。
>
> **验收：** contract 测试断言初始化、录放调用错误/资源冲突；target build。单项录放实测须逐项授权。
>
> **返回证据：** codec/HAL 来源、diff、测试/build、单项观察和 L2/L3 边界。
>
> **设备/外部动作授权：默认无；真实采音/播放另行明确授权。**

### W5c — A1 云台/STM32 BSP

> **目标：** 在 A1 motion owner 内接入 Watcher 受限云台/STM32 驱动和停止行为。
>
> **范围：** `firmware/components/desk_hardware/`、W0 冻结的 motion/UART 文件、`tests/firmware_contract/test_desk_hardware_arbiter.py`、`test_desk_hardware_motion_fault_propagation.py`。
>
> **禁止：** 不猜 UART/GPIO，不绕仲裁，不将 ACK/duration 当物理到位。
>
> **验收：** 对应测试断言仲裁/故障/stop；target build。实际动作先核行程/限位并取得单项授权。
>
> **返回证据：** 板/STM32协议来源、diff、测试/build、授权和 Level 3 实测观察。
>
> **设备/外部动作授权：默认无；真实云台动作另行明确授权。**

### W5d — A1 相机 BSP

> **目标：** 验证 Watcher 相机单帧 capture、释放和故障路径。
>
> **范围：** `firmware/components/desk_hardware/`、W0 冻结的 A1 camera driver 文件、`tests/firmware_contract/test_watcher_camera.py`（新建）。
>
> **禁止：** 不改 A6 LAN 服务，不录像/回放/采集音频，不将单帧说成视频流。
>
> **验收：** 测试断言单帧、超时、释放和资源冲突；target build。真实拍摄另需明确授权。
>
> **返回证据：** 相机 HAL 来源、diff、测试/build、单帧观察和 Level 2/3 限制。
>
> **设备/外部动作授权：默认无；真实相机采集另行明确授权。**

### W6 — A1 TClaw/Tuya 会话与工具结果

> **目标：** 保留 TClaw 为设备主框架，验证真实设备语音、Tuya 会话、播报和工具结果回传。
>
> **范围：** 仅 A1 固件/板级路径和测试；按已验收 BSP 调用 A1 接口。用户亲自完成需要 License/配网的步骤。
>
> **禁止：** 不用 Python 建第二个云语音会话，不写/打印 License/Secret，不把 stub/mock 当真实会话；未授权不配网/访问云。
>
> **验收：** 用 `tests/firmware_contract/test_tclaw_dialogue.py` 验状态/错误；真实语音/云端必须有明确授权和脱敏会话证据，并区分请求、播报、工具结果。
>
> **返回证据：** 固件 SHA、执行环境、授权范围、测试与真实会话的分别结果、失败/恢复；不含凭据。
>
> **设备/外部动作授权：默认无；真实配网、License、云会话另列具体授权。**

### W7a — A2 官方设备 bridge/runtime 兼容

> **目标：** 只核验并实现固件 SDK bridge 与 W0 锁定官方 Runtime/Daemon 的 wire 兼容；官方 Runtime/Daemon 是唯一连接所有者。
>
> **范围：** `firmware/components/desk_sdk_bridge/`、`tests/sdk_contract/test_sdk_bridge_protocol.py`、`tests/sdk_contract/test_official_sdk_runtime_compat.py`。
>
> **禁止：** 不实现 Host Application，不自建第二 Runtime，不猜 SDK API/私造字段，不绕 A1 硬件 owner。
>
> **验收：** 用官方源码/版本引用证明通道和字段；跑对应已有 contract tests。外部 SDK 路径不通则记录阻断，不宣称真机互通。
>
> **返回证据：** 文件 diff、官方版本/文件行、测试命令/结果、模拟与真机边界。
>
> **设备/外部动作授权：无。** 本地代码修改和测试已由此卡授权。

### W7b0 — A0 CR-001 配对语义裁决

> **目标：** 裁决设备侧期望配对码来源、失败响应、token 生命周期并更新 CR-001。
>
> **范围：** `docs/implementation/change-requests/CR-001-pairing-code-validation.md`、必要时 `docs/implementation/interfaces.md`。
>
> **禁止：** 不实现设备/Host代码，不猜官方 wire/API，不操作真机或读取凭据。
>
> **验收：** 引用官方文件/版本，给出唯一兼容裁决或保持 OPEN 并列出阻断；需要屏幕显示时单独触发 W7b2。
>
> **返回证据：** CR diff、来源行/版本、裁决与后续单 owner 包。
>
> **设备/外部动作授权：无。**

### W7b1 — A2 按 CR-001 实现配对协议

> **目标：** 仅按已关闭 CR-001 实现设备侧 pairing wire 校验和响应。
>
> **范围：** `firmware/components/desk_sdk_bridge/`、`tests/sdk_contract/test_sdk_bridge_protocol.py` 中 pairing 用例。
>
> **禁止：** CR 未关闭不改协议；不硬编码/记录/读取配对码或 token；不接触真机配对。
>
> **验收：** 覆盖缺码、错码、格式错误、fail-closed/裁决的失败响应、敏感日志脱敏；跑 `python -m unittest tests.sdk_contract.test_sdk_bridge_protocol`。
>
> **返回证据：** CR 引用、diff、测试命令/结果和 Level 2 边界。
>
> **设备/外部动作授权：无。** 真实配对另需明确授权。

### W7b2 — A1 配对码生成/展示（条件包）

> **目标：** 仅在 W7b0 裁决要求设备端生成/显示时，实现配对码的安全来源和用户可读展示。
>
> **范围：** `firmware/components/desk_tclaw/`、W5a 冻结的显示文件、`tests/firmware_contract/test_pairing_code_display.py`（新建）。
>
> **禁止：** W7b0 未要求则跳过；不改 bridge wire，不记录长期 Secret，不猜输入/过期规则。
>
> **验收：** 按裁决测试码来源/过期/展示；target build。实机显示/配对另需明确授权。
>
> **返回证据：** 裁决引用、diff、测试/build、脱敏观察。
>
> **设备/外部动作授权：默认无；实机配对另行明确授权。**

### W7c0 — A0 CR-002 运动确认契约裁决

> **目标：** 裁决 ACK/完成事件关联字段、物理反馈来源和 Host 超时语义并更新 CR-002。
>
> **范围：** `docs/implementation/change-requests/CR-002-motion-confirmation-correlation.md`、必要时 `docs/implementation/interfaces.md`。
>
> **禁止：** 不改 A1/A2/A3 代码，不按 duration 推断物理到位，不自造字段。
>
> **验收：** 证据说明软件序列完成与传感器/硬件反馈的区别；无物理反馈时明确维持 accepted/false。
>
> **返回证据：** CR diff、字段/责任 owner、条件工作包和未决问题。
>
> **设备/外部动作授权：无。**

### W7c1 — A2 实现冻结的设备关联协议

> **目标：** 按已关闭 CR-002 在设备 ACK/完成事件中使用冻结关联字段。
>
> **范围：** `firmware/components/desk_sdk_bridge/`、`tests/sdk_contract/test_sdk_bridge_protocol.py`。
>
> **禁止：** CR 未关闭不改 wire；不生成没有 A1 实际反馈来源支撑的完成事件。
>
> **验收：** 覆盖关联、重复/迟到/未知事件；运行 SDK bridge contract tests。软件 duration 结束不能标物理到位。
>
> **返回证据：** CR 引用、diff、测试结果、Level 2/3 边界。
>
> **设备/外部动作授权：无。**

### W7c2 — A1 提供真实物理完成状态（条件包）

> **目标：** 仅当 CR-002 证明板上有可读传感器/物理状态来源时，提供 A1 所有的真实完成反馈。
>
> **范围：** `firmware/components/desk_hardware/`、`tests/firmware_contract/test_watcher_motion_feedback.py`（新建）。
>
> **禁止：** 无实际传感器/状态源就跳过；不以命令回执或 delay 假造到位。
>
> **验收：** contract test 验证读数/故障；硬件标定需另授权并提供 Level 3 记录。
>
> **返回证据：** 传感器来源、diff、测试、实测和无反馈时的限制。
>
> **设备/外部动作授权：默认无；动作/标定另行明确授权。**

### W7c3 — A3 实现 Host 确认窗口

> **目标：** 只按 W7c0 定义及 W7c1/需用时 W7c2 的真实关联实现 Host 等待/状态语义。
>
> **范围：** W0 冻结的 A3 Host 路径、`tests/core/test_motion_ack_semantics.py`。
>
> **禁止：** 不跨 A2/A1 修改，不按 duration 当物理完成；无传感器反馈不得报 confirmed/succeeded。
>
> **验收：** 覆盖迟到/重复/无关联事件/stop/断连；无反馈时按裁决留在 accepted 或 unknown。
>
> **返回证据：** diff、测试命令/结果、CR 引用和真实反馈边界。
>
> **设备/外部动作授权：无。**

### W7d — A2 capability/media/input/error/reconnect 矩阵

> **目标：** 记录官方 Runtime 实际支持的能力与 bridge 对接状态。
>
> **范围：** `firmware/components/desk_sdk_bridge/`、`tests/sdk_contract/`。
>
> **禁止：** 不推测未文档化 API/字段，不绕 A1 资源 owner，不将 SDK 安装说成固件互通。
>
> **验收：** 跑 `test_sdk_bridge_protocol.py`、`test_wspk_framing.py`、`test_official_sdk_runtime_compat.py` 对应项；缺路径如实报告。
>
> **返回证据：** 能力表、官方文件/行或版本、diff、测试与 Level 1/2/3 区分。
>
> **设备/外部动作授权：无。**

### W7h — A3 最小官方受管 Application

> **目标：** 在真实官方 API 证据基础上接入最小受管 Application、工具请求到 Router、结果原 request ID 回传；这是 WG1 前置，不含生活服务/HA/Codex/相机。
>
> **范围：** 按本计划 Ownership 表：`host/desk_agent/main.py`、必要 Host Runtime bridge、当前 `host/desk_agent/connectors/robot/`（仅路由/连接迁移；不重写 HA/Codex/相机）、`tests/core/test_managed_application_bridge.py`（新建）。
>
> **禁止：** 不猜 SDK 签名，不由 A2 代写 Host，不增加其他业务功能，不伪造 succeeded。
>
> **验收：** 测试断言受管入口、Router dispatch、request ID 关联和失败透传；跑新测试及 `tests/core/test_host_daemon.py` 对应用例。
>
> **返回证据：** 官方 API 来源、路径映射、diff、命令/结果、L2 与真实 Runtime 边界。
>
> **设备/外部动作授权：无。**

### WG1 — A7 真实 G1（Live only）

> **目标：** 证明同一 Watcher 上真实 TClaw/Tuya 对话期间官方 Runtime/Application 在线、接收事件并执行一个受控动作，随后有可信状态与机器人反馈。
>
> **范围：** `tests/e2e/`、`tools/verification/verify_g1_live.py`（新建）、`docs/acceptance/`；A7 不改生产代码，缺陷交 owner。
>
> **禁止：** 现有 `tests/e2e/test_g1_official_sdk_and_tuya_interop.py` 是 native C/fake-driver 模拟，只能作 L2 回归；禁止 FakeFirmware/注入 driver/mock/模拟脚本证明 live。不得未经授权配网、做真实动作或改远端服务。
>
> **验收：** 新增 live-only `verify_g1_live.py`，默认不开设备连接，只有显式启用且授权后才连真实 Watcher、官方 Runtime/Application 与 Tuya。真实进程日志+人工动作观察证明闭环；手工观察不能让 Host 伪报 `state_confirmed`。现有模拟测试只列为 L2 前置。
>
> **返回证据：** 版本/dirty、执行主机、明确授权、脱敏日志、动作和状态观察、故障/恢复、证据等级。
>
> **设备/外部动作授权：默认无；Tuya/配网/受控动作分项具体授权。**

### W8 — A3 受管 Application、生活服务与泰山派

> **目标：** 实现 A3 ownership 内的路由、SQLite 事务/outbox、幂等提醒和恢复；最终常驻宿主为泰山派，Windows 只临时联调。
>
> **范围：** `host/desk_agent/core/`、`services/`、`deploy/edge/` 和 A3 测试。
>
> **禁止：** 不代写 HA/Codex/相机，不增加云语音，不未经授权安装全局依赖/服务或访问真实家居。
>
> **验收：** 运行 `tests/core/test_life_service.py`、`tests/core/test_host_daemon.py`；断言崩溃恢复、同 ID 幂等和无假播报。泰山派部署另行授权。
>
> **返回证据：** diff、命令/测试、持久化恢复、宿主/配置、权限和 Level 限制。
>
> **设备/外部动作授权：默认无；泰山派安装/常驻服务需另行授权。**

### W9 — A4 Home Assistant

> **目标：** 在 A4 connector 内实现状态/事件、受限服务白名单和观察到的目标状态确认。
>
> **范围：** `host/desk_agent/connectors/ha/` 与 A4 测试。
>
> **禁止：** 不改用户现有 HA 集成/实体/自动化，不提交 token，不把 HTTP 200 当物理执行。
>
> **验收：** 跑 `tests/connectors/test_ha_connector.py`；真实动作只有逐设备/动作明确授权后执行，并比对后续 state。
>
> **返回证据：** diff、测试、脱敏响应和实际状态观察、未验证项。
>
> **设备/外部动作授权：默认无；真实家居动作逐项授权。**

### W10 — A5 Windows Codex Companion

> **目标：** 验证 Windows Companion 通过 stdio JSON-RPC 管理已授权工程中的 thread/turn/审批/取消/恢复。
>
> **范围：** `companion/`、`deploy/windows/` 和 A5 测试。
>
> **禁止：** 不暴露 app-server 网络端口，不宣称能接管用户活跃桌面聊天，不未经授权提交真实工程任务。
>
> **验收：** 从仓根用 `& 'C:/Users/maida/.cache/codex-runtimes/codex-primary-runtime/dependencies/python/python.exe' -B -m unittest tests.companion.test_codex_companion tests.companion.test_companion_startup_security`；若固定 Python 无 requirements，则用 W0e 固定的项目局部环境运行相同用例。
>
> **返回证据：** Windows/版本、diff、测试结果、脱敏任务生命周期和重启恢复。
>
> **设备/外部动作授权：默认无；真实 Codex 工程任务单独授权。**

### W11 — A6 相机与管理页面

> **目标：** 提供受租约管理的 LAN 单帧 JPEG 与手动云台页面。
>
> **范围：** `host/desk_agent/camera/`、`host/desk_agent/web/` 和 A6 测试；底层相机驱动由 A1 所有。
>
> **禁止：** 不录像/回放/收音、不绕 motion owner、不把单帧说成实时视频。
>
> **验收：** 跑 `tests/camera/test_camera_service.py`；测试帧时效、过期停止、释放、错误和 motion owner。
>
> **返回证据：** diff、测试、单帧时间戳/租约/来源、LAN 授权与限制。
>
> **设备/外部动作授权：默认无；真实相机/LAN 访问另行授权。**

### W12 — A7 部署恢复、长稳与验收矩阵

> **目标：** 汇总 G2–G4 场景和证据；对断网、关机、重启、重复请求、页面关闭、长稳及恢复做端到端验证。
>
> **范围：** `tests/e2e/`、`tools/verification/`、`docs/acceptance/`；不直接改生产模块。
>
> **禁止：** 不自定未经裁决的时长/性能数字，不把 Level 1/2 当真机证据，不隐瞒失败。
>
> **验收：** 运行适用的 `tests/e2e/test_e2e_fault_injection.py`、`test_system_stability_stress.py` 和 `tools/verification/verify_all.sh` 阶段；长稳条件由总管家/产品先确定。
>
> **返回证据：** 完整版本基线、命令/日志摘要、故障恢复结果、矩阵变化、失败归属和仍缺的 Level 3/4 证据。
>
> **设备/外部动作授权：默认无；任何设备/云/家居外部动作按项授权。**

## 派发顺序摘要

| 阶段 | 只派发 | 通过后 | 停止条件 |
|---|---|---|---|
| 首轮 | W0 → W0e（需要时）→ W1 → W2，逐个派 | W2 由总管家审查 | 停在硬件授权前；不自动派 W3 |
| 安全首启 | W3 与 W3b 可并行准备；若 W2 被最小启动胶水阻塞则触发 W3b，A1 集成后 A0 重试 W2；W4a → W4b 顺序执行 | W4b 最小启动 Level 3 | 分区/恢复/镜像/授权任一缺失即停 |
| 板级和连接 | W5a–d 单独派；W6；W7a、W7b0→b1/b2、W7c0→c1/c2→c3、W7d、W7h | 单模块证据和 CR 裁决 | 失败交对应 owner，不能假报 G1 |
| 系统闭环 | WG1；W8–W11；W12 | 按 G1–G4 审查 | 缺真实链路、授权或恢复证据则保留未完成 |

未决 CR：CR-001 配对码来源/失败响应/token 生命周期；CR-002 运动 ACK→物理完成关联与窗口。只有获得正式裁决后才可实施相应协议变更。
