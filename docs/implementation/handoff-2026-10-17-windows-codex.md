# 交接清单：Windows Codex 会话接手真机调试

> 出具方：A0 架构与协议 / Lead Orchestrator
> 日期：2026-10-17
> 分支：`feat/real-device-integration-p1-remediation`
> 上一轮审阅 head：`b4ea71a6a074b155082596705fc8d78608f7c71f`
> 本清单**不开展**烧录、配网或硬件联调，仅交接状态与证据。

---

## 1. PR 与提交

- **PR**：https://github.com/Xepanda/DeskAgent/pull/11 （OPEN，**未合并**）
- **base**：`4ad0ab638295027f71e84af15b5e79f7c4ed782f`（未变）
- **本轮新增 5 个提交**（在上一轮审阅 head 之上）：

| 提交 | Owner | 内容 |
| :--- | :--- | :--- |
| `926c82a` | A1 | **最后一个 P1 的修复**：`desk_hardware.c` move/stop 无条件传播驱动错误；位置仅在驱动确认成功后更新 |
| `5881e5e` | A3 | Host 故障信封测试；`interfaces.md` 澄清两个 `request_id` 命名空间 |
| `137256d` | A2 | 真实 C 协议层：默认模式（strict=false）驱动故障传播测试 + 负向对照 |
| `2c59cfa` | A7 | 真实固件跨层链路测试；证据四分层；验收矩阵 V3.3.0 |
| `04e51a9` | A7 | G1 互通测试 TCP 分帧修复（累积至换行，不再假设一次 recv 一条消息） |

**本轮变更文件（9 个，+1678 / −38）**：
`firmware/components/desk_hardware/src/desk_hardware.c`、
`tests/firmware_contract/test_desk_hardware_motion_fault_propagation.py`（新）、
`tests/sdk_contract/test_sdk_bridge_protocol.py`、
`tests/core/test_hardware_fault_envelope.py`（新）、
`tests/e2e/test_real_firmware_motion_fault_chain.py`（新）、
`tests/e2e/test_g1_official_sdk_and_tuya_interop.py`、
`tools/verification/verify_all.sh`、`docs/acceptance/acceptance-matrix.md`、`docs/implementation/interfaces.md`

---

## 2. 最后一个 P1：修复方式与回归证据

### 缺陷
`desk_hardware_motion_move_to()` / `desk_hardware_motion_stop()` **仅在 `s_strict_hardware_mode == true` 时**才向上传播驱动错误，而该开关**默认为 `false`**。因此默认配置下驱动明确返回失败时，硬件层仍返回成功 → 桥接层回 `accepted` → Host 报告成功。此外 `move_to` 在调用驱动**之前**就把目标坐标写入 `s_current_pan/tilt`，使失败的动作仍被 `motion_get_position()` 报告为已达成。

### 修复（`926c82a`，仅 A1 所属模块）
1. 驱动返回值**无条件**传播，不再受 strict 开关约束。
2. 失败路径经**单一释放点**归还仲裁资源（`desk_arbiter_release` 幂等，仅当 `current_owner == owner` 时清除），无死锁。
3. `s_current_pan/tilt` **仅在驱动返回 `DESK_HW_OK` 时**更新——失败不改变已确认位置。
4. 未改动：strict 下的 UART 可用性预检查、默认驱动内部的 strict 开关（见 §8 遗留）。
5. 桥接层与 Host 层**无需改动**：A2/A3 各自核查后确认既有实现已正确（桥接回 `sys.nack` + `HARDWARE_FAULT` + 原 `request_id`；Host 映射为外层 `failed`）。

### 回归证据（四层，互不替代）

| 层 | 位置 | 覆盖 |
| :--- | :--- | :--- |
| ① 直接 C 函数测试 | `tests/firmware_contract/test_desk_hardware_motion_fault_propagation.py`（8 项） | 默认 strict=false 下 move/stop 失败传播、strict=true 同样传播、失败不更新位置、资源可重新获取、成功路径不变、边界守卫 |
| ② 真实 C 协议测试 | `tests/sdk_contract/test_sdk_bridge_protocol.py` WP1b 块（6 项） | 默认模式下桥接层回 `sys.nack`/500/`HARDWARE_FAULT`/原 `request_id`、绝不 `code:0`、资源可重取、成功仅 `accepted` |
| ③ Python 伪设备测试 | `tests/core/test_hardware_fault_envelope.py`（8 项） | Host 外层信封 `failed` + `HARDWARE_FAULT`、`request_id` 保留、无 `result`/`executed` |
| ④ 全跨层链路 | `tests/e2e/test_real_firmware_motion_fault_chain.py`（5 项） | **真实编译 C 驱动** → 真实 C 桥接 → 真实 `RobotAdapter` → 真实 `DeskAgentHostApp` → 外层信封 |

