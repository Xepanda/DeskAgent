# P0-3 官方 Python SDK 设备接入契约（A0 关口）

音频方向与 capability 宣告的专项裁决见 [CR-010](change-requests/CR-010-audio-direction-contract.md)。A2 已在 `0e4819e` 移除未实现的 `audio.stream` / `audio_stream` 宣告和 `audio_uplink` 协商；双向音频数据路径仍未实现，当前必须保持 unsupported。

核对范围：本地锁定副本 `.target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk`、`firmware/components/desk_sdk_bridge/`、`tests/sdk_contract/`。依据 `contracts/dependency_lock.json`，官方 Python SDK/Runtime 锁定为 tag `v0.1.11`、commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`。A2 最新报告该互操作套件 **15 passed、2 subtests passed**。测试分层：静态/直接 handler 测试为 Level 2；`test_c_firmware_client_runtime_commands_results_events_media_and_reconnect` 会启动未修改 pinned `ExternalWebSocketServer`，用生产 C WebSocket client 在 localhost 建立真实 socket，往返命令/结果、事件、WSPK JPEG fixture 并验证重连；另有错误配对码拒绝测试。这些是 Runtime+C 软件 loopback 的 Level 2，不代表物理 ESP32-S3、真实相机 JPEG 或设备端到端 L3/L4 已通过。

生产配对码采用 [CR-003](change-requests/CR-003-pairing-code-production-injection.md) 的 A0 推荐策略：用户在屏幕 UI 显式打开配对窗口后，由设备生成短时六位码，RAM 注入 A2 setter 并只在设备屏幕显示；配对成功、失败次数封锁、取消、超时、会话重置或重启均清零，重新开窗生成新码。A1/A2 的 UI 入口及 5 分钟窗口仍待实施验证，不能写成已实现。

## 必须实现的接入顺序

1. **主机发现设备**：官方 Runtime 在主机侧启动 UDP pairing 广播和外部 WebSocket 服务。`runtime/daemon/pairing/session.py::start_pairing()` 接收六位 `pairing_code` 并生成含 `target_mode`、`websocket_port` 的 `PairRequest`；`pairing/udp.py::broadcast_once()` 广播该请求。设备在 UDP 37021 收到后，必须将请求码与本机当前活动窗口显示的码精确比较，成功后单播 `pair.accept` 回主机。码的来源和生命周期见 CR-003；Host UI 输入不能代替设备侧验证。
2. **设备主动连主机**：`runtime/daemon/connections/websocket_server.py::start()` 用 `websockets.serve()` 在主机监听，首帧必须是硬件 `sys.client.hello`。因此设备是 WebSocket 客户端，目标为配对请求来源主机 IP + 请求中的 `websocket_port`。监听必须从设备所在 LAN 可达；将 Runtime 限制为 `127.0.0.1` 会阻断设备连接。`interfaces.md` §6.5 的 loopback 默认只适用于 Windows Companion HTTP RPC，不适用于官方 Runtime。设备侧监听 TCP 37020 的方向和协议均不同；裸 TCP 不会与 Runtime WebSocket 互通。
3. **HardwareHello 与配对态绑定**：首帧须严格包含 `type=sys.client.hello`, `code=0`, `data.role=hardware`, `pairing_protocol=watcher-lan-pairing`, `pairing_version=1.0`, `pair_request_id`, `daemon_instance_id`, `session_token`, `mode`。这些值须来自刚完成的配对结果；主机验证成功才回 `sys.ack`，协议/凭据错误会 `sys.nack` 并关闭。首帧超时、错配、重复 hello 均须失败关闭。
4. **能力和业务帧**：hello 成功后以 `sys.ack` 发布实际支持的 capability；`sys.sdk.ready.get` 返回一致能力。每个命令遵循当前 SDK 的 request/response envelope、请求 ID 关联、错误码和结果字段；不得把“已接受”冒充“动作已完成”。媒体走二进制 WSPK，当前头为 little-endian `<4sBBHII`、16 字节，包含 magic/type/flags/stream_id/sequence/payload_len；图像 frame type 为 3。文本 JSON 命令、JSON 结果和二进制媒体须在同一 WebSocket 连接上正确分帧。
5. **断线恢复**：连接断开后清理旧连接状态和未完成请求，按 pairing session 状态机重连；新连接仍执行 HardwareHello。SDK package 版本是 `v0.1.11`；pairing protocol / fast-reunite 消息中的 `1.0` / `1.1` 是协议版本字段，不是 SDK 发布版本。当前本仓测试没有验证 fast-reunite 的真机时序，安全基线为重新发现/配对恢复，不得伪造已配对。超时和服务退出要显式报告 offline/reconnecting。

## 当前固件与缺口

- `firmware/main/src/app_main.c:428-439` 启动设备侧 `0.0.0.0:37020` TCP server 和 `:37021` UDP pairing server。桥接实现 `desk_sdk_bridge.c:1906-1963` 使用 `SOCK_STREAM` listen；SDK 则由 Runtime listen、设备连接。**官方 SDK 不会因当前设备 TCP 服务而连上。**
- UDP wire message 校验已有：`desk_sdk_bridge.c:1028-1105` 检查六位格式/精确匹配，错码不发响应、不发 token；官方配对拒绝没有独立 wire 消息，静默丢弃是正确 fail-closed 行为。测试 `tests/sdk_contract/test_official_sdk_runtime_compat.py::test_official_udp_pairing_rejected_on_wrong_code` 覆盖错误码拒绝。
- 配对码只有 `desk_sdk_bridge_set_pairing_code()` 注入入口（`desk_sdk_bridge.c:101-119`），全仓未发现生产调用点；启动路径只启动服务。未配置则所有配对静默失败。CR-003 已提出设备生成、屏幕显示、短时 RAM 注入策略；A1/A2 的 UI 入口和生命周期尚未实现，不能写入生产配置、Tuya license 或持久化 KV。
- 现有 `sys.client.hello`（`desk_sdk_bridge.c` 约 1190 行起）按桌面 JSON bridge 返回 capability；HardwareHello 会话校验尚不等于目标硬件上的官方 Runtime 设备会话。SDK 官方 hello parser 位于 `runtime/daemon/pairing/protocol.py::parse_hardware_hello()`。测试既包含直接调用 C pairing/handler 的静态合同，也包含 Runtime+C localhost loopback；后者通过测试 shim 注入已校验的 Runtime 地址/端口，使用 `interop_4x3.jpg` fixture，不验证真实 UDP 网络发现或物理相机采集。
- capability 常量在 `desk_sdk_bridge.h:29-36`：`inputs`, `motion`, `camera`, `expression.audio_follow.v1`（另有兼容别名）、`mcp_tools`；当前列表不包含 `audio.stream` / `audio_stream`。A2 已将四类音频控制命令显式 NACK 为 `UNSUPPORTED_OPERATION`，并移除 `audio_uplink` 协商（CR-010，L2）；A2 必须逐项证明 SDK 各 API 实际可用，不能把声明列表当功能验收。
- 媒体打包和图片命令处理已在桥接层存在（`desk_sdk_bridge.c:900-1000, 1354-1445`）；连接官方 Runtime WebSocket 时发送端可通过 `desk_sdk_ws_client_send_binary()` 发送 WSPK，也保留自有 TCP fallback。Runtime+C loopback 测试已用 fixture JPEG 验证 WSPK binary；这不证明相机能采到真实 JPEG，当前相机实机仍须按 CR-004 验收。

## A2 可执行验收（通过条件）

1. 已有 Level 2：未修改 pinned Runtime loopback server 与生产 C WebSocket client 在 localhost 完成握手、`sys.ack`、命令/结果、事件、WSPK fixture 和重连。Level 3 尚需真实 ESP32-S3 在 LAN 上主动连接实际 Runtime，并证明完整发现/配对/握手；访问设备 TCP server 不能被误当成 SDK 互通。
2. 对正确码完成 UDP pair request/accept + HardwareHello；错误码、未配置码、错误 session token、过期/错配 request ID 全部拒绝，且日志不泄漏 pairing code/session token。
3. Runtime 读取 capabilities 后调用每个声明的低风险 API；命令 ID/结果关联正确，错误与超时被保留。媒体至少通过 SDK parser 收到一张可解码、现场内容正确的 JPEG WSPK image；未实现项从 capability 移除或如实标 unsupported。
4. 断开 Wi-Fi/关闭 Runtime/设备重启后，连接状态转 offline/reconnecting；恢复网络和 Runtime 后重新握手、命令往返成功，无旧连接残帧或幽灵结果。记录源码版本、固件 SHA、Runtime/设备日志及实际结果。

## 官方源码依据

- Pairing 消息/严格字段/HardwareHello：`src/watcherobot/runtime/daemon/pairing/protocol.py`（`parse_udp_message`, `parse_hardware_hello`, `build_hardware_hello_ack/nack`）；配对状态/凭据检查：`pairing/session.py::start_pairing/accept_device`。
- 主机广播和监听方向：`pairing/udp.py::broadcast_once`; `connections/websocket_server.py::start/_handle_connection/_handle_hello`。
- WSPK：`src/watcherobot/protocol.py::build_wspk/parse_wspk`；官方向设备发命令使用已声明的设备连接：`runtime/daemon/connections/registry.py`。
- 锁定版本：官方 WatcheRobot Python SDK/Runtime tag `v0.1.11`、commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`（见 `contracts/dependency_lock.json`）。`tests/sdk_contract/test_official_sdk_runtime_compat.py` 同时包含 Level 2 直接 handler 测试与真实 localhost loopback：启动 pinned 的未修改 Runtime `ExternalWebSocketServer`，由编译的生产 C WebSocket client 建立 socket，测试控制/结果、事件、fixture WSPK、断开重连及错码拒绝。测试 shim 设置 loopback peer，JPEG 是 4x3 fixture；不覆盖物理板、真实 UDP 网络发现或相机采集，因此不构成 Level 3 真机互通。
