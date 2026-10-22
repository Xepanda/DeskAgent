# CR-020：STM32 MOVE 歧义失败后的有界 STOP 策略

- **提出方**：A0 架构与协议
- **提出日期**：2026-10-22
- **关联**：CR-002、CR-007、`r3a-stm32-protocol-adjudication.md`、`desk_watcher_motion.c`
- **状态**：**PROPOSED / BLOCKED — 缺少当前 STM32 peer 对 NACK、重复序号与 STOP 顺序的权威语义；不得据此声称安全停止已实现或获设备端验证**
- **范围**：仅定义 DeskAgent 主机侧的失败分类和候选 STOP 策略；不修改生产代码、不改变帧 schema、不授权硬件动作。

## 1. 问题与现状

CR-002/R3a 定义 MOVE 与 STOP 各自等待 ACK，500 ms 超时、最多重试 2 次，重试保留原 sequence。A1 源码审计指出：MOVE 在同序号重试耗尽后返回 timeout，并释放 motion arbiter；当前没有在该失败路径发送 best-effort STOP。设备现场此前 MOVE 与 STOP 均 timeout，没有 STM32 RX/ACK 或物理反馈，动作状态未知（CR-007）。

已知完整 TX 只证明 ESP32 UART 写入成功，不能证明 STM32 收到。反之，超时也不能证明 MOVE 未执行。因此 MOVE 发送后没有可信终态时，动作交付状态是 **ambiguous**。在仲裁释放前追加 STOP 可能降低持续运动风险，但只有 peer 能保证其序号、处理顺序与取消语义时，才能将其定义为有意义的故障遏制；软件 STOP 永远不能替代现场急停或断电。

## 2. 建议的主机侧分类与行为（待 peer 契约确认）

| MOVE 结果 | 是否可能已发送/执行 | 候选行为 |
|---|---|---|
| 参数校验失败，或无法取得 motion owner，尚未发送任何 MOVE 字节 | 否 | 返回原始错误；不发 STOP。 |
| 收到 CRC/长度/版本/flags/sequence 均有效且与本次 MOVE sequence 关联的明确 NACK | 只有当 peer 契约保证 NACK 表示“执行前拒绝”时，才可确定否 | peer 保证前置拒绝时不发 STOP；否则按 ambiguous 路径处理。当前 CR-007 没有此保证，故不得仅凭 NACK 标志跳过 STOP。 |
| MOVE 字节已部分或完整写出，但最终 ACK/NACK 缺失；或仅收到坏 CRC、格式错误、非关联/错序响应 | 是/未知 | 按 ambiguous 路径尝试一次有界 STOP 事务。错序帧不能完成本请求，但也不能证明本次 MOVE 未执行。 |
| 收到有效、关联的 MOVE ACK | 已受理（CR-002 ACK-only） | 返回 accepted；不自动 STOP。ACK 不证明物理到位。 |

**Ambiguous 路径的候选 STOP 约束：**

1. 在释放发起 MOVE 的 motion owner 前，最多调用一次 STOP 事务；禁止递归 STOP。仲裁资源在整个 STOP 等待窗口内保持由同一 owner 持有，结束后沿既有单一释放路径释放一次。
2. STOP 必须用新的、单调递增的 sequence，并等待与该 STOP sequence 关联的有效 ACK；不得复用 MOVE sequence。候选 STOP 使用现有 500 ms ×（初次发送 + 2 次重试）上界，即至多 1.5 秒等待，重试帧保留 STOP sequence。此上界仅是主机等待预算，不是停止时限保证。
3. STOP ACK 仅表示 STOP 命令被 peer 接受（CR-002）；不得声称机身已静止。STOP timeout/NACK/坏帧均记录为 STOP 未确认/未知，且不触发第二次自动 STOP。
4. 上层最终结果保留原 MOVE 失败码（例如 `DESK_HW_ERR_TIMEOUT`）；STOP 的结果作为独立诊断字段/日志记录，不得被 STOP ACK 覆盖为 MOVE 成功。当前公共 API 若不能表达二级 STOP 结果，应由 A1 提交接口 CR；A0 在接口裁决前不批准擅自扩展 wire/API。
5. 系统需确保 STOP 与已经进入 STM32 队列的 MOVE 有确定顺序，且 STOP 的 `CANCEL_ALL` 能取消当前及排队动作。单纯在 ESP32 上先写 STOP 不足以建立该保证。

以上是**条件化建议，不是当前批准实现规范**。尤其是合法 NACK 的“执行前拒绝”语义、STM32 对相同 sequence 重发的幂等行为、STOP 的取消范围与队列顺序均没有本仓库可核验的 peer 固件来源。CR-007 继续 OPEN。

## 3. 阻塞解除所需证据

A0/A1 需取得可归属到当前板的 STM32 固件源码/协议规范或维护方权威说明，至少明确：

- NACK 是否保证在执行任何 MOVE 前产生；是否可能在动作已开始后返回 NACK；
- 同一 sequence 的重复 MOVE 是否幂等且不会重复启动动作；序号回绕/重启如何处理；
- MOVE 与 STOP 的接收/执行队列顺序；STOP `CANCEL_ALL` 是否中止运行中和待执行动作；
- STOP ACK 表示解析、入队还是取消生效；取消/急停是否有独立反馈；
- 通信中断时执行器自身是否有安全超时/硬件限位/物理急停机制。

随后 A0 才能将第 2 节由 PROPOSED 改为 APPROVED，或根据 peer 行为要求新增 ACK/NACK 状态、operation token、stop outcome 或设备侧 watchdog 契约。若无法证明 STOP 的有效顺序/取消范围，主机 best-effort STOP 只能标为尽力尝试，通信故障安全停止继续 BLOCKED。

## 4. 验收与证据等级

- **L1（契约/来源）**：上述 peer 语义有版本、来源和当前设备适用性证据；合同解释 NACK、重复 sequence、STOP 顺序与取消确认。
- **L2（主机软件）**：native C harness 覆盖：未发送前输入/仲裁失败不发 STOP；明确 pre-execution NACK 不发 STOP；未获该 NACK 保证时按 ambiguous 处理；部分 TX、完整 TX 后 ACK 丢失、坏 CRC、错序响应在同 owner 下发一次新 sequence STOP；STOP 走最大重试上界且不递归；原 MOVE 错误不被覆盖；STOP ACK 只记 accepted；所有路径 arbiter 恰好释放一次。L2 仅证明主机逻辑。
- **L3（单板/STM32）**：设备/PCB/STM32/ESP32 镜像身份和安全前置条件齐全；逻辑分析仪或 STM32 RX 证据证明 MOVE/STOP 原始帧、CRC、sequence 与 ACK/NACK 关联；受监督的小幅动作故障注入证明超时后的 STOP 被 peer 接收和按文档取消。仅 STOP ACK 不证明静止；需独立观察或设备反馈确认停止结果。
- **L4（恢复闭环）**：在现场监督、独立急停就位下覆盖断线、延迟/迟到 MOVE ACK、重复 MOVE、STOP 丢包/重试、ESP32/STM32 重启及状态恢复；证明动作 owner 释放、系统如实报告未知状态，并能通过物理急停回到安全状态。不得以任何软件 STOP 代替独立急停。

当前没有 peer 源码/规范、真实 RX/ACK、STOP 取消语义或物理停止证据；因此当前仅能给出设计提案，**L1 peer 互通未通过、L2 本 CR 未运行、L3/L4 未验证**。
