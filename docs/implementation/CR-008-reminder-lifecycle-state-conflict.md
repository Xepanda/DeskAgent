# CR-008：提醒 occurrence 生命周期转换冲突错误码

- **提出方：** A3 Python 核心与生活服务
- **裁决方：** A0 架构与协议 / Lead
- **状态：** **A0 批准新增 `STATE_CONFLICT`，语义为不可重试客户端错误**
- **范围：** 统一错误目录、提醒生命周期响应语义
- **不在范围：** Host transition 实现、数据库迁移、提醒业务策略和其他域错误映射

## 裁决

当调用方引用的 reminder occurrence 确实存在，但该 occurrence 的当前状态不允许请求的生命周期转换时，返回 `STATE_CONFLICT`，`status="failed"`，`retryable=false`。错误可附带 `reminder_id`、`occurrence_id`、`current_state`、`requested_transition` 等诊断字段；不得包含无关用户内容或凭据。

调用方不得以相同 request_id 原样重试。应先读取/确认当前生命周期状态；只有在状态发生变化或用户意图更新后，才使用新的 request_id 提交新的操作。该错误说明业务状态拒绝转换，不表示数据库、服务或物理设备不可用。

## 与既有码区分

| 代码 | 适用语义 | 本场景为何不适用 |
| --- | --- | --- |
| `INVALID_ARGUMENT` | 字段缺失、类型/格式无效或超出参数约束 | occurrence ID 和动作参数有效；拒绝原因是已存在记录的当前状态 |
| `RESOURCE_NOT_FOUND` | 指定实体或 occurrence 不存在 | 被引用 occurrence 存在 |
| `RESOURCE_BUSY` | 可暂时重试的硬件/租约资源竞争 | 不是资源互斥，等待不会自行解除 lifecycle 条件 |
| `STATE_UNCONFIRMED` | 命令已受理，但无法确认执行结果/物理状态 | 本场景是服务明确拒绝转换，不是执行结果未知 |
| **`STATE_CONFLICT`** | 资源存在，但当前生命周期状态不允许请求转换 | 精确匹配本场景；`CLIENT_ERROR`, `retryable=false` |

此码不可替代真正的 `RESOURCE_NOT_FOUND`、参数校验失败或存储/上游故障。若资源在查询与转换之间并发变化，服务应基于事务内读取/条件更新的最终状态决定使用 `STATE_CONFLICT` 或相应基础设施错误。

## 兼容性

- 新码是 `error_codes.json` 的**加法扩展**；既有错误码及既有错误码行为不变。`envelope_response.json` 的 error code 是受限大写标识符字符串，故不需更改 wire envelope 或 `schema_version`。
- 不认识该码的旧消费者仍可按通用 `status="failed"` 显示 `error.message`；客户端不应只接受本地枚举中的已知 code 后丢弃整个响应。新消费者应将其归为非重试的客户端/生命周期错误，保留 details 以供诊断。
- `retryable=false` 表示同一 request_id 的同一状态转换不应自动重放；幂等缓存返回的历史失败不应被误判成瞬时故障。
- 提醒生命周期 handler 的生产映射由 A3 实施。本 CR 不授权修改 Host 代码，也不表示运行时已经返回此码。

## 仓库证据与验收

1. `contracts/error_codes.json` 当前定义 `INVALID_ARGUMENT`, `RESOURCE_NOT_FOUND`, `RESOURCE_BUSY`, `STATE_UNCONFIRMED` 的描述/重试属性；其中前者是参数错误、not-found 是对象不存在、busy 是暂态硬件/租约竞争、state-unconfirmed 是无法确认执行状态，均不匹配“存在但当前状态不允许转换”。
2. `contracts/schemas/envelope_response.json` 将 `error.code` 约束为 `^[A-Z0-9_]+$`，错误对象允许 `retryable` 和 `details`；新增 code 不需修改 envelope schema。
3. `contracts/schemas/domain_life.json` 定义 reminder snooze/complete/stop_ringing 的参数结构，但未定义对应 transition failure 的独立错误码。现有 response fixture 有通用 failed/unknown 样例，没有 lifecycle state conflict 样例。
4. `host/desk_agent/services/life_service.py::stop_ringing()` 在 occurrence 存在但 state 不是 `ringing` 时抛出包含当前 state 的 `ValueError`；不存在时抛 `KeyError`。这直接证明状态冲突与 not-found 是两种情况。其他 lifecycle operation 的精确映射仍由 A3 按其实际行为实现并加测试。
5. `docs/implementation/interfaces.md` 原表列 13 个既有码。新增后目录为 14 个；`docs/acceptance/` 下历史数量记录归 A7 更新范围，本 CR 不越界修改。

验收门槛：契约 validator 能校验全部 fixtures 和 14 项 error catalog；A3 后续实现测试需分别覆盖 missing occurrence → `RESOURCE_NOT_FOUND`、存在但状态不允许 → `STATE_CONFLICT` / `retryable=false`、参数无效 → `INVALID_ARGUMENT`，并验证新 request_id 才能表达状态/用户意图改变后的新操作。
