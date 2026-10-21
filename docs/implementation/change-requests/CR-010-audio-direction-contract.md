# CR-010：官方 SDK 音频方向契约与能力宣告

- **提出方**：A0 架构与协议
- **提出日期**：2026-10-22
- **状态**：**部分完成 — capability truthfulness correction 已由 A2 在 `0e4819e` 完成（L2）；播放与麦克风上行数据路径仍 OPEN**
- **关联**：`docs/implementation/p0-3-official-sdk-device-contract.md`、`docs/implementation/firmware-bringup-dispatch-2026-10-18.md`、锁定版官方 SDK `src/watcherobot/robot.py`、`src/watcherobot/application/transport.py`、`docs/microphone-audio.md`
- **影响模块**：A0（契约维护）、A1（音频 HAL/资源所有权/采集与播放）、A2（SDK 控制与 WSPK 桥接）、A7（端到端验收）

## 1. 裁决范围

锁定版官方 Python SDK 定义了两条独立的单向音频路径：主机向设备播放 PCM，以及设备向主机发送麦克风 Opus。它们都属于当前官方 SDK API/传输契约。实现其中一条不代表另一条已实现；仅在 hello/ready 中宣告 capability 或 `negotiated.audio_uplink` 也不代表数据路径可用。

这两条单向路径**不构成同时收发的全双工合同**。官方 SDK 将 `rtc.audio.full_duplex.v1` 作为单独 RTC capability；DeskAgent 不得从播放与麦克风 API 并存推断全双工、AEC、回声消除或打断能力。当前硬件仲裁还明确让 TClaw 对话优先于远端 SDK 播放。

## 2. 主机 → 设备：PCM 播放

### 2.1 契约

1. 设备只有在整条播放路径可用时，才可在 `sys.client.hello` 与 `sys.sdk.ready.get` 的 capability 列表中宣告 `audio.stream`。
2. 主机发 `ctrl.audio.stream.begin`，携带 `stream_id`、`total_bytes`、`sample_rate_hz`、`channels`、`sample_width_bytes`、`audio_sha256`。设备必须校验参数并返回与请求关联的成功 ACK，或返回可诊断的 NACK；不能静默接受未实现的命令。
3. 主机随后在同一 WebSocket 连接发送 WSPK `FRAME_AUDIO` 二进制帧。payload 是 PCM 字节；stream id 与 begin 一致，sequence 从 0 单调递增，首帧置 `FIRST`，末帧置 `LAST`。发送端依照 `audio.flow` 的设备缓冲 credit 控制在途帧；设备须准确报告可用缓冲/播放状态或错误，避免无界缓存。
4. 设备完成播放后须提供 SDK 可识别的完成状态。取消/停止通过 `ctrl.audio.stop` 结束对应流；设备应停止该 stream 并回报结果。重复、未知 stream 或格式错误须返回明确错误，不得误报完成。
5. WSPK 使用官方 16 字节小端头 `<4sBBHII>`；`frame_type=FRAME_AUDIO (1)`，header 的 payload length 必须与本帧内容一致。

### 2.2 来源

- 官方 SDK `src/watcherobot/robot.py::_start_audio_playback()`：检查 `audio.stream`，发 `ctrl.audio.stream.begin` 及上述元数据，再调用 transport 发送流。
- 官方 SDK `src/watcherobot/application/transport.py::_send_audio_stream()`：WSPK FRAME_AUDIO、FIRST/LAST、sequence 与 credit 流控；同文件处理 audio flow 更新。
- 官方 SDK `src/watcherobot/protocol.py::build_wspk()`：16 字节 WSPK 头格式。

## 3. 设备 → 主机：麦克风 Opus 上行

### 3.1 契约

