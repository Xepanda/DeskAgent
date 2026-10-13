# Desk Agent R1 部署与运维指南 (Deployment & Operations Guide)

本文档面向运维工程师与系统总管家，提供 Desk Agent R1 在各环境（嵌入式机器人、泰山派 Linux 宿主、Windows Companion）的部署、配置、验证与故障回退方案。

---

## 1. 架构环境拓扑

```
+------------------------------------------------------------------+
|                   Desk Agent R1 硬件机器人                       |
|   (ESP32-S3 | IDF v5.4 | TClaw 核心对话 + WatcheRobot SDK 兼容)  |
+---------------------------------+--------------------------------+
                                  | Wi-Fi / LAN RPC
                                  v
+------------------------------------------------------------------+
|               泰山派 RK3566 (Ubuntu/Debian Linux)                |
|  - Desk Agent Host Service (Python 3.10+)                        |
|  - SQLite WAL (提醒、闹钟、便签持久化)                           |
|  - HA Connector (局域网直连 Home Assistant REST/WS)             |
|  - Camera Leased Service & Web Console (端口 8088)              |
+---------------------------------+--------------------------------+
                                  | 局域网 HTTP RPC
                                  v
+------------------------------------------------------------------+
|            Windows 开发机 (Windows 11 / PowerShell 7)            |
|  - Windows Codex Companion (端口 8765)                           |
|  - 本地工作区隔离白名单映射 (D:\ePanda\DeskAgent 等)              |
|  - Codex CLI / Turn 会话执行与审批流交互                          |
+------------------------------------------------------------------+
```

---

## 2. 泰山派 RK3566 宿主部署步骤

### 2.1 环境准备
- 操作系统: Ubuntu 22.04 LTS aarch64 或 Debian 12 aarch64
- Python 版本: >= 3.10
- 安装系统依赖:
  ```bash
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip sqlite3
  ```

### 2.2 安装应用
```bash
sudo useradd -r -s /bin/false deskagent
sudo mkdir -p /opt/desk-agent /var/lib/desk-agent /etc/desk-agent
sudo chown -R deskagent:deskagent /var/lib/desk-agent /opt/desk-agent

# 复制发布代码至 /opt/desk-agent
sudo cp -r host /opt/desk-agent/
sudo cp -r contracts /opt/desk-agent/

# 创建 Python 虚拟环境并安装依赖
sudo -u deskagent python3 -m venv /opt/desk-agent/venv
sudo -u deskagent /opt/desk-agent/venv/bin/pip install --upgrade pip
sudo -u deskagent /opt/desk-agent/venv/bin/pip install requests jsonschema
```

### 2.3 配置与启动
```bash
# 复制配置文件
sudo cp deploy/edge/config.example.json /etc/desk-agent/config.json
sudo chmod 600 /etc/desk-agent/config.json
sudo chown deskagent:deskagent /etc/desk-agent/config.json
# 编辑配置，填入 HA Access Token 等必要信息
sudo nano /etc/desk-agent/config.json

# 安装并启动 systemd 服务
sudo cp deploy/edge/desk-agent.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable desk-agent
sudo systemctl start desk-agent

# 检查服务状态与日志
sudo systemctl status desk-agent
sudo journalctl -u desk-agent -f
```

### 2.4 回退方案 (Rollback)
如果更新版本异常，执行回退：
```bash
sudo systemctl stop desk-agent
sudo cp -r /opt/desk-agent.bak/* /opt/desk-agent/
sudo systemctl restart desk-agent
```
数据库文件保持位于 `/var/lib/desk-agent/life_service.db`，由 SQLite WAL 保证向前/向后事务兼容。

---

## 3. Windows Codex Companion 部署步骤

### 3.1 环境要求
- Windows 10 / 11 64-bit
- Python 3.10+
- OpenAI Codex CLI 或兼容环境

### 3.2 配置与运行
1. 进入项目根目录 `companion/`。
2. 复制配置模板：
   ```powershell
   Copy-Item deploy\windows\companion-config.example.json deploy\windows\companion-config.json
   ```
3. 在 `companion-config.json` 中配置安全白名单工作区路径（例如将 `epanda-desk-agent` 映射为实际目录 `D:\ePanda\DeskAgent`）。
4. 运行 Companion 服务：
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\windows\run-companion.ps1
   ```
5. 服务将绑定在 `0.0.0.0:8765` 监听来自泰山派的 Codex 任务请求。

---

## 4. ESP32-S3 机器人固件构建与烧录

### 4.1 工具链基线
- ESP-IDF: **v5.4**
- 目标芯片: **ESP32-S3-WROOM-1** (16MB Flash, 8MB PSRAM)
- TuyaOpen SDK: 接入基准

### 4.2 本地编译测试（Linux Host 模拟器）
项目内置了 Linux C 模拟构建环境，用于在无需物理芯片时验证仲裁器与协议状态机：
```bash
cmake -B firmware/build -S firmware
cmake --build firmware/build
./firmware/build/desk_agent_firmware_test
```

### 4.3 物理设备编译与烧录（ESP-IDF）
```bash
cd firmware
idf.py set-target esp32s3
idf.py build
# 烧录固件至物理设备
idf.py -p /dev/ttyUSB0 flash monitor
```

---

## 5. 故障恢复与日常巡检 (Health Check)

| 巡检项目 | 检查命令 / 接口 | 预期状态 | 异常处置 |
|---|---|---|---|
| 宿主进程状态 | `systemctl is-active desk-agent` | `active` | 检查 `/var/log/journal` 日志，重启服务 |
| 数据库一致性 | `sqlite3 /var/lib/desk-agent/life_service.db "PRAGMA integrity_check;"` | `ok` | 若损坏使用 `.recover` 从 WAL 恢复 |
| 相机租约服务 | `curl http://localhost:8088/api/camera/frame` | 返回 403 (未持有效租约) 或 JPEG | 检查租约超时配置 |
| HA 连通性 | 发送 `ha.get_state` 请求 | 响应 `SUCCEEDED` | 检查局域网网络路由与 Token 有效期 |
| Codex Companion | `curl http://<win-ip>:8765/health` | `{"status":"ok"}` | 检查 Windows 防火墙 8765 端口入站规则 |
