# Desk Agent R1 编译产物与目标固件 (Release Dist Artifacts)

> **固件版本**：V1.0.0 (Release Candidate - PR #14 闭环版)  
> **构建基线**：ESP-IDF 5.4.0 + TuyaOpen SDK (Target: ESP32-S3, 32MB Flash)  
> **构建日期**：2026-10-20  
> **校验状态**：已锁定 SHA256 校验和

---

## 文件清单 (Artifacts)

| 文件名 | 大小 | 说明 |
| :--- | :--- | :--- |
| `desk_agent_firmware.bin` | 1.8 MB | **主应用分区镜像**（烧录至 `0x20000`）。推荐开发与日常调试使用，严格保留设备原始出厂凭据（0x9000 nvs, 0x11000 phy, 0x12000 agentcfg）。 |
| `desk_agent_firmware_QIO_1.0.0.bin` | 30.6 MB | **全量合并镜像**（包含 bootloader, partition-table, ota_data, app, srmodels），适用于全新空片全盘出厂量产烧录（烧录至 `0x0`）。 |
| `bootloader.bin` | 21 KB | ESP32-S3 二级引导程序（烧录至 `0x0`）。 |
| `partition-table.bin` | 3 KB | 32MB Flash 物理分区表定义（烧录至 `0x8000`）。 |
| `ota_data_initial.bin` | 8 KB | OTA 引导数据扇区（烧录至 `0xd000`）。 |
| `srmodels.bin` | 291 KB | 离线离手语音唤醒/命令词模型分区（烧录至 `0x1cf0000`）。 |
| `desk_agent_firmware_1.0.0.elf` | 17.6 MB | 带有完整调试符号与 DWARF 信息的 ELF 可执行文件，用于 GDB 调试与崩溃回溯（addr2line）。 |
| `desk_agent_firmware_1.0.0.map` | 15.7 MB | 完整内存链接映射符号表，用于验证内部 SRAM 与 PSRAM 变量布局。 |
| `SHA256SUMS.txt` | - | 本目录下所有二进制产物的标准 SHA256 校验清单。 |

---

## 烧录指南 (Flashing Guide)

### 方法 A：推荐安全单分区烧录（保留设备云授权与 Wi-Fi 凭据）
```powershell
# 需确保设备串口连接（例如 COM21）
tuyaopen-cli flash --port COM21 --baud 921600 --file firmware/dist/desk_agent_firmware_1.0.0/desk_agent_firmware.bin --address 0x20000
```

### 方法 B：esptool 烧录单分区或全部分区
```powershell
# 单分区更新
python -m esptool --chip esp32s3 -b 921600 write_flash 0x20000 firmware/dist/desk_agent_firmware_1.0.0/desk_agent_firmware.bin

# 全量出厂烧录（擦写整片）
python -m esptool --chip esp32s3 -b 921600 write_flash 0x0 firmware/dist/desk_agent_firmware_1.0.0/desk_agent_firmware_QIO_1.0.0.bin
```

---

## 串口监视与运行观测
```powershell
tuyaopen-cli monitor --port COM21 --baud 115200
```
启动时在串口终端中应观察到：
- `[BOOT] Hardware drivers & Resource Arbiter initialized.`
- `[BOOT] TClaw Core Dialogue Engine initialized.`
- `[BOOT] WatcheRobot SDK Protocol Bridge initialized.`
- `[BOOT] TClaw <-> SDK Bridge MCP Tool Channels connected.`
- `[BOOT] stage=host_cli_ready`
- `[BOOT] stage=tuya_iot_initialized`
- 圆屏点亮并呈现 Bloub 矢量大眼表情。
