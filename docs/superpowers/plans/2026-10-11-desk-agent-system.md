# Desk Agent R1 系统集成与多 Agent 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development or superpowers:executing-plans to implement this plan task-by-task. 用户将自行派发各工作包；本文件只定义任务，不代表授权立即执行。

**Goal:** 在 WatcheRobot 上运行 TClaw，接入全部目标硬件与 Tuya 官方云端对话，兼容官方 Python SDK，经常驻 Python 服务连接 HA、生活服务和 Windows Codex。

**Architecture:** TClaw 是机器人端主框架；硬件驱动、SDK 命令和 TClaw 工具共享资源仲裁。官方 SDK/Daemon 承担外部设备连接，Python Application 负责 HA、Codex、提醒备忘和局域网查看桥接；Windows Companion 管理本机 Codex。

**Tech Stack:** TClaw/TuyaOpen、WatcheRobot 嵌入式 SDK、官方 Python SDK/Runtime、Python 3.11 候选环境、SQLite、HA REST/WebSocket、Codex app-server stdio。嵌入式 IDF/LVGL 基线由 G0 验证决定，不预先宣称兼容。

**Spec:** 本计划中的最新架构约束优先于旧稿中的“现有固件优先”建议。参考 `docs/Desk_Agent_R1_产品设计_V2.1_用户体验草案.md` 与 `docs/research/` 三份专项报告；旧稿 UI 细节暂不作为开发前置条件。

## Global Constraints

- 当前工作区 `D:/ePanda/DeskAgent` 只有设计和研究文档，未确定实际代码仓库；不得假定已存在下面的代码目录。
- TClaw/Tuya 云端对话是主路线；不默认另外建立 Python 语音模型会话来替代 TClaw。
- 新固件必须验证官方 SDK 兼容，不能把“SDK 安装成功”当作新固件可连接。
- 相机首期仅局域网实时查看与手动云台；无录像、回放、远程查看或监控音频。
- 提醒/备忘持久化放常驻服务；电脑关闭时 Codex 不执行，常驻生活服务仍应工作。
- 主机迁移：先 Windows 开发联调，再验证泰山派常驻；这是阶段顺序，不是永久改变部署目标。
- 一处硬件只接受统一 owner 调度；SDK 外部控制与 TClaw 本地行为不得直接竞争驱动。
- 构建、模拟、单项硬件、云端和端到端证据分开。工具声明、API 存在、服务回执均不能代替真实执行验收。
- 实施任务另行派发。安装、刷机、设备写操作、凭据配置需以当次任务明确授权为准；推送、PR、发布不由本计划授权。

## Review Focus

1. TClaw 与设备“Python SDK”应用模式可能互斥：G1 必须证明对话与外部控制共存，不能只分别通过。
2. 重连或进程重启造成重复任务/重复响铃：A3/A5 用同 request ID 重试与崩溃恢复测试固定行为。
3. 采音、播报、屏幕、相机并发造成卡顿或内存耗尽：A1/A6/A7 测有界队列与目标板高水位。
4. 动作命令接收但未物理完成、HA 回执但设备未改变：A1/A4 分开提交态、完成态与未知态。
5. 关闭页面、断网后继续采集或旧画面冒充实时：A6 测租约到期、停止采集和旧帧遮盖。

## 1. 执行关口与分波次

|关口|必须取得的证据|决定|
|---|---|---|
|G0 基线与接口|真实代码路径、版本/commit、硬件/固件清单、SDK 协议入口、IDF/LVGL 冲突表、接口契约|确定代码分区与固件基线，允许并行实现|
|G1 设备最小贯通|TClaw 在目标设备完成一轮 Tuya 对话；官方 Python Runtime 同时连接，收输入事件并执行一个受限硬件操作|证明主架构成立；失败先定位模式/协议/资源原因，不默默换架构|
|G2 单链路闭环|HA 控制并反馈；Codex 真任务并回传；提醒入库到响铃；LAN 持续新画面与关闭|每条链路独立可用|
|G3 常驻与联合运行|泰山派 Runtime/Application 成功部署，PC 关闭后提醒播报可用；组合负载和恢复通过|确认最终宿主与整机运行能力|
|G4 首轮交付|固定版本的端到端场景、故障矩阵、72 小时建议长稳、已知限制、可复现部署与回退说明|完成首轮集成验收；不等于量产可靠性证明|

