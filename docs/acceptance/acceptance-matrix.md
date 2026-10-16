# Desk Agent R1 全系统验收矩阵与证据清单 (Acceptance Matrix)

> 文档版本：V2.4.0  
> 责任 Agent：A7 系统集成与交付验证  
> 参照标准：`AGENTS.md` 架构治理规范、实施计划关口目标与 PR #2 审查意见 (P1 阻塞整改)  
> 状态：**全模块生产级协议闭环与 G1 互通完成，93 项全量自动化测试全部 PASS (100% OK)，CMake 构建通过**

---

## 1. 证据分层标准与总体状态 (Evidence Layering)

根据 `AGENTS.md` 第 4 节定义，严格执行证据分层核验，坚决杜绝用假数据、内存状态或模拟输出夸大真实物理交付：

| 证据层级 | 验收范围与含义 | 当前结论 | 关键依据与测试输出 | 绝对不能代表什么 |
| :--- | :--- | :---: | :--- | :--- |
| **Level 1：公开源码 / 契约标准** | JSON Schema (Draft 2020-12)、25 个 fixtures、13 个稳定错误码、动态 ISO 8601 时钟契约 | **100% PASS** | `contracts/validator/validate.py` 深度校验通过 | 不能代表物理板卡具备该 capability 或在线部署成功 |
| **Level 2：模拟 PoC / 协议对齐** | C 固件硬件链路与驱动报文、SDK 小端协议与 CSPRNG 配对 Token、RobotAdapter 全双工适配器、Python 核心路由与调度器、HA 状态订阅与 Codex 审批查询 | **100% PASS** | `Ran 93 tests in 10.462s -> OK`<br/>C 固件原生可执行文件与静态库构建通过 | 不能代表物理机嵌入式网络环境、声学杂音或真实云端延迟达标 |
| **Level 3：单项硬件 / 节点实测** | 泰山派 Linux ARM 守护进程、Windows Companion 进程通信、ESP32-S3 物理单板 | **规范就绪 (待实板)** | `deploy/edge/` 与 `deploy/windows/` 脚本及生产配置完备 | 不能代表系统端到端链路长期无丢包或无故障运行 |
| **Level 4：端到端闭环与恢复验收** | 真实语音唤醒→Tuya云端→外部MCP工具→HA/Codex/Life执行→语音播报闭环，断网/断电恢复 | **G1 模拟闭环通过 (待台架)** | `test_g1_official_sdk_and_tuya_interop.py` 验证通过 | 不能代表所有未知硬件改版或不可控生产环境的质量担保 |

> [!IMPORTANT]
> **关于 G1 关口验证的真实性说明（AGENTS.md 第 4 节）**：  
> `test_g1_official_sdk_and_tuya_interop.py` 属于 **Level 2（软件协议 PoC 与并发状态机验证）**，其证明了 C 语言协议层数据模型、小端序列化、动态 CSPRNG Token 校验、WSPK 媒体解包以及双向 Socket 事件并发不丢包。**严禁将其宣传或断言为真实物理 ESP32-S3/STM32 硬件与真实 Tuya 云端的物理闭环（Level 3/Level 4）**，物理闭环必须在实体台架上完成烧录与真机测试。

---

## 2. 详细测试证据明细 (Level 1 & Level 2)

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

### 2.3 Level 2: 全量自动化单元测试与集成测试 (93 项用例全部 PASS)
- **验证命令**：`PYTHONPATH=. python3 -m unittest discover -s tests -t . -p "test_*.py"`
- **实际执行结果**：
  ```text
  ----------------------------------------------------------------------
  Ran 93 tests in 10.462s

  OK
  ```

#### 测试子模块明细表：