**负向对照（证明测试非空跑）**
- ①：把 `desk_hardware.c` 还原为修复前语义后，**11 项断言失败**。
- ②：同样还原后，新增 6 项中**5 项失败**，而既有的两项 strict 模式测试**仍然通过**——正是"旧测试无法捕获默认模式缺陷"的直接证明。
- ④：还原后 **3/5 失败**（`accepted` 而非 `failed`）。

**关键发现（记录，供后续参考）**：修复前既有的两项"真实 C 协议"驱动故障测试（`test_sdk_bridge_protocol.py:1219/1235`）都先调用 `desk_hardware_set_strict_mode(True)`，因此**在默认模式缺陷存续期间一直是绿的**——测试自身的 setup 掩盖了缺陷。本轮已补默认模式覆盖，并保留原 strict 模式测试。

---

## 3. 实际运行的命令、数量与结果

```
# 串行全量（本轮共 3 次，均 OK）
PYTHONPATH=. python3 -m unittest discover -s tests -t . -p "test_*.py"
  Ran 295 tests in 45.344s — OK

# 并发安全（3 轮 × 4 并发 = 12 次，均为 OK）
  12/12 OK，Address already in use = 0

# 主验收套件
./tools/verification/verify_all.sh
  exit=0；13 PASS / 0 FAIL；ALL 8 VERIFICATION PHASES COMPLETED SUCCESSFULLY
  阶段3 Ran 295 OK / 阶段4 Ran 10 OK / 阶段5 Ran 20+5 OK

# 固件原生构建
/usr/bin/cmake -B firmware/build -S firmware && /usr/bin/cmake --build firmware/build
  [100%] Built target desk_agent_firmware_test   （零新增告警）

# 契约校验
python3 contracts/validator/validate.py
  === ALL G0 CONTRACT & DEEP DOMAIN CHECKS PASSED (100% OK) ===
```

**分套件实测计数（合计 295）**：`firmware_contract 30 · sdk_contract 73 · camera 9 · companion 40 · connectors 26 · core 61 · e2e 47 · test_p1_gaps_verification 4 · test_p1_final_closure 5`

### 未执行的检查及原因
- **未烧录、未运行任何真实硬件**；未连接真实 Tuya 云端 / 局域网 HA / Windows Codex。
- **未做真实 G1 关口验收**（需 ESP32-S3 真机 + 官方 Runtime + 受控物理动作）。
- **未构建 ESP32-S3 目标固件**（本机无 ESP-IDF，见 §7）。
- **PowerShell 脚本未实际执行**（本机无 `pwsh`），仅 Level 2 静态证据。

---

## 4. 工作区状态

- 本轮全部改动**已提交**，工作区 **clean**。
- 本地分支与 `origin/feat/real-device-integration-p1-remediation` **已同步**。
- **未合并、未发布、未创建额外 PR。**
- 全程未执行破坏性 Git 操作；外部仓库仅只读访问（见 §6）。

---

## 5. CR-001 / CR-002 状态与影响

### CR-001（**OPEN**）— 配对校验失败响应与设备侧配对码来源未定义
`docs/implementation/change-requests/CR-001-pairing-code-validation.md`

- **缺口 A**：官方 wire 契约**未定义**"配对码错误"的响应类型。`pair.busy.reason` 仅允许字面量 `device_session_active`，不存在 `pair.reject` 等类型。
- **缺口 B**：设备侧"期望配对码"的**权威来源未定义**（无 provisioning 存储、无展示流程）。既有测试硬编码 `"123456"`，实际固化的是漏洞行为。
- **当前实现**：fail-closed——未配置期望码 / 非 6 位数字 / 不匹配一律拒绝，**不置 `s_paired`、不发 token、不回复 `pair.accept`**；未新增任何 wire 消息类型。
- **影响**：**官方配对流程未闭环**。裁决前，本 PR **不得**被宣称为"官方配对流程已完成"。

