# CR-020：STM32 MOVE 歧义失败后的有界 STOP 策略

- **提出方**：A0 架构与协议
- **提出日期**：2026-10-22
- **关联**：CR-002、CR-007、`r3a-stm32-protocol-adjudication.md`、`desk_watcher_motion.c`
- **状态**：**A0 策略已批准；A1 已完成 L2 主机实现，A7 review PASS。L2 仅为 mock UART/FreeRTOS C harness 模拟证据，不是 ESP-IDF 目标编译、STM32 peer 或物理停止证据。STM32 peer 兼容性及物理停止效果仍由 CR-007 阻塞；不得声称安全停止已实现或通过设备验证。**
- **范围**：冻结 DeskAgent 主机侧 MOVE 歧义失败后的单次 best-effort STOP 策略，授权 A1 按本 CR 实现并提供 L2 native C harness。仅定义主机发送/错误报告行为，不改变帧 schema，不证明 peer 接收、取消生效或物理停止。

## 1. 问题与现状

CR-002/R3a 定义 MOVE 与 STOP 各自等待 ACK，500 ms 超时、最多重试 2 次，重试保留原 sequence。A1 源码审计指出：MOVE 在同序号重试耗尽后返回 timeout，并释放 motion arbiter；当前没有在该失败路径发送 best-effort STOP。设备现场此前 MOVE 与 STOP 均 timeout，没有 STM32 RX/ACK 或物理反馈，动作状态未知（CR-007）。

已知完整 TX 只证明 ESP32 UART 写入成功，不能证明 STM32 收到。反之，超时也不能证明 MOVE 未执行。因此 MOVE 发送后没有可信终态时，动作交付状态是 **ambiguous**。在仲裁释放前追加 STOP 可能降低持续运动风险，但只有 peer 能保证其序号、处理顺序与取消语义时，才能将其定义为有意义的故障遏制；软件 STOP 永远不能替代现场急停或断电。

## 2. A0 裁决：主机侧一次性 best-effort STOP（已批准）

| MOVE 结果 | 是否可能已发送/执行 | A0 批准行为 |
|---|---|---|
| 参数校验/仲裁获取在发送前失败，或可证明零 MOVE 字节已送出 | 否 | 返回原始错误，不发 STOP。只有可证明未发送时才适用此例外。 |
| MOVE 有任何字节可能已送出，随后发生 timeout、部分写入、无效/坏帧、错序/非关联响应，或没有 peer 语义保证的 NACK | 是/未知 | 在释放原 gimbal owner 前 best-effort 发送一次 STOP。把结果视为 ambiguous；错误/NACK 本身不证明 MOVE 未执行。 |
| 收到有效、关联的 MOVE ACK | 已受理（CR-002 ACK-only） | 按既有语义返回 accepted；不因成功 ACK 自动 STOP。ACK 不证明物理到位。 |

批准的主机侧策略约束：

1. STOP 必须在 MOVE 调用仍持有的同一 gimbal owner 下尝试；STOP 尝试结束后，按单一路径释放 owner 恰好一次。实现不得经由会提前释放 owner 的 public wrapper 造成竞态或双重释放。
2. STOP 分配新的单调 sequence，不复用 MOVE sequence；本策略只 best-effort 发送一次 STOP，**不得重试 STOP**，也不得递归触发第二次 STOP。STOP ACK/NACK/timeout 均作为独立 STOP 结果记录。
3. 最终 MOVE 结果保留原 MOVE 失败码（例如 `DESK_HW_ERR_TIMEOUT`）；STOP 的结果不得覆盖 MOVE 错误或将 MOVE 改报成功。只增加本地诊断记录，不扩展 wire/schema。
4. 此策略仅保证主机在歧义失败后尽力发送 STOP 帧；不承诺 STOP 被 peer 接收、按顺序执行、取消任何动作或使机身停止。不得将其称为安全停止或急停替代品。

该本地行为不依赖 peer 证明重复 MOVE 幂等或 NACK 为执行前拒绝：当这些语义未获权威确认时，一律将可能已发出的 MOVE 结果按 ambiguous 处理并尝试 STOP。R3a same-sequence retry 是 DeskAgent 主机侧合同；见 [R3a §6.1](../r3a-stm32-protocol-adjudication.md) 的 peer 语义边界。

## 3. 仍由 CR-007 阻塞的 peer/设备结论

若要主张协议兼容、STOP 可有效取消动作或物理运动已停止，仍需取得可归属到当前板的 STM32 固件源码/协议规范或维护方权威说明，并通过相应设备验证。至少需明确：

- NACK 是否保证在执行任何 MOVE 前产生；是否可能在动作已开始后返回 NACK；
- 同一 sequence 的重复 MOVE 是否幂等且不会重复启动动作；序号回绕/重启如何处理；
- MOVE 与 STOP 的接收/执行队列顺序；STOP `CANCEL_ALL` 是否中止运行中和待执行动作；
- STOP ACK 表示解析、入队还是取消生效；取消/急停是否有独立反馈；
- 通信中断时执行器自身是否有安全超时/硬件限位/物理急停机制。

上述 peer 证据不会撤销已批准的主机 best-effort 尝试；但在语义与设备验证完成前，peer 兼容性、STOP 取消效果和“安全停止”继续 BLOCKED。若来源显示需要新增 ACK/NACK 状态、operation token、stop outcome 或设备侧 watchdog，应另行提交协议/接口裁决。

## 4. 验收与证据等级

- **L1（契约/来源）**：上述 peer 语义有版本、来源和当前设备适用性证据；合同解释 NACK、重复 sequence、STOP 顺序与取消确认。
- **L2（主机软件，PASS）**：A1 将真实生产 ESP_PLATFORM motion driver 编译并运行于 mock UART/FreeRTOS C harness，链接真实 HAL 与 arbiter。覆盖 zero/negative/partial/full timeout、NACK、ACK；验证 MOVE 错误不变、歧义路径仅发送一次新 sequence STOP 且不重试、owner 时序正确并恰好释放一次。32/32 tests pass；两个 RED mutations 均被测试捕获；A7 review PASS。该模拟证明主机逻辑与所链接真实组件在 harness 中的行为，不是 ESP-IDF 目标编译或目标板运行，不证明 STM32 peer 行为、STOP 被接收/取消生效或物理停止。
- **L3（单板/STM32）**：设备/PCB/STM32/ESP32 镜像身份和安全前置条件齐全；逻辑分析仪或 STM32 RX 证据证明 MOVE/STOP 原始帧、CRC、sequence 与 ACK/NACK 关联；受监督的小幅动作故障注入证明超时后的 STOP 被 peer 接收和按文档取消。仅 STOP ACK 不证明静止；需独立观察或设备反馈确认停止结果。
- **L4（恢复闭环）**：在现场监督、独立急停就位下覆盖断线、延迟/迟到 MOVE ACK、重复 MOVE、STOP 发送失败/丢包/无 ACK、ESP32/STM32 重启及状态恢复；验证主机不重试 STOP、动作 owner 正确释放、系统如实报告未知状态，并能通过物理急停回到安全状态。不得以任何软件 STOP 代替独立急停。

当前主机策略已由 A0 批准，A1 的 L2 harness 验收已通过。当前没有 peer 源码/规范、真实 RX/ACK、STOP 取消语义或物理停止证据；因此 peer 兼容性 **L1 未通过**，设备 **L3/L4 未验证**。L2 仅证明主机按策略尽力发送，不证明 STOP 生效。
