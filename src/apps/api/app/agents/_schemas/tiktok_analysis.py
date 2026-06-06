"""Schemas for nova.plan.tiktok_analyzer.

The analyzer agent consumes enriched per-video TikTok data (captions, views,
likes, engagement_rate, view_index) and distills it into a bounded text block
that threads into persona_generator, content_plan_generator, and intro_writer.

Trust boundary: the per-video data is UNTRUSTED (third-party API via yt-dlp).
Every caption/hashtag is sanitized before reaching the prompt and every output
text field is re-sanitized after parsing — the summary becomes prompt input to
three downstream agents so defense-in-depth applies twice.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

# Bump when prompts/analyze_tiktok_profile.txt changes.
# 2026-06-06 — initial: enriched-fetch analysis for persona/plan/hook injection.
TIKTOK_ANALYZER_PROMPT_VERSION = "2026-06-06"

# Bounded so the summary can't bloat three downstream prompts.
_MAX_SUMMARY_CHARS = 1200


class HookPattern(BaseModel):
    """A hook pattern that outperforms the creator's own baseline."""

    pattern: str = Field(min_length=1)
    # Evidence anchors the claim in the data (e.g. "indexed 2.8x account median").
    evidence: str = ""


class WinningTheme(BaseModel):
    """A content theme that over-indexed on this creator's account."""

    theme: str = Field(min_length=1)
    # view_index = views / account-median-views for the best video in this theme.
    view_index: float | None = Field(default=None, ge=0)


class TikTokAnalysis(BaseModel):
    """Distilled creator intelligence from their own TikTok performance data."""

    voice_description: str = ""
    # Opener patterns from high view_index videos, each with the evidence.
    hook_patterns_that_work: list[HookPattern] = Field(default_factory=list, max_length=6)
    # Recurring subjects that over-indexed (view_index > 1.5 own median).
    winning_themes: list[WinningTheme] = Field(default_factory=list, max_length=6)
    posting_cadence: str = ""
    # Who engages and why, derived from engagement_rate distribution.
    audience_signal: str = ""
    # Pre-rendered, bounded block that threads into all three generation prompts.
    # Empty when no useful signal was found. NEVER "(none)".
    summary_for_prompts: str = Field(default="", max_length=_MAX_SUMMARY_CHARS)


class TikTokAnalyzerInput(BaseModel):
    """Input to the analyzer — populated from fetch_profile_enriched output."""

    handle: str = ""
    follower_count: int | None = None
    median_views: float | None = None
    # Per-video records (TikTokVideoRecord dicts). Treated as UNTRUSTED DATA;
    # captions/hashtags are sanitized in render_prompt before reaching the model.
    videos: list[dict] = Field(default_factory=list)


class TikTokAnalyzerOutput(BaseModel):
    analysis: TikTokAnalysis

    def to_dict(self) -> dict:
        return self.model_dump()
