"""Shared building blocks for phone-recipe compilers (KRI-114 P1-2/P1-3).

`app.pipeline.phone_guided_plan` was the first phone compiler
(`compile_phone_guided_plan`) and keeps its own inline copies of the
export-safety-margin refit math and its rounding tolerance -- those stay
untouched (byte-identical behavior) since that logic is already load-bearing
and verified; only the float-noise text snap below is shared with it. This
module exists so the SECOND
compiler (`app.pipeline.phone_voiceover_montage_plan`, for the montage/day_vlog/
single_hero archetypes) doesn't have to reinvent that math, and so a THIRD
compiler (voiceover/subtitled/etc., future phases) has somewhere to import it
from instead of copy-pasting again.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.kria.clip_tilt import MAX_ZOOM, MIN_ZOOM
from app.kria.clip_tilt_row import parse_row_tilt
from app.kria.portable_drawing import compile_drawing_layers
from app.kria.recipes import (
    AssetFingerprint,
    Canvas,
    ClipBackdrop,
    MediaAsset,
    MediaSize,
    MediaTransform,
    TimelineClip,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest, RenderFingerprint

# Mirrors `phone_guided_plan._EXPORT_SAFETY_MARGIN_S` -- see that module's
# docstring for the full AVFoundation-boundary rationale. Never fit a refit
# window exactly to the device-measured duration.
EXPORT_SAFETY_MARGIN_S = 0.05

# KRI-479: a continuous voice cut on a sentence end may stop up to this long before the
# picture ends. At most 3 s and at most 15 % of the picture (a 6 s edit may not lose 3 s of
# voice). The composer (`phone_speech_montage_plan`) and the verifier
# (`creator_render_contract.verify_phone_recipe`, `voice_covers_timeline`) share this one rule.
VOICE_TAIL_SLACK_S = 3.0
VOICE_TAIL_SLACK_FRACTION = 0.15


def voice_tail_slack_s(picture_s: float) -> float:
    return min(VOICE_TAIL_SLACK_S, VOICE_TAIL_SLACK_FRACTION * max(0.0, float(picture_s)))


# Mirrors `phone_guided_plan._TIMING_ROUNDING_TOLERANCE_S` -- two
# independently millisecond-rounded timing quantities can legitimately differ
# by a couple of milliseconds without that being a real timing-program
# mismatch.
TIMING_ROUNDING_TOLERANCE_S = 0.005

# Mirrors the cloud's `afade=t=out:d=0.5` on a recorded voiceover
# (`_mix_user_voiceover`) and on a song bed (`_mix_template_audio`); the phone
# expresses it as a per-clip `TimelineClip.audio_fade_out` (KRI-139).
PHONE_AUDIO_FADE_S = 0.5

# Mirrors `app.tasks.template_orchestrate._NARRATED_FOOTAGE_BED_MAX_GAIN`: the
# resting level of a side-chain ducked footage bed. Only applied together with
# the native duck -- the flat (un-ducked) approximation keeps its shipped gain.
NARRATED_FOOTAGE_BED_MAX_GAIN = 0.6


def audio_fade(clip_duration_s: float) -> float:
    """`PHONE_AUDIO_FADE_S`, shortened so a fade never covers half a short clip."""
    return round(max(0.0, min(PHONE_AUDIO_FADE_S, clip_duration_s / 2)), 6)


def fit_transform(
    display_w: int, display_h: int, canvas: Canvas, landscape_fit: str
) -> MediaTransform:
    """Main-track ``MediaTransform`` that letterboxes a landscape source.

    The phone engine cover-fills every main-track clip into the canvas, then
    applies ``transform.scale`` about the canvas center over a black base. A
    landscape clip (``display_w > display_h``) with ``landscape_fit == "fit"``
    therefore needs ``scale = contain / cover`` to end up fully visible with
    bars -- 0.31640625 for 1920x1080 into 1080x1920. Everything else (portrait,
    square, ``"fill"``) is the identity transform, i.e. the engine's native
    cover-fill/center-crop, which keeps those recipes byte-identical.

    Mirrors the cloud's ``resolve_output_fit`` (``reframe.py``) fit semantics.
    """
    if landscape_fit != "fit" or display_w <= display_h:
        return MediaTransform()
    contain = min(canvas.width / display_w, canvas.height / display_h)
    cover = max(canvas.width / display_w, canvas.height / display_h)
    return MediaTransform(scale=contain / cover)


def max_cover_shift_px(display_w: float, display_h: float, canvas: Canvas) -> float:
    """How far (canvas px) the engine's cover-filled picture can slide sideways
    before a black edge shows: half the width the cover fill overflows by."""
    if display_w <= 0 or display_h <= 0:
        return 0.0
    cover = max(canvas.width / display_w, canvas.height / display_h)
    return max(0.0, (display_w * cover - canvas.width) / 2)


def face_fill_transform(
    display_w: float, display_h: float, canvas: Canvas, position_x: float
) -> MediaTransform:
    """Main-track ``MediaTransform`` for a face-filled crop (KRI-547).

    The engine cover-fills the clip centred, applies ``scale`` (identity here),
    then moves it ``position_x`` canvas pixels to the right
    (`KriaMediaEngine` ``Composition.transform``). ``position_x`` is clamped so
    the shifted picture always covers the canvas; a clip with no sideways
    overflow (portrait/square on a portrait canvas) gets the identity.
    """
    limit = max_cover_shift_px(display_w, display_h, canvas)
    clamped = max(-limit, min(limit, float(position_x)))
    # Hundredths of a pixel, rounded toward the centre so the clamp still holds.
    shift = math.copysign(math.floor(abs(clamped) * 100) / 100, clamped) + 0.0
    if shift == 0:
        return MediaTransform()
    return MediaTransform(position_x=shift)


def display_dims(original: Any) -> tuple[int, int]:
    """(width, height) as actually DISPLAYED once `orientation_degrees` is
    applied -- a 1080x1920-pixel file flagged 90/270 degrees is landscape on
    screen despite carrying portrait pixel dimensions (and vice versa).

    ``original`` is anything with ``width``/``height``/``orientation_degrees``
    (``OriginalMediaDescriptor``, a Visuals-pool binding, ...). Mirrors the
    golden-hour exact-canvas checks in the montage/guided compilers, which
    reason about the same rotation flag.
    """
    if int(original.orientation_degrees) % 360 in (90, 270):
        return original.height, original.width
    return original.width, original.height


# Main (cover-filled) video tracks whose clips may be letterboxed with
# ``fit_transform``: the subtitled speaker, the voiceover-montage track and the
# guided/unified-montage "story" track. Narrated / speech-montage / authored
# tracks are intentionally absent (KRI-307: they ignore ``landscape_fit``).
FIT_VIDEO_TRACK_IDS: frozenset[str] = frozenset({"subtitled", "montage", "story"})


def _recipe_clip_dims(
    recipe_assets: Mapping[str, Any], asset_id: str
) -> tuple[float, float] | None:
    """Display (width, height) of a recipe asset, or None for one with no natural size."""
    asset = recipe_assets.get(asset_id)
    if asset is None or asset.natural_size is None:
        return None
    size = asset.natural_size
    if int(asset.orientation_degrees) % 360 in (90, 270):
        return size.height, size.width
    return size.width, size.height


def letterbox_scale(dims: tuple[float, float] | None, canvas: Canvas) -> float:
    """The ``transform.scale`` ``"fit"`` gives a source of these display dims: 1 unless it is a
    landscape source on a portrait canvas (or its dims are unknown)."""
    if dims is None or canvas.height <= canvas.width:
        return 1.0
    return fit_transform(dims[0], dims[1], canvas, "fit").scale


def split_clip_scale(scale: float, letterbox: float) -> tuple[float, float]:
    """``(framing scale, tilt zoom)`` whose product is a clip's ``transform.scale`` (KRI-1169).

    A tilt zoom (0.5..3, hundredths) rides ``transform.scale`` on top of the clip's own
    framing scale, which is ``letterbox`` or 1. Only a zoom that is a clean hundredth over one
    of the two framings is read as one, so a letterboxed clip and a zoomed one never mix up.
    A scale equal to either framing is read as unzoomed first (every recipe from before the
    zoom, and the rare source whose letterbox scale times a zoom equals 1, stay as they were).
    A scale that fits neither is a plain framing scale with no zoom.
    """
    if scale == 1.0 or scale == letterbox:
        return scale, 1.0
    for base in dict.fromkeys((letterbox, 1.0)):
        zoom = scale / base
        if MIN_ZOOM <= round(zoom, 6) <= MAX_ZOOM and abs(zoom * 100 - round(zoom * 100)) < 1e-4:
            return base, round(zoom, 2)
    return scale, 1.0


def clip_tilt_zoom(recipe: EditRecipeV2, clip: TimelineClip) -> float:
    """The tilt zoom a recipe clip carries: its ``transform.scale`` over its letterbox scale."""
    dims = _recipe_clip_dims({asset.id: asset for asset in recipe.assets}, clip.source_asset_id)
    return split_clip_scale(clip.transform.scale, letterbox_scale(dims, recipe.canvas))[1]


def landscape_fit_from_recipe(recipe: EditRecipeV2) -> Literal["fill", "fit"]:
    """Inverse of the compilers' ``landscape_fit`` -> main-track transform
    projection (KRI-283, KRI-285): ``"fit"`` when ANY clip of a fit-capable
    main video track (``FIT_VIDEO_TRACK_IDS``) is letterboxed, else
    ``"fill"``. Lets an editor Save / re-cut re-derive the letterbox from the
    previously pinned recipe, like ``keep_segments``.

    A tilt zoom also lives in ``transform.scale`` (KRI-1169), so a clip counts as
    letterboxed only when its source is landscape on a portrait canvas and its scale is
    the letterbox scale (times a zoom); a source with no known size falls back to
    ``scale < 1``.

    Caveat: a recipe whose clips are ALL portrait/square compiled with
    ``"fit"`` is indistinguishable from ``"fill"`` (every transform is the
    identity), so callers that persist ``variant["landscape_fit"]`` should
    prefer it over this inference.
    """
    recipe_assets = {asset.id: asset for asset in recipe.assets}
    for track in recipe.tracks:
        if track.kind == "video" and track.id in FIT_VIDEO_TRACK_IDS:
            for clip in track.clips:
                dims = _recipe_clip_dims(recipe_assets, clip.source_asset_id)
                if dims is None:
                    if clip.transform.scale < 1:
                        return "fit"
                    continue
                letterbox = letterbox_scale(dims, recipe.canvas)
                if letterbox < 1 and split_clip_scale(clip.transform.scale, letterbox)[0] == (
                    letterbox
                ):
                    return "fit"
    return "fill"


def clip_tilt_fields(
    row: Any, *, fit_scale: float = 1.0
) -> tuple[dict[str, float], ClipBackdrop | None]:
    """What a row's ``tilt`` (KRI-1136) adds to its clip: ``(transform_patch, backdrop)``.

    ``row`` is the editor row (a mapping) or anything with a ``tilt`` attribute. The patch
    holds ``rotation_degrees`` (clockwise, the recipe's own sign) only when the clip is
    turned, and ``backdrop`` is ``None`` for a blank one, so an untilted clip compiles to
    exactly what it did before. A zoom (KRI-1169) is ``scale`` in the patch: the clip's own
    framing scale, ``fit_scale`` (its letterbox or face-fill scale, 1 by default), times the
    zoom, so it composes with a letterbox instead of replacing it. Present only while the zoom
    is not 1. Every phone writer applies this; none reads ``tilt`` itself.
    """
    raw = row.get("tilt") if isinstance(row, Mapping) else getattr(row, "tilt", None)
    tilt = parse_row_tilt(raw)
    if tilt is None:
        return {}, None
    patch = {"rotation_degrees": tilt.rotation_deg} if tilt.rotation_deg != 0 else {}
    if tilt.zoom != 1:
        patch["scale"] = fit_scale * tilt.zoom
    return patch, tilt.backdrop


def backdrop_asset_ids(clips: Iterable[TimelineClip]) -> set[str]:
    """The manifest ids (``visual-<media id>``) of every media backdrop the ``clips`` draw."""
    return {
        f"visual-{clip.backdrop.asset_id}"
        for clip in clips
        if clip.backdrop is not None and clip.backdrop.kind == "media"
    }


def register_backdrop_visuals(
    clips: Iterable[TimelineClip],
    *,
    visuals: Sequence[Any],
    assets: dict[str, Any],
    manifest: dict[str, Any],
) -> None:
    """Declare the Visuals-pool photo or video each media backdrop names (KRI-1139).

    The device draws a backdrop from a recipe asset it downloads like any pool visual, so the
    pinned ``PhoneVisualBinding`` becomes a ``VisualRenderAsset`` in the manifest and a
    ``MediaAsset`` beside the clips. A backdrop whose visual the job never pinned fails the
    compile closed (the Save pins it first), never a recipe the device cannot read. The
    reject names the clip but is never degraded: the guided worker binds only the visuals its
    timeline shows (``phone_visuals._timeline_visuals``), so a missing one here can be a
    background the creator did pin.
    """
    from app.pipeline.phone_guided_plan import UnsupportedPhonePlan  # noqa: PLC0415

    for clip in clips:
        backdrop = clip.backdrop
        if backdrop is None or backdrop.kind != "media":
            continue
        visual = next((value for value in visuals if value.media_id == backdrop.asset_id), None)
        if visual is None:
            raise UnsupportedPhonePlan(
                "a media backdrop needs a photo or video pinned to this video",
                capability="clipBackdropMedia",
                reason="backdrop_unbound",
                lane="moment",
                item_id=clip.id,
            )
        asset = visual.render_asset()
        manifest[asset.id] = asset
        assets[asset.id] = MediaAsset(
            id=asset.id,
            relative_path=asset.id,
            fingerprint=AssetFingerprint(hex=visual.sha256, byte_count=visual.byte_count),
            duration=(
                visual.duration_s
                if visual.kind == "video" and (visual.duration_s or 0) <= 1800
                else None
            ),
            natural_size=(
                None
                if visual.kind == "image"
                else MediaSize(width=visual.width or 1, height=visual.height or 1)
            ),
            orientation_degrees=visual.orientation_degrees,
        )


def _fit_eligible(clip: TimelineClip) -> bool:
    """A main-track clip whose transform is ours to set: a plain cover-filled
    video clip (no look -- the device throws on look + transform, no re-frame
    crop, no still card/hold) that still carries the compilers' centered
    transform. A tilt (``rotation_degrees``) does not disqualify it: the scale
    and the rotation compose, so a tilted sideways clip keeps its letterbox."""
    return (
        clip.look is None
        and clip.source_crop is None
        and clip.still_layout is None
        and clip.hold_duration is None
        and clip.transform.position_x == 0
        and clip.transform.position_y == 0
    )


def apply_landscape_fit(
    recipe: EditRecipeV2,
    bindings: Sequence[Any],
    fit: Literal["fill", "fit"],
) -> EditRecipeV2:
    """``recipe`` with its main-track clips (re)letterboxed for ``fit``.

    PURE and idempotent: every eligible clip's ``transform.scale`` is recomputed
    from scratch with ``fit_transform``, so ``fit -> fill -> fit`` round-trips
    and calling it twice changes nothing. ``"fill"`` restores the engine's
    native cover-fill/center-crop (identity transform).

    - Only clips of ``FIT_VIDEO_TRACK_IDS`` video tracks are touched. Overlay,
      audio, Visuals, cutaway and ending tracks are never modified.
    - Skipped (left as-is): clips with a ``look`` (golden_hour -- the device
      throws on a look combined with a transform), a ``source_crop``, a still
      card/hold, a non-centered transform, or whose source dimensions
      are unknown (stills carry no ``natural_size``).
    - A landscape (non-portrait) output canvas is returned unchanged: the
      cloud's landscape output always crops (``resolve_output_fit``).
    - Source dimensions come from ``bindings`` (``PhoneSourceBinding`` with an
      ``original`` descriptor, matched by ``media_id`` == asset id) and fall
      back to the recipe's own ``MediaAsset.natural_size``/orientation.
    - A tilt zoom (KRI-1169) is part of ``transform.scale``: the clip keeps its zoom and only
      the letterbox part of the scale changes (``split_clip_scale``).
    - Returns ``recipe`` itself when nothing changes (byte-identical).
    """
    canvas = recipe.canvas
    if canvas.height <= canvas.width:
        return recipe
    originals = {
        binding.media_id: binding.original for binding in bindings if hasattr(binding, "original")
    }
    recipe_assets = {asset.id: asset for asset in recipe.assets}

    def _dims(asset_id: str) -> tuple[float, float] | None:
        original = originals.get(asset_id)
        if original is not None and original.width and original.height:
            return display_dims(original)
        return _recipe_clip_dims(recipe_assets, asset_id)

    changed = False
    tracks = []
    for track in recipe.tracks:
        if track.kind != "video" or track.id not in FIT_VIDEO_TRACK_IDS:
            tracks.append(track)
            continue
        clips = []
        for clip in track.clips:
            dims = _dims(clip.source_asset_id) if _fit_eligible(clip) else None
            if dims is None:
                clips.append(clip)
                continue
            scale = fit_transform(dims[0], dims[1], canvas, fit).scale
            _, zoom = split_clip_scale(clip.transform.scale, letterbox_scale(dims, canvas))
            if zoom != 1:
                scale *= zoom
            if scale == clip.transform.scale:
                clips.append(clip)
                continue
            changed = True
            clips.append(
                clip.model_copy(
                    update={"transform": clip.transform.model_copy(update={"scale": scale})}
                )
            )
        tracks.append(track.model_copy(update={"clips": clips}))
    if not changed:
        return recipe
    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    fields.update(
        tracks=tracks,
        asset_manifest=RenderAssetManifest(assets=tuple(recipe.asset_manifest.assets)),
    )
    return EditRecipeV2(**fields)


def compile_color_moments(stored: dict | None, *, timeline_duration_s: float):
    """The variant's stored ``color_moments`` lane as recipe values, clamped to the timeline.

    ``stored`` is ``{"moments": [...], "end_fade": {"duration": s} | None}`` (KRI-1350), written
    only by the Kria chat op. Tolerant like `compile_drawing_layers`: a moment a shorter cut
    pushed past the end is shortened, or dropped once it would last under the minimum; an end
    fade longer than the video is dropped. Returns ``(moments, end_fade)``.
    """
    from app.kria.portable_color_moments import (  # noqa: PLC0415
        END_FADE_RANGE_S,
        MAX_COLOR_MOMENTS,
        MIN_COLOR_MOMENT_S,
        ColorMoment,
        EndFade,
    )

    if not isinstance(stored, dict):
        return [], None
    moments: list[ColorMoment] = []
    for raw in stored.get("moments") or []:
        try:
            moment = ColorMoment.model_validate(raw)
        except ValueError:
            continue
        end = min(moment.end, timeline_duration_s)
        if end - moment.start < MIN_COLOR_MOMENT_S - 1e-9:
            continue
        if moments and moment.start < moments[-1].end - 1e-6:
            continue
        moments.append(moment.model_copy(update={"end": end}) if end != moment.end else moment)
    moments = moments[:MAX_COLOR_MOMENTS]
    end_fade = None
    raw_fade = stored.get("end_fade")
    if isinstance(raw_fade, dict):
        try:
            candidate = EndFade.model_validate(raw_fade)
        except ValueError:
            candidate = None
        if (
            candidate is not None
            and END_FADE_RANGE_S[0] <= timeline_duration_s
            and (candidate.duration <= timeline_duration_s)
        ):
            end_fade = candidate
    return moments, end_fade


def with_color_moments(recipe: EditRecipeV2, stored: dict | None) -> EditRecipeV2:
    """``recipe`` carrying exactly the colour moments the variant's stored lane compiles to.

    Run on every phone editor Save right after `with_drawings`, from the variant's STORED
    ``color_moments`` (KRI-1350), whichever sections the Save carried: the creator's own phone
    Saves never send this lane, so it rides along untouched. ``None`` strips them (and the
    ``colorMoments`` capability), which a patching compiler does BEFORE it changes the
    duration. Returns ``recipe`` itself when nothing changes, so a recipe without colour
    moments stays byte-identical.
    """
    moments, end_fade = compile_color_moments(stored, timeline_duration_s=recipe.duration)
    if not moments and end_fade is None and not recipe.color_moments and recipe.end_fade is None:
        return recipe
    if [m.model_dump(mode="json") for m in moments] == [
        m.model_dump(mode="json") for m in recipe.color_moments
    ] and (end_fade.model_dump() if end_fade else None) == (
        recipe.end_fade.model_dump() if recipe.end_fade else None
    ):
        return recipe
    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    fields.update(
        color_moments=moments,
        end_fade=end_fade,
        required_capabilities=set(recipe.required_capabilities) - {"colorMoments"},
    )
    return EditRecipeV2(**fields)


def with_drawings(recipe: EditRecipeV2, drawings: list[dict] | None) -> EditRecipeV2:
    """``recipe`` carrying exactly the freehand drawing layers ``drawings`` compile to (KRI-589).

    The server's one entry point to `compile_drawing_layers`: every phone editor
    Save runs it on the recipe it just built or patched, from the variant's STORED
    ``drawings`` lane (clamped to this recipe's own duration), whichever sections the
    Save carried. A cut that shortened the video therefore drops or shrinks the
    drawings it pushed past the end, and a longer cut brings them back.

    ``None``/``[]`` strips any drawing layers (and the ``freehandDrawing``
    capability); it is also how a patching compiler clears the previous recipe's
    layers BEFORE it changes the duration, since the recipe validator rejects a
    layer past the end. Returns ``recipe`` itself when nothing changes, so a
    recipe without drawings stays byte-identical.
    """
    # The contract compile is tolerant: a window that no longer fits is dropped or clamped.
    layers = compile_drawing_layers(drawings, timeline_duration_s=recipe.duration)
    if not layers and not recipe.drawing_layers:
        return recipe
    if [layer.model_dump(mode="json") for layer in layers] == [
        layer.model_dump(mode="json") for layer in recipe.drawing_layers
    ]:
        return recipe
    fields = {name: getattr(recipe, name) for name in type(recipe).model_fields}
    fields.update(
        drawing_layers=layers,
        required_capabilities=set(recipe.required_capabilities) - {"freehandDrawing"},
    )
    return EditRecipeV2(**fields)


def snap_text_overshoot(layers: Iterable[Any], timeline_end_s: float) -> None:
    """Snap a text layer that overshoots the timeline by float noise back onto it.

    ``EditRecipeV2`` (and the Swift twin) reject ``layer.end > duration`` with a
    strict compare, so a layer that ends at a plan's nominal end can trip it when
    the summed millisecond-rounded cuts (or a ``usable / (usable / target)`` slow-
    down) land 1 ULP short: 23.531 + 1.467 == 24.997999999999998 < 24.998 (KRI-190
    device test, job 5df2e3ec). Only an overshoot of at most
    ``TIMING_ROUNDING_TOLERANCE_S`` moves, so every layer that already fits stays
    byte-identical and a real overrun is still rejected by the recipe validator.
    """
    for layer in layers:
        if 0 < layer.end - timeline_end_s <= TIMING_ROUNDING_TOLERANCE_S:
            layer.end = timeline_end_s


def timeline_end_s(clips: Iterable[Any]) -> float:
    """Where these clips end: ``EditRecipeV2.duration``'s formula over ``clips``."""
    return max(
        (
            clip.timeline_start + clip.source_duration / clip.rate + (clip.hold_duration or 0)
            for clip in clips
        ),
        default=0.0,
    )


def refit_source_window(
    source_start: float, source_duration: float, original_duration_s: float
) -> tuple[float, float]:
    """Refit a proxy-measured ``[source_start, source_start + source_duration)``
    window into what the device's own original file actually measures.

    The proxy is measured by server-side ffprobe; the original by the
    client's on-device measurement of conceptually the same file -- the two
    routinely disagree by a small amount. Prefer to keep the full requested
    duration by shifting the start rather than truncating it; when that still
    doesn't fit, truncate to whatever's available minus the export safety
    margin. Raises ``ValueError`` when essentially nothing usable remains.
    """
    available = max(0.0, original_duration_s - EXPORT_SAFETY_MARGIN_S)
    fitted_duration = min(source_duration, available)
    if fitted_duration < 0.1:
        raise ValueError("insufficient source duration remains after the on-device refit")
    start = min(source_start, max(0.0, available - fitted_duration))
    return start, round(fitted_duration, 6)


class PhoneMusicBed(BaseModel):
    """A resolved, immutable music-catalog receipt ready to compile into a
    phone recipe's audio track.

    Built by `app.tasks.generative_build._resolve_phone_music_bed`, which
    bridges the sync Celery worker to the (async-shaped) catalog lookup
    `app.services.render_library.catalog_path` normally uses -- there is no
    running event loop in a Celery worker, so it re-validates the same
    publish/ready/path-prefix contract directly against a sync DB session
    instead of calling that async function. `inspect_library_asset` (already
    sync) then pins the exact generation + fingerprint. Consumed by
    `compile_phone_voiceover_montage_plan`, which never touches the database or GCS
    itself -- it only reads this already-verified value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    catalog_id: str = Field(min_length=1, max_length=160)
    generation: str = Field(min_length=1, max_length=160)
    fingerprint: RenderFingerprint
    duration_s: float | None = Field(default=None, gt=0, le=1800)
    start_s: float = Field(default=0.0, ge=0)
    volume: float = Field(default=1.0, ge=0, le=2)


class PhoneNarrationBed(BaseModel):
    """A resolved, immutable voiceover receipt ready to compile into a phone
    recipe's narration audio track (KRI-132).

    Built by `app.tasks.generative_build._resolve_phone_voiceover_bed`, which
    re-reads the owning `PlanItem`'s current `voiceover_gcs_path`/
    `voiceover_generation` in a sync session, then calls
    `app.services.phone_voiceover.inspect_voiceover_asset` (mirrors
    `inspect_library_asset`'s pin-then-hash pattern) to pin the exact
    generation + fingerprint at compile time. Unlike the library catalog
    grant, `app.routes.device_render.download_device_asset` does NOT re-hash
    this asset on every device fetch -- it only re-checks the job's own
    `PlanItem` still carries this exact `(path, generation)` before signing;
    the DEVICE re-hashes the downloaded bytes against the pinned SHA-256
    itself. Unlike `PhoneMusicBed`'s shared catalog, this is private creator
    media addressed by plan item, not catalog id -- see
    `app.kria.render_assets.VoiceoverRenderAsset`.

    Consumed by `compile_phone_voiceover_montage_plan`, which never touches the
    database or GCS itself -- it only reads this already-verified value.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_item_id: str = Field(min_length=1, max_length=160)
    generation: str = Field(min_length=1, max_length=160)
    fingerprint: RenderFingerprint
    # The server-verified duration for the captured generation (`PlanItem
    # .voiceover_duration_s`, probed once at registration -- the generation
    # is immutable so that probe cannot drift from these exact bytes).
    duration_s: float = Field(gt=0, le=1800)


class PhoneSongBed(BaseModel):
    """A resolved, immutable creator-song receipt ready to compile into a phone
    recipe's song audio track (KRI-374).

    Catalog-free: the song is private creator media addressed by plan item (see
    `app.kria.render_assets.SongRenderAsset`). Built by the worker's
    `_resolve_phone_song_bed`, which re-reads the owning `PlanItem`'s CURRENT
    `song_gcs_path`/`song_generation` and pins the exact generation +
    fingerprint; the compiler then only reads this already-verified value and
    re-checks it against the plan's own pinned `UserSongPlan`.

    ``window_start_s`` / the fades are optional overrides: omitted, the compiler
    uses the approved plan's window and the mode's default fades (background
    0.5 s in/out; lip-sync 0.05 s in, 0.3 s out).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    plan_item_id: str = Field(min_length=1, max_length=160)
    generation: str = Field(min_length=1, max_length=160)
    fingerprint: RenderFingerprint
    # The server-verified duration of the whole uploaded song.
    duration_s: float = Field(gt=0, le=1800)
    window_start_s: float | None = Field(default=None, ge=0)
    volume: float = Field(default=1.0, ge=0, le=2)
    fade_in_s: float | None = Field(default=None, ge=0, le=60)
    fade_out_s: float | None = Field(default=None, ge=0, le=60)
