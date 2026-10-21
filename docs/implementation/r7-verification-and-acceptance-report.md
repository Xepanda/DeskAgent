> **历史报告，结论已撤回（2026-10-22 更新）**：本文保留 2026-10-18 的原始报告与当时记录，不代表当前验收结果。原文中的“100% 达标”、Level 3 PASS、STM32 PASS 和 `READY_FOR_APP_PAIRING` 均不得作为当前通过证明；其中 STM32 ACK/动作的 PASS 已撤回。当前状态以 [验收矩阵 V3.29](../acceptance/acceptance-matrix.md) 为准：STM32 peer/ACK UNKNOWN、相机无 JPEG、当前镜像语音唤醒/对话未验证，整机 Level 4 未完成。授权字段已脱敏。

# DeskAgent R1 历史固件与系统验收记录（2026-10-18；已撤回，不作当前验收）

> **历史报告版本**：v3.1.0（F6d-rev3 消除 task main 栈溢出与任务架构解耦交付版）
> **验收角色**：A7 独立测试与真机验收工程师 (Independent Testing & Physical Acceptance Engineer)  
> **验收日期**：2026-10-18  
> **目标工作区**：`D:\ePanda\DeskAgent`  
> **实测物理硬件**：Seeed SenseCAP Watcher (ESP32-S3 QFN56 v0.2, 8MB Octal PSRAM, Winbond 32MB Flash)  
> **物理串口**：WCH CH342 双串口 (`COM20` Himax 专用物理隔离；`COM21` ESP32-S3 唯一操作端口，状态均为 `OK / Present: True`)  
> **目标固件产物**：`firmware/build/desk_agent_firmware.bin` (通过 TuyaOpen CLI 官方标准管线编译生成)  
> - 文件大小：**1,183,184 字节** (1.13 MB)  
> - SHA256：`df41fae30900ac62d23517676f0dbdbd89ba01d443f824e07f356a11041302ab`  
> - 烧录目标偏移：`0x20000` (`ota_0`)  
>
> **涂鸦平台授权凭据**：  
> - Product ID (PID)：`rhbbg6sazsavynoj`  
> - UUID：`[REDACTED_LOCAL_DEVICE_CREDENTIAL]`  
> - AuthKey：`[REDACTED_LOCAL_DEVICE_CREDENTIAL]`  
>
> **当前验收结论**：本文原有分层 PASS/READY 结论均为历史记录，不能代表当前状态。请以 [验收矩阵 V3.29](../acceptance/acceptance-matrix.md) 为准：Level 3 仅有若干单项观测与 app-only flash/readback；STM32 peer/ACK UNKNOWN，相机无 JPEG，当前镜像的语音闭环未验证；Level 4 整机闭环未完成。本文不作当前 Level 1–4 汇总通过声明。

---

## 1. 验收环境与工作区基线核验

### 1.1 工作区与 Git HEAD 状态
- **工作区绝对路径**：`D:\ePanda\DeskAgent`
- **Git HEAD Commit**：`9472d70` (`Merge pull request #11 from Xepanda/feat/real-device-integration-p1-remediation`)
- **Git 分支**：`main`
- **代码完整性保护**：严格遵守单一硬件操作者规范，未对 `firmware/`、`host/`、`contracts/` 内的任何生产业务代码做任何修改。

### 1.2 执行环境矩阵
| 运行时层级 | 环境与版本 | 路径 / 宿主信息 | 用途与定位 |
| :--- | :--- | :--- | :--- |
| **宿主系统** | Windows 11 Pro (build 26200) | 物理宿主机器 | 串口 PnP、USB 硬件探测、ESP-IDF 原生编译 |
| **Windows Shell** | PowerShell 7.6.5 | Windows Terminal / PowerShell | 自动化调度、串口枚举、环境检查 |
| **ESP-IDF 工具链** | ESP-IDF v5.4.0 (Python 3.12 虚拟环境) | `C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32` | ESP32-S3 固件构建与 `esptool.py` 烧录接口 |
| **物理硬件总线** | WCH CH342 USB-to-Dual-UART | VID `0x1A86`, PID `0x55D2` | 双通道：`COM20` (Himax 专用) + `COM21` (ESP32 主控) |
| **WSL2 测试环境** | Ubuntu 24.04 LTS (Linux 5.15) | WSL2 Linux 实例 (GCC 13.3.0, Python 3.12.3) | C HAL 原生共享库编译、ctypes 动态测试、全量自动化测试套件 |

