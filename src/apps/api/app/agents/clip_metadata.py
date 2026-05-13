"""nova.video.clip_metadata — hook score + best moments + transcript per video segment.

Replaces `app.pipeline.agents.gemini_analyzer.analyze_clip`. The legacy function is
kept as a thin shim that builds a `ClipMetadataInput` and calls this agent —
existing callers continue to work unchanged while the LLM path migrates.

Input/output schemas mirror the existing `ClipMeta` dataclass exactly so the shim
is a 1:1 translation.

Domain-specific post-filter (`_filter_moments_by_action`) is preserved verbatim
from the legacy code — football-mode keyword filter that drops non-action moments
when `filter_hint` mentions ball/football/soccer.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import (
    Agent,
    AgentSpec,
    SchemaError,
)
from app.pipeline.prompt_loader import load_prompt

# ── Schemas ───────────────────────────────────────────────────────────────────


class TimeRange(BaseModel):
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., ge=0)


class Moment(BaseModel):
    start_s: float = Field(..., ge=0)
    end_s: float = Field(..., ge=0)
    energy: float = Field(default=5.0)
    description: str = ""


class ClipMetadataInput(BaseModel):
    file_uri: str
    file_mime: str = "video/mp4"
    segment: TimeRange | None = None
    filter_hint: str = ""


class ClipMetadataOutput(BaseModel):
    clip_id: str = ""
    transcript: str = ""
    hook_text: str
    hook_score: float = Field(..., ge=0, le=10)
    best_moments: list[Moment] = Field(default_factory=list)
    detected_subject: str = ""


# ── Domain-specific post-filter (lifted verbatim from gemini_analyzer.py) ────

_BALL_WHITELIST = (
    "ball",
    "top",
    "shot",
    "şut",
    "vuruş",
    "pass",
    "pas",
    "asist",
    "goal",
    "gol",
    "dribble",
    "çalım",
    "save",
    "kurtarış",
    "kurtardı",
    "tackle",
    "tekleme",
    "header",
    "kafa",
    "cross",
    "orta",
    "ortaladı",
    "strike",
    "volley",
    "voleyle",
    "korner",
    "corner",
    "freekick",
    "frikik",
    "penalty",
    "penaltı",
    "intercept",
    "kesme",
    "control",
    "kontrol",
    "attack",
    "atak",
    "hücum",
    "finish",
    "bitiriş",
)
_BALL_BLACKLIST = (
    "celebration",
    "kutlama",
    "celebrating",
    "empty",
    "boş",
    "geniş plan",
    "wide shot",
    "walking",
    "yürüyor",
    "walks",
    "training",
    "antrenman",
    "warmup",
    "ısınma",
    "bench",
    "yedek",
    "stoppage",
    "oyun durması",
    "stopped",
    "idle",
    "hareketsiz",
    "tribün",
    "stand",
    "audience",
    "crowd",
    "referee",
    "hakem",
    "linesman",
    "yan hakem",
    "interview",
    "röportaj",
    "halftime",
    "devre arası",
    "no ball",
    "topsuz",
)


def _filter_moments_by_action(moments: list[dict[str, Any]], hint: str) -> list[dict[str, Any]]:
    """Football-only post-filter. Pass-through for other hints / no hint."""
    hint_lower = hint.lower()
    if not any(k in hint_lower for k in ("ball", "top", "futbol", "football", "soccer")):
        return moments

    whitelist_hits: list[dict[str, Any]] = []
    neutral: list[dict[str, Any]] = []
    for m in moments:
        if not isinstance(m, dict):
            continue
        desc = (m.get("description") or "").lower()
        if not desc:
            continue
        if any(b in desc for b in _BALL_BLACKLIST):
            continue
        if any(w in desc for w in _BALL_WHITELIST):
            whitelist_hits.append(m)
        else:
            neutral.append(m)

    if len(whitelist_hits) >= 2:
        kept = whitelist_hits
    else:
        kept = whitelist_hits + neutral

    if not kept and moments:
        kept = [max(moments, key=lambda m: m.get("energy", 0) if isinstance(m, dict) else 0)]
    return kept


# ── Agent ─────────────────────────────────────────────────────────────────────


class ClipMetadataAgent(Agent[ClipMetadataInput, ClipMetadataOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.video.clip_metadata",
        prompt_id="analyze_clip",
        prompt_version="2026-05-14",
        model="gemini-2.5-flash",
        # Gemini pricing as of 2026 — input ~$0.075/M, output ~$0.30/M (2.5 Flash).
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Skip the clarification retry: prod logs showed `attempts=2
        # latency_ms=233209` (3m 53s) on schema/refusal retries that almost
        # always failed the same way. Caller (template_orchestrate) has a
        # Whisper fallback that's faster and good enough for the
        # transcript+best_moments fields. Saves ~100s on the ~10-15% of
        # clips where Gemini's first attempt returns malformed JSON.
        enable_clarification_retries=False,
    )
    Input = ClipMetadataInput
    Output = ClipMetadataOutput

    def media_uri(self, input: ClipMetadataInput) -> str | None:  # noqa: A002
        return input.file_uri

    def media_mime(self, input: ClipMetadataInput) -> str:  # noqa: A002
        return input.file_mime or "video/mp4"

    def required_fields(self) -> list[str]:
        # `hook_text` intentionally NOT required — silent / ambient clips (music
        # montage, B-roll, no dialogue) legitimately return "". Treating empty
        # `hook_text` as a refusal forced a Whisper fallback (~8s extra latency
        # + wasted Gemini call) for clips that were actually usable downstream.
        # As long as `best_moments` is populated, the clip carries enough signal
        # for the matcher.
        return ["hook_score", "best_moments"]

    # ── Prompt ────────────────────────────────────────────────────

    def render_prompt(self, input: ClipMetadataInput) -> str:  # noqa: A002
        if input.segment is not None:
            segment_instruction = (
                f"Analyze the video segment from {input.segment.start_s:.1f}s "
                f"to {input.segment.end_s:.1f}s."
            )
        else:
            segment_instruction = "Analyze this video clip."

        if input.filter_hint.strip():
            hint_lower = input.filter_hint.lower()
            is_football = any(
                k in hint_lower for k in ("ball", "top", "futbol", "football", "soccer")
            )
            if is_football:
                domain_block = (
                    "STRICT FILTER: " + input.filter_hint.strip() + "\n"
                    "ONLY include moments where the ball is visible AND a player "
                    "actively interacts with it (shot, pass, dribble, header, save, "
                    "tackle, control, cross, freekick, penalty). REJECT: empty pitch, "
                    "wide stadium shots, celebrations, walking, idle stance, ball out "
                    "of play, referee/crowd shots, training drills, warmup. Each "
                    "selected moment's description MUST name the specific ball "
                    "action that occurs — vague labels are forbidden.\n\n"
                )
            else:
                domain_block = (
                    f"STRICT FILTER: {input.filter_hint.strip()}\n"
                    "Reject any moment that violates this filter. Only include "
                    "moments that fully satisfy it.\n\n"
                )
            segment_instruction = domain_block + segment_instruction

        return load_prompt("analyze_clip", segment_instruction=segment_instruction)

    # ── Parse ─────────────────────────────────────────────────────

    def parse(self, raw_text: str, input: ClipMetadataInput) -> ClipMetadataOutput:  # noqa: A002
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"clip_metadata: invalid JSON — {exc}") from exc

        if not isinstance(data, dict):
            raise SchemaError("clip_metadata: response is not a JSON object")

        hook_score = data.get("hook_score", 5.0)
        try:
            hook_score = float(hook_score)
        except (TypeError, ValueError) as exc:
            raise SchemaError(f"clip_metadata: hook_score not numeric — {hook_score!r}") from exc
        hook_score = max(0.0, min(10.0, hook_score))

        moments_raw = data.get("best_moments", []) or []
        if not isinstance(moments_raw, list):
            moments_raw = []
        # Apply domain-specific post-filter when filter_hint indicates football.
        if input.filter_hint.strip():
            moments_raw = _filter_moments_by_action(moments_raw, input.filter_hint)

        # Coerce each moment into the Moment schema; drop malformed entries silently
        # (matches legacy behavior — better to keep partial data than fail the slot).
        moments: list[Moment] = []
        for m in moments_raw:
            if not isinstance(m, dict):
                continue
            try:
                moments.append(
                    Moment(
                        start_s=float(m.get("start_s", 0.0) or 0.0),
                        end_s=float(m.get("end_s", 0.0) or 0.0),
                        energy=float(m.get("energy", 5.0) or 5.0),
                        description=str(m.get("description", "") or ""),
                    )
                )
            except (ValidationError, TypeError, ValueError):
                continue

        # Surface "Gemini sent items but every one failed coercion" as a schema
        # error so the runtime retries with clarification. Without this, an empty
        # `moments` list silently passes back to the orchestrator (refusal check
        # already approved the raw JSON because `best_moments` was non-empty),
        # and downstream matching has nothing to work with.
        if moments_raw and not moments:
            raise SchemaError(
                f"clip_metadata: received {len(moments_raw)} moment(s) but "
                "every entry failed validation (likely wrong field names or "
                "non-numeric start_s/end_s)"
            )

        try:
            return ClipMetadataOutput(
                transcript=str(data.get("transcript", "") or ""),
                hook_text=str(data.get("hook_text", "") or ""),
                hook_score=hook_score,
                best_moments=moments,
                detected_subject=str(data.get("detected_subject", "") or ""),
            )
        except ValidationError as exc:
            raise SchemaError(f"clip_metadata: output validation — {exc}") from exc
