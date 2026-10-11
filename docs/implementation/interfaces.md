# Desk Agent R1 公共契约与接口规范 (Interfaces & Contracts)

> 文档版本：V1.0.0  
> 责任 Agent：A0 架构与协议  
> 状态：G0 关口冻结 (Frozen)  
> 配套 Schema：`contracts/schemas/`  
> 配套 Fixtures：`contracts/fixtures/`  
> 验证工具：`contracts/validator/validate.py`

---

## 1. 契约设计准则

1. **版本化与自描述**：所有跨模块信封强制包含 `schema_version: "1.0.0"`。
2. **幂等性与去重保证**：所有命令请求强制包含由调用方生成的 `request_id`（8~64 字符）。网络重试、崩溃恢复必须重用原 `request_id`。服务方必须维护去重缓存，不可对同一 `request_id` 重复触发副作用。
3. **严格时效性约束**：所有请求必须包含 `expires_at`（ISO 8601 UTC 时间）。若处理队列或接收方发现已过期，必须立即拒绝并返回 `EXPIRED` 错误，严禁回放过期命令（如旧关灯、旧舵机转动）。
4. **两阶段状态反馈**：
   - `accepted`：仅代表服务端成功接收并进入执行队列；
   - `succeeded` / `failed` / `cancelled` / `unknown`：终态结果，携带明确数据或稳定错误码。
   - **禁止**把 API 200 或 `accepted` 宣称为物理动作已完成。
5. **事件序号与作用域**：事件通知携带 `sequence`，其单调递增性严格限定在 `sequence_scope` 作用域内。序号出现空洞表明发生丢包，接收方必须通过拉取快照对账，不能假定事件永久保留。

---

## 2. 信封规范 (Envelope Specifications)

### 2.1 请求信封 (`envelope_request.json`)

```json
{
  "schema_version": "1.0.0",
  "request_id": "req-ha-exec-002",
  "operation": "ha.execute",
  "arguments": {
    "entity_id": "light.living_room_ceiling",
    "action": "turn_on"
  },
  "expires_at": "2026-10-12T00:00:00Z",
  "task_id": "tsk-voice-home-01",
  "session_id": "sess-turn-441",
  "client_id": "tclaw-firmware"
}
```

| 字段 | 类型 | 必填 | 约束与说明 |
| :--- | :--- | :--- | :--- |
| `schema_version` | String | 是 | 固定 `"1.0.0"` |
| `request_id` | String | 是 | 唯一幂等 ID，8~64 字符，正则 `^[a-zA-Z0-9_-]+$` |
| `operation` | String | 是 | 格式 `domain.action`，正则 `^[a-z0-9_]+(\.[a-z0-9_]+)+$` |
| `arguments` | Object | 是 | 操作具体入参，遵循对应域 Schema |
| `expires_at` | String | 是 | ISO 8601 UTC 时间戳，超时丢弃 |
| `task_id` | String | 否 | 关联的高层任务 ID |
| `session_id` | String | 否 | 关联的对话或会话轮次 ID |
| `client_id` | String | 否 | 发起方子系统标识 |

---

### 2.2 响应与终态信封 (`envelope_response.json`)

```json
{
  "schema_version": "1.0.0",
  "request_id": "req-ha-exec-002",
  "status": "succeeded",
  "timestamp": "2026-10-11T22:45:02Z",
  "result": {
    "entity_id": "light.living_room_ceiling",
    "action": "turn_on",
    "accepted": true,
    "observed_state": "on",
    "state_confirmed": true
  },
  "execution_duration_ms": 2040
}
```

