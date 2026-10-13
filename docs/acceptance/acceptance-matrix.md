# Desk Agent R1 全系统验收矩阵与证据清单 (Acceptance Matrix)

> 文档版本：V2.0.0  
> 责任 Agent：A7 系统集成与交付验证  
> 参照标准：`AGENTS.md` 架构治理规范、实施计划关口目标与 PR #1 审阅意见  
> 状态：**全模块生产实现与自动化契约单测完成，62 项全量测试全部 PASS (100% OK)**

---

## 1. 证据分层标准与总体状态 (Evidence Layering)

根据 `AGENTS.md` 第 4 节定义，严格执行证据分层核验，坚决杜绝用假数据、内存状态或模拟输出夸大真实物理交付：

| 证据层级 | 验收范围与含义 | 当前结论 | 关键依据与测试输出 | 绝对不能代表什么 |
| :--- | :--- | :---: | :--- | :--- |
| **Level 1：公开源码 / 契约标准** | JSON Schema (Draft 2020-12)、25 个 fixtures、13 个稳定错误码、接口映射表 | **100% PASS** | `contracts/validator/validate.py` 深度校验通过 | 不能代表物理板卡具备该 capability 或在线部署成功 |
| **Level 2：模拟 PoC / 契约检查** | C 固件桥接、仲裁器状态机、Python 核心路由、Life/HA/Codex/Camera 逻辑 | **100% PASS** | `Ran 62 tests in 1.956s -> OK`<br/>C 固件跨平台原生二进制执行通过 | 不能代表物理机嵌入式网络环境、声学杂音或真实云端延迟达标 |
| **Level 3：单项硬件 / 节点实测** | 泰山派 Linux ARM 守护进程、Windows Companion 进程通信、ESP32-S3 物理单板 | **规范就绪 (待实板)** | `deploy/edge/` 与 `deploy/windows/` 脚本及生产配置完备 | 不能代表系统端到端链路长期无丢包或无故障运行 |
| **Level 4：端到端闭环与恢复验收** | 真实语音唤醒→Tuya云端→外部MCP工具→HA/Codex/Life执行→语音播报闭环，断网/断电恢复 | **待物理台架** | 接口时序与端到端模拟链路 100% 连通（见 Section 2.3） | 不能代表所有未知硬件改版或不可控生产环境的质量担保 |

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
  gcc -O2 -Wall \
    -I firmware/components/desk_hardware/include \
    -I firmware/components/desk_tclaw/include \
    -I firmware/components/desk_sdk_bridge/include \
    firmware/components/desk_hardware/src/desk_arbiter.c \
    firmware/components/desk_hardware/src/desk_hardware.c \
    firmware/components/desk_tclaw/src/desk_tclaw_bridge.c \
    firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c \
    firmware/main/src/app_main.c -o /tmp/desk_firmware_main
  /tmp/desk_firmware_main
  ```
- **实际输出**：
  ```text
  ========================================
    Desk Agent R1 Firmware Booting...     
    Target: ESP32-S3 | Baseline: IDF v5.4 
  ========================================
  [BOOT] Hardware drivers & Resource Arbiter initialized.
  [BOOT] WatcheRobot SDK Protocol Bridge initialized.
  [BOOT] TClaw Dialogue Engine & MCP Tools started successfully.
  [READY] Desk Agent R1 Ready for Voice & SDK Operations.
  ```

### 2.3 Level 2: 全量自动化单元测试与集成测试 (62 项用例)
- **验证命令**：`PYTHONPATH=. python3 -m unittest discover -s tests -t . -p "test_*.py"`
- **实际执行结果**：
  ```text
  ----------------------------------------------------------------------
  Ran 62 tests in 1.956s

  OK
  ```

#### 测试子模块明细表：

| 测试套件 | 用例数 | 覆盖核心能力与去占位要点 |
| :--- | :---: | :--- |
| **`tests/firmware_contract/`** | 8 | 1. 仲裁器 5 级优先级抢占与释放<br/>2. 相机租约锁定 TClaw 机头转动追踪<br/>3. 云台电机机械物理极限钳制 (0..180°, 100..140°)<br/>4. 屏幕表情与状态文本存取<br/>5. TClaw 状态机与音频独占生命周期<br/>6. 用户触摸/语音打断 (Interrupt/Barge-in)<br/>7. MCP 工具 RequestEnvelope 派发与结果接入 |
| **`tests/sdk_contract/`** | 14 | 1. WSPK 16 字节头部封装、网络大端序与有效载荷上限防 OOM<br/>2. `sys.client.hello` 握手与 5 项 capabilities 协商<br/>3. 云台动态角度入参解析与物理限位安全<br/>4. 资源仲裁忙时拒收并返回 409 `RESOURCE_BUSY`<br/>5. 相机租约互斥冲突拦截与释放<br/>6. 未知命令严格返回 404 `UNKNOWN_OPERATION`<br/>7. 触摸事件封装符合 `envelope_event.json` 标准 Schema<br/>8. 双向工具 Request/Response RPC 转发 |
| **`tests/camera/`** | 3 | 1. 单客户端互斥租约仲裁（并发抢占拒绝）<br/>2. 超时过期自动释放与黑帧遮盖（防旧画面冒充实时流）<br/>3. 租约释放回调触发底层资源注销 |
| **`tests/companion/`** | 9 | 1. 生产模式缺失可执行文件真实报错（无伪成功）<br/>2. 工作区别名白名单与路径逃逸（Path Traversal）防御<br/>3. 真实 JSON-RPC stdio 客户端驱动与 Thread/Turn 生命周期<br/>4. 审批项目严格绑定与上下文一致性校验 |
| **`tests/connectors/`** | 6 | 1. 生产模式未配置凭据真实抛错（不伪造返回）<br/>2. 实体域白名单校验（仅放行合规设备）<br/>3. 两阶段状态确认机制（`accepted` 与 `state_confirmed` 解耦）<br/>4. 真实 HTTP 通信轮询与状态扭转 |
| **`tests/core/`** | 17 | 1. SQLite WAL 事务持久化保证零丢失<br/>2. 提醒创建、推迟(Snooze)与完成状态彻底解耦<br/>3. 开机追赶与到期轮次精准筛选<br/>4. 持久化发件箱（Outbox）出入队与去重<br/>5. 便签创建、全文检索与分页<br/>6. 宿主守护进程 HTTP RPC 端到端分发 |
| **`tests/e2e/`** | 5 | 1. 跨模块信封路由、幂等性去重与过期毫秒级拦截<br/>2. HA 领域指令执行与观察状态一致性闭环<br/>3. 生活服务持久化与服务重启恢复<br/>4. Codex 任务提交流程与敏感操作审批闭环<br/>5. 相机租约互斥与帧时效性检测 |

---

## 3. 架构规范与去占位整改对照表

| 架构规范项 | 改造前问题 (占位/伪实现) | 改造后生产实现 (真实接入) |
| :--- | :--- | :--- |
| **A1 硬件仲裁** | 假状态变量，无抢占机制，无打断机制 | 5 级优先级抢占状态机，相机租约锁机头跟随，单 Audio Owner 互斥与用户打断 |
| **A2 SDK 兼容** | `motion.move` 硬编码固定角度，绕过仲裁直接调硬件；未知命令返回假 ACK | 动态参数解析与限位，强制调用仲裁器（忙碌返 409），实现 WSPK 16 字节封包，未知命令返 404 |
| **A3 生活服务** | 纯内存字典，服务重启数据丢失；推迟操作误将任务置为完成 | SQLite WAL 事务表持久化，重启自愈追赶，到期轮次精确筛选，支持 Outbox |
| **A4 HA 接入** | 固定返回 mock 数据；提交即假定成功，未核实物理状态 | 真实 HTTP REST 通信，实体域白名单强校验，带超时的两阶段真实状态确认机制 |
| **A5 Codex** | 伪造内存假 ID（`thr_win_...`），审批未绑定实际上下文 | 真实 stdio JSON-RPC 客户端驱动，目录逃逸防护，审批项强校验与关联回传 |
| **A6 LAN 相机** | 硬编码 1x1 假 JPEG，并发查看无冲突控制，旧画面冒充实时流 | 单客户端互斥租约仲裁，有界帧缓存，超时黑帧遮盖，心跳续订机制 |

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
