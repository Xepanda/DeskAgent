# 2026-10-18 配网与真实硬件联调记录

## 本轮目标与边界

- 用户报告智能生活 App 配网失败、无法验证外设，授权总协调与 Luna 继续修复，并授权连接日志、重启 ESP32，由用户操作手机配网。
- 当前 HEAD：`9472d70879ba027aa61c96aa5c73e0de9cb83f99`，工作树有先前 Agent 未提交的实现与文档；HEAD 不能单独标识设备镜像。
- 保留现有修改；仅一个执行者操作 COM21。COM20/Himax 不作为试错烧录端口。
- 不清 NVS/身份、激活信息，不改 eFuse，不写 STM32/Himax，不推送或发布。
- 原始设备日志在本机受控目录保存；报告只保留脱敏摘要，禁止记录 UUID/AuthKey/token/密码。

## 当前判定

- 撤回此前“L1–L3 全部 PASS / 100% 达标”作为整体验收结论；手机配网和真实外设仍需逐项验证。
- 已记录的 TuyaOpen 构建确实包含 SDK cloud/netmgr/netcfg 头文件及库。不能仅由源码存在 host fallback 就认定运行固件没有真实 SDK。
- `source/embedded` 是指向 `firmware` 的 Junction，实际最新 TuyaOpen ELF/MAP 在 `firmware/.build/bin/`。`firmware/build/` 的 ELF/MAP 时间较旧，禁止与较新的 bin 混用做回溯。
- 当前默认硬件 ops 仍有占位：显示初始化标志、空音频播放、零采样录音、无真实 JPEG 驱动；状态文本不能视为物理外设实测。
- 发现报告含敏感授权字段：禁止继续整篇回显；待独立脱敏，不自行轮换或清除设备授权。

## 工作包

| 包 | 执行者 | 范围 | 状态 |
|---|---|---|---|
| NET-OBS | Luna / live_provisioning_diagnosis | 唯一设备操作者；COM21 有界日志、必要重启、手机配网阶段定位 | 已完成；首轮 STA 获取 IP，不代表绑定；第二轮硬复位后进入绑定态，未证实手机到云端闭环 |
| HW-HONEST | Luna / current_bsp_diagnosis | 仅硬件层与回归测试；缺驱动不得报告成功，生产 UART 可用性与写错误处理 | 第一轮 53 项测试通过，独立复核 3 项；修复两个 P2 边界中 |
| UPSTREAM-BOARD | Luna / integration_baseline_diagnosis | 官方 Watcher/TClaw BSP 来源、固定版本和兼容性，提供真实驱动接入依据 | 调研完成，见独立 integration map |
| SDK-BOOT | Luna / live_provisioning_diagnosis | 官方 TClaw/SDK 启动顺序、真实 TAL CLI、错误传播、脱敏阶段状态 | 实现中，暂停手机验收直到固件更新 |
| LCD-DEPS | Luna / integration_baseline_diagnosis | 固定官方 SPD2010 原生组件与项目本地构建注册 | 实现中；不修改外部干净 SDK |

## 决策

- Ruling: 在现有工作区按独占文件范围修复，不另建空白 worktree；现有未提交实现与本机工程绑定必须保留。风险是共享工作树变动，使用单 owner 文件边界、串行集成与隔离测试控制。
- Ruling: 对未连接的硬件返回 unavailable，不让缺 BSP 阻断整个网络和诊断服务；主入口应分别报告子系统状态，不能打印整体自检通过。
- Ruling: 配网按发现/连接、网络信息接收、STA关联、获取IP、激活、MQTT在线分别记录；广播或 AP DHCP 不是绑定成功。
- Ruling: 本地自建 `components/tal_cli` 遮蔽官方同名组件，批准改名为 `desk_cli` 并恢复 SDK registry/worker；公共构建修改由 A0，诊断注册由 A1，避免双串口读任务。
- Ruling: 首个真实 LCD 接入使用 Seeed 固定版本的 SPD2010 原生 panel API，先不引入整套 LVGL；新增依赖 wrapper 经现有 `esp_components` 发现，第三方源保留许可证与哈希。
- Ruling: 用户要求代码和固件准备好后再配网。先完成源码整合、构建与非手机诊断，不反复让用户在旧固件上尝试。

## 已执行的受控设备观测