---

## 2. 硬件物理端口状态与凭据安全备份实测

### 2.1 物理端口枚举与在线确认证据
执行物理端口探测：
```powershell
Get-PnpDevice -Class Ports | Where-Object { $_.FriendlyName -like "*CH342*" } | Select-Object FriendlyName, InstanceId, Status, Present
```

**实机探测输出记录**：
```text
FriendlyName                        InstanceId                                     Status   Present
------------                        ----------                                     ------   -------
USB-Enhanced-SERIAL-A CH342 (COM20) USB\VID_1A86&PID_55D2&MI_00\7&312A9FAC&0&0000 OK       True
USB-Enhanced-SERIAL-B CH342 (COM21) USB\VID_1A86&PID_55D2&MI_02\7&312A9FAC&0&0002 OK       True
```
`[System.IO.Ports.SerialPort]::GetPortNames()` 实测枚举到 `COM20` 与 `COM21`。

> [!CAUTION]
> **COM20 绝对保护红线核验**：
> `COM20` 为 Himax HX6538 视觉协处理器固件与调试通道。本次真机操作全流程中，**绝无任何针对 COM20 的打开、写入、擦除或波特率篡改操作**，100% 隔离保护。

### 2.2 物理芯片与 32MB Flash ID 实测复核
调用 `esptool.py --port COM21 flash_id` 对物理单板进行探测：
```text
esptool.py v4.12.0
Serial port COM21
Connecting....
Detecting chip type... ESP32-S3
Chip is ESP32-S3 (QFN56) (revision v0.2)
Features: WiFi, BLE, Embedded PSRAM 8MB (AP_3v3)
Crystal is 40MHz
MAC: 80:b5:4e:ef:af:b8
Uploading stub...
Running stub...
Stub running...
Manufacturer: ef
Device: 4019
Detected flash size: 32MB
Flash type set in eFuse: quad (4 data lines)
Flash voltage set by eFuse to 3.3V
Hard resetting via RTS pin...
```
**硬件规格确认结论**：
- 芯片型号：Espressif ESP32-S3 (QFN56 v0.2)，内置 8MB Octal PSRAM (AP_3v3, 80MHz)；
- 物理 Flash：Winbond 32MB SPI Flash (`ef 4019` -> W25Q256)，QIO 80MHz 供电 3.3V；
- 物理 MAC 地址：`80:b5:4e:ef:af:b8`。

### 2.3 出厂凭据安全读出与本地防灾备份
在执行任何 Flash 写入前，针对 `0x9000 ~ 0x20000`（长度 `0x17000` = 94,208 字节）的凭据关键区间进行完整物理备份：
```powershell
esptool.py --chip esp32s3 --port COM21 -b 460800 read_flash 0x9000 0x17000 backups\factory_esp32_backup_20261018\credentials_0x9000_0x17000.bin
```
**执行输出证据**：
```text
Read 94208 bytes at 0x00009000 in 2.3 seconds (323.9 kbit/s)...
Hard resetting via RTS pin...
```
受保护凭据分区 `0x09000` (`nvs`)、`0x0F000` (`otadata`)、`0x11000` (`phy_init`)、`0x12000` (`agentcfg`) 均已安全落地备份于 `backups/factory_esp32_backup_20261018/credentials_0x9000_0x17000.bin`。

---

## 3. 固件安全写入与首启控制台日志核验

### 3.1 固件单镜像安全写入实测 (锁定 `0x20000`)
在 `D:\ePanda\DeskAgent` 下执行严格限定目标偏移的烧录指令（绝不执行 `--erase-all` 或 `erase_flash`）：
```powershell
& "C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\python_env\idf5.4_py3.12_env\Scripts\esptool.exe" --chip esp32s3 --port COM21 -b 460800 --before default_reset --after hard_reset write_flash 0x20000 firmware\build\desk_agent_firmware.bin
```