1. 麦克风上行由独立的 `ctrl.microphone.open` / `ctrl.microphone.close` 控制，不以 `audio.stream` 播放 capability 作为其替代或充分条件。设备必须仅在麦克风采集、Opus 编码、WSPK 发送和会话关闭路径均可用时，才发布对应麦克风上行能力/协商结果；若现有 SDK/hello schema 没有独立 microphone capability 字段，不得擅自复用 `audio.stream`，应通过 A0 CR 明确其正式表示方式。
2. 主机发送 `ctrl.microphone.open`，当前 SDK 请求 `sample_rate_hz=16000`。成功 ACK 必须提供正值 `session_id`；无效参数、资源冲突或硬件不可用须 NACK，不能返回伪成功 session。
3. 设备将采集音频编码为 Opus，格式为 16 kHz、单声道、每包 60 ms；每个 WSPK `FRAME_AUDIO` payload 恰为一个完整 Opus packet。帧 stream_id 使用该会话 ID 的 16 位值（SDK 同时接受 stream_id 0 作为兼容路由），sequence 按帧递增。正常结束或远端停止时用 `LAST` 结束流。
4. 主机发送 `ctrl.microphone.close`（携带 session_id）后，设备停止采集与上行并 ACK；关闭/断线时不得残留采集任务、旧会话帧或占用资源。设备的 WSPK 上行必须与控制命令共用已配对 WebSocket 会话。
5. 麦克风采集必须通过 A1 音频 HAL/仲裁 API 获取 owner。A2 不得直接读 codec、调用驱动或绕过 TClaw/远端音频仲裁。现有 HAL 把录音 owner 固定为 `DESK_OWNER_TCLAW_VOICE`，与远端 SDK microphone session 的所有权语义需由 A1 提交最小接口 CR 或按此合同调整，不能以伪成功 ACK 掩盖冲突。

### 3.2 来源

- 官方 SDK `src/watcherobot/robot.py::_open_microphone()`：控制命令、16 kHz 请求、成功 ACK 中的正 `session_id`。
- 官方 SDK `src/watcherobot/robot.py::_on_binary()`：按 session/stream id 路由 FRAME_AUDIO，以 `LAST` 结束会话。
- 官方 SDK `docs/microphone-audio.md`：设备上行 Opus 16 kHz mono、60 ms、one packet per WSPK audio frame；SDK Application 可解码为 PCM。
- 锁定版 SDK `tests/fixtures/contracts/watcher_lan_pairing_v1.json`：`negotiated.audio_uplink` 声明 codec、采样率、通道、包时长、封包和版本。

## 4. 能力宣告及双向边界

- `audio.stream` 精确定义为主机到设备的 PCM 播放能力。只有 begin、PCM 帧接收、流控、播放、停止/错误反馈全部实现时才可宣告。
- 麦克风上行是另一条设备到主机路径，由 microphone open/close 与 `audio_uplink` 格式协商表示。设备仅在采集、Opus 编码、会话路由和 WSPK 上行实际可用时才可宣告/协商。由于当前 SDK 的 uplink fixture 使用 `negotiated.audio_uplink`，而公共 capability list 未给出 microphone capability 名称，新增/复用 capability 名称必须由 A0 另行裁决；实现者不得臆造名称或将 `audio.stream` 扩义。
- 双向 API 同时存在不代表可以并发。SDK 音频 RTC 的全双工能力另以 `rtc.audio.full_duplex.v1` 表示。没有对应 capability、AEC/声学路径、所有权策略与真实并发验收时，系统按半双工及现有 owner 仲裁处理；不可宣称全双工、AEC 或 barge-in。
- 未实现或资源不可用的方向必须从能力列表/协商结果中移除或返回明确 unsupported/busy，不得只因 hello 宣告而被 SDK 当作可用。

## 5. Owner 与最小实施边界

