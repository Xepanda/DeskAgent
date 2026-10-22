# CR-023 Decision Record：Edge 通过官方 SDK 运行 Managed Application

- **状态：** **A0 源码/部署配置审计完成；A3 实施被依赖解析与 ARM64 部署验证阻塞**
- **范围：** 仅裁决官方 WatcheRobot Python SDK Managed Application 的 Edge 打包/启动路径。本文不改服务、依赖、安装脚本或生产代码，不代表 Edge 已部署。
- **依据：** `contracts/dependency_lock.json` 当前记录的 Python SDK pin：`v0.1.11` / `8bc13967a06ef073459be47e930ff43ef37ee8fb`

## 1. 已核实的现状

1. 该 SDK 源码位于 `.target_logging/sdk-acceptance/vendor/WatcheRobot_python_sdk`，当前 HEAD 与 pin 完全相同，Git tag 为 `v0.1.11`。SDK `pyproject.toml` 的发行名为 `watcherobot`、版本从 `src/watcherobot/__init__.py` 动态读取；项目声明 Python `>=3.10,<3.13`，console script `watcherobot = watcherobot.cli:main`。其依赖声明包括 `websockets>=14,<16`、`av>=16,<17` 等。
2. `tests/requirements-sdk.txt` 明确将 SDK 定位为 test-only source，并要求依赖在本仓保持为测试用；根 `requirements.txt` 的 `websockets>=12,<14` 与 SDK `>=14,<16` 没有共同可安装版本。当前 `deploy/edge/install.sh` 只复制 `host/`、`companion/`、`contracts/` 和根 `requirements.txt`，再安装该文件；它不复制 SDK vendor 源码、安装 `watcherobot` 包或提供 Runtime。
3. 当前 `deploy/edge/desk-agent.service` 使用 `/opt/desk-agent/venv/bin/python3 -m host.desk_agent.main --config /etc/desk-agent/config.json`。这只是直接启动 Host Python 模块，不是 SDK Managed Application 启动器。
4. `host/desk_agent/main.py` 只有在检测到 `WATCHER_APP_ID`、`WATCHER_APP_RUN_CREDENTIAL`、`WATCHER_APP_DESKTOP_WS_URL`、`WATCHER_APP_DEVICE_WS_URL` 等 SDK 注入环境时才进入 `_run_managed_application()`；该路径 import `watcherobot.application.ApplicationContext` 并调用 `ApplicationContext.from_environment()`。SDK `context.py` 明确要求四个变量均存在；裸运行时不会自然产生这些授权运行环境。
5. SDK 使用固定 `app.json` / `app.py` Application 入口。其 CLI 文档与 `cli.py::run_application()` 明确：`watcherobot app run <directory>` 启动或复用 SDK Daemon、选择此本地 App，并由 Daemon 注入 Managed Application 环境；不能直接执行 `app.py` 来替代它。当前 DeskAgent 根目录和 Edge 安装包没有这套 Application manifest/entrypoint。
6. SDK 默认 Runtime state 与 process-instance lock 路径位于用户目录 `~/.watcherobot/...`。SDK 支持 `WATCHER_RUNTIME_STATE_ROOT`、`WATCHER_RUNTIME_INSTANCE_ROOT` 环境覆盖；当前 systemd unit 开启 `ProtectHome=read-only`，仅把 `/var/lib/desk-agent` 放入 `ReadWritePaths`，没有为 Runtime 设置可写 state 路径。

## 2. A3 后续单一实施方案

如后续通过阻塞项，建议只走以下一种路径：把 DeskAgent 封装为 SDK 本地 source Application，由同一固定 SDK Runtime 管理 Host 进程；不再由 systemd 直接调用 `python -m host.desk_agent.main`。

