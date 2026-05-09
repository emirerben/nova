"""Gemini-powered copy generation → PlatformCopy per clip.

Character limits enforced + truncated at last sentence.
Fallback: template strings on any Gemini failure.
"""


import structlog
from pydantic import BaseModel, field_validator

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
    platforms: list[str],  # noqa: ARG001 — kept for backwards-compatible signature
    has_transcript: bool = True,
    template_tone: str = "",
) -> tuple[PlatformCopy, str]:
    """Generate platform-native copy for a clip — returns (PlatformCopy, status).

    SHIM (v0.2): delegates to `app.agents.platform_copy.PlatformCopyAgent`.
    On agent failure, falls back to a hardcoded template via `_template_copy()`,
    preserving the legacy `'generated_fallback'` status code.
    """
    from app.agents._model_client import default_client  # noqa: PLC0415
    from app.agents._runtime import TerminalError  # noqa: PLC0415
    from app.agents.platform_copy import (  # noqa: PLC0415
        PlatformCopyAgent,
        PlatformCopyInput,
    )

    inp = PlatformCopyInput(
        hook_text=hook_text,
        transcript_excerpt=transcript_excerpt,
        has_transcript=has_transcript,
        template_tone=template_tone,
    )
    try:
        out = PlatformCopyAgent(default_client()).run(inp)
        log.info("copy_generated", hook=hook_text[:50])
        return out.value, "generated"
    except TerminalError as exc:
        log.warning("copy_writer_api_error", error=str(exc))
        log.error("copy_writer_fallback_to_template")
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
