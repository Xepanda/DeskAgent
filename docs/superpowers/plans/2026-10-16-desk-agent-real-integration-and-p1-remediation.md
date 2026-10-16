# Desk Agent R1 真实接入与关键缺陷收敛实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐真实设备工具请求到 Host 路由的通道、修复 ACK 假成功与超时返回、对齐 Codex 0.161.0 官方协议与初始化、打通相机会话与网页端云台控制、实现提醒单事务持久化与时区计算，达成真正的 G1 真实链路闭环。

**Architecture:** 
- 固件端（A1/A2）：在 `desk_sdk_bridge` 补全向客户端 TCP 发送工具请求报文，并在 `app_main.c` 连接 TClaw 与 SDK Bridge；支持 `ctrl.expression.play` 表情与音频播报；在没有收到 ACK 时严禁伪造成功（返回 408 超时）；
- 边缘主机端（A3/A4/A6）：`RobotAdapter` 注册工具请求回调，接入 `Router.dispatch` 并原路回传执行结果；生活服务使用单一事务完成 ringing 状态与 outbox 入队；HA 增强目标属性比对并支持配置服务名单；网页端补全帧来源响应头与 `resource.motion.stop`；
- Windows 伴侣端（A5）：根据 `codex-cli 0.161.0` Schema 对齐审批响应枚举（`accept`/`decline`/`cancel` 及 `approved`/`denied`）、取消调用使用 `turn/interrupt` 及 `threadId`/`turnId` 字段，增加 `initialize` 握手。

**Tech Stack:** C11 (ESP-IDF 5.4 / GCC), Python 3.10+, SQLite3 WAL, JSON-RPC 2.0, WebSocket, Codex CLI 0.161.0, WSPK binary protocol.

## Global Constraints

- 遵照 `AGENTS.md` 边界治理：A0~A7 各司其职，严禁跨模块乱写代码；
- 绝不使用假数据或伪造 ACK：未收到硬件或后端真实响应时严格返回错误/超时；
- 严禁破坏性 git 操作（禁止 `git reset --hard`、`git clean -fd`）；
- 严格遵循证据分层（Level 1~Level 4），不夸大无实体硬件环境下的交付状态。

---

### Task 1: [A0/A2/A3] 贯通设备工具请求到 Host Router 与消除 ACK 假成功

**Files:**
- Modify: `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c:795-815`
- Modify: `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c:660-690`
- Modify: `host/desk_agent/connectors/robot/robot_adapter.py:270-285`
- Modify: `host/desk_agent/main.py:170-190`
- Test: `tests/sdk_contract/test_sdk_bridge_protocol.py`
- Test: `tests/core/test_host_daemon.py`

**Interfaces:**
- `desk_sdk_bridge_dispatch_tool_request(const char *request_envelope_json)`: 必须向活动客户端 TCP Socket (`s_client_fd`) 写入报文与换行符。
- `RobotAdapter.send_command(...)`: 超时未收到 ACK 时返回 `{"type": "sys.nack", "code": 408, "error": {"code": "TIMEOUT", "message": "No ACK received from robot within timeout"}}`，禁止返回 `code: 0`。
- `DeskAgentHostApp._on_robot_tool_request(dict)`: 将硬件收到的工具请求派发至 `self.router.dispatch(...)`，并将结果通过 `self.robot_adapter.send_tool_response(...)` 送回。

- [ ] **Step 1: 编写失败用例验证 ACK 超时返回 408 以及工具请求派发通道**
  - 在 `tests/sdk_contract/test_sdk_bridge_protocol.py` 与 `tests/core/test_host_daemon.py` 中编写测试：
    1. 模拟设备向 RobotAdapter 发送工具请求，验证触发 Host 路由并在生活服务创建提醒；
    2. 验证 RobotAdapter 发送命令超时不再返回 `sent_unacknowledged` 假成功，而是返回 408 NACK。
- [ ] **Step 2: 运行测试确保失败**
  - 执行 `PYTHONPATH=. python3 -m unittest tests/sdk_contract/test_sdk_bridge_protocol.py`，确认失败。
- [ ] **Step 3: 实施 desk_sdk_bridge.c、robot_adapter.py 与 main.py 修改**
  - 在 `desk_sdk_bridge.c` 中，使 `desk_sdk_bridge_dispatch_tool_request` 同时写入 `s_client_fd`；
  - 在 `robot_adapter.py` 中，超时返回 `{"type": "sys.nack", "code": 408, "status": "timeout", "error": {"code": "TIMEOUT", "message": "No ACK received from robot within timeout"}}`；
  - 在 `main.py` 中注册 `self.robot_adapter.register_tool_request_handler(self._on_robot_tool_request)`。
- [ ] **Step 4: 运行测试确保通过**
  - 执行 `PYTHONPATH=. python3 -m unittest discover -s tests -t . -p "test_*.py"`，确认测试全部通过。
- [ ] **Step 5: 提交代码**
  - `git commit -m "fix(bridge): wire device tool requests to host router and eliminate false success on ACK timeout"`

---

