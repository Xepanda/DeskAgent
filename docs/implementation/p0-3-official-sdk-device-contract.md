# P0-3 官方 Python SDK 设备接入契约（A0 关口）

核对范围：本地锁定副本 `.target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk`、`firmware/components/desk_sdk_bridge/`、`tests/sdk_contract/`。这是 SDK 源码契约，不代表设备已互通。

## 必须实现的接入顺序

1. **主机发现设备**：官方 Runtime 在主机侧启动 UDP pairing 广播和外部 WebSocket 服务。`runtime/daemon/pairing/session.py::start_pairing()` 生成含六位 `pairing_code`、`target_mode`、`websocket_port` 的 `PairRequest`；`pairing/udp.py::broadcast_once()` 广播该请求。设备在 UDP 37021 收到后，校验配对码并单播 `pair.accept` 回主机。配对码是主机用户输入后发送给设备的值，设备必须与自身当前有效码精确比较。
2. **设备主动连主机**：`runtime/daemon/connections/websocket_server.py::start()` 用 `websockets.serve()` 在主机监听，首帧必须是硬件 `sys.client.hello`。因此设备是 WebSocket 客户端，目标为配对请求来源主机 IP + 请求中的 `websocket_port`。设备侧监听 TCP 的方向相反；裸 TCP 也不是 WebSocket，不能互通。
3. **HardwareHello 与配对态绑定**：首帧须严格包含 `type=sys.client.hello`, `code=0`, `data.role=hardware`, `pairing_protocol=watcher-lan-pairing`, `pairing_version=1.0`, `pair_request_id`, `daemon_instance_id`, `session_token`, `mode`。这些值须来自刚完成的配对结果；主机验证成功才回 `sys.ack`，协议/凭据错误会 `sys.nack` 并关闭。首帧超时、错配、重复 hello 均须失败关闭。
4. **能力和业务帧**：hello 成功后以 `sys.ack` 发布实际支持的 capability；`sys.sdk.ready.get` 返回一致能力。每个命令遵循当前 SDK 的 request/response envelope、请求 ID 关联、错误码和结果字段；不得把“已接受”冒充“动作已完成”。媒体走二进制 WSPK，当前头为 little-endian `<4sBBHII`、16 字节，包含 magic/type/flags/stream_id/sequence/payload_len；图像 frame type 为 3。文本 JSON 命令、JSON 结果和二进制媒体须在同一 WebSocket 连接上正确分帧。
5. **断线恢复**：连接断开后清理旧连接状态和未完成请求，按 pairing session 状态机重连；新连接仍执行 HardwareHello。SDK 提供 1.1 fast-reunite 消息，但当前对接的 1.0 手动配对路径可先按重新发现/配对恢复，不得伪造已配对。超时和服务退出要显式报告 offline/reconnecting。

## 当前固件与缺口

- `firmware/main/src/app_main.c:428-439` 启动设备侧 `0.0.0.0:37020` TCP server 和 `:37021` UDP pairing server。桥接实现 `desk_sdk_bridge.c:1906-1963` 使用 `SOCK_STREAM` listen；SDK 则由 Runtime listen、设备连接。**官方 SDK 不会因当前设备 TCP 服务而连上。**
- UDP wire message 校验已有：`desk_sdk_bridge.c:1028-1105` 检查六位格式/精确匹配，错码不发响应、不发 token；官方配对拒绝没有独立 wire 消息，静默丢弃是正确 fail-closed 行为。测试 `tests/sdk_contract/test_official_sdk_runtime_compat.py::test_official_udp_pairing_rejected_on_wrong_code` 覆盖错误码拒绝。
- 配对码只有 `desk_sdk_bridge_set_pairing_code()` 注入入口（`desk_sdk_bridge.c:101-119`），全仓未发现生产调用点；启动路径只启动服务。未配置则所有配对静默失败。权威来源/展示/过期策略是**产品与 A1 接口待定项**，不是 A2 可硬编码的值；请由 A1/A0 定义安全的运行时注入来源与生命周期后，A2 接入该接口。
- 现有 `sys.client.hello`（`desk_sdk_bridge.c` 约 1190 行起）按桌面 JSON bridge 返回 capability；HardwareHello 会话校验尚不等于官方 Runtime 设备端 `HardwareHello` authorizer 流程。SDK 官方 hello parser 位于 `runtime/daemon/pairing/protocol.py::parse_hardware_hello()`。现有测试通过 C 函数直接调用协议处理器，不经过 WebSocket Runtime。
- capability 常量在 `desk_sdk_bridge.h:29-36`：`inputs`, `motion`, `camera`, `audio.stream`（另有兼容别名）、`expression.audio_follow.v1`（另有兼容别名）、`mcp_tools`。测试目前只断言列表含若干域；A2 必须逐项证明 SDK 各 API 实际可用，不能把声明列表当功能验收。
- 媒体打包和图片命令处理已在桥接层存在（`desk_sdk_bridge.c:900-1000, 1354-1445`），但当前发送路径属于设备 TCP socket/outbound callback，不是 Runtime WebSocket。SDK WSPK解析器在 `watcherobot/protocol.py::parse_wspk()`；固件契约测试只证明帧格式/模拟回调，不证明真实相机 JPEG 可用。

## A2 可执行验收（通过条件）

1. 使用未修改的本地官方 SDK Runtime，设备以 WebSocket client 连接 Runtime 实际监听的主机地址/端口；证明完整握手及 `sys.ack`，同时证明访问设备 TCP server 没有被误当成 SDK 互通。
2. 对正确码完成 UDP pair request/accept + HardwareHello；错误码、未配置码、错误 session token、过期/错配 request ID 全部拒绝，且日志不泄漏 pairing code/session token。
3. Runtime 读取 capabilities 后调用每个声明的低风险 API；命令 ID/结果关联正确，错误与超时被保留。媒体至少通过 SDK parser 收到一张可解码、现场内容正确的 JPEG WSPK image；未实现项从 capability 移除或如实标 unsupported。
4. 断开 Wi-Fi/关闭 Runtime/设备重启后，连接状态转 offline/reconnecting；恢复网络和 Runtime 后重新握手、命令往返成功，无旧连接残帧或幽灵结果。记录源码版本、固件 SHA、Runtime/设备日志及实际结果。

## 官方源码依据

- Pairing 消息/严格字段/HardwareHello：`src/watcherobot/runtime/daemon/pairing/protocol.py`（`parse_udp_message`, `parse_hardware_hello`, `build_hardware_hello_ack/nack`）；配对状态/凭据检查：`pairing/session.py::start_pairing/accept_device`。
- 主机广播和监听方向：`pairing/udp.py::broadcast_once`; `connections/websocket_server.py::start/_handle_connection/_handle_hello`。
- WSPK：`src/watcherobot/protocol.py::build_wspk/parse_wspk`；官方向设备发命令使用已声明的设备连接：`runtime/daemon/connections/registry.py`。
- 与当前 A2 静态/模拟测试的差异：`tests/sdk_contract/test_official_sdk_runtime_compat.py`。它不是官方 Runtime 的 live socket/device integration test。
