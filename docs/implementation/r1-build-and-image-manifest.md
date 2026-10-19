> 历史阶段资料：当前状态、已验证范围与剩余风险以 [2026-10-19 交付文档](handoff-2026-10-19.md) 为准；本文件不构成新的执行授权或整体验收通过证明。

# DeskAgent R1 公共构建、依赖锁定与分区安全镜像清单报告 (R1 / A0)

> **报告版本**：v1.0.0（R1 编译与镜像清单正式版）  
> **执行角色**：A0 架构与公共构建 Orchestrator  
> **核查日期**：2026-10-18  
> **目标工作区**：`D:\ePanda\DeskAgent`  
> **前置依赖**：R0 冻结结论（`docs/implementation/f0-baseline-verification-report.md`、`contracts/dependency_lock.json`）  
> **构建判定**：**R1 验收通过 (PASS - VERIFIED)**，32MB 分区安全映射已固化，固件镜像全量编译成功并锁定 SHA256。

---

## 1. 工作区与 Git 状态核验

### 1.1 仓库与 HEAD 状态
- **工作区绝对路径**：`D:\ePanda\DeskAgent`
- **Git HEAD Commit**：`9472d70` (`Merge pull request #11 from Xepanda/feat/real-device-integration-p1-remediation`)
- **分支**：`main`（与 `origin/main` 保持一致）
- **脏工作区保护**：严格遵守 `AGENTS.md` 规范，用户现有未提交文件（A1/A2 私有文件）完整保留，未执行任何破坏性 Git 操作（无 reset/clean）。

---

## 2. 真实 32MB 分区表布局与安全保护边界核验

根据 R0 实机识别证据（Winbond W25Q256 32MB Flash），对 `firmware/partitions.csv` 进行了严格的分区边界与扇区对齐验证，并通过 `gen_esp32part.py` 反编译生成的 `partition-table.bin` 进行了 100% 比对：

### 2.1 32MB 真实 Flash 物理分区映射表

| 分区名 | 类型 (Type) | 子类型 (SubType) | 偏移地址 (Offset) | 分区大小 (Size) | 占用扇区范围 | 保护属性与安全边界声明 |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **bootloader** | - | - | `0x00000000` | 22.4 KB (占用) | `0x00000 ~ 0x057A0` | 引导程序，位于 `0x8000` 之前 |
| **partition_table** | - | - | `0x00008000` | 3.0 KB (占用) | `0x08000 ~ 0x08C00` | 分区表扇区，位于 `0x9000` 之前 |
| **`nvs`** | `data` | `nvs` | **`0x00009000`** | 24 KB (`0x6000`) | `0x09000 ~ 0x0EFFF` | **STRICTLY_PROTECTED_READ_ONLY**<br/>包含出厂 Wi-Fi 校准与系统 NVS，**严禁擦写** |
| **otadata** | `data` | `ota` | `0x0000F000` | 8 KB (`0x2000`) | `0x0F000 ~ 0x10FFF` | OTA 引导指针扇区（指示启动至 `ota_0`） |
| **`phy_init`** | `data` | `phy` | **`0x00011000`** | 4 KB (`0x1000`) | `0x11000 ~ 0x11FFF` | **STRICTLY_PROTECTED_READ_ONLY**<br/>包含 RF 物理射频出厂校准参数，**严禁擦写** |
| **`agentcfg`** | `data` | `nvs` | **`0x00012000`** | 56 KB (`0xE000`) | `0x12000 ~ 0x1FFFF` | **STRICTLY_PROTECTED_READ_ONLY**<br/>包含设备云激活凭据/Tuya License/AuthKey，**严禁擦写** |
| **`ota_0`** | `app` | `ota_0` | **`0x00020000`** | 6 MB (`0x600000`) | `0x20000 ~ 0x61FFFF` | **APP_FLASH_TARGET**<br/>主固件目标烧录槽位（当前固件仅占 791.8KB，余 87.1%） |
| **`ota_1`** | `app` | `ota_1` | `0x00620000` | 6 MB (`0x600000`) | `0x620000 ~ 0xC1FFFF` | 备用固件 OTA 槽位 |
| **model** | `data` | `spiffs` | `0x00C20000` | 1 MB (`0x100000`) | `0xC20000 ~ 0xD1FFFF` | AI 视觉模型存储分区 |
| **storage** | `data` | `spiffs` | `0x00D20000` | 4 MB (`0x400000`) | `0xD20000 ~ 0x111FFFF`| UI 资源与动画资产存储分区 |
| **coredump** | `data` | `coredump`| `0x01120000` | 320 KB (`0x50000`)| `0x1120000 ~ 0x116FFFF`| 崩溃转储分析分区 |
| **errlog** | `data` | `fat` | `0x01170000` | 512 KB (`0x80000`)| `0x1170000 ~ 0x11EFFFF`| 系统错误持久化日志 |
| **crasharc** | `data` | `fat` | `0x011F0000` | 3.5 MB (`0x380000`)|`0x11F0000 ~ 0x156FFFF`| 历史崩溃归档 |
| **stmota** | `data` | `undefined`| `0x01570000`| 128 KB (`0x20000`)| `0x1570000 ~ 0x158FFFF`| STM32 协处理器 OTA 固件缓存槽位 |

