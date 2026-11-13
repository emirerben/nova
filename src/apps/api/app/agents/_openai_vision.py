"""OpenAI image-input transport for bounded media review.

Deliberately separate from `_text_providers.OpenAITextClient`, which keeps rejecting media.
Frames are sent as inline JPEG bytes and never as a Gemini upload URI. One request, no SDK
retries, nothing stored provider-side. The caller owns the cost reservation: this module
never decides whether a request may be sent.
"""

from __future__ import annotations

import base64
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.agents._runtime import ProviderOutcomeUnknownError, TerminalError
from app.agents._text_providers import _translate
from app.agents.model_registry import SOL
from app.config import settings

VISION_MODEL = SOL
MAX_IMAGES = 6
MAX_IMAGE_BYTES = 128 * 1024
MAX_OUTPUT_TOKENS = 2_500
REASONING_EFFORT = "low"
DEFAULT_TIMEOUT_S = 60.0
_JPEG_SOI = b"\xff\xd8\xff"

ResponseStatus = Literal["completed", "incomplete", "truncated", "refused"]


@dataclass(frozen=True, slots=True)
class VisionUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0


@dataclass(frozen=True, slots=True)
class VisionResult:
    raw_text: str
    response_id: str | None
    status: ResponseStatus
    usage: VisionUsage
    model_used: str | None = None


def validate_frames(frames: Sequence[bytes]) -> None:
    if not 1 <= len(frames) <= MAX_IMAGES:
        raise TerminalError("media review requires one to six frames")
    for frame in frames:
        if (
            not isinstance(frame, bytes)
            or not frame.startswith(_JPEG_SOI)
            or not 0 < len(frame) <= MAX_IMAGE_BYTES
        ):
            raise TerminalError("media review frames must be bounded JPEG bytes")


def _status_of(response: Any) -> ResponseStatus:
    refused = any(
        getattr(content, "type", "") == "refusal"
        for item in response.output
        for content in getattr(item, "content", [])
    )
    reason = getattr(getattr(response, "incomplete_details", None), "reason", None)
    if refused or reason == "content_filter":
        return "refused"
    if reason == "max_output_tokens":
        return "truncated"
    return "completed" if response.status == "completed" else "incomplete"


class OpenAIVisionClient:
    def __init__(self, *, client: Any = None):
        self._client = client

    def _get(self):
        if self._client is None:
            from openai import OpenAI

            self._client = OpenAI(api_key=settings.openai_api_key, max_retries=0)
        return self._client

    def invoke_images(
        self,
        prompt: str,
        frames: Sequence[bytes],
        schema: dict,
        *,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> VisionResult:
        validate_frames(frames)
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        content.extend(
            {
                "type": "input_image",
                "image_url": "data:image/jpeg;base64," + base64.b64encode(frame).decode("ascii"),
                "detail": "high",
            }
            for frame in frames
        )
        client = self._get().with_options(max_retries=0)
        try:
            response = client.responses.create(
                model=VISION_MODEL,
                input=[{"role": "user", "content": content}],
                store=False,
                reasoning={"effort": REASONING_EFFORT},
                max_output_tokens=MAX_OUTPUT_TOKENS,
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "intent_review",
                        "strict": True,
                        "schema": schema,
                    }
                },
                timeout=timeout_s,
            )
            usage = response.usage
            if usage is None:
                raise ValueError("missing usage")
            return VisionResult(
                raw_text=response.output_text or "",
                response_id=response.id,
                status=_status_of(response),
                usage=VisionUsage(
                    input_tokens=int(usage.input_tokens),
                    output_tokens=int(usage.output_tokens),
                    reasoning_tokens=int(
                        getattr(
                            getattr(usage, "output_tokens_details", None), "reasoning_tokens", 0
                        )
                        or 0
                    ),
                    cached_tokens=int(
                        getattr(getattr(usage, "input_tokens_details", None), "cached_tokens", 0)
                        or 0
                    ),
                ),
                model_used=getattr(response, "model", None),
            )
        except Exception as exc:
            if isinstance(getattr(exc, "status_code", None), int) and exc.status_code >= 500:
                # KRI-1171: a paid review is never retried, so a provider 5xx is held as an
                # unknown liability here, unlike a planning call that may fall back.
                raise ProviderOutcomeUnknownError("vision provider outcome unknown") from exc
            raise _translate(exc) from exc
