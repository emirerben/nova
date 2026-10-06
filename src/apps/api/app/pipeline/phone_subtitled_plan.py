"""Project the single-clip "Talking to camera" (subtitled) edit format into a
native device render program (KRI-132), optionally carrying overlay
sticker/photo/video cards, a sound-effects track, and a muted ending clip
(KRI-174 Phase 1 -- see `app.pipeline.phone_subtitled_lanes`; video overlay
cards are KRI-183).

Companion to `app.pipeline.phone_voiceover_montage_plan.compile_phone_voiceover_montage_plan` and
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
a non-portrait source; the phone engine cover-fills (center-crops) with no
face tracking. KRI-283: with ``landscape_fit="fit"`` (the content-plan default,
same as the cloud) a landscape speaker clip is letterboxed instead -- every
speaker `TimelineClip` carries a `MediaTransform(scale=contain/cover)` that the
engine applies about the canvas center over black
(`app.pipeline.phone_recipe_shared.fit_transform`). With ``"fill"`` (and for
square sources) the clip is center-cropped, like the cloud's own fill path.

With ``lanes=None`` (or an all-empty `PhoneSubtitledLanes`) and
``visuals=()``, this compiler's output is byte-identical to the pre-KRI-174
shape -- every lane below is strictly additive and opt-in.

Multi-clip Talking head (KRI-136): ``cutaways`` turns the same recipe into
the phone version of the cloud `app.pipeline.talking_head_assembler`: the
speaker (spine) clip stays the ONLY main-track clip, so its audio plays the
whole way through, and every other clip covers the picture for a short
window as a muted, full-frame clip on its own overlay track
(`CUTAWAY_TRACK_ID`). The device engine already renders that shape --
a `VisualMediaPlacement` with no ``width_fraction`` cover-fills the canvas,
and a placed clip never contributes audio -- so no new native primitive is
needed. Cutaway footage may be landscape (it is centre-cropped, like the
cloud's b-roll reframe); only the speaker clip is letterboxed. With
``cutaways=()`` the output is byte-identical to the single-clip shape.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

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
from app.pipeline.phone_captions import (
    PhoneCaptionLook,
    caption_font_assets,
    compile_caption_layers,
)
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import (
    display_dims,
    fit_transform,
    landscape_fit_from_recipe,  # noqa: F401 - re-export, moved to phone_recipe_shared (KRI-285)
    snap_text_overshoot,
)
from app.pipeline.phone_subtitled_lanes import (
    CAPTION_BAND_TOP_FRAC,
    PhoneSubtitledLanes,
    ResolvedSoundEffect,
    SubtitledEndingClip,
    SubtitledOverlayCard,
    _lane_error,
)
from app.pipeline.silence_cut import CutPlan
from app.services.phone_sources import (
    PhoneSourceBinding,
    PhoneVisualBinding,
    require_bound_visual,
)

# Old private name, kept importable (moved to `phone_recipe_shared.display_dims`).
_display_dims = display_dims

# Mirrors `_render_subtitled_variant`'s own cap (generative_build.py, the
# `probe.duration_s > 300.0` check ~line 20690): "subtitled clips are capped
# at 5 minutes". Named up front rather than surfacing as a bare recipe
# validation error.
_MAX_CLIP_DURATION_S = 300.0

# The phone story canvas for every subtitled render -- subtitled has no
# `decision.orientation`/`extras["canvas"]` to read (unlike the montage
# compiler); a landscape source is letterboxed into it (KRI-283), never given
# a landscape canvas.
_STORY_CANVAS = Canvas(width=1080, height=1920)

# Below this, a clamped sound effect would be inaudible/pointless; skip it
# rather than emit a near-zero-length audio clip.
_MIN_SFX_DURATION_S = 0.05

# SFX-under-speech duck (KRI-181 follow-up). The phone renderer has no
# loudnorm or sidechain stage, so a full-scale catalog effect landing on a
# spoken word plays at its raw level and can mask the speaker. When
# ``duck_sfx_under_speech`` is on, an effect whose window overlaps speech plays
# at ``volume * SFX_SPEECH_DUCK_GAIN`` (0.35 is about -9 dB); an effect that
# lands in a pause keeps its full requested volume so the beat still hits.
# The speech clip itself is never lowered: ducking the speaker under the
# effect would make the words quieter at exactly the moment they compete.
SFX_SPEECH_DUCK_GAIN = 0.35
# Word gaps shorter than this are still "inside a sentence", so an effect
# dropped between two quick words counts as landing on speech.
_SPEECH_WORD_BRIDGE_S = 0.25
# Overlap shorter than this (a decaying tail brushing the next word) does not
# trigger the duck.
_SFX_SPEECH_MIN_OVERLAP_S = 0.05
# Per-variant receipt of the effects the duck lowered, keyed by sound-effect
# request id -> the requested (pre-duck) volume. The pinned recipe only
# carries the ducked volume, so the phone editor reads this to show and
# re-save the creator's own volume instead of ducking an effect twice.
SFX_DUCK_RECEIPT_FIELD = "phone_sfx_duck_receipt"

# KRI-467: layers compiled from the variant's text elements (the opening
# title, and any text the creator adds in the editor). Captions keep their own
# ``caption-`` ids and draw after (on top of) these, like the cloud's
# styled-text lane, which burns text before the captions.
TEXT_LAYER_PREFIX = "text-"
# One 30 fps frame: a text layer that would show for less is not drawn.
_MIN_TEXT_LAYER_S = 1 / 30

# KRI-136: the overlay track that carries multi-clip Talking-head cutaways.
# Listed right after the main track so a sticker/photo/video card on
# ``subtitled-overlays`` (same ``order`` range, later track) always draws on
# top of a cutaway, never under it.
CUTAWAY_TRACK_ID = "talking-head-cutaways"
# Mirrors `talking_head_assembler._MIN_WINDOW_S`: a shorter cutaway reads as
# a flicker, not a cut.
_MIN_CUTAWAY_S = 0.5


class PhoneCutaway(BaseModel):
    """One multi-clip Talking-head cutaway (KRI-136): ``binding``'s picture
    replaces the speaker's between ``start_s`` and ``end_s`` (final timeline
    seconds -- the CUT timeline when a speech-cleanup cut applies), reading
    the cutaway clip from ``source_start_s``. Its audio is never heard."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    binding: PhoneSourceBinding
    source_start_s: float = Field(default=0.0, ge=0)
    start_s: float = Field(ge=0, le=1800)
    end_s: float = Field(gt=0, le=1800)

    @model_validator(mode="after")
    def _window(self) -> PhoneCutaway:
        if self.end_s <= self.start_s:
            raise ValueError("cutaway window must have positive duration")
        return self


