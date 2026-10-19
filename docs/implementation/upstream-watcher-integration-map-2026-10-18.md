# SenseCAP Watcher 上游硬件集成来源图

日期：2026-10-18  
范围：定位 Watcher LCD/input/audio/HX6538 可复用上游，并记录本地固定依赖/真实 TClaw vendor 接入；未运行固件构建、不操作设备。

## 结论先行

首个实机画面应复用 Seeed 的 `sensecap-watcher` BSP 中 SPD2010 LCD 初始化和板级供电/总线配置。真实入口是 `bsp_lvgl_init()`（该函数会初始化 panel、touch、encoder 和 LVGL）或其底层 `bsp_lcd_pannel_init()` / `esp_lcd_new_panel_spd2010()`。A1 若首轮只要求显示一帧，可照 BSP 使用 `esp_lcd_new_panel_spd2010()` 建 panel 后调用 `esp_lcd_panel_draw_bitmap()`，暂时不启 LVGL；必须同时复用 PCA9535 电源初始化、LCD SPI3 pins、panel config、背光 GPIO/LEDC 与 RGB565/412×412 配置。不要只加 LCD driver manifest 而省略板级 power-up。

Watcher 的 HX6538 正常数据链路是 ESP32-S3 SPI2 + SYNC GPIO expander（SSCMA/PTL），不是 UART2。Seeed 的 BSP UART1/GPIO17/18 只出现在未启用的 flasher 接口代码中。Watcher 的实体 knob/touch、audio 也各有专属 pin map，不能从 XIAO/T5 示例推断。

TuyaOpen 本机 ESP32 IDF 为 5.4；Seeed BSP manifest 声明 `idf >=5.2.0`，可作为 5.4 移植基线但本报告未构建验证。WatcheRobot-public `646a977` 的整套硬件 SDK 明确要求 ESP-IDF 6.0.2，不能原样塞进当前 IDF 5.4 工程。可以借用它的硬件所有权封装思路；首个 LCD bring-up 应直接基于 Seeed BSP 的板级实现。

## 固定来源与版本

