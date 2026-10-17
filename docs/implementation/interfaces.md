# Desk Agent R1 公共契约与接口规范 (Interfaces & Contracts)

> 文档版本：V2.1.0  
> 责任 Agent：A0 架构与协议  
> 状态：**G0 关口公共契约冻结 (Public Contracts Frozen) + P1 修复补充裁决 (V2.1.0)**  
> 配套 Schema：`contracts/schemas/`  
> 配套 Fixtures：`contracts/fixtures/`  
> 验证工具：`contracts/validator/validate.py`  
> V2.1.0 变更：新增第 6 节「P1 修复契约补充裁决」，将此前隐含的 ACK 层级语义、请求关联、TCP 分帧与配对校验要求显式化。**未改变任何既有 wire 字段语义**，仅澄清与补充。

---

## 1. 契约设计准则与通用处理规则 (Contract Principles)

### 1.1 请求 ID 与幂等性规则 (Request ID & Idempotency)
1. **调用方强制生成**：所有跨模块控制信封（RequestEnvelope）强制包含 `request_id`，格式为 8~64 字符的字母数字及下划线短横线（正则 `^[a-zA-Z0-9_-]+$`）。
2. **重试一致性**：由于局域网抖动、超时重传或崩溃自愈产生的重试，调用方**必须重用原 `request_id`**，严禁生成新 ID。
3. **服务端去重缓存**：
   - 接收方（Router / Host / Companion）必须维护至少 1000 条最近请求的去重记录；
   - 若检测到相同 `request_id` 且已处理完成，直接返回历史响应结构；
   - 若相同 `request_id` 正在后台执行中，返回 `RESOURCE_BUSY`（带有 `retryable: true`）或复用进行中任务句柄；
   - **严禁对同一 `request_id` 重复触发任何物理副作用**（如重复开闭继电器、舵机重复下发、日历重复创建）。

### 1.2 严格时效性与动态过期规则 (Expiration Rules)
1. **动态 UTC 时间戳**：所有请求必须包含 `expires_at`，格式为 ISO 8601 UTC 时间字符串（例如 `2026-10-16T12:00:15Z`）。
2. **动态生成**：有效期必须依据当前系统时钟加 TTL 动态计算（例如 `now + 10s`），**严禁在代码或固件中硬编码固定的未来时间戳**。
3. **过期立即拒绝**：接收方在收到请求、出队分发或下发底层前，必须比对本地 UTC 时钟。若 `now >= expires_at`，必须立即拒绝并返回稳定错误码 `EXPIRED`，**严禁在网络恢复后盲目回放过期积压指令**。

### 1.3 超时与重试策略 (Timeouts & Retries)
各业务领域的标准超时时限与处理要求：
- **Codex 任务提交/执行**：默认超时 15,000 ms。
- **Home Assistant 状态查询**：默认超时 3,000 ms；服务调用等待确认：默认超时 5,000 ms。
- **机头云台舵机运动**：默认超时 3,000 ms。
- **相机单帧 JPEG 捕获**：默认超时 5,000 ms。
- **超时处置规则**：在超时阈值内未收到底层响应或对账状态时，返回 `status: "failed"` 或 `status: "unknown"`，附带错误码 `OPERATION_TIMEOUT` 及 `retryable: true`。**禁止在超时后假冒返回成功**。

### 1.4 两阶段状态确认机制 (Two-Phase State Confirmation)
为了防止误报，系统严格区分“指令已受理”与“物理已达成”：
1. `accepted`：仅代表请求通过合法性、权限白名单及参数校验，已成功进入执行队列或已向物理总线发出。
2. `succeeded`：已通过传感器回读、HA 状态订阅、或舵机完成事件确认目标状态已达成（`state_confirmed: true`）。
3. 若指令下发成功但超过确认时间无法观测到物理状态，终态必须标记为 `status: "unknown"`，`state_confirmed: false`，错误码 `STATE_UNCONFIRMED`。