def compile_phone_subtitled_plan(
    bindings: tuple[PhoneSourceBinding, ...],
    *,
    caption_cues: list[dict],
    caption_style: str = "sentence",
    visuals: tuple[PhoneVisualBinding, ...] = (),
    lanes: PhoneSubtitledLanes | None = None,
    duck_sfx_under_speech: bool = False,
    cut_plan: CutPlan | None = None,
    keep_segments: list[tuple[float, float]] | None = None,
    caption_look: PhoneCaptionLook | None = None,
    cutaways: tuple[PhoneCutaway, ...] = (),
    landscape_fit: Literal["fill", "fit"] = "fill",
    text_elements: Sequence[Mapping[str, Any]] = (),
    text_elements_user_edited: bool = False,
) -> EditRecipeV2:
    """Compile the subtitled edit format's phone recipe.

    ``bindings`` follows the same one-binding-per-clip contract as
    `compile_phone_voiceover_montage_plan`/`compile_phone_guided_plan`, but subtitled is
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
        Each effect may optionally trim to a sub-range of its own resolved
        source audio (``request.trim_start_s``/``trim_end_s``, KRI-182 step
        1); a trim start at or past the resolved duration is rejected
        (``SubtitledLaneError``) rather than silently emitting nothing.
      - ``lanes.ending_clip``: an optional MUTED (``volume=0``) Visuals-pool
        video appended to the SAME main video track right after the speaker
        clip, extending the recipe's own duration.

    ``duck_sfx_under_speech`` (default ``False``, from
    ``settings.phone_sfx_speech_duck_enabled``) scales the volume of every
    sound effect whose window overlaps a spoken word in ``caption_cues`` by
    `SFX_SPEECH_DUCK_GAIN`. It only changes existing clip ``volume`` values,
    so the recipe needs no new field or capability; ``False`` is
    byte-identical to the pre-duck output.

    ``cut_plan`` (optional, a required-speech-cleanup `CutPlan` already
    validated against this exact clip -- see
    `app.tasks.generative_build._run_phone_subtitled_job`) applies its
    ``keep_segments`` as HARD CUTS on the main video track: one
    `TimelineClip` per kept segment, back to back on the timeline, instead
    of the single full-duration clip. No `Transition`/crossfade joins them --
    a picture crossfade between two cuts of the SAME speaker would blend two
    unrelated words into each other. The phone engine instead crossfades only
    the AUDIO of such same-source cuts, borrowing ~25 ms of the removed span
    on each side (`AudioCutHandles` in KriaMediaEngine), so room tone runs
    through the join instead of dipping to silence. ``caption_cues`` and
    every ``lanes`` window MUST already be expressed in CUT-timeline
    coordinates when ``cut_plan`` is passed (the caller remaps them --
    `app.pipeline.phone_captions.remap_cues`,
    `app.pipeline.phone_subtitled_lanes.remap_lanes_for_cut`); this function
    only reshapes the video/audio timeline itself. A ``cut_plan`` with no
    removals (``cut_plan.removed`` empty -- a no-op or bailed-out plan) is
    equivalent to passing ``None``.

    ``keep_segments`` (optional, KRI-216) is the SAME shape as
    ``cut_plan.keep_segments`` but pre-resolved by the caller, for a phone
    editor Save that must reconstruct a previously pinned cut without
    rebuilding a full `CutPlan` (`app.services.phone_editor.
    _compile_subtitled_editor_commit` reads the kept windows straight off the
    previous pinned `EditRecipeV2`'s own main-track clips). Takes precedence
    over ``cut_plan`` when both are given -- a caller should only ever pass
    one. Same coordinate contract as ``cut_plan``: ``caption_cues``/``lanes``
    must already be expressed against the resulting cut timeline.

    ``caption_look`` (optional, KRI-216, `app.pipeline.phone_captions.
    PhoneCaptionLook`) forwards verbatim to `compile_caption_layers` --
    ``None`` (default) is that module's own hardcoded default look,
    byte-identical to this compiler's pre-KRI-216 caption appearance.

    ``cutaways`` (optional, KRI-136) makes this a multi-clip Talking head:
    ``bindings`` still holds exactly the ONE speaker clip, and each
    `PhoneCutaway` carries its own binding. See the module docstring and
    `_compile_cutaway_track` for the exact shape and rejections.

    ``landscape_fit`` (KRI-283, ``"fill"`` default) is the cloud's
    ``landscape_fit`` preference. ``"fit"`` letterboxes a landscape speaker
    clip via a main-track `MediaTransform(scale=contain/cover)` on EVERY speaker
    `TimelineClip` (all keep-segment cuts); the ending clip and cutaways are
    never transformed. ``"fill"``, square and portrait sources keep the
    identity transform, so those recipes are byte-identical to the pre-KRI-283
    shape.

    ``text_elements`` (KRI-467, optional) is the variant's text lane: the
    persisted ``TextElement`` rows (the opening title, and any text added in
    the editor), in CUT-timeline seconds like the cues. They compile through
    the cloud styled-text lane's own burn dicts
    (`generative_build._text_element_burn_dicts`: caption-cue mirrors and
    removed rows are skipped) and `compile_text_overlay`, as ``text-<n>``
    layers drawn UNDER the captions. ``text_elements_user_edited`` is the
    variant flag that picks the karaoke settle color. Empty (the default) is
    byte-identical to the pre-KRI-467 recipe.

    Rejects (all `UnsupportedPhonePlan`, fail-closed):
      - zero or more than one binding.
      - a non-video source (no probed width/height).
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
    speaker_transform = fit_transform(display_width, display_height, _STORY_CANVAS, landscape_fit)

    duration_s = float(original.duration_s)
    # An explicit `keep_segments` (editor Save reconstructing a previously
    # pinned cut) wins over `cut_plan`; a cut plan with no removals (no-op or
    # safety-bailed-out) renders the single full-duration clip exactly like
    # both being `None` -- this is the ONLY branch point either introduces;
    # every line below it is shared.
    if keep_segments is not None:
        resolved_keep_segments = [(float(start), float(end)) for start, end in keep_segments]
    elif cut_plan is not None and cut_plan.removed:
        resolved_keep_segments = [
            (float(start), float(end)) for start, end in cut_plan.keep_segments
        ]
    else:
        resolved_keep_segments = [(0.0, duration_s)]
    if not resolved_keep_segments or all(end <= start for start, end in resolved_keep_segments):
        raise UnsupportedPhonePlan("speech cleanup removed the entire clip")
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
    main_clips: list[TimelineClip] = []
    cursor = 0.0
    for index, (seg_start, seg_end) in enumerate(resolved_keep_segments):
        seg_duration = seg_end - seg_start
        if seg_duration <= 0:
            continue
        main_clips.append(
            TimelineClip(
                id=f"clip-{index}",
                source_asset_id=asset.id,
                source_start=seg_start,
                source_duration=seg_duration,
                timeline_start=cursor,
                rate=1.0,
                transform=speaker_transform,
            )
        )
        cursor += seg_duration
    speaker_end = cursor

    try:
        layers = compile_caption_layers(
            caption_cues,
            canvas_width=_STORY_CANVAS.width,
            canvas_height=_STORY_CANVAS.height,
            style=caption_style,
            timeline_duration_s=speaker_end,
            look=caption_look,
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

    if text_elements:
        text_layers = _compile_text_lane(
            text_elements,
            user_edited=text_elements_user_edited,
            timeline_duration_s=timeline_end,
        )
        if text_layers:
            for font_id, font_asset in caption_font_assets(text_layers).items():
                if font_id in manifest:
                    continue
                manifest[font_id] = font_asset
                assets[font_id] = MediaAsset(
                    id=font_id,
                    relative_path=font_id,
                    fingerprint=AssetFingerprint(
                        hex=font_asset.fingerprint.sha256,
                        byte_count=font_asset.fingerprint.byte_count,
                    ),
                )
            # Text first: the device composites layers in order, so captions
            # stay on top of a title they share a moment with.
            layers = [*text_layers, *layers]
            required_capabilities |= {"positionedText"}
            if any(layer.effect not in {"static", "none"} for layer in text_layers):
                required_capabilities |= {"animatedText"}

    tracks = [TimelineTrack(id="subtitled", kind="video", clips=main_clips)]

    if cutaways:
        cutaway_track = _compile_cutaway_track(
            cutaways,
            speaker_media_id=binding.media_id,
            speaker_end=speaker_end,
            assets=assets,
            manifest=manifest,
        )
        if cutaway_track.clips:
            tracks.append(cutaway_track)
            # Native derives `alphaOverlay` from every non-empty overlay
            # track, including opaque full-frame Talking-head cutaways. Keep
            # the server declaration aligned so a device never routes cloud
            # after the backend admitted the multi-clip shape.
            required_capabilities |= {
                "visualBlocks",
                "visualVideos",
                "alphaOverlay",
                "audioMix",
            }

    fullscreen_cards = (
        [c for c in lanes.overlays if c.display_mode == "fullscreen"] if lanes is not None else []
    )
    pip_cards = (
        [c for c in lanes.overlays if c.display_mode != "fullscreen"] if lanes is not None else []
    )
    if fullscreen_cards:
        # KRI-297: full-frame cutaways sit ABOVE the speaker track and BELOW
        # the PiP card track (appended first) and below captions (text layers
        # always composite last).
        try:
            fullscreen_track, fullscreen_has_video = _compile_fullscreen_track(
                fullscreen_cards,
                visuals=visuals,
                speaker_end=speaker_end,
                assets=assets,
                manifest=manifest,
            )
        except UnsupportedPhonePlan:
            raise
        except Exception as exc:  # noqa: BLE001 - untrusted lane content
            raise _lane_error("overlays", str(exc), capability="visualBlocks") from exc
        if fullscreen_track.clips:
            tracks.append(fullscreen_track)
            required_capabilities |= {"visualBlocks", "alphaOverlay", "audioMix"}
            if fullscreen_has_video:
                required_capabilities |= {"visualVideos"}

    if pip_cards:
        try:
            overlay_track, overlay_has_video = _compile_overlay_track(
                pip_cards,
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
                speech_windows=(
                    speech_windows_from_cues(caption_cues) if duck_sfx_under_speech else ()
                ),
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


def _compile_text_lane(
    text_elements: Sequence[Mapping[str, Any]],
    *,
    user_edited: bool,
    timeline_duration_s: float,
) -> list[Any]:
    """The variant's text elements as positioned text layers (KRI-467)."""
    from app.pipeline.portable_text_layout import compile_text_overlay  # noqa: PLC0415
    from app.tasks.generative_build import _text_element_burn_dicts  # noqa: PLC0415

    try:
        overlays = _text_element_burn_dicts(
            {
                "resolved_archetype": "subtitled",
                "text_elements": [dict(row) for row in text_elements],
                "duration_s": timeline_duration_s,
                "text_elements_user_edited": user_edited,
            }
        )
        layers = [
            compile_text_overlay(
                overlay,
                layer_id=f"{TEXT_LAYER_PREFIX}{index}",
                canvas=_STORY_CANVAS,
                dissolve_seed=101 + index * 37,
            )[0]
            for index, overlay in enumerate(overlays)
        ]
    except UnsupportedPhonePlan:
        raise
    except Exception as exc:  # noqa: BLE001 - untrusted editor text
        raise UnsupportedPhonePlan(f"unable to compile text: {exc}") from exc
    # A row held to the end can overshoot the recipe's own float end
    # (`EditRecipeV2` compares strictly, KRI-190).
    snap_text_overshoot(layers, timeline_duration_s)
    # A row timed past the clip (an edited row, or a title held to the end of a
    # clip the phone measured a hair shorter than its proxy) ends with the
    # clip instead of failing the whole recipe; one that would start after it
    # is not drawn at all.
    kept = []
    for layer in layers:
        if layer.start >= timeline_duration_s - _MIN_TEXT_LAYER_S:
            continue
        if layer.end > timeline_duration_s:
            layer.end = timeline_duration_s
        kept.append(layer)
    return kept


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
    speech_windows: tuple[tuple[float, float], ...] = (),
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


