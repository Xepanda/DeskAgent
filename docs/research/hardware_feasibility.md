# Desk Agent R1 硬件与 SDK 技术可行性

> 核验日期：2026-10-11。本文只做资料和源码核验，没有修改或烧录固件、连接机器人/泰山派/HA，也没有运行硬件 PoC。所有“能做”均为接口或源代码能力，不代表目标整机验收通过。目标设备 PCB/硬件修订和配套固件版本尚未从设备本身确认。

## 决策摘要

|用户能力|现有 Python Runtime/SDK 路线|嵌入式 WatcheRobot SDK 路线|首版建议与门槛|
|---|---|---|---|
|麦克风按住说话|**接口已具备**：设备发 Opus，Application 可 `microphone.open_pcm()` 得 16 kHz mono s16le PCM，或 `record_pcm()` 保存 PCM WAV。Runtime 持有设备连接；SDK 的 PyAV 依赖负责 Opus 解码。|**接口已具备**：`watche_hw_audio_read()`/`write()` 对 16/24 kHz mono s16le 阻塞读写；需应用任务管理，无唤醒词/空闲策略。|先做单一音频 owner、PTT、录音回显；在泰山派先验证 Python/PyAV ARM 环境，失败则走嵌入式采集并实现编码/传输。不要将板载麦克风当成可供泰山派直接读取的 Linux 麦克风设备。|
|语音播报/表情|**接口已具备但受 capability 限制**：SDK 支持 24 kHz mono s16le PCM 播放/stop、表达式、设备侧 RTC PCM 嘴形跟随。|**音频接口已具备**；表情需 LCD/LVGL/UI 层自建，音频阻塞调用必须与显示/网络隔离。|Python 路线先合成短句后播，验证真实 `audio.stream`、stop 响应和完成态；设备 SDK没有ASR/LLM/TTS产品逻辑。|
|屏幕表情/状态|SDK 有表达式/行为/动画控制、触摸/滚轮事件；公开接口不能据此认定通用 UI 或可任意更新中文字卡。|**可绘制 UI**：嵌入式 SDK 提供 LVGL display/touch handles 与显示锁。|R1 实施前先盘点当前 firmware 能力。状态 UI 由唯一 UI owner；若需中文状态文字而 Python capability 不支持，扩展固件 display adapter。|
|云台/机头运动|**控制 API 存在**：`motion.move_to(pan_deg, tilt_deg, duration_ms, profile)`、stop/job；限位与真实机械含义依设备 capability/固件。|**控制及反馈 API 存在**：body move/stop、position query、DONE/FAULT事件；接口声明 X=0–180°、Y=100–140°，仍不能当作该用户设备已实测安全行程。|先盘点当前机头电机是否由 STM32 body 协议控制，建立动作词典与安全 gate。PoC测限位、实际完成事件、停止时间、断链/复位状态；未通过不得开放自动动作。|
|相机单帧/局域网查看|`camera.capture()` 返回 JPEG 单帧；可循环请求最新帧并通过本地服务推送。它不是持续视频API。设备侧 `face_tracking.open_preview()` 确实可收JPEG帧，但绑定 face-tracking 能力/服务，不是通用相机预览。|`watche_hw_camera_capture()` 和 `watche_hw_camera_stream(fps, callback)` 输出 JPEG；回调只有短暂借用帧数据，需立即复制。HAL说明 stream 是 SSCMA paced one-shot `INVOKE` 循环，目标帧率1–30且受 HX6538负载影响。|先比较“Python串行重复 capture 的低帧率LAN预览”和“嵌入式JPEG callback + LAN帧传输”。Python路径改动少，目标 Runtime 支持 Linux ARM 是 gate；嵌入式更接近连续采集，但需要新增受鉴权LAN帧服务。两者都必须实测帧率/端到端延迟。JPEG快照或重复JPEG不可宣传为视频流验收完成。|
|提醒/备忘|放泰山派/Edge Hub 的本地数据库和调度器，与机器人SDK无关。|同左；MCU不适合作为唯一持久化/日历责任层，除非另行证明RTC、掉电和数据存储。|Edge Hub 管持久化和提醒；Robot 只作通知输出端。验证PC断电、Edge重启、机器人失联、时区和幂等恢复。|