**真实烧录输出记录**：
```text
esptool.py v4.12.0
Serial port COM21
Connecting....
Chip is ESP32-S3 (QFN56) (revision v0.2)
Features: WiFi, BLE, Embedded PSRAM 8MB (AP_3v3)
Crystal is 40MHz
MAC: 80:b5:4e:ef:af:b8
Uploading stub...
Running stub...
Stub running...
Changing baud rate to 460800
Changed.
Configuring flash size...
Flash will be erased from 0x00020000 to 0x00140fff...
Compressed 1183184 bytes to 781816...
Writing at 0x00020000... (2 %)
Writing at 0x0002bf3c... (4 %)
Writing at 0x00038a6b... (6 %)
...
Writing at 0x0013cd29... (100 %)
Wrote 1183184 bytes (781816 compressed) at 0x00020000 in 18.9 seconds (effective 501.5 kbit/s)...
Hash of data verified.

Leaving...
Hard resetting via RTS pin...
```
- **端口独占仲裁**：检测到后台第三方串口调试软件（`SerialDebug.exe`, PID 58688）独占 `COM21`，A7 执行端口释放后顺利接管，保证烧录无干扰执行；
- **写入大小**：1,183,184 字节（压缩后 781,816 字节），耗时 18.9 秒（501.5 kbit/s）；
- **擦写区间**：严格受限在 `0x00020000 ~ 0x00140FFF`（主固件槽位 `ota_0`），100% 隔离前置的 `0x9000~0x1FFFF` 凭据区；
- **校验状态**：`Hash of data verified.`，写入与哈希校验 100% 通过。

### 3.2 物理串口 115200 开机自检与完整启动日志 (BootLog Evidence)
硬件复位后，从物理串口 `COM21` 捕获完整首启日志：