_FULLSCREEN_TRACK_ID = "subtitled-fullscreen"
_MIN_FULLSCREEN_S = 0.3


def _compile_fullscreen_track(
    cards: list[SubtitledOverlayCard],
    *,
    visuals: tuple[PhoneVisualBinding, ...],
    speaker_end: float,
    assets: dict[str, MediaAsset],
    manifest: dict[str, object],
) -> tuple[TimelineTrack, bool]:
    """KRI-297: compile full-screen cutaway cards onto a muted, full-canvas
    cover-fill overlay track (a `VisualMediaPlacement` with no
    ``width_fraction`` -- the same shape `_compile_cutaway_track` uses, which
    the native engine cover-fills for stills and video alike). Windows are
    clamped to the speaker timeline, a video's window is shortened to its
    footage, and two overlapping windows are rejected (`SubtitledLaneError`)
    -- full-screen cards are a sequence, never a stack.
    """
    ordered = sorted(cards, key=lambda card: (card.start_s, card.end_s, card.id))
    clips: list[TimelineClip] = []
    has_video = False
    previous_end = 0.0
    for card in ordered:
        window_start = max(card.start_s, 0.0)
        window_end = min(card.end_s, speaker_end)
        if window_end - window_start < _MIN_FULLSCREEN_S:
            continue
        if window_start < previous_end - 1e-6:
            raise _lane_error(
                "overlays",
                "full-screen overlay windows must not overlap",
                capability="visualBlocks",
            )
        try:
            visual = require_bound_visual(
                visuals, media_id=card.media_id, path=card.gcs_path, generation=card.generation
            )
        except ValueError as exc:
            raise _lane_error("overlays", str(exc), capability="visualBlocks") from exc
        asset = visual.render_asset()
        source_start = 0.0
        if card.kind == "video":
            if visual.kind != "video":
                raise _lane_error(
                    "overlays",
                    "video overlay card requires a video visual",
                    capability="visualVideos",
                )
            has_video = True
            source_start = card.source_start_s
            available = (visual.duration_s or 0.0) - source_start
            if available <= 0:
                raise _lane_error(
                    "overlays",
                    "video overlay card's source_start_s is past the source video's duration",
                    capability="visualVideos",
                )
            window_end = min(window_end, window_start + available)
            if window_end - window_start < _MIN_FULLSCREEN_S:
                continue
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
        else:
            if visual.kind != "image":
                raise _lane_error(
                    "overlays", "overlay card requires an image visual", capability="visualBlocks"
                )
            manifest[asset.id] = asset
            assets[asset.id] = MediaAsset(
                id=asset.id,
                relative_path=asset.id,
                fingerprint=AssetFingerprint(hex=visual.sha256, byte_count=visual.byte_count),
            )
        clips.append(
            TimelineClip(
                id=f"subtitled-overlay-{card.id}",
                source_asset_id=asset.id,
                source_start=source_start,
                source_duration=window_end - window_start,
                timeline_start=window_start,
                rate=1,
                volume=0,
                visual_placement=VisualMediaPlacement(
                    order=1,
                    window_start=window_start,
                    window_end=window_end,
                    fade_in=card.fade,
                    fade_out=card.fade,
                ),
            )
        )
        previous_end = window_end
    return TimelineTrack(id=_FULLSCREEN_TRACK_ID, kind="overlay", clips=clips), has_video


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


