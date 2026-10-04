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

from collections.abc import Iterable, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.kria.recipes import Canvas, MediaTransform, TimelineClip
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest, RenderFingerprint

# Mirrors `phone_guided_plan._EXPORT_SAFETY_MARGIN_S` -- see that module's
# docstring for the full AVFoundation-boundary rationale. Never fit a refit
# window exactly to the device-measured duration.
EXPORT_SAFETY_MARGIN_S = 0.05

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


def landscape_fit_from_recipe(recipe: EditRecipeV2) -> Literal["fill", "fit"]:
    """Inverse of the compilers' ``landscape_fit`` -> main-track transform
    projection (KRI-283, KRI-285): ``"fit"`` when ANY clip of a fit-capable
    main video track (``FIT_VIDEO_TRACK_IDS``) is scaled below 1, else
    ``"fill"``. Lets an editor Save / re-cut re-derive the letterbox from the
    previously pinned recipe, like ``keep_segments``.

    Caveat: a recipe whose clips are ALL portrait/square compiled with
    ``"fit"`` is indistinguishable from ``"fill"`` (every transform is the
    identity), so callers that persist ``variant["landscape_fit"]`` should
    prefer it over this inference.
    """
    for track in recipe.tracks:
        if track.kind == "video" and track.id in FIT_VIDEO_TRACK_IDS:
            if any(clip.transform.scale < 1 for clip in track.clips):
                return "fit"
    return "fill"


def _fit_eligible(clip: TimelineClip) -> bool:
    """A main-track clip whose transform is ours to set: a plain cover-filled
    video clip (no look -- the device throws on look + transform, no re-frame
    crop, no still card/hold) that still carries the compilers' centered,
    unrotated transform."""
    return (
        clip.look is None
        and clip.source_crop is None
        and clip.still_layout is None
        and clip.hold_duration is None
        and clip.transform.rotation_degrees == 0
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
      card/hold, a non-centered/rotated transform, or whose source dimensions
      are unknown (stills carry no ``natural_size``).
    - A landscape (non-portrait) output canvas is returned unchanged: the
      cloud's landscape output always crops (``resolve_output_fit``).
    - Source dimensions come from ``bindings`` (``PhoneSourceBinding`` with an
      ``original`` descriptor, matched by ``media_id`` == asset id) and fall
      back to the recipe's own ``MediaAsset.natural_size``/orientation.
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
        asset = recipe_assets.get(asset_id)
        if asset is None or asset.natural_size is None:
            return None
        size = asset.natural_size
        if int(asset.orientation_degrees) % 360 in (90, 270):
            return size.height, size.width
        return size.width, size.height

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
