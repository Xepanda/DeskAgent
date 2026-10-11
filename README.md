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

## 资料目录

- docs/source/：原始 V1.0 Word 文档，原件内容不修改。
- docs/：V2.0/V2.1 草案与早期能力核验记录。
- docs/research/：三个专项研究与技术总评。
- docs/architecture/：最新系统架构。
- docs/implementation/：实施规范（基线台账 baseline.md、公共接口 interfaces.md、G1 贯通方案 g1-validation.md）。
- contracts/：跨模块公共契约（JSON Schemas、稳定错误码、可校验 fixtures 与自动化校验器）。
- docs/superpowers/plans/：多 agent 实施计划。
- docs/handoff/：下一阶段派发提示词。
- docs/prototypes/：两种交互布局 HTML，依赖原可视化宿主，不是独立产品页面。
- img/：历史系统框图参考图；最新架构以 docs/architecture/ 为准。

未来代码路径只是计划中的 ownership 建议，A0 已定位实际源码仓库并冻结映射。

## 首轮目标

G0 基线与公共契约已完成制定与校验冻结（见 [baseline.md](docs/implementation/baseline.md) 与 [contracts/](contracts/)）。下一步由 A1/A2 启动 G1 最小贯通实验，证明 TClaw 云端对话与官方 Python SDK 同时工作；接口冻结后并行开展 HA、Codex 和生活服务。尚无固件烧录、泰山派部署或端到端验收证据。

## 公开参考项目

- [Tuya TClaw](https://github.com/tuya/TClaw)
- [WatcheRobot 嵌入式 SDK](https://github.com/orulink-ai/WatcheRobot-public)
- [WatcheRobot Python SDK](https://github.com/orulink-ai/WatcheRobot_python_sdk)
- [Xepanda 个人资料库](https://github.com/Xepanda/wiki)

本仓库归档产品资料与研究链接，不复制第三方完整源码或改动其许可证。
