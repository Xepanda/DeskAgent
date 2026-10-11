# Desk Agent R1 全系统验收矩阵与证据清单 (Acceptance Matrix)

本矩阵遵循软件工程严格分层验收原则，明确区分**代码与契约层**、**宿主模拟构建与单元测试层**、**目标宿主（泰山派/Windows）就绪层**以及**真实硬件与在线云端缺口**。

---

## 1. 验收层级与状态总览

| 验收层级 | 验收范围 | 验证环境 | 状态 | 证据引用 |
|---|---|---|:---:|---|
| **Tier 1: 契约与源码** | JSON Schema (Draft 2020-12)、25个 fixtures、13个错误码、代码结构 | Linux (Git 工作区) | **通过 (100%)** | `contracts/validator/validate.py` 输出通过 |
| **Tier 2: 本地构建与单测** | C 固件跨平台构建、仲裁器状态机、Python 核心路由、Life/HA/Codex/Camera 逻辑 | Linux x86_64 Host (GCC, Python 3.14) | **通过 (100%)** | CMake 构建成功，18 项单测全部 PASS |
| **Tier 3: 泰山派宿主部署就绪** | Systemd unit、SQLite WAL 存储设计、无 Linux GUI 依赖、标准 POSIX 架构 | Linux x86/ARM64 规范化构建 | **就绪 (待实板)** | `deploy/edge/` 配置与单元文件完备 |
| **Tier 4: Windows Companion 就绪** | 路径白名单别名映射、PowerShell 脚本、Turn / Approval 审批闭环协议 | Windows 规范兼容代码与集成测试 | **就绪 (待 Windows 实机)** | `companion/src/codex_companion.py`、单测 PASS |
| **Tier 5: 真实硬件与在线设备** | ESP32-S3 物理烧录、舵机PWM、ST7789屏幕、OV2640相机、涂鸦云Token、物理HA网关 | 物理桌面机器人硬件套件与现场网络 | **未验证 (等待硬件)** | 见第 4 节《硬件实操缺口清单》 |

---

## 2. 详细测试证据明细 (Tier 1 & Tier 2)

### 2.1 契约验证结果 (Contract Schema & Fixtures)
- **命令**: `python3 contracts/validator/validate.py`
- **结果**:
  ```
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

### 2.2 固件模拟器构建与运行证据 (Firmware C Engine)
- **命令**:
  ```bash
  /usr/bin/cmake -B firmware/build -S firmware
  /usr/bin/cmake --build firmware/build
  ./firmware/build/desk_agent_firmware_test
  ```
- **输出证据**:
  ```
  [ 33%] Built target desk_hardware
  [ 55%] Built target desk_tclaw
  [ 77%] Built target desk_sdk_bridge
  [100%] Built target desk_agent_firmware_test
  ========================================
    Desk Agent R1 Firmware Booting...     
    Target: ESP32-S3 | Baseline: IDF v5.4 
  ========================================
  [BOOT] Hardware drivers & Resource Arbiter initialized.
  [BOOT] WatcheRobot SDK Protocol Bridge initialized.
  [BOOT] TClaw Dialogue Engine & MCP Tools started successfully.
  [READY] Desk Agent R1 Ready for Voice & SDK Operations.
  ```

### 2.3 Python 核心服务与集成测试证据 (Host Services & E2E)
- **命令**: `python3 -m unittest discover -s tests -t . -p "test_*.py"`
- **输出证据**:
  ```
  ..................
  ----------------------------------------------------------------------
  Ran 18 tests in 0.071s

  OK
  ```
- **覆盖特性矩阵**:
  1. `test_router_idempotency_and_expiration`: 过期请求毫秒级拦截、幂等性缓存去重。
  2. `test_ha_execution_and_confirmation`: 领域白名单拦截、执行指令与观察状态一致性确认。
  3. `test_life_service_persistence_and_recovery`: SQLite WAL 重启数据保持、推迟(Snooze)状态解耦。
  4. `test_codex_companion_task_and_approval_flow`: 任务排队、敏感指令审批挂起与决策反馈。
  5. `test_camera_service_lease_and_stale_detection`: 单用户租约互斥仲裁、租约过期自动释放、视频流新鲜度与 Stale 检测。
  6. `test_models_and_router`: 核心 Envelope 编解码与异常码映射。
  7. `test_ha_connector`: 域白名单强制校验。
  8. `test_codex_companion`: 跨平台工作区保护与目录逃逸防御。
  9. `test_camera_service`: 多客户端并发排它性测试。

---

## 3. 部署规范与架构一致性核查 (Tier 3 & Tier 4)

| 架构规范项 | 规范要求 | 实现确认 | 说明 |
|---|---|:---:|---|
| 统一硬件仲裁 | 机器人硬件（表情、云台、语音）统一仲裁，杜绝双主冲突 | **YES** | `desk_arbiter.c` 实现了优先级抢占与释放机制 |
| 官方 SDK 兼容 | 保留官方 WatcheRobot SDK 协议桥接 | **YES** | `desk_sdk_bridge.c` 映射 SDK 指令至硬件与仲裁器 |
| 宿主职责分离 | 泰山派运行生活服务与 HA，Windows 运行 Codex Companion | **YES** | 泰山派与 Windows 职责清晰，不建立重复对话大模型 |
| 相机局域网受限 | 仅支持局域网短时查看与手动云台，严禁云端录像推流 | **YES** | `camera_service.py` 强制租约超时与局域网鉴权 |
| 提醒与备忘持久化 | 重启不丢失、推迟不重复执行 | **YES** | `life_service.py` 基于 SQLite WAL，持久化与状态转移健全 |

---

## 4. 现场物理硬件与在线环境缺口 (Tier 5 Gap Analysis)

为保障生产环境安全，根据项目授权边界，当前会话不进行真实设备刷机与在线网络写操作。以下为移交物理设备后的后续验证清单：

| 验证项 | 依赖项 | 验证步骤 | 成功判定标准 |
|---|---|---|---|
| ESP32-S3 物理烧录 | 真实硬件、USB-C 数据线、ESP-IDF v5.4 | `idf.py -p /dev/ttyUSB0 flash monitor` | 串口正常输出 Boot 日志，Wi-Fi 连网成功 |
| 舵机云台与标定 | 2轴舵机硬件、供电模块 | 发送 `resource.motion.move` 指令 | 舵机旋转至目标角度无抖动与堵转 |
| ST7789 屏幕与表情 | SPI 240x240 LCD 屏幕 | 触发情绪事件 `emotion: happy` | LCD 刷新对应表情动画 |
| 涂鸦云端对话实通 | Tuya Product ID, Device Secret, License | 唤醒词激活对话 | 麦克风采集音频并收到涂鸦云端下发语音流 |
| 物理 Home Assistant 联动 | 实际部署的 HA 实例与网络路由 | 发送客厅灯控制指令 | 真实客厅灯具亮起并回传实体状态 |
| Windows 实机 Codex 联调 | 实际 Windows PC、OpenAI Codex 授权 | 提交生成测试脚本任务 | Windows 端正确弹出审批并生成文件 |
