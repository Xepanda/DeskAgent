# Desk Agent R1：HA、智能家居与提醒陪伴服务可行性

> 调研日期：2026-10-11。范围：Home Assistant 接口与公开集成源码、泰山派和个人资料库已有记录、提醒/备忘/陪伴的本地常驻架构，以及未来摄像头/云台的 HA 接口。未访问用户在线 HA，未读密钥，未操作设备、安装软件或写入外部服务。本文是技术可行性研究，不是端到端验收结论。

## 结论

技术路线可行，建议把 Home Assistant 作为家庭设备现状与执行的权威接口，把一个独立的 Desk Agent Hub 常驻服务部署在泰山派上，负责语音意图路由、提醒/闹钟/备忘持久化、陪伴会话状态、机器人通知及 HA 事件订阅。R1 的关键技术链可以由 HA WebSocket/REST 和独立本地数据库实现；HA 自动化也可作为既有规则的执行者。服务调用回执不能证明设备物理动作成功，必须读取有意义的实体状态或设备反馈。

目前可核实的是：用户说明小米/Tuya 已集中在 HA、泰山派运行 HA；资料库记录了泰山派 RK3566 上的 Armbian/Debian + Docker + HA Core 容器部署、持久配置目录、容器自动重启和部分资源快照。公开笔记不是当前设备探测或新服务 PoC。机器人语音控制、执行后实际状态回读、提醒持久化/准点播放、备忘查询修改、主动陪伴与设备之间的完整链路均未在本项目验证。

## 证据等级与已有资料