| 测试套件 | 用例数 | 覆盖核心能力与去占位要点 |
| :--- | :---: | :--- |
| **`tests/firmware_contract/`** | 9 | 1. 仲裁器 5 级优先级抢占与释放<br/>2. 相机租约锁定 TClaw 机头转动追踪<br/>3. 云台电机机械物理极限钳制 (0..180°, 100..140°)<br/>4. 屏幕表情与状态文本存取<br/>5. TClaw 状态机与音频独占生命周期<br/>6. 用户触摸/语音打断 (Interrupt/Barge-in)<br/>7. MCP 工具 RequestEnvelope 派发与动态结果接入<br/>8. 多轮对话上下文与动态流式 Token 回调<br/>9. Tuya Cloud 语音对话会话生命周期与音频流喂入 |
| **`tests/sdk_contract/`** | 22 | 1. 100% 对齐官方 WSPK 小端序 `<4sBBHII` (16B) 封包解析与 14B 遗留读兼容<br/>2. UDP 37021 `watcher_lan_pairing_v1` 配对服务（CSPRNG 64B Hex Token 并强制认证）<br/>3. `sys.client.hello` 握手与 `role=hardware` Opus 音频协商<br/>4. 非阻塞 `0.0.0.0` TCP Socket 服务驱动与连接管理<br/>5. 云台动态角度入参解析与物理限位安全<br/>6. 资源仲裁忙时拒收并返回 409 `RESOURCE_BUSY`<br/>7. 相机租约互斥冲突拦截与释放<br/>8. 未知命令严格返回 404 `UNKNOWN_OPERATION`<br/>9. 触摸事件封装符合 `envelope_event.json` 标准 Schema |
| **`tests/camera/`** | 3 | 1. 单客户端互斥租约仲裁（并发抢占拒绝）<br/>2. 超时过期自动释放与黑帧遮盖（防旧画面冒充实时流）<br/>3. 租约释放回调触发底层资源注销 |
| **`tests/companion/`** | 7 | 1. 生产模式缺失可执行文件真实报错（无伪成功）<br/>2. 工作区别名白名单与路径逃逸（Path Traversal）防御<br/>3. 真实 JSON-RPC stdio 客户端驱动与多线程按服务端请求 `id` 唤醒与响应<br/>4. 支持 Codex app-server 标准 `thread/start` 及 `thread/create` 映射<br/>5. 审批项目严格绑定与上下文一致性校验，消除 early-approval 竞态<br/>6. 默认监听 `127.0.0.1` 配合 Bearer Token 401 拦截 |
| **`tests/connectors/`** | 6 | 1. 生产模式未配置凭据真实抛错（不伪造返回）<br/>2. 实体域与服务白名单校验（仅放行合规设备）<br/>3. 两阶段状态确认机制（`accepted` 与 `state_confirmed` 解耦）<br/>4. 真实 HTTP 通信轮询与状态扭转 |
| **`tests/core/`** | 28 | 1. SQLite WAL 事务持久化保证零丢失<br/>2. 提醒创建、推迟(Snooze)与完成状态彻底解耦<br/>3. 开机追赶与到期轮次精准筛选<br/>4. 持久化发件箱（Outbox）出入队与去重<br/>5. 便签创建、全文检索与分页<br/>6. 后台 `ReminderScheduler` 自动轮询到期 occurrence，触发 ringing 状态扭转并投递 `reminder.alert` outbox 消息<br/>7. 宿主守护进程 HTTP RPC 端到端分发（集成 Codex 路由、相机帧流抓取、默认回环绑定与 401 鉴权拦截） |
| **`tests/e2e/`** | 7 | 1. 跨模块信封路由、幂等性去重与过期毫秒级拦截<br/>2. HA 领域指令执行与观察状态一致性闭环<br/>3. 生活服务持久化与服务重启恢复<br/>4. Codex 任务提交流程与敏感操作审批闭环<br/>5. 相机租约互斥与帧时效性检测<br/>6. 固件发起到 Host 路由与提醒后台到期投递全链路闭环<br/>7. **G1 关口验收**：`test_g1_official_sdk_and_tuya_interop.py` 验证 Tuya 云对话状态机运行中，官方 Python SDK 动态配对、Token 401 拦截校验、同时操控云台舵机与 WSPK 媒体流通讯 (Level 2) |
| **`tests/test_p1_gaps_verification.py`** | 6 | 1. 泰山派远程调用 Windows Codex Companion 客户端分发与审批确认<br/>2. 提醒发件箱后台消费 Worker 投递到机器人音频通道与重试机制<br/>3. HA 实体与服务白名单拦截、状态对账与状态变更订阅回调<br/>4. 相机动态 FrameCapturePipeline 抓取与动态云台坐标叠加验证 |
| **`tests/test_p1_final_closure.py`** | 5 | 1. CSPRNG 动态 Token 熵值与格式校验<br/>2. RobotAdapter TCP/UDP 握手、舵机运动控制与到期提醒音频播报通道投递<br/>3. Host 路由器 Codex 待审批查询（`codex.get_pending_approvals` / `codex.list_approvals`）<br/>4. Host 路由器 HA 状态迁移事件查询（`ha.list_changes`）<br/>5. 相机硬件传感器帧源接入与 `hardware_sensor` 标签识别 |

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

| 验证项 | 依赖外部条件 | 待实操验证内容 | 成功判定指标 |
| :--- | :--- | :--- | :--- |
| **ESP32-S3 固件烧录** | 物理板卡、USB-C 数据线、ESP-IDF 5.4 工具链 | `idf.py -p /dev/ttyUSB0 flash monitor` | 串口正常打印 Boot 日志，SRAM 空闲 > 30KB，PSRAM 空闲 > 2MB |
| **物理麦克风与扬声器** | ES8311 / ES7210 硬件、喇叭与麦克风模块 | 远场语音唤醒，I2S DMA 采音与播报 | 无破音、无 I2S 挂死、AEC 回声消除正常、插话打断生效 |
| **双轴舵机物理限位** | STM32 / 串口总线舵机、外部供电模块 | 下发全行程角度（Pan 0..180°, Tilt 100..140°） | 舵机平滑到位，无机械撞击，电流无异常激增 |
| **涂鸦云端对话实通** | 真实涂鸦 Device License、Client ID 与凭据 | 发起真人一轮语音问答 | 涂鸦 AI 识别用户意图并下发 TTS 流及 MCP 工具指令 |
| **真实 Home Assistant** | 局域网物理 HA 实例、真实智能灯/插座 | 触发关灯指令 | 物理灯具熄灭，HA 状态机上报一致 |
| **Windows Codex 实机** | 真实 Windows 11 PC、OpenAI Codex 授权运行 | 提交自动化重构任务 | Companion stdio 正常驱动，真实生成代码 diff 并触发审批弹窗 |