1. **发行物/版本固定：** 从 SDK 的精确 Git commit `8bc13967a06ef073459be47e930ff43ef37ee8fb`（tag `v0.1.11`）构建并纳入 Edge release bundle 的 `watcherobot` wheel；发布清单记录 wheel SHA-256、SDK commit/tag、Python minor 和完整 transitive dependency lock。不得在 Edge 安装时运行无版本的 `pip install watcherobot`，也不以 test vendor 路径作为生产安装源。
2. **Managed Application 入口：** release bundle 提供固定 `app.json` 与 `app.py` wrapper，wrapper 调用 DeskAgent Host 的 managed-entry path，并从 `/etc/desk-agent/config.json` 载入现有配置。该目录供 SDK CLI 作为本地 source Application 运行；Host 进程必须处于 SDK Daemon 注入的环境并通过 `ApplicationContext` 使用单一 SDK Runtime。
3. **systemd 命令：** 服务入口改为等效于：

   ```text
   /opt/desk-agent/venv/bin/watcherobot app run /opt/desk-agent/managed-app
   ```

   systemd 环境显式设置 `WATCHER_RUNTIME_STATE_ROOT=/var/lib/desk-agent/watcher-runtime` 和 `WATCHER_RUNTIME_INSTANCE_ROOT=/var/lib/desk-agent/watcher-runtime-instance`，并确保两个目录在 `ReadWritePaths` 下由 `deskagent` 可写。service stop 应先通过 SDK `watcherobot app stop` 请求 Managed Application 关闭，再由 systemd 回收该 service cgroup；需在目标机验证 stop/start、restart 与 Runtime 单实例行为。

此路径使用 SDK 支持的 `app run` 入口和本地 source app；不需要把官方 Runtime 再写一套，也不直接调用内部 Daemon HTTP API。实施时应将 SDK CLI、Application wrapper、Host 代码和配置路径作为一个可审计的 release bundle 发布。

## 3. 未解除的阻塞 / 最小补充证据

- **依赖冲突（必须先解）：** 本仓 Host `requirements.txt` 的 `websockets>=12,<14` 与 SDK 的 `websockets>=14,<16` 不相交。A3 需要验证并冻结一个同时满足 Host 和 SDK 的版本及完整依赖锁；未有兼容性测试前不能把两份 requirements 简单合并或宣称安装成功。
- **发行物映射（必须先固化）：** 当前锁定的是源码 commit/tag，不是 PyPI wheel 版本/哈希。SDK README 的通用 `pip install watcherobot` 示例不证明 PyPI 上现存 wheel 对应该 commit。Edge 发布流程必须从锁定 commit 产生并校验 wheel/hash，或将等价的精确源码构建工件锁入 release；不能依赖浮动版本。
- **Linux ARM64 / Python（必须实测）：** SDK metadata 只声明 Python `>=3.10,<3.13` 和通用 Python classifiers，没有本仓 TaishanPi/RK3566 Linux ARM64 安装记录。SDK 的 `av` 等依赖包含平台相关 wheel/系统依赖；本机 Windows/WSL 测试不证明 ARM64 可安装或运行。至少需要在目标 TaishanPi 或等价 Linux ARM64 Python 3.10–3.12 环境完成依赖安装、`watcherobot app run` offline smoke、Runtime 注入、真实设备配对/连接与 systemd stop/restart 验证。
- **Runtime 可写目录与生命周期（必须验证）：** `ProtectHome=read-only` 下要使用上述 state/instance root override；需验证权限、日志写入、Daemon 子进程在 systemd cgroup 内的停止顺序及异常重启恢复。
- **Application wrapper/配置（实施时必须落实）：** 当前仓库没有 SDK `app.json` / `app.py` wrapper；需要固定入口并证明其进入 `main.py` managed branch。该 wrapper 应作为受控 Edge release 文件，不在 systemd 内拼任意 Python 命令或手工伪造 `WATCHER_APP_*` 凭据/URL。

## 4. 证据等级

- **L1 已确认：** SDK commit/tag/version/metadata/CLI/API 形状；当前根 requirements、测试说明、Edge installer/systemd 与 Host managed-entry 源码关系。
- **推断，未验：** 以 pinned source 构建的 SDK wheel 在 RK3566 Linux ARM64 可安装/运行；systemd 下 SDK Daemon 与 Application 的启动、停止和故障恢复行为。
- **未完成：** 本轮未构建 wheel、未解析依赖、未访问 Edge 设备、未运行 SDK CLI 或 systemd；以上实施方案是有明确门槛的 A3 建议，不是 Edge 部署通过结论。
