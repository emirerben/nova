"""Section plan for a spoken-excerpt montage (KRI-282).

The planner (``nova.plan.speech_excerpt_planner``) reads timed speech segments
and the creator's request and returns an ordered list of sections:

* ``speech``  -- play one excerpt of a clip's own speech, quoted by phrase. The
  visual is either the ``speaker`` (cut to the person talking) or ``cutaways``
  (the speech continues over other footage).
* ``montage`` -- a run of fast cuts over the other footage, source audio muted.

The plan carries the creator's REQUEST (a quoted phrase), never a resolved
timestamp: a worker grounds each quote to real word timings at render time
(``app.services.speech_segments.ground_excerpt``), the same way KRI-178
reaction beats work. Clips are named by the short refs the planner was shown
(``c1``...), never by media id, so a hallucinated id cannot reach a recipe.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

MAX_SECTIONS = 24
MAX_QUOTE_CHARS = 400
MIN_MONTAGE_SECTION_S = 1.0
MAX_MONTAGE_SECTION_S = 20.0
MIN_CUT_S = 0.4
MAX_CUT_S = 1.5
# A single excerpt longer than this is a monologue, not an excerpt.
MAX_EXCERPT_S = 25.0

SpeechVisual = Literal["speaker", "cutaways"]


def _clean(value: object, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


class SpeechSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["speech"] = "speech"
    clip_ref: str = Field(min_length=1, max_length=20)
    quote: str = Field(min_length=1, max_length=MAX_QUOTE_CHARS)
    visual: SpeechVisual = "speaker"

    @field_validator("quote", mode="before")
    @classmethod
    def _quote(cls, v: object) -> str:
        return _clean(v, MAX_QUOTE_CHARS)

    @field_validator("clip_ref", mode="before")
    @classmethod
    def _ref(cls, v: object) -> str:
        return _clean(v, 20)


class MontageSection(BaseModel):
    model_config = ConfigDict(extra="ignore")

    kind: Literal["montage"] = "montage"
    duration_s: float = Field(ge=MIN_MONTAGE_SECTION_S, le=MAX_MONTAGE_SECTION_S)
    cut_s: float | None = Field(default=None, ge=MIN_CUT_S, le=MAX_CUT_S)

    @field_validator("duration_s", mode="before")
    @classmethod
    def _duration(cls, v: object) -> object:
        # Be lenient about a model that overshoots a bound: clamp, don't fail the plan.
        try:
            return min(max(float(v), MIN_MONTAGE_SECTION_S), MAX_MONTAGE_SECTION_S)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return v

    @field_validator("cut_s", mode="before")
    @classmethod
    def _cut(cls, v: object) -> object:
        if v is None:
            return None
        try:
            return min(max(float(v), MIN_CUT_S), MAX_CUT_S)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            return None


PlanSection = Annotated[SpeechSection | MontageSection, Field(discriminator="kind")]


class SpeechMontagePlan(BaseModel):
    """What the planner decided. ``question`` set means: ask the creator, render nothing."""

    model_config = ConfigDict(extra="ignore")

    wants_speech_excerpts: bool = False
    sections: list[PlanSection] = Field(default_factory=list, max_length=MAX_SECTIONS)
    question: str | None = Field(default=None, max_length=300)

    @field_validator("question", mode="before")
    @classmethod
    def _question(cls, v: object) -> str | None:
        text = _clean(v, 300)
        return text or None

    def speech_sections(self) -> list[SpeechSection]:
        return [s for s in self.sections if isinstance(s, SpeechSection)]


__all__ = [
    "MAX_CUT_S",
    "MAX_EXCERPT_S",
    "MAX_MONTAGE_SECTION_S",
    "MAX_QUOTE_CHARS",
    "MAX_SECTIONS",
    "MIN_CUT_S",
    "MIN_MONTAGE_SECTION_S",
    "MontageSection",
    "PlanSection",
    "SpeechMontagePlan",
    "SpeechSection",
    "SpeechVisual",
]