### 1.5 取消与打断机制 (Cancellation & Barge-in)
1. 正在进行的异步任务支持通过取消操作中断（如 `codex.cancel`, `resource.motion.stop`, `resource.audio.stop`）。
2. 中断完成后终态返回 `status: "cancelled"`，错误信息为 `TASK_INTERRUPTED`。
3. 用户在物理设备上语音打断或按键打断时，固件仲裁器优先切断当前 I2S DMA 播报，并向宿主通知打断事件。

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
  "expires_at": "2026-10-16T12:00:10Z",
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
| `client_id` | String | 否 | 发起方标识（如 `tclaw-firmware`、`edge-hub`） |

---

### 2.2 响应与终态信封 (`envelope_response.json`)
```json
{
  "schema_version": "1.0.0",
  "request_id": "req-ha-exec-002",
  "status": "succeeded",
  "timestamp": "2026-10-16T12:00:02Z",
  "result": {
    "entity_id": "light.living_room_ceiling",
    "action": "turn_on",
    "accepted": true,
    "observed_state": "on",
    "state_confirmed": true
  },
  "execution_duration_ms": 1840
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
  "timestamp": "2026-10-16T12:00:02Z",
  "payload": {
    "entity_id": "light.living_room_ceiling",
    "old_state": "off",
    "new_state": "on"
  }
}
```

---

## 3. 稳定错误代码体系 (`contracts/error_codes.json`)

系统统一定义 13 个不可变稳定错误码：

| 错误代码 | 错误类别 | 可重试 | 产生场景与处理规范 |
| :--- | :--- | :---: | :--- |
| `INVALID_ARGUMENT` | CLIENT_ERROR | 否 | 参数缺失、格式错误或超出物理限位，调用方修正入参 |
| `EXPIRED` | CLIENT_ERROR | 否 | 请求到达时已超过 `expires_at`，直接丢弃，严禁重放 |
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

## 4. 业务领域公共接口明细 (Public Domain Contracts)

### 4.1 生活服务与提醒 (`domain_life.json`)
- **操作列表**：
  - `reminder.create`: 创建提醒（入参：`title`, `due_at`, `timezone`, `repeat_rule`, `priority`）。
  - `reminder.update`: 修改提醒内容或时间（入参：`reminder_id`, 可选 `title`, `due_at`, `repeat_rule`）。
  - `reminder.delete`: 删除提醒（入参：`reminder_id`）。
  - `reminder.snooze`: 贪睡推迟提醒（入参：`reminder_id`, `occurrence_id`, `snooze_minutes`: 1~1440）。
  - `reminder.complete`: 标记事项完成（入参：`reminder_id`, `occurrence_id`）。
  - `reminder.stop_ringing`: **停止扬声器响铃**（入参：`reminder_id`, 可选 `occurrence_id`）。
    > [!IMPORTANT]
    > **停止响铃与完成事项的严格解耦**：用户按键或语音说“别响了/关闭闹钟”时，仅触发 `reminder.stop_ringing` 终止音频播报；**严禁私自调用 `reminder.complete` 将未做事项销毁**。事项完成必须由用户明确发出完成意图。
  - `note.create`: 创建便签备忘（入参：`title`, `content`, `tags`）。
  - `note.read`: 查询便签（入参：`note_id` 或 `query` 模糊关键词）。
- **事件**：
  - `reminder.triggered`: 调度器发现到期轮次并入库 outbox。
  - `reminder.alert`: 投递到机器人播报通道的播报事件。

---

### 4.2 云台与硬件资源仲裁 (`domain_resource.json`)
- **操作列表**：
  - `resource.motion.move`: 控制机头转动。
    - 入参：`pan_deg` (0.0 ~ 180.0°), `tilt_deg` (100.0 ~ 140.0°), `duration_ms` (100 ~ 5000), `profile` (`smooth` | `linear` | `fast`)。
    - 硬件强制限位：固件驱动层严格 Clamp 到 Pan 0~180°, Tilt 100~140°，杜绝物理撞击。
  - `resource.motion.stop`: 紧急停止机头动作（入参：`emergency: true/false`）。
  - `resource.expression.set`: 设置表情与状态文字（入参：`preset`, `status_text`, `duration_ms`）。
  - `resource.camera.acquire`: 申请相机最新帧查看租约。
    - 入参：`client_id`, `lease_duration_ms` (1000~60000ms), `format: "jpeg"`, `target_fps` (0.5~15.0)。
    - **仲裁约束**：持有活动相机租约期间，仲裁器自动锁定机头自动跟随，禁止 TClaw 表情点头破坏监控画面。
  - `resource.camera.release`: 显式释放租约（入参：`client_id`, `lease_id`）。
  - `resource.audio.stop`: 停止当前音频播放（入参：`stream_id`, `reason`）。