### 2.2 三大受保护凭据分区 100% 零覆盖安全保证
> [!CAUTION]
> **绝对保护红线核验**：
> 1. `0x09000` (`nvs`)：占用空间 `0x9000 ~ 0xEFFF`。
> 2. `0x11000` (`phy_init`)：占用空间 `0x11000 ~ 0x11FFF`。
> 3. `0x12000` (`agentcfg`)：占用空间 `0x12000 ~ 0x1FFFF`。
>
> **核验结论**：
> - 引导扇区 `bootloader.bin` 止于 `0x057A0`，距离 `0x9000` 留有 14.2KB 安全间隙；
> - 分区表扇区 `partition-table.bin` 止于 `0x08C00`，距离 `0x9000` 留有 1.0KB 安全间隙；
> - OTA 引导指针 `ota_data_initial.bin` 严格占用 `0xF000 ~ 0x10FFF`，不侵入 `0x9000` 或 `0x11000`；
> - 目标主程序 `desk_agent_firmware.bin` 烧录目标偏移**严格锁定为 `0x20000`**，占用 `0x20000 ~ 0xE5F30`，位于 `0x12000~0x1FFFF` 凭据区之后！
> - **严禁执行 `erase_flash`**，严禁向 `0x9000`、`0x11000`、`0x12000` 发送任何写入指令！

---

## 3. 公共构建环境与可复现编译验证

### 3.1 工具链与环境配置
- **操作系统**：Windows 11 Pro (build 26200)
- **ESP-IDF 平台基线**：ESP-IDF v5.4.0（位于 `C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\esp-idf`）
- **编译器**：`xtensa-esp32s3-elf-gcc` (esp-14.2.0_20241119)
- **构建系统**：CMake 3.30.2, Ninja 1.12.1
- **Python 运行时**：Python 3.12 虚拟环境（位于 `C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\python_env\idf5.4_py3.12_env`）

### 3.2 标准可复现构建命令
在 `D:\ePanda\DeskAgent` 目录下，在 PowerShell 中执行以下命令：

```powershell
$env:IDF_TOOLS_PATH = "C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif"
$env:IDF_PYTHON_ENV_PATH = "C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\python_env\idf5.4_py3.12_env"
$env:IDF_PATH = "C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\esp-idf"
$env:PATH = "C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\python_env\idf5.4_py3.12_env\Scripts;C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\tools\ninja\1.12.1;C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\tools\cmake\3.30.2\bin;C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\tools\xtensa-esp-elf\esp-14.2.0_20241119\xtensa-esp-elf\bin;C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\tools\idf-exe\1.0.3;" + $env:PATH

idf.py -C firmware build
```