- 第一轮 160 秒日志中 `sys_reboot` 返回未知命令，因此不算复位后观测。一次 Wi-Fi 连接报 `0x300a`（`ESP_ERR_WIFI_SSID`），随后 STA 获取 IP，未观测到激活或 MQTT 在线。
- 第二轮 90 秒使用已确认端口的硬件复位，日志包含 ESP-ROM 真启动、Tuya BIND_START、SoftAP 和 AP DHCP。该轮没有新的手机尝试，不能用于判定手机交接阶段失败。
- 两轮日志仅存本机受控目录。未清 NVS、未烧录、未改其它 MCU。
- 历史 r7 报告已增加撤回整体验收结论的声明，并脱敏两个授权字段；设备本机合法配置保留。

## 待验证

- 用户智能生活 App 本轮尝试的阶段与错误。
- 配网根因与修复后真实成功记录。
- 官方 Watcher 屏幕/输入/音频/Himax 相机实际驱动接入。
- STM32 物理 UART 映射、双方协议及真实 ACK。
- 真实语音工具请求、Host 处理及设备反馈端到端闭环。

## 续调：Luna 中断后由总管家接续

- 三个 Luna 执行包因工作区 credits 耗尽中断。用户要求继续后，由总管家接续本机集成；没有远端推送或发布。
- 源码已加入固定版本 TClaw、官方 SPD2010、原生 Watcher LCD 适配、真实 TAL CLI 注册及分阶段配网状态。不能据此认定配网成功。
- 修复生成配置陈旧问题：`using.config` 已启用 AI，但旧 `using.cmake/tuya_kconfig.h` 关闭 AI；新增 `tools/build_watcher_firmware.ps1` 根据实际配置输入指纹重建这三个项目生成文件。
- TuyaOpen 的应用编译和 IDF 原生组件分开：`esp_components` 改为普通目录，仅暴露原生依赖 wrapper，避免重复注册 DeskAgent 代码和错误依赖 IDF `tal_cli`。
- 原计划 `sensecap_watcher_audio` 超过 TDL 16 字节名称上限，统一改为 `watcher_audio`。
- 加入 Watcher ES8311 + ES7243/E 共享 I2S0 实现、真实采音计数/幅值诊断及有界测试音。当前仍需目标编译和物理验证，不标记音频通过。
- 原生 TClaw 启动适配器与编译入口已接入；可选 IM/ACP/WS 在无配置时禁用，初始化失败保留配网和诊断。
- 生产 ESP32 注册部分板级 ops 时，原动作停止接口仍可能返回成功。新增回归先复现 `0 != unavailable`，再修复缺失 move/stop 回调的返回路径。
- WSL 标准 unittest 实测 30 项通过（missing-BSP、LCD shim、runtime contract、provisioning state、safe startup）；仅属 L2。
- SDK 的 `audio_afe_processor_init` 在模型初始化失败后仍创建线程，项目 overlay 改为返回失败并保留诊断；SDK 源码不被覆盖。
- 当前正在解决最终 IDF 链接/配置问题；本次尚未烧录，未要求用户再配网。

### 仍存在的实质缺口

- 原生语音模型分区内容、唤醒/VAD、实际采音/播放均未验收。
- TClaw 原生 MCP 到既有 Host/Codex/HA 工具桥仍需真实联通测试，不能以本地关键词模拟替代。
- HX6538 运行时相机驱动未整合；STM32 尚缺真实握手和对应物理 UART 证据。
- 官方 mcu_link 在 A5/5A 原始帧外还有 COBS 编码与 0x00 定界；历史“COBS 与 A5/5A 二选一”的裁决不成立。未握手前不发送运动帧。

## 2026-10-19：官方回退验证后恢复自研固件

