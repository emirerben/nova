"""nova.ingest.image_metadata — what does this still image show? (plans/005 PR1a)

Analyzes ONE pool image (screenshot / photo) so the overlay-placement matcher can
reason about it in text. Sibling of ClipMetadataAgent (which covers video); stills
need far less: subject, one-line description, any legible on-screen text.

The output is MATCHING METADATA ONLY — it never becomes on-screen overlay text
(same trust boundary as clip metadata; the Gemini-text-leak rule applies).
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import BaseModel, Field, field_validator

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

_MAX_TEXT = 400


class ImageMetadataInput(BaseModel):
    file_uri: str
    file_mime: str = "image/png"


class ImageMetadataOutput(BaseModel):
    subject: str = ""
    description: str = ""
    on_screen_text: str = ""
    brands: list[str] = Field(default_factory=list)
    # "screenshot" | "photo" | "diagram" | "document" | "other"
    kind_hint: str = "other"

    @field_validator("subject", "description", "on_screen_text")
    @classmethod
    def _trim(cls, v: str) -> str:
        return (v or "").strip()[:_MAX_TEXT]

    @field_validator("brands", mode="before")
    @classmethod
    def _brands(cls, v: object) -> list[str]:
        if not isinstance(v, list):
            return []
        return [(s or "").strip()[:80] for s in v if (s or "").strip()][:10]

    @field_validator("kind_hint")
    @classmethod
    def _kind(cls, v: str) -> str:
        allowed = {"screenshot", "photo", "diagram", "document", "other"}
        v = (v or "").strip().lower()
        return v if v in allowed else "other"


class ImageMetadataAgent(Agent[ImageMetadataInput, ImageMetadataOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.ingest.image_metadata",
        prompt_id="image_metadata",
        prompt_version="1.1.0",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        thinking_budget=128,
    )
    Input = ImageMetadataInput
    Output = ImageMetadataOutput

    def media_uri(self, input: ImageMetadataInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: ImageMetadataInput) -> str:  # noqa: A002
        return input.file_mime or "image/png"

    def required_fields(self) -> list[str]:
        return ["subject", "description"]

    def render_prompt(self, input: ImageMetadataInput) -> str:  # noqa: A002
        return load_prompt("image_metadata")

    def parse(
        self,
        raw_text: str,
        input: ImageMetadataInput,  # noqa: A002
    ) -> ImageMetadataOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"image_metadata: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("image_metadata: response is not a JSON object")
        return ImageMetadataOutput.model_validate(data)
