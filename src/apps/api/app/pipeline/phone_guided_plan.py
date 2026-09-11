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
from app.pipeline.guided_story import GuidedStoryExecutionPlan, _story_canvas
from app.services.phone_sources import PhoneSourceBinding, require_bound_moment


class UnsupportedPhonePlan(ValueError):
    """A device recipe cannot yet represent the full approved plan."""


def compile_phone_guided_plan(
    plan: GuidedStoryExecutionPlan, bindings: tuple[PhoneSourceBinding, ...]
) -> EditRecipeV2:
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.portable_text_layout import compile_text_overlay

    for lane in (
        "music",
        "narration",
        "licensed_sfx_intent",
        "editor_sound_effects",
        "editor_media_overlays",
        "editor_visual_blocks",
        "editor_motion_scenes",
        "editor_custom_effects",
    ):
        if getattr(plan, lane):
            raise UnsupportedPhonePlan(f"unsupported phone lane: {lane}")
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
    if has_transitions and preserve_audio:
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
            duration = min(requested, min(previous.duration_s, moment.duration_s) * 0.3)
            # Cloud emits millisecond xfade durations/offsets. Reject a timing
            # program that would require a different frame-boundary decision.
            if not math.isclose(duration, round(duration, 3), abs_tol=1e-9):
                raise UnsupportedPhonePlan("phone transition timing must match cloud milliseconds")
            incoming = Transition(kind=transition_names[boundaries[index - 1]], duration=duration)
            expected_start -= duration
            if not math.isclose(expected_start, round(expected_start, 3), abs_tol=1e-9):
                raise UnsupportedPhonePlan("phone transition offset must match cloud milliseconds")
        if (
            not math.isclose(moment.output_start_s, expected_start, abs_tol=1e-6)
            or not math.isclose(source_duration, moment.duration_s, abs_tol=1e-6)
            or not math.isclose(
                moment.output_end_s - moment.output_start_s, moment.duration_s, abs_tol=1e-6
            )
            or moment.source_end_s > binding.original.duration_s
        ):
            raise UnsupportedPhonePlan("phone moment timing must preserve its exact source window")
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
        )
        clips.append(
            TimelineClip(
                id=moment.moment_id,
                source_asset_id=asset.id,
                source_start=moment.source_start_s,
                source_duration=source_duration,
                timeline_start=moment.output_start_s,
                rate=1,
                transition=incoming,
                look="golden_hour" if moment.look_preset == "golden_hour" else None,
            )
        )
        cursor = moment.output_end_s
    if not math.isclose(cursor, plan.resolved_duration_s, abs_tol=1e-6):
        raise ValueError("phone timeline duration differs from approved plan")
    if len({clip.id for clip in clips}) != len(clips):
        raise ValueError("phone moments must have unique identities")
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
