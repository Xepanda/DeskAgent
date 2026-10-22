# CR-011: HX6538 PTL 与 Seeed SSCMA SPI 传输裁决

**裁决者：** A0 架构与协议
**日期：** 2026-10-22
**输入：** `firmware/components/desk_hardware/PROTOCOL_CHANGE_REQUEST_SSCMA_SPI.md`
**状态：** 已裁决当前产品协议；目标设备 HX6538 固件身份仍待实机/固件证据核验
**范围：** ESP32-S3 到 HX6538 的相机数据传输选择。此裁决不授权串口操作、刷写、依赖引入或 A1 代码修改。

## 裁决

1. **DeskAgent 当前唯一选择的运行时相机传输为 WatcheRobot HX PTL 二进制协议。** 这是当前产品实现基线，不代表已经证明目标 HX6538 固件与 PTL 配套，也不代表相机验收通过。保留已锁定的 WatcheRobot 来源版本 `646a977bfd76a8516ae505fda2331b81e1049b87`；当前 DeskAgent 端 PTL 实现继续由唯一板级相机 owner 管理。
2. **不将 Seeed SSCMA SPI 视为 PTL 的兼容模式或直接替代解析器。** SSCMA 是另一个命令/回复协议。当前不新增 SSCMA 依赖，不启用双 transport，也不允许 SSCMA client 与 PTL driver 同时注册、持有或传输 SPI2/HX6538。
3. **不因本次源码对照切换现有设备固件或协议。** 目标板 HX6538 的固件版本、运行模式及它实际启用 PTL 还是 SSCMA 尚未得到目标设备证据。此为物理兼容性开放项；不能由相同板级引脚、Seeed BSP 源码或构建成功推断。
4. 只有在下一次获准的硬件窗口取得 HX6538 固件 build/version 身份及非破坏性协议证据后，A0 才能重新裁决。若证据表明目标设备实际运行 Seeed SSCMA 固件，则通过新的 A0 变更裁决把产品协议迁移到 SSCMA；迁移时必须关闭/移除 PTL owner，不能保留并发双栈。

## 源码证据与边界

| 项目 | WatcheRobot HX PTL | Seeed SSCMA SPI |
|---|---|---|
| 固定源码 | WatcheRobot-public `646a977bfd76a8516ae505fda2331b81e1049b87` 的 `firmware/sdk/components/watcher_hx_ptl/` | Seeed SenseCAP-Watcher-Firmware `8e37f7ccd3d836bb99f483e0f6e74e8252406644` 的 `components/sscma_client/` 与 `components/sensecap-watcher/` |
| 物理接口 | SPI2，SCLK/MOSI/MISO GPIO 4/5/6，CS GPIO 21，PCA9535 SYNC bit 6，12 MHz，mode 0 | 同一组 SPI2 引脚、CS、SYNC，12 MHz，mode 0；Seeed BSP 另用 PCA9535 bit 7 复位 HX |
| SPI 上层协议 | 7-byte `C0 5A` PTL base header，随后读取 HX media header、CRC 和 JPEG payload；JPEG 帧直接由二进制帧提供 | SSCMA transport 包含 feature/op、长度和校验字段；READ/WRITE/AVAILABLE/START/STOP/RESET 操作面向 SSCMA client 命令与回复。图像由 SSCMA reply 的 image 字段提取，不是读取 PTL media header |
| 互斥关系 | `watcher_hx_ptl.h` 明确写明 PTL 初始化与 SSCMA 互斥 | Seeed BSP 创建 SSCMA SPI client 并以 SSCMA client API 初始化；与 PTL 争用同一 SPI2/HX 接口 |
| 证据能证明 | WatcheRobot 项目为 Watcher HX 实现了 PTL receiver；其中固定 SPI 配置与 Seeed Watcher BSP 板级定义相符 | Seeed 官方 Watcher BSP 的这版源代码选择 SSCMA client 和 SSCMA SPI transport |
| 证据不能证明 | 目标板当前 HX 固件已烧入或启用此 WatcheRobot PTL 协议 | 目标板当前 HX 固件就是这版 Seeed SSCMA 固件，或当前 module 的运行模式可接受 SSCMA 命令 |

原始来源：

