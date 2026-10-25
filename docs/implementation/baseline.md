# Desk Agent R1 技术基线与依赖核验证明 (G0 Baseline)

> 文档版本：V2.0.0  
> 责任 Agent：A0 架构与协议  
> 核验与冻结日期：2026-10-16  
> 状态：**G0 基线与公共契约冻结 (Baseline & Contracts Frozen)**  
> 参照标准：`AGENTS.md` 架构治理规范、PR #2 评审整改要求与后续分步派发规则

---

## 1. 源码仓库与工作区映射台账 (Repository & Worktree Inventory)

依据 `AGENTS.md` 工作区纪律与保护 Dirty 状态要求，对本地及相关代码资产进行只读扫描与定位，记录绝对路径、分支、Commit SHA 及工作区状态：

| 仓库 / 资产角色 | 本地绝对路径 | 远端 Repository URL | 分支 / 标签 | Commit SHA | 工作区状态 (Dirty / Clean) | 核心职责与说明 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **DeskAgent (主仓)** | `/home/maidang/DeskAgent`<br/>*(Samba: `/home/share/samba/ePanda/DeskAgent`)* | `https://github.com/Xepanda/DeskAgent` | `main` | `6d863c2` (PR #2 合并) | **Clean** | 本项目主代码库、契约规范、Host 服务与 Windows Companion |
| **Tuya TClaw** | `/home/maidang/.gemini/antigravity-cli/brain/49ffa309-8e24-433a-a0da-2c22b3c125ea/scratch/tclaw-repo` | `https://github.com/tuya/TClaw.git` | `master` | `88bd37378f1d106fcc1175e61b368222cc8fabda` | **Dirty** (`?? .share/` 未跟踪文件) | 机器人端主框架，包含 MCP 工具、ACP 客户端与 Tuya 对话主循环 |
| **WatcheRobot Python SDK** | `/home/maidang/.gemini/antigravity-cli/brain/cb82573a-ba9b-42ea-a6be-405983e79f69/scratch/watcherobot_python_sdk` | `https://github.com/orulink-ai/WatcheRobot_python_sdk.git` | `detached HEAD` (tag `v0.1.11`) | `8bc13967a06ef073459be47e930ff43ef37ee8fb` | **Clean** (锁定 Release 0.1.11) | 官方 Python SDK/Daemon/Runtime，定义设备协议、Opus 解码与 WSPK 通道 |
| **WatcheRobot Embedded SDK** | `/home/maidang/.gemini/antigravity-cli/brain/cb82573a-ba9b-42ea-a6be-405983e79f69/scratch/watcherobot_public` | `https://github.com/orulink-ai/WatcheRobot-public.git` | `detached HEAD` (tag `watche-v0.1.2`) | `646a977bfd76a8516ae505fda2331b81e1049b87` | **Clean** (锁定预览版本) | 包含 SenseCAP Watcher 硬件板级 HAL（Audio/LCD/Touch/Camera/STM32 MCU Link） |
| **TuyaOpen SDK** | `/home/share/samba/ePanda/TuyaOpen` | `https://github.com/tuya/TuyaOpen.git` | `master` | `ec8aa7cb` | **Dirty** (`?? Tuya-vuln-report.7z`) | TuyaOpen 基础框架与配套 ESP-IDF v5.4 BSP 环境 |

> [!IMPORTANT]
> **外部工作区保护纪律（遵照 AGENTS.md 第 3 节）**：  
> 经检测，外部 TClaw 仓存在未跟踪目录 `.share/`，TuyaOpen 存在未跟踪文件。A0 与后续所有 Agent **严禁执行 `git reset --hard`、`git clean -fd` 或 `git checkout -f`**，必须如实保护现状，严禁破坏用户本地未提交内容。

---

## 2. 真实板卡型号、引脚与外设定义台账 (Board Hardware Ground Truth)

经核验 `WatcheRobot-public` 嵌入式板级驱动源码（`sensecap-watcher.h`, `mcu_link_uart.c`, `hal_camera.c`, `hal_audio.c`），目标机器人硬件硬件事实如下：

### 2.1 目标板卡核心硬件规格
- **目标板卡**: Seeed SenseCAP Watcher / WatcheRobot 开发套件
- **主控制器 (MCU)**: Espressif ESP32-S3 (双核 Xtensa LX7 @ 240MHz, 8MB Octal PSRAM, 16MB SPI Flash)
- **身体协处理器 (Body MCU)**: STM32 芯片，独立控制两轴舵机与底座状态
- **视觉协处理器 (Vision Coprocessor)**: Himax HX6538，运行 SSCMA/PTL 图像采集与人脸检测
- **音频编解码芯片 (Audio Codec)**: ES8311 DAC (扬声器输出) + ES7210 ADC (麦克风采音)
- **显示面板 (Display)**: 412×412 圆形 LCD，SPD2010 控制器，QSPI 接口
- **触摸芯片 (Touch)**: SPD2010 电容触摸，I2C 接口
- **IO 扩展芯片 (IO Expander)**: PCA95xx 16-bit IO Expander (I2C 地址 0x20)
- **实时时钟 (RTC)**: NXP PCF8563 (I2C 地址 0x51)

### 2.2 硬件引脚分配表 (Pinout & Peripheral Map)

| 外设子系统 | 硬件接口 / 控制器 | ESP32-S3 GPIO 引脚分配 | 附属控制 / 供电控制 (IO Expander) | 协议与电气参数 |
| :--- | :--- | :--- | :--- | :--- |
| **STM32 身体控制** | UART_NUM_2 | **TX: GPIO 19, RX: GPIO 20** | 无 | 921600 波特率, 8N1, 0xA5 0x5A 二进制帧封包 + CRC16-CCITT 校验 (COBS 已正式废除，见 r3a-stm32-protocol-adjudication.md) |
| **音频 I2S (Codec)** | I2S0 | **MCLK: GPIO 10, SCLK: GPIO 11, LRCK: GPIO 12<br/>DSIN: GPIO 15 (ADC), DOUT: GPIO 16 (DAC)** | **IO_EXP Pin 12 (PWR_CODEC_PA)** 功放电源使能 | 16kHz / 24kHz mono s16le PCM, 标准飞利浦 I2S 格式 |
| **通用 I2C (总线 0)** | I2C0 | **SDA: GPIO 47, SCL: GPIO 48, INT: GPIO 2** | 控制 IO 扩展芯片、PCF8563 RTC 与 Codec 配置 | 400 kHz 快速模式 |
| **触摸 I2C (总线 1)** | I2C1 | **SDA: GPIO 39, SCL: GPIO 38** | **IO_EXP Pin 5 (TOUCH_INT)** 触摸中断输入 | 400 kHz 快速模式 |
| **圆形显示屏 (LCD)** | QSPI (SPI3_HOST) | **PCLK: GPIO 7, DATA0: GPIO 9, DATA1: GPIO 1<br/>DATA2: GPIO 14, DATA3: GPIO 13, CS: GPIO 45<br/>BL: GPIO 8 (PWM 背光调节)** | **IO_EXP Pin 9 (PWR_LCD)** 屏幕供电使能 | 412×412 分辨率, RGB565 16-bit, 40MHz Pixel Clock |
| **Himax HX6538 相机** | SPI2_HOST + UART1 | **SCLK: GPIO 4, MOSI: GPIO 5, MISO: GPIO 6, CS: GPIO 21<br/>Flasher TX: GPIO 17, RX: GPIO 18 (921600)** | **IO_EXP Pin 6 (SYNC), Pin 7 (RST)<br/>Pin 11 (PWR_AI_CHIP)** 芯片供电使能 | PTL / SSCMA 协议, 单帧 JPEG 快照输出 |
| **滚轮旋钮 (Knob)** | GPIO 中断 + 扩展器 | **A: GPIO 41, B: GPIO 42** | **IO_EXP Pin 3 (KNOB_BTN)** 按键输入 | 旋转编码器双相脉冲 + 单击按键 |
| **RGB 指示灯** | 单线 RMT / SPI | **CTRL: GPIO 40** | 无 | WS2812 地址化单总线全彩 LED |
| **电池电量检测** | ADC1_CHANNEL_2 | **ADC: GPIO 3** (分压比 82k:20k) | **IO_EXP Pin 15 (PWR_BAT_ADC)** 采样使能<br/>**Pin 0 (CHRG), Pin 1 (STDBY), Pin 2 (VBUS)** | 0 ~ 1100 mV 衰减量程 (2.5dB), 电池电压反算 |

---

## 3. 四层架构职责与所有权边界 (Role Definition & Ownership)

为了彻底杜绝职责混淆、抢占冲突和第二套未受管协议，明确界定四大运行组件的核心职责与严苛边界：

```
+---------------------------------------------------------------------------------------+
|  1. 固件层 (Firmware - ESP32-S3):                                                     |
|     - TClaw 主框架: Tuya 官方云端语音对话、采音/播报主循环、MCP 外部工具请求生成      |
|     - 硬件资源仲裁器 (Resource Arbiter): 统筹 Audio, Motion, Camera, Display 单一所有权|
|     - 板级驱动 (BSP HAL): STM32 Body 舵机 (TX:19/RX:20)、ES8311/ES7210、HX6538 相机  |
|     - SDK 适配层: UDP 37021 配对处理；目标 Runtime 是 LAN WebSocket 服务端，设备应主动连接 |
|     [禁止]: 自建第二个设备连接 Runtime，禁止绕过仲裁直接抢占硬件驱动                  |
+---------------------------------------------------------------------------------------+
                                           | Wi-Fi LAN (UDP pairing + outbound WebSocket)
                                           v
+---------------------------------------------------------------------------------------+
|  2. 官方 SDK / Runtime 桥接层 (WatcheRobot Runtime):                                 |
|     - Host 广播 UDP 37021 并监听 LAN WebSocket；设备主动连接并在该连接上重连         |
|     - capabilities：motion, camera, expression.audio_follow.v1, expression, mcp_tools, |
|       inputs；按 CR-025 仅表示 roller 旋转，不含按钮或触摸输入；音频 unsupported  |
|     - 区分单设备所有权: 作为宿主侧唯一的物理机器人连接持有者，严格杜绝两套 daemon 抢占 |
|     - 严格请求响应与超时管理: 请求未收到设备 ACK 时必须超时报错，绝不伪造成功         |
+---------------------------------------------------------------------------------------+
                                           | In-Process 接口调用 / 异步回调
                                           v
+---------------------------------------------------------------------------------------+
|  3. Python 核心宿主与生活服务 (Desk Agent Host - 泰山派 RK3566):                      |
|     - 核心路由器 (Router): 统一处理 TClaw 外部工具信封 (RequestEnvelope)，幂等与过期校验|
|     - 生活服务 (LifeService): SQLite WAL 事务持久化、提醒/闹钟/便签、开机追赶补偿     |
|     - 可靠提醒投递: Scheduler 自动轮询到期 -> Outbox 消费 -> 机器人播报通道投递      |
|     - HA 连接器 (HAConnector): 实体状态快照、服务调用白名单、状态两阶段确认、事件订阅 |
|     - 相机租约与 Web 控制台: 局域网单帧 JPEG 租约、时效性检测、黑帧防冒充、LAN 鉴权   |
|     [禁止]: 代写底层固件驱动，禁止增加重复的外部云端语音会话                          |
+---------------------------------------------------------------------------------------+
                                           | 局域网 HTTP RPC (8765 端口 + Bearer Token)
                                           v
+---------------------------------------------------------------------------------------+
|  4. Windows Codex 伴侣进程 (Windows Companion - Windows 11 PC):                       |
|     - 驱动本地 codex app-server: 双向 JSON-RPC stdio 进程管理                         |
|     - 严格工作区别名白名单: 映射 epanda-desk-agent -> D:\ePanda\DeskAgent，拦截路径穿越|
|     - thread/turn 任务生命周期: 任务提交、状态轮询、取消与中断、结果汇总回传          |
|     - 敏感操作审批闭环: 捕获审批请求、绑定 task_id/turn_id、向泰山派暴露待决审批 RPC   |
|     [禁止]: 对外暴露原生 app-server 端口，禁止承诺接管人工聊天，禁止伪造代码已验证   |
+---------------------------------------------------------------------------------------+
```

---

## 4. 单设备连接所有权原则 (Single Connection Owner Invariant)

**架构铁律**：在整个系统运行环境中，**只允许存在一个物理设备网络连接持有者**。
1. **连接端点**：
   - 锁定的官方 Runtime 在 Host 侧发起 UDP 37021 配对广播，并监听 PairRequest 中指定的 LAN WebSocket 端口；设备应作为 WebSocket client 连接 Host。配对码是设备屏幕显示的短时码，不是 session token；
   - 固件当前的 TCP 37020 server 是 DeskAgent 自有协议端点，与官方 Runtime WebSocket 不是同一链路，不能声称互通。`RobotAdapter` 若连接 TCP 37020，只代表自有协议适配器。
2. **禁止自建多重 Daemon**：
   - 严禁在泰山派上同时启动多个独立的连接进程争抢 TCP 37020，所有运动控制、音频播报、表情显示与工具结果回送均经由统一的 `RobotAdapter` 实例下发；
   - 官方 Runtime 与自有 `RobotAdapter` 是不同传输路径；设备单连接 owner 规则应由产品选定并只启用一条受管链路，不得将 TCP 37020 包装为官方 Runtime 连接。

---

## 5. 多 Agent 协同推进节奏与关口依赖 (Roadmap & Gate Progression)

依据总管家制定的推进顺序，严格执行小步快跑、按关口晋升：

```
A0 (基线、接口与契约冻结 - 本次工作包)
                 ↓
A1 (真实固件/驱动/Tuya对话) ←→ A2 (官方 SDK/Runtime 接入)
                 ↓
【G1 关口验收】：真机 Tuya 云对话 + 官方 Runtime 连通 + 一个物理动作闭环
                 ↓
A3 (生活服务)    A4 (HA 家居接入)    A5 (Windows Codex 伴侣) [并行推进]
                 ↓
A6 (真实相机帧接入、手动云台仲裁、Web 管理台)
                 ↓
A7 (全链路端到端集成、故障注入、长稳恢复与验收交付)
```

**工作包派发纪律**：
- 每次只派发一个模块中的一个原子工作包；
- 严禁跨越 `AGENTS.md` 划定的独占文件目录；
- 在依赖组件尚未在真机接通前，严禁在文档中提前宣称端到端闭环完成。
