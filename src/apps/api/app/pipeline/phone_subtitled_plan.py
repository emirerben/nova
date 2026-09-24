"""Project the single-clip "Talking to camera" (subtitled) edit format into a
native device render program (KRI-132), optionally carrying overlay
sticker/photo/video cards, a sound-effects track, and a muted ending clip
(KRI-174 Phase 1 -- see `app.pipeline.phone_subtitled_lanes`; video overlay
cards are KRI-183).

Companion to `app.pipeline.phone_montage_plan.compile_phone_montage_plan` and
`app.pipeline.phone_guided_plan.compile_phone_guided_plan` -- see those
modules' docstrings for the general contract every phone compiler follows:
no media is downloaded or rendered here, and unsupported lanes fail closed
with `UnsupportedPhonePlan` until their native implementation and parity
fixtures exist (see docs/runbooks/phone-rendering.md).

Mirrors the cloud lean path, `_render_subtitled_variant` in
`app.tasks.generative_build` (~generative_build.py:20462-20780): edit_format
``subtitled`` is exactly ONE clip, rendered 1:1 (source_start=0, full
duration, rate=1.0 -- no trim, no speed change, no reframe crop) keeping its
OWN audio at full volume (no music bed, no narration track), with
Whisper-derived caption cues burned as `text_layers`
(`app.pipeline.phone_captions.compile_caption_layers`) instead of an ASS
file. The cloud path additionally reframes the clip to 9:16 via
`reframe_and_export`/`resolve_output_fit`, which can letterbox or center-crop
a non-portrait source; the phone engine only aspect-fills (center-crops) with
no face tracking, so this compiler rejects a non-portrait source clip
outright rather than silently cropping the speaker out of frame -- a v1
limitation, not a permanently-gated capability (hence `semanticCamera`, a
named-but-unimplemented `MediaCapability`, rather than leaving it bare).

With ``lanes=None`` (or an all-empty `PhoneSubtitledLanes`) and
``visuals=()``, this compiler's output is byte-identical to the pre-KRI-174
shape -- every lane below is strictly additive and opt-in.
"""

from __future__ import annotations

