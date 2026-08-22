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

# Formats spined by narration. With NARRATED_SELF_NARRATION_ENABLED off (the
# default), every one REQUIRES a recorded voiceover and generation is blocked
# until one is attached — without it the job silently falls back to montage.
# With the flag on, the footage's own speech may spine the edit instead
# (_resolve_archetype routes 1 clip → subtitled, 2+ → talking_head; no speech →
# montage with a persisted, user-visible reason). Single source of truth for
# the grouping.
NARRATED_EDIT_FORMATS: frozenset[str] = frozenset(
    {"narrated", "narrated_planned", "narrated_ready"}
)

# Strict guided rendering is intentionally audio-destructive: it removes source
# audio and substitutes a matched library track. Keep this allowlist positive so
# an unknown/future format can never accidentally enter that renderer.
GUIDED_EDIT_FORMATS: frozenset[str] = frozenset({"montage", "day_vlog", "single_hero"})

# These formats preserve a speech/audio spine and therefore cannot coexist with
# a guided story snapshot. This is a compatibility policy, not an archetype
# selector; the native resolver still decides the concrete assembler later.
AUDIO_LED_EDIT_FORMATS: frozenset[str] = frozenset(
    set(NARRATED_EDIT_FORMATS) | {"subtitled", "talking_head"}
)

RenderProgram = Literal["guided", "native"]


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


def render_program_for_intent(value: object, *, has_voiceover: bool) -> RenderProgram:
    """Choose the only render-program family allowed for an item snapshot.

    ``coerce_edit_format`` intentionally maps unknown values to montage for the
    normal edit engine. That fallback is not safe for guided compatibility: a new
    or malformed token must not silently opt into the audio-destructive guided
    renderer. Empty values retain the historical montage default; non-empty
    unknown values fall back to the native program.
    """

    if has_voiceover:
        return "native"
    if value is None:
        raw = DEFAULT_EDIT_FORMAT
    elif isinstance(value, str):
        raw = value.strip().lower().replace("-", "_").replace(" ", "_")
    else:
        return "native"
    if not raw:
        raw = DEFAULT_EDIT_FORMAT
    if raw not in EDIT_FORMATS:
        return "native"
    if raw in AUDIO_LED_EDIT_FORMATS:
        return "native"
    if raw in GUIDED_EDIT_FORMATS:
        return "guided"
    # Exhaustiveness guard: adding a format requires an explicit compatibility
    # decision above rather than inheriting guided behavior by accident.
    return "native"


def guided_edit_applicable(value: object, *, has_voiceover: bool) -> bool:
    """Return whether strict guided editing may be used for this intent."""

    return render_program_for_intent(value, has_voiceover=has_voiceover) == "guided"