### 3.3 构建执行结果与退出码
- **执行退出码**：`0` (Success)
- **生成产物耗时**：约 2 分钟完成全量增量编译与镜像打包
- **编译日志摘要**：
```text
[1035/1037] Linking CXX executable desk_agent_firmware.elf
[1036/1037] Generating binary image from built executable
esptool.py v4.12.0
Creating esp32s3 image...
Merged 2 ELF sections
Successfully created esp32s3 image.
Generated D:/ePanda/DeskAgent/firmware/build/desk_agent_firmware.bin
[1037/1037] Check sizes
desk_agent_firmware.bin binary size 0xc5f30 bytes. Smallest app partition is 0x600000 bytes. 0x53a0d0 bytes (87%) free.
Project build complete.
```

---

## 4. 固化镜像清单 (Firmware Image Manifest)

所有产物均位于 `firmware/build/` 目录下，经 SHA256 算法计算锁定：

| 镜像角色 / 段名称 | 文件相对路径 | 目标 Flash 偏移 | 文件大小 (字节 / KB) | SHA256 校验哈希 | 烧录动作与安全限制 |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **Bootloader** | `firmware/build/bootloader/bootloader.bin` | `0x0` | 22,432 字节 (21.9 KB) | `04dcc4c045bbfea3a420181afda4eab289091a897f33cce170de4ce807ce0016` | 首次初始化或恢复时写入 |
| **Partition Table** | `firmware/build/partition_table/partition-table.bin` | `0x8000` | 3,072 字节 (3.0 KB) | `3704ac472b5b5608d43afbffa2954763e4f7eee179c7d9c40394f3caf9fab9b9` | 固化 32MB 布局，首次写入 |
| **OTA Data** | `firmware/build/ota_data_initial.bin` | `0xf000` | 8,192 字节 (8.0 KB) | `7d2c7ac4888bfd75cd5f56e8d61f69595121183afc81556c876732fd3782c62f` | 初始化 OTA 指针指向 `ota_0` |
| **App Firmware** | `firmware/build/desk_agent_firmware.bin` | **`0x20000`** | 810,800 字节 (791.8 KB) | `565afbe425f5b8220a259a30e960634862d3db74798d3d7d03d98150eeb65a59` | **常规迭代/首启唯一烧录目标** |

---

## 5. 交付给下游角色的接口声明

### 5.1 交付给 A1（R2 硬件驱动、R3 单一硬件烧录执行者）
1. **唯一目标烧录端口**：严格锁定 `--port COM21`，波特率 `460800`（烧录）/ `115200`（控制台监视）。
2. **COM20 绝对保护**：`COM20` 是 Himax HX6538 视觉协处理器串口，**严禁向 COM20 发送任何写指令、烧录指令或更改波特率**。
3. **安全首启单镜像烧录命令**（仅刷写 App，100% 隔离凭据区）：
```powershell
& "C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\python_env\idf5.4_py3.12_env\Scripts\esptool.exe" --chip esp32s3 --port COM21 -b 460800 --before default_reset --after hard_reset write_flash 0x20000 firmware\build\desk_agent_firmware.bin
```
4. **全套四段安全烧录命令**（仅在必须完整初始化分区时使用，仍严禁全片擦除）：
```powershell
& "C:\Users\maida\TuyaOpenIDE\TuyaOpenSDK\platform\ESP32\.espressif\python_env\idf5.4_py3.12_env\Scripts\esptool.exe" --chip esp32s3 --port COM21 -b 460800 --before default_reset --after hard_reset write_flash --flash_mode dio --flash_size 32MB --flash_freq 80m 0x0 firmware\build\bootloader\bootloader.bin 0x8000 firmware\build\partition_table\partition-table.bin 0xf000 firmware\build\ota_data_initial.bin 0x20000 firmware\build\desk_agent_firmware.bin
```

### 5.2 交付给 A2（R5 协议路由与 SDK Bridge）
- 目标固件中已整合 `desk_sdk_bridge`，顶层 type 路由机制在固件映像中已具备符号支持。

### 5.3 交付给 A7（验证与测试）
- 镜像产物哈希与分区表二进制与 `dependency_lock.json` 保持严格一致，可作为后续自动化冒烟测试与完整性比对的标准基线。
