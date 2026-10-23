# COM21 只读现场状态快照（2026-10-23）

**源码基线**：PR #15 head `ae4dbcbbc892ec37643694d2e65407f6aae3a6fe`  
**证据级别**：Level 3，单项设备状态快照

## 证据范围

本记录仅覆盖一次约 54 秒的 ESP32-S3 串口日志采集和随后只读 CLI 状态查询，按 **Level 3：单项设备状态快照** 记录。它不构成语音对话、相机取帧、STM32 动作或端到端恢复验收。

日志采集命令：

```text
capture_esp32s3_logs.py --capture --port COM21 --baud 115200 --output-dir .target_logging/serial-capture-20261023 --no-raw --live-events stdout
```

连接成功。周期遥测记录 heap 值 `21911`、PSRAM 值约 `7063464`。采集期间没有观察到 KWS、ASR 或 TTS 事件。采集输出目录中的事件记录路径为 `.target_logging/serial-capture-20261023/events.jsonl`。该 ignored 输出目录不在本独立 worktree 内，命令行原始输出也没有归档；以上数值与事件观察按本轮现场采集结果摘要记录。

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
