# Desk Agent R1 部署与运维指南 (Deployment & Operations Guide)

本文档面向运维工程师与系统总管家，提供 Desk Agent R1 在各环境（嵌入式机器人、泰山派 Linux 宿主、Windows Companion）的部署、配置、启动参数、监听地址与故障回退方案。

---

## 1. 架构环境拓扑与网络拓扑

```
+-------------------------------------------------------------------------------+
|                      Desk Agent R1 硬件机器人 (ESP32-S3)                       |
|  - 架构: ESP-IDF v5.4 | 双核 Xtensa LX7 @ 240MHz | 16MB Flash | 8MB Octal PSRAM |
|  - TClaw 主框架: Tuya Cloud 语音对话、MCP 工具派发、Audio/Motion/Camera 仲裁   |
|  - SDK 桥接: UDP 37021 (CSPRNG 配对发现), TCP 37020 (全双工控制与 WSPK 媒体通道) |
+---------------------------------------+---------------------------------------+
                                        | Wi-Fi 局域网 TCP/UDP (37020/37021)
                                        v
+-------------------------------------------------------------------------------+
|                    泰山派 RK3566 (Ubuntu 22.04 / Debian 12)                   |
|  - Desk Agent Host Service (Python 3.10+)                                     |
|  - 默认监听地址: 127.0.0.1:8088 (跨设备 Web 查看时显式配置 0.0.0.0:8088 + Token) |
|  - SQLite WAL 持久化: /var/lib/desk-agent/life_service.db                      |
|  - HA Connector: 局域网连接 Home Assistant (REST API + WebSocket 状态订阅)      |
|  - Camera Leased Service: 局域网单帧 JPEG 租约、时效验证与黑帧遮盖防冒充        |
|  - 集中 Router: 接收 TClaw 工具请求信封，异步调度并回传执行结果                 |
+---------------------------------------+---------------------------------------+
                                        | 局域网 HTTP RPC (8765 端口 + Bearer Token)
                                        v
+-------------------------------------------------------------------------------+
|                  Windows 开发机 (Windows 11 / PowerShell 7)                   |
|  - Windows Codex Companion (Python 3.10+)                                     |
|  - 默认监听地址: 127.0.0.1:8765 (跨机被泰山派调用时配置 0.0.0.0:8765 + Token) |
|  - 本地工作区白名单映射: epanda-desk-agent -> D:\ePanda\DeskAgent 等           |
|  - Codex CLI / app-server stdio JSON-RPC 双向驱动与敏感操作人工审批           |
+-------------------------------------------------------------------------------+
```

---

## 2. 泰山派 RK3566 宿主部署步骤

### 2.1 环境准备
- 操作系统: Ubuntu 22.04 LTS aarch64 或 Debian 12 aarch64
- Python 版本: >= 3.10 (推荐 Python 3.11)
- 安装系统依赖:
  ```bash
  sudo apt-get update
  sudo apt-get install -y python3 python3-venv python3-pip sqlite3 curl
  ```

### 2.2 安装应用
```bash
sudo useradd -r -s /bin/false deskagent
sudo mkdir -p /opt/desk-agent /var/lib/desk-agent /etc/desk-agent
sudo chown -R deskagent:deskagent /var/lib/desk-agent /opt/desk-agent

# 复制发布代码至 /opt/desk-agent
sudo cp -r host /opt/desk-agent/
sudo cp -r contracts /opt/desk-agent/
sudo cp -r requirements.txt /opt/desk-agent/

# 创建 Python 虚拟环境并安装依赖
sudo -u deskagent python3 -m venv /opt/desk-agent/venv
sudo -u deskagent /opt/desk-agent/venv/bin/pip install --upgrade pip
sudo -u deskagent /opt/desk-agent/venv/bin/pip install -r /opt/desk-agent/requirements.txt
```

### 2.3 配置与启动参数说明
Host 守护进程支持以下命令行参数与配置文件选项：

- **命令行参数**：
  - `--config`, `-c`: 指定配置文件绝对路径（默认自动搜索 `/etc/desk-agent/config.json` 或 `./config.json`）。
  - `--port`, `-p`: 显式覆盖 HTTP 监听端口（优先级高于配置文件）。
