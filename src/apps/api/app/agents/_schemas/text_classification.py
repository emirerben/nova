"""Schemas for nova.compose.text_classification — Stage F of Layer-2 pipeline.

Input: phrase list (post-Stage-E text alignment) + per-phrase JPEG thumbnails.
Output: same phrases with four classified fields per phrase.

The four classified fields mirror `TemplateTextOverlay`'s fields exactly so
stage G can trivially assemble `TemplateTextOverlay` objects from the output.
VALID_EFFECTS / VALID_ROLES / VALID_SIZE_CLASSES are imported from
`_schemas/template_text.py` — single source of truth.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field

from app.agents._schemas.template_text import (
    VALID_EFFECTS,
    VALID_ROLES,
    VALID_SIZE_CLASSES,
)
from app.agents._schemas.text_overlay_pipeline import Phrase

__all__ = [
    "TextClassificationInput",
    "TextClassificationOutput",
    "ClassifiedPhrase",
    "VALID_EFFECTS",
    "VALID_ROLES",
    "VALID_SIZE_CLASSES",
]


class TextClassificationInput(BaseModel):
    """Input to the text_classification agent.

    `phrases` are the output of Stage E (transcript alignment). Each phrase
    carries corrected `sample_text`, timing, aabb, and confidence.

    `frame_paths` maps phrase index (0-based) to the absolute path of the
    JPEG frame extracted at `phrase.start_t_s` (or the nearest 2-fps sample).
    The orchestrator populates this mapping during stage A frame extraction
    and threads it through to stage F. Paths may be absent for a phrase if
    the pipeline couldn't extract a matching frame (e.g. phrase starts past
    the video's end); the agent handles missing entries gracefully.

    Convention: frame paths live in the pipeline's scratch directory for the
    duration of the run and are cleaned up after stage G completes. The agent
    reads them synchronously in `render_prompt`; no upload step is needed
    because we use Gemini's inline `Part.from_bytes()` rather than the File
    API (avoids latency + quota overhead for small JPEG thumbnails).

    `template_id` is optional context for logging / eval tracing only.
    """

    phrases: list[Phrase] = Field(default_factory=list)
    frame_paths: dict[int, Path] = Field(
        default_factory=dict,
        description="phrase_index → absolute path to the JPEG thumbnail for that phrase.",
    )
    template_id: str | None = None


class ClassifiedPhrase(BaseModel):
    """A `Phrase` with four additional classified fields.

    All four fields default to safe fallback values so partial Gemini
    responses don't crash the pipeline — a phrase with `effect='none'`,
    `role='label'`, `size_class='medium'`, `font_color_hex='#FFFFFF'` is
    worse than a correctly classified one but far better than a crash.

    enum fields are validated at parse time in `TextClassificationAgent.parse()`;
    if Gemini returns an out-of-enum value the agent clamps to the default
    (logs a warning).
    """

    phrase: Phrase
    effect: str = Field(default="none")
    role: str = Field(default="label")
    size_class: str = Field(default="medium")
    font_color_hex: str = Field(default="#FFFFFF")


class TextClassificationOutput(BaseModel):
    """Flat list of classified phrases — one per input phrase.

    Ordering matches the input phrase list (same index). Missing phrases
    (Gemini returned fewer entries than there were input phrases) are
    represented with the default enum values in `ClassifiedPhrase`.
    """

    classified: list[ClassifiedPhrase] = Field(default_factory=list)