- [WatcheRobot HX PTL receiver](https://github.com/orulink-ai/WatcheRobot-public/blob/646a977bfd76a8516ae505fda2331b81e1049b87/firmware/sdk/components/watcher_hx_ptl/watcher_hx_ptl.c) 与 [PTL public header](https://github.com/orulink-ai/WatcheRobot-public/blob/646a977bfd76a8516ae505fda2331b81e1049b87/firmware/sdk/components/watcher_hx_ptl/include/watcher_hx_ptl.h)。
- [Seeed Watcher board pin definitions](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware/blob/8e37f7ccd3d836bb99f483e0f6e74e8252406644/components/sensecap-watcher/include/sensecap-watcher.h)、[SSCMA BSP initialization](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware/blob/8e37f7ccd3d836bb99f483e0f6e74e8252406644/components/sensecap-watcher/sensecap-watcher.c) 与 [SSCMA SPI transport](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware/blob/8e37f7ccd3d836bb99f483e0f6e74e8252406644/components/sscma_client/src/sscma_client_io_spi.c)。
- Seeed 的 [SSCMA reply/image 处理](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware/blob/8e37f7ccd3d836bb99f483e0f6e74e8252406644/components/sscma_client/src/sscma_client_ops.c)读取 SSCMA reply 中的 image 字段；[组件 manifest](https://github.com/Seeed-Studio/SenseCAP-Watcher-Firmware/blob/8e37f7ccd3d836bb99f483e0f6e74e8252406644/components/sscma_client/idf_component.yml)声明 `sscma_client` 1.0.3、`idf >=4.4.2`，并说明该组件有 ESP-IDF 6 patch。此元数据不是在本项目 IDF 5.4 下构建兼容的证明。
- 本仓库现有来源与平台边界见 [`upstream-watcher-integration-map-2026-10-18.md`](upstream-watcher-integration-map-2026-10-18.md) 和 `contracts/dependency_lock.json`。本裁决未更改依赖锁。

## 锁定的接口与依赖规则

- 对 Host / 上层调用，保留 `desk_hardware_camera_capture_jpeg(...)` 作为单帧 JPEG 的稳定 HAL 入口。物理传输私有于 `desk_hardware` 板级 camera owner；上层不得依赖 PTL/SSCMA 的 SPI 包格式。
- 当前 camera owner 使用 WatcheRobot PTL 的本地适配实现，来源版本沿用依赖锁中的 WatcheRobot commit。不要把 WatcheRobot ESP-IDF 6.0.2 整套 SDK 引入 TuyaOpen 当前 ESP-IDF 5.4.0 工程；若复用协议源码，只能按本项目现有 vendored/adapted 来源及许可证记录维护。
- SSCMA 不是当前依赖。若未来提出迁移，提案必须锁定 Seeed 仓库 commit（此裁决比较快照为 `8e37f7ccd3d836bb99f483e0f6e74e8252406644`）、`sscma_client` component 版本及完整传递依赖/override，记录 Apache-2.0 notices，并先完成 TuyaOpen 当前 ESP-IDF 5.4 的干净构建验证。不得使用浮动 `main` 或仅依赖 Registry 的范围版本作为可复现锁。
- transport 更换不得改变 JPEG HAL 的单帧结果语义、超时、错误分类、缓冲区上限和 camera/motion 资源 owner；若 SSCMA reply 使用 JSON/Base64 图像字段，解码、容量上限、错误传播、图像有效性都必须由 A0/A1 单独定约并验证，不把“SSCMA reply 成功”当作 JPEG 可用。
- 任何 transport 只能有一个 SPI2/HX owner。transport 互斥要在编译配置或启动配置上 fail-closed，不能靠调用顺序避免并发注册。

## 最小下一步与验收门槛

**设备仍离线时：** 暂不改协议代码或组件依赖。A1 先从已有构建资料/已保存只读日志获取设备 HX 固件 build ID、版本、协议模式及其来源；如果没有资料，明确记录 `unknown`，等待下次硬件窗口。

**下一次硬件窗口：**

1. A1 先记录板卡 revision、HX firmware/build ID 和版本来源；只用已审阅的非破坏性读取方式，禁止刷写、擦除或修改 HX mode/config。
2. 对目标 firmware 分别按其明确支持的接口做有限只读识别：PTL header/readiness 证据和 SSCMA client 初始化/只读 info 查询证据。记录原始状态、协议响应和 SPI owner；不以初始化返回成功作为取帧成功。
3. 至少取得一张 JPEG，验证 SOI/EOI、解码可打开且内容对应现场；随后连续采集并验证释放、超时、复位/模式恢复。端到端 JPEG 才能晋升为硬件通过证据。
4. 如果只能有一种 transport 回应，则 A0 再决定是否维持或迁移；若两者都不回应，先查固件版本/供电/SYNC/总线状态，不做协议切换猜测。若两者均能应答，也仍须按单 owner 选择一种并验证关闭另一种不会留下竞争设备。

## 当前验收状态

- Seeed 与 WatcheRobot 公开源码显示了两套独立 SPI 协议：**Level 1 源码证据通过**。
- 项目当前默认协议裁决为 PTL，唯一传输 owner/HAL 边界已写明：**架构裁决通过**。
- 目标 HX6538 的 firmware/build ID 与 active protocol、真实 JPEG、连续帧、复位及资源释放：**未验证**。
- Seeed SSCMA 依赖在 TuyaOpen ESP-IDF 5.4 下的实际构建兼容性：**未验证，当前不引入**。
- 本次未触碰串口、未烧录、未修改 A1 文件或依赖锁。