### Task 2: [A1/A2] 固件支持 ctrl.expression.play 与真实硬件驱动挂钩

**Files:**
- Modify: `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c:660-690`
- Modify: `firmware/components/desk_hardware/src/desk_hardware.c:170-225`
- Test: `tests/firmware_contract/test_desk_hardware_arbiter.py`
- Test: `tests/sdk_contract/test_sdk_bridge_protocol.py`

**Interfaces:**
- `desk_sdk_bridge_process_line`: 支持解析 `ctrl.expression.play` 与 `ctrl.expression.set` 命令，申请 `DESK_RES_DISPLAY` 仲裁，调用 `desk_hardware_display_set_expression`，若包含声音或提醒播报，申请 `DESK_RES_AUDIO_PLAYBACK` 并调用 `desk_hardware_audio_play_pcm`。
- `default_driver_display_expression` / `default_driver_motion_move`: 绑定实际输出或在没有真实硬件时准确报错，杜绝伪静默吞没。

- [ ] **Step 1: 编写测试用例**
  - 在 `tests/sdk_contract/test_sdk_bridge_protocol.py` 中增加对 `ctrl.expression.play` 的解析与仲裁响应测试。
- [ ] **Step 2: 实施 SDK 桥接与驱动更新**
  - 在 `desk_sdk_bridge.c` 中增加对 `ctrl.expression.play` 的处理，正确返回 `sys.ack`；
  - 完善 `desk_hardware.c` 中的驱动钩子，确保运动下发 STM32 串口协议帧，显示表情有真实输出通路。
- [ ] **Step 3: 编译并运行验证**
  - `/usr/bin/cmake -B firmware/build -S firmware && /usr/bin/cmake --build firmware/build`
  - `PYTHONPATH=. python3 -m unittest tests/sdk_contract/test_sdk_bridge_protocol.py`
- [ ] **Step 4: 提交代码**
  - `git commit -m "feat(firmware): support ctrl.expression.play announcement and real board driver bindings"`

---

### Task 3: [A3] 提醒单事务持久化、时区重复计算与边缘安装脚本

**Files:**
- Modify: `host/desk_agent/services/life_service.py:30-70, 320-380`
- Modify: `host/desk_agent/main.py:285-315`
- Modify: `deploy/edge/install.sh:30-55`
- Modify: `requirements.txt:1-15`
- Test: `tests/core/test_life_service.py`
- Test: `tests/core/test_host_daemon.py`

**Interfaces:**
- `LifeService.trigger_occurrence_to_outbox(occurrence_id, topic="reminder.alert")`: 单一 SQLite 事务原子更新 occurrence 为 `ringing`、更新 reminder 为 `ringing`、自动排程下一次周期，并写入 outbox 记录。
- `compute_next_occurrence(current_iso, repeat_rule, tz_name=None)`: 引入 `zoneinfo.ZoneInfo(tz_name)` 进行本地时间跨天/跨夏令时准确运算。
- `LifeService.close()`: 显式关闭并释放 SQLite 连接。
- `deploy/edge/install.sh`: 自动创建 venv 并执行 `pip install -r requirements.txt`，同步项目代码至 `/opt/desk-agent`。

- [ ] **Step 1: 编写失败测试**
  - 编写测试验证：发生崩溃时单一事务保证 occurrence ringing 与 outbox 绝不脱节；验证时区感知计算；验证 `close()` 释放连接。
- [ ] **Step 2: 实施 life_service.py、main.py 与 install.sh 修改**
  - 实现 `trigger_occurrence_to_outbox`；
  - 在 `compute_next_occurrence` 中增加 `tz_name` 计算；
  - 补齐 `requirements.txt` 中的 `websockets>=12.0,<14.0`；
  - 完善 `deploy/edge/install.sh` 安装完整流程。
- [ ] **Step 3: 运行测试确保全部通过**
  - `PYTHONPATH=. python3 -m unittest tests/core/test_life_service.py`
- [ ] **Step 4: 提交代码**
  - `git commit -m "fix(life): atomic occurrence triggering, timezone-aware recurrence, and production edge installer"`

---

### Task 4: [A4] HA 连接器属性比对与服务权限配置注入

**Files:**
- Modify: `host/desk_agent/connectors/ha/ha_connector.py:560-600`
- Modify: `host/desk_agent/main.py:130-140`
- Test: `tests/connectors/test_ha_connector.py`

**Interfaces:**
- `HAConnector.execute_service(..., wait_for_state=...)`: 增强状态确认，除 `expected_state` 外，若入参或 `expected_attributes` 指定了属性（如 `brightness`、`color_temp`），必须比对 `attributes` 达成目标才判定 `state_confirmed: True`。
- `DeskAgentHostApp`: 从配置注入 `allowed_services`、`denied_services`、`denied_entities`。

- [ ] **Step 1: 编写测试用例**
  - 在 `tests/connectors/test_ha_connector.py` 增加属性比对确认测试（如开关开启但亮度未达目标时 `state_confirmed: False`，达到后为 `True`）。
