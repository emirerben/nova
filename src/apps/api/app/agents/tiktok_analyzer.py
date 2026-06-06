"""nova.plan.tiktok_analyzer — distill a creator's own TikTok performance into
structured insights for persona, content-plan, and hook personalization.

Off-Job agent (no media). Input is an enriched TikTok profile (per-video views,
likes, comments, captions, engagement_rate, view_index). Output is a bounded
`TikTokAnalysis` with a pre-rendered `summary_for_prompts` that threads verbatim
into generate_persona.txt, generate_content_plan.txt, and write_intro_text.txt.

Trust boundary: captions and hashtags from the TikTok profile are UNTRUSTED
third-party data. Every caption is sanitized with `_sanitize_text` before reaching
the prompt, and every output text field is re-sanitized after parsing (the summary
becomes prompt input to three downstream agents — defense-in-depth applies twice).

This agent is invoked from the best-effort `analyze_tiktok_profile` Celery task.
Any parse error → the task catches and returns without marking the persona failed.
"""

from __future__ import annotations

import json
import re
from typing import Any, ClassVar

import structlog
from pydantic import ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents._schemas.tiktok_analysis import (
    _MAX_SUMMARY_CHARS,
    TIKTOK_ANALYZER_PROMPT_VERSION,
    HookPattern,
    TikTokAnalysis,
    TikTokAnalyzerInput,
    TikTokAnalyzerOutput,
    WinningTheme,
)
from app.agents.music_matcher import _sanitize_text
from app.pipeline.prompt_loader import load_prompt

# Regex for stripping unsafe tokens from LLM output before it becomes prompt input
# to downstream agents. Mirrors intro_writer._strip_unsafe_tokens (defined locally
# to avoid a cross-agent dependency).
_URL_RE = re.compile(r"\b(?:https?://|www\.)\S+", re.IGNORECASE)
_BARE_DOMAIN_RE = re.compile(r"\b[\w-]+\.(?:com|net|org|io|co|gg|xyz|app|link)\b", re.IGNORECASE)
_HANDLE_RE = re.compile(r"[@#]\w+")


def _strip_output_unsafe(s: str) -> str:
    """Strip URLs, @handles, #hashtags, and collapse whitespace from an LLM output field.

    Applied on top of _sanitize_text as defense-in-depth: the summary becomes
    prompt input to three downstream agents, so URL/handle injection must be
    blocked at the source.
    """
    s = _URL_RE.sub("", s)
    s = _BARE_DOMAIN_RE.sub("", s)
    s = _HANDLE_RE.sub("", s)
    return re.sub(r"\s+", " ", s).strip()


def _sanitize_output(s: str) -> str:
    """Full output sanitization: control chars + role markers + URL/handle stripping."""
    return _strip_output_unsafe(_sanitize_text(s))


log = structlog.get_logger()

# Truncate captions before they reach the prompt — a 300-char cap (matching the
# flat fetch_profile cap) keeps the DATA block from ballooning on long captions.
_MAX_CAPTION_LEN = 300


def _safe_float(val: Any) -> str:
    """Format a numeric or None value for the prompt."""
    if isinstance(val, float):
        return f"{val:.2f}"
    if isinstance(val, int):
        return str(val)
    return "n/a"


class TikTokAnalyzerAgent(Agent[TikTokAnalyzerInput, TikTokAnalyzerOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.plan.tiktok_analyzer",
        prompt_id="analyze_tiktok_profile",
        prompt_version=TIKTOK_ANALYZER_PROMPT_VERSION,
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = TikTokAnalyzerInput
    Output = TikTokAnalyzerOutput

    def required_fields(self) -> list[str]:
        return ["analysis"]

    def render_prompt(self, input: TikTokAnalyzerInput) -> str:  # noqa: A002
        # Build the per-video block as DATA — sanitize every caption/hashtag.
        lines: list[str] = []
        for i, v in enumerate(input.videos, start=1):
            caption = _sanitize_text(str(v.get("caption") or ""))[:_MAX_CAPTION_LEN]
            hashtags = " ".join(_sanitize_text(h) for h in (v.get("hashtags") or [])[:10] if h)
            views = _safe_float(v.get("view_count"))
            er = _safe_float(v.get("engagement_rate"))
            vi = _safe_float(v.get("view_index"))
            upload = str(v.get("upload_date") or "")
            lines.append(
                f"[{i}] views={views} eng_rate={er} view_index={vi} "
                f"date={upload or 'n/a'} | {caption}" + (f" | tags: {hashtags}" if hashtags else "")
            )

        videos_block = "\n".join(lines) if lines else "(no video data)"

        return load_prompt(
            "analyze_tiktok_profile",
            handle=_sanitize_text(input.handle or "unknown"),
            follower_count=_safe_float(input.follower_count),
            median_views=_safe_float(input.median_views),
            videos_block=videos_block,
        )

    def parse(
        self,
        raw_text: str,
        input: TikTokAnalyzerInput,  # noqa: A002, ARG002
    ) -> TikTokAnalyzerOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"tiktok_analyzer: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("tiktok_analyzer: response is not a JSON object")

        analysis_raw = data.get("analysis")
        if not isinstance(analysis_raw, dict):
            raise SchemaError("tiktok_analyzer: missing/invalid 'analysis' object")

        # Pre-clamp summary_for_prompts BEFORE Pydantic validation so an overlong
        # LLM response doesn't hard-fail (max_length is enforced on the schema too;
        # clamping here ensures we never produce a RefusalError on length alone).
        if isinstance(analysis_raw.get("summary_for_prompts"), str):
            analysis_raw = dict(analysis_raw)
            analysis_raw["summary_for_prompts"] = analysis_raw["summary_for_prompts"][
                :_MAX_SUMMARY_CHARS
            ]

        try:
            analysis = TikTokAnalysis(**analysis_raw)
        except ValidationError as exc:
            raise RefusalError(f"tiktok_analyzer: analysis validation — {exc}") from exc

        # Re-sanitize every output text field. summary_for_prompts is injected
        # verbatim into three downstream agent prompts — strip any residual URL /
        # @handle / injection vector that slipped through (defense-in-depth).
        # _sanitize_output applies both _sanitize_text AND URL/handle stripping.
        cleaned = TikTokAnalysis(
            voice_description=_sanitize_output(analysis.voice_description),
            hook_patterns_that_work=[
                HookPattern(
                    pattern=_sanitize_output(h.pattern), evidence=_sanitize_output(h.evidence)
                )
                for h in analysis.hook_patterns_that_work
                if _sanitize_output(h.pattern)
            ],
            winning_themes=[
                WinningTheme(theme=_sanitize_output(t.theme), view_index=t.view_index)
                for t in analysis.winning_themes
                if _sanitize_output(t.theme)
            ],
            posting_cadence=_sanitize_output(analysis.posting_cadence),
            audience_signal=_sanitize_output(analysis.audience_signal),
            # Clamp again after sanitization (stripping can slightly shrink the length).
            summary_for_prompts=_sanitize_output(analysis.summary_for_prompts)[:_MAX_SUMMARY_CHARS],
        )

        try:
            return TikTokAnalyzerOutput(analysis=cleaned)
        except ValidationError as exc:
            raise SchemaError(f"tiktok_analyzer: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            '\n\nIMPORTANT: Return ONLY a JSON object {"analysis": {...}} with the '
            "exact keys described. summary_for_prompts must be ≤1200 characters "
            "and plain text (no @handles, no #hashtags, no URLs). "
            "hook_patterns_that_work and winning_themes must be arrays (can be empty)."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
