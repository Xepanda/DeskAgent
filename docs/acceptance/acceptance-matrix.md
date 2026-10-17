# Desk Agent R1 全系统验收矩阵与证据清单 (Acceptance Matrix)

> 文档版本：V3.2.0  
> 责任 Agent：A0 架构与协议（基线与证据校准） / A7 系统集成与交付验证  
> 参照标准：`AGENTS.md` 架构治理规范、实施计划关口目标与 PR #2 审查意见  
> 状态：**G0 契约与基线冻结完成；Level 1 (契约/Schema) 与 Level 2 (软件协议与模拟互通 PoC、真实链路 E2E、跨模块 P1 回归门禁、故障注入与稳定性压测) 自动化测试全部 PASS (266 项用例 100% OK)；提供一键全量核验工具 `tools/verification/verify_all.sh` (8 阶段)；G1 真实关口与硬件接入待真机台架实测 (Level 3/4 Pending)**  
> V3.2.0 变更：新增 A7 跨模块 P1 回归门禁 `tests/e2e/test_p1_regression_gates.py`（PR #11 整改：驱动故障绝不伪成功、无 ACK 超时绝不报完成、乱序并发 ACK 不串线、超时后迟到 ACK 不污染新请求、真实 socket 流分帧、配对 fail-closed、Companion 局域网启动门禁、既有链路无回归）；`verify_all.sh` 由 7 阶段扩展为 8 阶段并将回归门禁纳入总控执行；校准全量用例数为实测 266 项

---

## 1. 证据分层标准与总体状态 (Evidence Layering)

根据 `AGENTS.md` 第 4 节定义，严格执行证据分层核验，坚决杜绝用假数据、内存状态或模拟输出夸大真实物理交付：

| 证据层级 | 验收范围与含义 | 当前结论 | 关键依据与测试输出 | 绝对不能代表什么 |
| :--- | :--- | :---: | :--- | :--- |
| **Level 1：公开源码 / 契约标准** | JSON Schema (Draft 2020-12)、25 个 fixtures、13 个稳定错误码、动态 ISO 8601 时钟契约 | **100% PASS** | `contracts/validator/validate.py` 深度校验通过 | 不能代表物理板卡具备该 capability 或在线部署成功 |
| **Level 2：模拟 PoC / 协议对齐** | C 固件硬件链路与驱动报文、SDK 小端协议与 CSPRNG 配对 Token、RobotAdapter 全双工适配器、真实链路 E2E（设备工具请求经 TCP 贯通 Host 路由并原路回传）、跨模块 P1 回归门禁（驱动故障/超时绝不伪成功、请求关联、TCP 分帧、配对 fail-closed、Companion 启动门禁）、Python 核心路由与调度器、HA 状态订阅与 Codex 审批查询、E2E 故障注入与 200 轮全系统稳定性压测 | **100% PASS** | `Ran 266 tests in 37.645s -> OK`<br/>`tools/verification/verify_all.sh` 8 大阶段 100% 通过<br/>C 固件原生可执行文件与静态库构建通过 | 不能代表物理机嵌入式网络环境、声学杂音或真实云端延迟达标 |
| **Level 3：单项硬件 / 节点实测** | 泰山派 Linux ARM 守护进程、Windows Companion 进程通信、ESP32-S3 物理单板 | **未实测 (待现场实板)** | `deploy/edge/` 与 `deploy/windows/` 脚本及生产配置完备 | 不能代表系统端到端链路长期无丢包或无故障运行 |
| **Level 4：端到端闭环与恢复验收** | 真实语音唤醒→Tuya云端→外部MCP工具→HA/Codex/Life执行→语音播报闭环，断网/断电恢复 | **未完成 (Pending 实体台架)** | 真实环境待具备 | 不能代表所有未知硬件改版或不可控生产环境的质量担保 |

