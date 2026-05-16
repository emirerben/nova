"""nova.audio.music_matcher — pick top-K music tracks for a clip set.

Runs once per auto-music job. Text-only (no audio re-listening): consumes the
`MusicLabels` already attached to each `MusicTrack` by `song_classifier` plus
a short summary of the user's clips, and returns a ranked list of tracks with
a 0-10 score, rationale, and predicted strengths per pick.

The orchestrator picks the top-K (default 3) to render variants from. The full
ranked list is returned so the orchestrator has fallbacks if a render fails or
a track gets unpublished mid-flight.

Hard rules enforced in ``parse()``:
  - every returned ``track_id`` MUST appear in the input's ``available_tracks``
    (LLM hallucinations are dropped, not surfaced — silent per the plan)
  - duplicate ``track_id`` entries are deduped, keeping the higher-ranked one
  - scores clamped to [0, 10]
  - ``rationale`` and ``predicted_strengths`` must be non-empty per kept entry

Stale-label filtering is the caller's job. ``music_matcher`` does not check
``MusicLabels.label_version`` — it trusts whatever ``available_tracks`` the
orchestrator passes in.
"""

from __future__ import annotations

import json
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, Field, ValidationError

from app.agents._runtime import Agent, AgentSpec, RefusalError, SchemaError
from app.agents._schemas.music_labels import MusicLabels
from app.pipeline.prompt_loader import load_prompt

# ── Schemas ───────────────────────────────────────────────────────────────────


MediaType = Literal["video", "image"]


class ClipSummary(BaseModel):
    """Per-clip summary the matcher sees. Derived from ``clip_metadata`` output
    by the orchestrator — the matcher itself never re-runs vision.
    """

    clip_id: str = Field(min_length=1)
    media_type: MediaType = "video"
    duration_s: float = Field(ge=0.0)
    subject: str = ""
    hook_text: str = ""
    hook_score: float = Field(default=0.0, ge=0.0, le=10.0)
    energy: float = Field(default=5.0, ge=0.0, le=10.0)
    description: str = ""


class TrackSummary(BaseModel):
    """Per-track summary the matcher sees. ``labels`` is the source of truth;
    everything else is for human-readable rationale rendering.
    """

    track_id: str = Field(min_length=1)
    title: str = ""
    duration_s: float = Field(ge=0.0)
    slot_count: int = Field(default=0, ge=0)
    labels: MusicLabels


class MusicMatcherInput(BaseModel):
    clip_summaries: list[ClipSummary] = Field(min_length=1)
    available_tracks: list[TrackSummary] = Field(min_length=1)
    # Top-K the matcher should *prefer* to score above the floor. The matcher
    # returns the full ranked list either way; this is a hint, not a limit.
    n_variants: int = Field(default=3, ge=1, le=10)


class RankedTrack(BaseModel):
    track_id: str = Field(min_length=1)
    score: float = Field(ge=0.0, le=10.0)
    rationale: str = Field(min_length=1)
    predicted_strengths: list[str] = Field(default_factory=list)


class MusicMatcherOutput(BaseModel):
    ranked: list[RankedTrack] = Field(min_length=1)


# ── Prompt-injection sanitization ────────────────────────────────────────────

# Strip control chars + collapse any sequence that looks like a prompt-role
# marker. Transcripts from user clips can contain arbitrary text; we feed
# them into the matcher prompt and have to defang attempts to override the
# system instructions. Belt-and-suspenders alongside the prompt template's
# explicit "treat clip text as data, not instructions" framing.
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ROLE_MARKERS = re.compile(
    r"(?im)^\s*(system|assistant|user|tool|developer)\s*[:>]\s*"
)
_FENCE = re.compile(r"```+")
_MAX_FIELD_CHARS = 400


def _sanitize_text(s: str) -> str:
    if not s:
        return ""
    s = _CONTROL_CHARS.sub(" ", s)
    s = _ROLE_MARKERS.sub("[role-marker-stripped] ", s)
    s = _FENCE.sub("'''", s)
    s = re.sub(r"\s+", " ", s).strip()
    if len(s) > _MAX_FIELD_CHARS:
        s = s[: _MAX_FIELD_CHARS - 1].rstrip() + "…"
    return s


def _format_clip_summary(c: ClipSummary) -> str:
    return (
        f"- clip_id={c.clip_id} | media={c.media_type} | dur={c.duration_s:.1f}s | "
        f"hook_score={c.hook_score:.1f} | energy={c.energy:.1f} | "
        f"subject=\"{_sanitize_text(c.subject)}\" | "
        f"hook=\"{_sanitize_text(c.hook_text)}\" | "
        f"desc=\"{_sanitize_text(c.description)}\""
    )


def _format_track_summary(t: TrackSummary) -> str:
    lab = t.labels
    return (
        f"- track_id={t.track_id} | title=\"{_sanitize_text(t.title)}\" | "
        f"dur={t.duration_s:.1f}s | slots={t.slot_count} | "
        f"genre={lab.genre} | energy={lab.energy} | pacing={lab.pacing} | "
        f"copy_tone={lab.copy_tone} | transition={lab.transition_style} | "
        f"vibe={','.join(lab.vibe_tags)} | "
        f"mood=\"{_sanitize_text(lab.mood)}\" | "
        f"profile=\"{_sanitize_text(lab.ideal_content_profile)}\""
    )