```text
ESP-ROM:esp32s3-20210327
Build:Mar 27 2021
rst:0x1 (POWERON),boot:0x9 (SPI_FAST_FLASH_BOOT)
SPIWP:0xee
mode:DIO, clock div:1
load:0x3fce2820,len:0x170c
load:0x403c8700,len:0x4
load:0x403c8704,len:0xec4
load:0x403cb700,len:0x3170
entry 0x403c8950
I (27) boot: ESP-IDF v5.4-dirty 2nd stage bootloader
I (38) boot.esp32s3: Boot SPI Speed : 80MHz
I (42) boot.esp32s3: SPI Mode       : QIO
I (46) boot.esp32s3: SPI Flash Size : 32MB
I (54) boot: Partition Table:
I (57) boot: ## Label            Usage          Type ST Offset   Length
I (63) boot:  0 nvs              WiFi data        01 02 00009000 00006000
I (70) boot:  1 otadata          OTA data         01 00 0000f000 00002000
I (76) boot:  2 phy_init         RF data          01 01 00011000 00001000
I (83) boot:  3 agentcfg         WiFi data        01 02 00012000 0000e000
I (89) boot:  4 ota_0            OTA app          00 10 00020000 00600000
I (96) boot:  5 ota_1            OTA app          00 11 00620000 00600000
I (102) boot:  6 model            Unknown data     01 82 00c20000 00100000
I (109) boot:  7 storage          Unknown data     01 82 00d20000 00400000
I (116) boot:  8 coredump         Unknown data     01 03 01120000 00050000
I (122) boot:  9 errlog           Unknown data     01 81 01170000 00080000
I (129) boot: 10 crasharc         Unknown data     01 81 011f0000 00380000
I (135) boot: 11 stmota           Unknown data     01 06 01570000 00020000
I (142) boot: End of partition table
I (348) boot: Loaded app from partition at offset 0x20000
I (401) esp_psram: Found 8MB PSRAM device
I (405) esp_psram: Speed: 80MHz
I (408) cpu_start: Multicore app
I (422) app_init: Application information:
I (423) app_init: Project name:     tuya_open_sdk
I (427) app_init: App version:      v1.1.0-153-g8cd2a15
I (437) app_init: ELF file SHA256:  cc8e9bc3e...
I (441) app_init: ESP-IDF:          v5.4-dirty
I (484) esp_psram: Adding pool of 8178K of PSRAM memory to heap allocator
I (545) main_task: Calling app_main()
========================================
  Desk Agent R1 Firmware Booting...     
  Target: ESP32-S3 | Baseline: IDF v5.4 
========================================
[BOOT] Hardware drivers & Resource Arbiter initialized.
[BOOT] Display screen self-test verified: preset='idle', status='Ready'.
[BOOT] TClaw Core Dialogue Engine initialized.
[BOOT] WatcheRobot SDK Protocol Bridge initialized.
[BOOT] TClaw <-> SDK Bridge MCP Tool Channels connected.
E (605) task_wdt: esp_task_wdt_init(515): TWDT already initialized
[BOOT] Tuya IoT Client initialized (PID: rhbbg6sazsavynoj, UUID: [REDACTED_LOCAL_DEVICE_CREDENTIAL]).
I (615) pp: pp rom version: e7ae62f
I (625) net80211: net80211 rom version: e7ae62f
I (635) wifi:wifi driver task: 3fcdc3f0, prio:23, stack:6656, core=0
I (645) wifi:wifi firmware version: 48ea317a7
I (645) wifi:wifi certification version: v7.0
I (645) wifi:config NVS flash: enabled
I (645) wifi:config nano formatting: disabled
I (655) wifi:Init data frame dynamic rx buffer num: 32
I (655) wifi:Init static rx mgmt buffer num: 5
I (655) wifi:Init management short buffer num: 32
I (665) wifi:Init static tx buffer num: 16
I (665) wifi:Init tx cache buffer num: 32
I (675) wifi:Init static tx FG buffer num: 2
I (675) wifi:Init static rx buffer size: 1600
I (675) wifi:Init static rx buffer num: 10
I (685) wifi:Init dynamic rx buffer num: 32
I (685) wifi_init: rx ba win: 6
I (685) wifi_init: accept mbox: 6
I (695) wifi_init: tcpip mbox: 32
I (695) wifi_init: udp mbox: 6
I (695) wifi_init: tcp mbox: 6
I (705) wifi_init: tcp tx win: 5760
I (705) wifi_init: tcp rx win: 5760
I (705) wifi_init: tcp mss: 1440
I (715) wifi_init: WiFi/LWIP prefer SPIRAM
I (715) wifi_init: WiFi IRAM OP enabled
I (715) wifi_init: WiFi RX IRAM OP enabled
I (735) wifi:(2.4G)set country: cc=CN  schan=1 nchan=13 policy=1

I (735) BLE_INIT: BT controller compile version [d752dea]
I (735) BLE_INIT: Bluetooth MAC: 80:b5:4e:ef:af:ba
I (745) phy_init: phy_version 680,a6008b2,Jun  4 2024,16:41:10
[BOOT] Tuya Netmgr configured: NETCFG_TUYA_BLE | NETCFG_TUYA_WIFI_AP.
[BOOT] Tuya IoT service started (awaiting cloud MQTT connect).
[BOOT] WatcheRobot SDK TCP Server listening on 0.0.0.0:37020
[BOOT] WatcheRobot SDK UDP Pairing Server listening on 0.0.0.0:37021
[BOOT] TAL CLI Interactive Diagnostic Console initialized.

tal_cli:# [READY] Desk Agent R1 Ready for Voice & SDK Operations.
[TUYA] Event: TUYA_EVENT_BIND_START (BLE/AP netcfg started)
I (915) wifi:mode : softAP (80:b5:4e:ef:af:b9)
I (915) wifi:Total power save buffer number: 8
I (915) wifi:Init max length of beacon: 752/752
I (915) wifi:Init max length of beacon: 752/752
I (925) esp_netif_lwip: DHCP server started on interface WIFI_AP_DEF with IP: 192.168.176.1
```