def _compile_cutaway_track(
    cutaways: tuple[PhoneCutaway, ...],
    *,
    speaker_media_id: str,
    speaker_end: float,
    assets: dict[str, MediaAsset],
    manifest: dict[str, object],
) -> TimelineTrack:
    """Compile KRI-136 cutaways onto the muted, full-frame `CUTAWAY_TRACK_ID`
    overlay track.

    Each window is clamped to the speaker's own timeline (a cutaway never
    extends the video -- the speech is the spine), and shortened to the
    cutaway's own footage rather than holding a last frame (the device never
    freezes a clip; the speaker simply shows through again, like the cloud
    `eof_action=pass`). A window left shorter than `_MIN_CUTAWAY_S` is
    dropped. Rejects (`UnsupportedPhonePlan`) a cutaway over the speaker clip
    itself, a non-video cutaway, or two overlapping windows.
    """
    clips: list[TimelineClip] = []
    previous_end = 0.0
    for index, cutaway in enumerate(sorted(cutaways, key=lambda c: (c.start_s, c.end_s))):
        cut_binding = cutaway.binding
        original = cut_binding.original
        if cut_binding.media_id == speaker_media_id:
            raise UnsupportedPhonePlan("a Talking-head cutaway cannot reuse the speaker clip")
        if original.width is None or original.height is None:
            raise UnsupportedPhonePlan("a Talking-head cutaway requires a video clip")
        if cutaway.start_s < previous_end - 1e-6:
            raise UnsupportedPhonePlan("Talking-head cutaways must not overlap")
        window_start = cutaway.start_s
        window_end = min(cutaway.end_s, speaker_end)
        available = float(original.duration_s) - cutaway.source_start_s
        window_end = min(window_end, window_start + max(available, 0.0))
        if window_end - window_start < _MIN_CUTAWAY_S:
            continue
        asset = cut_binding.render_asset()
        manifest[asset.id] = asset
        assets[asset.id] = MediaAsset(
            id=asset.id,
            relative_path=asset.id,
            fingerprint=AssetFingerprint(hex=original.sha256, byte_count=original.byte_count),
            duration=original.duration_s,
            natural_size=MediaSize(width=original.width, height=original.height),
            orientation_degrees=original.orientation_degrees,
            is_proxy_available=True,
        )
        clips.append(
            TimelineClip(
                id=f"cutaway-{index}",
                source_asset_id=asset.id,
                source_start=cutaway.source_start_s,
                source_duration=window_end - window_start,
                timeline_start=window_start,
                rate=1,
                volume=0,
                visual_placement=VisualMediaPlacement(
                    order=1,
                    window_start=window_start,
                    window_end=window_end,
                ),
            )
        )
        previous_end = window_end
    return TimelineTrack(id=CUTAWAY_TRACK_ID, kind="overlay", clips=clips)


