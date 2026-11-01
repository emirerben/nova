"""Project shared approved decisions into a source-bound native render program.

No media is downloaded or rendered here. Unsupported lanes fail closed until
their native implementations and parity fixtures are available.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any, Literal

from app.kria.portable_visual import AudioMuteWindow
from app.kria.recipes import (
    AssetFingerprint,
    AudioMixRecipe,
    Canvas,
    MediaAsset,
    MediaSize,
    MediaTransform,
    NormalizedSourceCrop,
    TimelineClip,
    TimelineTrack,
    Transition,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import RenderAssetManifest, SongRenderAsset, VoiceoverRenderAsset
from app.pipeline.guided_story import (
    _FRAME_S,
    GuidedStoryExecutionPlan,
    _story_canvas,
    plan_preserves_source_audio,
)
from app.pipeline.phone_recipe_shared import (
    PhoneNarrationBed,
    PhoneSongBed,
    clip_tilt_fields,
    display_dims,
    face_fill_transform,
    fit_transform,
    register_backdrop_visuals,
    snap_text_overshoot,
    timeline_end_s,
)
from app.schemas.guided_edit_revision import GUIDED_EDITOR_FPS
from app.schemas.user_song import USER_SONG_REQUIRED_CAPABILITIES, USER_SONG_TRACK_ID
from app.services.phone_sources import (
    PhoneSourceBinding,
    PhoneVisualBinding,
    require_bound_moment,
    require_bound_visual,
)

if TYPE_CHECKING:
    from app.pipeline.phone_subtitled_lanes import (
        ResolvedSoundEffect,
        SubtitledOverlayCard,
        SubtitledSoundEffect,
    )


class UnsupportedPhonePlan(ValueError):
    """A device recipe cannot yet represent the full approved plan.

    ``capability`` names the ``MediaCapability`` this lane would require once
    the V2 recipe schema grows fields for it (see
    docs/reviews/kri-29/capability-matrix.md); it is informational only —
    fixing this by adding the capability to ``phone_render_verified_features``
    does nothing, since the underlying plan content never reaches a recipe.

    ``reason`` is a stable machine tag for rejects `app.pipeline.phone_plan_repair`
    classifies (``"sequence_effect"``, ``"transition_window"``); None otherwise.
    """

    def __init__(
        self, message: str, *, capability: str | None = None, reason: str | None = None
    ) -> None:
        super().__init__(message)
        self.capability = capability
        self.reason = reason


class UserSongPlanDeclined(UnsupportedPhonePlan):
    """The creator's own song could not be planned into an edit (KRI-466).

    The message is creator-facing copy (what to do next), stored on the job's
    ``error_detail`` and shown verbatim in chat under ``user_song_plan_declined``.
    """


class SilentAudioPlanDeclined(UnsupportedPhonePlan):
    """The talking take has no sound at all (KRI-653).

    A dead microphone records a valid audio track of zeros; captions and a speech cut over it
    only produce hallucinated words. Like ``UserSongPlanDeclined`` the message is creator-facing
    copy, stored on ``error_detail`` and shown verbatim in chat under ``silent_audio_declined``.
    """


# Plan lane -> the MediaCapability it would require once the recipe schema
# carries its content. Kept next to the reject loop so a new plan lane and
# its capability name are added together.
#
# `narration` is deliberately NOT here (KRI-132 phone-voiceover-gate
# follow-up): the guided-story narration lane now has a phone compiler --
# see the `plan.narration` handling below instead of the reject loop.
#
# Sound effects are deliberately NOT here either (KRI-608):
# `editor_sound_effects` compiles to an `sfx` audio track through
# `phone_subtitled_plan._compile_sfx_track` (the `sound_effects=` binding
# below). `licensed_sfx_intent` is provenance only -- the cloud renders just
# the `editor_sound_effects` the worker materialized from it -- so it needs
# no lane of its own. A plan that carries effects without a binding still
# fails closed (see `guided_sfx_requests`).
_UNSUPPORTED_PHONE_LANE_CAPABILITY: dict[str, str] = {
    "music": "musicBed",
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

# A lip-sync take's source start is `output_start + window_start - delta`. Plans
# round every position to a millisecond, so the recipe may differ from the exact
# value by rounding noise but never by more than this.
_LIPSYNC_SYNC_TOLERANCE_S = 0.0015

# Guided-editor v2 revisions quantize every position to the editor frame clock
# (`app.schemas.guided_edit_revision.GUIDED_EDITOR_FPS`, the same 1/30 s as
# guided_story's `_FRAME_S`); approval plans use a millisecond clock. A phone
# program may sit on either.


def _clock_aligned(value: float) -> bool:
    """True when `value` sits on the millisecond clock or the guided-editor frame clock."""
    return math.isclose(value, round(value, 3), abs_tol=1e-9) or math.isclose(
        value * GUIDED_EDITOR_FPS, round(value * GUIDED_EDITOR_FPS), abs_tol=1e-4
    )


# The device recipe's source bound (EditRecipeV2 and Models.swift reject a
# clip starting past it). Footage originals can't exceed it; pool videos can.
_DEVICE_SOURCE_LIMIT_S = 1800


def _still_tilt_fields(moment: Any) -> dict[str, Any]:
    """``transform`` / ``backdrop`` for a photo moment. A supporting card turns about its
    centre over its own blurred cover, so it takes the rotation only."""
    patch, backdrop = clip_tilt_fields(moment)
    fields: dict[str, Any] = {}
    if patch:
        fields["transform"] = MediaTransform(**patch)
    if backdrop is not None and moment.layout != "supporting_card":
        fields["backdrop"] = backdrop
    return fields


def _phone_source_crop(crop: dict[str, float] | None) -> NormalizedSourceCrop | None:
    """The plan's re-frame as a recipe crop; a malformed one fails closed."""
    if crop is None:
        return None
    try:
        return NormalizedSourceCrop.model_validate(crop)
    except ValueError as exc:
        raise UnsupportedPhonePlan("unsupported phone crop", capability="sourceCrop") from exc