| 来源 | 固定版本 | 本包使用位置 | 用途与适配判断 |
|---|---|---|---|
| Seeed [SenseCAP-Watcher-Firmware](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware/tree/8e37f7ccd3d836bb99f483e0f6e74e8252406644) | 公开 main，`8e37f7ccd3d836bb99f483e0f6e74e8252406644` | `components/sensecap-watcher/` | 第一优先的真实 Watcher ESP32 BSP；README 当前指定 IDF 5.2.1，BSP manifest 下限 5.2.0；与 IDF 5.4 相邻兼容，仍需 A1 实际构建确认。 |
| Seeed [OSHW-SenseCAP-Watcher](https://github.com/Seeed-Studio/OSHW-SenseCAP-Watcher/tree/3ca69aefa8441f57d38993a39df714048633d032) | 公开 main，`3ca69aefa8441f57d38993a39df714048633d032` | `Hardware/SenseCAP_Watcher_v1.0_SCH.pdf`、`Hardware/HX6538_datasheet.pdf`、`Documentation/Product_Overview/watcher_hardware_overview.md` | 板卡/器件原始资料。用于核对板级资源，不把其它 Seeed 板型引脚代入。 |
| orulink-ai [WatcheRobot-public](https://github.com/orulink-ai/WatcheRobot-public/tree/646a977bfd76a8516ae505fda2331b81e1049b87) | 用户给定 tag `watche-v0.1.2`，`646a977bfd76a8516ae505fda2331b81e1049b87` | `firmware/sdk/components/sdk/watche_hw_display/`、`watcher_hx_ptl/`、`hal/hal_camera/`、`hal/hal_audio/` | 硬件服务/所有权封装和当前 HX PTL 实现。其 `firmware/sdk/examples/sdk.cmake` 的 `watche_hw_check_idf()` 要求 IDF **6.0.2**，不可作为 IDF 5.4 整包依赖。 |
| Tuya [TClaw](https://github.com/tuya/TClaw/tree/88bd37378f1d106fcc1175e61b368222cc8fabda) | 用户给定 commit `88bd37378f1d106fcc1175e61b368222cc8fabda` | `CMakeLists.txt`、`agent/`、TuyaOpen submodule gitlink | TClaw 应用/agent 代码来源，不是 Watcher BSP；HEAD README 列出 ESP32-S3 泛平台支持，但没有替代 Watcher 的 SPD2010/触摸/expander/pin 配置。submodule 指向 TuyaOpen `e82840d`。 |
| 本机 TuyaOpen SDK | `C:/Users/maida/TuyaOpenIDE/TuyaOpenSDK`，`cdc064e78daeeae598157470244e351cd37cbbed`；bundled ESP-IDF `v5.4` | `tools/cli_command/cli_build.py`、`platform/ESP32/tuya_open_sdk/CMakeLists.txt` | 当前 ESP32 phase-2 构建宿主。项目本地 component root 由 CLI 提供，不必改写 SDK 安装目录。 |

> 仓库 SHA 来自本次 `git ls-remote` / detached checkout；它们是本次调研时的固定快照。后续更新必须重新审查兼容性。

## A1：真实 LCD / 输入入口

### LCD

- BSP：`SenseCAP-Watcher-Firmware/components/sensecap-watcher/sensecap-watcher.c`
  - `bsp_lcd_pannel_init()`：创建 SPI panel IO，并调用 `esp_lcd_new_panel_spd2010()`；板级 SPI clock、queue、command/parameter width 和 panel vendor config 都在这里。
  - `bsp_display_lcd_init()`：设置 412×412、RGB565 和 LVGL panel config。
  - `bsp_lvgl_init()` / `bsp_lvgl_init_with_cfg()`：完整显示初始化入口；调用时会一起建立触摸、旋钮输入。
  - `bsp_lcd_brightness_set()`：背光亮度控制。
- 公开 API：`components/sensecap-watcher/include/sensecap-watcher.h` 中的 `bsp_lvgl_init()`、`bsp_lcd_get_panel_handle()`、`bsp_lcd_brightness_set()`。
- LCD driver 依赖：BSP `idf_component.yml` 固定 `espressif/esp_lcd_spd2010` 1.0.1；其余共同依赖见下一节。
- LVGL 基线：Seeed BSP repo 的 `components/lvgl/lv_version.h` 为 **LVGL 8.4.0**；`esp_lvgl_port` 为 1.4.0，依赖本地 `../lvgl` override。完整 BSP/LVGL 入口要保留此版本组合；native panel draw bitmap 可避开该 LVGL API 面。
- 最小画面路径：项目组件引入官方 SPD2010 component；按 BSP 初始化 IO expander/power、SPI3 bus、backlight、panel config，再 `esp_lcd_panel_reset/init/display/on` 和 `esp_lcd_panel_draw_bitmap`。该路径绕过 LVGL，但不能跳过 BSP 的 `bsp_io_expander_init()` 电源上电序列及复用总线仲裁。只可由 A1 在确认板版与现有 owner 后实施。

### Touch / knob

- Touch：BSP `bsp_touch_indev_init()`；SPD2010 touch controller，经 I2C1 SDA GPIO39 / SCL GPIO38，400 kHz。依赖 `espressif/esp_lcd_touch_spd2010` 0.0.1。
- Rotary encoder：BSP `bsp_knob_indev_init()`，A/B GPIO41/42；按钮经 PCA9535 `IO_EXPANDER_PIN_NUM_3`。`esp_lvgl_port` 将它们注册为 LVGL indev；按钮长按事件有 BSP `bsp_set_btn_long_press_cb()` 等入口。
- 所以若只做 LCD 首屏，暂不调用 touch/knob init 可缩小 bring-up 面；完整 `bsp_lvgl_init()` 则会初始化这些输入并要求相应依赖和资源。

## 板级资源（以 Seeed Watcher BSP 为准）

| 资源 | 官方 BSP 配置 | 说明 |
|---|---|---|
| LCD | SPD2010，412×412，RGB565；SPI3/QSPI clock GPIO7，data0..3 GPIO9/1/14/13；CS GPIO45，DC GPIO1，背光 GPIO8；40 MHz pixel clock | panel vendor config / reset / init sequence 必须从 `bsp_lcd_pannel_init()` 复用，不要另猜初始化表。 |
| Touch | SPD2010；I2C1 GPIO39/38，400 kHz | 和 LCD panel 分开的 I2C controller 通路。 |
| General I2C / expander | I2C0 GPIO47/48，400 kHz；PCA9535 16-bit expander address 0x21 | Seeed BSP 用它控制系统 rail、LCD、HX、codec PA 等，初始化顺序是板级必要步骤。 |
| Knob | GPIO41/42；按钮 PCA9535 bit/pin 3 | 使用 `bsp_knob_indev_init()` 的官方编码器封装。 |
| Audio | I2S0：MCLK GPIO10、BCLK GPIO11、LRCK GPIO12、mic DIN GPIO15、speaker DOUT GPIO16；16 kHz/16-bit；ES8311 speaker、ES7243 或 ES7243E mic | `bsp_codec_init()`、`bsp_audio_init()`、`bsp_i2s_read/write()`；麦克风具体 ADC variant 由 BSP 的 I2C probe 选取。需 `esp_codec_dev` 与 I2S driver 组件。 |
| HX6538 data | SPI2：SCLK/MOSI/MISO GPIO4/5/6，CS GPIO21，12 MHz；SYNC 是 PCA9535 bit 6 输入；PCA9535 bit 11 控 HX power | Seeed BSP 的 SSCMA client SPI + WatcheRobot `watcher_hx_ptl` SPI/PTL 数据路径；不是 GPIO19/20 UART。 |
| HX flasher | Seeed BSP 定义 UART1 GPIO17/18、921600 的 flasher 接口，但 BSP 中 UART flasher 初始化片段被注释；这不等于 runtime HX image/AI data 走 UART。 | 刷机/维护接口与正常数据通道要分开。 |

引脚依据：Seeed `components/sensecap-watcher/include/sensecap-watcher.h`；HX SPI runtime 依据 WatcheRobot `firmware/sdk/components/watcher_hx_ptl/watcher_hx_ptl.c`。硬件图为 Watcher v1.0 schematic；如目标板 revision 不同，先由 A1/A0 对图确认。

## Watcher BSP 组件依赖及 TuyaOpen phase-2 接入

Seeed BSP manifest 的关键依赖：

- `espressif/esp_lcd_spd2010` 1.0.1
- `espressif/esp_lcd_touch_spd2010` 0.0.1
- `espressif/esp_codec_dev` 1.2.0
- 本地 `lvgl` **8.4.0** + `esp_lvgl_port` 1.4.0
- `espressif/led_strip` 2.5.4、`button` 3.2.3、`knob` 0.1.4
- 本地 overrides：`esp_lvgl_port`、`sscma_client`、`esp_io_expander_pca95xx_16bit`
- BSP CMake 还依赖 ESP-IDF 的 LCD、SPI/I2C、LEDC、I2S、PSRAM、FATFS/SPIFFS、LVGL 等。

TuyaOpen SDK 本地 source 可确认 ESP32 CLI 接入：

1. `tools/cli_command/cli_build.py` 在 ESP32 项目构建阶段将 `<app_root>/esp_components` 加入 `TUYAOS_EXTRA_COMPONENT_DIRS`。
2. `platform/ESP32/tuya_open_sdk/CMakeLists.txt` 把这些目录追加到 `EXTRA_COMPONENT_DIRS`，之后 ESP-IDF `project()` 组件发现器将其子目录视为 project components。
3. 本地 CLI 将 `<app_root>/esp_components` 加入环境变量 `TUYAOS_EXTRA_COMPONENT_DIRS`，而本仓库 `firmware/esp_components` 是指向 `firmware/components` 的 Junction。因此项目 wrapper 放在 `firmware/components/desk_watcher_deps/` 可由 phase-2 发现，原样上游代码则放入 `firmware/third_party/`；本包已使用此布局，无需修改 Junction 或 SDK checkout。
4. SPD2010 来源采用 registry 固定版本 archive vendoring，而非构建时在线 resolver；archive SHA-256、license 和源路径记录在 `firmware/third_party/esp_lcd_spd2010/PROVENANCE.md` 与 `contracts/dependency_lock.json`。本包不依赖 managed component 下载锁。

TuyaOpen 本机 SDK `cdc064e` 的 ESP32 target bundling IDF 5.4。Seeed BSP 声明 IDF `>=5.2.0` 且 README 测试说明用 5.2.1；因此该 BSP 比 WatcheRobot 6.0.2 SDK 更适合直接移植到现有基线。`>=5.2` 只是作者声明，不代替 IDF 5.4 构建结果。其 I2C driver 当前使用 legacy API，5.4 有该 API，但 component-manager 驱动版本和 SPD2010 touch compat 仍需实际构建检查。

## Watcher HX6538 runtime 与 body MCU 分界

- Seeed BSP `bsp_sscma_client_init()`：复用 `bsp_io_expander_init()` 和 SPI2，配置 `sscma_client_new_io_spi_bus()`，设置 reset/SYNC 经 expander，然后创建 SSCMA client。
- WatcheRobot `watcher_hx_ptl/watcher_hx_ptl.c`：在固定 SHA 上用 SPI2 4/5/6、CS21、12 MHz 与 expander SYNC bit6 实现 HX binary PTL；AI 芯片供电调用 BSP expander power bit。上层 `hal/hal_camera/src/hal_camera_ptl.c` 消费 JPEG 帧。
- 独立协议实现位于 `firmware/sdk/components/hx_ptl_protocol/` 与 `hx_media_protocol/`；这些是 WatcheRobot repo 的来源版本，不表示 DeskAgent 当前 STM32 协议就是 HX protocol。
- TClaw 是 agent/dialogue app 来源，不应承担/重复实现板级 SPI、LCD 或 camera transport。
- Watcher 的 HX runtime SPI 路径、维护 UART、机身 STM32/body 通道是三件不同事。首个 LCD bring-up 不需要触碰 HX、STM32 或任何串口。

## 本包依赖引入与 phase-2 注册结果

- 已将 Espressif Component Registry `espressif/esp_lcd_spd2010` v1.0.1 官方原样源码 vendoring 至 `firmware/third_party/esp_lcd_spd2010/`。锁定 registry archive SHA-256 见 `PROVENANCE.md`；保留 Apache-2.0 license、README、changelog、manifest。该版本为 Seeed Watcher BSP 在上游锁定提交使用的版本，源 manifest 声明 IDF `>=5.2.0`。
- IDF wrapper 位于 `firmware/components/desk_watcher_deps/`，经现有 `firmware/esp_components` Junction 指向的 `firmware/components` 被 TuyaOpen CLI phase-2 扫描。Wrapper 的 `CMakeLists.txt` 将 vendored 源注册为单一 `desk_watcher_deps` IDF component；wrapper 不含 `src/*.c`，因此不匹配 phase-1 `COMP_SRCS` glob。`firmware/main/CMakeLists.txt` 显式 `REQUIRES desk_watcher_deps`，确保进入 phase-2 dependency graph。没有更改或替换 Junction。
- phase-1 app include path 已添加 SPD2010 public header 和 IDF `esp_lcd` headers；驱动实现只由 phase-2 IDF component 编译。配置依赖 IDF 5.4 `esp_lcd` 与 `driver` 组件；仅做了源码/manifest/API 检查，本包没有跑 SDK configure/build。
- 下一 A1 adapter 可 `#include "esp_lcd_spd2010.h"`，由 `desk_watcher_deps` target 导出 include path/link dependency。v1.0.1 API 为 `SPD2010_PANEL_BUS_QSPI_CONFIG(...)` / `SPD2010_PANEL_IO_QSPI_CONFIG(...)`，`spd2010_vendor_config_t`、`esp_lcd_new_panel_spd2010()`；显示缓冲使用 IDF `esp_lcd_panel_draw_bitmap()`。官方说明 SPD2010 的 `x_start` 和 `x_end` 均须为 4 的倍数。若用 Seeed 4-wire/QSPI wiring，adapter 需按 BSP `bsp_lcd_pannel_init()` 实际总线配置使用，而不能把 SPI/QSPI macro 互换。
- PCA9535/I2C 官方参考位置仍是 Seeed `components/sensecap-watcher/sensecap-watcher.c` 中 `bsp_io_expander_init()` / `esp_io_expander_new_i2c_pca95xx_16bit_ex()`，以及其 `idf_component.yml` 对 `esp_io_expander_pca95xx_16bit` 的本地 `override_path`。本包没有 vendor PCA9535 driver 或板级代码；A1 需单独引入/锁定该官方 expander 组件并复用上游 power sequence，不能据本包声称 LCD 可直接点亮。
- 依赖锁的授权值字段已替换为本地安全配置引用/REDACTED，共 6 个字段；设备本地配置未读取、修改或轮换。

## TClaw 原生 agent app 接入（已 vendor，尚未构建验证）

- 固定来源为 `tuya/TClaw` commit `88bd37378f1d106fcc1175e61b368222cc8fabda`，上游 TuyaOpen submodule gitlink 为 `e82840dc9b629cb77fa922fb04e3f820dc2e78b4`；项目实际继续使用本机 TuyaOpen `cdc064e` / ESP-IDF 5.4，不拉取或修改 SDK submodule。vendor 文件、排除项、license、gitlink 与摘要 hash 见 `firmware/third_party/tclaw/PROVENANCE.md` 和依赖锁。
- `firmware/CMakeLists.txt` 将 vendor 的 `src/`, `agent/`, `tools/`, `cron_service/`, `heartbeat/`, `memory/`, `gateway/`, `skills/` 加入真实 `EXAMPLE_LIB`；上游 `IM/CMakeLists.txt` 也挂入该 target。Lua 按上游 `CONFIG_ENABLE_LUA` 开关加入。`src/tuya_app_main.c` 不 vendoring/不编译，避免第二 app 入口。
- 当前 SDK 的 `ai_mcp` 和 `ai_agent` target 必须存在且其对应源可替换，CMake 将其源替换为固定 TClaw commit 的官方 overlay；缺 target/source 会在 configure 时明确失败，不提供本地 stub fallback。实际 IDF 5.4 构建尚未运行，源 API 兼容仍待统一构建确认。
- A1 的唯一 app entry 应保留 TClaw 原生启动语义并由其适配入口调用 `sys_bus_init()`、`tclaw_chat_init()`、`tool_registry_init()`、`agent_loop_init()`；`ws_server_start()`、`acp_client_init()` 和 `app_im_init()` 依部署配置决定是否启动。相关真实公开头分别为 `include/sys_bus.h`、`include/tclaw_chat.h`、`tools/tools_register.h`、`agent/agent_loop.h`、`gateway/ws_server.h`、`gateway/acp_client.h`、`include/app_im.h`。禁止启用含 T5 pin 假设的 hardware MCP，直至 Watcher adapter 具备对应能力。
- Watcher profile `firmware/config/SEEED_SENSECAP_WATCHER.config` 已加入；相同的板型、TClaw AI overlay 和 PSRAM 选项也进入 CLI 默认 `firmware/app_default.config`，保留原来的 32 MB flash、UART pin 与 8192-byte app stack 设置。没有导入账号/云密钥配置。WatcheRobot SDK 对 IDF 6.0.2 的要求仍使本机 TuyaOpen 5.4 成为当前最小兼容路径；最终兼容结论以真实构建结果为准。

## 最小 A1 LCD adapter 范围（尚未实现/验证）

1. 输入固定使用本机 TuyaOpen `cdc064e` / ESP-IDF 5.4，并确定唯一 CLI app root 与产物路径。
2. 使用本包已注册的 SPD2010 driver；在 A1 board adapter 中复用 Seeed `sensecap-watcher` 的 PCA9535 power sequence、SPI3、panel init、背光与资源互斥。之后为 PCA9535 expander 单独增加固定版本官方 component。首屏可先不拉入 audio、SSCMA、HX PTL、触摸和完整 LVGL。
3. 仅由 `app_main` 经硬件 owner 初始化此 component，显示静态纯色/测试图并记录返回值。若 native draw bitmap，则在同一个 owner 中管理 SPI3/panel lifecycle，禁止再由另一处 BSP/LVGL 并发控制 panel。
4. 验收等级：构建 + 实机画面各自记录；本报告没有执行构建或设备验证。

## 主要可追溯文件

- Seeed firmware README：`SenseCAP-Watcher-Firmware/README.md`（IDF 5.2.1 build guide）
- Seeed BSP: `components/sensecap-watcher/{idf_component.yml,CMakeLists.txt,sensecap-watcher.c,include/sensecap-watcher.h}`
- Seeed hardware: `OSHW-SenseCAP-Watcher/Hardware/SenseCAP_Watcher_v1.0_SCH.pdf`、`Hardware/HX6538_datasheet.pdf`
- WatcheRobot wrapper/transport: `firmware/sdk/components/sdk/watche_hw_display/`、`firmware/sdk/components/watcher_hx_ptl/`、`firmware/sdk/components/hal/hal_camera/`、`firmware/sdk/examples/sdk.cmake`
- TClaw app source: `tuya/TClaw` at `88bd37378f1d106fcc1175e61b368222cc8fabda`
- TuyaOpen phase-2 component root: local `tools/cli_command/cli_build.py` and `platform/ESP32/tuya_open_sdk/CMakeLists.txt`

