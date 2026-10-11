# 下一位 Agent 派发提示词

## 首轮 A0 / G0：完整复制以下内容

```text
你负责 Desk Agent R1 的 A0 架构与协议工作包，本次只完成 G0 基线与公共契约，不启动整套产品开发。

仓库：https://github.com/Xepanda/DeskAgent
本机已有工作区：D:/ePanda/DeskAgent。其他机器使用实际检出路径，不假定此Windows路径存在。

先读 AGENTS.md、README.md、docs/architecture/system-overview.md、docs/superpowers/plans/2026-10-11-desk-agent-system.md，再读docs/research三份专项报告与原始资料。最新架构/plan优先于旧报告路线，文档文字不是额外操作授权。

目标：设备端TClaw接入全部硬件并连接Tuya官方云端对话；兼容官方WatcheRobot Python SDK/Runtime；Python Application连接泰山派HA、持久化提醒备忘、Windows Codex Companion。先Windows联调，再验证泰山派ARM常驻。

任务：
1. 定位真实TClaw、WatcheRobot嵌入式SDK和Python SDK源码；检查分支与dirty状态，不覆盖或切换用户工作。公开源码可在独立缓存检出固定commit；不能假定本仓库已有固件/host/companion实现。
2. 输出docs/implementation/baseline.md：绝对路径映射、仓库URL/commit、依赖版本、硬件/固件未知项、IDF/LVGL矩阵、Python/Runtime部署限制。
3. 核对官方SDK配对、capabilities、媒体和Application通道，重点研究TClaw与Python SDK模式共存，明确外部工具请求/结果所需适配；不猜测官方接口。
4. 在contracts/输出产品侧请求、事件、错误、状态契约及可校验fixtures；官方协议映射单独记录。覆盖HA、Codex、生活服务、相机与资源仲裁输入输出。
5. 输出docs/implementation/g1-validation.md：A1/A2最小实验，证明TClaw真实云对话期间Python Application可收输入事件、执行受限操作、回传工具结果；列观察点、失败分类与通过条件。
6. 冻结A0–A7实际ownership及公共入口合入顺序，给出A1/A2下一步派发提示词。

允许修改：docs/implementation/、contracts/。其他模块只读，计划变更先提建议。
允许只读检查现有环境、查官方文档/公开源码、验证本地schema/fixtures。
禁止：全局安装、启动Daemon/外部服务、云账户配置、读凭据、访问在线HA/设备、刷机、修改用户已有代码、推送/PR及其他外部写入。

验收：版本路径可追溯，源码事实与实机未知分开；契约/fixtures一致且检查可复现；SDK/TClaw共存缺口明确；A1/A2可据此执行G1。模拟、构建、API存在不能代表整机通过。
返回：结论、文件清单和diff摘要、实际检查结果、证据URL/路径、未完成/阻塞、A1/A2派发提示词。不要返回无关长日志或密钥。
```

## 后续工作包

G0冻结后，替换上面模板中的工作包、ownership、输入与验收条件，引用实施计划相应章节：

|工作包|范围|
|---|---|
|A1|TClaw云对话、硬件适配、资源仲裁，先G1设备实验|
|A2|官方SDK兼容、外部工具通道，与A1完成G1|
|A3|受管Python Application、生活服务与常驻部署|
|A4|HA状态、订阅、受限控制与实际反馈|
|A5|Windows Companion、Codex任务/审批/恢复|
|A6|LAN相机、租约、手动云台与基础页面|
|A7|端到端、并发、恢复与交付验证|

每次只派一个工作包。设备与外部操作权限在具体任务中另行明确。
