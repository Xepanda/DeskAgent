# CR-029：通知 UI overlay 与底层状态恢复接口提案

- **提出方：** A0 架构与协议
- **状态：** **A0 APPROVED（2026-10-23）：A1 本地 UI overlay 已实现并完成 L2 验证；设备验收待硬件现场。**
- **关联：** CR-017、CR-018、CR-019、CR-028；A1 当前 TClaw/cloud UI 接线改动
- **范围：** 定义设备端 notification presentation 与 voice/cloud base UI 的本地接口候选、优先级、生命周期及语义边界。不修改 wire/schema、业务生产代码、持久化格式或存储分区。

## 1. 当前调用链与缺口（源码审计）

1. `desk_hardware_notification_process_pending()` 在 `firmware/components/desk_hardware/src/desk_notification.c` 中获取 `DESK_RES_DISPLAY_UI` 后直接调用 `desk_hardware_display_set_expression("alert", text)`，然后释放资源；通知记录保存自身 `rendered`、`audio_attempted`、`pending` 和 `acknowledged` 阶段。该渲染函数是立即设置屏幕内容的命令，不是带生命周期的 UI overlay。
2. 当前 A1 工作区 diff 在 `firmware/main/src/tclaw_runtime_adapter.c` 增加 `desk_tclaw_runtime_set_cloud_online()`：MQTT 在线/离线分别直接设置 `happy/Online`、`offline/Cloud Offline`。`__desk_ai_chat_event_handler()` 在 cloud offline 时忽略迟到的 voice UI 事件，但 cloud online 时仍直接调用 `desk_hardware_set_screen_expression()` 更新状态。
3. `firmware/main/src/app_main.c` 的 MQTT 事件处理转发至上述 TClaw setter；此前入口自身直接设置 screen expression。屏幕 API 无优先级、租约、恢复或撤销 token 参数。当前源 diff 只能证明接线/静态行为变化，不证明设备显示效果。
4. 因此通知绘制之后，下一条 cloud/voice 更新可能覆盖通知；反向地，通知清除也没有已定义的“恢复最新底层状态”操作。持有 `DESK_RES_DISPLAY_UI` 只保护一次渲染调用，不保护后续可见期间，也不是 UI 状态所有权或通知租约。

## 2. 候选行为契约

### 2.1 显示优先级及状态更新

- 通知卡片是暂时的高优先级 overlay；voice/cloud 是持续更新的 base UI。存在有效 overlay 时，base UI 更新必须被接受并更新其最新快照，但不得替换当前屏幕内容。
- UI 层只维护一份权威 base snapshot（至少包含 preset 和 status text）。cloud online/offline、TClaw voice event 都通过同一 base-state 更新 seam 提交。cloud disconnected 时 TClaw 晚到事件仍按 A1 当前规则拒绝；overlay 不得改变此云端/voice 接受规则。
- 通知 overlay 可由唯一活跃通知占用。重复收到同一 notification ID 是幂等更新，不新增 overlay、不重置其已经过去的有效时间；新的不同 ID 到达时建议按 `expires_at` 更晚者优先，完全相同则保留当前项并排队/拒绝新项。**更推荐首期仅允许单活跃 overlay，冲突由调用方以 BUSY 重试，不引入未裁决的队列/优先级产品行为。**
- overlay 的有效截止采用 CR-017 payload 中可信 UTC `expires_at`，不得另造与提醒有效期不一致的 TTL。确认/关闭可提前结束当前 overlay；否则到 `expires_at` 自动清除。仅隐藏/关闭 UI 不得把已经 expired 的 occurrence 重新显示。
- 任何 overlay 结束时，重新渲染当时最新 base snapshot；不能恢复 overlay 创建时捕获的旧 preset/text。若没有有效 base snapshot，则显示既有初始化默认状态（由 A1 指定），不得显示已过期通知。
- 单调时间用于本进程 overlay 定时和竞争比较；UTC `expires_at` 用于与提醒有效期对账。若系统 UTC 无效、时间回拨使到期无法判定，按 fail-closed 处理：不延长可见/可播报期限，记录 unknown/diagnostic，不自行恢复已不确定的 overlay。具体 clock guard 复用通知当前可信时间检查，不另造持久时间服务。

### 2.2 分离显示、播报、ACK 与提醒完成

