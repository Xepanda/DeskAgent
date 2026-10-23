"""Static contract checks for the local single-frame camera viewer."""

from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "host/desk_agent/web/index.html"


class TestCameraWebContract(unittest.TestCase):
    def test_viewer_requests_authenticated_mjpeg_stream(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn("/api/camera/stream?client_id=", source)
        self.assertIn("headers: getAuthHeaders()", source)
        self.assertNotIn("/api/camera/frame?", source)
        self.assertIn("multipart\\/x-mixed-replace", source)
        self.assertIn("response.body.getReader()", source)
        self.assertIn("boundary=", source)

    def test_viewer_never_shows_non_hardware_stale_or_undecodable_images(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn("effectiveAgeMs > 1000", source)
        self.assertIn("createImageBitmap(blob)", source)
        self.assertIn("URL.revokeObjectURL(currentObjectUrl)", source)
        self.assertIn("LIVE 不可用：超过 1 秒没有新帧", source)
        self.assertIn("Host 暂时连接中断", source)

    def test_viewer_can_release_lease_explicitly_and_on_pagehide(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn('id="closeCamera"', source)
        self.assertIn('window.addEventListener("pagehide"', source)
        self.assertIn('"resource.camera.release"', source)
        self.assertIn("keepalive", source)

    def test_viewer_consumes_stream_without_snapshot_polling(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn("consumeMjpeg(response, controller, boundary)", source)
        self.assertIn("Content-Length", source)
        self.assertIn("MJPEG boundary 不匹配", source)
        self.assertIn("MJPEG part Content-Type 无效", source)
        self.assertIn("MJPEG part 缺少 CRLF/boundary", source)
        self.assertIn("multipart\\/x-mixed-replace", source)
        self.assertNotIn("refreshSnapshot", source)
        self.assertIn("cache: \"no-store\"", source)
        self.assertIn("实时相机画面（LIVE · 目标 15 帧/秒）", source)
        self.assertIn("rollingFps(streamFrameTimes, now)", source)
        self.assertIn("rollingFps(renderedFrameTimes, now)", source)
        self.assertIn("uncertainty <= 100", source)
        self.assertIn("X-Device-Capture-Monotonic-Ms", source)
        self.assertIn("X-Host-Receive-Monotonic-Ms", source)
        self.assertIn("X-Clock-Offset-Ms", source)
        self.assertIn("X-Clock-Uncertainty-Ms", source)
        self.assertIn("X-Session-Generation", source)

    def test_transient_unavailable_and_stream_end_retry_without_releasing_lease(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn("response.status === 503", source)
        self.assertIn("response.status === 429 || response.status === 503", source)
        self.assertIn("相机流订阅正在释放，保留租约并重试", source)
        self.assertIn("相机流已结束，保留租约并重连", source)
        self.assertIn("Host 暂时连接中断，保留租约并重试", source)
        self.assertIn("waitForRetry", source)
        self.assertIn("Math.min(5000, 500 * (2 **", source)
        self.assertIn("markStreamRetrying", source)
        self.assertIn("rpcCall(\"resource.camera.release\"", source)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the browser multipart parser")
    def test_mjpeg_parser_accepts_complete_parts_and_rejects_bad_boundary_type_or_crlf(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        script = source.split("<script>", 1)[1].split("</script>", 1)[0]
        helpers = script[script.index("function concatBytes"):script.index("async function renderStreamFrame")]
        checks = r'''
const assert = require("node:assert/strict");
const enc = new TextEncoder();
const boundary = "demo-boundary";
const jpeg = Uint8Array.from([255, 216, 1, 2, 255, 217]);
function part(type = "image/jpeg", sep = "\r\n--" + boundary + "\r\n", b = boundary) {
  const head = `--${b}\r\nContent-Type: ${type}\r\nContent-Length: ${jpeg.length}\r\nX-Frame-Seq: 7\r\nX-Frame-Age-Ms: 3\r\nX-Device-Sequence: 7\r\nX-Device-Capture-Monotonic-Ms: 1234\r\nX-Host-Receive-Monotonic-Ms: 2000\r\nX-Clock-Offset-Ms: 100.0\r\nX-Clock-Uncertainty-Ms: 20.0\r\nX-Source-To-Host-Receive-Ms: 766.0\r\nX-Session-Generation: session-a\r\n\r\n`;
  const out = new Uint8Array(enc.encode(head).length + jpeg.length + enc.encode(sep).length);
  let offset = 0; out.set(enc.encode(head), offset); offset += enc.encode(head).length;
  out.set(jpeg, offset); offset += jpeg.length; out.set(enc.encode(sep), offset);
  return out;
}
let valid = parseMjpegBuffer(part(), boundary);
assert.equal(valid.parts.length, 1);
assert.equal(valid.parts[0].seq, "7");
assert.equal(valid.parts[0].ageMs, 3);
assert.equal(valid.parts[0].deviceSeq, 7);
assert.equal(valid.parts[0].captureTimestampMs, 1234);
assert.equal(valid.parts[0].hostReceiveMonotonicMs, 2000);
assert.equal(valid.parts[0].clockOffsetMs, 100);
assert.equal(valid.parts[0].clockUncertaintyMs, 20);
assert.equal(valid.parts[0].sourceLatencyMs, 766);
assert.equal(valid.parts[0].sessionGeneration, "session-a");
assert.deepEqual(Array.from(valid.parts[0].jpeg), Array.from(jpeg));
assert.equal(new TextDecoder().decode(valid.remainder), `--${boundary}\r\n`);
for (const invalid of [part("text/plain"), part("image/jpeg", "\n--" + boundary + "\r\n"), part("image/jpeg", undefined, "wrong-boundary")]) {
  assert.throws(() => parseMjpegBuffer(invalid, boundary));
}
const split = part();
const cut = split.length - 5;
const incomplete = parseMjpegBuffer(split.slice(0, cut), boundary);
assert.equal(incomplete.parts.length, 0);
const completed = parseMjpegBuffer(concatBytes(incomplete.remainder, split.slice(cut)), boundary);
assert.equal(completed.parts.length, 1);
''';
        completed = subprocess.run(
            [shutil.which("node"), "-e", helpers + "\n" + checks],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the browser multipart consumer")
    def test_mjpeg_consumer_does_not_refresh_view_for_duplicate_frame_sequence(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        script = source.split("<script>", 1)[1].split("</script>", 1)[0]
        helpers = script[script.index("function concatBytes"):script.index("function waitForRetry")]
        checks = r'''
const assert = require("node:assert/strict");
const enc = new TextEncoder();
const boundary = "demo-boundary";
const jpeg = Uint8Array.from([255, 216, 1, 2, 255, 217]);
function part(seq) {
  const header = `--${boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.length}\r\nX-Frame-Seq: ${seq}\r\nX-Frame-Age-Ms: 3\r\n\r\n`;
  const head = enc.encode(header);
  const tail = enc.encode(`\r\n--${boundary}\r\n`);
  const out = new Uint8Array(head.length + jpeg.length + tail.length);
  out.set(head); out.set(jpeg, head.length); out.set(tail, head.length + jpeg.length);
  return out;
}
const all = concatBytes(part("7"), part("7"));
let read = false;
let createdUrls = 0;
const controller = { signal: { aborted: false } };
global.leaseActive = true;
global.frameExpiryTimer = null;
global.lastFrameSeq = null;
global.currentObjectUrl = null;
global.streamFrameTimes = [];
global.renderedFrameTimes = [];
global.pendingRenderMetric = null;
global.document = { getElementById: () => ({ style: {}, innerText: "" }) };
global.Blob = class { constructor(parts) { this.parts = parts; } };
global.createImageBitmap = async () => ({ close() {} });
global.URL = { createObjectURL: () => `blob:${++createdUrls}`, revokeObjectURL() {} };
global.clearTimeout = () => {};
global.setTimeout = () => 1;
const response = { body: { getReader: () => ({
  async read() { if (read) return { done: true }; read = true; return { done: false, value: all }; },
  async cancel() {}
}) } };
(async () => {
  await consumeMjpeg(response, controller, boundary);
  assert.equal(createdUrls, 1, "a duplicate frame sequence must not repaint or refresh freshness");
})().catch(err => { console.error(err); process.exitCode = 1; });
''';
        completed = subprocess.run(
            [shutil.which("node"), "-e", helpers + "\n" + checks],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute the browser multipart consumer")
    def test_aborted_camera_stream_does_not_render_a_chunk_returned_after_abort(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        script = source.split("<script>", 1)[1].split("</script>", 1)[0]
        helpers = script[script.index("function concatBytes"):script.index("function waitForRetry")]
        checks = r'''
const assert = require("node:assert/strict");
const enc = new TextEncoder();
const boundary = "demo-boundary";
const jpeg = Uint8Array.from([255, 216, 1, 2, 255, 217]);
const head = enc.encode(`--${boundary}\r\nContent-Type: image/jpeg\r\nContent-Length: ${jpeg.length}\r\nX-Frame-Seq: 1\r\nX-Frame-Age-Ms: 5\r\n\r\n`);
const tail = enc.encode(`\r\n--${boundary}\r\n`);
const part = new Uint8Array(head.length + jpeg.length + tail.length);
part.set(head); part.set(jpeg, head.length); part.set(tail, head.length + jpeg.length);
let finishRead;
const controller = new AbortController();
let createdUrls = 0;
global.leaseActive = true;
global.frameExpiryTimer = null;
global.currentObjectUrl = null;
global.lastFrameSeq = null;
global.streamFrameTimes = [];
global.renderedFrameTimes = [];
global.pendingRenderMetric = null;
global.performance = { now: () => 0 };
global.document = { getElementById: () => ({ style: {}, innerText: "", removeAttribute() {} }) };
global.Blob = class { constructor(parts) { this.parts = parts; } };
global.createImageBitmap = async () => ({ close() {} });
global.URL = { createObjectURL: () => `blob:${++createdUrls}`, revokeObjectURL() {} };
global.clearTimeout = () => {};
global.setTimeout = () => 1;
const response = { body: { getReader: () => ({
  read() { return new Promise(resolve => { finishRead = resolve; }); },
  async cancel() {}
}) } };
(async () => {
  const task = consumeMjpeg(response, controller, boundary);
  while (!finishRead) await Promise.resolve();
  controller.abort();
  finishRead({ done: false, value: part });
  await task;
  assert.equal(createdUrls, 0, "a chunk returned after stream abort must never repaint the closed stream");
})().catch(err => { console.error(err); process.exitCode = 1; });
''';
        completed = subprocess.run(
            [shutil.which("node"), "-e", helpers + "\n" + checks],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute browser freshness handling")
    def test_browser_hides_expired_frame_and_abort_stops_retry_wait(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        script = source.split("<script>", 1)[1].split("</script>", 1)[0]
        helpers = script[script.index("function hideFrame"):script.index("function markStreamRetrying")]
        checks = r'''
const assert = require("node:assert/strict");
const nodes = new Map();
for (const id of ["cameraFrame", "videoPlaceholder", "frameStats", "cameraConnectionStatus"]) {
  nodes.set(id, { style: {}, innerText: "", removeAttribute() {} });
}
global.document = { getElementById: id => nodes.get(id) };
global.leaseActive = true;
global.frameExpiryTimer = null;
global.currentObjectUrl = null;
global.lastFrameSeq = null;
let expiry;
let created = 0;
global.Blob = class { constructor(parts) { this.parts = parts; } };
global.createImageBitmap = async () => ({ close() {} });
global.URL = { createObjectURL: () => `blob:${++created}`, revokeObjectURL() {} };
global.clearTimeout = () => {};
global.setTimeout = callback => { expiry = callback; return 1; };
(async () => {
  const jpeg = Uint8Array.from([255, 216, 1, 2, 255, 217]);
  await renderStreamFrame(jpeg, { seq: 4, ageMs: 1001 }, 0);
  assert.equal(created, 0, "a stale incoming frame must never be displayed");
  await renderStreamFrame(jpeg, { seq: 5, ageMs: 10 }, 0);
  assert.equal(created, 1);
  assert.equal(nodes.get("cameraFrame").style.display, "block");
  expiry();
  assert.equal(nodes.get("cameraFrame").style.display, "none", "the last image must disappear at its freshness deadline");
  assert.equal(nodes.get("cameraConnectionStatus").innerText, "不可用");

  const controller = new AbortController();
  const pendingRetry = waitForRetry(30000, controller.signal);
  controller.abort();
  await pendingRetry;
  assert.equal(controller.signal.aborted, true, "closing the viewer must cancel a pending reconnect delay");
})().catch(err => { console.error(err); process.exitCode = 1; });
''';
        completed = subprocess.run(
            [shutil.which("node"), "-e", helpers + "\n" + checks],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute browser frame freshness handling")
    def test_browser_includes_network_and_decode_delay_in_frame_freshness(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        script = source.split("<script>", 1)[1].split("</script>", 1)[0]
        helpers = script[script.index("function rollingFps"):script.index("async function consumeMjpeg")]
        checks = r'''
const assert = require("node:assert/strict");
const nodes = new Map();
for (const id of ["cameraFrame", "videoPlaceholder", "frameStats", "cameraConnectionStatus"]) {
  nodes.set(id, { style: {}, innerText: "", removeAttribute() {} });
}
global.document = { getElementById: id => nodes.get(id) };
global.leaseActive = true;
global.frameExpiryTimer = null;
global.currentObjectUrl = null;
global.lastFrameSeq = null;
global.streamFrameTimes = [];
global.renderedFrameTimes = [];
global.pendingRenderMetric = null;
let now = 0;
let created = 0;
global.performance = { now: () => now };
global.Blob = class { constructor(parts) { this.parts = parts; } };
global.createImageBitmap = async () => { now += 120; return { close() {} }; };
global.URL = { createObjectURL: () => `blob:${++created}`, revokeObjectURL() {} };
global.clearTimeout = () => {};
global.setTimeout = () => 1;
(async () => {
  await renderStreamFrame(Uint8Array.from([255, 216, 1, 2, 255, 217]), { seq: 1, ageMs: 950 }, 0);
  assert.equal(created, 0, "a frame that expires during image decoding must never appear");

  now = 0;
  global.createImageBitmap = async () => { now += 80; return { close() {} }; };
  await renderStreamFrame(Uint8Array.from([255, 216, 1, 2, 255, 217]), { seq: 2, ageMs: 500 }, 0);
  assert.equal(created, 1);
  assert.match(nodes.get("frameStats").innerText, /新鲜度: 580 ms/);
  nodes.get("cameraFrame").onload();
  assert.match(nodes.get("frameStats").innerText, /采集到显示 未知/);

  now = 0;
  await renderStreamFrame(Uint8Array.from([255, 216, 1, 2, 255, 217]), {
    seq: 3, ageMs: 10, sourceLatencyMs: 100, clockUncertaintyMs: 50,
  }, 0);
  nodes.get("cameraFrame").onload();
  assert.match(nodes.get("frameStats").innerText, /接收 0\.0 FPS/);
  assert.match(nodes.get("frameStats").innerText, /采集到显示 180 ms/);
})().catch(err => { console.error(err); process.exitCode = 1; });
''';
        completed = subprocess.run(
            [shutil.which("node"), "-e", helpers + "\n" + checks],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    @unittest.skipUnless(shutil.which("node"), "Node.js is required to execute browser frame rendering")
    def test_late_image_load_from_replaced_frame_does_not_update_live_metrics(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        script = source.split("<script>", 1)[1].split("</script>", 1)[0]
        helpers = script[script.index("function hideFrame"):script.index("async function consumeMjpeg")]
        checks = r'''
const assert = require("node:assert/strict");
const nodes = new Map();
for (const id of ["cameraFrame", "videoPlaceholder", "frameStats", "cameraConnectionStatus"]) {
  nodes.set(id, { style: {}, innerText: "", removeAttribute() {} });
}
const image = nodes.get("cameraFrame");
global.document = { getElementById: id => nodes.get(id) };
global.leaseActive = true;
global.frameExpiryTimer = null;
global.currentObjectUrl = null;
global.lastFrameSeq = null;
global.streamFrameTimes = [];
global.renderedFrameTimes = [];
global.pendingRenderMetric = null;
let now = 0;
let created = 0;
global.performance = { now: () => now };
global.Blob = class { constructor(parts) { this.parts = parts; } };
global.createImageBitmap = async () => ({ close() {} });
global.URL = { createObjectURL: () => `blob:${++created}`, revokeObjectURL() {} };
global.clearTimeout = () => {};
global.setTimeout = () => 1;
(async () => {
  const jpeg = Uint8Array.from([255, 216, 1, 2, 255, 217]);
  await renderStreamFrame(jpeg, { seq: 10, ageMs: 5, sourceLatencyMs: 50, clockUncertaintyMs: 10 }, 0);
  const firstLoad = image.onload;
  await renderStreamFrame(jpeg, { seq: 11, ageMs: 7, sourceLatencyMs: 80, clockUncertaintyMs: 10 }, 0);
  const currentStats = nodes.get("frameStats").innerText;
  assert.match(currentStats, /LIVE \| 帧序号: #11 \| 新鲜度: 7 ms/);
  now = 100;
  firstLoad();
  assert.equal(
    nodes.get("frameStats").innerText,
    currentStats,
    "a delayed load event for a replaced image must not publish metrics for the newer image"
  );
  image.onload();
  assert.match(nodes.get("frameStats").innerText, /采集到显示 180 ms/);
})().catch(err => { console.error(err); process.exitCode = 1; });
''';
        completed = subprocess.run(
            [shutil.which("node"), "-e", helpers + "\n" + checks],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_disconnect_expiry_and_page_exit_clear_or_stop_the_viewer(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn("frameExpiryTimer", source)
        self.assertIn("streamAbortController", source)
        self.assertIn("streamAbortController.abort()", source)
        self.assertIn("releaseCameraLease(true)", source)


if __name__ == "__main__":
    unittest.main()
