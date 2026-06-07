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
import math
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

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


# Clip-understanding labels for the format-aware edit engine (Lane A / D1).
# content_type feeds archetype dispatch (talking_head spine vs B-roll cutaway);
# audio_type guides which variants make sense (e.g. a dialogue clip shouldn't get
# a lyrics overlay). Safe defaults ('broll' / 'ambient') never falsely promote a
# clip to the talking-head spine when the model omits or drifts the label.
ClipContentType = Literal["talking_head", "broll", "action", "ambience"]
ClipAudioType = Literal["dialogue", "voiceover", "ambient", "music", "mixed"]
_CONTENT_TYPES = ("talking_head", "broll", "action", "ambience")
_AUDIO_TYPES = ("dialogue", "voiceover", "ambient", "music", "mixed")


class ClipMetadataOutput(BaseModel):
    clip_id: str = ""
    transcript: str = ""
    hook_text: str
    hook_score: float = Field(..., ge=0, le=10)
    best_moments: list[Moment] = Field(default_factory=list)
    detected_subject: str = ""
    # Composition signal for agent-decided overlay sizing (see overlay_sizing.py).
    # Deliberately loose-typed: generative is best-effort and must never hard-fail
    # parsing on a drifted value, so a bad safe_zone/density is tolerated here and
    # validated/clamped downstream rather than rejected at the schema.
    text_safe_zone: dict | None = None  # {"x","y","w","h"} normalized to the 9:16 frame
    visual_density: float = 5.0  # 0 (empty) .. 10 (cluttered); clamped at use
    composition_note: str = ""
    # Format-aware labels. Defaulted + coerced so the existing hook/transcript
    # fields stay authoritative and a missing/drifted label can't break parsing.
    content_type: ClipContentType = "broll"
    audio_type: ClipAudioType = "ambient"

    @field_validator("content_type", mode="before")
    @classmethod
    def _coerce_content_type(cls, v: object) -> str:
        if isinstance(v, str):
            n = v.strip().lower().replace("-", "_").replace(" ", "_")
            if n in _CONTENT_TYPES:
                return n
        return "broll"

    @field_validator("audio_type", mode="before")
    @classmethod
    def _coerce_audio_type(cls, v: object) -> str:
        if isinstance(v, str):
            n = v.strip().lower().replace("-", "_").replace(" ", "_")
            if n in _AUDIO_TYPES:
                return n
        return "ambient"


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


# Minimum gap (seconds) between consecutive accepted moments. Mirrors HARD RULE 2
# in prompts/analyze_clip.txt. Gemini-2.5-flash sometimes ignores the prompt rule
# and emits 3 moments inside 0.5s; this post-filter is the deterministic safety net.
# Bumping this past ~3s starts dropping legitimately tight moments on short clips,
# so 2.0 matches the prompt's stated contract.
_MIN_MOMENT_SPACING_S = 2.0

# Minimum moment duration. Anything shorter is a frame index, not a moment
# (HARD RULE 3 in the prompt).
_MIN_MOMENT_DURATION_S = 1.0


def _enforce_moment_spread(moments: list[Moment]) -> list[Moment]:
    """Deterministic post-filter on the Gemini-returned best_moments list.

    The prompt has explicit HARD RULES for spread + duration but Gemini still
    occasionally returns 3 moments clustered inside 0.5s (TODOS.md 2026-05-13 —
    affected 3/5 prod fixtures, judge avg 2.5/5). This filter applies the same
    rules in code so the downstream matcher always sees distinct candidates:

      1. Drop any moment whose duration < _MIN_MOMENT_DURATION_S (rule 3).
      2. Sort the remainder by start_s.
      3. Greedy-accept: keep a moment if its start_s is at least
         _MIN_MOMENT_SPACING_S after the last accepted moment's start_s.

    A clip with one legitimate moment correctly returns one moment — the prompt
    explicitly says "fewer strong moments > padding the list" so we never invent.
    """
    valid = [
        m
        for m in moments
        if isinstance(m.end_s, (int, float))
        and isinstance(m.start_s, (int, float))
        and (m.end_s - m.start_s) >= _MIN_MOMENT_DURATION_S
    ]
    valid.sort(key=lambda m: m.start_s)

    kept: list[Moment] = []
    for m in valid:
        if not kept or (m.start_s - kept[-1].start_s) >= _MIN_MOMENT_SPACING_S:
            kept.append(m)
    return kept


