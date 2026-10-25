# CR-026：desk_cli 相机 capture 错误标签补齐

- **提出方：** A0 架构与协议
- **状态：** **A0 APPROVED — 一次性、狭义软件修改范围；L2 contract 验收完成后关闭本 CR 的软件项**
- **范围：** 仅授权 A1 在 `firmware/components/desk_cli/src/desk_cli.c` 的 `cli_camera_capture_error_name(int rc)` 中补充 `DESK_WATCHER_CAMERA_ERR_BUFFER_TOO_SMALL`（数值 `-27`）到明确 `buffer-too-small` 文案的映射，并为该映射补充对应 L2 contract 测试。
- **关联：** [CR-003 配对码生产注入与生命周期](CR-003-pairing-code-production-injection.md)、`firmware/components/desk_hardware/include/desk_watcher_camera.h`

## A0 狭义裁决

**批准 A1 完成本 CR 所列的一次性修改：让 camera capture CLI 对 `DESK_WATCHER_CAMERA_ERR_BUFFER_TOO_SMALL`（-27）显示 `buffer-too-small`，并添加 L2 contract 覆盖。** 修改范围限于上述错误码到标签的映射及其直接 contract 测试；不得借本 CR 扩展其他错误码行为或 CLI 功能。

本批准是针对单一错误标签的临时工作授权，**不把 `firmware/components/desk_cli/` 整个目录纳入 A1 永久 ownership**，也不修改 A0~A7 公共 ownership 矩阵。其他 `desk_cli` 改动仍须依据现有 ownership 规则另行裁决。

## 依据与边界

1. `desk_watcher_camera.h` 定义 `DESK_WATCHER_CAMERA_ERR_BUFFER_TOO_SMALL = -27`；`desk_cli.c::cli_camera_capture_error_name()` 当前未显式映射该值，因而落入默认的 `unknown camera error`。
2. 根 `AGENTS.md` §2 的 A1 路径矩阵未列 `firmware/components/desk_cli/`。
3. CR-003 §1、§2.1 与 §3 仅为配对码 `pairing open|close` 入口、处理和窗口生命周期，把 `desk_cli` 路径纳入该配对工作包；该局部授权不构成整个目录或相机错误标签工作的授权。
4. 本 CR 是对上述单一标签映射的明确 A0 授权，不构成硬件行为或实机能力证明。

## A1 实施与验收

- 只在 `cli_camera_capture_error_name(int rc)` 中增加 `DESK_WATCHER_CAMERA_ERR_BUFFER_TOO_SMALL` 对应的 `buffer-too-small` 标签。
- 添加或扩展 `tests/firmware_contract/` 中的定向 contract 测试，验证 `-27` 映射为 `buffer-too-small`，并保留现有已覆盖错误码的映射行为。
- 运行该定向 contract 测试；记录命令与结果。通过源码/contract 检查属于 **Level 2** 软件证据，不代表相机实机 capture、buffer 行为或硬件验收。
- 不修改根 `AGENTS.md`、其他生产代码、公共接口或构建配置；不进行硬件、串口、烧录或远端操作。

## 授权不涵盖

- `firmware/components/desk_cli/` 的永久或整体 ownership；
- `cli_camera_capture_error_name()` 之外的 CLI 行为、相机驱动或 HAL/TClaw 实现；
- 新错误码、协议/API 变更、设备能力声明、实机验证、刷机或发布。
