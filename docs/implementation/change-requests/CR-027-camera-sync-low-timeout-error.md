# CR-027：相机 SYNC 持续低电平超时错误契约

- **提出方：** A0 架构与协议
- **状态：** **A0 APPROVED — A1 实现完成；L2 软件验收通过；L3/L4 未验证**
- **范围：** 将 capture 开始后、首次读取 PTL 前，SYNC 一直未变为 active 直至 capture deadline 的结果，从通用 `DESK_HW_ERR_TIMEOUT` 改为相机专属 `DESK_WATCHER_CAMERA_ERR_SYNC_TIMEOUT = -28`。保留现有日志。仅本文件列出的驱动、TClaw、CLI 和定向 contract test 修改获批。
- **依据：** `desk_watcher_camera.c` 已打印 `SYNC timeout ... ready signal stayed low`，但返回 `-3`；该值也用于 SPI bus acquire deadline，因此上层无法区分两种故障。

## A0 裁决

**批准增加相机专属 `-28` 返回值，并按下文将其传播到 TClaw 与 CLI。** `-28` 不占用 `desk_hardware.h` 的通用错误码（当前 `-1..-6`），也不改变已有 `-20..-27` 的值和含义。API 仍返回 `int`，所以该修改不改变函数签名；但依赖原先将“SYNC 首次等待超时”识别为 `DESK_HW_ERR_TIMEOUT (-3)` 的消费者必须在本 CR 范围内更新。

这个裁决只覆盖**首次 readiness wait**：capture 开始后等候第一个 active SYNC，并在 deadline 前始终读到 inactive。若曾观察到 active SYNC，随后 PTL header/FIFO 始终为空并耗尽 deadline，仍返回 `DESK_WATCHER_CAMERA_ERR_EMPTY (-25)`；SPI bus 获取或重获耗尽 deadline，仍返回 `DESK_HW_ERR_TIMEOUT (-3)`。PCA9535/SYNC 输入读取失败仍返回 `DESK_WATCHER_CAMERA_ERR_SYNC_IO (-22)`。不以新错误码掩盖 I/O 失败或空 FIFO。

## 错误码与兼容策略

| 条件 | 返回码 | 用户可读标签 | 可重试 |
|---|---|---|---|
| 首次 readiness wait 中 SYNC 始终 inactive 至 capture deadline | `DESK_WATCHER_CAMERA_ERR_SYNC_TIMEOUT = -28` | `SYNC stayed low until deadline`（CLI 可用 `SYNC-low timeout`） | 是 |
| SYNC 输入读取失败 | `DESK_WATCHER_CAMERA_ERR_SYNC_IO = -22` | 保留现有标签 | 是，按现有策略 |
| 首次观测到 SYNC active 后 PTL FIFO/header 仍为空并耗尽期限 | `DESK_WATCHER_CAMERA_ERR_EMPTY = -25` | 保留现有标签 | 是，按现有策略 |
| SPI bus acquire/reacquire 耗尽 deadline | `DESK_HW_ERR_TIMEOUT = -3` | 保留现有标签 | 是，按现有策略 |

- 保留现有 `SYNC timeout after capture deadline ... ready signal stayed low` 日志及其参数；不得将日志移除或改成仅有通用 timeout 文案。
- 不保留 SYNC-low 分支返回 `-3` 的兼容别名，因为那会继续造成歧义。其他通用 timeout 场景保持 `-3`。
- TClaw 错误文本应准确指出 SYNC readiness 未到；`desk_camera_capture_error_retryable()` 必须将 `-28` 纳入可重试集合。
- CLI 应将 `-28` 显示为单独的 SYNC-low timeout 标签，而不是通用 `timeout` 或 `unknown camera error`。
- 在注释/日志中说明该错误指首次 ready wait，不表示 SPI 错误、PTL 空 FIFO、SYNC GPIO 读取失败或相机硬件已损坏。

## A1 实施 ownership 与获批路径

A1 可实施以下一次性修改，不扩展为目录 ownership 变更：

1. `firmware/components/desk_hardware/include/desk_watcher_camera.h`：声明 `DESK_WATCHER_CAMERA_ERR_SYNC_TIMEOUT = -28`。
2. `firmware/components/desk_hardware/src/desk_watcher_camera.c`：只将上述首次 SYNC-low deadline 分支改为返回新错误码；保留日志、释放资源行为及其余错误路径。
3. `firmware/components/desk_tclaw/src/desk_tclaw_bridge.c`：增加可读错误名，并让该码可重试。
4. `firmware/components/desk_cli/src/desk_cli.c`：增加专属 CLI 标签。此单次授权类似 CR-026，不把 `firmware/components/desk_cli/` 纳入 A1 永久 ownership。
5. `tests/firmware_contract/`：新增/扩展定向 contract test。测试路径不在 A1 永久路径矩阵内，本 CR 仅批准与本错误契约直接相关的测试修改。

不得改 SDK bridge/wire 协议、HAL 公共签名、构建配置或其它 CLI/TClaw 行为；如发现另有消费者依赖 `-3` 代表此分支，应记录并提交后续 CR，不得扩大本包范围。

## 实施与 L2 验收结果

- A1 在获批路径完成实现：camera header/driver 使用 `-28` 区分首次 SYNC readiness deadline；TClaw 增加错误文本并标记可重试；CLI 增加 `SYNC-low timeout` 标签；新增 `tests/firmware_contract/test_watcher_camera_error_contract.py` 覆盖错误分类。
- Root 在 WSL 独立复跑 `test_watcher_camera_error_contract` 与 `test_desk_cli_status_truthfulness` 两个测试模块，**30 tests passed**。该结果为 **Level 2** 软件 contract/source 验证。
- 验收覆盖 `-28` 常量及首次 SYNC-low 返回路径、原 timeout 日志保留、TClaw 命名/重试策略、CLI 标签，以及既有 SYNC I/O、空 FIFO、SPI deadline 错误分类边界。
- 定向测试和源码合同不能证明实体 SYNC、电源、SPI、HX6538 或现场 capture 正常；没有据此提升到 L3/L4。

## 尚未验收

当前无设备连接，本 CR 不授权串口访问、刷机或实机检查。**L3/L4 均未验证。** L3 仍需在授权的现场设备上分别观察 SYNC-low 超时、I/O 失败、空 FIFO 与 SPI acquire timeout 对应的返回码及日志；L4 仍需真实 JPEG 闭环及恢复场景。未取得这些证据前，不得声称相机已驱动或功能已跑通。
