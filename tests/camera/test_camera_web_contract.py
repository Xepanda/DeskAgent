"""Static contract checks for the local single-frame camera viewer."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "host/desk_agent/web/index.html"


class TestCameraWebContract(unittest.TestCase):
    def test_viewer_requests_authenticated_single_frame_snapshots(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn("/api/camera/snapshot?client_id=", source)
        self.assertIn("headers: getAuthHeaders()", source)
        self.assertNotIn("/api/camera/frame?", source)

    def test_viewer_never_shows_non_hardware_stale_or_undecodable_images(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn('source !== "hardware_sensor"', source)
        self.assertIn("!isHardware", source)
        self.assertIn("ageMs > 2000", source)
        self.assertIn("createImageBitmap(blob)", source)
        self.assertIn("URL.revokeObjectURL(currentObjectUrl)", source)
        self.assertIn("画面已过期", source)
        self.assertIn("Host 连接中断", source)

    def test_viewer_can_release_lease_explicitly_and_on_pagehide(self) -> None:
        source = WEB.read_text(encoding="utf-8")
        self.assertIn('id="closeCamera"', source)
        self.assertIn('window.addEventListener("pagehide"', source)
        self.assertIn('"resource.camera.release"', source)
        self.assertIn("keepalive", source)


if __name__ == "__main__":
    unittest.main()