from app.kria.portable_visual import VisualMediaPlacement
from app.kria.recipes import (
    AssetFingerprint,
    AudioMixRecipe,
    Canvas,
    MediaAsset,
    MediaSize,
    TimelineClip,
    TimelineTrack,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest
from app.pipeline.phone_captions import caption_font_assets, compile_caption_layers
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_subtitled_lanes import (
    CAPTION_BAND_TOP_FRAC,
    PhoneSubtitledLanes,
    ResolvedSoundEffect,
    SubtitledEndingClip,
    SubtitledOverlayCard,
    _lane_error,
)
from app.services.phone_sources import (
    PhoneSourceBinding,
    PhoneVisualBinding,
    require_bound_visual,
)

# Mirrors `_render_subtitled_variant`'s own cap (generative_build.py, the
# `probe.duration_s > 300.0` check ~line 20690): "subtitled clips are capped
# at 5 minutes". Named up front rather than surfacing as a bare recipe
# validation error.
_MAX_CLIP_DURATION_S = 300.0

# The phone story canvas for every subtitled render -- subtitled has no
# `decision.orientation`/`extras["canvas"]` to read (unlike the montage
# compiler), and the portrait-source requirement below makes a landscape
# canvas meaningless here.
_STORY_CANVAS = Canvas(width=1080, height=1920)

# Below this, a clamped sound effect would be inaudible/pointless; skip it
# rather than emit a near-zero-length audio clip.
_MIN_SFX_DURATION_S = 0.05


def compile_phone_subtitled_plan(
    bindings: tuple[PhoneSourceBinding, ...],
    *,
    caption_cues: list[dict],
    caption_style: str = "sentence",
    visuals: tuple[PhoneVisualBinding, ...] = (),
    lanes: PhoneSubtitledLanes | None = None,
) -> EditRecipeV2:
    """Compile the subtitled edit format's phone recipe.

    ``bindings`` follows the same one-binding-per-clip contract as
    `compile_phone_montage_plan`/`compile_phone_guided_plan`, but subtitled is
    single-clip by product definition: exactly one binding is required (the
    uploader already caps new subtitled items at one clip; an item switched
    from montage can still carry more, and the cloud path silently uses only
    the first -- this compiler fails closed instead, since dropping clips
    silently on the phone would be a surprising device/cloud divergence).

    ``caption_cues`` are the cloud caption-cue dicts exactly as
    `_render_subtitled_variant` builds them -- see
    `app.pipeline.phone_captions`'s module docstring for the exact shape.
    Passing an empty list is explicitly supported: the clip still renders,
    just without captions (matches the cloud's own "no detectable speech"
    fallback, which ships the clean clip).

    ``caption_style`` is ``"sentence"`` (default, pop-in blocks) or ``"word"``
    (word-by-word lime pop) -- forwarded verbatim to
    `compile_caption_layers`.

    ``visuals`` pins the Visuals-pool photos/videos referenced by ``lanes``'
    overlay cards and ending clip (KRI-121-style receipts); unused when
    ``lanes`` carries neither.

    ``lanes`` (KRI-174 Phase 1, optional) layers on:
      - ``lanes.overlays``: sticker/photo/video cards over the speaker clip,
        as a silent overlay track (mirrors
        `app.pipeline.phone_editor_visuals.compile_editor_media_track`'s clip
        shape). Each card's window is clamped to the speaker clip's own
        duration; a card that ends up fully outside that window is dropped.
        A card's ``y_frac`` is clamped so a sticker never sits inside the
        caption band (`CAPTION_BAND_TOP_FRAC`). A ``kind="video"`` card
        (KRI-183) plays muted starting at ``source_start_s``; if the source
        footage runs out before the card's spoken window does, the on-screen
        window is SHORTENED to match the footage (the device never holds the
        last frame) and ``visualVideos`` is added to
        ``required_capabilities``.
      - ``lanes.sound_effects``: one shared ``sfx`` audio track of resolved
        catalog sound effects, clamped to the full timeline (speaker clip
        plus the ending clip, if any -- an effect may play under the ending
        clip). Two effects sharing a catalog id share one manifest entry.
      - ``lanes.ending_clip``: an optional MUTED (``volume=0``) Visuals-pool
        video appended to the SAME main video track right after the speaker
        clip, extending the recipe's own duration.

    Rejects (all `UnsupportedPhonePlan`, fail-closed):
      - zero or more than one binding.
      - a non-video source (no probed width/height).
      - a source clip whose (rotation-corrected) display is landscape or
        square -- the phone engine's center-fill crop has no face tracking.
      - a clip longer than 300s (`_MAX_CLIP_DURATION_S`), matching the
        cloud's own cap.
      - any `lanes` content that cannot compile -- raised as
        `app.pipeline.phone_subtitled_lanes.SubtitledLaneError`, a
        `UnsupportedPhonePlan` subclass naming the failing lane so a caller
        can retry the compile with just that lane dropped
        (`app.pipeline.phone_subtitled_lanes.drop_lane`).
    """
    if len(bindings) != 1:
        raise UnsupportedPhonePlan(
            f"phone subtitled plan requires exactly one clip, got {len(bindings)}"
        )
    binding = bindings[0]
    original = binding.original
    if original.width is None or original.height is None:
        raise UnsupportedPhonePlan("phone subtitled plan requires a video source clip")
    if original.duration_s > _MAX_CLIP_DURATION_S:
        raise UnsupportedPhonePlan(
            "subtitled clips are capped at 5 minutes -- trim the clip and re-upload"
        )
    display_width, display_height = _display_dims(original)
    if display_width >= display_height:
        raise UnsupportedPhonePlan(
            "phone subtitled plan requires a portrait source clip -- the phone engine "
            "aspect-fills with no face tracking, so a landscape or square clip would "
            "silently center-crop the speaker",
            capability="semanticCamera",
        )

    duration_s = float(original.duration_s)
    speaker_end = duration_s
    asset = binding.render_asset()
    assets: dict[str, MediaAsset] = {
        asset.id: MediaAsset(
            id=asset.id,
            relative_path=asset.id,
            fingerprint=AssetFingerprint(hex=original.sha256, byte_count=original.byte_count),
            duration=original.duration_s,
            natural_size=MediaSize(width=original.width, height=original.height),
            orientation_degrees=original.orientation_degrees,
            # `PhoneSourceBinding.require_proxy` already guarantees a reserved
            # analysis proxy for every binding reaching this compiler.
            is_proxy_available=True,
        )
    }
    manifest: dict[str, object] = {asset.id: asset}
    main_clips = [
        TimelineClip(
            id="clip-0",
            source_asset_id=asset.id,
            source_start=0.0,
            source_duration=duration_s,
            timeline_start=0.0,
            rate=1.0,
        )
    ]

    try:
        layers = compile_caption_layers(
            caption_cues,
            canvas_width=_STORY_CANVAS.width,
            canvas_height=_STORY_CANVAS.height,
            style=caption_style,
            timeline_duration_s=duration_s,
        )
    except UnsupportedPhonePlan:
        raise
    except Exception as exc:  # noqa: BLE001 - untrusted transcript/edited cue content
        raise UnsupportedPhonePlan(f"unable to compile captions: {exc}") from exc

    for font_id, font_asset in caption_font_assets(layers).items():
        manifest[font_id] = font_asset
        assets[font_id] = MediaAsset(
            id=font_id,
            relative_path=font_id,
            fingerprint=AssetFingerprint(
                hex=font_asset.fingerprint.sha256,
                byte_count=font_asset.fingerprint.byte_count,
            ),
        )

    required_capabilities = {"basicComposition", "local1080Export"} | (
        {"positionedText"} if layers else set()
    )
    if any(layer.effect not in {"static", "none"} for layer in layers):
        required_capabilities |= {"animatedText"}

    # `timeline_end` grows to include the ending clip, if any, BEFORE the sfx
    # lane is compiled -- an sfx clip is allowed to play under the ending
    # clip, not just the speaker clip.
    timeline_end = speaker_end
    if lanes is not None and lanes.ending_clip is not None:
        try:
            ending_clip, ending_duration = _compile_ending_clip(
                lanes.ending_clip,
                visuals=visuals,
                speaker_end=speaker_end,
                assets=assets,
                manifest=manifest,
            )
        except UnsupportedPhonePlan:
            raise
        except Exception as exc:  # noqa: BLE001 - untrusted lane content
            raise _lane_error("ending_clip", str(exc), capability="visualVideos") from exc
        main_clips.append(ending_clip)
        timeline_end = speaker_end + ending_duration
        required_capabilities |= {"visualVideos", "audioMix"}

    tracks = [TimelineTrack(id="subtitled", kind="video", clips=main_clips)]

    if lanes is not None and lanes.overlays:
        try:
            overlay_track, overlay_has_video = _compile_overlay_track(
                lanes.overlays,
                visuals=visuals,
                speaker_end=speaker_end,
                assets=assets,
                manifest=manifest,
            )
        except UnsupportedPhonePlan:
            raise
        except Exception as exc:  # noqa: BLE001 - untrusted lane content
            raise _lane_error("overlays", str(exc), capability="visualBlocks") from exc
        if overlay_track.clips:
            tracks.append(overlay_track)
            required_capabilities |= {"visualBlocks", "alphaOverlay", "audioMix"}
            if overlay_has_video:
                # KRI-183: a video overlay card composites through the same
                # `visualVideos` path the ending clip already uses.
                required_capabilities |= {"visualVideos"}

    if lanes is not None and lanes.sound_effects:
        try:
            sfx_track = _compile_sfx_track(
                lanes.sound_effects,
                timeline_end=timeline_end,
                assets=assets,
                manifest=manifest,
            )
        except UnsupportedPhonePlan:
            raise
        except Exception as exc:  # noqa: BLE001 - untrusted lane content
            raise _lane_error("sound_effects", str(exc), capability="soundEffects") from exc
        if sfx_track.clips:
            tracks.append(sfx_track)
            required_capabilities |= {"soundEffects", "audioMix"}

    return EditRecipeV2(
        canvas=_STORY_CANVAS,
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=tracks,
        text_layers=layers,
        audio=AudioMixRecipe(original_volume=1.0),
        required_capabilities=required_capabilities,
    )


def _compile_ending_clip(
    ending: SubtitledEndingClip,
    *,
    visuals: tuple[PhoneVisualBinding, ...],
    speaker_end: float,
    assets: dict[str, MediaAsset],
    manifest: dict[str, object],
) -> tuple[TimelineClip, float]:
    try:
        visual = require_bound_visual(
            visuals,
            media_id=ending.media_id,
            path=ending.gcs_path,
            generation=ending.generation,
        )
    except ValueError as exc:
        raise _lane_error("ending_clip", str(exc), capability="visualVideos") from exc
    if visual.kind != "video":
        raise _lane_error(
            "ending_clip", "ending clip requires a video visual", capability="visualVideos"
        )
    visual_duration = visual.duration_s or 0.0
    available = visual_duration - ending.trim_start_s
    if ending.trim_start_s >= visual_duration or available <= 0:
        raise _lane_error(
            "ending_clip",
            "trim start exceeds the source video's duration",
            capability="visualVideos",
        )
    source_duration = (
        available if ending.max_duration_s is None else min(available, ending.max_duration_s)
    )
    if source_duration <= 0:
        raise _lane_error(
            "ending_clip", "no playable duration remains after trim", capability="visualVideos"
        )
    asset = visual.render_asset()
    manifest[asset.id] = asset
    assets[asset.id] = MediaAsset(
        id=asset.id,
        relative_path=asset.id,
        fingerprint=AssetFingerprint(hex=visual.sha256, byte_count=visual.byte_count),
        duration=(
            visual.duration_s
            if visual.duration_s is not None and visual.duration_s <= 1800
            else None
        ),
        natural_size=MediaSize(width=visual.width or 1, height=visual.height or 1),
        orientation_degrees=visual.orientation_degrees,
    )
    clip = TimelineClip(
        id="clip-ending",
        source_asset_id=asset.id,
        source_start=ending.trim_start_s,
        source_duration=source_duration,
        timeline_start=speaker_end,
        rate=1,
        volume=0,
    )
    return clip, source_duration


def _compile_overlay_track(
    cards: list[SubtitledOverlayCard],
    *,
    visuals: tuple[PhoneVisualBinding, ...],
    speaker_end: float,
    assets: dict[str, MediaAsset],
    manifest: dict[str, object],
) -> tuple[TimelineTrack, bool]:
    """Compile ``cards`` onto the silent ``subtitled-overlays`` track.

    Returns the track plus whether any compiled card was a video overlay
    (KRI-183) -- the caller folds that into ``required_capabilities`` instead
    of this function reaching for settings itself (the compiler stays
    settings-free; the worker gates via `phone_rollout.
    phone_subtitled_video_overlays_supported`).
    """
    ordered = sorted(cards, key=lambda card: (card.z, card.start_s, card.id))
    clips: list[TimelineClip] = []
    has_video = False
    for order, card in enumerate(ordered, start=1):
        window_start = max(card.start_s, 0.0)
        window_end = min(card.end_s, speaker_end)
        if window_end <= window_start:
            # Fully outside the speaker clip's own duration -- drop silently.
            continue
        try:
            visual = require_bound_visual(
                visuals, media_id=card.media_id, path=card.gcs_path, generation=card.generation
            )
        except ValueError as exc:
            raise _lane_error("overlays", str(exc), capability="visualBlocks") from exc
        if card.kind == "video":
            if visual.kind != "video":
                raise _lane_error(
                    "overlays",
                    "video overlay card requires a video visual",
                    capability="visualVideos",
                )
            has_video = True
            clips.append(
                _compile_video_overlay_clip(
                    card,
                    visual=visual,
                    window_start=window_start,
                    window_end=window_end,
                    order=order,
                    assets=assets,
                    manifest=manifest,
                )
            )
            continue
        if visual.kind != "image":
            raise _lane_error(
                "overlays", "overlay card requires an image visual", capability="visualBlocks"
            )
        asset = visual.render_asset()
        manifest[asset.id] = asset
        assets[asset.id] = MediaAsset(
            id=asset.id,
            relative_path=asset.id,
            fingerprint=AssetFingerprint(hex=visual.sha256, byte_count=visual.byte_count),
        )
        y_frac = min(card.y_frac, CAPTION_BAND_TOP_FRAC)
        clips.append(
            TimelineClip(
                id=f"subtitled-overlay-{card.id}",
                source_asset_id=asset.id,
                source_start=0.0,
                source_duration=window_end - window_start,
                timeline_start=window_start,
                rate=1,
                volume=0,
                visual_placement=VisualMediaPlacement(
                    order=order,
                    width_fraction=card.scale,
                    x_fraction=card.x_frac,
                    y_fraction=y_frac,
                    window_start=window_start,
                    window_end=window_end,
                    fade_in=card.fade,
                    fade_out=card.fade,
                ),
            )
        )
    return TimelineTrack(id="subtitled-overlays", kind="overlay", clips=clips), has_video


def _compile_video_overlay_clip(
    card: SubtitledOverlayCard,
    *,
    visual: PhoneVisualBinding,
    window_start: float,
    window_end: float,
    order: int,
    assets: dict[str, MediaAsset],
    manifest: dict[str, object],
) -> TimelineClip:
    """One video overlay card (KRI-183) -- muted, like `_compile_ending_clip`.

    ``window_start``/``window_end`` are already clamped to the speaker
    clip's own duration (same as an image card's window). The card's
    on-screen window is further SHORTENED to the footage (rather than
    holding the last frame, which the device engine does not do) whenever
    the source video is shorter than that spoken window.
    """
    source_start = card.source_start_s
    visual_duration = visual.duration_s or 0.0
    available = visual_duration - source_start
    if available <= 0:
        raise _lane_error(
            "overlays",
            "video overlay card's source_start_s is past the source video's duration",
            capability="visualVideos",
        )
    requested_window = window_end - window_start
    source_duration = min(requested_window, available)
    window_end = window_start + source_duration
    asset = visual.render_asset()
    manifest[asset.id] = asset
    assets[asset.id] = MediaAsset(
        id=asset.id,
        relative_path=asset.id,
        fingerprint=AssetFingerprint(hex=visual.sha256, byte_count=visual.byte_count),
        duration=(
            visual.duration_s
            if visual.duration_s is not None and visual.duration_s <= 1800
            else None
        ),
        natural_size=MediaSize(width=visual.width or 1, height=visual.height or 1),
        orientation_degrees=visual.orientation_degrees,
    )
    y_frac = min(card.y_frac, CAPTION_BAND_TOP_FRAC)
    return TimelineClip(
        id=f"subtitled-overlay-{card.id}",
        source_asset_id=asset.id,
        source_start=source_start,
        source_duration=source_duration,
        timeline_start=window_start,
        rate=1,
        volume=0,
        visual_placement=VisualMediaPlacement(
            order=order,
            width_fraction=card.scale,
            x_fraction=card.x_frac,
            y_fraction=y_frac,
            window_start=window_start,
            window_end=window_end,
            fade_in=card.fade,
            fade_out=card.fade,
        ),
    )


def _compile_sfx_track(
    resolved_effects: list[ResolvedSoundEffect],
    *,
    timeline_end: float,
    assets: dict[str, MediaAsset],
    manifest: dict[str, object],
) -> TimelineTrack:
    ordered = sorted(
        resolved_effects, key=lambda resolved: (resolved.request.at_s, resolved.request.id)
    )
    asset_by_catalog_id: dict[str, object] = {}
    clips: list[TimelineClip] = []
    for resolved in ordered:
        request = resolved.request
        if request.at_s >= timeline_end:
            # Starts at/after the end of the timeline -- nothing to play.
            continue
        clamped_duration = min(resolved.duration_s, timeline_end - request.at_s)
        if clamped_duration < _MIN_SFX_DURATION_S:
            continue
        existing = asset_by_catalog_id.get(resolved.asset.catalog_id)
        if existing is not None:
            if existing.fingerprint != resolved.asset.fingerprint or (
                existing.generation != resolved.asset.generation
            ):
                raise _lane_error(
                    "sound_effects",
                    "one catalog sound effect id resolved to two different assets",
                    capability="soundEffects",
                )
            asset = existing
        else:
            asset = resolved.asset
            asset_by_catalog_id[resolved.asset.catalog_id] = asset
        manifest[asset.id] = asset
        assets.setdefault(
            asset.id,
            MediaAsset(
                id=asset.id,
                relative_path=asset.id,
                fingerprint=AssetFingerprint(
                    hex=asset.fingerprint.sha256, byte_count=asset.fingerprint.byte_count
                ),
                duration=resolved.duration_s,
            ),
        )
        clips.append(
            TimelineClip(
                id=f"sfx-{request.id}",
                source_asset_id=asset.id,
                source_start=0.0,
                source_duration=clamped_duration,
                timeline_start=request.at_s,
                rate=1,
                volume=request.volume,
            )
        )
    return TimelineTrack(id="sfx", kind="audio", clips=clips)


def _display_dims(original) -> tuple[int, int]:
    """(width, height) as actually DISPLAYED once `orientation_degrees` is
    applied -- a 1080x1920-pixel file flagged 90/270 degrees is portrait on
    screen despite carrying landscape pixel dimensions (mirrors the
    golden-hour exact-canvas check in `phone_montage_plan.py`/
    `phone_guided_plan.py`, which reasons about the same rotation flag)."""
    if original.orientation_degrees in (90, 270):
        return original.height, original.width
    return original.width, original.height