- **事件**：
  - `resource.motion.completed`: 机头转动完成（载荷：`job_id`, `pan_deg`, `tilt_deg`, `execution_ms`）。
  - `resource.touch_event`: 触摸板、旋钮输入事件（载荷：`target`: `backtouch`/`screentouch`/`roller`, `action`: `short_press`/`long_press`/`scroll_up`/`scroll_down`）。

---

### 4.3 摄像头与多媒体时效性接口 (Camera & Media Freshness)
- **单帧拉流端点**：`GET /api/camera/frame`
- **帧元数据报头**：
  - `X-Frame-Seq`: 递增帧序号。
  - `X-Frame-Age-Ms`: 图像产生至今的毫秒数。
  - `X-Frame-Source`: `hardware_sensor`（真实硬件采集）或 `static_placeholder`（仅用于测试，生产严禁启用）。
- **过期黑帧防冒充机制**：
  - 阈值：`stale_threshold_ms`（默认 2000 ms）；
  - 若摄像头拔除、网络拥塞导致超过阈值无新硬件帧注入，接口自动返回带有“STALE / DISCONNECTED”提示的黑帧，**严禁将旧画面或静态图谎称为实时监控**。

---

### 4.4 Windows Codex 任务与审批接口 (`domain_codex.json`)
- **操作列表**：
  - `codex.submit_turn` (别名 `codex.submit`): 提交代码任务（入参：`workspace_alias`, `prompt`, `task_id`, `model`）。
    - 路径安全：只允许传入在 Companion 配置中预注册的别名（如 `epanda-desk-agent`），严禁客户端传入 Windows 绝对盘符。
  - `codex.get_task` (别名 `codex.get`): 查询任务状态（入参：`task_id`）。返回结果嵌入 `pending_approvals` 列表。
  - `codex.get_pending_approvals`: 专门查询待决审批（入参：可选 `task_id`）。
  - `codex.respond_approval`: 响应审批决定（入参：`approval_id`, `task_id`, `decision: "approve" | "reject"`, `reason`）。
  - `codex.cancel`: 中断正在运行的 turn（入参：`task_id`, `reason`）。
- **审批安全闭环**：
  - 审批项必须与具体 `thread_id`/`turn_id`/`item_id` 严格绑定；
  - 消除 early-approval 关联竞态：在 `turn/start` 触发前即完成任务映射绑定；
  - **严禁**任何形式的静默自动批准或跨任务通用授权。

---

### 4.5 Home Assistant 家居控制接口 (`domain_ha.json`)
- **操作列表**：
  - `ha.get_state`: 查询实体状态快照（入参：`entity_id`）。
  - `ha.execute`: 触发受控服务调用（入参：`entity_id`, `action`, `parameters`, `wait_for_state`）。
  - `ha.list_changes`: 查询实体状态历史变更记录。
- **状态确认**：
  - 执行 `action` 后，连接器在最长 `state_poll_timeout_ms` 时间内轮询或接收 WS 变更，确认实体 `state` 到达目标值后才标记 `state_confirmed: true`。

---

## 5. 跨模块链路映射与验证方法台账 (Cross-Module Link Catalog)

系统每条跨边界通信链路都有确定的调用方、接收方、协议及独立验证方法，不存在未经确认的暗道：

