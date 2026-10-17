# CR-002：运动确认窗口缺少 ACK→完成事件关联字段

- **提出方**：A0 架构与协议
- **提出日期**：2026-10-17
- **关联**：PR #11 审查工作包 1、`docs/implementation/interfaces.md` §6.1 表行 4、§6.6 裁决 R6
- **状态**：**待裁决 (OPEN)** — 阻塞 `STATE_UNCONFIRMED` 主动确认窗口的实现
- **影响模块**：A2（`firmware/components/desk_sdk_bridge/`）、A3（`host/desk_agent/`）

---

## 1. 问题陈述

`interfaces.md` §1.4.3 与 §6.1 表行 4 要求：

> 若指令下发成功但超过确认时间无法观测到物理状态，终态必须标记为
> `status: "unknown"`，`state_confirmed: false`，错误码 `STATE_UNCONFIRMED`。

实现该要求需要 Host 在收到"已受理"ACK 后，**主动等待**该次运动对应的物理完成事件
（§4.2 定义的 `resource.motion.completed`，载荷含 `job_id`、`pan_deg`、`tilt_deg`、`execution_ms`），
并在超时后判定 `STATE_UNCONFIRMED`。

**但当前 `sys.ack` 响应中不包含任何可用于关联的标识**：

```json
{"type":"sys.ack","code":0,"request_id":"req-...","data":{"status":"accepted","accepted":true,
 "pan_deg":45.0,"tilt_deg":110.0,"duration_ms":300}}
```

- 无 `job_id`
- 无 `operation_id`
- `request_id` 关联的是**请求**，而 `resource.motion.completed` 载荷中**不含** `request_id`

因此 Host **无法**将某个完成事件归属到某次具体请求。在缺少关联字段的前提下实现确认窗口，
只能靠"最近一次请求"之类的启发式猜测，等同于**发明未经对账的专有协议**，
违反 AGENTS.md 第 6.3 条。

---

## 2. 相关既有事实（Level 1 证据）

外部锁定官方 SDK 中，**基于 job 的操作**已经使用 `operation_id` 作为关联标识：

`watcherobot/robot.py`
```python
def _start_job(self, message_type: str, data: dict) -> Job:
    response = self._command(message_type, data)
    operation_id = response.get("data", {}).get("operation_id")
    if not isinstance(operation_id, int) or operation_id <= 0:
        raise WatcheRobotError(f"{message_type} ACK did not include operation_id")
    ...
```

即官方契约对**作业类**命令的 ACK 要求携带 `data.operation_id`，并以 `Job` 对象跟踪终态。
但 `ctrl.motion.move` / `ctrl.motion.stop` 走的是 `_command()`（非 `_start_job()`），
**不要求** `operation_id`，其 ACK 是否携带该字段由固件决定，官方未强制。

---

## 3. 待裁决问题

**Q1**：运动类命令的"物理完成"应如何关联到发起请求？可选方向：
- (a) 固件在 `ctrl.motion.move` 的 `sys.ack` 中回传 `data.operation_id`（对齐官方 job 模型），
  `resource.motion.completed` 事件载荷携带同一 `operation_id`；
- (b) `resource.motion.completed` 事件载荷直接携带 `request_id`；
- (c) 维持现状（不提供关联），契约 §1.4.3 对运动类命令**降级**为"仅保证 `accepted` 语义"。

**Q2**：若采纳 (a)/(b)，确认窗口超时阈值取多少？（§1.3 给出机头云台舵机默认超时 3,000 ms，
但那是"下发"超时，不是"物理到位"超时。）

**Q3**：在 Q1/Q2 裁决前，运动类命令的终态契约是否正式降级为
"成功 ACK ⇒ `accepted`，`state_confirmed: false`，不承诺 `STATE_UNCONFIRMED`"？

---

## 4. 本任务采取的临时策略

按 §6.6 裁决 R6：

1. 已受理未确认 → 终态 `accepted` + `state_confirmed: false`，**不**报 `succeeded`/`completed`；
2. **不**实现主动确认窗口（缺少关联字段，无法诚实实现）；
3. Host 侧保留 `STATE_UNCONFIRMED` 的**透传**能力，以备固件将来直接返回该错误码；
4. 本 PR **不得**被宣称为"已实现两阶段物理确认闭环"。

---

## 5. 请求裁决

请 A0 / 产品 / 官方 SDK 维护方就 Q1–Q3 给出结论。在裁决前，
`interfaces.md` §1.4.3 对**运动类**命令的强制力降级为"不得伪报成功"，
而非"必须产出 `STATE_UNCONFIRMED`"。
