# CR-030：Hardware CLI readiness 状态如实表达

- **提出方：** A0 架构与协议
- **状态：** **A0 APPROVED（一次性窄范围，2026-10-23）；IMPLEMENTED（L2）。** 仅授权本文列出的 A1 CLI 文案/语义及对应 L2 回归测试；实现结果见 §5。
- **范围：** 修正 `system` 命令把 arbiter 固定打印为 ACTIVE，以及 `display` 命令把 software shadow 读取成功报告为 READY 的语义。此 CR 不增加硬件能力或物理状态证明。
- **关联：** `AGENTS.md` A1/A7 ownership matrix；`docs/superpowers/plans/2026-10-11-desk-agent-system.md`；CR-003、CR-026、现有 `tests/firmware_contract/test_desk_cli_status_truthfulness.py`。

## 1. 只读依据与限制

1. `firmware/components/desk_cli/src/desk_cli.c::cmd_system()` 当前无条件输出 `Hardware Arbiter: ACTIVE`，没有查询 arbiter 初始化/健康状态。`desk_arbiter.h` 暴露资源 owner 查询，但没有 arbiter readiness/status API。
2. `cmd_display()` 先检查 `desk_hardware_is_display_initialized()`，再调用 `desk_hardware_display_get_expression()`。该 getter 当前只复制 `s_current_preset` 与 `s_current_status` 软件缓存并返回成功；它不是 panel readback，也不能证明 LCD 已完成实际 scanout。因此返回 0 和取得缓存只允许报告 software shadow/readback available。
3. `AGENTS.md` A1 矩阵没有列出 `firmware/components/desk_cli/`。CR-003 曾对配对 CLI 工作包作过一次性 A1 路径授权，但不构成 `desk_cli/` 的常设 ownership。本 CR 仅对下列两个精确路径再次作一次性授权，不更新或扩大公共 ownership matrix。

## 2. 一次性 ownership 与允许改动

- **A1 设备固件 owner：** 仅可修改：
  - `firmware/components/desk_cli/src/desk_cli.c`
  - `tests/firmware_contract/test_desk_cli_status_truthfulness.py`
- **A0：** 负责本 CR 范围、状态语义和路径例外的裁决；不修改代码、AGENTS.md 或 schema。
- **A7：** 可独立运行和复核本 CR 定向验证，负责把验证证据/缺口提交给总管家；不修改生产代码或上述测试文件。

任何需要修改 `desk_hardware/`、新增状态 API、改变跨模块状态编码，或更改其它测试/生产路径的需求均超出本批准范围，须停止并交 A0 另行裁决。此一次性授权不建立 `desk_cli/` 的未来常设 owner。

## 3. 输出语义

1. **Arbiter readiness：** 在无 arbiter readiness/status API 的现状下，`system` 必须输出 `UNKNOWN` 或 `NOT_EXPOSED` 并说明没有状态 API；禁止无条件输出 `ACTIVE`。不得把资源 owner 查询结果、单个硬件命令成功或初始化调用存在推导为 arbiter 全局健康。
2. **Display driver initialization：** 明确输出 `desk_hardware_is_display_initialized()` 所提供的 driver/software 初始化标志，例如 `INITIALIZED` / `NOT_INITIALIZED`。这个字段不能称作屏幕可见、panel ready 或物理显示成功。
3. **Software shadow/readback：** 若 `desk_hardware_display_get_expression()` 成功，明确标为 `software shadow` / `cached expression` / `software readback`，可以打印缓存的 preset/status；不得仅据该 API 返回成功打印 display `READY`。
4. **Physical scanout：** 当前没有读回/光学证明 API，所有分支均须将 `physical scanout` 报为 `UNKNOWN` / `NOT_EXPOSED`。不得伪造物理呈现成功。
5. 初始化未完成或 software getter 失败时，应保留各字段的独立状态与错误码；一个字段成功不能升级其它字段状态。

## 4. L2 验收条件

针对 `tests/firmware_contract/test_desk_cli_status_truthfulness.py` 扩充定向测试，并满足：

1. `system` 输出在当前 API 集合下不会报告 arbiter `ACTIVE`，而是明确 `UNKNOWN`/`NOT_EXPOSED`；测试同时保护不得通过 owner 值推断 readiness。
2. `display` 覆盖未初始化、初始化后 software shadow 成功、getter 失败三种分支；断言初始化、shadow/readback、physical scanout 是互不混淆的字段，缓存读成功不输出 display `READY`，physical scanout 始终 unknown/unexposed。
3. 执行 `python -m unittest tests.firmware_contract.test_desk_cli_status_truthfulness -v` 全部通过，并以项目受支持的 firmware compile/build 检查确认修改过的 CLI 源编译通过。日志需记录命令、环境、退出码；若构建环境缺失，明确标为未验证，不用其它 host test 替代编译证据。
4. 上述是 L2 软件/编译证据。没有硬件连接时不得运行串口/刷机，也不得声称物理屏幕验证通过。未来 L3 必须在实际设备通过 CLI 读取状态，并由现场观察屏幕画面；CLI 软件字段自身不能代替现场观察。当前 CR 不要求或授权硬件接入。

## 5. 实施与验证结果

- **A1 变更范围：** 仅修改本 CR 授权的 `firmware/components/desk_cli/src/desk_cli.c` 与 `tests/firmware_contract/test_desk_cli_status_truthfulness.py`；未新增硬件状态 API、未修改 `desk_hardware/`，也未扩大常设 ownership。
- **TDD 与回归：** A1 按 RED → GREEN 完成定向测试，并确认变异 guard 会拒绝回归到旧的误导状态输出。`python -m unittest tests.firmware_contract.test_desk_cli_status_truthfulness -v` **14/14 passed（L2）**。
- **独立审阅：** A7 review **PASS**。
- **目标构建：** ESP-IDF 5.4 full build archive `.target_logging/build-partitions-20261023-044436-589/` 中 `build-result.json` 记录 `success=true`、`exit_code=0`。此为 L2 编译证据。
- **硬件边界：** 固件未烧录。真实设备 CLI 输出、arbiter 状态与物理 LCD scanout 均未验证；不得将本 CR 标为 L3/L4 或据软件 shadow 断言屏幕实际呈现。

**当前状态：** 本 CR 的一次性 A0 授权范围已完成并通过 L2 定向测试、A7 review 与 ESP-IDF 5.4 构建。L3 物理显示和设备侧 arbiter 状态仍未验证。