def compile_phone_guided_plan(
    plan: GuidedStoryExecutionPlan,
    bindings: tuple[PhoneSourceBinding, ...],
    visuals: tuple[PhoneVisualBinding, ...] = (),
    *,
    narration: PhoneNarrationBed | None = None,
    allow_editor_media: bool = False,
    song: PhoneSongBed | None = None,
    sound_effects: tuple[ResolvedSoundEffect, ...] = (),
    landscape_fit: Literal["fill", "fit"] = "fill",
    timed_cards: tuple[SubtitledOverlayCard, ...] = (),
) -> EditRecipeV2:
    """``visuals`` pins approved Visuals-pool photos and videos (KRI-121). Callers
    bind each kind only while its feature (``stillImages`` / ``visualVideos``)
    is verified; without a binding, a pool moment fails closed exactly as before.

    ``narration`` (KRI-132 phone-voiceover-gate follow-up): required exactly
    when ``plan.narration`` is set -- resolved by the caller
    (`app.tasks.generative_build._run_phone_guided_job`, mirroring
    `_resolve_phone_voiceover_bed`'s use in the montage-family compiler) and
    re-checked here against the plan's own pinned identity so a voiceover
    replaced since approval fails closed instead of silently binding the
    wrong bytes. Compiles to a `VoiceoverRenderAsset` + a second
    `TimelineTrack(id="narration", kind="audio")`, mirroring
    `_mix_pinned_narration`'s cloud behavior exactly: the narration HARD-
    REPLACES the footage's own audio (`original_volume=0.0`, no ducking, no
    gain, no matched bed) -- see that function in `app.pipeline.guided_story`
    for why (`-map 1:a:0`, never `-filter_complex amix`).

    ``song`` (KRI-374): required exactly when ``plan.user_song`` is set; the
    creator's own uploaded song. Compiles to a `SongRenderAsset` + a
    `TimelineTrack(id="song", kind="audio")` whose clip starts at the plan's
    ``window_start_s`` of the song, and camera audio is muted
    (`original_volume=0.0`, `music_asset_id` = the song). Background fades
    0.5 s in/out; lip-sync 0.05 s in, 0.3 s out. A lip-sync take is never
    start-shifted by the on-device refit: only its END may be truncated, and a
    take whose start no longer fits fails closed.

    ``sound_effects`` (KRI-608): the catalog effects resolved by the caller
    (`_resolve_phone_sound_effect` on the worker, `lanes_from_editor_sections` on
    Save), required exactly when ``plan.editor_sound_effects`` is non-empty and
    re-checked against the plan's own request ids so a stale binding fails
    closed. Compiles to the same ``TimelineTrack(id="sfx", kind="audio")`` Talking
    plays and adds ``soundEffects`` + ``audioMix``. Mirrors the cloud mix
    (``_compose_guided_sfx``): trim + placement + gain, no speech duck.

    ``timed_cards`` (flag ``PHONE_TIMED_CARDS_ENABLED``, default ``()`` = byte-identical
    output): exact-timed small cards already grounded onto THIS plan's output clock by
    `app.pipeline.phone_timed_cards.ground_timed_cards` (the worker compiles once without
    them to learn where each clip really starts, grounds, then compiles again with them).
    Each card's Visual must be pinned in ``visuals``. Compiles to the same silent
    ``subtitled-overlays`` track Talking draws (`phone_subtitled_plan._compile_overlay_track`:
    caption-band clamp, ``visualBlocks`` + ``alphaOverlay`` + ``audioMix``, ``visualVideos``
    for a video card), clamped to the compiled duration.

    ``landscape_fit`` (KRI-285, ``"fill"`` default = unchanged output): ``"fit"``
    letterboxes a landscape (display-wide) video moment inside a PORTRAIT canvas
    with ``phone_recipe_shared.fit_transform`` instead of center-cropping it.
    Callers pass ``"fit"`` only when the creator explicitly chose black bars
    (``all_candidates["creator_render_shape"]``), so existing guided output
    never changes. Clips with a ``source_crop`` (the creator's own re-frame) or
    a ``look`` (the device throws on a look + transform), stills, and any
    landscape output canvas keep the identity transform. On ``"fill"`` a moment
    the worker framed for a "make it vertical" ask (KRI-1027,
    `phone_montage_framing`) slides by its ``fill_position_x`` to keep its face in
    the crop, or letterboxes alone when ``fill_letterbox`` (no crop holds the face).
    """
    # Import lazily: guided_story owns the shared caption-meta projection and
    # imports this compiler for its device path.
    from app.pipeline.generative_overlays import build_overlays_from_text_elements
    from app.pipeline.guided_caption_presentation import project_guided_caption_overlays
    from app.pipeline.guided_story import (
        _apply_guided_caption_meta,
        _assign_label_lane_roles,
        _tag_guided_text_overlays,
    )
    from app.pipeline.portable_text_layout import compile_text_overlay
    from app.pipeline.sequence_text_evidence import sequence_lineage

    for lane, capability in _UNSUPPORTED_PHONE_LANE_CAPABILITY.items():
        if lane == "editor_visual_blocks" and allow_editor_media:
            continue
        if getattr(plan, lane):
            raise UnsupportedPhonePlan(f"unsupported phone lane: {lane}", capability=capability)
    if plan.narration is not None:
        if narration is None:
            raise UnsupportedPhonePlan(
                "recorded voiceover requires a phone narration binding",
                capability="narrationAudio",
            )
        if (
            narration.generation != plan.narration.generation
            or abs(narration.duration_s - plan.narration.duration_s) > 0.05
        ):
            raise UnsupportedPhonePlan(
                "the approved voiceover was replaced since approval",
                capability="narrationAudio",
            )
    elif narration is not None:
        # A worker/plan mismatch: nothing in `plan.narration` asked for a
        # narration bed at all, so binding one here would silently attach
        # audio the approved plan never carried.
        raise UnsupportedPhonePlan(
            "phone narration binding was provided for a plan with no narration",
            capability="narrationAudio",
        )
    user_song = plan.user_song
    if user_song is not None:
        if song is None:
            raise UnsupportedPhonePlan(
                "a creator song requires a phone song binding", capability="musicBed"
            )
        if (
            song.plan_item_id != user_song.plan_item_id
            or song.generation != str(user_song.generation)
            or user_song.window_end_s > song.duration_s + 0.05
            or (
                song.window_start_s is not None
                and abs(song.window_start_s - user_song.window_start_s) > 0.001
            )
        ):
            raise UnsupportedPhonePlan(
                "the approved song was replaced since approval", capability="musicBed"
            )
    elif song is not None:
        raise UnsupportedPhonePlan(
            "phone song binding was provided for a plan with no creator song",
            capability="musicBed",
        )
    if plan.licensed_sfx_intent and not plan.editor_sound_effects:
        from app.services.phone_rollout import phone_guided_sfx_supported  # noqa: PLC0415

        # With the lane off the intent was never rendered, so keep failing closed rather
        # than silently dropping the ask. With it on, the worker materializes the intent
        # into placements before compiling (and raises when it can't ground one); the
        # planning-time dry run compiles the bare intent, which must not reject the plan.
        if not phone_guided_sfx_supported():
            raise UnsupportedPhonePlan(
                "unsupported phone lane: licensed_sfx_intent", capability="soundEffects"
            )
    sfx_requests = guided_sfx_requests(plan)
    if sorted(_sfx_key(resolved.request) for resolved in sound_effects) != sorted(
        _sfx_key(request) for request in sfx_requests
    ):
        # Either side missing (effects but no binding, a binding for a plan that has
        # none), or a bound effect whose identity, time, level or trim differs from the
        # plan's: never bind the wrong audio.
        raise UnsupportedPhonePlan(
            "the sound effects bound for the phone do not match the approved plan",
            capability="soundEffects",
        )
    lipsync_takes = user_song.takes if user_song is not None and user_song.mode == "lipsync" else {}
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
    preserve_audio = plan_preserves_source_audio(plan)
    # The creator's own level for the footage's sound (Sounds > Original audio). Only an
    # explicit, audible choice changes anything: unset keeps today's defaults.
    creator_original_level = plan.editor_original_level
    creator_hears_camera = creator_original_level is not None and creator_original_level > 0
    if creator_hears_camera:
        preserve_audio = True
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
    if len({visual.media_id for visual in visuals}) != len(visuals):
        raise ValueError("phone visuals must have unique media identities")
    for index, moment in enumerate(plan.story_timeline):
        # Visuals-pool media renders from its pinned pool bytes: a photo as a
        # still (fullscreen, or whole inside a supporting card), a video exactly
        # like bound footage. The native compositor throws on a look over a
        # still and has no zoom treatment, so anything else keeps failing closed.
        pooled = moment.lane == "asset"
        still = pooled and moment.kind == "image"
        visual = None
        binding = None
        if pooled:
            if not any(candidate.kind == moment.kind for candidate in visuals):
                raise UnsupportedPhonePlan(
                    "unsupported phone photo" if still else "unsupported phone visual video",
                    capability="stillImages" if still else "visualVideos",
                )
            visual = require_bound_visual(
                visuals,
                media_id=moment.media_id,
                path=moment.gcs_path,
                generation=moment.generation,
            )
            if visual.kind != moment.kind:
                raise ValueError("approved media does not match its pinned visual kind")
        else:
            binding = require_bound_moment(
                bindings,
                media_id=moment.media_id,
                path=moment.gcs_path,
                generation=moment.generation,
            )
        if still:
            if (
                moment.layout not in {"fullscreen", "supporting_card"}
                or moment.image_motion is not None
                or moment.look_preset != "none"
                or moment.look_adjustments
                or moment.playback_rate not in {None, 1}
                # A re-frame is applied to the whole photo; a card draws the
                # photo whole, so the two contradict each other.
                or (moment.source_crop is not None and moment.layout != "fullscreen")
            ):
                raise UnsupportedPhonePlan("unsupported phone photo treatment")
        else:
            # Constant speed is carried by the recipe, independently of the
            # approved source selection. Still images retain their own clock.
            if (
                moment.kind != "video"
                or moment.layout != "fullscreen"
                or moment.image_motion is not None
                or moment.look_preset not in {"none", "golden_hour"}
                or moment.look_adjustments
            ):
                raise UnsupportedPhonePlan("unsupported phone moment treatment")
            source = binding.original if binding is not None else visual
            if moment.look_preset == "golden_hour" and (
                (source.width, source.height) != (canvas.width, canvas.height)
                or source.orientation_degrees != 0
            ):
                raise UnsupportedPhonePlan("phone looks require exact-canvas unrotated sources")
        rate = float(moment.playback_rate if moment.playback_rate is not None else 1)
        if not math.isfinite(rate) or not 0.25 <= rate <= 4:
            raise UnsupportedPhonePlan(
                "unsupported phone playback rate", capability="variableSpeed"
            )
        if rate != 1 and moment.media_id in lipsync_takes:
            raise UnsupportedPhonePlan(
                "a lip-sync clip cannot change speed independently of its song"
            )
        source_duration = moment.source_end_s - moment.source_start_s
        # KRI-828: the creator's freeze holds the moment's last source frame after it plays.
        freeze_hold = float(moment.freeze_hold_s or 0.0)
        if freeze_hold and (still or rate != 1):
            # The engine holds a moving clip's last frame at 1x. A transition on either side
            # needs nothing extra: the incoming overlap rides the motion like any clip's, and
            # an outgoing one dissolves out of the held frame (the slot's own duration owns
            # both). A hold at another speed is a different program. A lip-sync take may hold
            # over song nobody filmed: the next take's own sync check below still applies.
            raise UnsupportedPhonePlan("a freeze frame needs a moving clip at normal speed")
        moving_duration = moment.duration_s - freeze_hold
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
        # A still has no source window: beat-snapped fast cuts keep the photo's
        # nominal source_end_s while duration_s moves, and cloud renders the
        # still for duration_s regardless.
        if (
            not math.isclose(
                moment.output_start_s, expected_start, abs_tol=_TIMING_ROUNDING_TOLERANCE_S
            )
            or (
                not still
                and not math.isclose(
                    source_duration / rate,
                    moving_duration,
                    abs_tol=_FRAME_S + _TIMING_ROUNDING_TOLERANCE_S,
                )
            )
            or not math.isclose(
                moment.output_end_s - moment.output_start_s,
                moment.duration_s,
                abs_tol=_TIMING_ROUNDING_TOLERANCE_S,
            )
        ):
            raise UnsupportedPhonePlan("phone moment timing must preserve its exact source window")
        if still:
            asset = visual.render_asset()
            manifest[asset.id] = asset
            assets[asset.id] = MediaAsset(
                id=asset.id,
                relative_path=asset.id,
                fingerprint=AssetFingerprint(hex=visual.sha256, byte_count=visual.byte_count),
            )
            clips.append(
                TimelineClip(
                    id=moment.moment_id,
                    source_asset_id=asset.id,
                    source_start=0,
                    source_duration=moment.duration_s,
                    timeline_start=moment.output_start_s,
                    rate=1,
                    transition=incoming,
                    still_layout="supporting_card" if moment.layout == "supporting_card" else None,
                    source_crop=_phone_source_crop(moment.source_crop),
                    **_still_tilt_fields(moment),
                )
            )
            cursor = moment.output_end_s
            continue
        if (
            moment.playback_rate is None
            and not freeze_hold
            and not math.isclose(
                source_duration, moment.duration_s, abs_tol=_TIMING_ROUNDING_TOLERANCE_S
            )
        ):
            # A v2 revision quantizes `duration_s` and an approval-inherited
            # `source_end_s` independently, so the two can disagree by one
            # frame (7.153 s -> 7.166667 s vs 7.133333 s). The output slot is
            # what the timeline, text and audio are timed against: fill it
            # from the source rather than leave a one-frame hole. The refit
            # below still shifts the window if the original runs out.
            source_duration = round(moment.duration_s * rate, 6)
        source_start = moment.source_start_s
        lipsync_take = lipsync_takes.get(moment.media_id)
        if lipsync_take is not None:
            # Lip-sync: the song sets where in the take this cut starts. The
            # recipe must keep `source_start - timeline_start == window - delta`
            # whatever the plan's rounding did.
            expected_offset = user_song.window_start_s - lipsync_take.delta_s
            if (
                abs((source_start - moment.output_start_s) - expected_offset)
                > _LIPSYNC_SYNC_TOLERANCE_S
            ):
                raise UnsupportedPhonePlan("a lip-sync clip has drifted off its song position")
            if moment.source_end_s > source.duration_s or source_start >= source.duration_s:
                # END-only refit. Shifting the start (what `refit_source_window`
                # does) would slide the singer's mouth off the audio.
                available = max(0.0, source.duration_s - _EXPORT_SAFETY_MARGIN_S)
                if available - source_start < 0.1:
                    raise UnsupportedPhonePlan(
                        "a lip-sync clip is shorter on this phone than when it was matched "
                        "to the song, so it can no longer stay in sync"
                    )
                refit_duration = round(min(source_duration, available - source_start), 6)
                # The export safety margin is an intended trim; anything beyond
                # one frame past it is footage the singer never filmed, and
                # trimming it would leave a hole while the song keeps playing.
                if source_duration - refit_duration - _EXPORT_SAFETY_MARGIN_S > _FRAME_S:
                    raise UnsupportedPhonePlan(
                        "a lip-sync clip runs past the end of its footage, so it can no "
                        "longer stay in sync with the song"
                    )
                source_duration = refit_duration
        elif moment.source_end_s > source.duration_s or source_start >= source.duration_s:
            if freeze_hold:
                # The frozen frame is the creator's moment: never slide it. A device file
                # a hair shorter than the proxy trims the end by at most the export margin
                # plus a frame; anything more needs a new selection.
                available = max(0.0, source.duration_s - _EXPORT_SAFETY_MARGIN_S)
                if (
                    source_start + source_duration - available > _FRAME_S + _EXPORT_SAFETY_MARGIN_S
                    or available - source_start < 0.1
                ):
                    raise UnsupportedPhonePlan("a freeze frame must stay on its selected frame")
                source_duration = round(available - source_start, 6)
            elif moment.playback_rate is not None:
                # Never slide an explicitly selected action to compensate for a
                # different device duration, including an explicit reset to 1x.
                # A tiny end-only measurement mismatch
                # is safe; a material mismatch requires a new selection.
                available = max(0.0, source.duration_s - _EXPORT_SAFETY_MARGIN_S)
                deficit = source_start + source_duration - available
                if deficit > rate * _FRAME_S + _EXPORT_SAFETY_MARGIN_S:
                    raise UnsupportedPhonePlan(
                        "phone speed must preserve its selected source window"
                    )
                if available <= source_start:
                    raise UnsupportedPhonePlan(
                        "phone speed must preserve its selected source window"
                    )
                source_duration = round(min(source_duration, available - source_start), 6)
            else:
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
                available = max(0.0, source.duration_s - _EXPORT_SAFETY_MARGIN_S)
                fitted_duration = min(source_duration, available)
                if fitted_duration < 0.1:
                    raise UnsupportedPhonePlan(
                        "phone moment timing must preserve its exact source window"
                    )
                source_start = min(source_start, max(0.0, available - fitted_duration))
                source_duration = round(fitted_duration, 6)
        if pooled and source_start + source_duration > _DEVICE_SOURCE_LIMIT_S:
            # Named up front rather than as the recipe's own validation error.
            raise UnsupportedPhonePlan("phone visual video window is past the 30-minute limit")
        asset = (binding or visual).render_asset()
        manifest[asset.id] = asset
        assets[asset.id] = MediaAsset(
            id=asset.id,
            relative_path=asset.id,
            fingerprint=AssetFingerprint(hex=source.sha256, byte_count=source.byte_count),
            # Informational on device (the engine reads the file's own track);
            # a long pool recording must not trip the asset's 30-minute bound.
            duration=source.duration_s if source.duration_s <= _DEVICE_SOURCE_LIMIT_S else None,
            natural_size=MediaSize(width=source.width, height=source.height),
            orientation_degrees=source.orientation_degrees,
            # `PhoneSourceBinding.require_proxy` already guarantees a reserved
            # analysis proxy exists for every binding reaching this compiler —
            # accurately reflect that on the asset rather than leaving the
            # field at its always-false default. No client consumes this yet
            # (proxy-based local preview is KRI-95's job); this only makes the
            # recipe describe reality. A pool video has no proxy: its full
            # bytes are already on the server.
            is_proxy_available=binding is not None,
        )
        source_crop = _phone_source_crop(moment.source_crop)
        transform = MediaTransform()
        if (
            canvas.height > canvas.width
            and source_crop is None
            and moment.look_preset != "golden_hour"
        ):
            if landscape_fit == "fit" or moment.fill_letterbox:
                display_w, display_h = display_dims(source)
                transform = fit_transform(display_w, display_h, canvas, "fit")
            elif moment.fill_position_x:
                # KRI-1027: the face-following crop (`phone_montage_framing`), the same
                # clamped sideways shift the Talking face fill uses (KRI-547).
                display_w, display_h = display_dims(source)
                transform = face_fill_transform(
                    display_w, display_h, canvas, moment.fill_position_x
                )
        tilt_patch, backdrop = clip_tilt_fields(moment)
        if backdrop is not None or tilt_patch:
            if moment.look_preset == "golden_hour":
                raise UnsupportedPhonePlan(
                    "a color grade cannot be combined with a tilted clip", capability="clipTilt"
                )
            transform = transform.model_copy(update=tilt_patch)
        hold_duration = None
        if freeze_hold:
            # The source window plays at 1x, then its last frame holds for the rest of the
            # slot: the creator's seconds plus any end the device file trimmed.
            hold_duration = round(moment.duration_s - source_duration, 6)
        elif moment.playback_rate is not None:
            # The saved source edges remain exact; the render clock is whole
            # frames. Never read beyond that selection or overlap the next
            # slot: trim at most one output frame, or hold its final frame.
            source_duration = min(source_duration, moment.duration_s * rate)
            hold = max(0.0, moment.duration_s - source_duration / rate)
            hold_duration = hold if hold > _TIMING_ROUNDING_TOLERANCE_S else None
        clips.append(
            TimelineClip(
                id=moment.moment_id,
                source_asset_id=asset.id,
                source_start=source_start,
                source_duration=source_duration,
                timeline_start=moment.output_start_s,
                rate=rate,
                hold_duration=hold_duration,
                transform=transform,
                transition=incoming,
                look="golden_hour" if moment.look_preset == "golden_hour" else None,
                source_crop=source_crop,
                backdrop=backdrop,
                # The creator silenced this occurrence's own sound; a default clip stays
                # byte-identical (volume unset).
                **({"volume": 0.0} if moment.source_audio_muted else {}),
            )
        )
        cursor = moment.output_end_s
    if not math.isclose(cursor, plan.resolved_duration_s, abs_tol=_TIMING_ROUNDING_TOLERANCE_S):
        raise ValueError("phone timeline duration differs from approved plan")
    if len({clip.id for clip in clips}) != len(clips):
        raise ValueError("phone moments must have unique identities")
    register_backdrop_visuals(clips, visuals=visuals, assets=assets, manifest=manifest)
    # Composition.swift throws invalidTimeline unless the outgoing clip still
    # plays through each incoming fade. The per-moment refit above routinely
    # saturates a clip to the device file's own measured length minus
    # `_EXPORT_SAFETY_MARGIN_S` (0.05s), while `transition.duration` was set
    # from the plan's nominal, pre-refit overlap -- a shortfall of tens of
    # milliseconds is the expected shape of that disagreement, not a broken
    # plan (observed live: job 31eb638e, KRI-163). Refusing the whole render
    # over a shave this small throws away the moment for no benefit: shorten
    # the fade to whatever overlap the refit clip actually has, rounded down
    # to a whole frame so Composition.swift's own frame-quantized timeline
    # still contains it. Only a genuine hole -- the previous clip doesn't even
    # reach where this one starts, a source dramatically shorter than
    # planned rather than a rounding disagreement -- still fails closed.
    previous_end = None
    for clip in sorted(clips, key=lambda clip: clip.timeline_start):
        if clip.transition is not None:
            available = round((previous_end or 0.0) - clip.timeline_start, 6)
            if previous_end is None or available <= 1e-6:
                raise UnsupportedPhonePlan(
                    "phone transition needs the full source window", reason="transition_window"
                )
            if available + 1e-6 < clip.transition.duration:
                fitted = math.floor((available + 1e-9) / _FRAME_S) * _FRAME_S
                clip.transition = (
                    clip.transition.model_copy(update={"duration": fitted})
                    if fitted >= _FRAME_S
                    else None
                )
        previous_end = (
            clip.timeline_start + clip.source_duration / clip.rate + (clip.hold_duration or 0)
        )
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
        (
            clip.timeline_start + clip.source_duration / clip.rate + (clip.hold_duration or 0)
            for clip in clips
        ),
        default=0.0,
    )
    # Millisecond rounding noise between independently-rounded timing fields
    # is expected and already tolerated everywhere else in this function (see
    # `_TIMING_ROUNDING_TOLERANCE_S`'s docstring above) -- it must not, by
    # itself, be treated as a refit shrink. Only clamp text when the refit
    # actually shortened the compiled timeline by more than that noise floor;
    # otherwise every ordinary plan (no clip ever needed a refit) stays
    # byte-identical, including a title that legitimately holds to exactly
    # `plan.resolved_duration_s`.
    # Source refits already reserve the file-reading safety margin. Text must
    # reach the resulting picture end so a confirmed closing stays visible.
    text_bound_s = (
        compiled_duration
        if compiled_duration < plan.resolved_duration_s - _TIMING_ROUNDING_TOLERANCE_S
        else None
    )
    caption_elements, hidden_caption_ids = _apply_guided_caption_meta(
        list(plan.text_elements), plan.editor_caption_meta
    )
    layers = []
    ordered_overlays = []
    source_elements_by_id = {}
    for lane, elements in (
        ("text", [element for element in caption_elements if element.id not in hidden_caption_ids]),
        ("context", plan.context_label_text_elements),
        ("narration", plan.narration_label_text_elements),
    ):
        source_elements_by_id.update({element.id: element for element in elements})
        overlays = build_overlays_from_text_elements(
            elements, video_duration_s=plan.resolved_duration_s, independent_box_alignment=True
        )
        overlays = _tag_guided_text_overlays(overlays, elements)
        if lane == "text":
            overlays = project_guided_caption_overlays(overlays, plan.editor_caption_meta)
        else:
            # Same role rewrite as the cloud burn: context/narration labels
            # render as independent overlays, never inside the sequence
            # composite (so a pop-in topic/score label is not held to its
            # effect fence below).
            overlays = _assign_label_lane_roles(overlays, lane)
        ordered_overlays.extend(
            (f"{lane}-{index}", overlay) for index, overlay in enumerate(overlays)
        )
    # Production renders non-sequence inputs first, then the sequence composite.
    # Keep stable order within each partition. After the label-lane role rewrite
    # only genuine text-lane sequence blocks sort last, so labels draw beneath
    # them exactly as in cloud.
    ordered_overlays.sort(key=lambda entry: entry[1].get("role") == "generative_sequence")
    for index, (layer_id, overlay) in enumerate(ordered_overlays):
        if overlay.get("role") == "generative_sequence" and overlay.get("effect", "none") not in {
            "fade-in",
            "static",
            "none",
            "handwriting",
            "ink-reveal",
        }:
            raise UnsupportedPhonePlan(
                "sequence effect needs composite-stream parity", reason="sequence_effect"
            )
        source_id = overlay.get("element_id")
        source_element = source_elements_by_id.get(source_id)
        lineage = (
            sequence_lineage(
                {"element_id": source_element.id, "source_params": source_element.source_params}
            )
            if source_element is not None
            else None
        )
        # Sequence children need their compiler-owned lineage in the portable
        # recipe so the phone contract can compose them. Ordinary layers retain
        # their historical positional ids.
        compiled_layer_id = f"text-{source_id}" if lineage is not None else layer_id
        layer, font = compile_text_overlay(
            overlay, layer_id=compiled_layer_id, canvas=canvas, dissolve_seed=101 + index * 37
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
    else:
        # No refit shrink, but a layer that ends at the plan's nominal end can still
        # overshoot the recipe's own duration by float noise: the millisecond-rounded
        # cuts 23.531 + 1.467 sum to 24.997999999999998, just under a layer that ends
        # at 24.998, and `EditRecipeV2` compares with a strict `>` (KRI-190 device
        # test, job 5df2e3ec). Snap only such an overshoot, so every layer that
        # already fits stays byte-identical.
        snap_text_overshoot(layers, timeline_end_s(clips))
    tracks = [TimelineTrack(id="story", kind="video", clips=clips)]
    if plan.editor_visual_blocks:
        from app.pipeline.phone_editor_visuals import (  # noqa: PLC0415
            UnsupportedEditorMedia,
            compile_editor_media_track,
        )

        if not allow_editor_media:
            raise UnsupportedPhonePlan(
                "unsupported phone lane: editor_visual_blocks", capability="visualBlocks"
            )
        try:
            tracks.append(
                compile_editor_media_track(
                    plan.editor_visual_blocks,
                    visuals=visuals,
                    timeline_duration_s=compiled_duration,
                    assets=assets,
                    manifest=manifest,
                )
            )
        except UnsupportedEditorMedia as exc:
            raise UnsupportedPhonePlan(str(exc), capability="visualBlocks") from exc
    audio = AudioMixRecipe(
        original_volume=(
            creator_original_level
            if creator_original_level is not None and preserve_audio
            else plan.editor_audio_level
            if preserve_audio
            else 0
        )
    )
    required_capabilities = (
        {"basicComposition", "local1080Export"}
        | ({"variableSpeed"} if any(clip.rate != 1 for clip in clips) else set())
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
        | ({"audioMix"} if preserve_audio else set())
        # Match the native engine's content-derived requirements for every
        # silent overlay track, including a full-frame opaque media block.
        | ({"visualBlocks", "alphaOverlay", "audioMix"} if plan.editor_visual_blocks else set())
    )
    if plan.narration is not None:
        # `narration is not None` always holds here (the checks at the top of
        # this function raise otherwise) -- spelled out again for clarity.
        narration_asset = VoiceoverRenderAsset(
            id=f"voiceover-{narration.plan_item_id}",
            plan_item_id=narration.plan_item_id,
            generation=narration.generation,
            fingerprint=narration.fingerprint,
        )
        manifest[narration_asset.id] = narration_asset
        assets[narration_asset.id] = MediaAsset(
            id=narration_asset.id,
            relative_path=narration_asset.id,
            fingerprint=AssetFingerprint(
                hex=narration.fingerprint.sha256, byte_count=narration.fingerprint.byte_count
            ),
            duration=narration.duration_s,
        )
        # Bound to the timeline this recipe actually compiled (mirrors
        # `compile_phone_voiceover_montage_plan`'s `total_duration_s` clamp): an
        # on-device refit can shrink `compiled_duration` below the plan's
        # nominal `resolved_duration_s`, and the audio must never outlast the
        # video track it plays under.
        narration_duration_s = max(0.1, min(narration.duration_s, max(compiled_duration, 0.1)))
        tracks.append(
            TimelineTrack(
                id="narration",
                kind="audio",
                clips=[
                    TimelineClip(
                        id="narration-voice",
                        source_asset_id=narration_asset.id,
                        source_start=0.0,
                        source_duration=narration_duration_s,
                        timeline_start=0.0,
                        rate=1.0,
                        volume=1.0,
                    )
                ],
            )
        )
        # `_mix_pinned_narration` (guided_story.py) HARD-REPLACES the
        # footage's own audio with the pinned narration (`-map 1:a:0`, no
        # ducking, no gain, no matched original-audio bed) regardless of
        # `plan.montage_audio.preserve_source_audio` -- mirror that exactly
        # rather than the `preserve_audio`-derived value above, which is for
        # the no-narration case only.
        audio = AudioMixRecipe(original_volume=0.0, narration_asset_id=narration_asset.id)
        required_capabilities |= {"narrationAudio", "audioMix"}
    if user_song is not None:
        # `song is not None` always holds here (checked at the top).
        song_asset = SongRenderAsset(
            id=f"song-{song.plan_item_id}",
            plan_item_id=song.plan_item_id,
            generation=song.generation,
            fingerprint=song.fingerprint,
        )
        manifest[song_asset.id] = song_asset
        assets[song_asset.id] = MediaAsset(
            id=song_asset.id,
            relative_path=song_asset.id,
            fingerprint=AssetFingerprint(
                hex=song.fingerprint.sha256, byte_count=song.fingerprint.byte_count
            ),
            duration=song.duration_s,
        )
        # The song never outlasts the video it plays under (a refit can shrink
        # the compiled timeline below the plan's nominal duration).
        song_start_s = user_song.window_start_s
        song_duration_s = round(
            max(
                0.1,
                min(user_song.window_duration_s, compiled_duration, song.duration_s - song_start_s),
            ),
            6,
        )
        lipsync = user_song.mode == "lipsync"
        fade_in = song.fade_in_s if song.fade_in_s is not None else (0.05 if lipsync else 0.5)
        fade_out = song.fade_out_s if song.fade_out_s is not None else (0.3 if lipsync else 0.5)
        if (
            not lipsync
            and song.fade_out_s is None
            and song_duration_s < compiled_duration - 0.05
            and song_start_s + song_duration_s < song.duration_s - 0.05
        ):
            # KRI-561: the creator stopped the music before both the video and the song end,
            # so it fades out instead of cutting dead (a song that simply runs out keeps 0.5 s).
            fade_out = 1.5
        tracks.append(
            TimelineTrack(
                id=USER_SONG_TRACK_ID,
                kind="audio",
                clips=[
                    TimelineClip(
                        id="song-bed",
                        source_asset_id=song_asset.id,
                        source_start=song_start_s,
                        source_duration=song_duration_s,
                        timeline_start=0.0,
                        rate=1.0,
                        volume=song.volume,
                        audio_fade_in=min(fade_in, song_duration_s / 2),
                        audio_fade_out=min(fade_out, song_duration_s / 2),
                    )
                ],
            )
        )
        # The song REPLACES the footage's own audio (like pinned narration): a
        # lip-sync take's own sound would double the singer, and a montage with
        # a chosen song is that song.
        # `music_asset_id` stays: the device uses it to resolve the pinned song asset. But
        # the engine ALSO plays that asset as its own bed from 0s at `music_volume`, on top
        # of the `song-bed` track above (which starts at the creator's chosen window). Left
        # at the default 1.0 the song played twice at once (prod job 934811f3, KRI-481).
        # The audible gain lives on the track clip, so the legacy bed is silenced.
        # An explicit creator level (Sounds > Original audio) lets the camera play WITH the song
        # at that level; the song keeps its own level on the `song-bed` clip, and a lip-sync
        # take's timing is untouched (only the camera gain changes). Unset = silent, as before.
        audio = AudioMixRecipe(
            original_volume=float(creator_original_level) if creator_hears_camera else 0.0,
            music_asset_id=song_asset.id,
            music_volume=0.0,
        )
        required_capabilities |= set(USER_SONG_REQUIRED_CAPABILITIES)
        if creator_hears_camera:
            required_capabilities |= {"audioMix"}
    mute_windows = _source_mute_windows(plan.story_timeline, clips)
    if mute_windows and audio.original_volume > 0:
        # KRI-942: part of a clip's own sound turned off in chat. The recipe validator adds
        # the capability the engine's mute windows need (`visualBlocks`).
        audio = audio.model_copy(update={"mute_windows": mute_windows})
    if sound_effects:
        # Imported lazily: phone_subtitled_plan imports UnsupportedPhonePlan from here.
        from app.pipeline.phone_subtitled_plan import _compile_sfx_track  # noqa: PLC0415

        # `compiled_duration`, not the plan's nominal duration: an on-device refit can
        # shrink the timeline, and an effect must never outlast the video it plays on.
        sfx_track = _compile_sfx_track(
            list(sound_effects),
            timeline_end=compiled_duration,
            assets=assets,
            manifest=manifest,
        )
        if sfx_track.clips:
            tracks.append(sfx_track)
            required_capabilities |= {"soundEffects", "audioMix"}
    if timed_cards:
        from app.pipeline.phone_subtitled_plan import _compile_overlay_track  # noqa: PLC0415

        overlay_track, has_video_card = _compile_overlay_track(
            list(timed_cards),
            visuals=visuals,
            speaker_end=compiled_duration,
            assets=assets,
            manifest=manifest,
        )
        if overlay_track.clips:
            tracks.append(overlay_track)
            required_capabilities |= {"visualBlocks", "alphaOverlay", "audioMix"}
            required_capabilities |= {"visualVideos"} if has_video_card else {"stillImages"}
    return EditRecipeV2(
        canvas=Canvas(width=canvas.width, height=canvas.height),
        assets=list(assets.values()),
        asset_manifest=RenderAssetManifest(assets=tuple(manifest.values())),
        tracks=tracks,
        text_layers=layers,
        audio=audio,
        required_capabilities=required_capabilities,
    )


def _source_mute_windows(moments, clips: list[TimelineClip]) -> list[AudioMuteWindow]:  # noqa: ANN001
    """Each moment's ``source_audio_mute_spans`` on the compiled clock (KRI-942).

    A span is in the clip's source seconds; it lands where the COMPILED clip (after the
    device refit) plays those seconds, clamped to the clip's own sound. A sliver shorter
    than a frame is dropped.
    """
    by_id = {clip.id: clip for clip in clips}
    windows: list[AudioMuteWindow] = []
    for moment in moments:
        clip = by_id.get(moment.moment_id)
        if clip is None or not moment.source_audio_mute_spans:
            continue
        start = clip.timeline_start
        end = start + clip.source_duration / clip.rate
        for source_start, source_end in moment.source_audio_mute_spans:
            lo = max(start, start + (source_start - clip.source_start) / clip.rate)
            hi = min(end, start + (source_end - clip.source_start) / clip.rate)
            if hi - lo >= _FRAME_S:
                windows.append(
                    AudioMuteWindow(start=round(lo, 6), end=round(hi, 6), clip_ids=[clip.id])
                )
    return windows[:100]


def _sfx_key(request: SubtitledSoundEffect) -> tuple:
    return (
        request.id,
        request.catalog_id,
        round(request.at_s, 3),
        round(request.volume, 3),
        request.trim_start_s,
        request.trim_end_s,
    )


def guided_sfx_requests(plan: GuidedStoryExecutionPlan) -> list[SubtitledSoundEffect]:
    """The plan's ``editor_sound_effects`` as phone lane requests (KRI-608).

    Rows are ``SoundEffectPlacement`` dicts (``gain`` becomes ``volume``). The phone
    plays catalog effects only: a row with no ``sound_effect_id`` (a creator upload)
    fails closed rather than being silently dropped.
    """
    from app.pipeline.phone_subtitled_lanes import SubtitledSoundEffect  # noqa: PLC0415

    requests: list[SubtitledSoundEffect] = []
    for row in plan.editor_sound_effects:
        catalog_id = row.get("sound_effect_id")
        if not isinstance(catalog_id, str) or not catalog_id.strip():
            raise UnsupportedPhonePlan(
                "a custom sound effect can't play on the iPhone yet; pick one from the library",
                capability="soundEffects",
            )
        try:
            requests.append(
                SubtitledSoundEffect(
                    id=str(row.get("id") or ""),
                    catalog_id=catalog_id,
                    at_s=float(row.get("at_s") or 0.0),
                    volume=float(row.get("gain", 1.0)),
                    trim_start_s=row.get("trim_start_s"),
                    trim_end_s=row.get("trim_end_s"),
                )
            )
        except (TypeError, ValueError) as exc:
            raise UnsupportedPhonePlan(
                "a sound effect in this plan can't be rendered on the iPhone",
                capability="soundEffects",
            ) from exc
    return requests