| 链路编号 | 调用方 (Caller) | 接收方 (Receiver) | 通信通道与协议 | 承载操作 / 接口 | 验证命令与测试用例 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **L-01** | TClaw 语音框架 (ESP32) | Tuya Cloud 大模型 | TLS WebSocket | 语音 Opus 流上传、云端 ASR、LLM 推理、TTS 音频下发、Tool Call 事件 | `tests/firmware_contract/test_tclaw_dialogue.py` (仿真) / 真机台架 (A1 实测) |
| **L-02** | TClaw MCP 工具 (ESP32) | Host 核心路由器 (RK3566) | TCP 37020 协议桥接通道 | `RequestEnvelope` (格式: `ha.*`, `codex.*`, `reminder.*`) | `tests/firmware_contract/test_tclaw_dialogue.py` / `test_p1_final_closure.py` |
| **L-03** | Host 核心路由器 (RK3566) | TClaw 语音框架 (ESP32) | TCP 37020 协议桥接通道 | `ResponseEnvelope` (执行结果及终态反馈) | `tests/firmware_contract/test_tclaw_dialogue.py` / `test_g1_official_sdk_and_tuya_interop.py` |
| **L-04** | Host 提醒调度器 (RK3566) | 机器人扬声器 (ESP32) | TCP 37020 `RobotAdapter` | `reminder.alert` -> `ctrl.expression.play` / WSPK 音频流 | `tests/core/test_host_daemon.py` / `test_p1_final_closure.py` |
| **L-05** | Host 核心路由器 (RK3566) | Windows Companion (PC) | HTTP RPC (端口 8765, Bearer Token) | `codex.submit_turn`, `codex.get_task`, `codex.respond_approval`, `codex.get_pending_approvals` | `tests/companion/test_codex_companion.py` / `test_p1_final_closure.py` |
| **L-06** | Windows Companion (PC) | Codex app-server | Stdio JSON-RPC 双向管道 | `thread/start`, `turn/start`, `turn/approval`, `thread/cancel` | `tests/companion/test_codex_companion.py` |
| **L-07** | Host HAConnector (RK3566) | Home Assistant 实例 | REST API + WebSocket API | `GET /api/states`, `POST /api/services`, WS 事件订阅 | `tests/connectors/test_ha_connector.py` / `test_p1_final_closure.py` |
| **L-08** | Web 浏览器 / 管理端 | Host 相机与控制台 (RK3566) | HTTP (端口 8088, Token 认证) | `resource.camera.acquire`, `GET /api/camera/frame`, `POST /api/rpc` | `tests/camera/test_camera_service.py` / `test_host_daemon.py` |
| **L-09** | 官方 Python SDK (RK3566) | 固件 SDK 桥接器 (ESP32) | UDP 37021 (配对发现) + TCP 37020 (数据流) | `sys.client.hello`, `ctrl.motion.move`, `ctrl.expression.play`, `WSPK` 二进制音频帧 | `tests/sdk_contract/test_sdk_bridge_protocol.py` / `test_wspk_framing.py` |
| **L-10** | 固件板级驱动 (ESP32) | STM32 身体微控制器 | UART2 (TX:19, RX:20, 921600) | `watche_hw_body_move`, `watche_hw_body_stop`, `position_request` | `firmware/build/desk_agent_firmware_test` (C 驱动层) / 真机台架 (A1 实测) |

---

## 6. P1 修复契约补充裁决 (V2.1.0 Rulings)

本节由 A0 就 PR #11 审查发现的五类问题作出裁决。以下条款均**从既有冻结契约推导**，不引入新协议；仅将此前隐含的语义显式化。

### 6.1 ACK 确认层级语义（对应工作包 1）

**裁决 R1：设备侧 `sys.ack` 仅表示"已受理"，绝不表示"物理已达成"。**

依据 §1.4 两阶段确认机制。设备 SDK 桥接层的成功 ACK 语义严格限定为：

| 设备侧消息 | 语义 | 允许对应的响应终态 |
| :--- | :--- | :--- |
| `sys.ack code:0`（含 `data.accepted: true`） | 请求已通过校验、已取得仲裁、已下发驱动 | `accepted`（**不得**直接判定 `succeeded`） |
| `sys.nack`（驱动返回失败） | 下发失败 | `failed` + `HARDWARE_FAULT` |
| 无任何响应（超时） | 链路或设备无应答 | `unknown` + `OPERATION_TIMEOUT`（`retryable: true`） |
| ACK 已受理但确认窗口内观测不到物理状态 | 已下发未确认 | `unknown` + `STATE_UNCONFIRMED`（§1.4.3） |