- **显示/rendered：** 屏幕 API 成功将通知内容提交给显示路径，只表示 render 请求成功。它不能证明人已看见，也不代表音频开始/完成、ACK 或提醒完成。
- **播报/played：** 当前 `desk_hardware_audio_play_pcm()` 返回成功并不能证明音频真实播放完成；沿用 `desk_notification.h` 中 `DESK_NOTIFICATION_PLAYED` 的保留注释，不因成功返回而发 `played`。音频尝试失败/结果不确定不能抹掉已成功显示的事实。声音是否包含通知内容或提示音仍由现有通知逻辑决定，不属于 overlay handle。
- **ACK：** 用户明确确认通知后，以现有 `desk_hardware_notification_acknowledge(notification_id, source)` 记独立回执。ACK 不表示显示成功或播报成功；如 ACK 同时关闭 overlay，先记回执成功，再按 UI 关闭语义收起。重复 ACK 幂等。
- **Dismiss：** 只结束当前屏幕 overlay，不创建 ACK，也不改变通知 delivery phase 或 reminder occurrence 状态。若产品决定某个实体按键/滚轮手势代表“确认”，该输入必须显式调用 ACK，而不能只因 UI dismiss 推断 ACK。
- **Reminder complete/stop ringing：** 是独立的业务动作，不由显示、播放、ACK 或 dismiss 隐式触发。
- `notification.deliver` 的 durable accept、状态事件与重试规则继续服从 CR-017/018/019。Volatile UI overlay 只是一段运行时显示状态，不提供 durable dedupe、重启恢复、可靠提醒或离线错过处理能力。

## 3. 最小本地接口候选（仅提案，不是已批准 API）

建议 A1 在固件内部增加单一 display-state owner/facade；不要求跨 wire 导出，也不应让 notification 或 TClaw 模块持有底层 LVGL/display backend 句柄。概念接口可按项目命名调整：

```c
typedef uint32_t desk_ui_overlay_handle_t;

int desk_ui_set_base_state(const char *preset, const char *status_text);
int desk_ui_overlay_present(const char *owner, const char *key,
                            const char *preset, const char *status_text,
                            uint64_t expires_epoch_us,
                            desk_ui_overlay_handle_t *out_handle);
int desk_ui_overlay_dismiss(desk_ui_overlay_handle_t handle,
                            enum desk_ui_dismiss_reason reason);
int desk_ui_overlay_acknowledge(desk_ui_overlay_handle_t handle,
                                const char *ack_source);
int desk_ui_overlay_process_expiry(uint64_t now_epoch_us);
```

- `desk_ui_set_base_state()` 仅更新 base snapshot。它可在 overlay 可见时调用，成功表示 snapshot 已接收，不保证当下显示。
- `desk_ui_overlay_present()` 仅创建/更新 volatile overlay 并呈现。`owner` 建议为稳定模块枚举（如 `notification`），`key` 用 notification ID 做本地去重关联；两者不是 wire 新字段。返回 handle 绑定 owner、key 和 overlay generation。需定义 `BUSY`、invalid/expired 和 display I/O 错误，不得把资源忙误报为已呈现。
- `dismiss()` 与 `acknowledge()` 都必须匹配当前活跃 handle/generation，防止旧异步回调关闭后来替换的新通知；dismiss reason 至少区分 `USER_DISMISSED`、`ACKNOWLEDGED`、`EXPIRED`、`OWNER_CLOSED` 和 `DISPLAY_ERROR`。ACK 来源继续由通知业务提供并用现有 allowlist/长度校验。
- `process_expiry()` 可由既有 A1 loop/timer 调用；不建立第二个 UI task/runtime。进程生命周期中有效期到达时先使 handle 无效，再恢复最新 base snapshot。UI 层不保存/持久化通知记录。
- 回调/通知以不可变的小型事件 `{handle, key, result/reason}` 传递；不得持锁调用 AFE/audio、Tuya SDK、host bridge 或任意业务 callback。若使用同步 API，则返回值仅表示本地状态变更/绘制提交；不能在持有 arbiter/resource lock 时回调。
- handle 用 generation 防 ABA/迟到回调；资源/锁只在更新 base/overlay 状态及挑选当前渲染 snapshot 时短暂持有。慢速硬件绘制若需要 serialized executor，复用当前显示 owner，不允许每个通知另开线程。

## 4. 并发、重复与异常边界

