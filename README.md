# Desk Agent R1

TClaw 驱动的桌面机器人：连接 Tuya 云端对话、Home Assistant 与 Windows Codex，支持提醒、备忘、表情动作和局域网摄像头查看。

当前是产品资料、技术评审和实施计划仓库，尚未交付整机运行代码。接口和公开源码证据不代表目标设备已验证。

## 当前架构

机器人 TClaw + 全硬件适配 → 官方 Python SDK/Runtime → 常驻 Python 服务 → HA / Windows Codex。

- TClaw 是设备端主框架，连接 Tuya 官方云端模型；不默认在 Python 侧再建立替代对话系统。
- 新固件需要兼容官方 SDK；TClaw 与 SDK 同时工作是首要验证关口。
- 先在 Windows 联调，再验证泰山派 Linux ARM 常驻部署。
- 摄像头首期仅局域网查看与手动云台，无录像、回放、远程查看和监控音频。
- 表情优先；当前重点是打通功能链路，界面文件仅为交互示意。

## 阅读顺序

1. [最新系统概览](docs/architecture/system-overview.md)
2. [实施计划：A0–A7 分工与 G0–G4 验收](docs/superpowers/plans/2026-10-11-desk-agent-system.md)
3. [下一位 agent 派发提示词](docs/handoff/next-agent-prompt.md)
4. [协作规范](AGENTS.md)
5. [技术总评](docs/research/Desk_Agent_R1_技术可行性总评与任务分解.md)、[硬件与 SDK](docs/research/hardware_feasibility.md)、[Codex 与语音](docs/research/codex_voice_feasibility.md)、[HA 与生活服务](docs/research/ha_services_feasibility.md)

最新架构与实施计划优先于旧草案和早期技术路线建议。技术报告保留其核验时的事实，但其中“现有固件优先”等建议已经被 TClaw 新固件主路线替代。原始文档是参考资料，不是当前执行指令。

## 代码与资料目录

- `firmware/`：机器人固件源码与组件（ESP32-S3 / IDF v5.4 / TClaw 核心、WatcheRobot SDK 兼容桥接、硬件 HAL 驱动、统一仲裁器）。内置 Linux 模拟构建支持。
- `host/`：常驻 Python 宿主服务（泰山派 RK3566 / Linux）：
  - `core/`：通用请求/响应/事件信封、幂等分发路由器。
  - `services/`：生活服务（SQLite WAL 持久化、提醒、闹钟、便签、推迟解耦）。
  - `connectors/`：Home Assistant 局域网接入器（域名白名单、指令下发与真实状态确认）。
  - `camera/`：局域网短时查看服务（租约互斥仲裁、新鲜度检测、Stale 拦截）。
  - `web/`：局域网响应式查看与手动云台操作控制台。
- `companion/`：Windows Codex Companion（工作区目录白名单映射、Turn 会话管理、敏感操作审批闭环）。
- `contracts/`：跨模块公共契约（JSON Schemas Draft 2020-12、13 个稳定错误码、25 个正反向 fixtures 与自动化验证器）。
- `deploy/`：部署清单与配置文件示例（泰山派 systemd unit、Windows PowerShell 启动器、配置模板）。
- `tests/`：自动化测试套件（单元测试、端到端集成测试，18/18 测试通过）。
- `docs/acceptance/`：系统分层验收矩阵与现场硬件缺口台账（[acceptance-matrix.md](docs/acceptance/acceptance-matrix.md)）。
- `docs/deployment/`：生产与运维部署指南（[deployment-guide.md](docs/deployment/deployment-guide.md)）。
- `docs/implementation/`：实施规范（基线台账 baseline.md、公共接口 interfaces.md、G1 贯通方案 g1-validation.md）。
- `docs/research/`：三个专项研究与技术总评。
- `docs/architecture/`：最新系统架构文档。

## 验证与测试

```bash
# 1. 契约规范与 fixtures 深度校验
python3 contracts/validator/validate.py

# 2. 固件 C 仲裁器与协议状态机构建测试 (Linux Host GCC)
cmake -B firmware/build -S firmware && cmake --build firmware/build && ./firmware/build/desk_agent_firmware_test

# 3. 运行全套 Python 核心、HA、Codex、Camera 与 E2E 自动化测试
python3 -m unittest discover -s tests -t . -p "test_*.py"
```

## 公开参考项目

- [Tuya TClaw](https://github.com/tuya/TClaw)
- [WatcheRobot 嵌入式 SDK](https://github.com/orulink-ai/WatcheRobot-public)
- [WatcheRobot Python SDK](https://github.com/orulink-ai/WatcheRobot_python_sdk)
- [Xepanda 个人资料库](https://github.com/Xepanda/wiki)

本仓库归档产品资料与研究链接，不复制第三方完整源码或改动其许可证。