def _coerce_density(value: object) -> float:
    """Clamp a model-supplied visual_density into [0, 10]; default 5.0 on junk.
    Loose by design — a drifted value must never fail the best-effort clip parse.
    Non-finite (NaN/inf, which `float("NaN")` produces) is treated as junk so it
    can't poison the sizer's clamp math downstream."""
    try:
        d = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 5.0
    if not math.isfinite(d):
        return 5.0
    return max(0.0, min(10.0, d))


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
        # 2026-05-28 — _enforce_moment_spread() post-filter added in parse().
        # 2026-05-31 — prompt emits composition fields (text_safe_zone /
        # visual_density / composition_note) consumed by overlay_sizing for the
        # agent-decided generative intro size.
        # 2026-05-31.1 — added content_type + audio_type labels (format-aware edit
        # engine), layered on the composition fields above. New fields default +
        # coerce, so a hook_score regression here is attributable; D4 eval gate
        # compares hook_score against the prior baseline before merge.
        # 2026-06-07   — image branch: still photograph analysis path in render_prompt,
        # requesting text_safe_zone + visual_density so intro_sizing doesn't degrade.
        prompt_version="2026-06-07",
        model="gemini-2.5-flash",
        # Gemini pricing as of 2026 — input ~$0.075/M, output ~$0.30/M (2.5 Flash).
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
        # Cap internal reasoning. Default dynamic thinking burned ~4k thought-
        # tokens / 13-18s on real clips (A/B measured) vs ~5s at 512 — with NO
        # quality loss: subject/hook_text/best_moments held or improved across
        # 3 real clips + repeats (default occasionally returned an empty
        # transcript / degenerate moment that the capped run did not). 512 keeps
        # ample reasoning headroom for the vision extraction. Validated on real
        # clips because the eval fixtures' source videos were GC'd from GCS.
        thinking_budget=512,
        # Skip the clarification retry: prod logs showed `attempts=2
        # latency_ms=233209` (3m 53s) on schema/refusal retries that almost
        # always failed the same way. Caller (template_orchestrate) has a
        # Whisper fallback that's faster and good enough for the
        # transcript+best_moments fields. Saves ~100s on the ~10-15% of
        # clips where Gemini's first attempt returns malformed JSON.
        enable_clarification_retries=False,
        # On the longest clips (14k+ input tokens) Gemini truncates JSON
        # output near the token ceiling — production job 1b555c69-…
        # ("concert crowd") died with `Invalid JSON: Expecting property
        # name…` on a missing closing brace + stray comma. Opt in to the
        # runtime's one-shot json-repair pass so the punctuation-broken
        # response is salvaged before falling through to terminal_refusal
        # (which would force the slow Whisper fallback for a recoverable
        # response).
        enable_json_repair=True,
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
        # ── Image (still photograph) path ─────────────────────────
        # When the file is a still image, bypass the video prompt and use
        # a photo-specific instruction set.  We still request all composition
        # fields (text_safe_zone + visual_density) so _hero_composition in
        # generative_build.py can size the intro overlay correctly — omitting
        # them would silently degrade every photos-only job to the full-width
        # fallback intro.
        if input.file_mime.startswith("image/"):
            return load_prompt("analyze_clip_photo")

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

        # Deterministic post-filter for the clustering bug (TODOS.md 2026-05-13):
        # Gemini periodically returns 3 moments inside 0.5s despite the prompt's
        # HARD RULES, defeating downstream matcher variety. Enforce spread in code.
        moments = _enforce_moment_spread(moments)

        try:
            return ClipMetadataOutput(
                transcript=str(data.get("transcript", "") or ""),
                hook_text=str(data.get("hook_text", "") or ""),
                hook_score=hook_score,
                best_moments=moments,
                detected_subject=str(data.get("detected_subject", "") or ""),
                # Composition fields drive overlay_sizing. parse() reconstructs the
                # output field-by-field, so these MUST be threaded explicitly or the
                # model's values are silently dropped (the inert-feature bug caught by
                # local-render: Gemini returned them but the sizer never saw them).
                # Loose/defensive to match the schema's best-effort contract.
                text_safe_zone=(
                    safe_zone
                    if isinstance((safe_zone := data.get("text_safe_zone")), dict)
                    else None
                ),
                visual_density=_coerce_density(data.get("visual_density")),
                composition_note=str(data.get("composition_note", "") or ""),
                # Format-aware labels — same field-by-field threading rule: pass the
                # raw values; the schema's before-validators coerce None/unknown to
                # the safe defaults (broll/ambient), so a missing label never breaks
                # parse and never falsely promotes a clip to the talking-head spine.
                content_type=data.get("content_type"),
                audio_type=data.get("audio_type"),
            )
        except ValidationError as exc:
            raise SchemaError(f"clip_metadata: output validation — {exc}") from exc