- 第一波：A0 主导基线和契约，A1/A2 共同验证 G1；A3/A4/A5/A6 可以准备模块设计与模拟端，但不得各自发明设备协议。
- 第二波：G0 契约冻结后，A3/A4/A5 并行；A6 在相机取帧入口确认后并行；A1/A2 继续硬件与 SDK 能力补齐。
- 第三波：A7 联合各模块 owner 跑 G2/G3；失败交回对应 owner 修复。
- 第四波：A7 执行 G4，总管家审阅证据与缺口。

## 2. 分工与代码边界

以下路径为拟定 ownership；A0 必须先映射到实际仓库，记录绝对路径，再冻结边界。第三方源码与自有适配层分开；各 agent 不自行升级依赖基线。

|Agent|职责|拟定独占路径|禁止跨界|
|---|---|---|---|
|A0 架构与协议|资产基线、仓库定位、版本锁定、公共契约、兼容性裁决|`docs/implementation/`、`contracts/`、依赖锁定清单|不包办各模块实现|
|A1 设备固件|TClaw、Tuya 云对话、硬件驱动、资源仲裁、表情与动作|`firmware/components/desk_hardware/`、`desk_tclaw/`、板级配置|不修改 SDK 协议组件或 Python 业务|
|A2 SDK 兼容|新固件设备协议、capabilities、媒体与事件、官方 SDK 对接|`firmware/components/desk_sdk_bridge/`、`tests/sdk_contract/`|不自建第二个 Runtime，不直接占用硬件驱动|
|A3 Python 核心与生活服务|受管 Application、外部工具路由、提醒备忘、通知、持久化、泰山派部署|`host/desk_agent/core/`、`services/`、`deploy/edge/`|不代写 HA/Codex/相机模块，不增加重复云语音会话|
|A4 HA 接入|状态、事件、受限控制、状态确认|`host/desk_agent/connectors/ha/`、对应测试|不改变用户 HA 集成和自动化|
|A5 Codex 接入|Windows Companion、stdio 协议、项目/任务/审批、恢复|`companion/`、`deploy/windows/`、对应测试|不暴露 app-server LAN listener，不承诺当前桌面聊天接管|
|A6 相机与管理页面|局域网查看、帧新鲜度、租约、手动云台、基础管理页面|`host/desk_agent/camera/`、`web/`、对应测试|不修改相机驱动、绕过 motion owner 或增加录像|
|A7 集成与验证|场景脚本、故障注入、稳定性、交付证据|`tests/e2e/`、`tools/verification/`、`docs/acceptance/`|不直接跨模块修生产代码，问题交 owner|

公共构建文件、依赖清单与组件注册由 A0 或指定集成 owner 顺序合入。A1/A2 修改同一个启动入口时，由 A1 合入，A2 提交调用接口/补丁。

## 3. 公共接口：先冻结，再开发

产品侧接口与官方设备协议分开。下面是自有 bridge 的接口目标；A0 输出可执行 schema/类型与 fixtures，核对如何映射到 SDK 通道。不得擅自增加官方协议字段。

- 请求信封：`schema_version`、`request_id`、`operation`、`arguments`、`expires_at`。关联任务或资源时增加 `task_id`/`session_id`；重试保持同 request ID。
- 事件信封：`schema_version`、`event_id`、`sequence`、`request_id`、`type`、`timestamp`、`payload`。序号作用域由契约明确；重连查询当前状态，不能假定事件永久保留。
- 操作结果：`accepted` 仅表示接收；最终 `succeeded`、`failed`、`cancelled` 或 `unknown` 另行报告，携带可读原因和稳定错误码。
- Codex 操作：`submit(workspace_alias, prompt)`、`append(task_id, text)`、`cancel(task_id)`、`continue(task_id, text)`、`get(task_id)`、`respond_approval(approval_id, decision)`。审批必须绑定实际 thread/turn/item/request。
- HA 操作：`get_state(entity_id)`、`execute(entity_id, action, parameters)`、事件订阅；提交结果与观察到的状态分开。
- 生活服务：提醒 create/update/delete/snooze/complete、备忘 create/read/update/delete；停止响铃不调用 complete。
- 资源接口：命名表情、受限 motion preset/stop、播放/stop、camera acquire/release；自然语言模型不直接生成电机角度。
- A0 明确 TClaw 外部工具请求如何进入 Python Application、如何回传结果/错误/超时。若官方高层 API 不具备此能力，给最小兼容扩展方案，G1 验证后再冻结。

