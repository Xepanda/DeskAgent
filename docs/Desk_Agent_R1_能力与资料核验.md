# Desk Agent R1 能力与资料核验

> 核验日期：2026-10-11。记录的是官方资料可支持的接口与边界；不代表本项目代码、设备或服务已完成集成。硬件和本地运行结果待补。

## 官方资料

|资料|可支持的结论|不能据此推出|
|---|---|---|
|[OpenAI Codex app-server](https://developers.openai.com/codex/app-server/)（当前重定向至 [Learn ChatGPT 文档](https://learn.chatgpt.com/docs/app-server)）|协议初始化、thread start/resume/read/list、turn start/steer/interrupt、item completed 与 turn completed 最终状态等协议生命周期；默认 stdio 接入思路|不能证明任意 Codex 桌面活跃聊天都可被另一进程读取/操纵；thread/read 不是事件订阅；不能据此直接暴露 TCP/WebSocket 到家庭网络，也不能保证本项目集成可用|
|[Home Assistant WebSocket API](https://developers.home-assistant.io/docs/api/websocket/)|WebSocket API 的认证/命令交互，包括 get_states、call_service、subscribe_events 与 state_changed 等|服务回执不能证明物理设备已改变；需读取状态或设备反馈；不能证明所有实体都提供可确认状态|
|[Home Assistant REST API](https://developers.home-assistant.io/docs/api/rest/)|HA REST API 的实体状态与服务调用接口类别|不能保证网络中断、集成延迟或服务成功后物理设备必然执行|
|[Home Assistant Voice Pipelines](https://developers.home-assistant.io/docs/voice/pipelines/)|HA 语音管线的阶段和集成架构，可用于评估语音前后端关系|不能证明机器人当前麦克风、固件或泰山派支持特定管线，也不能证明本地ASR/TTS资源够用|
|[Wyoming integration](https://www.home-assistant.io/integrations/wyoming)|Wyoming 可作为 HA 语音服务集成路径之一进行评估|不能证明现场已安装服务、音频质量达标或无需外部网络|
|[Tuya integration](https://www.home-assistant.io/integrations/tuya/)|HA 的 Tuya 集成路径及其云依赖/功能暴露限制需按具体设备核验|不能推断所有 Tuya 设备均本地可控、功能齐全或状态即时|
|[TuyaOpen TClaw](https://docs.tuyaopen.ai/tclaw) 与 [Tuya/TClaw](https://github.com/tuya/TClaw)|官方说明提及 device-cloud agent、cron/reminder/memory/skills，以及支持板的ASR、Tuya云ASR/TTS/RAG等能力，可作为独立服务端PoC方向|不能证明 WatcheRobot ESP32适配、泰山派ARM部署或当前账号权限；不因此要求TClaw写入机器人固件或作为HA控制入口|
|[Xiaomi Home HA integration](https://github.com/XiaoMi/ha_xiaomi_home)|官方 Xiaomi Home 集成仓库，可用于核对其安装、账号和设备支持说明|不能将此集成的行为套用到所有小米设备或其他 Xiaomi 集成；不能凭名称推断设备一定本地控制|
|[Xiaomi Miio integration](https://www.home-assistant.io/integrations/xiaomi_miio/)|另一种 HA 小米集成及其特定支持方式|不能把其局部/轮询行为推广到 Xiaomi Home 集成或所有型号|
|[WatcheRobot Python SDK README](https://raw.githubusercontent.com/orulink-ai/WatcheRobot_python_sdk/main/README.md)|官方中文名文件为 README.zh-CN.md；Python 3.10–3.12；Windows/macOS 首次 BLE Wi-Fi provisioning；配网后 Runtime 六位码设备配对；`watcherobot app run` 通过唯一 Daemon 注入 channels；有 behavior/official expressions/Creator works、PCM、touch/roller 等能力|不能据此宣称完整 Linux ARM 支持、泰山派部署通过、逐token实时TTS流播或任意自定义UI卡片/LVGL远控；作品PCM不等于TTS生成；SDK不含default application的ASR/LLM/TTS；Application不能绕过Daemon直连设备|
|[Python SDK audio.py](https://raw.githubusercontent.com/orulink-ai/WatcheRobot_python_sdk/main/src/watcherobot/audio.py) 与 [robot.py](https://raw.githubusercontent.com/orulink-ai/WatcheRobot_python_sdk/main/src/watcherobot/robot.py)|`AudioDomain.play(sound_id)`、`play_file(path)`、`play_pcm(data, sample_rate_hz=24000, channels=1, sample_width_bytes=2)`、`stop()`；PCM stream 要求 `audio.stream` capability，流 begin 携带 metadata/SHA256，结束状态有 COMPLETED/FAILED/CANCELLED；支持先合成短句PCM再播|不能推出逐token实时边合成边播；4MiB为PCM输入上限而非建议缓冲；动态音频格式、播放时钟、stop时延和声学体验需目标设备PoC|
|[Python SDK inputs.py](https://raw.githubusercontent.com/orulink-ai/WatcheRobot_python_sdk/main/src/watcherobot/inputs.py) 与 [vision.py](https://raw.githubusercontent.com/orulink-ai/WatcheRobot_python_sdk/main/src/watcherobot/vision.py)|有 backtouch/screentouch(x,y,tap)/roller(delta,rotate) 输入队列；face tracking 为设备侧能力，受 compatible device 和模型条件限制；预览416x416是预览尺寸|不能推断任意UI卡片/文字渲染能力；416x416不是屏幕分辨率；输入队列可能丢旧事件并暴露丢弃计数；不能把Python device-side tracking与嵌入式JPEG路径混为一谈|
|[Python SDK robot.py](https://raw.githubusercontent.com/orulink-ai/WatcheRobot_python_sdk/main/src/watcherobot/robot.py)|`ExpressionRuntimeDomain` 有受限 gaze/eyes/blink/style/tag/custom vector参数；`expression.audio_follow.v1`可让设备按成功播放PCM的播放时钟驱动程序化嘴形；`MotionDomain.move_to(pan_deg, tilt_deg, duration_ms, profile)`与stop命令模型|能力受固件白名单与设备capability限制；custom vector内部坐标不是屏幕分辨率；播放时钟嘴形不等于音素级唇形或硬件RTC；pan/tilt命令不证明实际机械行程/限位或动作结束|
|[WatcheRobot 嵌入式 SDK README](https://raw.githubusercontent.com/orulink-ai/WatcheRobot-public/main/firmware/sdk/README_zh.md)|开发预览 SDK、自有 app_main、IDF 6.0.2、工程Flash配置16MB；body/head职责、JPEG回调复制、音频阻塞隔离、LVGL锁、STM32断线 stop 等注意事项|不能当作生产成熟度或完整整机稳定性证明；运动请求接收/提交不代表动作完成；具体用户板型和安全能力仍需验证|
|[WatcheRobot 嵌入式 SDK VALIDATION](https://raw.githubusercontent.com/orulink-ai/WatcheRobot-public/main/firmware/sdk/VALIDATION.md)|构建、host tests 和部分 JPEG/PCM 显示、patched STM32 命令链 HIL 的验证范围|不能推出完整可靠性、机械行程/安全范围、音质、长稳或用户整机验收通过|

## 本项目当前证据状态

|主题|已有输入|当前状态|下一项可核验材料|
|---|---|---|---|
|机器人算力与存储|原始设计说明提到 ESP32-S3、8MB PSRAM、实体 Flash 32MB、工程配置 16MB|未做完整板上验证；容量和分区差异仍待确认|芯片读数、SDK配置、分区表、堆/PSRAM高水位日志|
|运动/灯光/触摸|说明提到 STM32 负责相关功能|未确认总线协议、紧停与限位设计|原理图、引脚/总线文档、驱动代码、断电和紧停实测|
|图像|说明提到 HX6538 JPEG|未确认传感器、帧率、图像链路、隐私开关和边缘处理方案|模块资料、固件接口、拍摄/缓存/传输流程说明|
|显示|具体屏幕尺寸、分辨率、方向、触摸能力未知|UI只能保留布局建议|屏幕型号、FPC/接口、驱动和可视区域尺寸|
|音频|麦克风数量与布置、DSP、扬声器功率和AEC未知|不能承诺无唤醒或远场能力|原理图、麦克风拓扑、codec/驱动、现场噪声录音评估|
|Codex会话互通|官方 app-server 协议说明已提供|本机 Companion 集成与桌面活跃会话互通尚未PoC|独立进程验证 thread 选择、事件流、审批、取消、重启重连|
|HA与设备|用户描述家中小米、Tuya 已集成HA，泰山派跑HA|集成名称/版本、安装类型、设备状态反馈、网络拓扑待现场确认|通过首配向导确认环境与实体能力|
|RTC与断电|未知|不能承诺断电后闹钟准点|RTC器件/电池资料、掉电时钟漂移和重新校时试验|
|固件路线|现有固件+Python Runtime、TuyaOpen/Watche SDK 两条候选|逻辑架构先确定 Adapter 边界，具体实现待Runtime动态音频/UI/Linux ARM PoC决定；不并行实施|现固件接口清单、SDK版本和板级支持、OTA/显示/音频/运动PoC|
|Python Runtime部署|官方 Python SDK 的配对、Application和通道模型已核对|仅为待验证候选，泰山派上的 Python/Daemon/设备链路及动态音频/UI能力未实测|在目标泰山派验证 Python 版本、BLE provisioning、六位码配对、channels、音频收发和重连|

## WatcheRobot 官方 SDK 要点

- Python SDK 文件正式中文名是 `README.zh-CN.md`。README 提供 `watcherobot app run` Application 生命周期；唯一 Daemon 负责设备连接并把 channels 注入 Application。DeskAgent Application 不应绕过 SDK 自行连设备 WebSocket。
- `AudioDomain` 提供有限 PCM 播放和 stop API，设备需开放 `audio.stream`。建议由外部TTS先合成一条短句的 24 kHz mono s16le PCM 再播放，产品建议每条5–10秒；不把4MiB传输上限当作正常buffer。未证明逐token边合成边播。播放状态需接真实完成/取消状态。
- ExpressionRuntime 有白名单内眼神、眼睑、眨眼、style/tag等表达；设备可能支持 `expression.audio_follow.v1` 以成功播放的 RTC PCM 驱动程序化嘴形，需目标板验证。失败时用普通 speaking 动画，不承诺音素级同步。
- SDK 提供 backtouch/screentouch/roller 输入事件。可用触摸/滚轮实现有限快捷回应，但不能据此宣称支持任意自定义UI卡片。screen/face preview 的内部尺寸不能推断屏幕分辨率。
- `vision.py` 描述 device-side face tracking，需兼容设备和模型；相机首版默认关闭。Face tracking 只可选用于在场/注视交互，不用于身份认证或情绪推断。
- Python SDK 不拥有官方 default application 的 ASR/LLM/TTS 业务逻辑。DeskAgent 需自建语音链路或选服务供应商，对隐私、延迟、离线行为单独验收。
- 官方资料没有完整 Linux ARM 支持矩阵；泰山派常驻 Runtime、Daemon、配对和重连需要目标环境PoC。Windows-only Runtime 不满足 PC 关机后的陪伴要求。
- Robot设备配对六位码来自SDK Runtime流程；Edge-PC配对的短期PIN/双向确认是本产品新链路建议。官方公开资料未证明Robot原生媒体通道有TLS/等效加密，生产安全须作为PoC和发布门槛。
- 嵌入式 SDK 是开发预览，IDF 6.0.2，工程Flash配置16MB。其验证文档列出构建/host test 和部分 HIL；不能替代用户整机的稳定性、机械行程、音质和长时间运行验收。

## 设计引用的事实与产品承诺边界

- Codex 事件应以 app-server 提供的实际 thread/turn/item 生命周期为依据。Bridge 的 task id 是产品侧关联键，不能替代 thread_id、turn_id；需实现幂等、持久化和重连核对。
- 审批提示只由实际等待审批事件触发，且必须绑定具体请求；过期/重启后的待审批状态不得自动同意。
- HA 服务调用成功只能表述为服务请求成功提交；设备状态已改变需要可观察状态或反馈证据。对无反馈实体，产品文案只能说“已发送”或“当前无法确认”。
- Tuya 与小米的本地/云端能力依赖具体集成与设备。用户现有设备已纳入 HA 是规划前提，仍需只读核实所用集成与实体能力。
- PoC前须记录SDK commit、Robot/STM32/Himax固件版本与Codex app-server实际schema；SDK常数或协议上限只说明接口，不代表本项目运行容量。
- 机器人表情、云台、安全停机、UI适配、语音误唤醒、连续对话时长、内存余量、72小时稳定性、闹钟断电恢复和现场延迟均没有本项目实测证据，属于建议验收目标或待验证项。
- 本核验未访问用户设备或HA实例，未读取账户/凭据，未安装依赖、创建云端页面或修改原始 DOCX。
