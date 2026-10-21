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
        self.assertIn("ageMs > 2000", source)
        self.assertIn("createImageBitmap(blob)", source)
        self.assertIn("URL.revokeObjectURL(currentObjectUrl)", source)
        self.assertIn("画面已过期", source)
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
  const head = `--${b}\r\nContent-Type: ${type}\r\nContent-Length: ${jpeg.length}\r\nX-Frame-Seq: 7\r\nX-Frame-Age-Ms: 3\r\n\r\n`;
  const out = new Uint8Array(enc.encode(head).length + jpeg.length + enc.encode(sep).length);
  let offset = 0; out.set(enc.encode(head), offset); offset += enc.encode(head).length;
  out.set(jpeg, offset); offset += jpeg.length; out.set(enc.encode(sep), offset);
  return out;
}
let valid = parseMjpegBuffer(part(), boundary);
assert.equal(valid.parts.length, 1);
assert.equal(valid.parts[0].seq, "7");
assert.equal(valid.parts[0].ageMs, 3);
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

    def test_disconnect_expiry_and_page_exit_clear_or_stop_the_viewer(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn("frameExpiryTimer", source)
        self.assertIn("streamAbortController", source)
        self.assertIn("streamAbortController.abort()", source)
        self.assertIn("releaseCameraLease(true)", source)


if __name__ == "__main__":
    unittest.main()
