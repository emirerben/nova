"""Project shared approved decisions into a source-bound native render program.

No media is downloaded or rendered here. Unsupported lanes fail closed until
their native implementations and parity fixtures are available.
"""

from __future__ import annotations

import math

from app.kria.recipes import (
    AssetFingerprint,
    AudioMixRecipe,
    Canvas,
    MediaAsset,
    MediaSize,
    TimelineClip,
    TimelineTrack,
    Transition,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest
from app.pipeline.guided_story import _FRAME_S, GuidedStoryExecutionPlan, _story_canvas
from app.schemas.guided_edit_revision import GUIDED_EDITOR_FPS
from app.services.phone_sources import PhoneSourceBinding, require_bound_moment


class UnsupportedPhonePlan(ValueError):
    """A device recipe cannot yet represent the full approved plan.

    ``capability`` names the ``MediaCapability`` this lane would require once
    the V2 recipe schema grows fields for it (see
    docs/reviews/kri-29/capability-matrix.md); it is informational only —
    fixing this by adding the capability to ``phone_render_verified_features``
    does nothing, since the underlying plan content never reaches a recipe.
    """

    def __init__(self, message: str, *, capability: str | None = None) -> None:
        super().__init__(message)
        self.capability = capability


# Plan lane -> the MediaCapability it would require once the recipe schema
# carries its content. Kept next to the reject loop so a new plan lane and
# its capability name are added together.
_UNSUPPORTED_PHONE_LANE_CAPABILITY: dict[str, str] = {
    "music": "musicBed",
    "narration": "narrationAudio",
    "licensed_sfx_intent": "soundEffects",
    "editor_sound_effects": "soundEffects",
    "editor_media_overlays": "mediaCards",
    "editor_visual_blocks": "visualBlocks",
    "editor_motion_scenes": "motionScenes",
    "editor_custom_effects": "customEffects",
}


# `story_timeline` moments persist source/output timestamps independently
# rounded to milliseconds (see guided_story.py's `round(x, 3)` calls). Two
# separately-rounded millisecond values compared against a third (e.g.
# `source_end_s - source_start_s` vs. the stored `duration_s`) can legitimately
# differ by up to ~2ms of compounding rounding noise — confirmed live via a
# 1ms mismatch (job a994fddd) that a microsecond-scale tolerance rejected
# outright. None of these checks are about float-precision noise; they exist
# to catch a genuinely different timing program, which differs by much more
# than a couple of milliseconds.
_TIMING_ROUNDING_TOLERANCE_S = 0.005

# When a moment's source window has to be refit into what the device's own
# original file actually measures (see the refit block below), never fit
# exactly to that boundary — reading a track to its precise reported duration
# is a classic AVFoundation edge case (the true last frame can land a few ms
# past the nominal duration, or reading up to the exact boundary lands
# mid-frame and the on-device export fails outright).
_EXPORT_SAFETY_MARGIN_S = 0.05

# Guided-editor v2 revisions quantize every position to the editor frame clock
# (`app.schemas.guided_edit_revision.GUIDED_EDITOR_FPS`, the same 1/30 s as
# guided_story's `_FRAME_S`); approval plans use a millisecond clock. A phone
# program may sit on either.


def _clock_aligned(value: float) -> bool:
    """True when `value` sits on the millisecond clock or the guided-editor frame clock."""
    return math.isclose(value, round(value, 3), abs_tol=1e-9) or math.isclose(
        value * GUIDED_EDITOR_FPS, round(value * GUIDED_EDITOR_FPS), abs_tol=1e-4
    )


def compile_phone_guided_plan(
    plan: GuidedStoryExecutionPlan, bindings: tuple[PhoneSourceBinding, ...]
) -> EditRecipeV2:
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.portable_text_layout import compile_text_overlay

    for lane, capability in _UNSUPPORTED_PHONE_LANE_CAPABILITY.items():
        if getattr(plan, lane):
            raise UnsupportedPhonePlan(f"unsupported phone lane: {lane}", capability=capability)
    transition_names = {
        "crossfade": "crossfade",
        "dip_to_black": "fade_black",
        "flash": "fade_white",
    }
    boundaries = [
        moment.transition_after or plan.transition_policy.type
        for moment in plan.story_timeline[:-1]
    ]
    has_transitions = any(value not in {"none", "cut"} for value in boundaries)
    preserve_audio = bool((plan.montage_audio or {}).get("preserve_source_audio"))
    # V6 reconstructs cloud source audio at the same overlapping source
    # windows as the native mixer. Legacy transition renders discarded audio.
    if has_transitions and preserve_audio and plan.compiler_version < 6:
        raise UnsupportedPhonePlan("source audio across guided transitions is not yet verified")
    if plan.story_timeline and plan.story_timeline[-1].transition_after not in {None, "cut"}:
        raise UnsupportedPhonePlan("final phone moment cannot transition to a missing moment")
    if len({binding.media_id for binding in bindings}) != len(bindings):
        raise ValueError("phone bindings must have unique media identities")
    assets = {}
    manifest = {}
    clips = []
    cursor = 0.0
    canvas = _story_canvas(plan.output_orientation)
    for index, moment in enumerate(plan.story_timeline):
        binding = require_bound_moment(
            bindings,
            media_id=moment.media_id,
            path=moment.gcs_path,
            generation=moment.generation,
        )
        if (
            moment.kind != "video"
            or moment.layout != "fullscreen"
            or moment.image_motion is not None
            or moment.look_preset not in {"none", "golden_hour"}
            or moment.look_adjustments
        ):
            raise UnsupportedPhonePlan("unsupported phone moment treatment")
        if moment.look_preset == "golden_hour" and (
            (binding.original.width, binding.original.height) != (canvas.width, canvas.height)
            or binding.original.orientation_degrees != 0
        ):
            raise UnsupportedPhonePlan("phone looks require exact-canvas unrotated sources")
        source_duration = moment.source_end_s - moment.source_start_s
        incoming = None
        expected_start = cursor
        if index and boundaries[index - 1] not in {"none", "cut"}:
            previous = plan.story_timeline[index - 1]
            requested = previous.transition_duration_s or plan.transition_policy.duration_s or 0.3
            clamped = min(requested, min(previous.duration_s, moment.duration_s) * 0.3)
            # The overlap the plan actually laid out is the authority: approval
            # plans place `output_start_s` exactly `clamped` before the previous
            # moment ends (millisecond clock), while guided-editor v2 revisions
            # re-clock every position onto 1/30 s frames and keep the requested
            # `transition_duration_s` as authored (0.12 s requested, 4 frames =
            # 0.133333 s laid out). Rendering the authored value against
            # frame-clocked positions is a different program from the one the
            # revision encodes -- and rejecting the frame clock outright made
            # every timeline save on a phone variant a 422 (2026-09-19, job
            # d9a965b0). Accept either clock; still refuse a layout that
            # contradicts the request by more than one frame.
            duration = round(previous.output_end_s - moment.output_start_s, 6)
            if (
                duration <= _TIMING_ROUNDING_TOLERANCE_S
                or duration
                > min(previous.duration_s, moment.duration_s) + _TIMING_ROUNDING_TOLERANCE_S
            ):
                raise UnsupportedPhonePlan("phone transition must overlap its neighbouring moments")
            if abs(duration - clamped) > _FRAME_S + _TIMING_ROUNDING_TOLERANCE_S:
                raise UnsupportedPhonePlan(
                    "phone transition timing must match the approved overlap"
                )
            if not _clock_aligned(duration):
                raise UnsupportedPhonePlan("phone transition timing must match cloud milliseconds")
            incoming = Transition(kind=transition_names[boundaries[index - 1]], duration=duration)
            expected_start -= duration
            if not _clock_aligned(expected_start):
                raise UnsupportedPhonePlan("phone transition offset must match cloud milliseconds")
        if (
            not math.isclose(
                moment.output_start_s, expected_start, abs_tol=_TIMING_ROUNDING_TOLERANCE_S
            )
            or not math.isclose(
                source_duration, moment.duration_s, abs_tol=_FRAME_S + _TIMING_ROUNDING_TOLERANCE_S
            )
            or not math.isclose(
                moment.output_end_s - moment.output_start_s,
                moment.duration_s,
                abs_tol=_TIMING_ROUNDING_TOLERANCE_S,
            )
        ):
            raise UnsupportedPhonePlan("phone moment timing must preserve its exact source window")
        if not math.isclose(
            source_duration, moment.duration_s, abs_tol=_TIMING_ROUNDING_TOLERANCE_S
        ):
            # A v2 revision quantizes `duration_s` and an approval-inherited
            # `source_end_s` independently, so the two can disagree by one
            # frame (7.153 s -> 7.166667 s vs 7.133333 s). The output slot is
            # what the timeline, text and audio are timed against: fill it
            # from the source rather than leave a one-frame hole. The refit
            # below still shifts the window if the original runs out.
            source_duration = round(moment.duration_s, 6)
        source_start = moment.source_start_s
        if (
            moment.source_end_s > binding.original.duration_s
            or source_start >= binding.original.duration_s
        ):
            # `moment.source_start_s`/`source_end_s` are planned against the
            # analysis proxy's server-measured (ffprobe) duration;
            # `binding.original.duration_s` is the client's on-device
            # (AVFoundation) measurement of the same file — independent
            # measurements of conceptually the same quantity, and planning
            # intentionally saturates a clip's proxy-measured capacity (and
            # centers shorter windows within it) when the target duration
            # demands it. A short clip carrying a fraction of a beat, or a
            # multi-clip beat's centered window, routinely lands outside what
            # the on-device file actually measures (observed live: jobs
            # aeb62e3c/0e84c6f8/031c8ff0). That disagreement is expected, not
            # a broken plan — refit the window into what the device's own
            # file actually has, preferring to keep the full requested
            # duration by shifting the start rather than truncating it (a
            # truncated moment would desync from the text/audio timed against
            # its original duration).
            # Never fit exactly to the device-measured boundary: reading a
            # track to its precise reported duration is a classic AVFoundation
            # edge case (the true last frame can land a few ms past the
            # nominal duration, or reading up to the exact boundary lands
            # mid-frame and the export fails). Leave a small safety margin.
            available = max(0.0, binding.original.duration_s - _EXPORT_SAFETY_MARGIN_S)
            fitted_duration = min(source_duration, available)
            if fitted_duration < 0.1:
                raise UnsupportedPhonePlan(
                    "phone moment timing must preserve its exact source window"
                )
            source_start = min(source_start, max(0.0, available - fitted_duration))
            source_duration = round(fitted_duration, 6)
        original = binding.original
        asset = binding.render_asset()
        manifest[asset.id] = asset
        assets[asset.id] = MediaAsset(
            id=asset.id,
            relative_path=asset.id,
            fingerprint=AssetFingerprint(hex=original.sha256, byte_count=original.byte_count),
            duration=original.duration_s,
            natural_size=MediaSize(width=original.width, height=original.height),
            orientation_degrees=original.orientation_degrees,
            # `PhoneSourceBinding.require_proxy` already guarantees a reserved
            # analysis proxy exists for every binding reaching this compiler —
            # accurately reflect that on the asset rather than leaving the
            # field at its always-false default. No client consumes this yet
            # (proxy-based local preview is KRI-95's job); this only makes the
            # recipe describe reality.
            is_proxy_available=True,
        )
        clips.append(
            TimelineClip(
                id=moment.moment_id,
                source_asset_id=asset.id,
                source_start=source_start,
                source_duration=source_duration,
                timeline_start=moment.output_start_s,
                rate=1,
                transition=incoming,
                look="golden_hour" if moment.look_preset == "golden_hour" else None,
            )
        )
        cursor = moment.output_end_s
    if not math.isclose(cursor, plan.resolved_duration_s, abs_tol=_TIMING_ROUNDING_TOLERANCE_S):
        raise ValueError("phone timeline duration differs from approved plan")
    if len({clip.id for clip in clips}) != len(clips):
        raise ValueError("phone moments must have unique identities")
    # The on-device refit above (per moment) can shorten a clip's
    # `source_duration` below what `moment.output_end_s`/`plan.resolved_duration_s`
    # expected, without moving any `timeline_start` -- so the COMPILED timeline
    # can end earlier than the plan's nominal duration even though `cursor`
    # (which only ever tracks the unrefit planned positions) still matched it
    # exactly, just above. Mirrors `EditRecipeV1.duration`'s own formula so
    # this is exactly what the recipe will report once constructed below.
    # Text layers are compiled from the plan's (pre-refit) timing and must
    # never be allowed to end past this real, possibly-shrunk duration -- see
    # the clamp pass after the layer loop.
    compiled_duration = max(
        (clip.timeline_start + clip.source_duration / clip.rate for clip in clips), default=0.0
    )
    # Millisecond rounding noise between independently-rounded timing fields
    # is expected and already tolerated everywhere else in this function (see
    # `_TIMING_ROUNDING_TOLERANCE_S`'s docstring above) -- it must not, by
    # itself, be treated as a refit shrink. Only clamp text when the refit
    # actually shortened the compiled timeline by more than that noise floor;
    # otherwise every ordinary plan (no clip ever needed a refit) stays
    # byte-identical, including a title that legitimately holds to exactly
    # `plan.resolved_duration_s`.
    text_bound_s = (
        max(0.0, compiled_duration - _EXPORT_SAFETY_MARGIN_S)
        if compiled_duration < plan.resolved_duration_s - _TIMING_ROUNDING_TOLERANCE_S
        else None
    )
    layers = []
    ordered_overlays = []
    for lane, elements in (
        ("text", plan.text_elements),
        ("context", plan.context_label_text_elements),
        ("narration", plan.narration_label_text_elements),
    ):
        overlays = build_overlays_from_text_elements(
            elements, video_duration_s=plan.resolved_duration_s, independent_box_alignment=True
        )
        ordered_overlays.extend(
            (f"{lane}-{index}", overlay) for index, overlay in enumerate(overlays)
        )
    # Production renders non-sequence inputs first, then the sequence composite.
    # Keep stable order within each partition, including context/narration lanes.
    ordered_overlays.sort(key=lambda entry: entry[1].get("role") == "generative_sequence")
    for index, (layer_id, overlay) in enumerate(ordered_overlays):
        if overlay.get("role") == "generative_sequence" and overlay.get("effect", "none") not in {
            "fade-in",
            "static",
            "none",
            "handwriting",
            "ink-reveal",
        }:
            raise UnsupportedPhonePlan("sequence effect needs composite-stream parity")
        layer, font = compile_text_overlay(
            overlay, layer_id=layer_id, canvas=canvas, dissolve_seed=101 + index * 37
        )
        if font is not None:
            manifest[font.id] = font
            assets[font.id] = MediaAsset(
                id=font.id,
                relative_path=font.id,
                fingerprint=AssetFingerprint(
                    hex=font.fingerprint.sha256, byte_count=font.fingerprint.byte_count
                ),
            )
        layers.append(layer)
    # Clamp every text layer (title, context/narration labels, sequence, and
    # any "hold to the end of the story" layer whose compiled `end` equals the
    # plan's nominal duration) to the timeline this recipe will actually
    # report, shrinking `start` too if a layer would otherwise become shorter
    # than one frame. Never emit a layer that ends past `recipe.duration` --
    # see `EditRecipeV2.validate_asset_manifest`'s "text layer exceeds the
    # timeline" check. `text_bound_s` is None (no-op) unless a real refit
    # shrink happened, so a layer that already fit is left untouched.
    if text_bound_s is not None:
        for layer in layers:
            if layer.end > text_bound_s:
                layer.end = text_bound_s
                if layer.end - layer.start < _FRAME_S:
                    layer.start = max(0.0, layer.end - _FRAME_S)
    return EditRecipeV2(
        canvas=Canvas(width=canvas.width, height=canvas.height),
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=[TimelineTrack(id="story", kind="video", clips=clips)],
        text_layers=layers,
        audio=AudioMixRecipe(original_volume=plan.editor_audio_level if preserve_audio else 0),
        required_capabilities={"basicComposition", "local1080Export"}
        | (
            {"crossfade"}
            if any(clip.transition and clip.transition.kind == "crossfade" for clip in clips)
            else set()
        )
        | ({"positionedText"} if layers else set())
        | (
            {"animatedText"}
            if any(layer.effect not in {"static", "none"} for layer in layers)
            else set()
        )
        | ({"audioMix"} if preserve_audio else set()),
    )