**路线结论：** 语音采集、短语播报、表情和运动已有公开接口，不需要先从零重写整机协议。目标工程的主要技术风险是：当前 WatcheRobot 固件是否声明相应 capability；Python Runtime/Daemon 是否能在泰山派长期运行；TClaw/ TuyaOpen 工程与 WatcheRobot SDK 的平台版本、LVGL主版本冲突；相机只提供JPEG帧时如何在局域网做低延迟、鉴权且可立即关闭的预览。

## 硬件清单与证据等级

|部件/型号|资料中观察到的规格或接口|证据与限制|
|---|---|---|
|主控|Espressif ESP32-S3；8 MB Octal PSRAM；实体 Flash 报告 32 MB，但示例固件按 16 MB 配置|用户提供的 V1.0 DOCX「硬件能力与已知限制」明确列出。官方嵌入式SDK README 目标也是 ESP32-S3、16 MB Flash和octal PSRAM；其 VALIDATION 记载一台板的实测是 ESP32-S3 rev 0.2、8 MB PSRAM、物理Flash 32MB。该记录不是用户设备的身份确认。分区表不能按物理容量猜测或覆盖。|
|身体控制|STM32 负责运动、灯光、身体触摸；SPI/UART链路的具体线路、型号、版本未在用户材料中给出|V1.0 DOCX。官方公开SDK为 `watche_hw_body`，声明 X/Y servo角度、stop、position request及DONE/FAULT事件；配套 STM32/Himax固件组合未被官方完整认证。|
|相机/视觉|HX6538，PTL/SSCMA 协议经 ESP32 交互；图像 API 是 JPEG Capture/Stream/Stop|V1.0 DOCX 与官方 HAL 注释。传感器/镜头具体型号、FOV、原始分辨率、PTL版本、帧率和配套 Himax firmware 哈希未确认。公开SDK的 preview验收只确认一帧 JPEG有效，连续取流/重启未验收。|
|显示/触摸|LCD、LVGL与Touch驱动；官方 head例程使用 SPD2010 LCD/触摸驱动|V1.0 DOCX与官方SDK验证记录。目标面板尺寸、分辨率、方向、触摸映射和中文字体未由目标板读数确认。嵌入式SDK自带LVGL接口；Python能力不等于任意文字UI渲染。|
|麦克风/扬声器|板载麦克风/扬声器；PCM采集播放|V1.0 DOCX。Python设备协议将麦克风媒体定义为16kHz mono Opus每包60ms，SDK解码为PCM；嵌入式音频HAL支持16/24kHz mono s16le。麦克风数量、拾音距离、AEC/NS、codec、扬声器功率和音质未确认。|
|供电/RTC/隐私指示|资料无可核实型号或电路|未确认供电稳定性、RTC/备份电池、物理麦克风静音、摄像头隐私开关/LED及失电安全行为。不能把软件状态图标当硬件隐私指示。|
|云台|V1.0列“STM32身体控制/运动”；SDK运动 API可控制角度与停止|真实机头机械行程、是否两个舵机/云台实现、齿轮/限位方式、是否有实际位置反馈均待确认。源码角度范围是接口事实，不等同设备验证。|

### 用户提供硬件仓库核验

