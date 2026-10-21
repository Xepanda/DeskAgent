# CR-006：基于现有单帧捕获的 Host multipart MJPEG 查看

- **提出方：** A0 架构与协议
- **范围：** A3 官方 SDK camera capture 调度、A6 Host latest-frame 与 Web viewer
- **状态：** **Lead/A0 已接受实现路径；未实施、未做硬件流验收**
- **关联：** CR-004、`contracts/camera_frame_delivery.md`、`host/desk_agent/camera/camera_service.py`、`host/desk_agent/web/index.html`
- **限制：** 不改设备 WSPK、不增加设备命令、不修改官方 SDK；不把 multipart MJPEG 称作设备原生连续视频

## 1. 裁决

**当前链路可以在 Host 侧提供 multipart MJPEG HTTP stream。** 复用 A3 经官方 SDK 逐次调用现有 `camera.capture()`、按当前租约刷新（建议目标 2 fps）、A6 `CameraViewerService` 单槽 latest JPEG，以及设备无关的浏览器 HTTP。MJPEG handler 仅在 `frame_seq` 前进且帧仍新鲜时输出一段 JPEG part；浏览器由 1 秒 snapshot polling 改为消费一条长连接，因此可显示每个新到的 capture，不必每秒重新建 HTTP 请求。

这提供**持续的 Host HTTP 图像流**，但设备仍是重复单帧 capture；并未证明传感器连续采集、WSPK 设备主动推流或高帧率视频。实际可见更新率受设备 capture 耗时、相机驱动、Host 调度和网络限制，2 fps 只是目标上限，不是已测性能保证。现有 CR-004 的 CameraViewerService 2000 ms freshness、JPEG 校验及单帧缓存足够作为基础；新增工作属于 Host HTTP/UI 表现边界，不需要新设备协议。

建议新增 A6 endpoint `/api/camera/stream`，使用 `multipart/x-mixed-replace`，boundary 由服务生成。每个 part 至少带 `Content-Type: image/jpeg`、`Content-Length`、`X-Frame-Seq`、`X-Frame-Age-Ms`；可沿用现有 `X-Frame-Source` 和 `X-Frame-Hardware`。part 只包含一张完整、已验证 JPEG，不含 WSPK header。实现不应缓存累计视频或录音。

## 2. Lease、鉴权与生命周期

- 浏览器先沿用当前 `resource.camera.acquire` 获取唯一 Host lease，再以同一 `client_id` 请求 stream；stream handler 要求有效 Bearer Access Token、有效 Host lease，并验证 `client_id` 就是 lease owner。鉴权失败返回 401，缺租约或 owner 不匹配分别返回 403 `NO_ACTIVE_LEASE` / `RESOURCE_BUSY`，不得为 stream 自动创建第二个 lease。
- Host lease 是唯一租约事实来源。A3 仅在 Host lease 有效期间对设备执行 CR-004 定义的 `ctrl.camera.acquire` 与有界串行 `camera.capture()` 刷新；A6 stream 订阅不直接控制设备，不增加新的 lease 或 capture 请求。
- stream 连接不是 lease。浏览器关闭/取消 stream 时释放该 HTTP subscriber；若其他页面/调用仍持有相同 owner 的有效 lease，不因此释放设备 lease。显式 release、owner 断开导致 lease teardown、lease 到期或 Host 退出时，A6 终止对应 subscriber 并清 latest frame，A3 停止刷新并在设备仍连接时发送现有 `ctrl.camera.release`。失联时记为 release 未确认，依赖设备租约超时。
- 防止资源失控：默认每个 lease owner 最多一个活跃 stream subscriber；全 Host 的订阅数也须有小型配置上限。达到上限返回 429/现有服务忙状态，不创建额外 Runtime、socket 或设备 capture loop。所有 disconnect、write error、client cancel 和 lease teardown 路径都必须释放 handler、subscriber slot 和等待者。
- 现有浏览器请求使用自定义 Bearer header，原生 `<img src>` 无法设置 Authorization header。前端应通过带 Authorization header 的 `fetch()` 读取 multipart body 并解析 boundary，或使用同等的受控读取实现；**禁止把长期 Bearer token 放入 URL/query、日志或 multipart part**。如使用 JS reader，释放 lease 时必须 abort fetch、丢弃部分 boundary/part buffer 并清除当前图像。

## 3. 帧、背压与错误语义

