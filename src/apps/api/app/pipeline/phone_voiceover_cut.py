"""Swap a phone Voiceover recipe's clip cut for the creator's own (KRI-290).

A phone `narrated` or montage `voiceover` variant has no guided plan: its only
program is the recipe pinned for the device. A timeline Save (trim, extend,
reorder, split, delete) therefore swaps just the main video track, the same
way `replace_narrated_captions` swaps captions and `replace_editor_lanes`
swaps the sound-effect / Visuals lanes. The voiceover, music bed, audio mix
and text keep their pinned shape and are only re-fitted to where the new cut
ends:

- The video owns the clock. Footage shorter than the voiceover cuts the voice
  at the video's end (with a fade); footage longer than it lets the video run
  on after the voice ends. Extending again plays the voiceover (and the music
  bed) up to their natural length -- never past it, never looped.
- Text that ran to the old end runs to the new end; text past the new end is
  clamped or dropped. Callers recompile captions and lanes afterwards from
  their own editor state, so those stay non-destructive across a
  shorten-then-extend.

This mirrors the native editor's preview, where the video track is the
composition clock and the narration plays ``min(video, narration)``.

Everything here is PURE: no I/O, no settings.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from app.kria.recipes import (
    AssetFingerprint,
    MediaAsset,
    MediaSize,
    MediaTransform,
    TimelineClip,
    Transition,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest, VoiceoverRenderAsset
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_narrated_plan import _fit_step_window
from app.pipeline.phone_recipe_shared import (
    EXPORT_SAFETY_MARGIN_S,
    audio_fade,
    display_dims,
    fit_transform,
    landscape_fit_from_recipe,
    refit_source_window,
    timeline_end_s,
)
from app.services.phone_sources import PhoneSourceBinding

# The recipe track that carries each archetype's source clips (the same ids
# `app.services.phone_voiceover_timeline` projects the editor timeline from).
VOICEOVER_VIDEO_TRACK_IDS = {"narrated": "narrated", "voiceover": "montage"}

# Editor transition vocabulary -> recipe transition kind (the native preview's
# own mapping, `NativeEditorRenderCompiler`).
_TRANSITION_KINDS = {"crossfade": "crossfade", "dip_to_black": "fade_black", "flash": "fade_white"}

# Native preview parity (`NativeEditorInteraction.transitionOverlap`): an
# overlap is capped at 0.3 s and at 30% of either clip, and a cap under 0.1 s
# is a hard cut.
_MAX_TRANSITION_OVERLAP_S = 0.3
_TRANSITION_CLIP_FRACTION = 0.3
_MIN_TRANSITION_OVERLAP_S = 0.1

# Below this an audio clip or text layer left by a shorter cut is dropped.
_MIN_REMAINING_S = 0.01

# A text layer counts as "runs to the end" when it ended within this much of
# the old cut's text bound (compilers stop text one export margin short of the
# end; frame-grid rounding moves that by a sub-frame).
_TO_THE_END_TOLERANCE_S = EXPORT_SAFETY_MARGIN_S + 0.05


def transition_overlap_s(left_slot: dict, left_duration_s: float, right_duration_s: float) -> float:
    """Seconds two neighbouring slots overlap; 0 for a hard cut."""
    if str(left_slot.get("transition_after") or "cut") == "cut":
        return 0.0
    requested = left_slot.get("transition_duration_s")
    overlap = min(
        _MAX_TRANSITION_OVERLAP_S,
        float(requested) if requested is not None else _MAX_TRANSITION_OVERLAP_S,
        left_duration_s * _TRANSITION_CLIP_FRACTION,
        right_duration_s * _TRANSITION_CLIP_FRACTION,
    )
    return round(overlap, 3) if overlap >= _MIN_TRANSITION_OVERLAP_S else 0.0


def narrated_slot_window(
    binding: PhoneSourceBinding, in_s: float, duration_s: float
) -> tuple[float, float, float]:
    """``(source_start, source_duration, rate)`` for a narrated slot.

    The narrated compiler's own fit (`_fit_step_window`): trim at rate 1 when
    the footage covers the window, else slow it down to fill it. The native
    preview mirrors it (`NativeNarratedSourceTiming.span`).
    """
    return _fit_step_window(binding, in_s, duration_s)


def _media_asset(binding: PhoneSourceBinding) -> MediaAsset:
    original = binding.original
    return MediaAsset(
        id=binding.render_asset().id,
        relative_path=binding.render_asset().id,
        fingerprint=AssetFingerprint(hex=original.sha256, byte_count=original.byte_count),
        duration=original.duration_s,
        natural_size=MediaSize(width=original.width, height=original.height),
        orientation_degrees=original.orientation_degrees,
        is_proxy_available=True,
    )


def _video_clips(
    previous: Sequence[TimelineClip],
    *,
    slots: Sequence[dict],
    bindings: dict[str, PhoneSourceBinding],
    pool: Sequence[str],
    narrated: bool,
    canvas: Any,
    assets: dict[str, Any],
    manifest: dict[str, Any],
    landscape_fit: str = "fill",
) -> list[TimelineClip]:
    by_id = {clip.id: clip for clip in previous}
    looks = {clip.look for clip in previous}
    default_look = next(iter(looks)) if len(looks) == 1 else None
    clips: list[TimelineClip] = []
    cursor = 0.0
    for index, slot in enumerate(slots):
        clip_index = int(slot["clip_index"])
        if not 0 <= clip_index < len(pool) or pool[clip_index] not in bindings:
            raise UnsupportedPhonePlan("voiceover clip has no phone source binding")
        binding = bindings[pool[clip_index]]
        duration = float(slot["duration_s"])
        if narrated:
            start, source_duration, rate = narrated_slot_window(
                binding, float(slot["in_s"]), duration
            )
            # `phone_voiceover_timeline` reads the step id back out of this shape.
            clip_id = f"step-{index}-{slot['slot_id']}"
        else:
            rate = float(slot.get("playback_rate") or 1.0)
            start, source_duration = float(slot["in_s"]), duration * rate
            original_s = float(binding.original.duration_s)
            if start + source_duration > original_s:
                try:
                    start, source_duration = refit_source_window(start, source_duration, original_s)
                except ValueError as exc:
                    raise UnsupportedPhonePlan("voiceover clip window exceeds its footage") from exc
            clip_id = str(slot["slot_id"])
        asset = binding.render_asset()
        manifest.setdefault(asset.id, asset)
        if asset.id not in assets:
            assets[asset.id] = _media_asset(binding)

        timeline_start = cursor
        transition = None
        if index > 0:
            overlap = transition_overlap_s(
                slots[index - 1], float(slots[index - 1]["duration_s"]), duration
            )
            if overlap > 0:
                transition = Transition(
                    kind=_TRANSITION_KINDS.get(
                        str(slots[index - 1]["transition_after"]), "crossfade"
                    ),
                    duration=overlap,
                )
                timeline_start = cursor - overlap
        update = {
            "id": clip_id,
            "source_asset_id": asset.id,
            "source_start": start,
            "source_duration": source_duration,
            "timeline_start": timeline_start,
            "rate": rate,
            "transition": transition,
        }
        old = by_id.get(clip_id)
        look = old.look if old is not None else default_look
        original = binding.original
        if look == "golden_hour" and (
            (original.width, original.height) != (canvas.width, canvas.height)
            or original.orientation_degrees != 0
        ):
            # The montage compiler's own rule for a newly placed source.
            raise UnsupportedPhonePlan("phone looks require exact-canvas unrotated sources")
        if old is not None:
            clips.append(old.model_copy(update=update))
        else:
            # A newly placed clip keeps the recipe's letterbox (KRI-285): a
            # surviving clip already carries its own transform, a new one must
            # not silently revert to a center-crop beside neighbours with bars.
            transform = MediaTransform()
            if landscape_fit == "fit" and canvas.height > canvas.width:
                display_w, display_h = display_dims(original)
                transform = fit_transform(display_w, display_h, canvas, landscape_fit)
            if default_look is not None and transform != MediaTransform():
                raise UnsupportedPhonePlan(
                    "a color grade cannot be combined with letterboxed landscape fit"
                )
            clips.append(TimelineClip(**update, transform=transform, look=default_look))
        cursor = timeline_start + source_duration / rate
    return clips


def _fit_audio_clips(
    clips: Sequence[TimelineClip],
    *,
    video_end: float,
    assets: dict[str, Any],
    manifest: dict[str, Any],
) -> list[TimelineClip]:
    """Clips of one audio track, ending no later than the video.

    The track's last voiceover or music clip may also grow back up to its
    asset's natural length, so a cut that shortened it once can extend it again.
    """
    ordered = sorted(clips, key=lambda clip: clip.timeline_start)
    fitted: list[TimelineClip] = []
    for position, clip in enumerate(ordered):
        room = (video_end - clip.timeline_start) * clip.rate
        if room <= _MIN_REMAINING_S:
            continue
        length = clip.source_duration
        reference = manifest.get(clip.source_asset_id)
        natural = getattr(assets.get(clip.source_asset_id), "duration", None)
        extendable = (
            position == len(ordered) - 1
            and natural is not None
            and (
                isinstance(reference, VoiceoverRenderAsset)
                or (
                    getattr(reference, "kind", None) == "library"
                    and getattr(reference, "catalog", None) == "music"
                )
            )
        )
        if extendable:
            length = max(length, float(natural) - clip.source_start)
        length = min(length, room)
        if length <= _MIN_REMAINING_S:
            continue
        if abs(length - clip.source_duration) <= 1e-9:
            fitted.append(clip)
            continue
        fade = audio_fade(length / clip.rate)
        fitted.append(
            clip.model_copy(
                update={
                    "source_duration": round(length, 6),
                    "audio_fade_in": None if clip.audio_fade_in is None else fade,
                    "audio_fade_out": None if clip.audio_fade_out is None else fade,
                }
            )
        )
    return fitted


def _fit_text_layers(layers: Sequence[Any], *, old_end: float, new_end: float) -> list[Any]:
    """Text that ran to the old end keeps the same gap to the new end; anything
    else keeps its window, clamped to (or dropped past) the new end."""
    fitted = []
    for layer in layers:
        end = layer.end
        if end >= old_end - _TO_THE_END_TOLERANCE_S:
            end += new_end - old_end
        end = min(end, new_end)
        if end - layer.start <= _MIN_REMAINING_S:
            continue
        fitted.append(layer if end == layer.end else layer.model_copy(update={"end": end}))
    return fitted


def replace_voiceover_cut(
    recipe: EditRecipeV2,
    *,
    archetype: str,
    slots: Sequence[dict],
    bindings: Sequence[PhoneSourceBinding],
    pool: Sequence[str],
    landscape_fit: str | None = None,
) -> EditRecipeV2:
    """``recipe`` with its main video track rebuilt from ``slots`` (KRI-290).

    ``slots`` are the server-resolved active editor slots in timeline order
    (`phone_voiceover_timeline.resolve_phone_voiceover_slots`): ``slot_id``,
    ``clip_index`` into ``pool``, ``in_s``, ``duration_s`` (output seconds),
    ``transition_after`` / ``transition_duration_s`` and, for a montage clip,
    ``playback_rate``. A clip whose id survives keeps every other pinned
    field (its look, for example).

    ``landscape_fit`` (KRI-285): how NEWLY placed clips are framed on a portrait
    montage canvas. ``None`` recovers it from the pinned recipe
    (``landscape_fit_from_recipe``); callers that persisted
    ``variant["landscape_fit"]`` should pass it, since an all-portrait cut
    compiled with ``"fit"`` is indistinguishable from ``"fill"``. Narrated
    recipes never letterbox (KRI-307).

    Raises `UnsupportedPhonePlan` when a slot cannot be built on the phone.
    """
    track_id = VOICEOVER_VIDEO_TRACK_IDS.get(archetype)
    video = next((track for track in recipe.tracks if track.id == track_id), None)
    if video is None or not video.clips:
        raise UnsupportedPhonePlan("pinned voiceover recipe has no video track")
    if not slots:
        raise UnsupportedPhonePlan("a voiceover edit needs at least one clip")
    if (
        recipe.motion_scenes is not None
        or recipe.visual_fills
        or recipe.camera_pulses
        or recipe.audio.mute_windows
    ):
        # None of these reach a phone Voiceover recipe today (the phone pilot
        # validator refuses them); never silently desync one from a new cut.
        raise UnsupportedPhonePlan("voiceover clip edits can't move timed visual effects")

    assets: dict[str, Any] = {asset.id: asset for asset in recipe.assets}
    manifest: dict[str, Any] = {asset.id: asset for asset in recipe.asset_manifest.assets}
    if archetype == "narrated":
        resolved_fit = "fill"
    else:
        resolved_fit = landscape_fit or landscape_fit_from_recipe(recipe)
    clips = _video_clips(
        video.clips,
        slots=slots,
        bindings={binding.proxy_path: binding for binding in bindings},
        pool=pool,
        narrated=archetype == "narrated",
        canvas=recipe.canvas,
        assets=assets,
        manifest=manifest,
        landscape_fit=resolved_fit,
    )
    old_end = timeline_end_s(video.clips)
    new_end = timeline_end_s(clips)

    tracks = []
    for track in recipe.tracks:
        if track is video:
            tracks.append(track.model_copy(update={"clips": clips}))
        elif track.kind == "audio":
            fitted = _fit_audio_clips(
                track.clips, video_end=new_end, assets=assets, manifest=manifest
            )
            if fitted:
                tracks.append(track.model_copy(update={"clips": fitted}))
        else:
            # Other visual tracks (the Visuals lane) are recompiled by the
            # caller from their own editor state; never let one outrun the cut.
            kept = []
            for clip in track.clips:
                room = (new_end - clip.timeline_start) * clip.rate
                if room <= _MIN_REMAINING_S:
                    continue
                if clip.source_duration > room:
                    clip = clip.model_copy(update={"source_duration": round(room, 6)})
                kept.append(clip)
            if kept:
                tracks.append(track.model_copy(update={"clips": kept}))

    still_used = {clip.source_asset_id for track in tracks for clip in track.clips}
    dropped = {clip.source_asset_id for clip in video.clips} - still_used
    assets = {key: value for key, value in assets.items() if key not in dropped}
    manifest = {key: value for key, value in manifest.items() if key not in dropped}

    required = set(recipe.required_capabilities) - {"variableSpeed", "crossfade"}
    if any(clip.rate != 1 for clip in clips):
        required.add("variableSpeed")
    if any(clip.transition is not None for clip in clips):
        required.add("crossfade")

    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    fields.update(
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=tracks,
        text_layers=_fit_text_layers(recipe.text_layers, old_end=old_end, new_end=new_end),
        required_capabilities=required,
    )
    return EditRecipeV2(**fields)