**强制要求**：
1. 固件 `ctrl.motion.move` / `ctrl.motion.stop` 分支**必须检查驱动返回值**；驱动非 `DESK_HW_OK` 时**严禁**返回 `code:0`，必须返回 `sys.nack` + `HARDWARE_FAULT`。
2. 运动类成功 ACK 的 `data.status` **必须为 `"accepted"`**，并提供 `data.accepted: true`。原 `"succeeded"` 属夸大确认层级，予以废弃。
   - 官方 SDK 侧影响评估：`WatcheRobot.set_target()` / `stop()` 经 `_command()` 仅校验 ACK 成功，**不读取 `data.status`**，故本变更不破坏官方 SDK 兼容性。
3. 物理完成语义仍由既有 `resource.motion.completed` 事件（§4.2）承载。
4. Host 侧**严禁**在未确认时报告 `executed: true`、`completed` 或 `succeeded`。
   - `executed` 字段**不在** `contracts/schemas/` 任何 Schema 中，属 Host 侧自造字段；裁决**移除**，统一改用契约既有词汇：`accepted`、`state_confirmed`、`status`。
5. 仲裁拒绝（`RESOURCE_BUSY`，HTTP/错误码 409）与断连（`UPSTREAM_UNAVAILABLE`）必须原样传播，不得被吞没为成功。

### 6.2 请求关联（对应工作包 2）

**裁决 R2：所有响应必须回传原 `request_id`；等待者只能消费属于自己的响应。**

依据 §1.1。补充强制要求：

1. 固件对**任何**携带 `request_id` 的请求，其 `sys.ack` / `sys.nack` **必须**原样回传该 `request_id`（握手类 `sys.client.hello` 除外）。
2. `RobotAdapter.send_command` **禁止**消费无 `request_id` 的公共 `sys.ack` / `sys.nack`。仅允许按键 `request_id` 精确匹配消费。
3. 超时后到达的迟到 ACK **必须**按 `request_id` 投递；若对应等待者已超时退出，该响应**不得**被任何其他等待者消费，应直接丢弃。
4. `request_id` 在并发下必须唯一。禁止使用毫秒时间戳等低熵来源（同毫秒并发会碰撞）。Host 侧生成的 `request_id` 必须满足 §1.1 正则 `^[a-zA-Z0-9_-]+$`。
5. **旧协议兼容**：若需兼容不回传 `request_id` 的旧设备，必须**显式协商**（能力位或配置开关）并**串行化**该链路上的请求；**严禁**静默回退到"消费任意 ACK"。

### 6.3 TCP 流分帧（对应工作包 3）

**裁决 R3：沿用既有换行分帧协议，不更换公共协议。**

依据 §5 链路 L-09（TCP 37020 数据流）。补充强制要求：

1. 接收侧**必须**维护跨 `read()` 调用的持久缓冲，按 `\n` 切分消息，正确处理：半包（消息被拆到多次 `read`）、粘包（一次 `read` 含多条消息）、单次多消息。
2. 必须定义并强制**最大帧长**；超长帧必须丢弃并返回错误，且不得污染后续消息边界。
3. 非法输入（非 JSON、无终止换行）必须丢弃且不破坏链路。
4. 断连时必须**清空残留缓冲**，严禁残留字节与重连后的新消息拼接。
5. 发送侧**必须**处理短写（`write()` 返回值小于请求长度时循环续写），严禁假设一次 `write` 完成，严禁以 `(void)` 丢弃返回值。
6. WSPK 二进制媒体帧协议（`<4sBBHII`）**保持不变**。

### 6.4 配对校验（对应工作包 4）

**裁决 R4：官方 wire 契约已定义 `pairing_code` 字段与格式，但未定义校验失败的响应；本仓设备侧亦无配对码来源。故采取 fail-closed，并提交接口问题 CR-001。**

依据外部锁定官方 SDK `watcherobot/runtime/daemon/pairing/protocol.py`（Level 1 证据）：