> [!IMPORTANT]
> **关于 G1 关口验收状态的严格校正（遵照 AGENTS.md 第 4 节）**：  
> `test_g1_official_sdk_and_tuya_interop.py` 属于 **Level 2（软件协议 PoC 与并发状态机验证）**，其实际执行的是 GCC 编译本项目 C 代码、ctypes 动态调用以及 Python 本地 Socket 模拟。**严禁将其宣称为实施计划定义的 G1 关口已完成**。  
> 真实的 **G1 关口通过标准** 为：**ESP32-S3 真机固件连接真实 Tuya 云端完成一轮语音对话 + 官方 WatcheRobot Runtime 接入 + 一个受控物理动作（如转头或表情）端到端闭环**。该工作包由下一阶段 A1（真实固件/驱动/Tuya云）与 A2（官方 Runtime/SDK 适配）联合实测完成。

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

### 2.3 Level 2: 全量自动化单元测试与集成测试 (266 项用例全部 PASS)
- **验证命令**：`PYTHONPATH=. python3 -m unittest discover -s tests -t . -p "test_*.py"`
- **实际执行结果**：
  ```text
  ----------------------------------------------------------------------
  Ran 266 tests in 37.645s

  OK
  ```

#### 测试子模块明细表：

| 测试套件 | 用例数 | 覆盖核心能力与仿真要点 (Level 2) |
| :--- | :---: | :--- |
| **`tests/firmware_contract/`** | 22 | 1. 仲裁器 5 级优先级抢占与释放<br/>2. 相机租约锁定 TClaw 机头转动追踪<br/>3. 云台电机机械物理极限钳制 (0..180°, 100..140°)<br/>4. 屏幕表情与状态文本存取<br/>5. TClaw 状态机与音频独占生命周期<br/>6. 用户触摸/语音打断 (Interrupt/Barge-in)<br/>7. MCP 工具 RequestEnvelope 派发与动态结果接入<br/>8. 多轮对话上下文与动态流式 Token 回调<br/>9. Tuya Cloud 语音对话会话生命周期与音频流喂入仿真 |
| **`tests/sdk_contract/`** | 67 | 1. 100% 对齐官方 WSPK 小端序 `<4sBBHII` (16B) 封包解析与 14B 遗留读兼容<br/>2. UDP 37021 `watcher_lan_pairing_v1` 配对服务（CSPRNG 64B Hex Token 并强制认证）<br/>3. `sys.client.hello` 握手与 `role=hardware` Opus 音频协商<br/>4. 非阻塞 `0.0.0.0` TCP Socket 服务驱动与连接管理<br/>5. 云台动态角度入参解析与物理限位安全<br/>6. 资源仲裁忙时拒收并返回 409 `RESOURCE_BUSY`<br/>7. 相机租约互斥冲突拦截与释放<br/>8. 未知命令严格返回 404 `UNKNOWN_OPERATION`<br/>9. 触摸事件封装符合 `envelope_event.json` 标准 Schema<br/>10. ACK 超时严格返回 408 NACK 而非伪成功 `code: 0`<br/>11. **R3 TCP 持久缓冲分帧**：半包重组、粘包多消息、超长帧丢弃后重同步、断连残留缓冲清空<br/>12. **R4 配对码 fail-closed**：未配置/格式错/不匹配一律拒绝且不发 token<br/>13. 官方 WatcheRobot Runtime 契约字段级对齐与机器人适配器端到端联调 |
| **`tests/camera/`** | 9 | 1. 单客户端互斥租约仲裁（并发抢占拒绝返回 `RESOURCE_BUSY`）<br/>2. 超时过期自动释放与黑帧遮盖（防旧画面冒充实时流）<br/>3. 租约释放回调触发底层资源注销<br/>4. 硬件真实传感器与合成画面源精准标注（`is_hardware` / `source` 字段标签）<br/>5. 自动续租租约延长机制<br/>6. Web 控制台 Token 鉴权（Bearer、Header、Query 三通道）及云台安全角度操作 |
| **`tests/companion/`** | 40 | 1. 生产模式缺失可执行文件真实报错（无伪成功）<br/>2. 工作区别名白名单与路径逃逸（Path Traversal）防御<br/>3. 真实 JSON-RPC stdio 客户端驱动与多线程按服务端请求 `id` 唤醒与响应<br/>4. 支持 Codex app-server 标准 `thread/start` 及 `thread/create` 映射<br/>5. 审批项目严格绑定与上下文一致性校验，消除 early-approval 竞态<br/>6. 默认监听 `127.0.0.1` 配合 Bearer Token 401 拦截<br/>7. `RemoteCodexCompanionClient` 远程 HTTP POST /rpc 代理调用及审批应答通道<br/>8. 对齐 codex-cli 0.161.0 审批枚举与 `turn/interrupt` 驼峰字段<br/>9. **R5 启动安全门禁**：非回环监听强制 ≥16 字符非占位强令牌，空/占位/短令牌一律拒绝启动（含所有启动入口统一校验路径） |
| **`tests/connectors/`** | 26 | 1. 生产模式未配置凭据真实抛错（不伪造返回）<br/>2. 实体域与服务黑白名单多级校验（放行合规设备，拦截锁/安防/更新等敏感域）<br/>3. 两阶段状态确认机制（`accepted` 与 `state_confirmed` 解耦）<br/>4. 目标属性比对确认（亮度/色温等未达目标时 `state_confirmed=False`）<br/>5. 真实 HTTP 通信轮询与状态扭转<br/>6. WebSocket `/api/websocket` 客户端鉴权握手与状态流式订阅<br/>7. 指数退避断网重连与状态对账消除状态漂移<br/>8. 双通道容灾（WebSocket 中断时平滑降级 REST 轮询）与 HTTP 429 限流退避 |
| **`tests/core/`** | 51 | 1. SQLite WAL 事务持久化保证零丢失<br/>2. 提醒创建、推迟(Snooze)与完成状态彻底解耦<br/>3. 开机追赶与到期轮次精准筛选<br/>4. 持久化发件箱（Outbox）出入队与去重<br/>5. 便签创建、全文检索与分页<br/>6. 后台 `ReminderScheduler` 自动轮询到期 occurrence，触发 ringing 状态扭转并投递 `reminder.alert` outbox 消息<br/>7. 宿主守护进程 HTTP RPC 端到端分发（集成 Codex 路由、相机帧流抓取、默认回环绑定与 401 鉴权拦截）<br/>8. 单事务原子触发（occurrence ringing / reminder ringing / 周期排程 / outbox 入队绝不脱节）<br/>9. 时区感知 (`zoneinfo`) 跨天与跨夏令时重复计算<br/>10. **R1/R2 运动 ACK 语义与请求关联**：固件 ACK 只判 `accepted`、驱动失败→`HARDWARE_FAULT`、静默→`unknown`+`OPERATION_TIMEOUT`、乱序/迟到/无 `request_id` 的 ACK 一律丢弃、`request_id` 并发唯一 |
| **`tests/e2e/`** | 42 | 1. 跨模块信封路由、幂等性去重与过期毫秒级拦截<br/>2. HA 领域指令执行与观察状态一致性闭环<br/>3. 生活服务持久化与服务重启恢复<br/>4. Codex 任务提交流程与敏感操作审批闭环<br/>5. 相机租约互斥与帧时效性检测<br/>6. 固件发起到 Host 路由与提醒后台到期投递全链路闭环<br/>7. **G1 软件协议仿真 (Level 2 PoC)**：`test_g1_official_sdk_and_tuya_interop.py`<br/>8. **A7 E2E 故障注入与网络自愈 (`test_e2e_fault_injection.py`)**：HA WebSocket 突发断连重连与状态对账自愈、进程异常退出与 SQLite WAL 崩溃恢复（到期提醒自动打上 `is_catchup=True` 追赶标记）、10 个高并发客户端争夺相机租约的绝对互斥仲裁、Router 高并发重复请求幂等缓存去重<br/>9. **A7 全系统长期稳定性压测 (`test_system_stability_stress.py`)**：200 轮跨域高频密集连续操作（50 提醒 + 50 HA 两阶段 + 50 Codex 任务/审批 + 50 相机拉流/续租），执行后校验 SQLite `PRAGMA integrity_check` 与 `foreign_key_check` 零损坏零外键违背<br/>10. **A7 真实链路端到端 (`test_real_chain_tclaw_to_host.py`)**：起真实 `DeskAgentHostApp`（真实 RobotAdapter / Router / LifeService / 后台调度器与发件箱 Worker），对端以忠实伪固件复现 `desk_sdk_bridge` TCP 服务与 TClaw 对话引擎，严格按 C 固件线格式收发。覆盖：工具请求经 TCP 贯通 Host 路由并在 SQLite 真实落库、固件 ACK 仅判 `accepted`（`state_confirmed=False`）、**固件扣发 ACK 时超时判定为 `unknown`+`OPERATION_TIMEOUT` 且绝不伪报成功**、到期提醒经调度器/发件箱回推 `ctrl.expression.play` 播报且 outbox 行为 `dispatched`（非退避）、未知操作与过期信封错误原路回传、脏数据不破坏链路、链路断开后自动重连并恢复链路<br/>11. **A7 跨模块 P1 回归门禁 (`test_p1_regression_gates.py`)**：见 §2.6 |
| **`tests/test_p1_gaps_verification.py`** | 4 | 1. 泰山派远程调用 Windows Codex Companion 客户端分发与审批确认<br/>2. 提醒发件箱后台消费 Worker 投递到机器人音频通道与重试机制<br/>3. HA 实体与服务白名单拦截、状态对账与状态变更订阅回调<br/>4. 相机动态 FrameCapturePipeline 抓取与动态云台坐标叠加验证 |
| **`tests/test_p1_final_closure.py`** | 5 | 1. CSPRNG 动态 Token 熵值与格式校验<br/>2. RobotAdapter TCP/UDP 握手、舵机运动控制与到期提醒音频播报通道投递<br/>3. Host 路由器 Codex 待审批查询（`codex.get_pending_approvals` / `codex.list_approvals`）<br/>4. Host 路由器 HA 状态迁移事件查询（`ha.list_changes`）<br/>5. 相机硬件传感器帧源接入与 `hardware_sensor` 标签识别 |

