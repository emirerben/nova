"""nova.plan.slide_post_composer — AI ordering/cover/caption for slide posts.

Composes an ordered mixed-media post (images + videos) from an item's
already-analyzed pool assets. Deliberately does NO new media analysis — the
input is each `PlanItemAsset.analysis` the pool analyzer already computed
(plans/024). The agent may only reorder/select from the ids it is given; it
can never invent, drop, or duplicate one — `parse` enforces this as a hard
validation failure (SchemaError), not a best-effort repair, because a
dropped id would silently produce an incomplete post.
"""

from __future__ import annotations

import json
from typing import ClassVar

import structlog
from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt

log = structlog.get_logger()

COMPOSER_PROMPT_VERSION = "2026-09-09-kria"

_MAX_CAPTION_LEN = 2200  # matches SlidePostDraft.caption cap
_MAX_ALT_LEN = 500

_PLATFORM_LABELS = {
    "tiktok_photo": "TikTok photo post",
    "instagram_carousel": "Instagram carousel",
}


class SlidePostMediaItem(BaseModel):
    id: str
    kind: str = "image"
    # Short, pre-summarized description derived from PlanItemAsset.analysis —
    # never the raw analysis payload (keeps the prompt bounded and private
    # analysis fields out of the model context).
    description: str = ""


class SlidePostComposerInput(BaseModel):
    theme: str = ""
    idea: str = ""
    notes: str = ""
    persona_summary: str = ""
    platform_profile: str = "tiktok_photo"
    media: list[SlidePostMediaItem] = Field(default_factory=list, min_length=1)


class SlidePostComposerOutput(BaseModel):
    order: list[str] = Field(min_length=1)
    cover_id: str
    # No max_length here on purpose — an over-long raw caption is truncated
    # by `parse()` below, not rejected. A hard cap here would turn a merely
    # verbose model response into a RefusalError (burning a retry) for
    # something `parse()` can fix for free.
    caption: str = ""
    alt_text: dict[str, str] = Field(default_factory=dict)


def _format_media_block(media: list[SlidePostMediaItem]) -> str:
    lines = []
    for item in media:
        desc = _sanitize_text(item.description) or "(no description)"
        lines.append(f'  id="{item.id}" kind={item.kind}: {desc}')
    return "\n".join(lines) if lines else "  (no media)"


class SlidePostComposerAgent(Agent[SlidePostComposerInput, SlidePostComposerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.slide_post_composer",
        prompt_id="slide_post_composer",
        prompt_version=COMPOSER_PROMPT_VERSION,
        model="gemini-2.5-flash",
        max_attempts=3,
        backoff_s=(2.0, 6.0),
        timeout_s=20.0,
        thinking_budget=512,
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = SlidePostComposerInput
    Output = SlidePostComposerOutput
    response_json = True

    def required_fields(self) -> list[str]:
        return ["order", "cover_id"]

    def render_prompt(self, input: SlidePostComposerInput) -> str:  # noqa: A002
        return load_prompt(
            "slide_post_composer",
            theme=_sanitize_text(input.theme),
            idea=_sanitize_text(input.idea),
            notes=_sanitize_text(input.notes),
            persona_summary=_sanitize_text(input.persona_summary),
            platform_label=_PLATFORM_LABELS.get(input.platform_profile, "mixed-media post"),
            media_block=_format_media_block(input.media),
        )

    def parse(
        self,
        raw_text: str,
        input: SlidePostComposerInput,  # noqa: A002
    ) -> SlidePostComposerOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"slide_post_composer: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("slide_post_composer: response is not a JSON object")
        try:
            output = SlidePostComposerOutput(**data)
        except ValidationError as exc:
            raise RefusalError(f"slide_post_composer: validation — {exc}") from exc

        known_ids = {item.id for item in input.media}
        ordered_unique = list(dict.fromkeys(output.order))
        if set(ordered_unique) != known_ids or len(ordered_unique) != len(output.order):
            # A dropped/invented/duplicated id would silently produce an
            # incomplete or malformed post — this is a hard schema failure,
            # not a best-effort degrade, so the caller falls back to the
            # original pool order rather than trusting a partial one.
            raise SchemaError(
                "slide_post_composer: order does not exactly cover the given media ids"
            )
        cover_id = output.cover_id if output.cover_id in known_ids else ordered_unique[0]
        alt_text = {
            key: value.strip()[:_MAX_ALT_LEN]
            for key, value in output.alt_text.items()
            if key in known_ids and isinstance(value, str) and value.strip()
        }
        return SlidePostComposerOutput(
            order=ordered_unique,
            cover_id=cover_id,
            caption=output.caption.strip()[:_MAX_CAPTION_LEN],
            alt_text=alt_text,
        )

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: return ONLY valid JSON with keys: order (array of every "
            "given id, each exactly once), cover_id (one id from order), caption "
            "(string), alt_text (object mapping some ids to short strings). No "
            "markdown, no prose outside the JSON."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
