# CR-035：PR #24 隔离固件目标构建输入与产物边界

- **提出方：** A0 架构与公共构建
- **状态：** A0 APPROVED — 构建安全契约；目标构建仍 BLOCKED，待满足本文件非敏感配置前置条件并提供 L2 构建证据
- **范围：** 为 PR #24 的固件源码定义可审计、可复现的 ESP32-S3 目标构建输入、配置生成、TuyaOpen 组件解析和产物归档规则。只规定构建契约，不授权修改源码、运行构建、安装依赖、连接设备或烧录。
- **目标基线：** `TuyaOpen` commit `cdc064e`、ESP-IDF `5.4.0`、DeskAgent 固件源码及依赖锁文件中固定的组件版本。实际构建记录必须写出完整 40 位 SDK commit、IDF 版本和所有输入摘要；短 SHA 只用于本 CR 识别基线。

## A0 裁决

目标构建必须从新建的隔离 stage 开始。**不得读取、复制、解析、散列或以任何方式复用共享 TuyaOpen checkout 中 ignored/generated 的 `platform/ESP32/tuya_open_sdk/sdkconfig`、`sdkconfig.old`、`build/config/sdkconfig.h` 或其他生成配置；也不得读取 credentials、NVS dump、机器本地授权配置或包含密钥的项目头文件。** 不得把外部 SDK 的构建目录、生成配置或本机生成产物作为默认值来源。

配置仅从仓库内经审核的非敏感输入重新生成：

1. ESP-IDF defaults 取自 `firmware/sdkconfig.defaults`；TuyaOpen 应用/Kconfig 选择取自 `firmware/app_default.config`、`firmware/Kconfig` 及固定源码中的 Kconfig。生成的 `sdkconfig`、`sdkconfig.h`、CMake/Ninja 缓存只能在本次归档内的 stage 中产生。
2. 配置生成过程必须使用 fresh stage，显式指定 ESP32-S3、IDF 5.4.0，并记录默认文件、Kconfig 源文件及解析后配置的 SHA256。由默认文件不能推导出的选项不得从共享配置“补齐”。
3. 若编译要求某个未被上述非敏感输入定义的设置，暂停构建并请求人工提供**经过脱敏、仅含非敏感 Kconfig 项和值**的输入文件及来源说明。真实凭据不得作为构建输入；若某必需选项只能通过读取凭据文件得到，则该目标构建保持阻塞，不以假凭据或推断值宣称通过。

## TuyaOpen 组件与配置归属

- `tal_cli` 必须解析到锁定 TuyaOpen SDK 的官方组件实现；DeskAgent 使用自己的 `desk_cli` 组件调用该 API。构建前应从组件解析清单证明唯一 provider 和其来源路径/摘要。若出现本地同名 `tal_cli` 遮蔽、重复注册或解析来源无法审计，则构建失败；不得创建第二个 CLI runtime 或修改组件优先级来绕过冲突。
- `managed_components` 是源码输入，不是生成输出。只允许把与 `dependencies.lock` 一致且来源/版本可验证的组件源码复制进隔离 stage；不复制共享 `build`、缓存或生成的配置。依赖缺失、锁文件不匹配或需要未授权联网解析时停止并报告，不自动更新锁文件。
- IDF/Kconfig 解析必须纳入 DeskAgent 与 TClaw 的受控 Kconfig 源；解析结果应能证明板型、ESP32-S3、分区表文件及所需 HAL/CLI 选项来自已声明输入。不得把 `firmware/sdkconfig.defaults` 与 TuyaOpen `app_default.config` 当作可互换文件：前者提供 IDF 默认值，后者提供 TuyaOpen 应用配置选择，最终配置由本次 Kconfig/config 生成过程合成。

## 隔离与归档要求

1. 每次运行在新建、唯一的 `.target_logging/pr24-isolated-build-<run-id>/` 下建立源码 stage、SDK stage、工具链登记、生成配置、构建目录、日志和 manifest。stage 不得通过 junction、symlink 或其他别名回写到原 SDK/工程；所有构建写入和中间产物必须落在该归档目录内。
2. Stage 只包含构建所需的可审计源码、固定版本工具链输入与非敏感 defaults。排除共享 SDK 的 `sdkconfig*`、生成目录、机器本地配置与凭据文件。不得遍历、摘要或复制这些排除项。
3. Manifest 至少记录 DeskAgent HEAD 与固件 diff 摘要、SDK 完整 commit、IDF 精确版本、组件锁定结果、Kconfig/defaults 文件摘要、解析后非敏感配置摘要、stage/输出根路径、构建命令、退出码及最终 ELF/BIN/分区表哈希。Manifest 与日志不得包含配置值中的 Secret；若配置审查发现敏感项，删除本次归档并中止交付，不能把其内容写入日志。
4. 该流程仅编译和归档，不执行 `flash`、`erase_flash`、串口访问或设备读写。生成的 merged image 不得被当作设备烧录清单；烧录仍受分区安全策略和单独的当次授权约束。

## L2 验收条件

- 从干净的新隔离归档完成配置生成和 ESP32-S3 目标构建，使用固定 TuyaOpen/IDF 版本；不依赖共享 generated `sdkconfig` 或已有 build cache。
- 构建日志证明使用锁定的唯一 `tal_cli` provider，依赖解析与 `dependencies.lock` 一致；PR #24 的目标源码确实进入构建图。
- 所有 stage、解析配置、构建中间文件、日志和输出均位于该归档目录；stage 与外部工程/SDK 不共享文件或路径别名。Manifest 完整且经过敏感字段检查。
- 构建退出码为 0，并归档 ELF、应用 BIN、分区表生成结果及 SHA256。只可据此报告 **L2 目标构建通过**；不证明真机播放、SDK 会话、相机、语音或 L3/L4 行为。
- 任一配置无法从审核过的非敏感输入生成、组件来源不唯一、工具链版本不匹配、依赖需隐式更新、产物落在归档外或出现凭据风险时，结果为 **BLOCKED/FAIL**，不得以旧镜像或旧 build cache 代替。

## 当前未决项与执行边界

- 本 CR 未运行构建，未读取共享 SDK 的 ignored `sdkconfig`，也未访问任何凭据；因此目前没有目标构建通过证据。
- 需由 A1/A0 实现或确认的隔离构建入口，必须满足上述规则。若现有入口会复制共享 `sdkconfig` 或将 build 写入归档之外，应先由 A0 单独裁决改造范围，再运行目标构建；本 CR 本身不授权修改 runner。
- 若 Kconfig/config 生成仍要求无法从已审核 defaults 得出的非敏感选项，执行者应列出**符号名、为何必需、可接受的非敏感值格式**，请求人工提供 sanitized input。不得展示或请求真实凭据值。
- 本契约与 PR #24 的实际源码链接/编译结果尚未验证；补齐 L2 后仍须等待硬件连接，另行验收音频 HAL 的 L3/L4。