检查了 [Xepanda/wiki](https://github.com/Xepanda/wiki) 的公开仓库树（`master`，commit [`7e16002af8b30d639c1be8d5711984ae404379c9`](https://github.com/Xepanda/wiki/commit/7e16002af8b30d639c1be8d5711984ae404379c9)，核验日期2026-10-11）。没有 WatcheRobot/SenseCAP Watcher 专属硬件文档；相关硬件条目是泰山派 RK3566 网关文档。因此本报告中的 WatcheRobot 型号/内存/flash输入来自给定 DOCX与 WatcheRobot 官方SDK资料，**不是从该 Wiki 得到的设备实测结果**。目标设备 exact SKU、板修订和固件版本需在PoC清单中补录。

## Python SDK 与嵌入式 SDK 的职责边界

核验的公开源码版本：

- [WatcheRobot-public](https://github.com/orulink-ai/WatcheRobot-public/tree/646a977bfd76a8516ae505fda2331b81e1049b87)，`main` commit `646a977bfd76a8516ae505fda2331b81e1049b87`（提交时间2026-10-07）。SDK自称 Development Preview；README目标 ESP-IDF 6.0.2，工程 Flash配置16MB。
- [WatcheRobot_python_sdk](https://github.com/orulink-ai/WatcheRobot_python_sdk/tree/8bc13967a06ef073459be47e930ff43ef37ee8fb)，`main` commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`（提交时间2026-10-10，release 0.1.11）。README要求 Python 3.10–3.12；首次BLE配网明确使用 Windows/macOS。其公开页面没有完整 Linux ARM Runtime/Daemon支持矩阵。
- Tuya/TClaw 的官方文档当前将 ESP32-S3列为支持目标；[ESP32-S3 quick start](https://docs.tuyaopen.ai/docs/tclaw/ducky-quick-start-ESP32S3)给出的例板规格为8MB PSRAM/16MB Flash。TClaw处于active development。此板支持声明不能证明当前 Watcher 板组合已移植成功。

|功能/API|Python Runtime/SDK|嵌入式 WatcheRobot SDK|产品适配层需要承担|
|---|---|---|---|
|麦克风采集|`robot.microphone.open_pcm()`返回解码PCM；`record_pcm()`录成PCM WAV。设备端Opus解码由SDK的PyAV (`av`)完成；60ms/包、16kHz mono。队列可能出现 dropped_frames/decode_failures，须读取统计。|`watche_hw_audio_init(16000或24000, volume)`、阻塞 `read/write`、volume、close；一个owner task；没有 wake-word/idle策略。|PTT生命周期、编码/传输、超时/丢包与buffer、半双工仲裁、隐私状态、ASR/TTS服务调用及取消/打断。|
|播报|`play_pcm`仅允许24kHz mono s16le；需设备声明 `audio.stream`。可 stop并读取 Job完成状态。SDK支持一次整段PCM流；不能据此认定逐token边合成边播。|阻塞 PCM输出；应用需要管理音频线程和codec生命周期。|合成后限长分段；确保录放不同时抢麦克风/codec；急停、告警和用户交互优先级。|
|摄像头|`robot.camera.capture(width,height,quality,timeout)`为单帧JPEG，默认timeout 5秒，且SDK内有camera lock避免并发capture。返回帧带时间戳/序号。重复调用形成拉取快照循环在接口上可行，但实际吞吐、节流策略、重复捕获耗时及过期画面处理未给出。`face_tracking.open_preview(width,height,frame_stride,queue_size)`是受能力门控的face tracking preview，队列有界，不能当作未绑定模型的泛用video feed。|`watche_hw_camera_capture(cb)`单帧；`watche_hw_camera_stream(fps,cb)`1–30 fps目标；JPEG仅在callback借用有效，需复制后异步发网。HAL实现为有间隔的SSCMA INVOKE拍摄循环。|独占Camera owner、帧复制/背压/限速、LAN鉴权及只允许本地网络访问、浏览器显示、停止信号及时性、灯/状态显示、无录像/无远程路由。|
|屏幕与触摸|有触摸/滚轮输入、表达式和动画；Python SDK capability不保证任意文字UI接口。相机尺寸/自定义向量画布不能推出面板分辨率。|`watche_hw_display_init()`、LVGL/display/touch handle和锁。|单一UI线程/owner、线程安全消息队列、UI避免在LVGL锁内录音/关屏或等待网络。若现有SDK缺中文文字或卡片，再扩展display协议。|
|云台|`move_to(pan_deg,tilt_deg,duration_ms,profile)`生成Job，motion域接受能力需核验。|`watche_hw_body_move(x,y,duration,&seq)`，stop、位置请求、运动事件；submit不是完成；SDK已实现命令sequence匹配和旧响应过滤。|自然语言转有限动作词典，single motion arbiter；安全限位、完成/故障反馈、过期命令丢弃、Camera viewing期间禁止自动跟随。|
|设备运行与配对|唯一 Runtime/Daemon 拥有设备连接；Application必须经 `watcherobot app run` 获得注入channels，不应自建第二条直连。文档明示Windows/macOS配网；Linux ARM目标需PoC。|自己拥有 `app_main`，不依赖Python Runtime、Daemon、Server；可运行最小设备程序。|选择一条固件/设备连接所有权路线。禁止 Python Daemon和自建嵌入式应用同时抢同一套硬件/协议。|

### 麦克风 Runtime 依赖风险

Python解 Opus 使用 `av.CodecContext.create("opus", "r")` 和 PyAV；SDK的 `pyproject.toml`要求 `av>=16,<17`，PyAV wheel包含相应FFmpeg媒体库。PyPI `av 16.0.1`有CPython 3.11 Linux ARM64 manylinux_2_28与musllinux 1.2 wheels，见[官方PyPI文件列表](https://pypi.org/project/av/16.0.1/)。这消除了“必定要在泰山派本机编译FFmpeg”的假设，但**不等于泰山派可直接安装**：操作系统是glibc还是musl、最低glibc、CPU ABI、Python版本、pip wheel tag以及SDK依赖整体都要满足。Wheel约38–40 MB，也需计入下载/部署磁盘，运行内存与实时解码余量需另测。公开 SDK 未声明对 Linux ARM 的整套 Daemon/设备链路支持。

## LAN 摄像头实时查看方案比较

“实时查看”产品定义应该是用户按下查看后持续收到新画面、可观察到采集状态，关闭后摄像头停止采集/传输；**JPEG帧可以组成刷新画面，但单次JPEG截图不等于视频，现有资料也没有声音/录像/回放能力证据。**

|路线|实现方式|优势|门槛/风险|建议排序|
|---|---|---|---|---|
|A. Python repeated capture|Local hub每次调用 `camera.capture()`拿JPEG并按浏览器请求刷新或经WebSocket/MJPEG向局域网页面送最新帧；单帧循环严格串行、旧帧丢弃。|改动最小；有请求边界可做清晰的开始/停止和懒采样；不需固件新增推流。|Capture调用默认timeout 5s；相机锁等待前一capture；接口没有给定最小间隔/最大帧率或节流承诺。浏览器要拿到的是Hub转发出的帧，不应从浏览器直连机器人内部控制端口。须实测P95帧间隔、画面年龄和停止延迟。还依赖泰山派上的Runtime支持。|首个PoC优先。若达到用户约定的最低可接受刷新率及延迟，则作为R1 LAN预览路径。先不要宣称连续视频规格。|
|B. Python face-tracking preview|使用能力门控的 `face_tracking.open_preview()`读JPEG frame，走SDK事件流。|SDK已有有界latest-frame queue与停止/清理路径。|功能实际绑定tracking/model capability，可能同时驱动运动；与LAN纯查看职责不匹配，无法假设相机可脱离face tracking工作。|仅当设备firmware证明preview可不启tracking/不抢云台时考虑；默认不选。|
|C. 嵌入式 JPEG stream + LAN帧服务|固件单一camera service调 `watche_hw_camera_stream(fps,callback)`；callback快速复制到有界latest-frame buffer；独立Network task用本地认证HTTP MJPEG或WebSocket JPEG帧发给单个查看客户端。|连续采集流程有官方API；网络/用户会话不占用Himax callback；可在设备侧更明确地管隐私状态。|需要接入/移植固件API、Himax PTL镜像配套、TCP/Wi-Fi发送、认证和拥塞控制；官方SDK自己不提供LAN viewer/视频传输协议。stream真实fps受Himax AI负载影响。|A不达需求或Runtime不支持ARM时的候选。必须先以单客户端、限帧、无存储原型测试。|

浏览器第一期应明确走家庭LAN；不创建WAN relay、隧道或外部转发。界面关闭/路由切换/客户端掉线时服务端也要stop采集。图片只保留当前最新帧的内存槽，除非另有用户授权和规格变更，不写磁盘/数据库、不加录制接口。相机过程中机头让位给人工查看，自动跟随关闭；手动云台控制需单独经安全仲裁。鉴权token不得写进URL query并被日志记录。

## 固件整合、资源并发和阻塞点

### 已知平台冲突

用户V1.0方案给出先在 TuyaOpen ESP-IDF 5.4 基线做 WatcheRobot SDK移植PoC；WatcheRobot Embedded SDK当前main以 ESP-IDF 6.0.2为目标；用户材料还指出 TClaw/Tuya图形方案的 LVGL 9 与 Watche显示SDK/LVGL 8可能冲突。不能把整个 TuyaOpen升级到6.0.2或复制合并所有BSP目录视为默认方案。最小工程策略是独立新建开发项目，引入Watche组件依赖，先跑head/body单项；验证后再决定IDF基线迁移和LVGL adapter。当前任务没有实机板资料，无法决定移植对接引脚/flash配置。

### 并发资源和所有权

- **内部RAM/PSRAM：** TClaw/AI上下文、TLS网络、camera JPEG帧缓存、LVGL draw buffers、音频DMA/codec buffer以及app队列会竞争。8MB PSRAM不是可用heap预算；需要内部RAM和PSRAM分别测低水位、分配失败与碎片。由于明确不录制，视频只保留有界的最新JPEG（发送中buffer另计），严禁无界frame queue。
- **CPU/总线：** JPEG capture/decode与LVGL刷新、Opus decode、音频编码/网络/TLS并发；Camera SSCMA/HX6538由UART/SPI链路交互，GPIO/IO expander/共享SPI初始化顺序重要。相机回调内不能做网络、UI、AI或内存长时间复制；应先用预分配buffer迅速复制并投递。
- **音频占用：** Python运行时设备端麦克风在语音会话中连续出Opus；嵌入式API阻塞read/write且单task owner、共享bus/codec独占。PTT采集与扬声器播放要半双工仲裁；播报不能悄悄盖住用户的取消/打断输入。
- **屏幕并发：** Embedded SDK拥有LVGL flush；所有操作用锁，不能在持锁状态等待相机、音频或网络，关闭display也不得在回调中做。Python以业务事件更新UI，避免多个功能直接抢动画owner。
- **相机与运动：** LAN查看时Camera service独占图像采集；自动跟随/陪伴点头不应同时控制云台。相机关闭必须stop并读回状态；动作命令只允许单一Motion service发出，掉线不重放旧动作。
- **固件空间：** 设备物理Flash报告32MB，但可见SDK示例工程16MB配置；不同partition table时app-only flash可能无法启动。须先核对当前运行固件/分区并备份和验证恢复链路。

### 主要阻塞与未知

1. **硬件基线不完整：** 用户Wiki没有WatcheRobot条目；DOCX未列PCB/SKU修订、STM32/Himax镜像版本与hash、schema和引脚表。任何此类事实不能靠相似Seeed/SenseCAP产品页代替。
2. **配套固件能力：** Python路径需读取目标机器人实时capabilities并确认microphone、audio.stream、camera.capture、motion与表达式；嵌入式路径需确认STM32 capability与PTL Himax pairing。官方预览SDK明确未声称全量配套固件验收。
3. **常驻运行：** Python SDK需要3.10–3.12，BLE首次配网用Windows/macOS；泰山派部署形态、CPU arch、Python和Linux ARM Daemon可用性无实测。PyAV wheel存在不代表整个SDK能长期在目标机运行。
4. **相机质量：** 目标FPS、单帧capture latency、分辨率/画质、照明、掉帧/卡死恢复、Wi-Fi LAN端到端延迟和预览停止时延无本项目数据；HAL报告 stream 只是 paced capture loop。
5. **运动安全：** 角度表、position query可用性、STALL/断链后的STM32自治停机、物理机械限位与紧停未在此设备上实测。不得据动作接口存在就开放云台控制。
6. **语音与显示质量：** 目标拾音距离/噪声、回声消除、短句可懂度、扬声器响度、中文字库、屏幕可视区域触摸坐标未验证。
7. **多负载运行：** 官方验证记录是部分单项/短时硬件smoke与开发测试，不是同时开启TClaw、TLS、音频、屏幕、相机后长稳/内存测试。不得把理论内存或单模块ESP_OK当作并发验收。

## 可安排的最小PoC与验收顺序

以下仅为开发门槛，具体刷新率/延迟由用户产品需求锁定后填入阈值；本报告不代填未经同意的数字。

|阶段|任务|通过条件与证据|
|---|---|---|
|0. 基线记录|识别机器人 exact SKU/PCB rev；读ESP32 flash/PSRAM、分区表、ESP-IDF与firmware build ID；STM32版本/capabilities；Himax版本/hash；屏幕/麦克风/codec/供电资料。做官方恢复镜像并验证恢复流程。|资产记录表字段齐全；确认flash真实partition映射；至少一次可回退演练。未知字段留unknown，不刷写前推断。|
|1A. Python Runtime gate|在不改固件状态下，为泰山派CPU/OS/Python版本确认SDK与Daemon Linux ARM支持；静态确认`av`对应wheel tag/FFmpeg解码可加载。然后经授权硬件连接验证单次麦克风Opus→PCM、按住/松开、播放/stop状态。|有确定的Python ABI/OS wheel与守护进程支持结论；录音无decode failures/dropped frames；PCM采样和时间符合16k/mono；实际扬声器可听且stop可测。不要在当前只读调研环境执行。|
|1B. 嵌入式音频备选|独立开发工程只跑PCM 16k capture/play，随后加LVGL状态显示，不并入TClaw业务。|音频owner没有并发调用；记录内存低水位、录放质量、关闭/重开、连续运行与失败原因。|
|2. Camera route A/B比较|先用Python serial `capture()` loop，记录每帧序号、设备时间戳、开始/返回时间和JPEG分辨率，空闲场景、display activity、其他设备负载各采一轮；经LAN只向单浏览器转发最新JPEG。A不达用户定义门槛再做C：嵌入式stream callback+有界buffer+单会话LAN传输。|记录实际fps、P50/P95帧间隔与capture latency、frame age、掉帧、峰值内存和close-to-stop延迟；证明未写磁盘、WAN不可达、客户端断开/页面关闭后firmware `streaming=false`且不再采帧。JPEG截图本身不足以通过实时查看验收。|
|3. Display + motion|屏幕显示状态/中文、触摸；机头在清场下跑低速、逐步、小范围人工确认测试；测stop、DONE/FAULT/实际position及断开STM32后的安全状态。|所有动作均在设备端安全范围以内；以device反馈/视频观察分开记录命令提交与物理完成。任何limit/stop/断链项失败则禁用云台动作。|
|4. 并发与长期|选定路线后并发开语音PTT/短播、屏幕刷新、LAN相机查看、Wi-Fi/TLS业务；再长时间运行，逐项注入Wi-Fi、客户端、PC、Himax/STM32断开并恢复。|内部RAM/PSRAM high-water、帧队列长度、掉音/掉帧、温度/重启、设备连接、stop时间有记录；无无界队列、无相机自动落盘、重连不回放旧动作。长稳时长按项目G6门槛执行。|

推荐工作切片：

1. **建硬件基线台账**（固件工程/硬件）：把板、MCU、Himax、LCD、音频、flash/partition、idf版本和可恢复烧录证据冻结。
2. **做SDK分叉裁决**（固件工程）：先在独立IDF项目跑Watche head/body，再评估移植到TuyaOpen/TClaw IDF 5.4；输出LVGL8/9与driver依赖冲突清单。不要先把完整SDK目录复制进TClaw。
3. **交付语音竖切**（固件+服务）：优先PoC Python Runtime ARM/Opus decode；若Daemon不支持，嵌入式读PCM后经受控LAN协议交给Edge ASR；ASR/TTS供应商与音频发云授权仍需产品决策。
4. **实现LAN JPEG查看**（设备服务+Web）：先做Python重复单帧方案测吞吐；只有无法达到需求才实现嵌入式采集服务。网络层单独认证、限帧、只发内存中的最新帧；严格实现用户显式开启和关闭。
5. **表情与动作**（固件/UI）：状态机唯一决定表情/声音；运动放在独立安全仲裁器。运动安全先于动画丰富度。
6. **接回TClaw/HA/提醒**（平台集成）：本地业务走Edge Hub，HA作为设备入口；在固件/设备连接稳定后再并发接入，避免把摄像头预览和Agent/TLS引起的资源回归混在一起。

## 可选新能力候选

|候选|公开SDK已支持|固件/开发门槛|
|---|---|---|
|设备侧人脸跟踪|Python SDK的`face_tracking.open_preview()`可读取JPEG预览与同帧遥测，并提供停止策略；vision API可查询设备模型/状态。|需要兼容固件声明相应tracking能力且有可用模型；接口本身不证明用户设备已安装模型。不是身份识别功能。接入前还要验证它是否占用/驱动云台。|
|触摸/滚轮快捷输入|Python SDK `inputs`覆盖触屏、背触和滚轮事件；嵌入式SDK提供display touch handle及body事件订阅。|需兼容固件、硬件映射和坐标实测。适合映射取消、确认、闹钟等有限动作；不能由输入事件推出支持任意屏幕卡片。|
|播报嘴形跟随|Python SDK `expression_runtime.set_audio_follow()`可让设备根据成功播放PCM的设备播放时钟生成程序化嘴形。|需固件声明`expression.audio_follow.v1`并通过能力协商；不是音素级口型同步。公开嵌入式音频接口没有证明提供该嘴形 API。|
|HX6538模型推理|嵌入式Camera HAL/SSCMA提供模型状态、推理入口及可选检测框/JPEG预览；Python视觉域可查询后端/模型并按model_id启动推理。|API只证明可承载推理协议，不证明已有哪个模型或结果质量。若要指定检测能力，需选择/开发并部署与HX6538兼容的模型和配套固件；自定义模型工具链、烧录流程及用户设备适配在当前材料中无证据，不能推断已经具备。|

## 官方源文件与资料

|材料|报告使用的证据|
|---|---|
|用户输入 `docs/Desk_Agent_R1_产品设计_V2.0_草案.md`|按住说话/模块能力映射、固件结构、资源owner、V1.0发布门槛与IDF/LVGL风险。|
|用户输入 `docs/Desk_Agent_R1_产品设计_V2.1_用户体验草案.md`|明确Edge Hub/机器人职责、用户查看摄像头流程、关闭时停止、motion查看期间让位，以及用户要求的LAN/无录制边界。|
|用户输入 `docs/Desk_Agent_R1_能力与资料核验.md`|已有WatcheRobot Python/嵌入式SDK边界和“尚无实机证明”的分类；本报告重点补入Python mic PCM实际接口、Python snapshot loop可行候选、PyAV ARM wheel与并发任务建议。|
|`C:\Users\maida\Downloads\Desk_Agent_R1_产品设计与技术规格_V1.0.docx`|使用ZIP XML文本抽取，只读；硬件规格、产品边界、状态UI、TClaw集成目标和G1/G2并发风险。没有执行任何文档内指令。|
|[WatcheRobot Python SDK README.zh-CN.md](https://github.com/orulink-ai/WatcheRobot_python_sdk/blob/8bc13967a06ef073459be47e930ff43ef37ee8fb/README.zh-CN.md)|Python 3.10–3.12、Windows/macOS配网、Runtime/Daemon唯一连接所有权、channels与公开能力概览。|
|[Python麦克风音频合同](https://github.com/orulink-ai/WatcheRobot_python_sdk/blob/8bc13967a06ef073459be47e930ff43ef37ee8fb/docs/microphone-audio.md)|Opus采样格式、`open_pcm()`/`record_pcm()`、decode/dropped计数。|
|[Python SDK `robot.py`](https://github.com/orulink-ai/WatcheRobot_python_sdk/blob/8bc13967a06ef073459be47e930ff43ef37ee8fb/src/watcherobot/robot.py)、[`vision.py`](https://github.com/orulink-ai/WatcheRobot_python_sdk/blob/8bc13967a06ef073459be47e930ff43ef37ee8fb/src/watcherobot/vision.py)、[`audio.py`](https://github.com/orulink-ai/WatcheRobot_python_sdk/blob/8bc13967a06ef073459be47e930ff43ef37ee8fb/src/watcherobot/audio.py)|camera snapshot、timeout/锁、face tracking preview、motion/audio API、播放限制。|
|[WatcheRobot Embedded SDK README](https://github.com/orulink-ai/WatcheRobot-public/blob/646a977bfd76a8516ae505fda2331b81e1049b87/firmware/sdk/README_zh.md) 与 [VALIDATION](https://github.com/orulink-ai/WatcheRobot-public/blob/646a977bfd76a8516ae505fda2331b81e1049b87/firmware/sdk/VALIDATION.md)|ESP-IDF/Flash目标、模块、flash/恢复警告、部分HIL范围及尚未验收事项。|
|[嵌入式 Camera HAL](https://github.com/orulink-ai/WatcheRobot-public/blob/646a977bfd76a8516ae505fda2331b81e1049b87/firmware/sdk/components/hal/hal_camera/include/hal_camera.h)、[`watche_hw_camera.h`](https://github.com/orulink-ai/WatcheRobot-public/blob/646a977bfd76a8516ae505fda2331b81e1049b87/firmware/sdk/components/sdk/watche_hw_camera/include/watche_hw_camera.h)、[`watche_hw_audio.h`](https://github.com/orulink-ai/WatcheRobot-public/blob/646a977bfd76a8516ae505fda2331b81e1049b87/firmware/sdk/components/sdk/watche_hw_audio/include/watche_hw_audio.h)、[`watche_hw_body.h`](https://github.com/orulink-ai/WatcheRobot-public/blob/646a977bfd76a8516ae505fda2331b81e1049b87/firmware/sdk/components/sdk/watche_hw_body/include/watche_hw_body.h)、[`watche_hw_display.h`](https://github.com/orulink-ai/WatcheRobot-public/blob/646a977bfd76a8516ae505fda2331b81e1049b87/firmware/sdk/components/sdk/watche_hw_display/include/watche_hw_display.h)|帧源机制、回调所有权、音频阻塞/单owner、运动角度/stop/位置回读、LVGL触摸API。|
|[TClaw官方ESP32-S3快速开始](https://docs.tuyaopen.ai/docs/tclaw/ducky-quick-start-ESP32S3) 与 [TClaw README](https://github.com/tuya/TClaw/blob/master/README.md)|ESP32-S3例板配置、TuyaOpen/TClaw支持范围与active development状态。|
|[PyPI av 16.0.1](https://pypi.org/project/av/16.0.1/)|Python 3.11 Linux ARM64 manylinux/musllinux wheel确实存在；目标系统兼容性仍待实测。|

## 不能据此宣称

- 不能说目标设备固件已经支持全部Python SDK接口；必须读取真实capability并核对 firmware/STM32/Himax版本。
- 不能说HX6538自定义模型、远场唤醒词、AEC或人脸识别已集成；本期不要求这些能力。
- 不能说某一JPEG快照等于实时视频，也不能以源码目标FPS当作实机FPS。
- 不能说motion API角度就是安全机械角度，或请求提交就是动作完成。
- 不能说ESP32 32MB physical flash允许烧录16MB之外配置；需要校验分区与恢复。
- 不能把PyPI有ARM64 PyAV wheel等同于SDK Daemon在泰山派已受支持。
- 没有通过此任务验证语音ASR/TTS/LLM、Codex连接、HA调用、提醒重启恢复或持续并发运行。