|证据|能支持的结论|不能支持的结论|
|---|---|---|
|本地产品与核验文档：[能力与资料核验](../Desk_Agent_R1_能力与资料核验.md)、[V2.0](../Desk_Agent_R1_产品设计_V2.0_草案.md)、[V2.1](../Desk_Agent_R1_产品设计_V2.1_用户体验草案.md)|R1 目标边界、已查 SDK 接口、未完成项目清单：HA 为家居入口；Edge Hub 负责提醒/记录；机器人 SDK 有有限输入、表情、PCM、运动等 API|产品草案和 SDK 接口描述不证明本地服务、目标机器人或设备实测可用|
|个人 wiki 的 [README 导航](https://github.com/Xepanda/wiki/blob/master/README.md) 与 [泰山派硬件评估](https://github.com/Xepanda/wiki/blob/master/docs/02-projects/hardware-embedded/taishanpi/01-hardware-eval-and-architecture.mdx)|文档记录 RK3566、2GB RAM、16GB eMMC、Armbian/Debian、Docker 和 HA 等服务的 All-in-One 设计/测试过程|数值与结果属于 wiki 记录；不代表本次现场状态、当前配置或 72 小时稳定性|
|[泰山派 HA 小米/Tuya 实战笔记](https://github.com/Xepanda/wiki/blob/master/docs/02-projects/hardware-embedded/taishanpi/08-home-assistant-xiaomi-tuya-voice-bridge.mdx)|记录 `homeassistant/home-assistant:stable`，Docker host 网络、`--restart=always`、`/opt/all-in-one/homeassistant/config:/config`，另记 HA 访问地址、容器资源快照和自动化 YAML|作为资料库中 HA Core 容器实际运行的历史记录；不能证明现在仍在运行、备份恢复/断电恢复通过，也不能证明 Desk Agent Hub 已存在|
|[泰山派 HA 巡检笔记](https://github.com/Xepanda/wiki/blob/master/docs/02-projects/hardware-embedded/taishanpi/09-home-assistant-ops-and-troubleshooting.mdx)|说明已有 Docker、HA API HTTP 探针、日志、`docker stats`、Recorder SQLite 状态排查流程|巡检脚本、建议阈值、历史日志片段不等于本次执行或持续健康保证|
|[Docker 与系统部署笔记](https://github.com/Xepanda/wiki/blob/master/docs/02-projects/hardware-embedded/taishanpi/02-base-environment-and-tuning.mdx)、[容器部署笔记](https://github.com/Xepanda/wiki/blob/master/docs/02-projects/hardware-embedded/taishanpi/03-docker-and-proxy-acceleration.mdx)|记录 Armbian/Debian 与 Docker/Compose、日志限制、ZRAM 等运维方式|配置意图不能证明 HA 容器及新服务自动启动成功或资源余量够用|

资料库中“当前泰山派服务宿主机”与“历史/期望的 All-in-One 方案”需要在后续首配时区分。本文不访问 `nas.local`、HA API 或设备，以免把历史记录当作现场状态。

## 能力矩阵

|能力|接口/现状|可行性与限制|状态|
|---|---|---|---|
|读取 HA 实体状态|REST `GET /api/states`、WebSocket `get_states`；订阅 `state_changed`|适合初始化快照 + 长连接增量更新。重连后重新取全量状态，再按实体 ID/时间戳对账；事件订阅不是永久队列|官方接口已确认；本项目未联调|
|控制设备|HA REST action/service API 或 WebSocket `call_service`|执行回执表明 API 接受/处理请求，不保证实体、云端或物理设备已改变；需要单独观察目标状态和超时；不确定状态时不得盲目重放|官方接口已确认；具体设备能力待设备逐项核验|
|主动 HA 事件与规则|WebSocket `subscribe_events`（如 `state_changed`）；HA 自动化/脚本|适用于门磁、环境、完成状态通知等规则。事件触发应有去抖、来源、时间、重复键及失效规则；动作逻辑放 Hub 或 HA 需有唯一权威，避免重复执行|接口与既有规则模式已确认；Desk Agent 规则未实现|
|自然语言家居控制|HA Conversation API `POST /api/conversation/process`；Assist pipeline 可由 WebSocket 执行|可让 HA 处理其公开暴露给 Assist 的实体和意图；设备暴露范围、agent、语种、实体匹配与机器人音频采集要配置/PoC。推荐语音识别后将文本交给 Hub，再调用 HA Conversation API；若需要 R1 权限/确认策略，则 Hub 做意图解析并只调用 allowlist 服务|接口可行；语音端到端和实体暴露未验证|
|本地/云端边界|HA Core 在本地；集成的数据路径逐类不同|LAN API 可在 WAN 断开时访问 HA，但某些设备集成自身依赖厂商云。设备“接入 HA”不代表设备控制本地化或离线可用|按实体集成及 IoT Class 清单核对|
|小米集成|官方 Xiaomi Home README 明确支持网关本地模式：Xiaomi Central Hub Gateway 固件 ≥ `3.3.0_0023`，或内建中央网关功能设备软件 ≥ `0.8.9`；HA 集成通过网关 MQTT 收发属性/事件与控制命令。无符合条件的网关时，控制经 Xiaomi Cloud。另可开启有限的 Xiaomi LAN control|存在潜在本地化机会，但须确认目标网关/内建网关型号与版本、实际接入路径及设备支持。LAN control 只覆盖 HA 同一 LAN 的 Wi-Fi/Ethernet IP 设备，不能控制 BLE Mesh、Zigbee 等；官方说明可能导致异常且不推荐；同 LAN 已有中央网关时 LAN control 不生效。不得将某一工作模式泛化为全部小米设备|官方分模式能力已确认；用户现场 HA 集成、网关与设备清单未知|
|Tuya 集成|HA 官方 Tuya 使用 Tuya/Smart Life 账号授权，官方文档称锁和 remote 平台不支持，功能覆盖依赖其 SDK|不能默认全本地、所有功能可用、状态实时。wiki 记录某 OEM 红外设备在官方集成中 unsupported，使用社区 LocalTuya 与场景绕行；该历史案例只能说明该型号/当时配置问题。LocalTuya/tuya-local 属第三方路线，需单独评估 key 管理与维护|官方支持边界已确认；现场设备和依赖 WAN 情况未知|
|提醒/闹钟|Hub SQLite 持久化 + 常驻调度器 + 机器人本地音效/短语音；HA 可提供输入/事件联动但不取代 Hub 的个人提醒库|Hub 持有权威 occurrence 状态；入库事务提交后才确认已保存。启动时由 DB 重建调度，断网期间已持久化提醒仍可按策略触发（前提 Hub、机器人和 LAN 可达）；停电、校时和机器人离线时无法承诺准点响铃|产品设计目标；服务/存储/声音链路均待 PoC|
|备忘/陪伴|Hub 管理文本笔记、对话会话和用户同意的偏好；语音模型/ASR/TTS 可插拔|备忘不需要云同步即可实现；ASR/LLM/TTS 若走云则 WAN 断开时只能报告不可用，不能伪装离线对话。陪伴的长期偏好应显式同意、可查看/删除|产品设计目标；没有本项目运行证据|
|机器人输入与反馈|WatcheRobot SDK 已核验有限 backtouch/screentouch/roller 输入、受限表情、PCM 播放/stop、Motion API；项目核验文档指出 Python SDK 不含默认 ASR/LLM/TTS|足够作为闹钟贪睡/停止、交互状态表达、短提示音和有限预设动作的候选接口；泰山派 Linux ARM Runtime/Daemon、音频/运动设备能力、机械安全均须 PoC|公开 SDK 源码/文档能力；当前设备未测|
|摄像头/云台接 HA|HA Camera entity 提供 still 与可选 stream；设备 integration 也可以暴露相机动作或其它实体|如机器人摄像头要在 HA UI 展示，可实现 camera entity/自定义集成，但当前 SDK JPEG 图像接口不等于实时视频协议。HA Camera 集成本身不定义机器人 PTZ API；运动可另由受限脚本/集成服务提供预设动作|标准接口可行；机器人视频/PTZ/隐私与并发待验证|

### HA、语音与 Assist 接口边界

- REST 适合短查询/动作；WebSocket 适合事件和状态实时更新。保存令牌只在 Hub 服务端，使用独立 HA 用户；将实体/服务 allowlist 放在 Hub。HA Long-Lived Access Token 不是实体级权限令牌，因此不能把 token 发给机器人或浏览器。
- REST `POST /api/services/<domain>/<service>` 与 WebSocket `call_service` 的服务回执不构成硬件状态确认。明确动作后再轮询/订阅状态，使用超时和状态置信条件（例如目标状态与新 `last_changed` / `context` 关系）；不适合观测的设备只能报告“已发送，当前无法确认”。
- Assist pipeline 是完整语音管线（音频采集/唤醒、STT、Conversation、TTS）；机器人要接入它需实现兼容的音频/卫星接入路径。较小 PoC 先由机器人/Hub 自己完成语音采集和 STT，再把文本交给 Conversation API 或 Hub allowlist，避免一开始同时引入音频链路和设备控制两个未知项。
- HA REST 及 WebSocket API 属本地 HA 服务面，不自动提供外网安全入口。首版仅家庭 LAN；不通过云隧道或公网转发 HA token。

## 泰山派部署路线与持久运行

### 已有部署记录

wiki 的泰山派 HA 页面给出可复核的配置描述：RK3566（2 GB RAM / 16 GB eMMC）、Armbian Debian 13、Docker Engine、HA Core `homeassistant/home-assistant:stable` 容器、`--net=host`、`--restart=always` 和 `/opt/all-in-one/homeassistant/config:/config`。这意味着截至该文档所描述的方案，HA 形态是自行管理宿主机的 **Home Assistant Container/Core 容器**，不应报告为 HAOS，也不应假设有 Supervisor Apps/Add-ons。

官方当前部署政策：2025.12 后官方支持路线为 HAOS 与 Container；Core（独立 Python 环境）与 Supervised 已弃用。HAOS 是面向支持硬件的一体化方案；该资料只证实泰山派 Armbian 上的 Container 记录，没有证据表明泰山派有官方 HAOS 镜像/受支持目标。因此 R1 推荐维持当前 Docker 宿主，给 Desk Agent Hub 单独容器和 systemd/Docker 重启策略，不迁移或重装 HA。

Hub 与 HA 应分容器部署：HA Container 不带 Supervisor Apps；Hub 作为普通 ARM64 容器或宿主机 systemd 服务，限定 CPU/内存和日志大小、显式健康检查、开机启动、滚动日志。Hub 应将应用数据库放到专用 bind mount（例如 `/opt/desk-agent/data`），设置明确的 Docker volume 路径、磁盘空间告警和定期离机备份。当前 HA 有 `/config` bind mount 的描述，尚未发现 Desk Agent 数据目录、备份或恢复证据。

RK3566 2 GB 是硬约束：wiki 中 HA 单容器历史快照约 338 MB，另一页记录 HA 约 500–700 MB；数字口径/时期不同，不应直接相加或当作当前余量。Hub 基础调度和 SQLite 开销应小，但容器、Python/Node runtime、ASR/LLM/视频均需现场测量。推荐首期不在泰山派运行大型本地语音模型/视频转码；先量测 HA + Hub + 其它既有容器的可用内存、Swap/ZRAM、磁盘 I/O、温度和重启恢复，再决定本地 STT/TTS。

### 提醒与备忘可靠性设计

1. 用 SQLite WAL + 外键/约束做本地持久库；Reminder、重复规则、单次 occurrence、Note、通知投递状态分表。插入/修改/删除在一个事务内提交，成功后才向机器人/用户回报保存成功。备忘与提醒内容按用户数据权限保存，不写入普通诊断日志。
2. 调度器不把未来任务只放在内存 timer 中。进程启动、数据库恢复、时钟校正、机器人重连时都从数据库重建近未来工作；每个 occurrence 使用唯一 ID 和状态机（scheduled→delivering→heard/snoozed/completed/failed），唯一约束确保重启和重复 HA 事件不产生双响。触发过程采用 outbox/租约或事务标记避免“播放成功但记录未写”导致重播。
3. 使用泰山派宿主机系统时钟和 NTP 健康标记；每个提醒保存 IANA 时区和本地重复语义，显式定义夏令时/墙钟跳变行为。RTC/断电时钟保持尚无证据；断电期间不承诺响铃准点。Hub 重启后按用户配置的 missed policy 汇总未响项，不连续补播。
4. Hub 在线但 WAN 中断：已落库的提醒和备忘查询仍可工作；闹钟能否响取决于机器人在线、局域网通路和本地音效。米家/Tuya 云端集成及云端 ASR/TTS 可能不可用，应分别显示故障。Hub/泰山派断电：没有单独电源/RTC/机器人本地持久调度证据，不能承诺离线自治。
5. 备份数据库采用每日快照/SQLite backup API，保留轮数、磁盘水位和恢复演练。不要仅复制运行中的 SQLite 主文件；WAL 一致性必须保证。服务被删除/重建时 bind mount 保留数据。设备日志不应包含访问令牌、原始音频或完整私密备忘。

## 摄像头与云台接入契约建议

这是 Desk Agent 新接口契约，不是已实现的 HA 集成。建议分出两类资源：

|接口面|HA 表达|契约要求|
|---|---|---|
|实时画面|`camera.desk_agent_robot` camera entity；只在支持可解码视频流且经 PoC 后报告 `STREAM` 能力|只对本地认证 session 提供临时 stream URL/访问凭证；开始/结束时间可追踪；用户点开才启动；关闭/心跳超时/断网即释放；不录像、不回放、不含监控音频；过期帧清空遮罩。HA token 不送到前端。|
|隐私/连接状态|`binary_sensor.desk_agent_camera_privacy`、`binary_sensor.desk_agent_camera_streaming`、`sensor.desk_agent_camera_frame_age`|分别反映物理隐私关、正在采集/发送、最新帧年龄；未知/断流要上报 unavailable 或 stale，不能复用旧帧冒充实时。状态应能从设备/适配器验证。|
|云台安全动作|不依赖通用 camera entity；可提供 `desk_agent_robot.move_preset` / `stop` 服务或 HA script，设备本身不支持动态 PTZ 时不创建这些能力|模型只可调用有限 preset（center/attention/nod 等），角度范围和速度在 Adapter 内夹限；stop 优先级最高；要求真实运动完成/错误状态，若目前只有命令接受则返回 `accepted` 而不是 `completed`；查看流期间 view session 独占云台，禁止陪伴动作抢占。|
|认证与资源租约|Hub/Adapter 的本地会话，与 HA 认证隔离|按 `request_id/session_id` 幂等；短时授权；camera owner 有 lease/heartbeat，页面异常关闭后由超时释放；服务重启回到安全静止状态；审计不含令牌/音频。|

HA Camera 文档支持图像与可选直播流，并提醒预加载会持续占资源；核心 Camera entity 不能替机器人定义 PTZ 或无录像隐私策略。因此先验收机器人媒体协议、媒体鉴权和资源并发，再决定是否将直播实体暴露在 HA，网页管理页面仍使用 Hub 认证入口。

## 推荐实施路径、PoC 与稳定性验收

### 分阶段路线

1. **只读环境盘点（不直接连在线实例的准备阶段）：** 首配时由用户/向导展示 HA 版本、Installation type、CPU 架构、实体域名/集成来源、关键服务实体及 `state` 可观测性；记录机器人/Runtime 版本和泰山派容器资源。不得导出访问令牌到报告。
2. **Hub 生命周期与提醒存储 PoC：** 在泰山派同宿主 Docker 中单独运行 Hub；先用模拟机器人 Adapter。创建、查询、改期、删除提醒/备忘；设备重启 Hub、容器删除重建、宿主冷启动后检查数据保留与 occurrence 去重。
3. **HA Connector PoC：** 先读少量明确 allowlist entity，WebSocket 重连后快照对账；再以一盏低影响设备测试一次控制，分别模拟可观察状态成功、状态延迟/不可观测、HA 不可达、云 WAN 中断；状态未确认不能报完成。
4. **机器人语音和反馈 PoC：** 证明从麦克风/PTT或唤醒输入→解析时间/设备→必要确认→HA/Reminder route→Hub 持久化/状态回读→短语音+表情/贪睡按键的完整闭环；所有未支持的动作清楚提示不可用。
5. **语音与陪伴扩展：** 逐个比较本地 Wyoming STT/TTS 与云供应商，测内存、时延和断 WAN 提示；默认先不运行泰山派本地大模型。用户偏好记忆须显式同意，且支持查看、修改、删除。
6. **摄像头/云台 Gate：** 先确认 SDK/固件的视频流、身份认证、隐私门控、帧新鲜度、无音频/录像；再接入 HA camera entity 和安全预设动作。未通过前页面不得声称直播/急停安全已完成。

### 最小 PoC 清单

- 单一重复提醒（含 `Asia/Shanghai` 时区）、单次闹钟、备忘录；创建后确认读回、改期/删除、重启 Hub 与泰山派后状态/重复 occurrence 正确。
- 机器人报时提示用 PCM 本地音效或预装音频，控制输入能执行一次贪睡和停止；`heard` 不等于事项完成。分别断 WAN、断机器人 LAN、重启 Hub、重启 HA；验证错过策略和恢复提示。
- 选 1 个明确可读写且低影响的 HA 灯/插座实体和 1 个已有传感器实体。执行一次状态读取、一次动作、订阅外部状态变化；遇到超时/不可观测时报告“请求发送，无法确认”。
- 采集 72 小时日志：进程/容器重启原因、HA/Hub 可达率、提醒计划/实际首声差值、重复/丢失次数、SQLite 错误、内存峰值/Swap、磁盘/数据库增长、温度与 NTP 偏差。72 小时只是建议首轮稳定性门槛，不代表可以覆盖季节性或长期可靠性。

### 建议验收门槛（需实测后确认）

|指标|建议目标|口径|
|---|---|---|
|数据持久性|Hub/HA/泰山派正常重启、Hub 容器删建后提醒/备忘/状态零丢失、零重复响铃|分别执行冷启动与异常进程终止；用 occurrence ID 对账|
|调度误差|Edge、LAN、机器人在线且 NTP 健康时首声相对计划时间 ±2 秒；单列高负载和校时异常数据|设备实际首声时间戳，不以 DB 查询/服务日志代替|
|WAN 故障|已存提醒按本地策略继续；HA 云集成/云语音失效时状态明确；禁止重放过期家居命令|断外网但保留 LAN，单独核验米家/Tuya设备实际路径|
|掉电/RTC|不承诺断电准点；恢复后展示 missed summary，不连环补播；记录时钟是否保持|现场验证掉电时间和重校时，未验证即视为无保障|
|家居反馈|在线且有明确设备状态的动作，P95 ≤3 秒可作为初始目标；实际设备不可观测则准确率目标不适用并降为“请求已发送”|分开计时服务回执与实体状态确认|
|稳定运行|至少 72 小时，关键内存和磁盘余量保持 >20%（建议值）；无静默崩溃、通知丢失/重复|以目标固件/HA integration/容器配置记录采样，重新启动也纳入测试|
|安全与隐私|HA token 仅存在服务端；提醒/备忘可查询/删除；无密钥、音频、摄像头录像进入日志/DB；camera 关闭后不再传输|审查 API/文件/网络抓包和日志，不以 UI 文案作证|

## 新能力候选：已有接口与待验证项

|分类|能力|依据与建议|
|---|---|---|
|已有平台/软件路线|HA 自动化可因实体状态和事件触发；wiki 已记 MQTT、ESPHome、Matter、Tuya 与小米设备/场景|用现有实体增加“门窗未关提醒、环境阈值提醒、设备完成提示”等主动陪伴上下文。先盘点可观测实体和云/本地 IoT class，不能虚构未在现场的传感器或功能|
|待现场确认的潜在本地化机会|Xiaomi Home 官方集成可通过指定版本的小米中央网关或内建中央网关以本地 MQTT 做设备属性/事件与控制；无网关时走云。另有受限 IP 设备 LAN control|首配盘点网关型号/固件、HA 集成 domain/config、设备拓扑和断 WAN 表现后，可将符合条件的设备纳入本地路径 PoC。LAN control 官方不推荐且范围有限，不把它当通用替代方案。现有 wiki 与 R1 文档没有确认用户拥有/部署符合条件的中央网关，不据此推断实际设备库存|
|已有 SDK 接口，项目待验证|机器人触摸/滚轮、有限表情、PCM 播放/停止、motion preset、设备侧 face tracking（兼容条件约束）|可扩展闹钟贪睡/停止、提醒反馈、陪伴表情和预制点头。仅接口与 SDK 文档证据，不代表泰山派配对、运动安全或音频播放已经实测|
|已有资料库项目，非当前产品集成|泰山派 Docker/HA、Kindle Wi-Fi/SSH 与工具箱、多色 4.2 寸墨水屏图像处理管线|匹配的后续扩展：将当天/近期提醒摘要做成低功耗墨水屏只读副屏；适用于离线可见的日程提示/状态，不替代机器人即时闹钟。wiki 是项目记录/方案与实践材料，不证明墨水屏已接入 R1、当前泰山派已部署此客户端|
|不建议纳入 R1 核心|Herdr + Collie + Tailscale Agent 终端访问|资料库导航/公开 Collie 说明是面向个人的 Agent 终端/手机访问方案，适合开发者远程查看 agent，不是家庭提醒数据库/机器人控制路径。最多作为运维者后续可选入口；不把私有终端会话带入家居语音权限面|
|需验证后再承诺|泰山派运行本地 STT/TTS、离线自然对话/长时记忆、多用户辨识、HA 摄像头直播、机器人云台控制、断电闹钟自治|CPU/RAM、音频资源、隐私、模型数据路径、安全运动、RTC/电池与掉电恢复均无本产品验收证据|

## 阻塞与待确认

- **现场部署身份：** HA 版本、installation type、泰山派当前 OS/CPU arch、Docker 挂载和自动重启策略是否仍与 wiki 一致；必须由用户首配页面/现场只读检查确认。
- **硬件状态：** RTC、供电/UPS、喇叭实际可播提示音、机器人与泰山派网络可达性、泰山派磁盘健康/可用容量、内存峰值未知。断电准时提醒不得作为已具备能力。
- **设备集成清单：** 小米与 Tuya 实际集成名称、版本、设备型号、所需服务/状态反馈、Wi-Fi/LAN 或 WAN 依赖、不可观测/假定状态实体仍未盘点。对 Xiaomi Home，特别确认是否存在固件 ≥ `3.3.0_0023` 的中央网关或软件 ≥ `0.8.9` 的内建中央网关设备，并确认是 gateway local、cloud 还是受限 LAN control；不能从“泰山派跑 HA”或 wiki 中的历史小米设备记述推断用户拥有这些网关/设备。自定义/社区集成与官方版本需分别标识。
- **wiki 设备资料缺口：** 泰山派 HA 页面叙述了小米音箱/路由器/插座、Tuya 插座/红外遥控器和 Matter 灯等生态案例，但没有形成可核验的当前设备台账；小米中央网关具体型号、是否存在内建网关设备、固件/软件版本、设备当前在线状态和真实接入方式都未查明。将这些页面视为“已记录的方案/案例”，不能据此断定用户当前拥有或仍在使用这些设备。
- **ASR/TTS/LLM 选择：** 用户可接受的云端音频/文本处理边界、供应商、离线退化尚未决定；不应默认全本地或默认上传云端。
- **机器人 Linux ARM 路径：** Python Runtime/Daemon、配对、设备掉线重连、audio.stream、输入事件和设备固件 capability 需在泰山派实测。
- **备份与恢复：** 当前 wiki 显示 HA `/config` bind mount；未发现 Desk Agent DB 或恢复演练。需确定 Hub 数据目录、备份保留、恢复责任人、磁盘告警和用户删除语义。
- **摄像头隐私/云台：** 是否由 HA 提供摄像头实体、SDK 是否能持续视频而不是 JPEG still、帧率/并发/媒体认证、隐私物理开关、急停/限位反馈均待核验。
- **提醒策略：** 时区、夏令时、重复规则、错过/恢复、勿扰及闹钟优先级是产品规则；需明确确认并覆盖测试。

## 建议开发任务列表

|序号|任务|前置依赖|产出|验收条件|
|---|---|---|---|---|
|1|泰山派/HA 只读基线与 ARM64 部署评估|用户在首配时授权查看本机只读信息；不需访问云端|版本/安装类型/CPU arch/容器配置概要；资源基线；HA 集成实体来源清单|准确区分 HAOS/Container；不输出 token；记录 HA、内存、磁盘与 NTP 基线，并标注未验证项目|
|2|Hub 常驻服务与数据层|任务1确认 Docker/ARM64 及剩余资源；先定数据保留/备份策略|Hub 镜像/服务部署方案、SQLite schema、migration、health 状态、绑定持久目录、受限日志/资源配置|开机自动启动；进程异常恢复；容器删建数据保留；事务提交才回确认；健康状态可观测|
|3|提醒/闹钟/备忘调度器|任务2完成；提醒时区/重复/missed policy 决策|提醒 occurrence 状态机、备忘 CRUD、幂等 scheduler、恢复/补报逻辑、备份与恢复说明|重启/断网测试不丢、不重复；删除/贪睡对账正确；缺网/HA 不可达时不伪称成功；数据能恢复|
|4|HA Connector（只读起步、受限写入）|任务1已盘点 entity/domain/service；用户显式选定少数验证设备|WebSocket 订阅/重连快照对账、REST/WS action client、HA entity/service allowlist、状态确认器|外部状态变化实时收敛；服务回执与设备态分开；断连不重放过期命令；未知实体不能越权写入|
|5|机器人 Reminder/HA Adapter 与语音闭环|任务2–4；设备型号、SDK commit、Runtime/Daemon 联通、TTS 数据边界已定|有界 JSON 消息协议、提醒/状态语音和表情映射、touch/roller贪睡/停止、离线反馈|固定脚本完成提醒创建/修改/到期播报；HA 执行后读回真实状态；播放/终止反馈与 DB 状态一致；失败可理解|
|6|家庭传感器主动通知与陪伴规则|任务4；明确选定用户实际有的状态实体和推送规则|基于实体 allowlist 的规则订阅、去重/冷却/勿扰策略、用户同意的偏好存储界面|正/负触发、重复抑制、关闭/删除规则有效；不根据图像推断情绪/健康；偏好可查看修改删除|
|7|摄像头 Camera entity + 云台安全 PoC|任务1/SDK版本；视频流、认证、隐私开关、物理限位/stop 与资源能力通过单项确认|HA Camera entity/Stream 或明确不支持说明；隐私/帧龄传感器；安全 preset 服务契约|仅 LAN 临时查看、无录像/音频；旧帧清除；关闭/断连释放；查看期间云台锁定；真实 stop/error/complete 状态通过|
|8|72小时稳定性与恢复演练|任务2–5 在目标硬件可运行；日志与时间同步可观测|HA+Hub+机器人完整记录、负载/时钟/调度统计、数据库备份恢复记录、已知限制表|按本报告建议门槛记录零重复/丢失；遇异常如实标注并修复后复跑；不把工具调用、配置存在或服务回执当端到端通过|

## 官方文档与源码参考

- [Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/)：身份验证、`get_states`、`subscribe_events`、`state_changed`、服务调用没有设备层结果返回。
- [Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/)：Bearer token、实体状态/历史/服务 API；token 应保密。
- [Home Assistant Conversation API](https://developers.home-assistant.io/docs/intent_conversation_api/)：`/api/conversation/process` 文本意图处理。
- [Assist pipelines 开发文档](https://developers.home-assistant.io/docs/voice/pipelines/) 与 [Wyoming 集成](https://www.home-assistant.io/integrations/wyoming/)：语音 pipeline 与本地/网络语音服务集成路径。
- [HA 2025.12 Core/Supervised 弃用公告](https://www.home-assistant.io/blog/2025/05/22/deprecating-core-and-supervised-installation-methods-and-32-bit-systems/) 与 [Linux Container 安装说明](https://www.home-assistant.io/installation/linux/)：当前推荐安装路线、Container 与 HAOS 差异。
- [HA Tuya 集成说明](https://www.home-assistant.io/integrations/tuya/)：平台覆盖与官方 SDK 功能限制。
- [Xiaomi Home HA 官方集成 README](https://raw.githubusercontent.com/XiaoMi/ha_xiaomi_home/main/README.md)：FAQ 的 local mode 与 Control through the Cloud / Control locally 分别说明中央网关版本、本地 MQTT、无网关时的云控制，以及受限且官方不推荐的 Xiaomi LAN control。控制路径取决于现场网关/集成配置，不能泛化为全部走云。
- [HA 集成 IoT Class 源码规范](https://developers.home-assistant.io/docs/creating_integration_manifest/#iot-class)：区分 local/cloud polling/push 与 assumed state。
- [HA Camera 集成](https://www.home-assistant.io/integrations/camera/) 与 [Camera entity 开发文档](https://developers.home-assistant.io/docs/core/entity/camera/)：图像/可选视频 stream；预加载资源代价；相机实体可提供 HLS/WebRTC，但 PTZ/隐私仍须具体 integration 支持。
- [泰山派硬件与 HA/Tuya/Matter/小米部署记录](https://github.com/Xepanda/wiki/tree/master/docs/02-projects/hardware-embedded/taishanpi)：包含以上硬件、运行形态、Docker、HA 集成和巡检原始资料路径。
- [Kindle 技术专栏](https://github.com/Xepanda/wiki/tree/master/docs/02-projects/hardware-embedded/kindle) 与 [4.2 寸墨水屏管线](https://github.com/Xepanda/wiki/tree/master/docs/02-projects/hardware-embedded/epaper)：只作为低功耗提醒副屏扩展依据，不是当前 R1 集成证据。
- [R1 WatcheRobot SDK 能力与项目核验记录](../Desk_Agent_R1_能力与资料核验.md)：项目已有的 Python/嵌入式 SDK 源码引用、Robot API、输入/音频/运动能力与未验证边界。