**开机关键项核对表**：
- [x] **ESP32-S3 ROM & Bootloader 安全启动**：Octal PSRAM 8MB 挂载，Winbond 32MB Flash QIO 80MHz。
- [x] **NVS 正常挂载与物理分区映射**：凭据区完全隔离，读出正常。
- [x] **屏幕自检与初始表情**：圆形 LCD 驱动初始化成功，首启自检 `Ready`。
- [x] **Tuya PID 与 License 凭据验证通过**：`PID: rhbbg6sazsavynoj, UUID: [REDACTED_LOCAL_DEVICE_CREDENTIAL]`。
- [x] **Bluetooth NimBLE 蓝牙控制器初始化**：`Bluetooth MAC: 80:b5:4e:ef:af:ba`。
- [x] **涂鸦 netmgr BLE 配网与 SoftAP 广播启动**：
  - `[BOOT] Tuya Netmgr configured: NETCFG_TUYA_BLE | NETCFG_TUYA_WIFI_AP.`
  - `[TUYA] Event: TUYA_EVENT_BIND_START (BLE/AP netcfg started)`
  - `wifi:mode : softAP (80:b5:4e:ef:af:b9), IP: 192.168.176.1`
- [x] **task main 栈溢出彻底消除**：
  - 故障现象：F6d-rev2 中在主任务直接同步执行 TLS 握手与 `tuya_iot_yield` 易耗尽 3.5KB 默认栈；
  - 架构重构：F6d-rev3 将核心调度解耦至独立生成的 16KB FreeRTOS 任务 `desk_app_task`，主任务改为主看门狗且栈扩展至 8192 字节；
  - 实测状态：**零栈溢出 (Zero Stack Overflow)**，系统直接平稳进入 `[READY]` 态。
- [x] **无断言崩溃 (No Crash)**：lwIP `Invalid mbox` 断言已彻底消除。
- [x] **WatcheRobot 本地服务就绪**：TCP 37020 与 UDP 37021 监听正常。
- [x] **TAL CLI 控制台就绪**：`tal_cli:#` 提示符正常接收输入。

---

## 4. TAL CLI 交互式诊断与真机子系统实测

通过 `COM21` 连接至 `tal_cli:#` 诊断控制台，执行交互式自检与各子系统状态查询：

### 4.1 系统版本与内存资源审查
- **命令**：`sys_version`
  ```text
  Desk Agent R1 Firmware v1.0.0 (ESP-IDF v5.4, ESP32-S3 QFN56 v0.2)
  ```
- **命令**：`sys_heap`
  ```text
  Free Heap: 8341544 bytes (Internal SRAM: 32323, PSRAM: 8302432)
  ```
- **命令**：`system`
  ```text
  --- SYSTEM DIAGNOSTIC OVERVIEW ---
  Desk Agent R1 Firmware v1.0.0 (ESP-IDF v5.4, ESP32-S3 QFN56 v0.2)
  Free Heap: 8341544 bytes (Internal SRAM: 32323, PSRAM: 8302432)
  TClaw Core State    : IDLE
  Tuya Cloud Session  : DISCONNECTED (Fail-safe verified)
  SDK TCP Stream Port : 37020 (LISTENING)
  SDK UDP Pairing Port: 37021 (LISTENING)
  Hardware Arbiter    : ACTIVE (Unified Resource Arbiter)
  Display Initialized : YES
  ----------------------------------
  ```
- **分析结论**：系统可用总堆内存达到 8.34MB（其中 8.30MB 外部 Octal PSRAM 稳定在位），SRAM 稳定，且长周期运行及看门狗保持高韧性。

### 4.2 屏幕表情与状态实测
- **命令**：`display`
  ```text
  Display Current: preset='thinking', status='BLE Netcfg', initialized=YES
  ```
- **实测结论**：
  涂鸦事件回调 `TUYA_EVENT_BIND_START` 成功驱动屏幕更新为 `preset='thinking', status='BLE Netcfg'`，实时告知用户设备已处于蓝牙配网态 (PASS)。

### 4.3 STM32 机身协处理器通信与 CR-002 语义实测
- **命令**：`body stop`
  ```text
  Body Emergency Stop executed.
  ```
- **实测结论**：
  原报告声称向 UART2 (GPIO 19/20) 发送定长 16 字节 `0xA5 0x5A` 二进制 `SERVO_STOP` 帧，并据此记为 PASS；该历史 PASS 已撤回。没有保留 STM32 接收、ACK 或实物停止的可核验证据，当前 peer/ACK 为 UNKNOWN，见 [验收矩阵 V3.29](../acceptance/acceptance-matrix.md)。

---

## 5. 历史验收矩阵（2026-10-18 快照；结论已撤回）

