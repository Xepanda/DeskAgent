"""
Desk Agent R1 Cloud Vision Analysis Service.

Provides structured multimodal analysis for single JPEG camera snapshots:
- Validates JPEG SOI/EOI framing and bounds.
- Rejects invalid or corrupt images.
- Connects to multimodal vision models (OpenAI/Gemini/Tuya compatible endpoints)
  with fallback to local heuristic evaluation when offline or unconfigured.
- Delivers structured object detection, scene classification, and descriptive tags.
"""

from __future__ import annotations

import base64
import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)

MAX_VISION_JPEG_BYTES = 512 * 1024  # 512 KiB max frame size
DEFAULT_VISION_PROMPT = (
    "Analyze this camera snapshot. Identify key objects, classify the scene, "
    "and provide a concise description."
)


@dataclass
class VisionObject:
    name: str
    confidence: float
    box: Optional[list[float]] = None  # [ymin, xmin, ymax, xmax] normalized 0..1


@dataclass
class VisionAnalysisResult:
    success: bool
    description: str = ""
    scene: str = ""
    tags: list[str] = field(default_factory=list)
    objects: list[dict[str, Any]] = field(default_factory=list)
    timestamp: float = 0.0
    provider: str = "mock"
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def validate_jpeg_bytes(data: Optional[bytes]) -> tuple[bool, Optional[str]]:
    """Validates JPEG byte sequence boundaries and bounds."""
    if not data or not isinstance(data, (bytes, bytearray)):
        return False, "EMPTY_PAYLOAD"
    if len(data) < 4:
        return False, "PAYLOAD_TOO_SMALL"
    if len(data) > MAX_VISION_JPEG_BYTES:
        return False, "PAYLOAD_TOO_LARGE"
    if data[0] != 0xFF or data[1] != 0xD8:
        return False, "MISSING_JPEG_SOI"
    if data[-2] != 0xFF or data[-1] != 0xD9:
        return False, "MISSING_JPEG_EOI"
    return True, None


class BaseVisionProvider:
    """Abstract interface for image analysis providers."""

    def analyze(self, jpeg_bytes: bytes, prompt: Optional[str] = None) -> VisionAnalysisResult:
        raise NotImplementedError


class MockVisionProvider(BaseVisionProvider):
    """Deterministic local provider for offline testing and fallback."""

    def __init__(self, default_scene: str = "desk_workspace") -> None:
        self.default_scene = default_scene

    def analyze(self, jpeg_bytes: bytes, prompt: Optional[str] = None) -> VisionAnalysisResult:
        size = len(jpeg_bytes)
        # Produce deterministic tags and objects based on payload size
        objects = [
            {"name": "desk_surface", "confidence": 0.96},
            {"name": "workspace_item", "confidence": 0.88},
        ]
        tags = ["workspace", "indoor", "desk", f"bytes_{size}"]
        description = f"Desk workspace scene captured via camera ({size} bytes)."

        return VisionAnalysisResult(
            success=True,
            description=description,
            scene=self.default_scene,
            tags=tags,
            objects=objects,
            timestamp=time.time(),
            provider="mock",
        )


class CloudMultimodalVisionProvider(BaseVisionProvider):
    """HTTP client for OpenAI/Gemini/Tuya-compatible multimodal vision endpoints."""

    def __init__(
        self,
        api_key: str,
        api_url: str = "https://api.openai.com/v1/chat/completions",
        model: str = "gpt-4o-mini",
        timeout: float = 10.0,
    ) -> None:
        self.api_key = api_key
        self.api_url = api_url
        self.model = model
        self.timeout = timeout

    def analyze(self, jpeg_bytes: bytes, prompt: Optional[str] = None) -> VisionAnalysisResult:
        b64_image = base64.b64encode(jpeg_bytes).decode("ascii")
        prompt_text = prompt or DEFAULT_VISION_PROMPT

        system_instruction = (
            "You are a computer vision analyzer. Output ONLY valid JSON with keys: "
            "'scene' (string), 'description' (string), 'tags' (list of strings), "
            "and 'objects' (list of objects with 'name' and 'confidence' float 0.0-1.0)."
        )

        payload = {
            "model": self.model,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system_instruction},
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt_text},
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{b64_image}",
                                "detail": "low",
                            },
                        },
                    ],
                },
            ],
            "max_tokens": 500,
        }

        req = urllib.request.Request(
            self.api_url,
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.api_key}",
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
                if status != 200:
                    return VisionAnalysisResult(
                        success=False,
                        error=f"CLOUD_API_HTTP_{status}",
                        timestamp=time.time(),
                        provider="cloud",
                    )
                data = json.loads(body)
                content = data["choices"][0]["message"]["content"]
                parsed = json.loads(content)

                return VisionAnalysisResult(
                    success=True,
                    description=parsed.get("description", ""),
                    scene=parsed.get("scene", "unknown"),
                    tags=parsed.get("tags", []),
                    objects=parsed.get("objects", []),
                    timestamp=time.time(),
                    provider="cloud",
                )
        except urllib.error.HTTPError as e:
            logger.warning("Cloud vision HTTP error: %s", e)
            return VisionAnalysisResult(
                success=False,
                error=f"HTTP_ERROR_{e.code}",
                timestamp=time.time(),
                provider="cloud",
            )
        except Exception as e:
            logger.warning("Cloud vision request exception: %s", e)
            return VisionAnalysisResult(
                success=False,
                error=str(e),
                timestamp=time.time(),
                provider="cloud",
            )


class VisionService:
    """High-level vision analysis service with validation and graceful fallback."""

    def __init__(
        self,
        provider: Optional[BaseVisionProvider] = None,
        api_key: Optional[str] = None,
        api_url: Optional[str] = None,
        model: str = "gpt-4o-mini",
        fallback_to_mock: bool = True,
    ) -> None:
        self.fallback_to_mock = fallback_to_mock
        self._mock_provider = MockVisionProvider()

        if provider is not None:
            self._provider = provider
        elif api_key:
            url = api_url or "https://api.openai.com/v1/chat/completions"
            self._provider = CloudMultimodalVisionProvider(api_key=api_key, api_url=url, model=model)
        else:
            self._provider = self._mock_provider

    def analyze_jpeg(self, jpeg_bytes: bytes, prompt: Optional[str] = None) -> VisionAnalysisResult:
        valid, err = validate_jpeg_bytes(jpeg_bytes)
        if not valid:
            return VisionAnalysisResult(
                success=False,
                error=err or "INVALID_JPEG",
                timestamp=time.time(),
                provider="validator",
            )

        try:
            result = self._provider.analyze(jpeg_bytes, prompt=prompt)
            if not result.success and self.fallback_to_mock and self._provider != self._mock_provider:
                logger.info("Cloud provider failed (%s), falling back to mock provider", result.error)
                fallback_res = self._mock_provider.analyze(jpeg_bytes, prompt=prompt)
                fallback_res.error = f"FALLBACK_FROM_{result.error}"
                return fallback_res
            return result
        except Exception as e:
            logger.warning("Vision analysis error: %s", e)
            if self.fallback_to_mock:
                fallback_res = self._mock_provider.analyze(jpeg_bytes, prompt=prompt)
                fallback_res.error = f"EXCEPTION_FALLBACK_{e}"
                return fallback_res
            return VisionAnalysisResult(
                success=False,
                error=str(e),
                timestamp=time.time(),
                provider="error",
            )
