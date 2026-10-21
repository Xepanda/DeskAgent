# CR-001：配对码校验失败响应与设备侧配对码来源未定义

- **提出方**：A0 架构与协议
- **提出日期**：2026-10-17
- **关联**：PR #11 审查工作包 4、`docs/implementation/interfaces.md` §6.4（裁决 R4）
- **状态**：配对码来源/生命周期已由 A0 在 [CR-003](CR-003-pairing-code-production-injection.md) 给出推荐裁决；失败 wire response 仍按静默拒绝处理。本文件保留为历史问题背景，不再作为当前实施契约。

> 当前契约以 `interfaces.md` §6.4 和 CR-003 为准。本文记录原始缺口与 fail-closed 背景；Q2 的产品生成来源不再开放，剩余实施/UI验收由 CR-003 跟踪。
- **影响模块**：A2（`firmware/components/desk_sdk_bridge/`）

---

## 1. 问题陈述

PR #11 审查发现 `desk_sdk_bridge_process_pairing_udp()` 读取 `pairing_code` 后**从未校验**，
即无条件 `s_paired = true` 并发放 `session_token`。修复要求"缺失、错误、过期的配对凭据必须拒绝"。

但在实现"拒绝"时发现**两处契约缺口**，按 AGENTS.md 第 6.3 条「遇到接口缺失的处理方式」，
不得自行发明协议，故提交本 CR。

---

## 2. 缺口一：官方契约未定义"配对码错误"的响应

**证据（Level 1，外部锁定官方 SDK）**：
`watcherobot/runtime/daemon/pairing/protocol.py`

```python
_LEGACY_UDP_TYPES = ("pair.request", "pair.accept", "pair.busy", "pair.cancel")
_UDP_FIELDS = {
    "pair.request": _COMMON_UDP_FIELDS | {"pairing_code", "target_mode", "websocket_port"},
    "pair.accept":  _COMMON_UDP_FIELDS | {"target_mode", "session_token"},
    "pair.busy":    _COMMON_UDP_FIELDS | {"reason"},
    "pair.cancel":  _COMMON_UDP_FIELDS | {"session_token"},
}
```

```python
if message_type == "pair.busy":
    return PairBusy(
        request_id=request_id,
        daemon_instance_id=daemon_instance_id,
        reason=_require_literal(message, "reason", "device_session_active"),   # 仅此一个字面量
    )
```

**结论**：官方 UDP 配对契约中，`pair.busy` 的 `reason` **仅允许** `"device_session_active"`
（语义为"设备已有活动会话"）。契约**不存在** `pair.reject`、`pair.denied`
或任何用于表达"配对码错误/缺失/过期"的消息类型。

**待裁决问题 Q1**：设备在配对码校验失败时，应当：
- (a) 静默丢弃请求（不回复任何配对响应）——**当前采用的临时策略**；
- (b) 复用 `pair.busy` 并扩展其 `reason` 字面量集合（**属修改公共契约**，需官方 SDK 同步）；
- (c) 由官方 SDK 后续版本定义新的拒绝消息类型。

---

## 3. 缺口二：设备侧"期望配对码"的来源未定义

**现状**：本仓库中 `pairing_code` 仅作为**入参被解析**，没有任何：
- 设备侧期望码的配置项；
- 期望码的持久化存储；
- 期望码的 provisioning / 注入接口；
- 期望码的展示（用户如何得知应输入哪 6 位码）。

现有测试（`tests/sdk_contract/test_sdk_bridge_protocol.py`、`test_official_sdk_runtime_compat.py`）
均以硬编码 `"123456"` 作为请求中的配对码，而设备侧无任何期望值与之比对——
即这些测试实际**固化的是漏洞行为**。

**待裁决问题 Q2**：设备侧期望配对码的**权威来源**应为：
- (a) 出厂 provisioning 写入设备 NVS/Flash（由 A1 板级配置提供）；
- (b) 设备启动时随机生成并在屏幕显示，用户抄录后输入 SDK；
- (c) 由 Tuya 云端下发绑定。

该决定影响 A1（存储/显示）、A2（校验）、以及产品交互流程，超出本任务范围。

---

## 4. 本任务采取的临时策略（fail-closed）

在 Q1/Q2 裁决前，A2 按 `interfaces.md` §6.4 裁决 R4 实施**保守且不发明协议**的行为：

1. 新增显式配置注入接口 `desk_sdk_bridge_set_pairing_code(const char *code)`；
2. 设备侧**未配置**期望码时 → **拒绝配对**（不置 `s_paired`、不发 token、不回复 `pair.accept`）；
3. 设备侧**已配置**期望码时 → 要求请求中 `pairing_code` 为 6 位数字且**精确匹配**，否则拒绝；
4. 失败路径**不新增**任何 wire 消息类型（即缺口一的 (a) 方案）；
5. `pairing_code` 与 `session_token` **严禁**写入日志（按官方 `redact_sensitive_fields` 规则脱敏）。

**该策略不代表官方配对流程已完成**，仅确保"未经验证不得授予"这一安全底线。
Q1/Q2 裁决后需按官方结论补齐失败响应与期望码来源。

---

## 5. 原始请求与后续裁决

原始请求是请 A0 / 产品 / 官方 SDK 维护方就 Q1、Q2 给出结论。后续 A0 以 CR-003 推荐设备本地生成、现场显示、短时 RAM 注入作为 Q2 的当前产品方案；Q1 采用静默 fail-closed。CR-003 的真机验收未通过前，PR #11 或本仓配对相关改动**不得**被宣称为“官方配对流程已闭环”。