- **网络监听地址与鉴权安全策略**：
  - **默认绑定回环**：代码默认绑定 `127.0.0.1:8088`，实现本机安全隔离。
  - **跨设备访问**：若需从局域网手机/PC 浏览器打开 Web 控制台或接收远程请求，必须在配置中将 `server.host` 显式设为 `0.0.0.0`，并配置全局 `auth_token`。
  - **鉴权支持方式**：开启 Token 后，客户端可通过以下任一方式传递凭据：
    1. HTTP 报头：`Authorization: Bearer <AUTH_TOKEN>`
    2. HTTP 报头：`X-Desk-Agent-Token: <AUTH_TOKEN>`
    3. URL Query 参数：`?token=<AUTH_TOKEN>`（用于浏览器 `<img src="...">` 标签拉流）

```bash
# 复制配置文件模板
sudo cp deploy/edge/config.example.json /etc/desk-agent/config.json
sudo chmod 600 /etc/desk-agent/config.json
sudo chown deskagent:deskagent /etc/desk-agent/config.json

# 编辑配置：填入 HA Token、Windows Companion 地址与 Token
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
如果更新版本异常，执行快速回退：
```bash
sudo systemctl stop desk-agent
sudo cp -r /opt/desk-agent.bak/* /opt/desk-agent/
sudo systemctl restart desk-agent
```
数据库文件保持位于 `/var/lib/desk-agent/life_service.db`，由 SQLite WAL 模式保证向前/向后事务兼容与崩溃安全。

---

## 3. Windows Codex Companion 部署步骤

### 3.1 环境要求
- Windows 10 / 11 64-bit
- Python >= 3.10
- OpenAI Codex CLI 或兼容环境（可正常执行 `codex` 命令）
- PowerShell 7 (或 Windows PowerShell 5.1)

### 3.2 配置与运行
1. 进入项目根目录 `companion/`。
2. 复制配置模板（`run-companion.ps1` **不再自动复制**，必须显式执行）：
   ```powershell
   Copy-Item deploy\windows\companion-config.example.json deploy\windows\companion-config.json
   ```
   > 模板默认 `host: 127.0.0.1` 且 `auth_token` 为空，即**默认仅回环、不对外暴露**。这是安全默认值，可直接使用。
3. 在 `companion-config.json` 中配置安全参数：
   - `host`: 默认回环 `127.0.0.1`。需供泰山派跨局域网访问时设置为 `0.0.0.0`。
   - `port`: 默认 `8765`。
   - `auth_token`: **跨局域网暴露时强制设置强密钥**（**至少 16 字符**），防止未经授权的远程执行。
   - `registered_workspaces`: 严格配置白名单别名（例如将 `epanda-desk-agent` 映射为实际目录 `D:\ePanda\DeskAgent`，严禁外部传入原始盘符路径）。
4. **启动前自检（可选但推荐）**：
   ```powershell
   python -m companion.src.codex_companion --config deploy\windows\companion-config.json --check-config
   ```
   该命令仅打印 `{status, host, port, loopback_only, auth_required}`（**绝不回显 token**），校验通过返回 0，否则返回 2。
5. 运行 Companion 服务：
   ```powershell
   powershell -ExecutionPolicy Bypass -File deploy\windows\run-companion.ps1
   ```
   > **fail-closed 强制校验**：当 `host` 为非回环地址时，若 `auth_token` 为空、过短（<16 字符）或为仓库中公开的占位令牌，
   > 服务将**拒绝启动**并以非零码退出，**绝不静默降级为无鉴权监听**。
6. 防火墙配置：若设置为 `0.0.0.0`，在 Windows 高级安全防火墙中放行 TCP 8765 端口入站规则（仅限家庭/受信任子网）。

### 3.3 登录自启动注册与服务管理 (Task Scheduler)
为保证日常使用中 Companion 随 Windows 登录无感启动，项目提供无管理员特权要求的 Task Scheduler 注册脚本：
```powershell
# 注册开机登录后台自启动（默认最小化静默运行）
powershell -ExecutionPolicy Bypass -File deploy\windows\register-autostart.ps1

# 同时将 Host 进程注册为 Windows 自启动（可选，适用于 PC 单机全栈运行场景）
powershell -ExecutionPolicy Bypass -File deploy\windows\register-autostart.ps1 -IncludeHost

# 取消自启动任务
powershell -ExecutionPolicy Bypass -File deploy\windows\unregister-autostart.ps1
```

### 3.4 Windows 本地 Host 服务启动 (可选开发/全单机模式)
除泰山派部署外，Host 守护进程亦可直接在 Windows 上运行：
```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\run-host.ps1
```

### 3.5 一键连接引导与诊断工具 (Setup Wizard)
在完成固件与 Host/Companion 配置后，使用诊断脚本校验全链路连通性：
```powershell
powershell -ExecutionPolicy Bypass -File deploy\windows\setup-connection.ps1
```
诊断向导依次检查：
1. Python 运行环境与版本
2. Codex CLI 工具状态
3. Companion 配置文件与安全策略
4. Companion 8765 端口健康检查
5. Host 8088 端口健康检查
6. 机器人 37020 端口连通状态与 Wi-Fi 建议

---

## 4. ESP32-S3 机器人固件构建与烧录

### 4.1 工具链基线
- 芯片型号: **ESP32-S3-WROOM-1 / N16R8** (16MB SPI Flash, 8MB Octal PSRAM)
- 固件依赖基线: **ESP-IDF v5.4**（TuyaOpen 官方支持环境）
- Python 编译工具链环境: Python 3.10 ~ 3.12 (ESP-IDF 5.4 官方要求)

### 4.2 本地原生仿真编译测试（Linux Host 模拟器，Level 2）
项目内置了 Linux C 模拟构建环境，用于在无需物理芯片时验证资源仲裁器、WSPK 封包与 Tuya 云状态机协议逻辑：
```bash
cmake -B firmware/build -S firmware
cmake --build firmware/build
# 运行单元自测与轮询任务
./firmware/build/desk_agent_firmware_test --poll
```

### 4.3 物理设备编译与烧录（ESP-IDF，Level 3 待现场实测）
> [!IMPORTANT]
> 烧录与物理实测需当次明确授权。主机环境必须先激活匹配的 Python 3.10~3.12 ESP-IDF v5.4 虚拟环境。

```bash
cd firmware
# 导出 IDF 工具链环境
. $IDF_PATH/export.sh

# 设置目标芯片并构建
idf.py set-target esp32s3
idf.py build

# 烧录固件至物理设备并观察串口启动日志
idf.py -p /dev/ttyUSB0 flash monitor
```

---

## 5. 故障恢复与日常巡检 (Health Check)

| 巡检项目 | 检查命令 / 接口 | 预期状态 | 异常处置 |
|---|---|---|---|
| 宿主进程状态 | `systemctl is-active desk-agent` | `active` | 检查 `/var/log/journal` 日志，重启服务 |
| 数据库一致性 | `sqlite3 /var/lib/desk-agent/life_service.db "PRAGMA integrity_check;"` | `ok` | 若损坏使用 `.recover` 从 WAL 恢复 |
| 宿主健康检查 | `curl -s http://127.0.0.1:8088/health` | `{"status":"ok"}` | 检查服务是否挂起或端口占用 |
| 相机租约服务 | `curl -s http://127.0.0.1:8088/api/camera/frame` | 返回 403 (未持有效租约) 或 JPEG | 检查租约超时配置及摄像头硬件采集源 |
| HA 连通性 | 发送 `ha.get_state` 请求 | 响应 `succeeded` | 检查局域网网络路由与 Long-lived Token 有效期 |
| Codex Companion | `curl -s http://<win-ip>:8765/health` | `{"status":"ok"}` | 检查 Windows 服务是否存活及防火墙入站规则 |
| 机器人 SDK 连接 | 检查 Host 日志 `Connected to Robot SDK Bridge` | 已连接 | 检查机器人 Wi-Fi IP 与 UDP 37021 / TCP 37020 端口 |
