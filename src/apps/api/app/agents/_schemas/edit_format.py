"""Shared `edit_format` vocabulary for the format-aware edit engine.

The content_plan agent declares an `edit_format` per day; the generative
orchestrator resolves it against the uploaded footage and dispatches the matching
assembler archetype (talking-head + B-roll, day-vlog temporal sequence, single
hero, subtitled single-clip auto-captions, or the default beat-synced montage).
See the format-aware-edit-engine plan.

`subtitled` is a single talk-to-camera clip whose OWN audio is transcribed into
editable sentence-block captions (Turkish + English). Unlike the `narrated`
family it needs NO voiceover — the spine is the clip's existing audio — so it is
deliberately kept OUT of `NARRATED_EDIT_FORMATS`.

`montage` is the safe default and the existing render path — any job without a
declared/recognized format renders exactly as it does today. `coerce_edit_format`
is the single normalization point: one bad LLM token must never drop an otherwise
good plan item (best-effort, mirrors how `filming_suggestion`/`rationale` degrade).
"""

from __future__ import annotations

from typing import Literal, get_args

# The canonical vocabulary. Keep this Literal, the EDIT_FORMATS tuple, the
# plan_items.edit_format CHECK-free Text column (server_default 'montage'), and
# the per-archetype variant-set config in generative_build in lockstep.
EditFormat = Literal[
    "montage",
    "talking_head",
    "day_vlog",
    "single_hero",
    "subtitled",
    "narrated",
    "narrated_planned",
    "narrated_ready",
]

DEFAULT_EDIT_FORMAT: EditFormat = "montage"

EDIT_FORMATS: tuple[str, ...] = get_args(EditFormat)

# Formats that render the narrated archetype — every one REQUIRES a user voiceover
# (the narration is the spine). Generation must be blocked for these until a
# voiceover is attached; without it the job has no narration and silently falls
# back to montage. Single source of truth for that grouping.
NARRATED_EDIT_FORMATS: frozenset[str] = frozenset(
    {"narrated", "narrated_planned", "narrated_ready"}
)


def coerce_edit_format(value: object) -> EditFormat:
    """Normalize an arbitrary value to a known EditFormat, defaulting to montage.

    Defensive on purpose: the LLM-emitted value, a legacy DB row, or a stale API
    payload can all be None / unknown / wrong-cased. Anything we don't recognize
    falls back to the montage default rather than raising, so a single drifted
    token can't 422 a whole content plan or hard-fail a render.
    """
    if isinstance(value, str):
        normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
        if normalized in EDIT_FORMATS:
            return normalized  # type: ignore[return-value]
    return DEFAULT_EDIT_FORMAT