- 官方 v0.4.4 已真实启动，用户确认原界面和操作正常；回退基线见 `official-recovery-assessment-2026-10-18.md`。旧的截断 ota0 备份不能作为恢复镜像。
- 用户明确批准初始化已备份的尾部 `0x1FBF000–0x1FFFFFF`，已执行。Tuya 虚拟数据区使用独立标记和边界校验，不改写现有分区表、NVS 或 agentcfg；首次实机日志确认注册成功，后续重启识别既有数据区。
- 用户明确批准在 `0xC20000` 写入本地“你好小智”模型。完整 1 MiB 原内容已私密备份；新模型 291,042 字节，SHA256 `7c87dd7adb5a7623907b6354d49bea7f9289371262e6a57f15458fb8908c5814`，写入校验成功。未改写 Himax 固件。
- 第一版自研镜像实机确认 LCD 首帧、ES8311/ES7243E 音频 codec 初始化、TAL CLI 和 `tuya_iot_initialized`，随后 BLE 报 `Malloc failed` / `emi.c` assert 并反复重启，不能算配网就绪，更不能算语音验收通过。
- 移除 ESP32 生产路径误保留的 32 KiB 模拟音频环与 64 KiB 模拟相机帧；主机测试继续使用隔离缓存。生产相机无真实驱动时返回不可用。回归 40 项通过，目标构建成功。
- 修复日志回调：SDK `tkl_log_output` 不处理传入的变参，旧 `%s` 写法隐藏实际日志；过滤敏感行后用 `fputs` 输出。新增 radio 启动前内部 SRAM 余量及最大连续块观测。
- 内存修复镜像 SHA256 `2c835d823f3664a7d6f65cb8d1d0837d49f682e7d89b149b5720501f6f6f83af`，1,862,720 字节，ESP32-S3/32MB 镜像头与校验有效。仅写应用地址 `0x20000`，后续实机结果另记。
- 原始日志和凭据相关备份留在本机受控目录，不纳入 Git。未推送、未发布。

### PSRAM 调整与第二轮实测准备

- 前一轮 115 秒实机观测：无 panic/复位循环，音频读取累计超过 3,000 帧、错误 0，提示音 API 返回 0；用户当时未注意，不能认定听感通过。
- BLE 已真实初始化并开始广播，但内部 SRAM 约 8 KiB，SoftAP `ap_netcfg_t` 申请失败。设备总剩余堆约 7.8 MiB，不能用总堆数字掩盖内部 SRAM 耗尽。
- 使用项目内 PSRAM BSS 属性迁移协议接收/UDP 配对/提示音和 CLI JPEG 工作缓存；ELF 核验六个缓存地址均为 `0x3c...`，合计 24,706 字节。JSON 改用官方 `tal_psram_malloc/free`；AFE 两个任务使用真实 IDF 支持 PSRAM 的 pinned task API，并配对使用 Caps 删除接口，保留内部 TCB。
- 固件契约回归 92 项通过。新目标构建成功，镜像 1,862,704 字节，SHA256 `d6ca7cc0f97f11e1eafb3cade12855a35f1eb572fe00732f74432aa5bdae5ef8`；32MB 镜像头、镜像校验有效。
- 当前 esp-sr 二进制拒绝 `WEBRTC` NS 模型名，因此 WakeNet-only 模型下明确关闭缺失的降噪模型，保留真实 WakeNet/VAD；不宣称神经降噪已实现。
- 复用官方音视频链路：锁定 WatcheRobot-public `e89a5fd3ae9fa9115e5122e252ab90fe5dd0b72b`，存在 `watcher_hx_ptl`、`watcher_hx_control`、`hx_media_protocol`、`hal_camera_ptl`；SDK 部分 Apache-2.0。当前 IDF 5.4 没有上游使用的 `SPI_TRANS_DMA_USE_PSRAM` 标志，需用受控 DMA bounce 适配，并复用现有 I2C/expander owner，不能直接把 IDF 6.0.2 BSP 整套混入。尚未合入或验证相机。

## 2026-10-19 阶段收尾

- 用户确认配网成功；独立 COM21 只读诊断确认 `Provisioning stage: mqtt_online`。本轮未烧录、未复位、未清除配网。
- 联网后实际剩余内部 SRAM 约 1,835 字节，`system` 诊断时观察到 111 字节，PSRAM 约 7.5 MB。此项列为下一轮 P0，不能视为稳定版本。
- 麦克风累计 23,123 帧、读取错误 0；用户反馈未听到提示音。播放和真实唤醒未验收，下一轮只做官方 SDK 音频链的硬件适配和验证。
- 最新全量软件验证 374 项（364 通过、10 跳过）、契约校验通过，WSL GCC 原生链接成功。主验收脚本受 CRLF 和 WSL 无 cmake 影响，未声称其全阶段通过。
- 当前交接与后续完整任务，以 `handoff-2026-10-19.md` 为准；补充用户指定的 bloub UI/表情参考。
