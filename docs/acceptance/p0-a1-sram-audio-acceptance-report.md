# P0 / A1 工作包阶段验收报告：内部 SRAM 极低治理与 Watcher 音频硬件适配

**版本基线**：`99e747a` (main, post-PR #12)  
**当前状态**：
- **Level 1（源码机制与上游对齐）**：通过。
- **Level 2（单元测试 374 项、契约校验与本地真目标编译）**：通过。
- **Level 3（单项硬件与实时内存测量）**：待设备物理接通 USB COM21 后执行。
- **Level 4（端到端声学唤醒与云端语音闭环）**：待设备物理接通 USB COM21 后执行。

---

## 一、根因剖析与架构治理

### 1. 内部 SRAM 与启动 Flash/PSRAM 栈冲突治理 (G1)
- **启动路径与 PSRAM/Flash 冲突评审根因**：
  - ESP-IDF 5.4 在执行 SPI Flash 操作（如 `nvs_flash_init`、`esp_partition_read/write/erase`、Tuya KV 存储）时会调用底层接口临时关闭 CPU 外部存储总线 Cache。
  - 前一版方案尝试将整个 `desk_app_task` 栈直接分配在外部 PSRAM（`MALLOC_CAP_SPIRAM`），导致启动阶段在 PSRAM 栈上执行 `nvs_flash_init`、`desk_watcher_storage_init` 以及运行时 Flash 写入时，触发 Cache-disabled 外部栈访问异常与断言风险。
  - `watcher_storage.c` 直接调用 `esp_partition_read/write` 校验和注册尾部分区，绕过 Tuya TKL Flash 代理；同时 `esp_partition_write` 直接将位于 Flash `.rodata` 段的常量指针作为写入源，存在 Flash 互斥读取冲突隐患。
- **架构级整改措施**：
  1. **保留必要内部栈**：`desk_app_task` 严格分配在内部 SRAM（`MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT`），栈大小由历史未评估的 16KB 精简至 8KB（8,192 字节），既彻底杜绝 Flash Cache 禁用冲突，又为深度初始化调用链留出安全裕量。
  2. **释放主任务栈**：核验有效配置为 native 标准值 `CONFIG_ESP_MAIN_TASK_STACK_SIZE = 3584`（经由构建流水线与 [firmware/sdkconfig.defaults](file:///D:/ePanda/DeskAgent/firmware/sdkconfig.defaults) 统一对齐至 3,584 字节）。`app_main()` 成功派发 `desk_app_task` 后直接 `return 0`，利用 ESP-IDF 框架自释放机制自动回收 `main_task` 占用的 3,584 字节内部内存，杜绝空转常驻。实际释放收益以硬件串口实测遥测数据为准，坚决不作无证据估算。
  3. **加固持久化存储操作**：在 [watcher_storage.c](file:///D:/ePanda/DeskAgent/firmware/main/src/watcher_storage.c) 中增加栈探针检查（`esp_ptr_internal`），强制阻断从非内部栈发起的 Flash 操作；在写 Flash 前将 `expected` 结构体复制至局部 RAM 缓冲区，确保写入源指针完全位于内部 RAM。
  4. **外移非 Flash 关键线程**：
     - SDK 轮询任务 `desk_poll_worker`（8KB）无 Flash 操作，保持配置在 PSRAM（`MALLOC_CAP_SPIRAM`）。
     - 麦克风采集任务 `watcher_mic`（4KB）仅执行 I2S 读取与环形缓冲馈送，保持配置在 PSRAM。
     - 媒体传输 WSPK 大缓存与相机抓图缓存（64KB）全部切换至 `desk_psram_malloc` / `desk_psram_free`。

---

### 2. 内存观测与栈水位遥测增强 (G2)
- 在 [desk_cli.c](file:///D:/ePanda/DeskAgent/firmware/components/desk_cli/src/desk_cli.c) 中扩展 `sys_heap` 命令，增加任务栈最小剩余水位（High-Water Mark）遥测：
  - `desk_app_task_stack_hwm()`：实时读取并报告应用主任务栈历史最低剩余字节；
  - `desk_firmware_background_task_stack_hwm()`：实时读取并报告后台轮询任务栈历史最低剩余字节；
  - 输出内部 SRAM 剩余（Free）、最大连续块（Largest）、历史最低值（Minimum）以及 PSRAM 剩余。
- 在 [app_main.c](file:///D:/ePanda/DeskAgent/firmware/main/src/app_main.c) 启动尾部（`tuya_iot_start` 就绪后）输出 `[BOOT] stage=stack_status` 栈余量打点，为硬件实测提供直接依据，坚决不以理论估算伪造实测堆值。

---

### 3. Watcher 音频硬件适配与错误检查加固 (G3)
- **共享 I2S0 硬件契约对齐**：
  - SenseCAP Watcher 硬件上 ES8311 DAC 与 ES7243/E ADC 共享 I2S0 时钟与数据接口（MCLK=10, BCLK=11, WS=12, DOUT=16, DIN=15）。
  - 在 `esp_codec_dev_open` 之后，显式重置并校验 I2S0 硬件参数：
    - TX（ES8311）：单声道双槽（`I2S_STD_SLOT_BOTH`），16-bit 宽度，BCLK 锁定 512kHz。
    - RX（ES7243/E）：单声道右声道（`I2S_STD_SLOT_RIGHT`），16-bit 宽度，麦克风硬件增益配置为 27dB。
- **全链路错误检查治理**：
  - 严格检查 `i2s_channel_disable`、`i2s_channel_reconfig_std_slot`、`i2s_channel_reconfig_std_clock`、`i2s_channel_enable` 的 `esp_err_t` 返回值；任一阶段失败立即转入 `fail:` 分支，关闭功放 PA，严禁虚报 `s_ready = true`。
- **测试提示音行为规范**：
  - [desk_watcher_audio_test_tone()](file:///D:/ePanda/DeskAgent/firmware/components/desk_hardware/src/desk_watcher_audio.c) 播音前先读取并暂存当前硬件音量与静音状态（`esp_codec_dev_get_out_vol` / `esp_codec_dev_get_out_mute`）；
  - 解除静音并设置测试音量为 65，带返回值校验；
  - 播放 1000ms / 1kHz 方波（幅值 ±12000）；
  - 测试结束释放仲裁器前，完整恢复原音量与静音状态。

---

## 二、代码变更清单

| 文件路径 | 变更要点 |
| :--- | :--- |
| [firmware/main/src/app_main.c](file:///D:/ePanda/DeskAgent/firmware/main/src/app_main.c) | `desk_app_task` 栈保留在内部 SRAM（8KB），杜绝 Flash Cache 禁用冲突；导出 `desk_app_task_stack_hwm`；`app_main` 返回 0 回收 3584B `main_task` 栈。 |
| [firmware/main/src/watcher_storage.c](file:///D:/ePanda/DeskAgent/firmware/main/src/watcher_storage.c) | 引入栈内部探针（`esp_ptr_internal`）；Flash 写入源复制至局部 RAM 缓冲，防止只读 Flash 冲突。 |
| [firmware/components/desk_hardware/src/desk_watcher_audio.c](file:///D:/ePanda/DeskAgent/firmware/components/desk_hardware/src/desk_watcher_audio.c) | I2S 重构 8 个步骤全部增加 `esp_err_t` 严格校验；测试音增加前置状态暂存与后置恢复逻辑。 |
| [firmware/components/desk_cli/src/desk_cli.c](file:///D:/ePanda/DeskAgent/firmware/components/desk_cli/src/desk_cli.c) | `cmd_sys_heap` 增加任务栈水位（`app_task` 及 `poll_worker` 最小剩余字节）遥测输出。 |
| [firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h](file:///D:/ePanda/DeskAgent/firmware/components/desk_sdk_bridge/include/desk_sdk_bridge.h) | 声明 `desk_firmware_background_task_stack_hwm` 接口。 |
| [firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c](file:///D:/ePanda/DeskAgent/firmware/components/desk_sdk_bridge/src/desk_sdk_bridge.c) | 实现 `desk_firmware_background_task_stack_hwm`，并在 PSRAM 分配背景轮询线程栈。 |
| [p0_a1_sram_audio_acceptance_report.md](file:///D:/ePanda/DeskAgent/p0_a1_sram_audio_acceptance_report.md) | 根目录报告修改为索引指向本规范归档主报告，防止内容分叉。 |

---

## 三、分层证据（L1～L4）

### Level 1：官方源码与硬件原理依据
- **Seeed Watcher BSP 源码契约**：引脚定义（I2S0 TX DOUT=16, RX DIN=15, WS=12, BCLK=11, MCLK=10）；I2C 控制（ES8311 @ 0x18, ES7243E @ 0x14, PCA9535 @ 0x21）；TX 双槽复制、RX 右槽采样、16kHz / 16-bit / 27dB 麦克风增益。
- **ESP-IDF 5.4 内存与 Flash 规范**：SPI Flash 控制器在非 XIP 读写擦除时禁用外部总线缓存，栈与数据缓冲区必须处于内部 SRAM（`esp_ptr_internal` 为 true）。

### Level 2：契约验证与自动化测试
1. **WSL 单元测试套件**：
   - 命令：`wsl python3 -m unittest discover -s tests -t . -p "test_*.py"`
   - 结果：**374 项测试，364 项通过、10 项跳过、0 失败、0 错误**（耗时 82.384 秒）。
2. **契约校验器**：
   - 命令：`wsl python3 contracts/validator/validate.py`
   - 结果：G0 契约与全领域数据模式全部通过（**100% OK**）。
3. **真目标本地构建**：
   - 命令：`powershell -File tools/build_watcher_firmware.ps1`
   - 结果：ESP-IDF 5.4 + TuyaOpen SDK 目标编译通过，生成最新固件二进制。

### 编译固件哈希与分区安全边界 (G0)

| 分区 / 内容 | 起始地址 | 烧录控制 | 文件长度 | SHA-256 |
| :--- | :--- | :--- | :--- | :--- |
| **DeskAgent 候选应用** | `0x20000` | **受控单分区烧录** | 1,864,480 字节 | `855EE03C84E3582CC51CDA9E2669F798EB2E7C1868AA3133179F2FB33BF7449B` |
| **KWS 语音模型** | `0xC20000` | **严禁修改（哈希比对一致）** | 291,042 字节 | `7C87DD7ADB5A7623907B6354D49BEA7F9289371262E6A57F15458FB8908C5814` |
| **Bootloader** | `0x0` | **严禁修改** | - | `4A3F860385E5E3EA021BB85D8C75930F1F25189908F73C86BE07E990A67EE6D8` |
| **分区表** | `0x8000` | **严禁修改** | - | `DE7EA934A231F9D21EBAB6E28642D00D31145F722E4A921F11BA9BF094898F8D` |
| **配网与授权凭据** | `0x1FBF000+` | **严禁擦除 / 完整保留** | - | 本地 KV / NVS 保留原配网及已绑定数据 |

---

### Level 3 & Level 4：硬件实测与语音闭环状态
> [!IMPORTANT]
> **真实硬件状态**：当前系统设备管理器检测到 USB 串口断开（`no ports found`，COM21 离线）。
> 按照《AGENTS.md》防假断言清单与证据分层规定，**在未连接真实硬件并取得实机日志前，严禁将 L2 结果或 API 0 返回声明为 L3/L4 通过**。

- **待实测 L3 项目**：
  1. 向 `COM21` 的 `0x20000` 烧录固件 `desk_agent_firmware.bin`；
  2. 启动后观测 `stage=stack_status`，记录 `app_task` 实际初始栈剩余量；
  3. 设备联网连上 MQTT 后，执行 TAL CLI `sys_heap`，记录内部 SRAM Free、Largest、Minimum 与任务栈水位真实实测值（而非理论推算值）。
- **待实测 L4 项目**：
  1. 执行 `audio test`，现场确认 1000ms 1kHz 方波提示音实际物理可听见；
  2. 连续 3 轮中文真实云端语音闭环（“你好小智”唤醒 → VAD 分段 → 云端识别 → 大模型回复 → TTS 扬声器播放）；
  3. 执行一次断网与自动恢复测试，确认恢复后依然能够正常语音对话。