### 2.4 一键系统级验收套件 (`tools/verification/verify_all.sh`)
为落实 `AGENTS.md` 规范第 4 节，提供自动化脚本对 Level 1 ~ Level 4 进行统一检查：
```bash
./tools/verification/verify_all.sh
```
- **阶段 1 (Level 1)**: 契约与 JSON Schema (Draft 2020-12) 深度校验 -> **PASS**
- **阶段 2 (Level 2)**: C 固件原生 HAL 与 SDK Bridge 静态编译验证 -> **PASS**
- **阶段 3 (Level 2)**: 266 项全量自动化测试套件执行（含故障注入与稳定性压测） -> **PASS**
- **阶段 4 (Level 2)**: 真实链路端到端验证（TClaw → SDK Bridge TCP → Host Router → Life 执行 → 结果回传设备） -> **PASS**
- **阶段 5 (Level 2)**: 跨模块 P1 回归门禁（驱动故障/超时防伪成功、ACK 关联、TCP 分帧、配对 fail-closed、Companion 局域网启动门禁） -> **PASS**
- **阶段 6 (Level 2)**: 泰山派 Linux 边缘环境 Pre-flight 检查 -> **PASS**
- **阶段 7 (Level 3/4)**: 物理单板与在线台架硬性约束审计 -> **PASS**（仅审计缺口清单完备性，不代表硬件已实测）
- **阶段 8 (Level 1/4)**: 证据分层防夸大审计门禁 -> **PASS**（校验 Level 3/4 仍如实标记为未实测/未完成，且验收矩阵与真实链路套件保持同步）

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