| 字段 | 类型 | 必填 | 约束与说明 |
| :--- | :--- | :--- | :--- |
| `schema_version` | String | 是 | 固定 `"1.0.0"` |
| `request_id` | String | 是 | 对应请求信封的 `request_id` |
| `status` | String | 是 | 枚举：`accepted` \| `succeeded` \| `failed` \| `cancelled` \| `unknown` |
| `timestamp` | String | 是 | 响应生成时间 (ISO 8601 UTC) |
| `result` | Object | 否 | 成功或中间执行结果 |
| `error` | Object | 否 | 失败详情 (`code`, `message`, `retryable`, `details`) |
| `execution_duration_ms` | Integer | 否 | 耗时（毫秒） |

---

### 2.3 事件信封 (`envelope_event.json`)

```json
{
  "schema_version": "1.0.0",
  "event_id": "evt-ha-state-1001",
  "sequence": 104,
  "sequence_scope": "ha:event_bus",
  "request_id": "req-ha-exec-002",
  "type": "ha.state_changed",
  "timestamp": "2026-10-11T22:45:02Z",
  "payload": { ... }
}
```

| 字段 | 类型 | 必填 | 约束与说明 |
| :--- | :--- | :--- | :--- |
| `schema_version` | String | 是 | 固定 `"1.0.0"` |
| `event_id` | String | 是 | 事件唯一 ID，8~64 字符 |
| `sequence` | Integer | 是 | 递增序号 (≥ 0) |
| `sequence_scope` | String | 是 | 序号作用域 (如 `'device:inputs'`, `'task:tsk-01'`) |
| `request_id` | String | 否 | 若事件由明确请求触发则携带 |
| `type` | String | 是 | 格式 `domain.event_name` |
| `timestamp` | String | 是 | 发生时间 (ISO 8601 UTC) |
| `payload` | Object | 是 | 领域事件数据 |

---

## 3. 稳定错误代码体系 (`contracts/error_codes.json`)

| 错误代码 | 错误类别 | 可重试 | 产生场景与处理规范 |
| :--- | :--- | :--- | :--- |
| `INVALID_ARGUMENT` | CLIENT_ERROR | 否 | 参数缺失、格式错误或超出物理限位，调用方修正入参 |
| `EXPIRED` | CLIENT_ERROR | 否 | 请求到达时已超过 `expires_at`，直接丢弃，不重放 |
| `UNAUTHORIZED` | SECURITY_ERROR | 否 | 访问未经允许的 HA 实体或未注册的 Windows 工作区 |
| `UNKNOWN_OPERATION` | CLIENT_ERROR | 否 | 调用的 `domain.action` 未实现或未注册 |
| `RESOURCE_BUSY` | TRANSIENT_ERROR | 是 | 硬件被更高/同级所有者占用（如相机预览中禁止跟随运动） |
| `RESOURCE_NOT_FOUND` | CLIENT_ERROR | 否 | 目标 `entity_id`、`task_id`、`reminder_id` 不存在 |
| `OPERATION_TIMEOUT` | TRANSIENT_ERROR | 是 | 在超时时限内未收到下层或物理响应 |
| `HARDWARE_FAULT` | HARDWARE_ERROR | 否 | 舵机机械卡死 (Stall)、I2C 总线挂起或驱动故障 |
| `UPSTREAM_UNAVAILABLE` | INTEGRATION_ERROR | 是 | HA Core、Tuya 云端或 Codex app-server 断开连接 |
| `STATE_UNCONFIRMED` | INTEGRATION_ERROR | 否 | 命令虽已送达，但在确认超时内无法观测到物理状态改变 |
| `APPROVAL_REJECTED` | WORKFLOW_ERROR | 否 | 人工拒绝了敏感命令或文件变更审批 |
| `TASK_INTERRUPTED` | WORKFLOW_ERROR | 否 | 任务被用户主动通过语音或界面取消 |
| `DUPLICATE_REQUEST` | IDEMPOTENCY_INFO | 否 | 重复请求，返回已保存的历史处理结果 |

---

## 4. 业务领域契约明细