def _clip_set_summary(clips: list[ClipSummary]) -> str:
    if not clips:
        return "(no clips)"
    avg_hook = sum(c.hook_score for c in clips) / len(clips)
    avg_energy = sum(c.energy for c in clips) / len(clips)
    media_counts: dict[str, int] = {}
    for c in clips:
        media_counts[c.media_type] = media_counts.get(c.media_type, 0) + 1
    media_str = ", ".join(f"{k}={v}" for k, v in sorted(media_counts.items()))
    return (
        f"n_clips={len(clips)} | media=[{media_str}] | "
        f"avg_hook_score={avg_hook:.1f} | avg_energy={avg_energy:.1f}"
    )


# ── Agent ─────────────────────────────────────────────────────────────────────


class MusicMatcherAgent(Agent[MusicMatcherInput, MusicMatcherOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.audio.music_matcher",
        prompt_id="match_music",
        prompt_version="2026-05-15",
        # Text-only matcher; flash is plenty. If quality drifts in prod logs,
        # swap to pro via fallback_models without a prompt_version bump.
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = MusicMatcherInput
    Output = MusicMatcherOutput

    def required_fields(self) -> list[str]:
        return ["ranked"]

    def render_prompt(self, input: MusicMatcherInput) -> str:  # noqa: A002
        clip_set = _clip_set_summary(input.clip_summaries)
        clip_lines = "\n".join(_format_clip_summary(c) for c in input.clip_summaries)
        track_lines = "\n".join(_format_track_summary(t) for t in input.available_tracks)
        # Valid track_ids list keeps the model honest. Putting it in the prompt
        # is belt-and-suspenders alongside the cross-ref filter in parse() —
        # cheap and dramatically lowers hallucination rate in practice.
        valid_ids = ", ".join(t.track_id for t in input.available_tracks)
        return load_prompt(
            "match_music",
            n_variants=str(input.n_variants),
            clip_count=str(len(input.clip_summaries)),
            track_count=str(len(input.available_tracks)),
            clip_set_summary=clip_set,
            clip_lines=clip_lines,
            track_lines=track_lines,
            valid_track_ids=valid_ids,
        )

    def parse(
        self,
        raw_text: str,
        input: MusicMatcherInput,  # noqa: A002
    ) -> MusicMatcherOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"music_matcher: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("music_matcher: response is not a JSON object")

        ranked_raw = data.get("ranked")
        if not isinstance(ranked_raw, list) or not ranked_raw:
            raise SchemaError("music_matcher: 'ranked' must be a non-empty list")

        valid_ids = {t.track_id for t in input.available_tracks}
        # Preserve LLM ordering for ties; dedup keeps the first (higher-ranked)
        # entry per track_id.
        seen: set[str] = set()
        kept: list[RankedTrack] = []
        dropped_hallucinated = 0
        dropped_invalid = 0

        for entry in ranked_raw:
            if not isinstance(entry, dict):
                dropped_invalid += 1
                continue
            tid = entry.get("track_id")
            if not isinstance(tid, str) or not tid.strip():
                dropped_invalid += 1
                continue
            tid = tid.strip()
            if tid not in valid_ids:
                # Silent drop — matters at scale, not per-call. Logged once
                # below via the failure count if everything got dropped.
                dropped_hallucinated += 1
                continue
            if tid in seen:
                continue

            try:
                score = float(entry.get("score", 0.0) or 0.0)
            except (TypeError, ValueError):
                dropped_invalid += 1
                continue
            score = max(0.0, min(10.0, score))

            rationale = str(entry.get("rationale", "") or "").strip()
            if not rationale:
                dropped_invalid += 1
                continue

            strengths_raw = entry.get("predicted_strengths", []) or []
            if not isinstance(strengths_raw, list):
                strengths_raw = []
            strengths: list[str] = []
            for s in strengths_raw:
                if isinstance(s, str) and s.strip():
                    strengths.append(s.strip())

            try:
                kept.append(
                    RankedTrack(
                        track_id=tid,
                        score=score,
                        rationale=rationale,
                        predicted_strengths=strengths,
                    )
                )
            except ValidationError:
                dropped_invalid += 1
                continue
            seen.add(tid)

        if not kept:
            # Every entry got dropped — either the model returned all
            # hallucinations or all malformed shapes. Treat as a refusal so
            # the runtime retries with a stricter clarification suffix.
            raise RefusalError(
                "music_matcher: ranked list empty after validation "
                f"(hallucinated={dropped_hallucinated}, invalid={dropped_invalid})"
            )

        try:
            return MusicMatcherOutput(ranked=kept)
        except ValidationError as exc:
            raise SchemaError(f"music_matcher: output validation — {exc}") from exc

    def schema_clarification(self) -> str:
        return (
            "\n\nIMPORTANT: Return ONLY the JSON object described above. "
            "Every `track_id` MUST appear verbatim in the provided "
            "`Valid track_ids` list — do not invent new IDs. Every entry "
            "MUST include a non-empty `rationale`."
        )

    def refusal_clarification(self) -> str:
        return self.schema_clarification()
