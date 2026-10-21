# CR-007：STM32 当前板协议兼容性与动作验证证据

- **提出方**：A0 架构与协议
- **提出日期**：2026-10-22
- **关联**：`docs/implementation/r3a-stm32-protocol-adjudication.md`、`contracts/dependency_lock.json`、`firmware/components/desk_hardware/src/desk_watcher_motion.c`、2026-10-22 当前板 move/stop 现场记录
- **状态**：**OPEN — 证据不足，待当前板资料与协议来源核对；本 CR 不裁决协议语义**
- **影响模块**：A0（协议/证据裁决）、A1（硬件接线与 STM32 驱动）、A7（HIL 验收）

---

## 1. 问题与当前观测

2026-10-22 在 COM21 对当前设备执行的 `body move` 和后续 `body stop` 均返回 `rc=-3`（`DESK_HW_ERR_TIMEOUT`）。没有观测到 STM32 RX、ACK/NACK 或物理反馈；因此不能判断 STM32 是否收到命令，也不能判断 stop 是否执行，机身当前动作状态未知。证据记录见：

- `docs/acceptance/acceptance-matrix.md` §0 的 STM32 项；
- `docs/acceptance/field-runbook-2026-10-21.md` §1、§5；
- `.target_logging/com21-small-motion-test-20261022.log`（本地现场日志）。

本轮记录的 ESP32-S3 app 镜像 SHA-256 为 `C09E84798654C8071875D04901213B8979BF2032F1B14F921CF911A6F857BE65`，见 field runbook §0。该值用于标识当前 ESP32 app，不代表 STM32 固件版本或协议。

---

## 2. 已知契约差异（不在本 CR 中裁决）

### 2.1 ACK 消息 ID

- `docs/implementation/r3a-stm32-protocol-adjudication.md` §4.3 允许通用 ACK ID `0x00`，或回显被确认的 MOVE/STOP ID。
- `firmware/components/desk_hardware/src/desk_watcher_motion.c` 的 `desk_watcher_motion_parse_ack()` 接受 ACK ID `0x00` 或 `expected_msg_id`。
- `contracts/dependency_lock.json` 的 `body_coprocessor.frame_spec.responses` 只列出 ACK ID `0x00` 与 NACK ID `0xFF`，没有表达回显 ACK。

A0 需从有权威性的 STM32 协议/固件来源确认回包集合，并在协议裁决文档、schema、依赖锁和驱动契约之间对齐。此 CR 未关闭前，不把两种形式都写成已被 STM32 实机验证。

### 2.2 超范围角度：拒绝或钳位

- `docs/implementation/r3a-stm32-protocol-adjudication.md` §7.2 要求机械硬限幅截断（clamp）。
- `desk_watcher_motion_move()` 在调用打包 helper 前，对超范围 pan/tilt 返回 `DESK_HW_ERR_INVALID_ARG`；打包 helper 自身虽有 clamp，但该公开入口不会把超范围值送入打包流程。

A0/A1 需确认产品与 STM32 契约预期是拒绝输入还是钳位输入，并说明哪些层负责该行为。在裁决前保留现有观察，不将 helper 的 clamp 描述成公开动作接口已实现 clamp。

---

## 3. 当前缺失的来源与归属证据

仓库内目前没有可核对并归属到本轮设备的下列资料：

1. 当前设备 SKU、机身/主板 PCB 修订与对应装配版本；
2. 当前板型适用的原理图版本，以及 STM32 UART TX/RX/GND、逻辑电平、供电和线束连接核验记录；
3. 当前 STM32 固件的版本、构建来源、镜像 SHA-256，以及其 UART 帧/CRC/ACK 规范；
4. 可对应到当前板与当前 ESP32 app 镜像的 UART 原始 TX/RX 捕获，包含时间戳、字节流、序号、CRC 和 ACK/NACK；
5. 可复现的当前板 HIL 记录，含设备身份、ESP32 app hash、STM32 固件 hash、测试步骤、接收证据和可观察动作结果。

`docs/implementation/r3a-stm32-protocol-adjudication.md` §7.1 提及审查过硬件设计原理图，但没有给出可在本仓库核对的图纸版本或附件。该陈述不能替代当前实物修订与线束证据。

`docs/implementation/r7-verification-and-acceptance-report.md` §4.3 与验收矩阵第 22 项把历史 `body stop` 记录为 PASS，但没有提供当前板/STM32 固件身份、可关联的 ESP32 app SHA、原始 UART RX/ACK 帧或独立物理动作观察。该历史记录不能证明当前板或本轮 SHA 为 `C09E…BE65` 的镜像下 STM32 互通。它与 2026-10-22 当前 move/stop 均超时的记录必须分别保留，不能互相覆盖。

---

## 4. 恢复任何机身动作前的证据门槛

以下条件全部满足后，A0/A1/A7 再共同确定受控 HIL 步骤；本 CR 本身不授权动作或改线：

1. **目标识别**：记录当前设备 SKU、PCB 丝印/修订与装配来源，并确认测试对象与资料匹配。
2. **电气与线束**：提供当前板原理图版本；通过连续性/独立测量确认 UART TX/RX/GND、逻辑电平、供电和连接方向。存在矛盾时保持停止，不猜测引脚或改线。
3. **固件归属**：取得 STM32 固件版本及 SHA-256、协议来源和 ACK 语义；记录 ESP32 测试 app SHA-256、构建来源与实际刷入校验结果。
4. **静态协议对账**：先关闭第 2 节的 ACK-ID 和角度边界差异，或由 A0 明确记录裁决依据并同步全部契约载体；用原始帧 fixture 覆盖合法 ACK、回显 ACK（若权威契约允许）、NACK、错序、坏 CRC、超时重试与同序号规则。
5. **安全预检**：确认动作 owner、机械运动范围与限位/急停办法；在现场可直接观察并停止设备。此前 `stop` 未获 ACK、当前动作状态未知，必须先在不产生新动作的方式下确认设备处于可安全测试状态。不得把无 ACK 的 stop 当作已经停止。
6. **逐步 HIL**：从不会导致危险运动的链路观察开始；通过逻辑分析仪或独立接收计数同时保留 TX/RX 原始数据。只有命令实际接收、ACK 的 ID/序号/CRC 与请求吻合后，才在安全范围内做最小可观察动作及 stop 验证。任何超时、错帧、意外动作或无法确认停止均立即中止。
7. **结果与恢复**：分别记录命令发送、STM32 接收/ACK、可观察物理动作和 stop 结果；ACK 仅按已裁决语义报告 accepted，不能声称物理到位。记录异常恢复方法和测试后设备状态，并归档原始数据及 hash。

---

## 5. 关闭条件与责任分工

- **A0**：取得权威协议来源后，裁决 ACK-ID 与超范围角度语义；对齐 adjudication、schema、dependency lock 和接口契约。
- **A1**：在资料匹配当前板后，核对 UART 接线/电气配置与实际 STM32 固件协议；仅在安全预检通过后参与硬件验证。
- **A7**：依据冻结后的契约执行并归档可重复的 HIL 测试，确保记录可关联当前 ESP32/STM32 镜像与设备身份。

只有来源核对、契约一致、当前板 ACK 收发、受控动作及 stop 安全结果均有可复核证据后，才可关闭相关硬件验收项。若 STM32 固件或协议来源无法取得，CR 保持 OPEN，STM32 兼容性和机身动作标记为未验证。