- **A1**：提供有 owner 的音频 HAL 接口；定义 TClaw 与 SDK 播放/采集之间的仲裁、抢占和停止行为；提供 PCM 播放入口、麦克风 PCM 采集入口及可复用/可验证的 Opus 编码器路径；处理采样率、声道、缓冲、错误和资源释放。若当前接口不能表达 SDK microphone owner，提交接口 CR，由 A0 对齐接口后再实现。
- **A2**：实现 WebSocket `ctrl.audio.stream.begin`、`ctrl.audio.stop`、`ctrl.microphone.open/close` 控制处理及 ACK/NACK；实现 FRAME_AUDIO WSPK 入站解析与 PCM 播放交给 A1；实现 A1 上行音频的 WSPK FRAME_AUDIO 封装、sequence/session 生命周期及必要 `audio.flow`/状态反馈；仅在对应端到端方向完整可用后发布能力。
- **A7**：按方向独立验收并归档现场设备、固件 SHA、SDK 版本、协议日志与听感/采集证据。一个方向通过不能替代另一个方向。
- **A0**：维护 capability 命名/协商 schema；裁决 A1 提出的 HAL ownership 接口 CR。当前官方 source 能证明传输格式，不证明本设备 Opus encoder 或并发音频硬件能力。

## 6. 验收证据

**播放方向通过**须用未修改锁定 SDK 完成真实 PCM 播放，保留 begin ACK、至少首/中/末 WSPK 帧的 stream/sequence/flags/payload 长度、credit/播放状态、stop 或完成状态；确认设备扬声器听感与长度合理，并验证 busy、坏参数、取消和断线时无假成功或卡死。源码测试/模拟 transport 只算 Level 2。

**麦克风上行通过**须用未修改锁定 SDK 打开 microphone session，保留 open ACK/session_id、设备侧采集/Opus 编码状态及连续 WSPK FRAME_AUDIO 的 session/sequence/帧长/`LAST` 日志；SDK 能解码并实际读取有声 PCM，close 后停止发帧且资源 owner 释放。现场音频内容应由测试者确认，不归档不必要的原始敏感语音。

**全双工**不属于本 CR 的验收目标。若产品要求同时采集和播放，须另开 CR 定义 `rtc.audio.full_duplex.v1` 的合同、AEC/回声路径、owner 仲裁、插话语义及 Level 3/4 实测门槛。

## 7. 当前状态与剩余缺口

### 7.1 已完成：能力声明与命令拒绝修正（Level 2）

A2 commit `0e4819e31bad42c3cdc6632d574ce465b980004e` 已完成软件侧 truthfulness correction：从 `sys.client.hello` / `sys.sdk.ready.get` capability 列表移除 `audio.stream` 与兼容别名 `audio_stream`；hardware hello 不再协商 `audio_uplink`；`ctrl.audio.stream.begin`、`ctrl.audio.stop`、`ctrl.microphone.open`、`ctrl.microphone.close` 均明确返回 `UNSUPPORTED_OPERATION`，不再伪造 microphone session 或启动采集。该代码也包含在本轮设备 app 镜像的源码 commit 中，但本项的验收证据仍为软件 L2；不代表设备已完成官方 SDK 音频互通。

### 7.2 未完成：双向媒体数据路径（仍 OPEN）

- **Host → device 播放**：WebSocket `FRAME_AUDIO` 入站解析、PCM payload 校验、audio flow/credit、A1 播放 HAL、停止/完成反馈尚未打通。
- **Device → Host 麦克风上行**：SDK microphone session、A1 音频 owner/采集接口、Opus 编码、WSPK `FRAME_AUDIO` 上行、sequence/session 关闭与断线清理尚未打通。
- 因此目前两条方向都应保持 unsupported；`audio.stream` / `audio_stream` 不应重新宣告，也不得生成 `negotiated.audio_uplink`。实现须先解决 A1 HAL owner 契约，再按第 6 节逐方向验收。

A2 的 Runtime+C localhost 软件互操作测试不能填补上述缺口：测试的 JPEG 是 fixture；测试没有真实 PCM 播放、麦克风采集或 Opus 音频帧交换。双向音频只有在未修改 pinned SDK 与真实设备完成对应方向的 L3 实测后，才可将该方向从 unsupported 升级为可用；全双工仍不在本 CR 范围。