- `pair.request` 字段集固定为 `{type, protocol, version, request_id, daemon_instance_id, pairing_code, target_mode, websocket_port}`，`pairing_code` 必须匹配 `^[0-9]{6}$`。
- `pair.accept` 字段集固定为 `{type, protocol, version, request_id, daemon_instance_id, target_mode, session_token}`，`session_token` 必须为 64 位小写十六进制。
- `pairing_code` 与 `session_token` 属 `_SENSITIVE_FIELDS`，**严禁**写入日志（须按官方 `redact_sensitive_fields` 规则脱敏）。
- **官方契约未定义**"配对码错误"的响应类型：`pair.busy` 的 `reason` 仅允许字面量 `"device_session_active"`，无 `pair.reject` 等类型。

**强制要求**：
1. 设备侧必须校验 `pairing_code`：格式（6 位数字）与**与设备侧已配置的期望码精确匹配**。
2. **校验通过前**，严禁置 `s_paired = true`，严禁生成或发放 `session_token`，严禁回复 `pair.accept`。
3. 未配置期望配对码时，**必须 fail-closed**（不配对、不发 token）。设备侧期望码只能经显式**配置注入**（`desk_sdk_bridge_set_pairing_code()`），**严禁硬编码**生产配对码，**严禁**写入日志。
4. 由于官方契约未定义校验失败响应，本阶段**不新增** wire 消息类型；失败时**不回复 `pair.accept`**（即不授予），并记录诊断日志（已脱敏）。
5. 未定义项作为接口问题登记于 **CR-001**，由 A0 与产品/官方 SDK 方确认后再补齐失败响应语义。

### 6.5 Windows Companion 启动安全（对应工作包 5）

**裁决 R5：默认回环；跨机监听必须显式配置强令牌，且拒绝占位/空令牌。**

依据 §4.4 与 AGENTS.md 第 5 节（严禁写入真实生产凭据）：

1. 默认监听**必须**为 `127.0.0.1`。
2. 当监听地址为非回环（如 `0.0.0.0`）时，**必须**要求显式配置非空、非占位的 `auth_token`；否则**拒绝启动**（fail-closed）。
3. 空令牌、`None`、以及仓库中公开的示例占位令牌（如 `REPLACE_WITH_STRONG_SHARED_TOKEN_MATCHING_TAISHANPI_CONFIG`）**必须**被拒绝。
4. `start-companion.ps1` / `run-companion.ps1` 等**所有**启动入口必须走同一校验路径；禁止"复制样例即暴露服务"。
5. 校验失败时**必须**拒绝启动并给出明确错误，**严禁**静默降级为无鉴权监听。
6. 必须保持泰山派跨机访问 Windows Companion 的**可配置**能力（合法令牌下正常工作）。
7. **严禁**对外暴露 Codex app-server 本身（仅暴露 Companion 的 `/health` 与 `/rpc`）。

### 6.6 第二轮补充裁决 (V2.1.0 追加)

**R6：`STATE_UNCONFIRMED` 主动确认窗口 —— 暂不实现，登记为 CR-002。**
§6.1 表中"ACK 已受理但确认窗口内观测不到物理状态 → `unknown` + `STATE_UNCONFIRMED`"要求 Host 主动等待物理完成事件。
但当前 `sys.ack` **不携带** `job_id` / `operation_id`，ACK 与 `resource.motion.completed` 事件之间**无关联字段**，
无法在不新增接口的前提下实现该窗口。故本轮裁决：
- 已受理未确认 → 终态 `accepted` + `state_confirmed: false`（**不得**报 `succeeded`/`completed`）；
- 主动确认窗口的实现依赖 ACK→完成事件关联，作为接口缺口登记于 **CR-002**；
- Host 侧保留 `STATE_UNCONFIRMED` 的**透传**处理，以备固件将来直接返回该码。

**R7：机器人硬件链路断开 → `failed` + `UPSTREAM_UNAVAILABLE`。**
机器人 SDK 桥接连路不可达时，请求**未能下发**，终态必须为 `failed`（而非 `unknown`——Host 明确知道失败原因），
错误码 `UPSTREAM_UNAVAILABLE`，`retryable: true`（链路恢复后可重试）。
说明：`contracts/error_codes.json` 中该码的既有描述列举的是 HA Core / Tuya 云端 / Codex app-server；
将机器人桥接链路纳入同一语义（"上游/对端不可达"）为 A0 认可的解释，**不修改错误码定义本身**。