def cutaways_from_recipe(
    recipe: EditRecipeV2, bindings: tuple[PhoneSourceBinding, ...]
) -> tuple[PhoneCutaway, ...]:
    """The inverse of `_compile_cutaway_track`: read a pinned recipe's
    cutaways back so a phone-editor Save recompiles them unchanged (the
    editor never moves them; captions/lanes share the same timeline).
    Returns ``()`` for a single-clip Talking recipe. Raises ``ValueError`` if
    a cutaway's clip no longer has a phone source binding."""
    track = next((t for t in recipe.tracks if t.id == CUTAWAY_TRACK_ID), None)
    if track is None:
        return ()
    by_media_id = {b.media_id: b for b in bindings}
    cutaways: list[PhoneCutaway] = []
    for clip in track.clips:
        cut_binding = by_media_id.get(clip.source_asset_id)
        if cut_binding is None:
            raise ValueError("Talking-head cutaway has no phone source binding")
        cutaways.append(
            PhoneCutaway(
                binding=cut_binding,
                source_start_s=clip.source_start,
                start_s=clip.timeline_start,
                end_s=clip.timeline_start + clip.source_duration,
            )
        )
    return tuple(cutaways)


def speaker_binding_from_recipe(
    recipe: EditRecipeV2, bindings: tuple[PhoneSourceBinding, ...]
) -> PhoneSourceBinding:
    """The binding whose asset plays on a pinned recipe's main
    (``"subtitled"``) track -- the speaker. With one binding this is always
    that binding; a multi-clip Talking head picks the spine out of all of
    them. Raises ``ValueError`` when no binding matches."""
    if len(bindings) == 1:
        return bindings[0]
    main_track = next((t for t in recipe.tracks if t.id == "subtitled"), None)
    main_assets = {clip.source_asset_id for clip in main_track.clips} if main_track else set()
    matches = [b for b in bindings if b.media_id in main_assets]
    if len(matches) != 1:
        raise ValueError("Talking-head recipe has no single speaker clip")
    return matches[0]


