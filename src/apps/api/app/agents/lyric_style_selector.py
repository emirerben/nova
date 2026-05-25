"""nova.audio.lyric_style_selector — pick a lyric STYLE SET for a music job.

Text-only (no audio re-listening): consumes the `MusicLabels` already attached
to the track by `song_classifier` plus the candidate style sets eligible for
the music path, and returns one `style_set_id`. The chosen set supplies the
lyric style + styling defaults to `lyric_injector` (admin `lyrics_config` still
overrides per-field).

Best-effort by design: the orchestrator wraps `run()` in a try/except and leaves
`style_set_id` unset on any failure, so lyrics still render with their existing
defaults. `parse()` clamps an out-of-catalog choice to "default" rather than
raising, mirroring the silent-drop posture of `music_matcher`.
"""

from __future__ import annotations

import json
from typing import ClassVar

from pydantic import BaseModel, Field

from app.agents._runtime import Agent, AgentSpec, SchemaError
from app.agents._schemas.music_labels import MusicLabels
from app.pipeline.prompt_loader import load_prompt
from app.pipeline.style_sets import list_style_sets

_DEFAULT_SET_ID = "default"


class StyleSetCandidate(BaseModel):
    id: str = Field(min_length=1)
    label: str = ""
    tags: list[str] = Field(default_factory=list)


class LyricStyleSelectorInput(BaseModel):
    labels: MusicLabels
    title: str = ""
    # Eligible sets the agent may pick from. Defaults to the music-applicable
    # catalog so callers usually don't pass it; injected into the prompt and
    # used by parse() to reject hallucinated ids.
    available_sets: list[StyleSetCandidate] = Field(default_factory=list)


class LyricStyleSelectorOutput(BaseModel):
    style_set_id: str = Field(min_length=1)
    rationale: str = ""


def _music_candidates() -> list[StyleSetCandidate]:
    return [StyleSetCandidate(**s) for s in list_style_sets(applies_to="music")]


class LyricStyleSelectorAgent(Agent[LyricStyleSelectorInput, LyricStyleSelectorOutput]):
    spec: ClassVar[AgentSpec] = AgentSpec(
        name="nova.audio.lyric_style_selector",
        prompt_id="select_lyric_style",
        prompt_version="2026-05-25",
        model="gemini-2.5-flash",
        cost_per_1k_input_usd=0.000075,
        cost_per_1k_output_usd=0.0003,
    )
    Input = LyricStyleSelectorInput
    Output = LyricStyleSelectorOutput

    def required_fields(self) -> list[str]:
        return ["style_set_id"]

    def render_prompt(self, input: LyricStyleSelectorInput) -> str:  # noqa: A002
        candidates = input.available_sets or _music_candidates()
        set_lines = "\n".join(
            f"- id={c.id} | {c.label} | tags={', '.join(c.tags) or '(none)'}" for c in candidates
        )
        lab = input.labels
        return load_prompt(
            "select_lyric_style",
            title=(input.title or "").replace("\n", " ")[:120],
            genre=lab.genre,
            energy=lab.energy,
            pacing=lab.pacing,
            mood=lab.mood,
            vibe_tags=", ".join(lab.vibe_tags) or "(none)",
            set_lines=set_lines,
        )

    def parse(
        self,
        raw_text: str,
        input: LyricStyleSelectorInput,  # noqa: A002
    ) -> LyricStyleSelectorOutput:
        try:
            data = json.loads(raw_text)
        except (ValueError, TypeError) as exc:
            raise SchemaError(f"lyric_style_selector: invalid JSON — {exc}") from exc
        if not isinstance(data, dict):
            raise SchemaError("lyric_style_selector: response is not a JSON object")

        valid_ids = {c.id for c in (input.available_sets or _music_candidates())}
        chosen = str(data.get("style_set_id", "") or "").strip()
        if chosen not in valid_ids:
            # Hallucinated / empty id → fall back to default rather than fail
            # the whole job. The catalog always contains "default".
            chosen = _DEFAULT_SET_ID
        return LyricStyleSelectorOutput(
            style_set_id=chosen,
            rationale=str(data.get("rationale", "") or "").strip(),
        )