### CR-002（**OPEN**）— 运动确认窗口缺少 ACK→完成事件关联字段
`docs/implementation/change-requests/CR-002-motion-confirmation-correlation.md`

- 契约 §1.4.3 要求"已下发未确认 → `STATE_UNCONFIRMED`"，但运动 `sys.ack` **不携带** `job_id`/`operation_id`，`resource.motion.completed` 事件**不携带** `request_id`，**关联字段不存在**，无法诚实实现确认窗口。
- **当前实现**：已受理未确认 → 终态 `accepted` + `state_confirmed: false`；Host 保留 `STATE_UNCONFIRMED` 透传能力。
- **影响**：**本 PR 不得**被宣称为"已实现两阶段物理确认闭环"。

### 其他在案遗留（R10，`interfaces.md` §6.6）
错误码词汇漂移（`RESOURCE_ERROR`/`HARDWARE_ERROR`/`OUT_OF_MEMORY`/`INVALID_ARGUMENTS`/`AUTH_FAILED` 不在 13 个冻结码内）；`camera.capture` 将驱动失败映射为 409 `RESOURCE_BUSY`（应为 `HARDWARE_FAULT`）；配对路径复用静态 provision 凭据作为会话令牌（缺前向保密）。

---

## 6. Windows 接手所需：外部仓库与构建入口

> 版本与路径摘自 `contracts/dependency_lock.json`（锁定于 2026-10-16T10:50:00Z），
> 并已**现场复核**其当前 commit 与 dirty 状态（见"当前实测"列）。

| 仓库 | 路径 | 锁定版本 | 当前实测 | 状态 |
| :--- | :--- | :--- | :--- | :--- |
| **DeskAgent（本仓）** | `/home/maidang/DeskAgent` | 锁定记录为 `main` @ `6d863c2` | 本分支 `feat/real-device-integration-p1-remediation` | clean，与 origin 同步 |
| **TClaw** | `/home/maidang/.gemini/antigravity-cli/brain/49ffa309-8e24-433a-a0da-2c22b3c125ea/scratch/tclaw-repo` | `master` @ `88bd37378f1d106fcc1175e61b368222cc8fabda` | 同左 | **dirty**：1 项未跟踪 `.share/`（受保护，**未改动**） |
| **WatcheRobot-public（嵌入式 HAL）** | `/home/maidang/.gemini/antigravity-cli/brain/cb82573a-ba9b-42ea-a6be-405983e79f69/scratch/watcherobot_public` | tag `watche-v0.1.2` @ `646a977bfd76a8516ae505fda2331b81e1049b87` | 同左 | clean（detached HEAD） |
| **WatcheRobot_python_sdk（官方协议参考）** | `/home/maidang/.gemini/antigravity-cli/brain/cb82573a-ba9b-42ea-a6be-405983e79f69/scratch/watcherobot_python_sdk` | tag `v0.1.11` @ `8bc13967a06ef073459be47e930ff43ef37ee8fb` | 同左 | clean（detached HEAD） |

> ⚠️ `dependency_lock.json` 中本仓条目记录的是 `main` 分支，**不代表**本 PR 分支状态。
> ⚠️ 上述三个外部仓库均位于**本机绝对路径**下；Windows 会话若在不同机器上工作，这些路径**不存在**，需重新定位（**未知**）。
> ⚠️ `tests/sdk_contract/test_official_sdk_runtime_compat.py` 通过**硬编码绝对路径**导入官方 SDK；路径不存在时该套件无法运行。此为既有耦合，未修改。

### 构建入口
| 用途 | 入口 |
| :--- | :--- |
| 主机侧固件仿真构建/测试 | `firmware/CMakeLists.txt`（standalone 分支）→ `firmware/build/` |
| ESP32-S3 目标构建 | `firmware/CMakeLists.txt`（`CMAKE_TOOLCHAIN_FILE` 或 `IDF_PATH` 存在时走 ESP-IDF 分支）+ `firmware/main/CMakeLists.txt` + `firmware/sdkconfig.defaults` + `firmware/partitions.csv` |
| 一键全量验收 | `./tools/verification/verify_all.sh`（8 阶段） |
| 契约校验 | `python3 contracts/validator/validate.py` |
| 边缘环境自检 | `bash deploy/edge/verify_edge.sh` |
| Windows Companion 启动 | `deploy/windows/run-companion.ps1` / `start-companion.ps1`（含 `--check-config` 预检） |

