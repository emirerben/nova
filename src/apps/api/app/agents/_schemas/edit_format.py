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
    "slides",
]

DEFAULT_EDIT_FORMAT: EditFormat = "montage"

# Worker boundary fence for the first non-legacy guided format.  This is kept
# with the vocabulary so API/worker mixed-version jobs can fail closed instead
# of treating a newly-known token as the legacy montage default.
DAY_VLOG_RENDERER_VERSION = 1
# Worker boundary fence for the single-hero guided renderer.  Keep this
# independent from day-vlog so either format can roll out or roll back without
# accepting a queued job authored by a different renderer contract.
SINGLE_HERO_RENDERER_VERSION = 1
# Worker boundary fence for the slide-post (mixed-media carousel) renderer.
# Not a guided format (see GUIDED_EDIT_FORMATS below) but the same class of
# hazard applies: a mixed API/worker deploy must never let an old worker
# silently coerce a "slides" job to montage. Bump on any change to the
# stamped variant contract (variant_id, slide_post shape, bundle layout).
SLIDES_RENDERER_VERSION = 1

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

# Positive allowlist of EditFormat values whose decisions have a phone-recipe
# compiler (app/pipeline/phone_<archetype>_plan.py) and can therefore render
# directly from on-device analysis-proxy sources. Guided-story approval is a
# SEPARATE gate handled via `guided_applicable` / an approved edit proposal in
# content_plan_build — this set governs everything else. Grown one phase at a
# time as each archetype's phone compiler ships; kept positive on purpose so
# an unknown/future format never accidentally qualifies.
#
# IMPORTANT — this is COMPILER EXISTENCE, not "will render right now". It is a
# static, settings-free constant (importable at module load, safe to
# monkeypatch-free-compare in tests) so it deliberately does NOT encode
# runtime flags/rollout state. Every runtime decision (dispatch gate, the
# worker's own dispatch fork, the chat picker) MUST consult
# `app.services.phone_rollout.phone_render_supported_formats()` instead — that
# settings-aware helper intersects this set with the currently-enabled
# subset. Mirrors the existing montage-family precedent: `montage` has been in
# this set since KRI-114 even though a voiceover on it additionally requires
# `phone_narration_rendering_enabled` + a verified `narrationAudio` before it
# actually renders (see that flag's docstring in `app/config.py`).
#
# KRI-114 P1-2/P1-4: montage/day_vlog/single_hero without a voiceover compile
# through `app.pipeline.phone_montage_plan.compile_phone_montage_plan`
# (`app.tasks.generative_build._run_phone_montage_job`); WITH a voiceover they
# compile through the same function once `phone_narration_rendering_enabled`
# + narrationAudio are satisfied (KRI-132).
# KRI-132: `subtitled` (exactly one clip, own audio → editable captions)
# compiles through `app.pipeline.phone_subtitled_plan.compile_phone_subtitled_plan`
# (`_run_phone_subtitled_job`), gated by `phone_subtitled_rendering_enabled` +
# `subtitled_archetype_enabled`. `narrated`/`narrated_planned`/`narrated_ready`
# WITH a recorded voiceover compile through
# `app.pipeline.phone_narrated_plan.compile_phone_narrated_plan`
# (`_run_phone_narrated_job`), gated by `phone_narrated_rendering_enabled` +
# `phone_narration_rendering_enabled` + narrationAudio + `narrated_archetype_enabled`.
# A narrated item with NO voiceover only ever reaches the phone through the
# 1-clip self-narration exception documented on
# `phone_render_supported_formats` (talking_head's 2+-clip self-narration
# resolution has no phone compiler and is never included here).
PHONE_RENDER_SUPPORTED_FORMATS: frozenset[str] = frozenset(
    {
        "montage",
        "day_vlog",
        "single_hero",
        "subtitled",
        "narrated",
        "narrated_planned",
        "narrated_ready",
    }
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
