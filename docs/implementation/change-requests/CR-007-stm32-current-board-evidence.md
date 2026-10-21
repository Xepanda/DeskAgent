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

---

## 6. A0 只读源码对账（2026-10-22）

本节只确认仓库代码与测试表达了什么，不确认 STM32 固件采用相同协议，也不代表当前设备 UART 已收发成功。源码行号以本次工作树为准。

### 6.1 当前 Watcher 板的动作路径

`firmware/components/desk_hardware/src/desk_watcher_board.c:745-778` 在板初始化中调用 `desk_watcher_motion_init()`，并将 `.motion_move`、`.motion_stop` 指向 `desk_watcher_motion_move()` / `desk_watcher_motion_stop()`。因此当前 Watcher board ops 的动作路径进入 `desk_watcher_motion.c`。`desk_hardware.c` 同时包含协议打包、默认 UART 传输和接收处理，但它不是仅凭源码就能替代上述实际 board-op 路径的证据；应根据当前固件构建配置和启动日志进一步确认最终注册/调用链。

### 6.2 帧、CRC 与序号重试

- `desk_watcher_motion.c:57-97,100-120` 生成未做 COBS 封装的 `A5 5A` 原生帧：12-byte header、LE 序号/载荷长度、MOVE 9-byte payload 或 STOP 2-byte payload，尾部附 CRC16；CRC 实现为 init `0xFFFF`、poly `0x1021`，低字节先发（同文件 `41-55`）。MOVE/STOP 的帧格式和 `docs/implementation/r3a-stm32-protocol-adjudication.md` §3-5 与原生帧契约一致。该驱动中的 COBS helper (`175-249`) 不在发送路径上；`motion_send_and_wait_ack()` 将 `frame_data` 原样传给 UART (`319-323`)。
- `desk_watcher_motion.c:314-387` 每次尝试重发同一 `frame_data`，总尝试数为初次发送加 `MCU_MAX_RETRIES` 次；超时常量和契约文件仍需同版本核对。TX 完整写入只是本端写入成功，不证明 STM32 接收。
- `desk_watcher_motion.c:251-291` 的 ACK parser 检查魔数、版本、class、精确总长度和 CRC；随后要求 `rx_seq == expected_seq`。NACK 需满足 NACK flag 或 ID 且 payload 长度恰为 2；ACK 要求 flags 恰为 `ACK_RESP`、payload 长度恰为 4，并接受通用 ACK ID `0x00` 或回显 MOVE/STOP ID。由此可确认驱动实现允许 ACK echo；不能据此确认 STM32 实际发送哪一种。
- 同仓库 `desk_hardware.c:834-896` 是另一套接收实现：验证 CRC 后按序号关联 ACK/NACK，但 ACK 条件为 ID 命中**或** ACK flag 命中，且没有对 ACK flags/payload length 作与上面相同的精确限制。`r3a` 文档/依赖锁称 `desk_hardware.c` 是权威活动实现，而 Watcher board ops 指向 `desk_watcher_motion.c`；需要 A0/A1 根据实际构建产物和调用日志确认活动路径，并统一重复实现的契约，避免“一份契约、两种验收行为”。

### 6.3 角度边界与停止语义

- `desk_watcher_motion.c:394-405` 直接调用时会拒绝超范围 pan/tilt；其内部 pack helper (`57-69`) 仍会 clamp。`desk_hardware.c:1043-1052` 的外层硬件 API 会先 clamp 再委托 board driver，所以通过该外层 API 时，传给 Watcher driver 的值应已在范围内。两者并非同一个入口语义：若产品规范要求统一行为，需核对调用方是否存在绕过外层 API 的路径，并由 A0 决定契约是 clamp 还是 reject。静态代码不能证明机械限位安全。
- Watcher STOP 打包 `mode=DECEL/EMERGENCY` 与 `CANCEL_ALL` (`desk_watcher_motion.c:100-120`)，之后等待与其序号关联的 ACK (`407-412`)。ACK 只能依 CR-002 解释为 accepted；源码和当前日志都不能证明舵机已停或机身已静止。失败/超时也不能当成停止成功。

### 6.4 测试与板级证据边界

- `tests/firmware_contract/test_stm32_body_protocol.py:329-599` 覆盖 CRC golden vector、MOVE/STOP 帧、角度/duration clamp、ACK/NACK、错误 CRC、帧长度/魔数和序号匹配等 host 原生 C harness 场景；这是 L2，不是 STM32 firmware interop。
- `tests/firmware_contract/test_stm32_uart_native_frame_contract.py` 与 `test_watcher_hardware_contract.py` 是源码/host harness 合约检查，也不提供目标 UART 的 RX 证据。
- 当前可追溯现场记录仍是本 CR §1 的 COM21 `body move`、`body stop` 均 `rc=-3`、未见 RX/ACK/物理反馈；验收矩阵 §0 已明确 `Last Packet Length` 是格式化/发送帧记录而非 STM32 RX 计数，UART READY 只说明初始化。此现场结果不能确定命令是否到达，也不能确定 stop 是否执行。
- 本次只读审计没有运行硬件命令、占用 COM21/COM20、发送 STM32 动作帧或读取凭据；也没有新的 Level 3/4 证据。

### 6.5 下一步现场证据（保持无动作直至安全状态确认）

1. 先补当前 PCB/SKU/线束与适配原理图版本、STM32 固件来源/版本/hash；按 §4 核对 TX/RX/GND、电平和波特率，不通过软件命令推断接线正确。
2. 在不向执行器发送 move/stop 的前提下，用独立逻辑分析仪或已验证的只读接收路径观察 STM32 TX/RX 空闲及启动字节，归档带时间戳的原始波形/字节和设备、固件身份。仅 UART READY 或 ESP32 的 TX 计数不足以通过。
3. 对照权威 STM32 协议来源，先统一 ACK ID/flags/payload 长度、角度边界、序号回绕和 retry 语义，并为两套实现增加相同负例/正例覆盖；确认同一协议实现被当前构建实际选用。
4. 当前 stop 无 ACK 且动作状态未知。在任何新的动作之前，须由现场人员通过独立目视/机械安全确认设备静止、执行器工作区域清空、可用物理断电/急停手段就位，并明确现场验证窗口。此前置条件未满足时不发动作命令。
5. 仅在安全预检完成并有现场监督后，先采集可区分的原始 TX/RX（不发送动作的链路观察优先）；若后续最小动作验证获得明确授权且确认安全，每条命令都要求同序号、正确 CRC 的 STM32 响应和独立可见的动作/停止观察。任一超时、坏帧、错序、意外动作或无法确认静止即中止；不得把软件 stop 重试当作安全急停。

**A0 结论：**源码实现大体符合 `A5 5A` / CRC16-CCITT-FALSE / 32-bit 序号关联 / 同序号重试的冻结契约，但 ACK 回包集合和角度边界在不同接口/契约载体中有差异，且存在两套可能行为不同的固件实现。仓库测试只证明 L2。因当前设备两条动作命令均超时且没有 STM32 RX/ACK、固件身份或物理状态证据，CR-007 继续 OPEN；当前板协议兼容、动作执行与安全停止均未验证。