> ⚠️ **本机 `PATH` 上的 `cmake` 是坏的 Python shim**（`/home/maidang/.local/bin/cmake` → `ModuleNotFoundError: No module named 'cmake'`）。
> 必须使用 **`/usr/bin/cmake`**（4.2.3）；`verify_all.sh` 已硬编码该路径。

---

## 7. 固件产物：主机测试构建（无目标板产物）

**当前 `firmware/build/` 全部为主机（host）构建产物**，由 standalone CMake 生成：

| 产物 | 类型 | sha256 |
| :--- | :--- | :--- |
| `desk_agent_firmware_test` | 主机可执行文件（72K ELF） | `ccb3d6ce…9515a4` |
| `libdesk_hardware.a` | 主机静态库 | `0ea9cf43…abcbe7` |
| `libdesk_sdk_bridge.a` | 主机静态库 | `679f4b85…2886ee3` |
| `libdesk_tclaw.a` | 主机静态库 | `499b9718…cfbd62fa` |

**无 ESP32-S3 目标板产物**：无 `sdkconfig`、无固件镜像 `.bin`、无 bootloader/partition-table。
（`firmware/build/CMakeFiles/…/CMakeDetermineCompilerABI_*.bin` 是 CMake 编译器探测文件，**不是**固件镜像。）

**本机无法产出目标板产物**：`IDF_PATH` 未设置、`idf.py` 不在 PATH、常见位置无 ESP-IDF 检出。
目标构建的**入口已就绪但未构建**（`firmware/main/`、`sdkconfig.defaults`、`partitions.csv` 齐备）。

---

## 8. 尚未实现 / 尚未验证

### 尚未实现（已知、在案）
1. **CR-001**：官方配对流程闭环（失败响应语义 + 期望配对码来源）。
2. **CR-002**：运动物理完成的两阶段确认窗口（缺 ACK→完成事件关联字段）。
3. **R10**：错误码词汇漂移收敛；`camera.capture` 失败码修正。
4. **配对令牌前向保密**：仍复用静态 provision 凭据作为会话令牌，未改为每次握手新签发。

### 尚未验证（诚实边界）
1. **Level 3（物理节点）**：ESP32-S3 单板、泰山派 ARM 守护进程、Windows Companion 进程通信——**全部未实测**。
2. **Level 4（端到端在线闭环）**：真实语音唤醒 → Tuya 云端 → MCP 工具 → HA/Codex/Life 执行 → 语音播报，断网/断电恢复——**全部未实测**。
3. **G1 关口**：**未达成**。需真机固件 + 真实 Tuya 云端 + 官方 Runtime + 一个受控物理动作。
4. **PowerShell 启动脚本**：仅静态证据，未在 Windows 运行时执行。
5. **默认驱动在主机（无 UART）非 strict 模式下仍返回成功**：本轮按裁决**有意保留**（strict 开关是"主机无 UART"的既有模式语义，移除会破坏既有测试契约）。这意味着**主机仿真下默认驱动无法失败**；真机路径需 `strict=true` 或真实板级驱动注入。**建议 Windows 会话在真机上以 `strict=true` 复核。**

### 已知残余风险（非缺陷，已记录）
- `desk_sdk_bridge_process_command` 以 `strstr` 分派命令，`tool.response` 分支位于多个设备命令分支之后；若某 `tool.response` 报文正文包含 `motion.move` / `motion.stop` / `sys.ping` / `expression.play` / `camera.acquire` 等子串，会被误判为设备命令。**当前不可达**（真实 `ResponseEnvelope.to_dict()` 无 `operation` 字段），属潜在健壮性风险。

---

## 9. 证据分层声明

本清单所载全部验证均为 **Level 2（软件协议与模拟 PoC）**：
固件侧为 GCC 原生编译 + ctypes 动态调用，Host 侧为真实组件 + 本地 socket。

**本 PR 不代表真实 G1、硬件或云端验收通过。**
Level 3 / Level 4 在 `docs/acceptance/acceptance-matrix.md` 中仍如实标记为 `未实测` / `未完成`，
并由 `verify_all.sh` 阶段 8 门禁强制校验该标记不被篡改。