- 生产者仍只有 A3 一条 capture scheduler：单设备同时最多一个 command 在途，complete JPEG 交给 A6 的 latest-frame slot。stream handler 只观察 `frame_seq`，每个新 seq 最多发送一次；客户端慢时跳过中间 seq，只发送最新的一帧。禁止按每个 subscriber 建 capture task、无限队列或保留历史 JPEG。
- 只有 `get_latest_frame(lease_owner)` 返回帧且 `frame_age_ms <= 2000`、JPEG 有效、来源为 `hardware_sensor`/硬件帧时才能发 part。不得重新发送相同 seq 伪装新图，不得把合成/placeholder 画面标为硬件帧。
- 没有首帧时 HTTP response headers 尚未发出，最多等待一个有界 capture timeout；无帧则返回现有 unavailable/timeout HTTP error。headers 发出后遇到设备断线、capture 超时、lease 到期或 freshness 超限时，立即停止输出并关闭 multipart response；不得发送 stale JPEG 或缓存图。浏览器以 seq/age 和无新帧超时清除旧显示，明确标注相机不可用/正在恢复，不能留旧图冒充实时画面。
- 收到 malformed/oversized JPEG 时由 A6 现有 JPEG 校验拒收；stream 不生成 part。 capture 恢复后只在新鲜 seq 到达时继续有帧输出；如果连接已关闭，浏览器需在 lease 仍有效时显式重连，且重新鉴权，不复用旧 reader 或旧 part。
- 共享一条 HTTP stream 的频率不高于 A3 最新帧供给频率。建议以 2 fps 为首个目标，并按 `target_fps`/捕获耗时节流；超过能力时丢弃中间帧，不排队，不并发 capture。真实目标 fps 应以设备 Level 3/4 测量调整。

## 4. Owner 分界

- **A3 (`host/desk_agent/connectors/robot/` + `main.py` 最小接线)：** 依 CR-004 拥有唯一官方 SDK camera adapter、Host-to-device lease 映射和串行 capture scheduler；每个 Host lease 一个 scheduler，产出完整 JPEG 调用 A6 `ingest_robot_frame`。不为浏览器 stream 新增第二采集链路。若现有 `/api/camera/stream` 尚无 HTTP route，A3 负责在 `host/desk_agent/main.py` 增加最小 route dispatch/依赖接线，使请求到达 A6 stream service；该改动不得实现 JPEG 缓存、鉴权/lease 业务、multipart 编码或浏览器行为。
- **A6 (`host/desk_agent/camera/`, `host/desk_agent/web/`，以及 route handler 行为)：** 拥有 stream service：multipart boundary 编码、Bearer 鉴权、owner 检查、subscriber 上限、latest-frame 观察、expiry/disconnect 终止和 subscriber 资源释放；拥有浏览器 Bearer fetch/stream parser、帧新鲜度呈现、旧图清除及 cancel/reconnect 行为。A6 可在 `host/desk_agent/main.py` route handler 内实现 A6 HTTP 行为，但需与 A3 的最小 route dispatch/依赖接线按区块协调；A3 不因此取得 camera service/web owner 权限。
- **A1/A2：** 无需改固件或 WSPK；继续提供现有 capture 命令和 WSPK `FRAME_IMAGE`。真实相机帧是否可用仍是单独硬件验收门槛。
- **A7：** 在 Host/UI 实现后验证真实设备持续帧、帧时效、关闭页面/释放租约/断线恢复及资源清理；不得用 fixture 或 mock 证明真机 stream。

## 5. 实现验收门槛

1. 合法 Bearer + 当前 lease owner 可连接，其他 owner、缺 lease 和 invalid token 被拒绝；token 不出现在 URL、HTML、日志或 part 中。
2. 未修改设备 WSPK，官方 SDK scheduler 顺序请求现有 capture；设备源帧到达后 multipart 中的 JPEG 可解码、seq 递增、age 有效，UI 更新不高于实际帧率。慢 client 不导致队列/内存无限增长或并发 capture。
3. 帧缺失/超过 2000 ms、capture timeout、设备/Runtime disconnect、lease expiry/release 均导致旧图清除、stream 终止或不可用状态；新 lease/新连接必须等新鲜帧，不能复用旧帧。
4. 用户关闭页面或取消 stream 后，subscriber/socket/等待器释放；若无其他 lease 持有方，设备 refresh 停止并释放 lease。server shutdown 清理所有 stream task。
5. mock multipart/JPEG 单测只算 Level 2。Level 3 需证明真机在目标连续时间内输出多个真实、可解码、内容对应现场且 seq 递增的 JPEG；Level 4 还需证明页面关闭、租约到期、网络/设备断线和重连后无旧画面及资源泄漏。

## 6. 待决

Lead/A0 已接受本方案作为当前视频查看实现路径：浏览器持续取得新帧，Host MJPEG 目标上限 2 FPS；遵循唯一 camera lease、鉴权、新鲜帧以及断线/释放清除规则。如果“实时推流”意指设备相机 sensor 连续视频编码/主动媒体推送，则当前 `ctrl.camera.capture` + WSPK 单帧合同不能证明可行，需要另立设备视频媒体契约并评估芯片、带宽、功耗和资源，不属于本 CR，也不能通过循环 capture 冒称原生视频流。
