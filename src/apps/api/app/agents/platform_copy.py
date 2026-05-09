"""nova.compose.platform_copy — TikTok/IG/YT social copy generator.

Replaces `app.pipeline.agents.copy_writer.generate_copy`. The Pydantic copy
models (`TikTokCopy`, `InstagramCopy`, `YouTubeCopy`, `PlatformCopy`) and field
truncation validators are imported as-is from the legacy module — they are
already production-grade Pydantic schemas and don't need re-writing.

Failure handling: the legacy code falls back to a hardcoded template on Gemini
failure. We preserve that semantics in the legacy shim — the agent itself raises
TerminalError, and the shim catches and falls back. Keeps the agent contract
clean (output is always validated copy) while preserving observable behavior.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, ValidationError

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.pipeline.agents.copy_writer import (
    InstagramCopy,
    PlatformCopy,
    TikTokCopy,
    YouTubeCopy,
)


class PlatformCopyInput(BaseModel):
    hook_text: str
    transcript_excerpt: str = ""
    has_transcript: bool = True
    template_tone: str = ""


class PlatformCopyOutput(BaseModel):
    # Field is `value` rather than `copy` because Pydantic BaseModel.copy() is
    # an inherited method — naming the field `copy` shadows it and warns.
    value: PlatformCopy

    model_config = {"arbitrary_types_allowed": True}


class PlatformCopyAgent(Agent[PlatformCopyInput, PlatformCopyOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.compose.platform_copy",
        prompt_id="_inline",  # prompt is built from input, no template file
        prompt_version="2026-05-09",
        model="gemini-2.5-flash",
        # Legacy used 2 attempts; keep the runtime's 5-attempt default but a
        # tighter timeout — copy is short and shouldn't take long.
        max_attempts=5,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = PlatformCopyInput
    Output = PlatformCopyOutput

    def render_prompt(self, input: PlatformCopyInput) -> str:  # noqa: A002
        transcript_note = (
            f'Transcript excerpt: "{input.transcript_excerpt[:500]}"'
            if input.has_transcript
            else "No transcript available — infer content type from the hook text."
        )
        tone_note = (
            f'\nTone guidance: match this style — "{input.template_tone}".'
            if input.template_tone
            else ""
        )
        return (
            "Generate platform-native social media copy for a short-form video clip.\n\n"
            f'Hook text (first line of clip): "{input.hook_text}"\n'
            f"{transcript_note}"
            f"{tone_note}\n\n"
            "Return a JSON object with this structure:\n"
            '{"tiktok": {"hook": str, "caption": str, "hashtags": [str, ...]},\n'
            ' "instagram": {"hook": str, "caption": str, "hashtags": [str, ...]},\n'
            ' "youtube": {"title": str, "description": str, "tags": [str, ...]}}\n\n'
            "Requirements:\n"
            "- TikTok: punchy hook (≤150 chars), caption (≤300 chars), 5 hashtags\n"
            "- Instagram Reels: engaging hook (≤150 chars), caption (≤300 chars), 10 hashtags\n"
            "- YouTube Shorts: SEO title (≤100 chars, include #shorts), "
            "description (≤300 chars), 15 tags\n"
            "All copy should feel native to each platform. Be direct and energetic.\n"
            "Return ONLY valid JSON, no markdown."
        )

    def parse(
        self, raw_text: str, input: PlatformCopyInput
    ) -> PlatformCopyOutput:  # noqa: A002, ARG002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"platform_copy: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("platform_copy: response is not a JSON object")

        try:
            copy = PlatformCopy(
                tiktok=TikTokCopy(**data["tiktok"]),
                instagram=InstagramCopy(**data["instagram"]),
                youtube=YouTubeCopy(**data["youtube"]),
            )
        except (KeyError, ValidationError, TypeError) as exc:
            raise SchemaError(f"platform_copy: schema — {exc}") from exc

        return PlatformCopyOutput(value=copy)