1. **并发 base update：** 以 display owner 锁串行化；最终可见状态取最后接受的 base sequence。overlay 活跃时只更新缓存，解除时读取最新缓存。
2. **overlay present 与 expiry/ack/dismiss 同时发生：** 以 generation 和状态锁线性化；到期判断优先于 render/ack 接受。已过期通知不新建 overlay，也不由迟到 ACK 复活。
3. **同 ID 重复与不同 ID 竞争：** 同 ID 返回/复用同一有效 overlay handle，保持原 expires_at；不同 ID 在单 overlay 策略下返回 BUSY 或由 notification owner 先显式结束旧 handle。禁止隐式覆盖后再让旧 cleanup 清掉新项。
4. **重复清除/ACK：** 同一 handle 的重复 dismiss 幂等；旧 generation 的 dismiss/ack 返回 stale/no-op，不得触及当前项。重复 ACK 继续由已有持久 notification layer 幂等处理。
5. **显示提交失败：** 标记显示失败/错误，不激活一个不可见 overlay；尝试恢复最新 base。已发生的其他 notification phase 不回滚或伪造。
6. **base 更新绘制失败：** 保留最新 base snapshot 并回报 display error；如果 overlay 可见，不得用失败的 base render 清掉 overlay。解除 overlay 后应有重试/下一次状态变更触发重绘，不得恢复缓存的较旧成功状态。
7. **owner 异常退出/重启：** display facade 可在 owner teardown 时按 owner 使其所有 volatile handle 失效并恢复最新 base。重启自然清除 RAM overlay；不从 volatile overlay 推断提醒已处理，也不自动重播或重新显示。重启恢复仍受 CR-018 和 CR-017 约束。
8. **ACK 存储失败：** 不得显示 ACK 已成功；由业务层保留原状态并按当前通知回执错误策略上报。overlay 是否继续显示由业务层显式决策，提议存储失败时保持 overlay 可见直到有效 ACK/dismiss/expiry，避免 UI 暗示已确认。
9. **权限/资源仲裁：** facade 是显示状态的唯一写入所有者。UI overlay 与 arbiter 的 `DESK_RES_DISPLAY_UI` 的关系由 A1 落地；不允许把短暂 acquire/release 当作 overlay 租约，也不绕过已存在的资源仲裁。

## 5. 所需裁决及责任

- **A0/A1 内部接口批准：** base-state API、单 overlay handle/generation、owner、expiry 规则、错误返回、回调线程与唯一 display owner。A1 负责内部实现与测试；A0 确认不会修改既有 notification wire/schema 与 CR-017 语义。
- **产品决定（只在行为扩展时需要）：** 本提案默认无自动短时隐藏：通知保留到用户明确 dismiss/ack 或 `expires_at`；若希望固定秒数自动收起、多个通知排队/抢占、滚轮/按键如何 ACK 或是否播报提示，则属于用户可见交互策略，须由产品 owner 决定。现有已确认的按钮沿用 TuyaOpen 短按/长按及滚轮 UI 导航，可作为事件来源，但不能在本 CR 中擅自重新映射为提醒完成。
- **存储决定：** CR-018 仍为 **BLOCKED**。本提案的 volatile overlay 仅解耦绘制优先级并正确恢复 base UI，不解除 durable storage/provider 的授权阻塞，不使 notification deliver 可被宣称为持久接收，不等于持久提醒，也不解决断电错过提醒后的补偿。
- **验收建议（实施后 L2）：** host/native 渲染 seam 验证：overlay 存在期间连续 cloud/voice base 更新均不覆盖；ack/dismiss/expiry 后恢复最新 base；旧 handle/过期事件不能关闭/复活新 overlay；相同通知幂等、不同通知 BUSY；显示/ACK/played/reminder complete 语义分离；显示失败及 ACK 存储失败行为正确。硬件显示观感与真实通知闭环仍须 L3/L4 现场验证，不能以单元测试替代。

### A0 裁决（2026-10-23）

批准 A1 在设备内部实现以下受限契约：单一 volatile notification overlay；同 ID 幂等且不延长原 `expires_at`；不同 ID 在现有 overlay 期间返回 busy；voice/cloud base state 继续更新但不覆盖 overlay；dismiss、有效 ACK 或 `expires_at` 到期时恢复最新 base state；generation 防止迟到事件误清新 overlay。不得新增 wire/schema 字段，不得改变按键/滚轮含义，不自动隐藏/排队/逐项播报，不将显示或 ACK 推断为提醒完成/物理动作，不创建第二个 UI task。CR-017/018/019 durable acceptance 与 storage blocked 语义保持原样。

本裁决仅批准 A1 的本地 UI state owner 接口及软件实现，不批准产品级提醒汇总、输入手势重映射、设备持久化或新的用户可见策略。实现采用单一 volatile overlay、generation handle 和 dirty-render retry；忙时无法立即恢复屏幕会保留重绘请求，并由既有处理循环重试。A1 报告 6 个测试模块共 68 项通过，native notification 测试通过；主控另完成 ESP-IDF 5.4 全量固件构建（1341/1341 步，退出码 0）。这些属于 L2/构建证据，目标屏幕上的通知覆盖、过期/ACK 后恢复仍需 L3/L4 现场验收。

## 6. 当前证据等级与限制

- **源码实现与本地测试：L2**：当前工作区有共享 overlay state、generation stale guard、最新 base 恢复及 busy 后 dirty-render retry；A1 报告 68 项相关单元/契约测试和 native notification 测试通过。主控全量 ESP-IDF 5.4 构建通过（1341/1341，退出码 0）。构建产物尚未烧录。
- **设备验收：未验证。** 当前回合无硬件连接；不声称通知优先级、恢复绘制、实体确认或 durable reminder 在实机通过。
