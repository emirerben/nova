"""OpenAI binary transports with one submission, explicit organization and finite waits."""

from __future__ import annotations

import io
import json
import time
from decimal import Decimal

from PIL import Image, ImageOps

from app.services.generated_asset_contracts import (
    MAX_IMAGE_BYTES,
    NARRATION_MODEL,
    ImageRequest,
    NarrationRequest,
    decode_image,
    decode_narration,
    image_payload,
    narration_events,
)

GENERATED_PRICE_VERSION = "openai-generated-2026-10-29.v1"


class ProviderRejected(Exception):
    def __init__(self, *, retryable: bool):
        super().__init__("Provider rejected the generation request")
        self.retryable = retryable


def _provider_error(exc):
    status = getattr(exc, "status_code", None)
    if isinstance(status, int) and 400 <= status < 500 and status != 408:
        raise ProviderRejected(retryable=status == 429) from None
    raise exc


def validate_reference(content: bytes) -> tuple[str, bytes, str]:
    if not isinstance(content, bytes) or not 0 < len(content) <= MAX_IMAGE_BYTES:
        raise ValueError("invalid image edit reference")
    try:
        with Image.open(io.BytesIO(content)) as image:
            if (
                image.format not in {"PNG", "JPEG", "WEBP"}
                or image.width * image.height > 40_000_000
            ):
                raise ValueError("unsupported image edit reference")
            image.load()
            output = io.BytesIO()
            normalized = ImageOps.exif_transpose(image).convert("RGBA")
            normalized.thumbnail((1536, 1536), Image.Resampling.LANCZOS)
            normalized.save(output, format="PNG")
    except (OSError, SyntaxError) as exc:
        raise ValueError("invalid image edit reference") from exc
    return "reference.png", output.getvalue(), "image/png"


class OpenAIGeneratedTransport:
    def __init__(self, *, client=None, websocket_connect=None, api_key=None, organization=None):
        self.client = client
        self.websocket_connect = websocket_connect
        self.api_key = api_key
        self.organization = organization

    def _credentials(self):
        from app.config import settings

        key = self.api_key if self.api_key is not None else settings.openai_api_key
        organization = self.organization or settings.generated_media_billing_organization
        if not key or not organization:
            raise ValueError("verified provider credentials and organization required")
        return key, organization

    def _client(self):
        if self.client is None:
            from openai import OpenAI

            key, organization = self._credentials()
            self.client = OpenAI(api_key=key, organization=organization, max_retries=0)
        return self.client.with_options(max_retries=0)

    def generate(self, request: ImageRequest):
        payload = image_payload(request)
        try:
            response = self._client().images.generate(**payload, timeout=90)
        except Exception as exc:
            _provider_error(exc)
        return decode_image(response.model_dump(), request)

    def edit(self, request: ImageRequest, reference: bytes):
        payload = image_payload(request)
        upload = validate_reference(reference)
        try:
            response = self._client().images.edit(**payload, image=upload, timeout=90)
        except Exception as exc:
            _provider_error(exc)
        return decode_image(response.model_dump(), request)

    def narrate(self, request: NarrationRequest):
        from websockets.sync.client import connect

        key, organization = self._credentials()
        events = narration_events(request)
        deadline = time.monotonic() + 90
        with (self.websocket_connect or connect)(
            f"wss://api.openai.com/v1/realtime?model={NARRATION_MODEL}",
            additional_headers={
                "Authorization": f"Bearer {key}",
                "OpenAI-Organization": organization,
            },
            open_timeout=10,
            close_timeout=5,
            max_size=4 * 1024 * 1024,
        ) as socket:

            def receive():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("narration deadline exceeded")
                return json.loads(socket.recv(timeout=remaining))

            socket.send(json.dumps(events[0]))
            for _ in range(16):
                event = receive()
                if event.get("type") == "error":
                    raise ValueError("narration session rejected")
                if event.get("type") == "session.updated":
                    session = event.get("session", {})
                    audio = session.get("audio", {}).get("output", {})
                    if (
                        session.get("model") != NARRATION_MODEL
                        or audio.get("format") != {"type": "audio/pcm", "rate": 24000}
                        or audio.get("voice") != request.voice
                    ):
                        raise ValueError("narration session configuration mismatch")
                    break
            else:
                raise ValueError("narration session not acknowledged")
            socket.send(json.dumps(events[1]))

            def response_events():
                while True:
                    event = receive()
                    yield event
                    if event.get("type") == "response.done":
                        return

            return decode_narration(response_events(), request)


def generated_usage_cost(usage: dict, capability: str) -> float:
    def count(value):
        if type(value) is not int or value < 0:
            raise ValueError("missing generated token usage")
        return Decimal(value)

    incoming = count(usage.get("input_tokens"))
    outgoing = count(usage.get("output_tokens"))
    if count(usage.get("total_tokens")) != incoming + outgoing:
        raise ValueError("inconsistent generated token usage")
    if capability in {"image", "image_edit"}:
        details = usage.get("input_tokens_details") or {}
        text = count(details.get("text_tokens"))
        image = count(details.get("image_tokens"))
        if text + image != incoming:
            raise ValueError("missing image modality usage")
        cost = text * 5 + image * 8 + outgoing * 30
    elif capability == "narration":
        details = usage.get("output_token_details") or {}
        text = count(details.get("text_tokens"))
        audio = count(details.get("audio_tokens"))
        if text + audio != outgoing:
            raise ValueError("missing narration modality usage")
        cost = incoming * Decimal(".6") + text * Decimal("2.4") + audio * 20
    else:
        raise ValueError("unsupported generated capability")
    return float(cost / 1_000_000)
