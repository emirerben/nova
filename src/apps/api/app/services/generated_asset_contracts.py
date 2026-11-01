"""Offline generated-asset contracts. No credentials, transport, storage or rendering."""

from __future__ import annotations

import base64
import binascii
import io
import re
import unicodedata
import wave
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

IMAGE_MODEL = "gpt-image-2.5-sunburst"
NARRATION_MODEL = "gpt-realtime-2.1-mini"
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_AUDIO_BYTES = 60 * 24000 * 2


@dataclass(frozen=True, slots=True)
class ImageRequest:
    prompt: str
    editable_title: str = ""
    size: str = "1024x1536"
    quality: str = "low"
    transparent_background: bool = False


@dataclass(frozen=True, slots=True)
class NarrationRequest:
    approved_script: str
    voice: str = "marin"


@dataclass(frozen=True, slots=True)
class GeneratedAsset:
    content: bytes
    mime_type: str
    model: str
    usage: dict[str, Any]
    provider_request_id: str | None = None
    admission_status: str = "not_registered"
    editable_title: str = ""


def image_payload(request: ImageRequest) -> dict:
    if not request.prompt.strip() or len(request.prompt) > 4000:
        raise ValueError("image prompt must contain 1 to 4000 characters")
    if request.size not in {"1024x1024", "1024x1536", "1536x1024"}:
        raise ValueError("explicit supported image size required")
    if request.quality not in {"low", "medium", "high"}:
        raise ValueError("explicit bounded image quality required")
    return {
        "model": IMAGE_MODEL,
        "prompt": "Create only the visual asset. Do not include lettering, captions or titles.\n"
        + request.prompt,
        "size": request.size,
        "quality": request.quality,
        "n": 1,
        "output_format": "png",
        **({"background": "transparent"} if request.transparent_background else {}),
    }


def narration_events(request: NarrationRequest) -> list[dict]:
    if not request.approved_script.strip() or len(request.approved_script) > 1500:
        raise ValueError("approved narration must contain 1 to 1500 characters")
    if request.voice not in {"marin", "cedar"}:
        raise ValueError("unsupported preset voice")
    return [
        {
            "type": "session.update",
            "session": {
                "type": "realtime",
                "model": NARRATION_MODEL,
                "output_modalities": ["audio"],
                "audio": {
                    "output": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "voice": request.voice,
                    }
                },
            },
        },
        {
            "type": "response.create",
            "response": {
                "conversation": "none",
                "output_modalities": ["audio"],
                "max_output_tokens": 4096,
                "instructions": (
                    "Read the supplied approved script verbatim. "
                    "Add no introduction, translation or commentary."
                ),
                "input": [
                    {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": request.approved_script}],
                    }
                ],
            },
        },
    ]


def _bytes(value: str, limit: int) -> bytes:
    if not isinstance(value, str) or len(value) > ((limit + 2) // 3) * 4:
        raise ValueError("missing or excessive encoded asset bytes")
    try:
        content = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("invalid asset encoding") from exc
    if not content or len(content) > limit:
        raise ValueError("empty or excessive asset bytes")
    return content


def _usage(value) -> dict:
    if not isinstance(value, dict) or any(
        type(value.get(k)) is not int or value[k] < 0
        for k in ("input_tokens", "output_tokens", "total_tokens")
    ):
        raise ValueError("complete nonnegative token usage required")
    if value["total_tokens"] != value["input_tokens"] + value["output_tokens"]:
        raise ValueError("inconsistent token usage")
    return dict(value)


def decode_image(response: dict, request: ImageRequest) -> GeneratedAsset:
    image_payload(request)
    from PIL import Image, UnidentifiedImageError

    data = response.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise ValueError("exactly one generated image required")
    content = _bytes(data[0].get("b64_json"), MAX_IMAGE_BYTES)
    try:
        with Image.open(io.BytesIO(content)) as image:
            expected_size = tuple(int(edge) for edge in request.size.split("x"))
            if image.format != "PNG" or image.size != expected_size:
                raise ValueError("unexpected generated image format or dimensions")
            image.verify()
        with Image.open(io.BytesIO(content)) as image:
            image.load()
    except (UnidentifiedImageError, OSError, SyntaxError) as exc:
        raise ValueError("invalid generated image") from exc
    return GeneratedAsset(
        content,
        "image/png",
        IMAGE_MODEL,
        _usage(response.get("usage")),
        editable_title=request.editable_title,
    )


def _words(value: str) -> list[str]:
    return re.findall(r"\w+", unicodedata.normalize("NFC", value).casefold())


def decode_narration(events: Iterable[dict], request: NarrationRequest) -> GeneratedAsset:
    narration_events(request)
    chunks = bytearray()
    response_id = None
    audio_done = False
    transcript = None
    completed = None
    content_identity = None
    for count, event in enumerate(events):
        if count >= 10000:
            raise ValueError("excessive narration events")
        kind = event.get("type")
        if kind == "error":
            raise ValueError("narration provider error")
        if kind == "response.created":
            if response_id is not None:
                raise ValueError("duplicate narration response")
            response_id = event.get("response", {}).get("id")
            if not isinstance(response_id, str) or not response_id:
                raise ValueError("missing narration response identity")
        elif kind and kind.startswith("response."):
            identity = (
                event.get("response", {}).get("id")
                if kind == "response.done"
                else event.get("response_id")
            )
            if response_id is None or identity != response_id:
                raise ValueError("mismatched narration response identity")
            if kind in {
                "response.output_audio.delta",
                "response.output_audio.done",
                "response.output_audio_transcript.done",
            }:
                item_id = event.get("item_id")
                output_index = event.get("output_index")
                content_index = event.get("content_index")
                if (
                    not isinstance(item_id, str)
                    or not item_id
                    or type(output_index) is not int
                    or output_index < 0
                    or type(content_index) is not int
                    or content_index < 0
                ):
                    raise ValueError("missing narration content identity")
                identity = (item_id, output_index, content_index)
                if content_identity is not None and identity != content_identity:
                    raise ValueError("mismatched narration content identity")
                content_identity = identity
            if kind == "response.output_audio.delta":
                if audio_done:
                    raise ValueError("audio received after completion")
                chunks.extend(_bytes(event.get("delta"), MAX_AUDIO_BYTES - len(chunks)))
            elif kind == "response.output_audio.done":
                if audio_done:
                    raise ValueError("duplicate audio completion")
                audio_done = True
            elif kind == "response.output_audio_transcript.done":
                if transcript is not None:
                    raise ValueError("duplicate narration transcript")
                transcript = event.get("transcript")
            elif kind == "response.done":
                completed = event["response"]
                break
    if (
        not completed
        or completed.get("status") != "completed"
        or not audio_done
        or not chunks
        or len(chunks) % 2
    ):
        raise ValueError("incomplete narration audio")
    if not isinstance(transcript, str) or _words(transcript) != _words(request.approved_script):
        raise ValueError("narration differs from approved script")
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(24000)
        audio.writeframes(chunks)
    return GeneratedAsset(
        output.getvalue(), "audio/wav", NARRATION_MODEL, _usage(completed.get("usage")), response_id
    )
