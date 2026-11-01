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
    NormalizedSourceCrop,
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
    clip_tilt_fields,
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
    per_clip_sound: bool = False,
) -> list[TimelineClip]:
    by_id = {clip.id: clip for clip in previous}
    # A selected source can appear in more than one slot. This map lets a
    # split piece and a later newly-added occurrence keep the source's unit
    # selection marker even when their slot id is new. The explicit slot value
    # below wins when present (the normal Save path supplies it).
    source_audio_by_asset = {clip.source_asset_id: clip.volume for clip in previous}
    source_audio_mode = any(clip.volume != 1.0 for clip in previous) or any(
        "source_audio_volume" in slot for slot in slots
    )
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
        if source_audio_mode:
            raw_volume = slot.get("source_audio_volume")
            if raw_volume is None:
                # A source that was not part of the pinned selection remains
                # muted. This prevents a newly introduced clip from becoming
                # audible simply because it has no predecessor slot.
                volume = source_audio_by_asset.get(asset.id, 0.0)
            else:
                try:
                    volume = float(raw_volume)
                except (TypeError, ValueError) as exc:
                    raise UnsupportedPhonePlan("voiceover source audio volume is invalid") from exc
                if not 0.0 <= volume <= 1.0:
                    raise UnsupportedPhonePlan(
                        "voiceover source audio volume must be between 0 and 1"
                    )
            update["volume"] = volume
        # A creator crop (KRI-44). The slot omits it to keep the pinned one; an
        # explicit None clears it. A cropped clip is cover-fitted from the crop by
        # the device, so it never also carries a letterbox transform.
        if "source_crop" in slot:
            raw_crop = slot["source_crop"]
            update["source_crop"] = (
                NormalizedSourceCrop.model_validate(raw_crop) if raw_crop is not None else None
            )
        crop = (
            update["source_crop"] if "source_crop" in update else (old.source_crop if old else None)
        )
        look = old.look if old is not None else default_look
        original = binding.original
        if look == "golden_hour" and (
            (original.width, original.height) != (canvas.width, canvas.height)
            or original.orientation_degrees != 0
        ):
            # The montage compiler's own rule for a newly placed source.
            raise UnsupportedPhonePlan("phone looks require exact-canvas unrotated sources")
        # KRI-285: a clip's letterbox transform is per SOURCE (its display
        # aspect). A surviving slot id keeps every pinned field, but when the
        # slot is repointed at a different source the old transform is stale
        # (a portrait clip shrunk to 32% on black, or a landscape clip
        # cropped), so recompute it for the new source. A newly placed clip
        # gets the recipe's fit the same way.
        clearing_crop = (
            old is not None
            and old.source_crop is not None
            and "source_crop" in update
            and crop is None
        )
        if old is not None and old.source_asset_id == asset.id and not clearing_crop:
            if crop is not None:
                update["transform"] = MediaTransform(
                    rotation_degrees=old.transform.rotation_degrees
                )
            placed = old.model_copy(update=update)
        else:
            transform = MediaTransform()
            keeps_identity = crop is not None or (old is not None and old.still_layout is not None)
            if landscape_fit == "fit" and canvas.height > canvas.width and not keeps_identity:
                display_w, display_h = display_dims(original)
                transform = fit_transform(display_w, display_h, canvas, landscape_fit)
            if look is not None and transform != MediaTransform():
                raise UnsupportedPhonePlan(
                    "a color grade cannot be combined with letterboxed landscape fit"
                )
            if old is not None:
                # A repointed slot is a different picture: it never inherits the old
                # source's crop unless the slot sends one (KRI-44).
                update.setdefault("source_crop", None)
                placed = old.model_copy(update={**update, "transform": transform})
            else:
                placed = TimelineClip(**update, transform=transform, look=default_look)
        clips.append(_with_tilt(placed, slot, old))
        cursor = timeline_start + source_duration / rate
    if per_clip_sound:
        clips = _keep_clip_sound(previous, clips, slots)
    return clips


def _with_tilt(placed: TimelineClip, slot: dict, old: TimelineClip | None) -> TimelineClip:
    """``placed`` carrying the slot's tilt (KRI-1136).

    A slot that sends ``tilt`` owns it (``None`` straightens). One that omits it keeps the
    pinned clip's tilt while it plays the same footage; a repointed or new slot starts
    upright, like a crop.
    """
    if "tilt" in slot:
        patch, backdrop = clip_tilt_fields(slot)
        rotation = patch.get("rotation_degrees", 0.0)
    elif old is not None and old.source_asset_id == placed.source_asset_id:
        rotation, backdrop = old.transform.rotation_degrees, old.backdrop
    else:
        rotation, backdrop = 0.0, None
    if placed.still_layout is not None:
        backdrop = None
    if (rotation, backdrop) == (placed.transform.rotation_degrees, placed.backdrop):
        return placed
    if placed.look is not None and (rotation or backdrop is not None):
        raise UnsupportedPhonePlan("a color grade cannot be combined with a tilted clip")
    return placed.model_copy(
        update={
            "transform": placed.transform.model_copy(update={"rotation_degrees": rotation}),
            "backdrop": backdrop,
        }
    )


def _step_slot_id(clip: TimelineClip) -> str | None:
    """The slot a narrated clip id (``step-<index>-<slot id>``) names."""
    head, _, rest = clip.id.partition("-")
    index, _, slot_id = rest.partition("-")
    return slot_id if head == "step" and index.isdigit() and slot_id else None


def _keep_clip_sound(
    previous: Sequence[TimelineClip], clips: list[TimelineClip], slots: Sequence[dict]
) -> list[TimelineClip]:
    """``clips`` with each clip's own-sound gain kept by the slot it plays (KRI-842).

    A recipe with per-clip sound (`compile_phone_narrated_plan`: ``original_volume`` 1.0 and
    each clip's ``volume`` the gain) must not lose it when a cut moves a slot to another index
    (the clip id carries the index), repoints it at other footage, or adds a slot: a gain is
    the slot's only while it still plays the same footage; anything else is silent, exactly
    like every clip the creator did not ask to hear.
    """
    prior = {slot_id: clip for clip in previous if (slot_id := _step_slot_id(clip)) is not None}
    kept = []
    for clip, slot in zip(clips, slots, strict=True):
        before = prior.get(str(slot["slot_id"]))
        same = before is not None and before.source_asset_id == clip.source_asset_id
        kept.append(clip.model_copy(update={"volume": before.volume if same else 0.0}))
    return kept


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
        per_clip_sound=(
            archetype == "narrated"
            and recipe.audio.original_volume == 1.0
            and any(clip.volume != 1 for clip in video.clips)
        ),
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

    required = set(recipe.required_capabilities) - {"variableSpeed", "crossfade", "clipTilt"}
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