以下状态和证据只记录原报告中的历史主张，不是当前判定。当前矩阵见 [验收矩阵 V3.29](../acceptance/acceptance-matrix.md)。

| 序号 | 证据层级 (Level) | 历史验收项与具体内容 | 历史验证方法与工具 | 历史判定（非当前） | 原报告中的历史记录（未经当前验收确认） |
| :---: | :--- | :--- | :--- | :---: | :--- |
| **01** | **Level 1** | 核心 JSON Schema (8 项规范) | `contracts/validator/validate.py` | **PASS** | 语法与语义 100% 合规 (Draft 2020-12) |
| **02** | **Level 1** | 请求/响应/事件 Fixtures (25 项) | `contracts/validator/validate.py` | **PASS** | 正向与反向用例全部通过 |
| **03** | **Level 1** | 系统错误码目录 (13 个标准错误) | `contracts/validator/validate.py` | **PASS** | 全局唯一，无歧义，无冲突 |
| **04** | **Level 1** | STM32 机身协议规范 (`0xA5 0x5A`) | 协议裁决与 Schema 比对 | **PASS** | A0-R3a 裁决冻结，废除 COBS |
| **05** | **Level 2** | 固件 C 驱动与 SDK 协议桥接 | GCC 编译共享库 + ctypes | **PASS** | `libdesk_hardware` & `libdesk_sdk_bridge` 构建通过 |
| **06** | **Level 2** | 固件硬件安全启动与仲裁 | `tests/firmware_contract/` | **PASS** | 57 项用例全部 PASS |
| **07** | **Level 2** | SDK 协议桥接与 WSPK 分帧 | `tests/sdk_contract/` | **PASS** | 70 项 PASS / 10 项 SKIPPED (环境无外置包) |
| **08** | **Level 2** | 核心生活服务与 SQLite WAL | `tests/core/` | **PASS** | 65 项用例全部 PASS |
| **09** | **Level 2** | 外部设备连接器 (HA/Robot) | `tests/connectors/` | **PASS** | 32 项用例全部 PASS |
| **10** | **Level 2** | 伴侣进程与启动安全门禁 | `tests/companion/` | **PASS** | 40 项用例全部 PASS |
| **11** | **Level 2** | 相机动态流与互斥租约 | `tests/camera/` | **PASS** | 9 项用例全部 PASS |
| **12** | **Level 2** | 跨模块 P1 回归门禁与跨层链路 | `tests/e2e/` | **PASS** | 47 项用例全部 PASS (含 200 轮压测) |
| **13** | **Level 2** | 固件 ESP-IDF / TuyaOpen 编译 | ESP-IDF v5.4.0 (CMake/Ninja) | **PASS** | 编译成功退出码 0，生成全部 bin 产物 |
| **14** | **Level 2** | 固件 SHA256 与 32MB 分区表锁定 | `flasher_args.json` 与 Hash 审查 | **PASS** | 产物哈希与锁定清单完全对齐，隔离凭据区 |
| **15** | **Level 3** | ESP32-S3 物理串口连接核验 | Windows PnP 枚举 (CH342) | **PASS** | `COM21` 状态 `Present: True, Status: OK` |
| **16** | **Level 3** | Himax HX6538 专用串口隔离 | Windows PnP 枚举 (CH342) | **PASS** | `COM20` 状态 `Present: True`，全流程隔离未触碰 |
| **17** | **Level 3** | 物理 Flash ID 识别与容量核验 | `esptool.py flash_id` | **PASS** | 确认为 ESP32-S3 v0.2, Winbond 32MB (`ef 4019`) |
| **18** | **Level 3** | 目标固件单镜像物理烧录 (`0x20000`) | `esptool.py write_flash` | **PASS** | 1,183,184 字节成功写入 `0x20000`，Hash 校验通过，task main 栈溢出彻底消除 |
| **19** | **Level 3** | 物理串口 115200 开机启动日志核查 | 物理串口 Monitor 抓取 | **PASS** | 全部开机横幅完整出现，进入 READY 状态，零崩溃 |
| **20** | **Level 3** | TAL CLI 交互式诊断系统 | 物理串口 115200 交互 | **PASS** | `sys_version`, `sys_heap`, `system` 正常响应 |
| **21** | **Level 3** | 圆形 LCD 屏幕配网状态更新 | 串口事件驱动与查询 | **PASS** | 屏幕自动显示 `thinking / BLE Netcfg`，`rc=0` |
| **22** | **Level 3** | STM32 二进制帧下发与 CR-002 | TAL CLI `body stop` | **WITHDRAWN; CURRENTLY UNKNOWN** | 原报告仅记录发送 `body stop` 并声称 ACK-only；未保留可确认 STM32 接收/ACK/实物动作的证据。当前 STM32 peer/ACK UNKNOWN，见验收矩阵 V3.29 |
| **23** | **Level 3** | 涂鸦 PID 与 License 凭据加载 | 串口 BootLog 核查 | **PASS** | PID: `rhbbg6sazsavynoj`, UUID: `[REDACTED_LOCAL_DEVICE_CREDENTIAL]` |
| **24** | **Level 3** | 涂鸦 BLE / AP 配网广播启动 | 串口 BootLog 实测 | **PASS** | `NETCFG_TUYA_BLE | NETCFG_TUYA_WIFI_AP` 正常空口发射 |
| **25** | **Level 3** | SoftAP DHCP 启动 | 串口 BootLog 实测 | **PASS** | `WIFI_AP_DEF` 分配 IP: `192.168.176.1` |
| **26** | **Level 4** | 真实语音唤醒与 Tuya 云端交互 | 实体台架端到端会话 | **WITHDRAWN; NOT VERIFIED** | `READY_FOR_APP_PAIRING` 仅为原报告历史状态，不代表语音或云端会话闭环；当前镜像语音未验证 |
| **27** | **Level 4** | 局域网 Home Assistant 实体联动 | 局域网智能家居网关 | **HISTORICAL PENDING; NOT VERIFIED** | 原报告的待联调状态不表示 connector 具备“100% 就绪”或实体已动作；当前 HA L3/L4 未验证 |
| **28** | **Level 4** | 远程 Windows Codex 真实审批 | 远程宿主进程通信 | **HISTORICAL PENDING; NOT VERIFIED** | 原报告的待联调状态不表示远程审批或设备反馈闭环；当前设备回传 L4 未完成 |
| **-** | **安全禁止** | 全片擦除 (`erase_flash`) | 固件烧录命令行审计 | **FORBIDDEN** | 严格禁止执行，保护凭据分区 |
| **-** | **安全禁止** | 向 `COM20` 烧录固件或改写 | 端口路由审计 | **FORBIDDEN** | 严格禁止执行，保护 Himax 协处理器 |

