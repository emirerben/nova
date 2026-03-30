"""Gemini-powered copy generation → PlatformCopy per clip.

Character limits enforced + truncated at last sentence.
Fallback: template strings on any Gemini failure.
"""

import json

import structlog
from pydantic import BaseModel, field_validator

from app.config import settings
from app.pipeline.agents.gemini_analyzer import _get_client

log = structlog.get_logger()

# Platform character/tag limits
INSTAGRAM_CAPTION_MAX = 2200
INSTAGRAM_HASHTAG_COUNT = 10
TIKTOK_CAPTION_MAX = 2200
TIKTOK_HASHTAG_COUNT = 5
YOUTUBE_TITLE_MAX = 100
YOUTUBE_DESCRIPTION_MAX = 5000
YOUTUBE_TAG_COUNT = 15


class TikTokCopy(BaseModel):
    hook: str
    caption: str
    hashtags: list[str]

    @field_validator("caption")
    @classmethod
    def truncate_caption(cls, v: str) -> str:
        return _truncate(v, TIKTOK_CAPTION_MAX)

    @field_validator("hashtags")
    @classmethod
    def limit_hashtags(cls, v: list[str]) -> list[str]:
        return v[:TIKTOK_HASHTAG_COUNT]


class InstagramCopy(BaseModel):
    hook: str
    caption: str
    hashtags: list[str]

    @field_validator("caption")
    @classmethod
    def truncate_caption(cls, v: str) -> str:
        return _truncate(v, INSTAGRAM_CAPTION_MAX)

    @field_validator("hashtags")
    @classmethod
    def limit_hashtags(cls, v: list[str]) -> list[str]:
        return v[:INSTAGRAM_HASHTAG_COUNT]


class YouTubeCopy(BaseModel):
    title: str
    description: str
    tags: list[str]

    @field_validator("title")
    @classmethod
    def truncate_title(cls, v: str) -> str:
        return _truncate(v, YOUTUBE_TITLE_MAX)

    @field_validator("description")
    @classmethod
    def truncate_description(cls, v: str) -> str:
        return _truncate(v, YOUTUBE_DESCRIPTION_MAX)

    @field_validator("tags")
    @classmethod
    def limit_tags(cls, v: list[str]) -> list[str]:
        return v[:YOUTUBE_TAG_COUNT]


class PlatformCopy(BaseModel):
    tiktok: TikTokCopy
    instagram: InstagramCopy
    youtube: YouTubeCopy


def generate_copy(
    hook_text: str,
    transcript_excerpt: str,
    platforms: list[str],
    has_transcript: bool = True,
    template_tone: str = "",
) -> tuple[PlatformCopy, str]:
    """Generate platform-native copy for a clip using Gemini.

    template_tone: optional tone descriptor from a TemplateRecipe (e.g. 'casual',
                   'energetic') — used for template-mode jobs to match the reference.

    Returns (PlatformCopy, copy_status) where copy_status is one of:
      'generated' | 'generated_fallback'
    """
    client = _get_client()

    transcript_note = (
        f'Transcript excerpt: "{transcript_excerpt[:500]}"'
        if has_transcript
        else "No transcript available — infer content type from the hook text."
    )
    tone_note = (
        f'\nTone guidance: match this style — "{template_tone}".'
        if template_tone
        else ""
    )

    prompt = (
        "Generate platform-native social media copy for a short-form video clip.\n\n"
        f"Hook text (first line of clip): \"{hook_text}\"\n"
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

    from google.genai import types as genai_types  # type: ignore[import]

    for attempt in range(2):
        try:
            response = client.models.generate_content(
                model=settings.gemini_model,
                contents=[prompt],
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                ),
            )
            data = json.loads(response.text)
            result = PlatformCopy(
                tiktok=TikTokCopy(**data["tiktok"]),
                instagram=InstagramCopy(**data["instagram"]),
                youtube=YouTubeCopy(**data["youtube"]),
            )
            log.info("copy_generated", hook=hook_text[:50], attempt=attempt)
            return result, "generated"
        except Exception as exc:
            log.warning("copy_writer_api_error", error=str(exc), attempt=attempt)
            if attempt == 1:
                log.error("copy_writer_fallback_to_template")
                return _template_copy(hook_text), "generated_fallback"

    return _template_copy(hook_text), "generated_fallback"


def _template_copy(hook_text: str) -> PlatformCopy:
    """Minimal fallback copy when GPT-4o fails twice."""
    hook = hook_text[:150] if hook_text else "Watch this"
    return PlatformCopy(
        tiktok=TikTokCopy(
            hook=hook,
            caption="Auto-copy failed — edit before posting",
            hashtags=["viral", "fyp", "trending", "video", "content"],
        ),
        instagram=InstagramCopy(
            hook=hook,
            caption="Auto-copy failed — edit before posting",
            hashtags=["reels", "viral", "trending", "instagram", "content",
                      "creator", "video", "fyp", "explore", "share"],
        ),
        youtube=YouTubeCopy(
            title=f"{hook[:90]} #shorts",
            description="Auto-copy failed — edit before posting",
            tags=["shorts", "viral", "trending", "youtube", "video",
                  "content", "creator", "fyp", "subscribe", "like",
                  "comment", "share", "new", "today", "watch"],
        ),
    )


def _truncate(text: str, max_len: int) -> str:
    """Truncate at last sentence boundary that fits within max_len."""
    if len(text) <= max_len:
        return text
    truncated = text[:max_len]
    # Try to end at last sentence
    for sep in (".", "!", "?"):
        last = truncated.rfind(sep)
        if last > max_len // 2:
            return truncated[: last + 1] + "…"
    return truncated + "…"