## 4. 工作包与验收

### A0 — 基线和契约

- [ ] 读取适用 AGENTS、现有报告，定位真实 TClaw/WatcheRobot/SDK 仓库与工作区，记录 dirt，不切换或覆盖用户工作。
- [ ] 输出 `docs/implementation/baseline.md`：SDK/firmware/IDF/LVGL commit、硬件未知字段、Python/OS/HA/Codex 基线、目标仓库映射。
- [ ] 输出 `contracts/` 类型/schema/fixtures 与 `docs/implementation/interfaces.md`；用 schema 校验器验证正常、过期、未知 operation、重复 ID 示例。
- [ ] 与 A1/A2确认 SDK 模式与 TClaw 并行方式；给明确 G1 实验和失败分类。
- [ ] 验收：模块输入输出同一套契约、无假定官方 API、版本可追溯；构建基线冲突有裁决和回退。

### A1 — TClaw 与全部目标硬件

- [ ] 从单一可恢复固件工程开始，确认构建配置/分区；不要直接合并全部 SDK 或升级整个依赖栈。
- [ ] 接通 TClaw→Tuya 对话；实际语音完成一次问答，分别记录采音、云请求、播报证据。
- [ ] 封装麦克风、扬声器、屏幕、触摸/滚轮、相机、STM32，输出能力清单与统一资源 owner API 给 A2/A6。
- [ ] 实现表情、状态文字目标、有限 motion preset/stop；板型支持差异如实标记。
- [ ] 测录放/打断、运动完成与故障、断链停止、相机与显示音频并发；VAD/AEC/唤醒不足时给缺口，不把 PTT 当最终完整语音体验。
- [ ] 验收：单项真实硬件可用，TClaw 对话可用；请求提交与完成分开；动作行程/限位/停止有设备证据。

### A2 — 官方 SDK 兼容与外部工具通道

- [ ] 用固定官方 SDK commit 建兼容矩阵：配对、连接、capability、音频、相机、运动、表情、输入、错误和重连。
- [ ] 通过 A1 资源 API 实现设备协议适配，保持官方 Runtime 所有权，不另建绕过 Daemon 的设备连接。
- [ ] 证明 TClaw 一轮对话期间，Python Application 仍能收触摸事件、执行安全操作、回传外部工具结果。
- [ ] 测协议未知字段/错误、媒体丢包、重复 request、断网重连及 SDK 新旧版本不匹配。
- [ ] 验收：G1 同时运行通过；输出兼容/未支持表、固定复现脚本与媒体鉴权/加密的实际结论。若 app 模式互斥，先提出最小固件调整，不自行替换官方 SDK。

### A3 — Python Application、生活服务与常驻部署

- [ ] 通过官方 `app run` 与注入 channels 建立受管 Application；实现 A0 路由、超时、错误、结果回传。
- [ ] 用 SQLite 实现提醒 occurrence、备忘、任务关联和通知 outbox；保存事务成功后才回成功。
- [ ] 实现调度、贪睡/完成分离、时区/重复/错过策略、开机恢复、备份恢复、主动通知规则。
- [ ] 先 Windows 联调，再在泰山派隔离 Python 环境验证 Runtime/Daemon/PyAV、已配对连接、开机自启和资源占用。
- [ ] 测同 ID 重试、保存后崩溃、播放前后崩溃、容器重建、断 WAN、PC关闭；设备不可达不能误报响铃成功。
- [ ] 验收：不丢提醒/备忘、不重复任务/响铃；PC关闭后本地提醒真实播报；泰山派失败时给具体环境原因与受支持常驻宿主备选，不把失败隐藏为 Windows-only 交付。

### A4 — HA Connector

- [ ] 读取状态快照并订阅事件，维护实体/服务允许清单；重连全量对账。
- [ ] 将 TClaw 家居工具请求映射到 HA，分开服务回执和状态确认。
- [ ] 以允许范围内实际实体完成读取、控制、外部操作状态更新；无反馈实体返回无法确认。
- [ ] 测 unavailable、超时、设备外部竞争、云集成失效、重复 ID；不自动重放过期控制。
- [ ] 验收：真实设备动作与回读一致；明确每个测试实体的本地/云依赖；不修改用户现有集成。

### A5 — Windows Companion / Codex