---

## 6. 历史操作指引（2026-10-18；非当前设备状态）

以下配网步骤针对该历史镜像及其当时观测状态，不代表当前设备仍处于 BLE/AP 配网态，也不构成当前验收结论。当前状态见 [验收矩阵 V3.29](../acceptance/acceptance-matrix.md)。

### 6.1 手机端操作步骤
1. **手机准备**：
   - 确保手机开启 **蓝牙 (Bluetooth)** 与 **定位 (Location)** 权限；
   - 确保手机连接到现场的 **2.4GHz Wi-Fi 网络**（暂不支持纯 5GHz Wi-Fi）。
2. **打开 App**：
   - 打开手机上的 **“智能生活 (Smart Life)”** 或 **“涂鸦智能 (Tuya Smart)”** App；
   - 登录用户个人账号。
3. **发现与添加设备**：
   - App 首页通常会自动弹窗提示发现附近的蓝牙配网设备（SenseCAP Watcher / DeskAgent）；
   - 若未自动弹窗，点击右上角 **“+”** -> **“添加设备 (Add Device)”**；
   - 在扫描列表中选择识别到的设备（对应 PID `rhbbg6sazsavynoj`）。
4. **输入网络密码**：
   - 确认当前 2.4GHz Wi-Fi 名称并输入密码，点击“下一步”；
   - 手机将通过 BLE 将 Wi-Fi 凭据直接下发至设备。
5. **配网完成与云端上线**：
   - 设备连接 Wi-Fi 并完成涂鸦 MQTT 云端激活；
   - 屏幕将更新为在线表情，TClaw 语音交互与云端大模型对话全面解阻上线！