def _compile_sfx_track(
    resolved_effects: list[ResolvedSoundEffect],
    *,
    timeline_end: float,
    assets: dict[str, MediaAsset],
    manifest: dict[str, object],
    speech_windows: tuple[tuple[float, float], ...] = (),
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
        source_start = request.trim_start_s or 0.0
        if source_start >= resolved.duration_s:
            raise _lane_error(
                "sound_effects",
                "trim start exceeds the sound effect's duration",
                capability="soundEffects",
            )
        trim_end = request.trim_end_s
        playable_duration = (
            trim_end
            if trim_end is not None and trim_end <= resolved.duration_s
            else resolved.duration_s
        ) - source_start
        clamped_duration = min(playable_duration, timeline_end - request.at_s)
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
                source_start=source_start,
                source_duration=clamped_duration,
                timeline_start=request.at_s,
                rate=1,
                volume=_sfx_volume(
                    request.volume,
                    start_s=request.at_s,
                    end_s=request.at_s + clamped_duration,
                    speech_windows=speech_windows,
                ),
            )
        )
    return TimelineTrack(id="sfx", kind="audio", clips=clips)


def sfx_duck_receipt(lanes: PhoneSubtitledLanes | None, recipe: EditRecipeV2) -> dict | None:
    """``{"version", "gain", "volumes": {request_id: requested_volume}}`` for
    every effect whose compiled clip volume differs from its request (i.e. the
    speech duck lowered it), or ``None`` when nothing was ducked -- so a
    flag-off compile never writes the receipt."""
    if lanes is None or not lanes.sound_effects:
        return None
    compiled = {
        clip.id: clip.volume for track in recipe.tracks if track.id == "sfx" for clip in track.clips
    }
    volumes = {
        resolved.request.id: resolved.request.volume
        for resolved in lanes.sound_effects
        if f"sfx-{resolved.request.id}" in compiled
        and compiled[f"sfx-{resolved.request.id}"] != resolved.request.volume
    }
    if not volumes:
        return None
    return {"version": 1, "gain": SFX_SPEECH_DUCK_GAIN, "volumes": volumes}


