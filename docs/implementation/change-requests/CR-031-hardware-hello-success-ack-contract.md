# CR-031 — HardwareHello success ACK 严格合同

- **提出方：** A0 架构与协议
- **状态：** **A0 APPROVED；授权 A2 按本 CR 完成主机侧 L2 ACK 解析与合同测试。**
- **范围：** 仅允许修改 `firmware/components/desk_sdk_bridge/` 和 `tests/sdk_contract/`。只约束设备作为 WebSocket client 发送 HardwareHello 后收到的首个 success ACK；不改 SDK/Runtime、配对协议、capability、媒体或其它连接路径。
- **关联：** [P0-3 官方 Python SDK 设备接入契约](../p0-3-official-sdk-device-contract.md)、[CR-003](CR-003-pairing-code-production-injection.md)、锁定的 WatcheRobot SDK/Runtime `v0.1.11`。

## 1. 上游证据与现状

锁定副本 `.target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk` 的 `src/watcherobot/runtime/daemon/pairing/protocol.py::build_hardware_hello_ack()` 返回以下字段：顶层 `type="sys.ack"`、整数 `code=0`；`data` 中 `type="sys.client.hello"`、`role="hardware"`、`session_state="connected"`，以及 `negotiated.audio_uplink` 对象。后者当前字段和值为 `codec="opus"`、`sample_rate=16000`、`channels=1`、`frame_duration_ms=60`、`packetization="one_opus_packet_per_wspk"`、整数 `version=1`（上游函数约第 542–561 行）。WebSocket server 在接受硬件 hello 后直接发送该 builder 的结果（`runtime/daemon/connections/websocket_server.py` 约第 381–417 行）。

该 `negotiated.audio_uplink` 是 pinned Runtime 当前 ACK 的字面内容，不证明固件实现或支持音频上行；固件 capability 与音频合同仍按 CR-010。v0.1.11 success ACK 中没有 `status`、`pair_request_id`、`daemon_instance_id` 或 `session_token` 字段，因此本合同不新增或要求这些线上字段。

当前生产 client `firmware/components/desk_sdk_bridge/src/desk_sdk_websocket.c` 约第 723–725 行通过 `strstr` 检查 ACK 文本是否包含 `type=sys.ack`、`sys.client.hello` 和 `code:0`，未验证 JSON 嵌套、字段和值是否完整匹配。不得把此宽松检查视为严格 success ACK 验收。

## 2. A0 批准的 success ACK 合同

设备只能在收到并解析以下完整 JSON object 后，才将当前 WebSocket 连接标为 HardwareHello 已确认：

```json
{
  "type": "sys.ack",
  "code": 0,
  "data": {
    "type": "sys.client.hello",
    "role": "hardware",
    "session_state": "connected",
    "negotiated": {
      "audio_uplink": {
        "codec": "opus",
        "sample_rate": 16000,
        "channels": 1,
        "frame_duration_ms": 60,
        "packetization": "one_opus_packet_per_wspk",
        "version": 1
      }
    }
  }
}
```

约束：

1. 顶层仅允许 `type`, `code`, `data`；`type` 必须是字符串 `sys.ack`，`code` 必须是 JSON 整数零（布尔值不算整数零），`data` 必须是对象。
2. `data` 仅允许 `type`, `role`, `session_state`, `negotiated`；三项字符串值必须分别精确等于 `sys.client.hello`、`hardware`、`connected`。本 pinned Runtime ACK 不含 `status`；收到 `status` 或其它额外字段均拒绝。
3. `negotiated` 与 `audio_uplink` 必须是对象，字段集和值必须与上方 pinned Runtime 当前输出一致。此检查只识别 Runtime ACK 格式；不得据此宣告音频 capability、开启音频路径或改变 CR-010。
4. 顶层、`data`、`negotiated`、`audio_uplink` 任一对象出现缺字段、未知字段、错误类型/值、非法 JSON 或重复 JSON key，均拒绝该 ACK，关闭当前连接并按既有 reconnect policy 处理。重复 key 的拒绝是本地 fail-closed 解析要求；不声称上游 Python 默认 JSON decoder 会拒绝重复 key。
5. 上游 ACK 不含 pairing/session correlation 字段。只允许将该 ACK 关联到**同一 WebSocket socket generation**上、该连接唯一尚待确认的首个 HardwareHello；不得要求或伪造 `pair_request_id`、`daemon_instance_id`、`session_token` 回显。旧 generation 的迟到 ACK 不得确认新连接。
6. `sys.nack`、超时、binary frame、非文本首响应、其它 `type` 或上述任一检查失败都不是 success ACK；不得将连接标为已确认。按当前连接错误与重连路径关闭/重试，不修改线上 schema。

本裁决严格镜像锁定 v0.1.11 当前 success ACK builder 的输出。SDK/Runtime 升级导致该 ACK 改变时，应重新核对上游源码并提交 A0 CR 更新合同，不得靠放宽生产 parser 静默接受新格式。

## 3. A2 实施与验收边界

- **A2 狭义授权：** 仅修改 `firmware/components/desk_sdk_bridge/` 和 `tests/sdk_contract/`，实现以上 ACK 解析、same-generation/one-pending-Hello关联和失败关闭，并增加生产 C path 合同测试及 pinned Runtime localhost loopback 回归。不得改产品 capability、音频协商、UDP Pairing、Host 服务或其它 Agent 路径。
- **L2 通过条件：** 正例接受 pinned Runtime 原样 ACK；反例逐项覆盖错误/嵌套位置的 type、code（含 boolean/非整数）、data/role/session_state/negotiated 字段缺失、额外/`status` 字段、错误值/类型、每层 duplicate key、截断/非法 JSON、NACK、非文本首帧、旧 socket generation ACK。测试证明失败 ACK 不设置 ready/authenticated latch、不向上层报告已连接，旧 generation ACK 不可确认新连接；loopback 使用未修改 pinned Runtime 和生产 C WebSocket client。
- **L2 证据限制：** native C 测试和 localhost loopback 只证明解析/状态逻辑及与 pinned Runtime 软件互通，不证明 ESP-IDF 目标编译或固件烧录。
- **L3 尚未通过：** 需真实目标设备通过局域网完成发现、配对、WebSocket 连接和 HardwareHello success ACK；本 CR 不授权硬件操作。
- **L4 尚未通过：** 需另行完成系统级断网、Runtime/设备重启、恢复和长稳闭环；本 CR 的 L2 通过不代表 L4。

## 4. 未裁决事项

- 当前 pinned Runtime success ACK 明确发送 `negotiated.audio_uplink`，但其意义与固件 CR-010 的音频 unsupported 结论之间是否存在上游协议语义冲突，尚未由 SDK 上游或独立权威协议文档解释。本 CR 仅要求字面格式验证并禁止固件据此启用音频，不裁定音频能力协商语义。
- 若未来要求 ACK 携带或核验配对会话 ID、session token 回显、status 字段或其它 correlation 标记，当前 pinned Runtime 源码不支持这些字段。不得由 A2自行增加要求；先提交新的 A0/上游协议 CR。