- [ ] 注册工作区别名与绝对路径；本机 stdio 启动锁版本 app-server，完成 initialize/thread/turn 生命周期。
- [ ] 实现追加、取消、继续、结果与错误，审批绑定实际请求；由 Companion 连接 Python 服务的认证通道。
- [ ] 将任务记录与 thread/turn ID 持久关联，重启后核对实际状态，不重放未知进行中的请求。
- [ ] 在明确授权的测试工作区完成实际任务，返回 diff/命令退出码/检查结果；测拒绝审批、活动轮次取消、离线和重复提交。
- [ ] 验收：TClaw 发起到 Codex 执行、进展/审批/终态回传闭环通过；turn completed 不冒充目标验收成功。桌面当前聊天互通单独列结果。

### A6 — LAN 相机与基础管理页面

- [ ] 消费 A2 官方相机入口；先 serial capture 最新帧，性能不足才与 A1协商 embedded stream 入口。
- [ ] 实现认证、单查看租约、有界最新帧缓存、时间戳/帧龄、停止与断流遮盖；不落盘、不录像、不传监控音频。
- [ ] 手动云台只调用 A1 motion owner；查看期间禁止自动转头/跟随抢占。
- [ ] 基础页面覆盖查看、任务结果/审批、提醒备忘与连接状态；沿用表情优先方向，不扩大视觉精修。
- [ ] 测页面关闭、浏览器崩溃、进入后台、租约超时、网络断开、旧帧与并发语音；记录实际 FPS、帧龄和停止时间。
- [ ] 验收：持续收到新画面，异常不展示旧帧为实时；关闭后停止采集/发送并释放资源，实际指标满足经评审确认的门槛。

### A7 — 系统集成与交付验证

- [ ] 固定模块版本，依照契约联调；每条链路必须有实际请求/结果与设备观察，模拟单独归档。
- [ ] 跑语音→Tuya→HA、语音→Tuya→Codex、语音提醒→持久化→到期播报、LAN查看→云台→关闭。
- [ ] 验证聊天、任务进展、提醒、摄像头同时使用时仲裁正确，记录 MCU内部RAM/PSRAM 与泰山派资源。
- [ ] 分别断 PC/WAN/LAN/HA/Hub/Robot；测试重启、审批过期、错过提醒、相机停止和数据库恢复。
- [ ] 建议首轮72小时长稳，记录失联、重启、重复、丢失、时延及资源峰值；指标先经评审冻结，不声称建议数值已通过。
- [ ] 输出 `docs/acceptance/` 验收矩阵、失败与限制、部署/回退手册；总管家审核 G0–G4 证据。

## 5. 派发模板与交付格式

每次只派一个工作包或其中一个明确 gate，不把整套产品一次交给单个 agent：

```text
执行本计划工作包 A{编号}，本次只完成 {明确范围/阶段}。
工作区：{已核对的绝对路径}。先读适用 AGENTS 与本计划，再读对应专项报告。
允许修改：{该工作包实际 ownership 路径}。
禁止：修改其他模块、升级公共协议/依赖、覆盖现有工作、未经授权操作设备或外部状态。
输入：{固定版本、接口契约、前置任务产物}。
验收：{本工作包明确条件}；区分模拟、构建、目标硬件、云端和端到端。
返回：文件清单与diff摘要；实际检查命令及结果；证据路径；未完成/阻塞；接口变更请求。
接口缺失先提变更请求，不自行发明邻接模块接口或用mock声称闭环完成。
```

每个 agent 输出：`结论 → 变更 → 验收证据 → 未通过项 → 需要其他owner处理的事项`。真实日志只给相关片段，禁止密钥进入报告。A0登记接口变更，总管家负责 scope、冲突裁决与合入顺序。

## 6. 首轮派发建议

1. 派 A0：完成 G0 资产/仓库映射与契约设计；A1/A2提供固件与 SDK 兼容输入。
2. 派 A1+A2：只做 G1 最小贯通，尚不开发完整 UI、录像或大规模外部功能。
3. G0冻结后派 A3/A4/A5，使用统一 fixtures 并行；G1失败时这些模块可独立验证，但整机不能宣称通过。
4. 相机入口确认后派 A6；单链路可用后派 A7推进G2–G4。

本计划无固定工期承诺。最大未决项是 TClaw/官方 SDK 同时运行与目标硬件/Runtime兼容，先取得这两类证据，再估算后续工作量。