- [ ] **Step 2: 实施修改**
  - 在 `ha_connector.py` 中补充 attributes 比对逻辑；
  - 在 `main.py` 中传入配置参数。
- [ ] **Step 3: 运行测试并提交**
  - `PYTHONPATH=. python3 -m unittest tests/connectors/test_ha_connector.py`
  - `git commit -m "feat(ha): enhance two-stage confirmation with target attributes and wire service permissions"`

---

### Task 5: [A5] 对齐 Codex 0.161.0 协议规范与持久化

**Files:**
- Modify: `companion/src/codex_companion.py:150-250, 450-520`
- Test: `tests/companion/test_codex_companion.py`

**Interfaces:**
- `respond_approval`: 对齐 Schema 枚举，`CommandExecutionRequestApprovalResponse` 决策使用 `"accept"` / `"decline"` / `"cancel"`；`ApplyPatchApprovalResponse` 决策使用 `"approved"` / `{"denied": {"rejection": ...}}`。
- `cancel_task`: 调用 `turn/interrupt` 协议，参数采用 camelCase `{"threadId": thread_id, "turnId": turn_id}`。
- 初始化握手：建立 stdio 连接时发送 `initialize` 请求并在成功后确认。
- 任务与待审批状态持久化：使用本地 SQLite 表保存，避免进程重启丢失状态。

- [ ] **Step 1: 编写协议 Schema 对齐测试**
  - 在 `tests/companion/test_codex_companion.py` 中测试 `accept`/`decline` 枚举输出与 `turn/interrupt` 驼峰字段结构。
- [ ] **Step 2: 实施 codex_companion.py 修改**
  - 规范化枚举转换；
  - 参数调整为 `threadId` 与 `turnId`；
  - 加入 `initialize` 握手。
- [ ] **Step 3: 运行测试并提交**
  - `PYTHONPATH=. python3 -m unittest tests/companion/test_codex_companion.py`
  - `git commit -m "fix(companion): align with codex-cli 0.161.0 schema, approval enums, and turn/interrupt params"`

---

### Task 6: [A6] 相机真实帧响应头与 Web 控制台云台控制闭环

**Files:**
- Modify: `host/desk_agent/main.py:560-580, 680-690`
- Modify: `host/desk_agent/connectors/robot/robot_adapter.py:300-320`
- Modify: `firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c:500-530`
- Test: `tests/camera/test_camera_service.py`
- Test: `tests/core/test_host_daemon.py`

**Interfaces:**
- `/api/camera/frame` HTTP 响应：返回 `X-Frame-Source: hardware_sensor` (或 `synthetic_pipeline` / `stale_mask`) 及 `X-Frame-Hardware: 1`。
- `resource.motion.move`: 同时兼容 `pan_deg`/`tilt_deg` 与 `pan`/`tilt`。
- `resource.motion.stop`: 注册路由并向硬件客户端下发 `ctrl.motion.stop`，更新相机服务坐标。

- [ ] **Step 1: 编写失败测试**
  - 验证 `/api/camera/frame` 包含 `X-Frame-Source` 与 `X-Frame-Hardware` 报头；
  - 验证 `resource.motion.move` 传入 `pan_deg` / `tilt_deg` 正确驱动云台；
  - 验证 `resource.motion.stop` 正确路由并返回成功。
- [ ] **Step 2: 实施修改**
  - 更新 `main.py`、`robot_adapter.py` 与 `desk_sdk_bridge.c`。
- [ ] **Step 3: 运行测试并提交**
  - `PYTHONPATH=. python3 -m unittest tests/camera/test_camera_service.py tests/core/test_host_daemon.py`
  - `git commit -m "fix(camera): expose frame source headers and complete pan_deg/tilt_deg and motion.stop handling"`

---

### Task 7: [A7] 端到端真实链路验证与验收矩阵 V3.1.0 升级

**Files:**
- Create: `tests/e2e/test_real_chain_tclaw_to_host.py`
- Modify: `tools/verification/verify_all.sh`
- Modify: `docs/acceptance/acceptance-matrix.md`
- Test: 全量验证套件

**Interfaces:**
- `test_real_chain_tclaw_to_host`: 严格检验端到端链路：
  `TClaw 工具请求 -> SDK Bridge TCP -> RobotAdapter -> Host Router -> HA/Life 执行 -> 结果送回 TCP -> TClaw 收到响应播报`。
- `verify_all.sh`: 统一自动化执行所有 7 个阶段检查。

- [ ] **Step 1: 编写全链路端到端测试**
  - 创建 `tests/e2e/test_real_chain_tclaw_to_host.py`。
- [ ] **Step 2: 运行主验收套件**
  - `./tools/verification/verify_all.sh`
- [ ] **Step 3: 更新验收矩阵与文档**
  - 更新 `docs/acceptance/acceptance-matrix.md` 至 V3.1.0，真实记录测试数据与物理条件。
- [ ] **Step 4: 提交并合入**
  - `git commit -m "feat(integration): complete end-to-end real chain test and update acceptance matrix V3.1.0 (A7)"`
