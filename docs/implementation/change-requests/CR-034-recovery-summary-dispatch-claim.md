# CR-034：恢复摘要 dispatch claim 与发送竞态

- **提出方：** A0 架构与协议
- **状态：** **APPROVED — 窄范围一次性实现授权**
- **范围：** 在 Host 发送 `notification.summary.deliver` 前，原子领取仍有效的摘要 outbox 项；定义领取与取消/ACK/重启之间的状态边界。
- **关联：** CR-028（恢复摘要策略、有效期与 ACK）；CR-019（A3 对 `main.py` 的既有窄范围例外）；`AGENTS.md` A0–A7 ownership matrix。
- **证据级别：** 本 CR 定义 L1 软件契约。实现及定向测试最多证明 L2；不证明设备已收到、呈现摘要，也不构成 L3/L4。

## 1. 问题与批准决定

Host delivery worker 读取 pending outbox 后，摘要可能在实际 adapter 调用前被 ACK、取消、过期或被另一 worker 领取。提醒分支已有 `claim_reminder_alert`，摘要分支目前没有对应的原子领取。

批准由 A3 在 `host/desk_agent/services/life_service.py` 增加摘要专用 atomic claim API，并在 `host/desk_agent/main.py` 的 `DeskAgentHostApp._delivery_loop()` 中为 `notification.summary.deliver` 分支增加**唯一的一次调用 hook**。这是对 A3 ownership matrix 的一次性、单点例外；不授权 A3 修改 `main.py` 中其他逻辑或扩大到其他 topic。A3 可在其独占的 `host/desk_agent/core/`、`host/desk_agent/services/` 和对应测试目录实现及测试该 API，并可为这个 hook 增加最小 core-level 测试。

## 2. Claim 接口与线性化语义

1. 新 API 只接受摘要 outbox 项（`event_type/topic = notification.summary.deliver`），并在一个 SQLite 事务中检查 outbox 为 `pending`、关联 batch 仍为 active、摘要未过期且未被取消或 ACK，然后条件更新 `pending → dispatching`。
2. 条件更新成功是本地线性化点，也表示 Host 开始该次发送尝试。claim 返回成功后，delivery loop 才可调用 `deliver_summary`；claim 失败必须跳过 adapter 调用，不得把该项重新标成 pending 或制造新的 summary ID。
3. claim 失败或终态冲突均不得复活 cancelled、ACKed 或 expired 的摘要。ACK 和取消必须在其事务中保留各自终态；不能以重试、重扫或重复 ACK 将其改回 active/pending。
4. claim 成功后，外部传输可能已经开始。随后到达的取消或 ACK **不能撤回已开始的外部传输**；本 CR 不承诺网络侧 exactly-once，也不改变 CR-028 的 ACK 业务语义。任何尚未开始的后续投递仍须服从该摘要当前有效状态。
5. 进程重启时，只恢复属于仍 active batch 的 `dispatching` 摘要；已 cancelled、ACKed 或 expired 的 batch/outbox 不得恢复投递。恢复后沿用原 batch ID、summary ID 和冻结 payload。

## 3. 实现边界

- **A3：** 实现 LifeService atomic claim、事务条件、恢复过滤及其 service 测试；仅获准在 `host/desk_agent/main.py::_delivery_loop` 的 `notification.summary.deliver` 分支加入调用 hook，并增加覆盖 hook 次序/claim 失败跳过发送的最小 core-level 测试。
- **A0：** 本 CR 冻结接口状态语义；若实现需要改变摘要 wire schema、CR-028 策略或终态语义，须先提交新的 A0 CR。
- **其他 owner：** 不修改 `main.py` 的其他调用点、设备固件、SDK bridge 或 Host adapter 协议。

## 4. L2 验收门槛

1. 并发两个 claim 时仅一个从 pending 成功转为 dispatching；只有成功者调用 adapter。
2. cancelled、ACKed、expired、非摘要 topic、非 active batch 和已过期摘要均 claim 失败，且不触发 adapter。
3. ACK/取消与 claim 事务竞争时，数据库终态由事务顺序决定；claim 之后已开始的传输不可撤回，终态不得被后续失败处理复活。
4. 重启恢复仅恢复 active batch 的 dispatching 项，复用原 ID/payload；终态项不重播。
5. 测试验证调用 hook 在 adapter 发送之前执行，以及 claim 失败时没有任何传输调用。

上述均为模拟/软件 L2 证据。真实设备接收、摘要 UI 呈现、网络恢复现场表现仍须另行取得 L3/L4 证据。
