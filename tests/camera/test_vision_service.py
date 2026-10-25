"""Unit tests for VisionService and Multimodal Vision Providers."""

import time
import unittest
from unittest.mock import MagicMock, patch

from host.desk_agent.camera.vision_service import (
    MAX_VISION_JPEG_BYTES,
    BaseVisionProvider,
    CloudMultimodalVisionProvider,
    MockVisionProvider,
    VisionAnalysisResult,
    VisionService,
    validate_jpeg_bytes,
)


class TestVisionService(unittest.TestCase):
    def setUp(self) -> None:
        self.valid_jpeg = b"\xff\xd8" + b"\x00" * 32 + b"\xff\xd9"

    def test_validate_jpeg_bytes(self) -> None:
        # Empty or non-bytes
        self.assertEqual(validate_jpeg_bytes(None), (False, "EMPTY_PAYLOAD"))
        self.assertEqual(validate_jpeg_bytes(b""), (False, "EMPTY_PAYLOAD"))
        self.assertEqual(validate_jpeg_bytes("not bytes"), (False, "EMPTY_PAYLOAD"))

        # Too small
        self.assertEqual(validate_jpeg_bytes(b"\xff\xd8\xff"), (False, "PAYLOAD_TOO_SMALL"))

        # Missing SOI
        self.assertEqual(validate_jpeg_bytes(b"\x00\x00\x00\xff\xd9"), (False, "MISSING_JPEG_SOI"))

        # Missing EOI
        self.assertEqual(validate_jpeg_bytes(b"\xff\xd8\x00\x00\x00"), (False, "MISSING_JPEG_EOI"))

        # Too large
        oversized = b"\xff\xd8" + (b"\x00" * (MAX_VISION_JPEG_BYTES + 10)) + b"\xff\xd9"
        self.assertEqual(validate_jpeg_bytes(oversized), (False, "PAYLOAD_TOO_LARGE"))

        # Valid
        self.assertEqual(validate_jpeg_bytes(self.valid_jpeg), (True, None))

    def test_mock_vision_provider(self) -> None:
        provider = MockVisionProvider(default_scene="living_room")
        res = provider.analyze(self.valid_jpeg)

        self.assertTrue(res.success)
        self.assertEqual(res.scene, "living_room")
        self.assertIn("workspace", res.tags)
        self.assertGreater(len(res.objects), 0)
        self.assertEqual(res.provider, "mock")
        self.assertIsNone(res.error)

    def test_vision_service_validation_rejection(self) -> None:
        svc = VisionService()
        bad_payload = b"not a jpeg at all"
        res = svc.analyze_jpeg(bad_payload)

        self.assertFalse(res.success)
        self.assertEqual(res.error, "MISSING_JPEG_SOI")
        self.assertEqual(res.provider, "validator")

    def test_vision_service_default_mock_execution(self) -> None:
        svc = VisionService()
        res = svc.analyze_jpeg(self.valid_jpeg, prompt="What is on the desk?")

        self.assertTrue(res.success)
        self.assertEqual(res.scene, "desk_workspace")
        self.assertGreater(len(res.objects), 0)
        self.assertIsNone(res.error)

    def test_vision_service_fallback_on_cloud_failure(self) -> None:
        mock_cloud = MagicMock(spec=BaseVisionProvider)
        mock_cloud.analyze.return_value = VisionAnalysisResult(
            success=False,
            error="RATE_LIMITED_429",
            provider="cloud",
        )

        svc = VisionService(provider=mock_cloud, fallback_to_mock=True)
        res = svc.analyze_jpeg(self.valid_jpeg)

        self.assertTrue(res.success)
        self.assertEqual(res.scene, "desk_workspace")
        self.assertIn("FALLBACK_FROM_RATE_LIMITED_429", res.error)

    def test_vision_service_fallback_on_cloud_exception(self) -> None:
        mock_cloud = MagicMock(spec=BaseVisionProvider)
        mock_cloud.analyze.side_effect = ConnectionError("Network unreachable")

        svc = VisionService(provider=mock_cloud, fallback_to_mock=True)
        res = svc.analyze_jpeg(self.valid_jpeg)

        self.assertTrue(res.success)
        self.assertEqual(res.scene, "desk_workspace")
        self.assertIn("EXCEPTION_FALLBACK", res.error)


if __name__ == "__main__":
    unittest.main()
