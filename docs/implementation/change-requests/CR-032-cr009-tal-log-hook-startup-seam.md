# CR-032：CR-009 TAL logger hook 启动 seam

- **提出方：** A0 架构与协议
- **状态：** **APPROVED — 仅批准既有 TAL logger callback 的窄范围启动接线与分类接口；未扩大日志事件 allowlist**
- **关联：** [CR-009](../CR-009-esp32s3-network-log-source.md) §3.2、§4、§5.2；`firmware/main/src/app_main.c`；A1 `desk_watcher_log_source`。
- **证据级别：** 本文为 L1 启动/接口裁决。代码和定向测试未在本 CR 中修改或执行；不代表 L2/L3/L4 通过。

## 1. 现有启动点与归属裁决

当前 `firmware/main/src/app_main.c` 的 `desk_tuya_log_output()` 是 TAL 初始化注册的输出回调；`desk_tuya_log_output()` 保留原有敏感行抑制和 `fputs(..., stdout)` 行为。ESP_PLATFORM 启动流程在 `tal_time_service_init()` 之后调用 `tal_log_init(TAL_LOG_LEVEL_INFO, 1024, desk_tuya_log_output)`，之后才初始化 timer/work queue（`app_main.c` 当前约第 148–158、335–349 行）。

CR-009 §5.2 原先说明 `app_main.c` 不属于当时已列明的 A1/A2 路径，必须另行裁决才能接线。本 CR 为该项**一次性窄授权**：A1 可修改 `firmware/main/src/app_main.c`，但只允许在现有 `desk_tuya_log_output()` 回调内加入对下述 A1 接口的一次调用；允许在 `firmware/components/desk_hardware/include/desk_watcher_log_source.h` 与 `src/desk_watcher_log_source.c` 定义/实现该接口。A2 不修改启动入口；不得改 `tal_log_init()` 注册参数、初始化次序或新增第二个 logger callback/hook。

唯一调用关系：

```text
TAL PrintLogV
  -> desk_tuya_log_output(line)              # 现有唯一 TAL output callback
     -> 原有敏感检查与 UART/stdout sink      # 必须原样保留
     -> desk_watcher_log_source_observe_tal_line(line)  # 新的 A1-owned bounded classifier
```

`desk_watcher_log_source_observe_tal_line(const char *formatted_line)` 只在 `desk_watcher_log_source` 已启用时尝试分类；不得保存或延迟使用 `formatted_line` 指针。回调必须有界、非阻塞、可并发/重入，不分配动态内存、不做网络 I/O、不调用 `ESP_LOG`/TAL logger、不等待队列空间。该接口只可发布 CR-009 已批准枚举；当前 CR-009 v1 allowlist 只有 `CAMERA_CAPTURE_EMPTY(fields={})`，而该事件继续由现有 camera structured source 产生。本接口不得从普通自由文本合成该事件，因此当前 TAL 文本全部不映射为网络事件；未来新增来源或事件必须先由 A0 另行批准。

## 2. 保留行为与失败关闭

- `desk_tuya_log_output()` 的既有敏感标记判断及 UART/stdout 输出顺序/内容保持不变。敏感行按现有策略抑制后，不传给 classifier；即使 classifier 禁用或失败，也不改变原本地输出结果。
- classifier 只读取已格式化的回调文本，不改变 TAL log level、buffer size、UART sink 或 `tal_log_init()` 返回值。分类失败、source 未初始化、队列关闭/满、输入为空或过长时，安全丢弃网络候选并按既有计数/gap 规则处理；不得阻塞启动或 TAL 日志调用，也不得启用网络授权。
- 不得转发原始日志行、自由字符串、tag、敏感 marker、凭据、用户内容或未登记字段；不写入 Flash/NVS/SD。TAL 是否可被 logger output callback 重入以及输入 buffer 生命周期须由实现/测试按同步借用处理，不得保留借用指针。
- 本裁决不增加 `event_id`、`fields`、组件 allowlist、日志来源覆盖承诺或资源预算；不修改 `desk_sdk_bridge/`、Host consumer、网络协议、`app_main.c` 其他启动行为或 UART fallback 工具。

## 3. A1 实施边界与 L2 验收建议

后续 A1 单一实现包可修改：

1. `firmware/main/src/app_main.c`：仅在现有 `desk_tuya_log_output()` 中接入新 A1 classifier 调用，保留本地输出逻辑；
2. `firmware/components/desk_hardware/include/desk_watcher_log_source.h`、`src/desk_watcher_log_source.c`：新增 A1-owned 函数及 bounded 分类实现；
3. 对应 `tests/firmware_contract/` 定向测试。不得改 A2 SDK bridge 文件。

至少覆盖：

- source 启用/关闭、空/超长/格式异常输入、队列满、并发调用与重入时均有限时返回；不保存回调指针、不分配动态内存、不调用网络发送或递归 logger；
- 当前所有 TAL 自由文本（包括看似包含 `CAMERA_CAPTURE_EMPTY` 的文本）均不产生 `sys.log.event`/新的 event record；只有既有结构化 camera API 可产生已批准事件；
- 原敏感行过滤与 UART/stdout sink 的输入和调用次序不因 hook 失败而改变；source 未初始化/禁用/满载时本地 UART 仍按既有策略工作；
- 编译期/源契约检查唯一回调注册仍为 `tal_log_init(..., desk_tuya_log_output)`，没有第二 hook、`app_main.c` 其他初始化重排或 A2 路径越界。

以上为 L2 源码/模拟验收建议，须由对应 A1 实施包实际运行并报告结果。真实目标板 TAL 锁行为、UART 保真、内存/栈水位和语音/控制并发影响仍需另行证据；此 CR 不宣称 L3/L4。