**R8：Windows Companion 启动安全 —— 认可 A5 的从严解读。**
A5 在 §6.5 基础上采取了四项更严格的处理，A0 **全部认可**并纳入契约：
1. 占位令牌在**回环地址下同样拒绝**（提交进仓库的占位值永远不是真实凭据）；
2. 非回环监听要求令牌**至少 16 字符**（`MIN_LAN_AUTH_TOKEN_LENGTH = 16`）；
3. 显式指定的 `--config` 路径不存在时**拒绝启动**（不再静默忽略）；
4. 新增 `--check-config` 自检入口（仅输出 `{status, host, port, loopback_only, auth_required}`，**绝不回显令牌**）。

**R9：`desk_sdk_bridge_set_pairing_token()` 仅设置凭据，不得置 `s_paired`。**
`s_paired` 语义为"已有客户端成功完成配对"，**只能**由经过校验的配对握手置位（§6.4）。
Provisioning 接口置位配对状态属于"未经验证即进入已配对状态"的反模式，即使该路径当前不可从网络触达，也必须纠正。

**R10：本轮不予修复、留待后续裁决的既有问题（记录在案，不属本任务范围）。**
1. 错误码词汇漂移：`RESOURCE_ERROR`、`HARDWARE_ERROR`、`OUT_OF_MEMORY`、`INVALID_ARGUMENTS`、`AUTH_FAILED`
   不在 `contracts/error_codes.json` 的 13 个冻结码内（应分别对应 `HARDWARE_FAULT` / `INVALID_ARGUMENT` / `UNAUTHORIZED`）。
   修改会影响其他 owner 的测试，需单独裁决。
2. `camera.capture` 将驱动采集失败映射为 409 `RESOURCE_BUSY`，语义上应为 `HARDWARE_FAULT`。
3. §6.1 的一般性 ACK 层级规则（设备侧成功 ACK 一律为"已受理"）已由 A2 一致应用于
   `motion.action.play`、`camera.acquire`、`camera.release`、`microphone.close`、`expression.play/set`，A0 认可。

**R11：`desk_sdk_bridge_set_pairing_token()` 必须校验凭据格式（`^[0-9a-f]{64}$`）。**
配对路径会把已 provision 的凭据**原样**作为 `pair.accept.session_token` 下发，
而官方契约要求该字段匹配 `^[0-9a-f]{64}$`（`_require_lower_hex(..., _LOWER_HEX_64)`）。
若 provision 一个短值或非十六进制值，固件将产出**自身违反冻结 wire 契约**的 `pair.accept`
（官方 SDK 的 `parse_udp_message` 会直接拒绝）。故裁决：
- `set_pairing_token()` 必须校验入参格式，不合规（长度错误、含大写、非十六进制、空）一律返回 `-1` 且**不得**装载；
- 仍然仅设置凭据、不置 `s_paired`（R9 不变）；
- 该缺陷属 WP4"错误配对凭据必须拒绝"的范畴，故本轮修复。

**R11 附：未决设计问题（不在本任务范围）。**
配对路径复用**静态 provision 凭据**作为会话令牌，意味着跨会话复用同一 `session_token`，
缺少前向保密性；更合理的做法是每次配对握手**新签发**一枚 CSPRNG 令牌。
该变更会改变既有 API 语义，故本轮**不实现**，作为设计问题登记（并入 CR-001 一并裁决）。

**R11 附二：`NULL` 入参语义（A0 认可）。**
`set_pairing_token(NULL)` **保留**原有的"新铸造一枚随机凭据"语义（返回 0），不予拒绝。理由：
1. 该语义为既有文档化行为；
2. 它是铸造随机凭据的唯一入口；
3. `generate_dynamic_token()` 恒定产出 64 位小写十六进制，**不可能**违反 wire 契约。
空字符串 `""` 则按 R11 拒绝（返回 -1）。

