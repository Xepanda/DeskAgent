# COM21 只读现场状态快照（2026-10-23）

**源码基线**：PR #15 head `ae4dbcbbc892ec37643694d2e65407f6aae3a6fe`

**证据级别**：Level 3，单项设备状态快照

## 证据范围

本记录仅覆盖一次约 54 秒的 ESP32-S3 串口日志采集和随后只读 CLI 状态查询，按 **Level 3：单项设备状态快照** 记录。它不构成语音对话、相机取帧、STM32 动作或端到端恢复验收。

日志采集命令：

```text
capture_esp32s3_logs.py --capture --port COM21 --baud 115200 --output-dir .target_logging/serial-capture-20261023 --no-raw --live-events stdout
```

连接成功；`--live-events stdout` 将结构化事件行实时输出。脱敏事件文件保存在主机 ignored 路径 `D:\ePanda\DeskAgent\.target_logging\serial-capture-20261023\esp32s3_COM21_20261023T121438_415914Z.events.jsonl`，不包含在本 PR 中。文件中共 7 条事件：1 条 `transport connected`（12:14:38.564Z）和 6 条周期 memory 事件（12:14:42–12:15:32Z）。6 次 heap 均为 `21911`；PSRAM 首次为 `7063472`，其后 5 次均为 `7063464`。事件文件没有 KWS、ASR 或 TTS 事件。CLI 原始终端转录未保存；以上摘要仅列传输与内存事件，不复述其他字段。

随后将 COM21 的 DTR/RTS 置低，执行只读 CLI：

| 查询 | 现场观测 | 证据边界 |
| --- | --- | --- |
| `body status` | UART READY；cached setpoint `90/120`；TX length `0`；peer/ACK `UNKNOWN` | setpoint 是缓存目标值，不是机身位置反馈。TX length 是发送侧计数，不是 STM32 接收计数或 ACK。未确认实物动作。 |
| `audio status` | READY；mic YES；frames `73618`；errors `0`；peak `73`；playback owner `0`；record owner `2` | 证明本次快照中音频输入状态和帧计数可读；没有观察到 KWS/ASR/TTS 事件，不能据此判定唤醒或问答闭环通过。 |
| `camera status` | READY；owner `0`；cached frame `NONE` | 没有取得 JPEG；READY 状态不代表已采集图像。 |

## 验收状态

- **语音唤醒与对话：未验证。** 本次约 54 秒日志中未捕获 KWS/ASR/TTS 事件；没有进行现场唤醒或问答验收。
- **相机：未通过真实取帧验收。** 缓存帧为 NONE；本次没有执行 `camera capture`，没有 JPEG。
- **STM32：实物 ACK 与动作未知。** 仅读取状态；peer/ACK 为 UNKNOWN，TX length 为 0 且不是接收或 ACK 计数，没有执行任何动作。
- **操作边界：** 未烧录固件；未执行相机采集或任何动作；COM20 未触碰。

该快照只反映本次连接和采集窗口内看到的状态。CLI 原始输出未归档，因此逐字输出不可复核；不得将本记录外推为其他时间、固件或设备状态。