def speech_windows_from_cues(caption_cues: list[dict]) -> tuple[tuple[float, float], ...]:
    """Merged ``(start_s, end_s)`` windows where someone is talking.

    Uses each cue's per-word timings when present (the subtitled caption path
    always attaches them), else the cue's own span. Word gaps shorter than
    `_SPEECH_WORD_BRIDGE_S` are merged so a sentence reads as one window.
    Cues are untrusted transcript data: malformed or non-positive spans are
    skipped instead of failing the compile.
    """
    spans: list[tuple[float, float]] = []
    for cue in caption_cues or ():
        if not isinstance(cue, dict):
            continue
        words = cue.get("words")
        sources = words if isinstance(words, list) and words else [cue]
        for item in sources:
            if not isinstance(item, dict):
                continue
            start, end = item.get("start_s"), item.get("end_s")
            if isinstance(start, bool) or isinstance(end, bool):
                continue
            if not isinstance(start, (int, float)) or not isinstance(end, (int, float)):
                continue
            start, end = float(start), float(end)
            if end > start >= 0:
                spans.append((start, end))
    merged: list[tuple[float, float]] = []
    for start, end in sorted(spans):
        if merged and start - merged[-1][1] < _SPEECH_WORD_BRIDGE_S:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return tuple(merged)


def _sfx_volume(
    volume: float,
    *,
    start_s: float,
    end_s: float,
    speech_windows: tuple[tuple[float, float], ...],
) -> float:
    overlap = sum(
        max(0.0, min(end_s, speech_end) - max(start_s, speech_start))
        for speech_start, speech_end in speech_windows
    )
    if overlap < _SFX_SPEECH_MIN_OVERLAP_S:
        return volume
    return round(volume * SFX_SPEECH_DUCK_GAIN, 4)