### 4.1 Home Assistant (`domain_ha.json`)
- **操作列表**：
  - `ha.get_state`: 查询实体当前状态快照（入参 `entity_id`）。
  - `ha.execute`: 触发受限服务调用（入参 `entity_id`, `action`, `parameters`, `wait_for_state`）。
- **执行与回读原则**：
  - `wait_for_state` 指定了期望的目标状态及最长等待毫秒数。
  - 若在超时时间内实体 `state` 更新为预期值，返回 `state_confirmed=true`；若超时仍未变化，返回 `state_confirmed=false`，状态标记为 `unknown`。
- **事件**：
  - `ha.state_changed`: 订阅推送的实体状态变化事件。

### 4.2 Windows Codex (`domain_codex.json`)
- **操作列表**：
  - `codex.submit`: 启动受控任务（入参 `workspace_alias`, `prompt`, `task_id`, `model`）。禁止传入未经校验的 Windows 绝对路径。
  - `codex.append`: 追加进行中 turn 的说明文本。
  - `codex.cancel`: 请求中断活动 turn。
  - `codex.continue`: 恢复已中断的任务 thread。
  - `codex.get`: 查询任务运行状态。
  - `codex.respond_approval`: 响应待决审批（入参 `approval_id`, `task_id`, `decision: "approve" | "reject"`）。
- **审批安全规则**：
  - 审批项必须与具体 `thread_id`/`turn_id`/`item_id` 严格绑定；
  - 严禁任何形式的跨任务通用授权或静默自动批准。

### 4.3 生活服务 (`domain_life.json`)
- **操作列表**：
  - `reminder.create` / `update` / `delete`: 提醒维护，基于 RFC 5545 规则与 IANA 时区。
  - `reminder.snooze`: 贪睡指定分钟数（1~1440 分钟）。
  - `reminder.complete`: 标记任务完成。
  - `note.create` / `read`: 纯文本持久化备忘。
- **重要准则**：
  - 用户按下机器人触摸键停止响铃时，仅执行 `stop_ringing`，**严禁自动调用 `reminder.complete`**。
  - 提醒落库必须在 SQLite 事务提交成功后方可确认。

### 4.4 硬件与资源仲裁 (`domain_resource.json`)
- **操作列表**：
  - `resource.expression.set`: 设置命名表情与文本状态。
  - `resource.motion.move`: 控制机头转动（限制：Pan 0~180°, Tilt 100~140°，硬件强制夹限）。
  - `resource.motion.stop`: 紧急停止机头动作。
  - `resource.camera.acquire`: 申请相机最新帧查看租约。
  - `resource.camera.release`: 显式释放相机租约。
  - `resource.audio.stop`: 停止当前音频播放。
- **事件**：
  - `resource.motion.completed`: 物理运动完成。
  - `resource.touch_event`: 触摸板、背部触摸或滚轮交互事件。

---

## 5. 与第三方协议通道映射表

| Desk Agent 契约概念 | 对应 WatcheRobot Python SDK 映射 | 对应 Tuya / TClaw 映射 |
| :--- | :--- | :--- |
| 请求下发 | `transport.send_command(message_type, data)` | TClaw MCP 工具 / `acp_client_inject()` |
| 结果回传 | `{"type": "sys.ack" / "sys.nack", "data": ...}` | ACP `chat.send` 回复 / 消息总线响应 |
| 音频采音 | `robot.microphone.open_pcm()` (Opus 帧解码) | TClaw 硬件 I2S DMA 采音 |
| 音频播报 | `robot.play_pcm()` (WSPK Binary Frame `type=1`) | TClaw Tuya TTS / ES8311 I2S 输出 |
| 相机拉流 | `robot.camera.capture()` (单帧 JPEG 请求) | HX6538 PTL JPEG 回调 |
| 机头运动 | `robot.motion.move_to()` | STM32 Body Protocol 串口/SPI 指令 |
| 输入事件 | `robot.inputs` 事件监听器 | ESP32 GPIO 中断 / 触摸驱动事件 |
