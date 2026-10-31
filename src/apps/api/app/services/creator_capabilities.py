"""Server-owned capability resolution for the Main Creator Agent.

This module only describes what is available for a snapshot.  It does not mint
storage URLs, mutate rows, dispatch Celery work, or execute agent commands.
Execution remains the responsibility of a separately authenticated route.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from typing import Any

from app.agents._schemas.creator_agent import (
    FULLSCREEN_OVERLAY_DISPLAYS,
    BackgroundMedia,
    CapabilityAvailability,
    CreativeStrategy,
    CreatorCatalogRef,
    CreatorEditPlan,
    CreatorEditSnapshot,
    CreatorLimits,
    CreatorMediaRef,
    CreatorNarrationIdentity,
    DispatchRenderCommand,
    DraftGuidedProposalCommand,
    ResolvedCreatorManifest,
    SetItemIntentCommand,
    UserSongFacts,
    canonical_context_hash,
    canonical_manifest_hash,
)
from app.agents._schemas.creator_policy import (
    CAPABILITY_DRAFT_GUIDED_PROPOSAL,
    CAPABILITY_GUIDED_VOICEOVER,
    CAPABILITY_PHONE_SOURCE_AUDIO,
    CAPABILITY_PHONE_STILL_IMAGES,
    CAPABILITY_PHONE_VISUAL_VIDEOS,
    CAPABILITY_USER_SONG,
    GUIDED_VOICEOVER_NATIVE_ONLY_CODE,
    MAX_MAIN_CREATOR_SELECTED_MEDIA,
    MixedMediaTimingUnavailableError,
    MontageCadenceUnavailableError,
    UserSongUnavailableError,
    effective_render_program,
    native_voiceover_all_scope,
    normalize_creator_strategy_media,
    own_video_ids,
    repair_creator_strategy_shape,
    repair_creator_user_song,
    repair_creator_voice_mode,
)
from app.agents._schemas.edit_format import (
    CLIP_INTENT_FREE_EDIT_FORMATS,
    EDIT_FORMATS,
    GUIDED_EDIT_FORMATS,
    NARRATED_EDIT_FORMATS,
    coerce_edit_format,
    guided_edit_applicable,
    render_program_for_intent,
)
from app.config import settings
from app.schemas.edit_proposal import MAX_CREATOR_PINNED_TEXTS
from app.services.creator_errors import CreatorCapabilityError, CreatorStrategyError
from app.services.phone_destination import phone_drawable_visual_kinds
from app.services.phone_rollout import (
    TALKING_MULTI_CLIP_MAX_CLIPS,
    phone_guided_narration_supported,
    phone_guided_sfx_supported,
    phone_narrated_title_supported,
    phone_render_supported_formats,
    phone_subtitled_closing_title_supported,
    phone_subtitled_overlays_supported,
    phone_subtitled_reaction_beats_supported,
    phone_subtitled_title_supported,
    phone_subtitled_video_overlays_supported,
    phone_talking_head_supported,
    phone_talking_multi_clip_supported,
    phone_timed_cards_supported,
    phone_timed_video_cards_supported,
    phone_user_song_supported,
    phone_voiceover_reaction_beats_supported,
    speaker_background_supported,
)

CAPABILITY_SET_ITEM_INTENT = "set_item_intent"
CAPABILITY_GUIDED_STORY = "guided_story"
CAPABILITY_NATIVE_RENDER = "native_render"
CAPABILITY_DISPATCH_RENDER = "dispatch_render"
CAPABILITY_SELECT_READY_VARIANT = "select_ready_variant"
CAPABILITY_CAPTION_STYLE = "caption_style"
CAPABILITY_AUTOMATIC_CUT = "automatic_cut"
# KRI-178: name/word-triggered photo/sticker + sound reaction beats on a
# phone-rendered `subtitled` (Talking) edit.
CAPABILITY_REACTION_BEATS = "reaction_beats"
# KRI-183: VIDEO Visuals eligible as (muted, trimmed) picture-in-picture
# cards on a phone-rendered `subtitled` (Talking) edit -- a sibling signal
# to `media_overlays` (KRI-176), which covers photo cards only. Only ever
# set on the manifest when true; see the KRI-183 comment at its call site
# in `resolve_creator_manifest` for why it is omitted, not `_unavailable`,
# otherwise.
CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS = "media_overlays:video_cards"
# KRI-297: ALL Visuals as a full-screen cutaway sequence on a phone `subtitled`
# edit. Omitted (not `_unavailable`) when off so flag-off manifests stay
# byte-identical.
CAPABILITY_MEDIA_OVERLAY_FULLSCREEN = "media_overlays:fullscreen"
# Exact-timed small cards (flag PHONE_TIMED_CARDS_ENABLED): a Visuals photo (or video, where
# `phone_timed_video_cards_supported()`) as a picture-in-picture card at a stated second or
# clip of a phone Montage (guided / unified / voiceover montage) or narrated Voiceover edit.
# Present in `capabilities` ONLY while the flag is on, so a flag-off manifest's dict and its
# `context_hash` / `manifest_hash` stay byte-identical. Never the generic `media_overlays`
# key: that still gates the cloud `overlays` treatment the phone compiler rejects.
CAPABILITY_TIMED_CARDS = "timed_cards"
TIMED_CARDS_UNAVAILABLE_NOTICE = (
    "Small cards at an exact time aren't available for this edit yet; left them out."
)
# KRI-1117: a photo or looping video behind the speaker of a phone Talking edit. Present in
# `capabilities` ONLY while the gate (`speaker_background_supported()`, the phone media lanes)
# is open on a phone `subtitled` manifest, so every other manifest and hash stays byte-identical.
CAPABILITY_SPEAKER_BACKGROUND = "speaker_background"
# The reason a background (the creator's pick in the camera, or the agent's
# `background_media`) is not kept. The creator reads it before approving; the Turkish twins
# live in `app.kria.strategy_policy._KNOWN_TR`.
SPEAKER_BACKGROUND_NOTICES: dict[str, str] = {
    "unavailable": (
        "Couldn't put that behind you: a photo or video background only works on a "
        "talking-to-camera edit rendered on your iPhone, and it isn't available here right now."
    ),
    "not_found": (
        "Couldn't find the photo or video you named for the background; kept your own background."
    ),
    "pick_not_found": (
        "Couldn't use the background you picked in the camera: it is no longer in this "
        "edit's photos and videos; kept your own background."
    ),
    "kind_off": ("Couldn't use that as the background: this iPhone can't draw it behind you yet."),
}
TALKING_CLIP_INTENTS_DROPPED_NOTICE = (
    "Talking edits caption what you say, so I didn't add separate labels or captions "
    "to individual clips."
)
# Fixed wording on purpose (KRI-667): the time and the photo's name live in the receipt
# (`brief_checks._check_timed_cards`), so the notice needs no variable parts and the Turkish
# twin is an exact `_KNOWN_TR` lookup rather than a regex.
TIMED_CARD_VISUAL_NOT_FOUND_NOTICE = (
    "Couldn't find the photo or video you named for a small card; left it out."
)
TIMED_VIDEO_CARD_UNAVAILABLE_NOTICE = (
    "Videos can't show as small cards in this edit yet; left that card out."
)


class CreatorSfxUnavailableError(CreatorStrategyError):
    """An explicit licensed-SFX request cannot be fulfilled safely."""


_FEATURE_SETTINGS = {
    "main_creator_agent": "main_creator_agent_enabled",
    "execution": "main_creator_agent_execution_enabled",
    "review": "main_creator_agent_review_enabled",
    "auto_iteration": "main_creator_agent_auto_iteration_enabled",
    "guided_conversation": "guided_edit_conversation_enabled",
    "guided_direction_confirmation": "guided_edit_direction_confirmation_enabled",
    "guided_auto_design": "guided_auto_design_enabled",
    "transitions": "edit_transitions_enabled",
    "wide_looks": "edit_wide_looks_enabled",
    "media_overlays": "media_overlays_enabled",
    "visual_blocks": "visual_blocks_enabled",
    "motion_scenes": "motion_scenes_enabled",
    "sound_effects": "sound_effects_enabled",
}

# Treatments compile_phone_guided_plan rejects: the overlay, visual-block and
# motion-scene lanes outright (SFX only while `phone_guided_sfx_supported()` is off, the
# one override applied below, KRI-608), and an edit-wide look on any source that is not
# exact-canvas golden_hour. A phone manifest must not advertise them, so the
# request is refused while planning instead of failing the device render later.
# `media_overlays` is the one exception: a `subtitled` (Talking) phone manifest
# overrides this default back to available once
# `phone_rollout.phone_subtitled_overlays_supported()` holds (KRI-176) --
# every other phone recipe (guided/montage family, narrated) keeps the
# blanket refusal below unconditionally.
_PHONE_UNSUPPORTED_CAPABILITIES = (
    "sound_effects",
    "media_overlays",
    "motion_scenes",
    "wide_looks",
)


def _available() -> CapabilityAvailability:
    return CapabilityAvailability(available=True)


def _unavailable(code: str, detail: str) -> CapabilityAvailability:
    return CapabilityAvailability(available=False, reason_code=code, reason=detail)


def _format_availability(edit_format: str, *, has_voiceover: bool) -> CapabilityAvailability:
    """Resolve real assembler availability; fallback-in-worker is not a capability."""

    if edit_format == "day_vlog":
        if not settings.edit_format_day_vlog_enabled:
            return _unavailable(
                "disabled_by_setting",
                "day_vlog is disabled by the server (EDIT_FORMAT_DAY_VLOG_ENABLED)",
            )
        if not settings.NARRATIVE_CLIP_ORDER_ENABLED:
            return _unavailable(
                "chronology_disabled",
                "day_vlog requires chronological filming-guide ordering "
                "(NARRATIVE_CLIP_ORDER_ENABLED)",
            )
        if has_voiceover:
            return _unavailable(
                "native_render_required",
                "day_vlog uses the guided renderer and cannot carry a voiceover",
            )
        return _available()
    if edit_format == "single_hero":
        if not settings.edit_format_single_hero_enabled:
            return _unavailable(
                "disabled_by_setting",
                "single_hero is disabled by the server (EDIT_FORMAT_SINGLE_HERO_ENABLED)",
            )
        if has_voiceover:
            return _unavailable(
                "native_render_required",
                "single_hero uses the guided renderer and cannot carry a voiceover",
            )
        return _available()
    if edit_format == "montage":
        return _available()
    if edit_format == "talking_head" and settings.edit_format_talking_head_enabled:
        return _available()
    if edit_format == "subtitled" and settings.subtitled_archetype_enabled:
        return _available()
    if edit_format in {"narrated", "narrated_planned", "narrated_ready"}:
        if settings.narrated_archetype_enabled and (
            has_voiceover or settings.narrated_self_narration_enabled
        ):
            return _available()
    return _unavailable(
        "format_unavailable",
        f"{edit_format} is disabled or has no production renderer for this media snapshot",
    )


def _as_media_refs(values: Sequence[CreatorMediaRef | Mapping[str, Any]]) -> list[CreatorMediaRef]:
    return [
        value if isinstance(value, CreatorMediaRef) else CreatorMediaRef.model_validate(value)
        for value in values
    ]


def _as_catalog_refs(
    values: Sequence[CreatorCatalogRef | Mapping[str, Any]],
) -> list[CreatorCatalogRef]:
    return [
        value if isinstance(value, CreatorCatalogRef) else CreatorCatalogRef.model_validate(value)
        for value in values
    ]


def resolve_creator_manifest(
    *,
    item_id: str,
    edit_format: object = None,
    has_voiceover: bool = False,
    current_edit: CreatorEditSnapshot | Mapping[str, Any] | None = None,
    media: Sequence[CreatorMediaRef | Mapping[str, Any]] = (),
    catalog: Sequence[CreatorCatalogRef | Mapping[str, Any]] = (),
    has_ready_variant: bool = False,
    limits: CreatorLimits | None = None,
    guided_capability_enabled: bool | None = None,
    narration: CreatorNarrationIdentity | Mapping[str, Any] | None = None,
    phone_source_media_ids: Sequence[str] | None = None,
    phone_rendering_allowed: bool = False,
    phone_visuals_only: bool = False,
    user_song_item: object | None = None,
    runs_guided_voiceover: bool = True,
    speaker_background_pick: str | None = None,
) -> ResolvedCreatorManifest:
    """Resolve a descriptive v1 manifest from server state and policy.

    ``runs_guided_voiceover`` (KRI-598) is False for a planner that has no
    guided-proposal step (runtime v2): a recorded voiceover's ``guided_voiceover`` is
    then unavailable with ``GUIDED_VOICEOVER_NATIVE_ONLY_CODE``, so the model never
    picks a contract the planner can't run. A manifest without a voiceover is unchanged.

    ``user_song_item`` (KRI-374) is the PlanItem (or any object with the same
    ``song_*`` attributes) read through ``getattr`` only: ``song_gcs_path``,
    ``song_duration_s`` and ``song_analysis``. ``None`` or no attached song adds
    nothing to the manifest, so it stays byte-identical to before the feature.

    ``speaker_background_pick`` (KRI-1117) is the creator's explicit background chosen in the
    camera (``thread.state["speaker_background"]["media_id"]``). It is kept on the manifest
    only while the ``speaker_background`` capability is advertised, so every other manifest
    stays byte-identical.

    ``render_program_for_intent`` and ``guided_edit_applicable`` are the same
    policy used by the existing plan-item/generative routes.  In particular,
    audio-led formats and voiceover items cannot be advertised as guided edits.
    """

    resolved_media = _as_media_refs(media)
    # None means cloud sources; an empty list means phone provenance whose
    # receipts could not be verified. Never erase that provenance on failure.
    # The one exception is ``phone_visuals_only``: a project with no footage
    # at all that phone_destination routed to the iPhone has no receipts to hold.
    resolved_phone_source_ids = set(phone_source_media_ids or ())
    resolved_catalog = _as_catalog_refs(catalog)
    resolved_narration = (
        None
        if narration is None
        else (
            narration
            if isinstance(narration, CreatorNarrationIdentity)
            else CreatorNarrationIdentity.model_validate(narration)
        )
    )
    resolved_edit = (
        None
        if current_edit is None
        else (
            current_edit
            if isinstance(current_edit, CreatorEditSnapshot)
            else CreatorEditSnapshot.model_validate(current_edit)
        )
    )
    resolved_limits = limits or CreatorLimits()
    ready_variant = has_ready_variant or (
        resolved_edit is not None and resolved_edit.status == "ready"
    )
    normalized_format = coerce_edit_format(edit_format)
    render_program = render_program_for_intent(edit_format, has_voiceover=has_voiceover)
    guided_applicable_now = guided_edit_applicable(edit_format, has_voiceover=has_voiceover)
    has_media = bool(resolved_media)
    has_native_media = any(not media.media_id.startswith("asset-") for media in resolved_media)
    guided_enabled = (
        settings.guided_edit_capability_enabled
        if guided_capability_enabled is None
        else guided_capability_enabled
    )
    guided_executable = guided_enabled and guided_applicable_now
    guided_voiceover_executable = (
        getattr(settings, "creator_prompt_fidelity_enabled", False)
        and has_voiceover
        and resolved_narration is not None
        and guided_enabled
        and has_media
    )
    has_dispatchable_media = has_native_media or (
        (render_program == "guided" and guided_executable and has_media)
        or guided_voiceover_executable
    )

    if not guided_enabled:
        guided = _unavailable("disabled_by_setting", "guided editing is disabled by the server")
    elif not guided_applicable_now:
        guided = _unavailable(
            "native_render_required",
            "this edit format or audio contract requires the native renderer",
        )
    else:
        guided = _available()

    capabilities: dict[str, CapabilityAvailability] = {
        CAPABILITY_SET_ITEM_INTENT: _available(),
        CAPABILITY_GUIDED_STORY: guided,
        CAPABILITY_NATIVE_RENDER: (
            _available()
            if has_native_media
            else _unavailable("no_native_clip", "attach a video clip before native rendering")
        ),
        CAPABILITY_DRAFT_GUIDED_PROPOSAL: (
            _available()
            if guided.available and has_media
            else (
                _unavailable("no_media", "attach source media before drafting a proposal")
                if guided.available
                else guided
            )
        ),
        CAPABILITY_DISPATCH_RENDER: (
            _available()
            if has_dispatchable_media
            else _unavailable(
                "no_native_clip" if has_media and not has_native_media else "no_media",
                "attach a video clip before rendering"
                if has_media and not has_native_media
                else "attach source media before dispatching a render",
            )
        ),
        CAPABILITY_SELECT_READY_VARIANT: (
            _available()
            if ready_variant
            else _unavailable("no_ready_variant", "a ready variant is required before selection")
        ),
        # Caption style has no separate rollout switch; it is available only
        # when the authenticated Creator execution gateway is enabled.
        CAPABILITY_CAPTION_STYLE: (
            _available()
            if settings.main_creator_agent_execution_enabled
            else _unavailable("disabled_by_setting", "caption styling is disabled by the server")
        ),
        # Reviewable speech-cut candidates are produced independently by the
        # silence/filler and retake detectors.  Execution validates the exact
        # pending candidate and its detector-specific switch again.
        CAPABILITY_AUTOMATIC_CUT: (
            _available()
            if settings.silence_cut_enabled or settings.retake_cut_enabled
            else _unavailable(
                "disabled_by_setting",
                "automatic speech cuts are disabled by the server",
            )
        ),
        CAPABILITY_GUIDED_VOICEOVER: (
            _available()
            if (guided_voiceover_executable)
            else _unavailable(
                "disabled_by_setting",
                "guided voiceover is disabled by the server",
            )
            if not getattr(settings, "creator_prompt_fidelity_enabled", False)
            else _unavailable(
                "narration_identity_missing",
                "a recorded voiceover generation and duration are required",
            )
            if has_voiceover and resolved_narration is None
            else _unavailable(
                "requires_voiceover",
                "guided voiceover requires a recorded voiceover",
            )
            if not has_voiceover
            else _unavailable(
                "disabled_by_setting",
                "guided editing is disabled by the server",
            )
            if not guided_enabled
            else _unavailable("no_media", "attach source media before guided voiceover")
        ),
    }

    for capability_name, setting_name in _FEATURE_SETTINGS.items():
        if getattr(settings, setting_name, False):
            capabilities[capability_name] = _available()
        else:
            capabilities[capability_name] = _unavailable(
                "disabled_by_setting", f"{capability_name} is disabled by the server"
            )
    if phone_source_media_ids is not None:
        attached_media = [
            media for media in resolved_media if not media.media_id.startswith("asset-")
        ]
        drawable_visual_kinds = phone_drawable_visual_kinds()
        visuals_only = (
            phone_visuals_only
            and not resolved_phone_source_ids
            and not attached_media
            and any(
                media.media_id.startswith("asset-") and media.kind in drawable_visual_kinds
                for media in resolved_media
            )
        )
        if not phone_rendering_allowed:
            phone = _unavailable(
                "disabled_by_setting", "phone rendering is unavailable for this account"
            )
        elif not visuals_only and (
            not resolved_phone_source_ids
            or resolved_phone_source_ids
            != {media.media_id for media in attached_media if media.kind == "video"}
            or any(media.kind != "video" for media in attached_media)
        ):
            phone = _unavailable(
                "unverified_phone_sources", "verified phone-only video sources are required"
            )
        elif has_voiceover:
            # KRI-132: a recorded voiceover over a montage-family edit
            # (day_vlog/single_hero/montage — never the guided-story
            # narration lane, which `guided_voiceover_executable` identifies
            # and which stays hard-blocked via `CAPABILITY_GUIDED_VOICEOVER`
            # below regardless of this flag) can render on the phone once the
            # rollout flag is on AND the device has verified narrationAudio.
            # Flag/capability off: byte-identical to pre-KRI-132.
            #
            # KRI-132 follow-up: a narrated* item WITH an already-recorded
            # voiceover must ALSO resolve `phone` available here -- this is
            # the manifest-level gate `effective_render_program` checks
            # FIRST (before it ever reaches the per-format `phone_format:
            # {format}` capability below), so leaving it montage-only would
            # dead-end Generate's own chat-planning turn for a narrated item
            # that already has its voiceover, even though the dispatch gate
            # and worker both already support it (`phone_render_supported_
            # formats()` folds in the narrated-specific flags this branch
            # used to check only for montage via `guided_edit_applicable`).
            #
            # `guided_voiceover_executable` deliberately does NOT veto this.
            # In prod (`creator_prompt_fidelity_enabled` + guided editing on)
            # it is True for EVERY project whose voiceover has a resolved
            # narration identity, so gating on it blocked every phone
            # voiceover render ("Remove the voiceover and I can design the
            # edit") while unit tests without a narration identity stayed
            # green. The guided-story narration lane is hard-blocked on the
            # phone on its own: `CAPABILITY_GUIDED_VOICEOVER` is set
            # unavailable unconditionally a few lines below, and
            # `effective_render_program` refuses an all-media / mixed-timing
            # voiceover strategy without that capability.
            declared_format = coerce_edit_format(edit_format)
            if declared_format in GUIDED_EDIT_FORMATS:
                montage_voiceover_ready = (
                    not visuals_only
                    and settings.phone_narration_rendering_enabled
                    and "narrationAudio" in settings.phone_render_verified_features
                )
            elif declared_format in NARRATED_EDIT_FORMATS:
                montage_voiceover_ready = (
                    not visuals_only and declared_format in phone_render_supported_formats()
                )
            else:
                montage_voiceover_ready = False
            phone = (
                _available()
                if montage_voiceover_ready
                else _unavailable(
                    "unsupported_phone_audio",
                    "phone rendering does not support recorded voiceover",
                )
            )
        else:
            phone = _available()
        capabilities[CAPABILITY_PHONE_SOURCE_AUDIO] = phone
        # Visuals photos and videos render on the device only once its engine
        # is verified for each. Omitted otherwise so flag-off manifests keep
        # their hashes.
        if "stillImages" in settings.phone_render_verified_features:
            capabilities[CAPABILITY_PHONE_STILL_IMAGES] = phone
        if "visualVideos" in settings.phone_render_verified_features:
            capabilities[CAPABILITY_PHONE_VISUAL_VIDEOS] = phone
        for capability_name in _PHONE_UNSUPPORTED_CAPABILITIES:
            capabilities[capability_name] = _unavailable(
                "unsupported_on_phone", f"{capability_name} cannot render on the iPhone yet"
            )
        # `sound_effects` (KRI-608): the guided / montage phone compiler turns the plan's
        # SFX into an `sfx` audio track once `phone_guided_sfx_supported()` (the single
        # source of truth the worker and Save also consult) holds, so the manifest may
        # advertise it for the guided formats. A recorded voiceover is excluded: that
        # edit is built by a different phone writer whose first build has no SFX lane
        # (it gets effects through the editor instead). Every other phone recipe
        # (Talking, narrated) keeps the blanket refusal, so KRI-178 reaction beats still
        # repair a Talking SFX request exactly as before. The generic
        # `sound_effects_enabled` gate is folded into the helper.
        if (
            phone.available
            and not has_voiceover
            and coerce_edit_format(edit_format) in GUIDED_EDIT_FORMATS
            and phone_guided_sfx_supported()
        ):
            capabilities["sound_effects"] = _available()
        # `visual_blocks` (KRI-118 L1 item 5): unlike the capabilities above,
        # this has real native parity once BOTH the editor-media rollout flag
        # and the device's verified feature set agree -- mirrors
        # `phone_rollout.validate_phone_pilot_recipe`'s exact compile-time
        # rule (`(phone_editor_media_enabled) and "visualBlocks" in
        # phone_render_verified_features`) so the manifest never advertises a
        # plan the compiler would then reject. The generic
        # `visual_blocks_enabled` gate (already applied above by the
        # `_FEATURE_SETTINGS` loop) still has to hold too.
        if not (
            phone.available
            and capabilities["visual_blocks"].available
            and getattr(settings, "phone_editor_media_enabled", False)
            and "visualBlocks" in settings.phone_render_verified_features
        ):
            capabilities["visual_blocks"] = _unavailable(
                "unsupported_on_phone", "visual_blocks cannot render on the iPhone yet"
            )
        # `media_overlays` (KRI-176): unlike the blanket phone refusal set by
        # `_PHONE_UNSUPPORTED_CAPABILITIES` above, the `subtitled` (Talking,
        # talk-to-camera) phone recipe has real native parity for
        # picture-in-picture Visuals cards once the KRI-174 Phase 1 lane flag
        # and every device feature agree -- mirrors
        # `phone_rollout.phone_subtitled_overlays_supported()`'s exact rule,
        # the single source of truth also consulted by
        # `_run_phone_subtitled_job`, so the manifest never advertises a lane
        # the worker would then skip. Every other phone recipe (the
        # guided/montage family, narrated) keeps the blanket refusal above --
        # only Talking has a compiler path for this lane today. The generic
        # `media_overlays_enabled` gate (already applied above by the
        # `_FEATURE_SETTINGS` loop, and re-read directly here by setting name
        # since the `_PHONE_UNSUPPORTED_CAPABILITIES` loop just overwrote the
        # `capabilities["media_overlays"]` entry it produced) still has to
        # hold too.
        if (
            phone.available
            and getattr(settings, "media_overlays_enabled", False)
            and coerce_edit_format(edit_format) == "subtitled"
            and phone_subtitled_overlays_supported()
        ):
            capabilities["media_overlays"] = _available()
        # `media_overlays:video_cards` (KRI-183): once this phone
        # `subtitled` manifest already advertises `media_overlays` (photo
        # picture-in-picture cards, KRI-176 immediately above), VIDEO
        # Visuals may ALSO become (muted, trimmed) cards once
        # `phone_rollout.phone_subtitled_video_overlays_supported()` agrees
        # -- mirrors its exact rule (KRI-183 lane flag riding the same
        # overlay gates as photos, plus the device's verified
        # `visualVideos` feature) so the manifest never advertises a video
        # card the compiler/worker would then reject
        # (`_run_phone_subtitled_job` consults the same helper). A brand
        # new capability key -- never `media_overlays` itself -- so an
        # unavailable/absent case adds nothing: omitted entirely (not set
        # `_unavailable`) when the gate is False, so a flag-off manifest's
        # `capabilities` dict, and therefore its `context_hash`/
        # `manifest_hash`, stay byte-identical to a pre-KRI-183 manifest --
        # the same "omit, don't mark unavailable" pattern `phone_still_
        # images`/`phone_visual_videos` use above.
        if capabilities["media_overlays"].available and phone_subtitled_video_overlays_supported():
            capabilities[CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS] = _available()
        if (
            capabilities["media_overlays"].available
            and phone.available
            and coerce_edit_format(edit_format) == "subtitled"
            and getattr(settings, "phone_subtitled_fullscreen_overlays_enabled", False)
        ):
            capabilities[CAPABILITY_MEDIA_OVERLAY_FULLSCREEN] = _available()
        if not phone.available:
            for capability_name in (
                CAPABILITY_DRAFT_GUIDED_PROPOSAL,
                CAPABILITY_GUIDED_STORY,
                CAPABILITY_DISPATCH_RENDER,
            ):
                capabilities[capability_name] = phone
        # KRI-132 phone-voiceover-gate follow-up: the guided-story narration
        # lane (execution contract `guided_voiceover_v1`, the only lane that
        # combines Visuals-pool media with a recorded voiceover) now has a
        # phone compiler (`app.pipeline.phone_guided_plan.compile_phone_
        # guided_plan`'s narration branch). Keep the cloud-computed value
        # (set above from `guided_voiceover_executable`) when the phone
        # itself can carry audio at all AND the rollout is on for this lane
        # specifically (`phone_guided_narration_supported()`, the single
        # source of truth `app.services.phone_rollout` also gives the
        # dispatch gate and the worker); otherwise force it unavailable
        # exactly as before -- a phone account whose rollout isn't there yet,
        # or whose recorded voiceover has no resolved narration identity
        # (`guided_voiceover_executable` False), must still see this
        # capability closed.
        capabilities[CAPABILITY_GUIDED_VOICEOVER] = (
            capabilities[CAPABILITY_GUIDED_VOICEOVER]
            if (phone.available and phone_guided_narration_supported())
            else _unavailable(
                "unsupported_phone_audio", "phone rendering does not support recorded voiceover"
            )
        )
        # KRI-132: per-format phone-compile availability, voiceover-state
        # aware -- `app.agents._schemas.creator_policy.effective_render_program`
        # (a pure function that must not import settings) consults this
        # instead of `guided_edit_applicable` directly, so a phone-account
        # strategy correctly resolves `subtitled`/`narrated*` as renderable
        # once rolled out, not just the montage family. `phone_render_
        # supported_formats()` is the settings-aware single source of truth
        # this mirrors (`app.services.phone_rollout`); the clip-count and
        # self-narration nuances it cannot see are resolved here, where the
        # manifest's attached clip count is already known.
        if not phone.available:
            for candidate_format in EDIT_FORMATS:
                capabilities[f"phone_format:{candidate_format}"] = phone
            for candidate_format in NARRATED_EDIT_FORMATS:
                capabilities[f"phone_format_pending_voiceover:{candidate_format}"] = phone
        else:
            clip_count = len(attached_media)
            supported_now = phone_render_supported_formats()
            # KRI-132 follow-up: whether a narrated* format WOULD render on
            # this phone once a voiceover is recorded -- a pure rollout-flag
            # check (`phone_render_supported_formats()` doesn't take
            # `has_voiceover` at all), independent of whether THIS item has
            # recorded one yet or how many clips are attached so far. This is
            # what lets the chat-planning turn proceed for the normal
            # Narrated journey (pick format -> chat/script -> record
            # voiceover -> Generate) instead of dead-ending before the
            # creator has had a chance to record anything -- `phone_format:
            # {format}` above answers a DIFFERENT question ("is this item's
            # CURRENT media shape renderable right now", which for
            # self-narration needs exactly one clip today) and stays
            # unchanged. Generate itself still fails closed with no
            # recording via the dispatch gate (`content_plan_build.py`).
            for candidate_format in NARRATED_EDIT_FORMATS:
                capabilities[f"phone_format_pending_voiceover:{candidate_format}"] = (
                    _available()
                    if candidate_format in supported_now
                    else _unavailable(
                        "phone_format_unavailable",
                        f"{candidate_format} does not render on this iPhone yet",
                    )
                )
            for candidate_format in EDIT_FORMATS:
                if candidate_format in GUIDED_EDIT_FORMATS:
                    # Unaffected by this KRI-132 addition: the montage
                    # family's phone eligibility (with or without a
                    # voiceover) is already fully described by `phone` above
                    # (`phone.available` already true here); every
                    # montage-family format is always structurally
                    # phone-format-eligible at this point.
                    phone_format_capability = _available()
                elif candidate_format == "subtitled":
                    # KRI-719: with multi-clip Talking live, 1..MAX clips render
                    # (speakers + cutaways); flag off keeps "exactly one clip".
                    multi_clip_talking = phone_talking_multi_clip_supported()
                    talking_clip_ok = (
                        1 <= clip_count <= TALKING_MULTI_CLIP_MAX_CLIPS
                        if multi_clip_talking
                        else clip_count == 1
                    )
                    phone_format_capability = (
                        _available()
                        if (
                            candidate_format in supported_now
                            and talking_clip_ok
                            and not has_voiceover
                        )
                        else _unavailable(
                            "phone_format_unavailable",
                            (
                                "talking-to-camera rendering on this iPhone requires "
                                f"1 to {TALKING_MULTI_CLIP_MAX_CLIPS} clips and no "
                                "recorded voiceover"
                                if multi_clip_talking
                                else "talking-to-camera rendering on this iPhone requires "
                                "exactly one clip and no recorded voiceover"
                            ),
                        )
                    )
                elif candidate_format in NARRATED_EDIT_FORMATS:
                    if has_voiceover:
                        narrated_ok = candidate_format in supported_now
                        phone_format_capability = (
                            _available()
                            if narrated_ok
                            else _unavailable(
                                "phone_format_unavailable",
                                f"{candidate_format} does not render on this iPhone yet",
                            )
                        )
                    elif clip_count >= 2 and phone_talking_head_supported():
                        # KRI-136: 2+ clips resolve to `talking_head`, which
                        # renders on the phone as the speaker clip plus
                        # muted full-frame cutaways.
                        phone_format_capability = _available()
                    elif clip_count >= 2:
                        # KRI-118 L1 item 3: self-narration (no recorded
                        # voiceover) across 2+ clips would need
                        # `talking_head`, which has no phone compiler at all
                        # -- only the single-clip shape can ever resolve to
                        # the phone-supported `subtitled` archetype
                        # (`_resolve_archetype`, generative_build.py). Refuse
                        # explicitly at planning time, before a Job is ever
                        # minted, with a distinct reason/copy rather than the
                        # generic `phone_format_unavailable` below.
                        phone_format_capability = _unavailable(
                            "self_narration_multi_clip",
                            "Narrating across several clips isn't on iPhone yet. "
                            "Record a voiceover, or keep one clip for talking-to-camera.",
                        )
                    else:
                        # Self-narration: only the single-clip shape can
                        # ever resolve to the phone-supported `subtitled`
                        # archetype (`_resolve_archetype`,
                        # generative_build.py).
                        narrated_ok = (
                            settings.narrated_self_narration_enabled
                            and "subtitled" in supported_now
                            and clip_count == 1
                        )
                        phone_format_capability = (
                            _available()
                            if narrated_ok
                            else _unavailable(
                                "phone_format_unavailable",
                                f"{candidate_format} does not render on this iPhone yet",
                            )
                        )
                else:
                    phone_format_capability = _unavailable(
                        "phone_format_unavailable",
                        f"{candidate_format} does not render on this iPhone yet",
                    )
                capabilities[f"phone_format:{candidate_format}"] = phone_format_capability
    # KRI-178: `reaction_beats` is resolved on EVERY manifest (cloud and
    # phone alike), unlike `media_overlays` above which is only touched
    # inside the phone-only branch -- a cloud manifest, or any phone
    # manifest that isn't a `subtitled` (Talking) edit, always advertises
    # this capability unavailable with reason_code `phone_talking_only`
    # (the feature is phone-Talking-only by design, not a rollout gap).
    # A phone `subtitled` manifest instead reports `unsupported_on_phone`
    # when the gate (media_overlays availability +
    # `phone_rollout.phone_subtitled_reaction_beats_supported()`, the
    # single source of truth also consulted by `_run_phone_subtitled_job`)
    # fails, so the manifest never advertises a lane the worker would
    # then skip.
    is_phone_subtitled = (
        phone_source_media_ids is not None
        and capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO) is not None
        and capabilities[CAPABILITY_PHONE_SOURCE_AUDIO].available
        and normalized_format == "subtitled"
    )
    # KRI-519: a phone Voiceover edit (recorded voiceover, phone-renderable
    # narrated format) grounds the same beats against the voiceover and draws
    # them in its KRI-281 lanes. It is the only way runtime v2 times a Visual
    # to the voice, so it is advertised only when the worker will honour it;
    # otherwise the capability keeps its pre-KRI-519 `phone_talking_only` refusal.
    is_phone_voiceover = (
        phone_source_media_ids is not None
        and capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO) is not None
        and capabilities[CAPABILITY_PHONE_SOURCE_AUDIO].available
        and normalized_format in NARRATED_EDIT_FORMATS
        and has_voiceover
        and resolved_narration is not None
        and capabilities.get(f"phone_format:{normalized_format}", _unavailable("x", "x")).available
    )
    from app.services.phone_rollout import (
        phone_voiceover_source_audio_supported,
        phone_voiceover_spoken_text_supported,
    )

    # Advertised only while its authoring flag is on: with the flag off the
    # manifest (and its confirmation-fence hashes) stays byte-identical to
    # before these lanes existed, and readers treat a missing entry as
    # unavailable.
    for name, enabled, supported in (
        (
            "phone_voiceover_spoken_text",
            settings.phone_voiceover_spoken_text_enabled,
            phone_voiceover_spoken_text_supported,
        ),
        (
            "phone_voiceover_source_audio",
            settings.phone_voiceover_source_audio_enabled,
            phone_voiceover_source_audio_supported,
        ),
    ):
        if not enabled:
            continue
        capabilities[name] = (
            _available()
            if is_phone_voiceover and supported()
            else _unavailable(
                "voiceover_feature_unavailable",
                "This Voiceover feature needs a supported app and server release.",
            )
        )
    if is_phone_voiceover and phone_voiceover_reaction_beats_supported():
        capabilities[CAPABILITY_REACTION_BEATS] = _available()
        # N2: the existing full-screen rollout switch and the verified
        # Voiceover editor-lane capability are both required before a
        # word-timed Voiceover visual may take over the frame.  Talking keeps
        # its established capability branch above unchanged.
        if getattr(settings, "phone_subtitled_fullscreen_overlays_enabled", False):
            capabilities[CAPABILITY_MEDIA_OVERLAY_FULLSCREEN] = _available()
    elif is_phone_subtitled:
        if (
            capabilities.get("media_overlays", _unavailable("x", "x")).available
            and phone_subtitled_reaction_beats_supported()
        ):
            capabilities[CAPABILITY_REACTION_BEATS] = _available()
        else:
            capabilities[CAPABILITY_REACTION_BEATS] = _unavailable(
                "unsupported_on_phone",
                "reaction beats cannot render on this iPhone deployment yet",
            )
    else:
        capabilities[CAPABILITY_REACTION_BEATS] = _unavailable(
            "phone_talking_only",
            "reaction beats are only available on an iPhone talking-to-camera edit",
        )
    if getattr(settings, "phone_timed_cards_enabled", False):
        phone_on = (
            phone_source_media_ids is not None
            and capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO) is not None
            and capabilities[CAPABILITY_PHONE_SOURCE_AUDIO].available
        )
        # The three phone writers that compile the lane: a guided-format montage (with or
        # without a recorded voiceover) and a narrated edit WITH its voiceover recorded. A
        # narrated item without one is self-narration (Talking's beats own that edit), and
        # Talking itself never advertises it.
        timed_format_ok = normalized_format in GUIDED_EDIT_FORMATS or (
            normalized_format in NARRATED_EDIT_FORMATS
            and has_voiceover
            and capabilities.get(
                f"phone_format:{normalized_format}", _unavailable("x", "x")
            ).available
        )
        if phone_source_media_ids is None:
            capabilities[CAPABILITY_TIMED_CARDS] = _unavailable(
                "phone_only", "small cards at an exact time render on the iPhone only"
            )
        elif not (phone_on and timed_format_ok):
            capabilities[CAPABILITY_TIMED_CARDS] = _unavailable(
                "phone_timed_cards_format",
                "small cards at an exact time are only available on an iPhone montage or "
                "voiceover edit",
            )
        elif not phone_timed_cards_supported():
            capabilities[CAPABILITY_TIMED_CARDS] = _unavailable(
                "unsupported_on_phone",
                "small cards at an exact time cannot render on this iPhone deployment yet",
            )
        else:
            capabilities[CAPABILITY_TIMED_CARDS] = _available()
    background_pick: str | None = None
    if is_phone_subtitled and not has_voiceover:
        # Advertised only when open (like `timed_cards`): a closed gate leaves the dict, both
        # hashes and the creator prompt exactly as they were before this lane existed.
        if settings.phone_subtitled_media_lanes_enabled and speaker_background_supported():
            capabilities[CAPABILITY_SPEAKER_BACKGROUND] = _available()
            background_pick = speaker_background_pick or None
    if has_voiceover and not runs_guided_voiceover:
        # KRI-598: runtime v2 downgraded every guided_voiceover_v1 strategy to the native
        # edit, losing what the contract carried (prod thread f77a5c16 lost a photo beat).
        # This reason is what the model reads, and its retry feedback if it picks one.
        visuals = (
            "a Visuals photo or video shows only on a spoken word, via reaction_beats"
            if capabilities[CAPABILITY_REACTION_BEATS].available
            else "Visuals photos and videos cannot be timed to it here"
        )
        capabilities[CAPABILITY_GUIDED_VOICEOVER] = _unavailable(
            GUIDED_VOICEOVER_NATIVE_ONLY_CODE,
            "This chat cannot run guided_voiceover_v1: leave execution_contract unset. The "
            'voiceover edit uses the project\'s own clips ("all" = every clip); ' + visuals + ".",
        )

    # KRI-374: the creator's own song. Added to `capabilities` ONLY when a song is
    # attached (available or with the reason it can't be used), so a manifest
    # without a song keeps its pre-KRI-374 capabilities, hashes and prompt.
    user_song_facts = _resolve_user_song(
        user_song_item,
        capabilities=capabilities,
        phone_rendering=phone_source_media_ids is not None,
        normalized_format=normalized_format,
        has_voiceover=has_voiceover,
    )

    if capabilities["main_creator_agent"].available and not getattr(
        settings, "main_creator_agent_rollout_percent", 0
    ):
        capabilities["main_creator_agent"] = _unavailable(
            "rollout_disabled", "main creator agent rollout is set to zero"
        )
    for candidate_format in EDIT_FORMATS:
        capabilities[f"edit_format:{candidate_format}"] = _format_availability(
            candidate_format, has_voiceover=has_voiceover
        )

    context_payload = {
        "schema_version": 1,
        "item_id": item_id,
        "edit_format": normalized_format,
        "render_program": render_program,
        "has_voiceover": has_voiceover,
        "narration": resolved_narration,
        "current_edit": resolved_edit,
        # Duration is asynchronous analysis evidence, not source identity. A
        # probe finishing while the creator reviews a plan must not invalidate
        # ordinary confirmation. Cadence plans recheck capacity explicitly.
        "media": [
            media.model_dump(mode="json", exclude={"duration_s"}) for media in resolved_media
        ],
        "catalog": resolved_catalog,
        "has_ready_variant": ready_variant,
        "capabilities": capabilities,
        "limits": resolved_limits,
    }
    if user_song_facts is not None:
        context_payload["has_user_song"] = True
        context_payload["user_song"] = user_song_facts.model_dump(mode="json")
    if background_pick is not None:
        context_payload["speaker_background_pick"] = background_pick
    context_hash = canonical_context_hash(context_payload)
    manifest = ResolvedCreatorManifest(
        item_id=item_id,
        edit_format=normalized_format,
        render_program=render_program,
        has_voiceover=has_voiceover,
        has_user_song=user_song_facts is not None,
        user_song=user_song_facts,
        speaker_background_pick=background_pick,
        narration=resolved_narration,
        current_edit=resolved_edit,
        media=resolved_media,
        catalog=resolved_catalog,
        capabilities=capabilities,
        limits=resolved_limits,
        context_hash=context_hash,
        manifest_hash="0" * 64,
    )
    return manifest.model_copy(update={"manifest_hash": canonical_manifest_hash(manifest)})


def _song_has_lyrics(analysis: object) -> bool:
    if analysis is None:
        return False
    if isinstance(analysis, Mapping):
        return bool(analysis.get("words"))
    return bool(getattr(analysis, "has_lyrics", False))


def _resolve_user_song(
    item: object | None,
    *,
    capabilities: dict[str, CapabilityAvailability],
    phone_rendering: bool,
    normalized_format: str,
    has_voiceover: bool,
) -> UserSongFacts | None:
    """KRI-374: resolve the ``user_song`` capability; return the facts to show
    the planner only when the song is usable.

    Writes ``capabilities[CAPABILITY_USER_SONG]`` only when a song is attached.
    Available only on a phone-rendered montage-family manifest with no recorded
    voiceover, while ``phone_user_song_supported()`` (kill switch + the two device
    capabilities) holds. Everything is read via ``getattr`` so this works against
    a PlanItem, a thread object or a test stub.
    """

    if item is None or not getattr(item, "song_gcs_path", None):
        return None
    phone = capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO)
    if not phone_rendering or phone is None or not phone.available:
        capabilities[CAPABILITY_USER_SONG] = _unavailable(
            "phone_only", "your own song only works on edits that render on your iPhone"
        )
        return None
    if not phone_user_song_supported():
        capabilities[CAPABILITY_USER_SONG] = _unavailable(
            "disabled_by_setting", "your own song is unavailable on this server right now"
        )
        return None
    if normalized_format not in GUIDED_EDIT_FORMATS:
        capabilities[CAPABILITY_USER_SONG] = _unavailable(
            "unsupported_format", "your own song only works on montage edits"
        )
        return None
    if has_voiceover:
        capabilities[CAPABILITY_USER_SONG] = _unavailable(
            "voiceover_present", "your own song can't be combined with a recorded voiceover"
        )
        return None
    guided = capabilities.get(CAPABILITY_DRAFT_GUIDED_PROPOSAL)
    if guided is None or not guided.available:
        capabilities[CAPABILITY_USER_SONG] = _unavailable(
            "no_media", "attach source media before using your own song"
        )
        return None
    capabilities[CAPABILITY_USER_SONG] = _available()
    try:
        duration_s = float(getattr(item, "song_duration_s", None) or 0.0)
    except (TypeError, ValueError):
        duration_s = 0.0
    return UserSongFacts(
        duration_s=max(0.0, min(duration_s, 3600.0)),
        has_lyrics=_song_has_lyrics(getattr(item, "song_analysis", None)),
    )


def _repair_creator_reaction_beats(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> tuple[CreativeStrategy, list[str]]:
    """KRI-178: repair (never reject) `reaction_beats`/`closing_media` against
    the live manifest, the same shape `repair_creator_strategy_shape` uses for
    a stale story shape -- an unavailable capability, an unresolved image
    reference, or an unresolved sound description are all silently downgraded
    with a plain-language notice instead of failing the whole strategy.

    Repair (a): the capability is unavailable for this manifest -> drop both
    fields entirely.
    Repairs (b)/(c): resolve every `visual_id`/`badge_visual_id` against an
    owned IMAGE media entry (drop the beat, or fall back to no closing media,
    when it doesn't resolve; a beat's `visual_id` may also be a Visuals VIDEO
    where video cards render, KRI-521) and every `sound` against the sound-effect
    catalog (canonical id when it matches, otherwise left as the creator's own
    words for the worker to resolve by description).
    Repair (e), KRI-840: a beat asked `display_mode="fullscreen"` keeps it only where
    `media_overlays:fullscreen` is available (phone Talking, native Voiceover) and the beat
    has a visual; otherwise it becomes a corner card ("pip"), with a notice when a photo is
    affected.
    """

    if strategy.reaction_beats is None and strategy.closing_media is None:
        return strategy, []

    notices: list[str] = []
    reaction_beats_cap = manifest.capabilities.get(CAPABILITY_REACTION_BEATS)
    if reaction_beats_cap is None or not reaction_beats_cap.available:
        notices.append(
            "Sound and photo pop-ins timed to your words aren't available for this "
            "edit yet; left them out."
        )
        return (
            strategy.model_copy(update={"reaction_beats": None, "closing_media": None}),
            notices,
        )

    def _resolve_sound(sound: str | None) -> str | None:
        if sound is None:
            return None
        resolved = resolve_creator_sfx_catalog_ref(manifest, sound)
        return resolved.catalog_id if resolved is not None else sound

    # KRI-521: a beat may also pop a Visuals VIDEO in the corner ("when I say
    # 'first stop', show the brewing video small") wherever the phone draws
    # muted video cards (KRI-183).
    video_cards = manifest.capabilities.get(CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS)
    videos_allowed = video_cards is not None and video_cards.available
    beats = []
    native_voiceover = bool(
        manifest.has_voiceover
        and strategy.render_program != "guided"
        and strategy.edit_format in NARRATED_EDIT_FORMATS
    )
    fullscreen_cap = manifest.capabilities.get(CAPABILITY_MEDIA_OVERLAY_FULLSCREEN)
    fullscreen_available = fullscreen_cap is not None and fullscreen_cap.available
    # KRI-840: "show it full screen at the word" is a full-frame cutaway on a native
    # Voiceover edit and on phone Talking (both advertise `media_overlays:fullscreen`).
    # Anywhere else the photo stays a corner card -- never dropped, which would read as
    # "couldn't find the photo".
    phone_talking = strategy.edit_format == "subtitled"
    # `media_scope="all"` on a native voiceover expands to every own video
    # during normalisation below. Resolve a following clip against that same
    # effective selection now, because reaction-beat repair intentionally runs
    # before generic media normalisation.
    selected_ids = (
        set(own_video_ids(manifest))
        if native_voiceover_all_scope(manifest, strategy)
        else set(strategy.selected_media_ids)
    )
    original_videos = [
        media
        for media in manifest.media
        if media.kind == "video" and not media.media_id.startswith("asset-")
    ]
    for beat in strategy.reaction_beats or []:
        visual_id = beat.visual_id
        resolved_visual = None
        if visual_id is not None:
            resolved_media = resolve_creator_image_media_ref(manifest, visual_id)
            if resolved_media is None:
                video = resolve_creator_visual_video_media_ref(manifest, visual_id)
                if video is not None and videos_allowed:
                    resolved_media = video
                elif video is not None:
                    notices.append(
                        "Videos can't pop up on your words in this edit yet, so the video "
                        f'for "{beat.trigger}" is left out.'
                    )
                    continue
                else:
                    notices.append(
                        f'Couldn\'t find the photo or sticker for "{beat.trigger}" in your '
                        "Visuals; left it out."
                    )
                    continue
            visual_id = resolved_media.media_id
            resolved_visual = resolved_media
        updates: dict[str, object] = {
            "visual_id": visual_id,
            "sound": _resolve_sound(beat.sound),
        }
        following_clip_id = beat.following_clip_id
        if following_clip_id is not None:
            # A photo-then-original-video sequence is atomic.  Keeping just
            # the photo would let approval claim a different request was met.
            if not (
                beat.display_mode == "fullscreen"
                and native_voiceover
                and fullscreen_available
                and resolved_visual is not None
                and resolved_visual.kind == "image"
                and beat.occurrence == "first"
            ):
                notices.append(
                    f'The photo-then-video sequence for "{beat.trigger}" needs a first, '
                    "full-screen photo on this voiceover edit; left that sequence out."
                )
                continue
            # Resolve labels as well as ids, then persist only the manifest's
            # canonical original-media id for the renderer binding.
            following = _match_owned_media_ref(original_videos, following_clip_id)
            if following is None:
                notices.append(
                    "Couldn't find the selected original video to show after "
                    f'"{beat.trigger}"; left that sequence out.'
                )
                continue
            if following.media_id not in selected_ids:
                notices.append(
                    f'The video after "{beat.trigger}" is not selected for this edit; '
                    "left that sequence out."
                )
                continue
            updates["following_clip_id"] = following.media_id
        elif beat.display_mode == "fullscreen":
            if visual_id is None:
                updates["display_mode"] = "pip"  # a sound-only beat has nothing to fill the screen
            elif not (fullscreen_available and (native_voiceover or phone_talking)):
                updates["display_mode"] = "pip"
                notices.append(
                    "Full screen isn't available for this edit yet, so "
                    f'"{beat.trigger}" shows as a small card in the corner instead.'
                )
        beats.append(beat.model_copy(update=updates))
    resolved_beats = beats or None

    closing = strategy.closing_media
    if closing is not None:
        resolved_closing_media = resolve_creator_image_media_ref(manifest, closing.visual_id)
        if resolved_closing_media is None:
            notices.append("Couldn't find the closing photo you named; kept the normal ending.")
            closing = None
        else:
            resolved_badge = (
                resolve_creator_image_media_ref(manifest, closing.badge_visual_id)
                if closing.badge_visual_id is not None
                else None
            )
            if closing.badge_visual_id is not None and resolved_badge is None:
                notices.append("Couldn't find the closing badge you named; left it off.")
            closing = closing.model_copy(
                update={
                    "visual_id": resolved_closing_media.media_id,
                    "badge_visual_id": (
                        resolved_badge.media_id if resolved_badge is not None else None
                    ),
                }
            )

    return (
        strategy.model_copy(update={"reaction_beats": resolved_beats, "closing_media": closing}),
        notices,
    )


# KRI-841: why an `ending_clip` (a video appended after the talk, with its own sound) cannot
# be kept. The creator reads the notice before approving; the brief receipt words the same
# code in `app.kria.brief_checks`, and the Turkish twins of both live with their readers.
ENDING_CLIP_NOTICES: dict[str, str] = {
    "not_talking": (
        "Couldn't add that video at the end with its own sound: only a talking-to-camera edit "
        "on your iPhone can end on another video."
    ),
    "not_found": "Couldn't find the video you named for the end; kept the normal ending.",
    "not_video": (
        "Couldn't add that at the end with its own sound: it's a photo, not a video. A photo "
        "can be the closing photo instead."
    ),
    "visual_videos_off": (
        "Couldn't add that video at the end: this iPhone can't play videos from Visuals yet."
    ),
    "media_lanes_disabled": (
        "Couldn't add that video at the end: this iPhone render can't place pictures and "
        "videos around your talk yet."
    ),
    "audio_mix_not_verified": (
        "Couldn't add that at the end with its own sound: this iPhone can't mix its sound "
        "into the video yet."
    ),
    "muted_footage_ending_unsupported": (
        "Couldn't add that clip at the end silent: one of your own clips can only end the "
        "video with its own sound."
    ),
    "not_selected": ("Couldn't add that clip at the end: you asked to leave it out of this edit."),
    "only_clip": "Couldn't add that clip at the end: it is the only clip in this edit.",
    "multi_clip_off": (
        "Couldn't add that clip at the end: ending on another of your clips needs a Talking "
        "edit with several clips, which isn't available yet."
    ),
}
ENDING_CLIP_NOT_ALSO_A_POP_IN_NOTICE = (
    "The video you wanted at the end plays only there, not also on a spoken word."
)


def _ending_clip_resolution(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> tuple[CreatorMediaRef | None, str | None]:
    """(the media an ``ending_clip`` names, None) when the phone can append it, else (None, why).

    Only a phone Talking edit appends a video after the talk. The video is a Visuals video
    (the device must draw them) or one of the item's own footage clips, which needs
    multi-clip Talking and at least one OTHER clip to be the talk.
    """

    ending = strategy.ending_clip
    if ending is None:
        return None, None
    phone = manifest.capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO)
    if (
        strategy.edit_format != "subtitled"
        or phone is None
        or not phone.available
        or manifest.has_voiceover
    ):
        return None, "not_talking"
    videos = [item for item in manifest.media if item.kind == "video"]
    media = _match_owned_media_ref(videos, ending.media_id)
    if media is None:
        if resolve_creator_image_media_ref(manifest, ending.media_id) is not None:
            return None, "not_video"
        return None, "not_found"
    if media.media_id.startswith("asset-"):
        # The gates `_run_phone_subtitled_job` applies to a Visuals ending: the card / ending
        # lane (media lanes), the device's video decode, and the main-track audio mix.
        if not settings.phone_subtitled_media_lanes_enabled:
            return None, "media_lanes_disabled"
        if "video" not in phone_drawable_visual_kinds():
            return None, "visual_videos_off"
        if not _phone_audio_mix_verified():
            return None, "audio_mix_not_verified"
        return media, None
    if sum(1 for item in videos if not item.media_id.startswith("asset-")) < 2:
        return None, "only_clip"
    if not ending.keep_audio:
        # The worker puts a footage clip last on the main track with its own sound only.
        return None, "muted_footage_ending_unsupported"
    if not _phone_audio_mix_verified():
        return None, "audio_mix_not_verified"
    if not phone_talking_multi_clip_supported():
        return None, "multi_clip_off"
    if _creator_left_out(strategy, media.media_id):
        return None, "not_selected"
    return media, None


def _creator_left_out(strategy: CreativeStrategy, media_id: str) -> bool:
    """True when a resolved ``exclude`` clip intent (the creator's own words) names this clip."""
    needle = media_id.casefold()
    return any(
        intent.status == "resolved"
        and intent.op == "exclude"
        and any(a.media_id.casefold() == needle for a in intent.assignments)
        for intent in strategy.resolved_clip_intents or ()
    )


def _phone_audio_mix_verified() -> bool:
    return "audioMix" in settings.phone_render_verified_features


def phone_talking_title_lane_open(manifest: ResolvedCreatorManifest) -> bool:
    """KRI-467: a phone Talking edit draws its opening title through its text lane
    (`phone_subtitled_title`). A cloud Talking edit has no title lane, and
    `PHONE_SUBTITLED_TITLE_ENABLED=false` closes it."""

    phone = manifest.capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO)
    return bool(phone is not None and phone.available and phone_subtitled_title_supported())


def ending_clip_problem(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> str | None:
    """The reason code ``repair_creator_ending_clip`` drops this strategy's ending, else None."""

    return _ending_clip_resolution(manifest, strategy)[1]


def ending_clip_candidates(manifest: ResolvedCreatorManifest) -> list[CreatorMediaRef]:
    """Videos a phone Talking edit could end on right now (empty = nothing to teach or keep)."""

    phone = manifest.capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO)
    if manifest.edit_format != "subtitled" or manifest.has_voiceover:
        return []
    if phone is None or not phone.available:
        return []
    videos = [item for item in manifest.media if item.kind == "video"]
    visuals = [item for item in videos if item.media_id.startswith("asset-")]
    own = [item for item in videos if not item.media_id.startswith("asset-")]
    if not _phone_audio_mix_verified():
        return []  # every ending plays on the main track, which needs the audio mix
    found = (
        visuals
        if settings.phone_subtitled_media_lanes_enabled and "video" in phone_drawable_visual_kinds()
        else []
    )
    if len(own) >= 2 and phone_talking_multi_clip_supported():
        found = [*found, *own]
    return found


def _repair_creator_timed_cards(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> tuple[CreativeStrategy, list[str]]:
    """Repair (never reject) `timed_cards` against the live manifest, the same shape
    `_repair_creator_reaction_beats` uses.

    (a) the `timed_cards` capability is absent/unavailable -> drop them all with one plain
    notice (`TIMED_CARDS_UNAVAILABLE_NOTICE`). (b) every `visual_id` resolves (id or label)
    to an owned Visuals IMAGE -- or a Visuals VIDEO where the device draws video cards
    (`phone_timed_video_cards_supported()`) -- and is canonicalised to the manifest id; an
    unresolved one is dropped, named by the creator's own words and time. (c) a `clip_id`
    anchor resolves (id or label) to one of the project's own clips, canonicalised; an
    unresolved one is dropped with a notice. The worker grounds the survivors.
    """

    cards = strategy.timed_cards
    if not cards:
        return strategy, []
    capability = manifest.capabilities.get(CAPABILITY_TIMED_CARDS)
    if capability is None or not capability.available:
        return strategy.model_copy(update={"timed_cards": None}), [TIMED_CARDS_UNAVAILABLE_NOTICE]
    videos_allowed = phone_timed_video_cards_supported()
    own_clips = [
        media
        for media in manifest.media
        if media.kind == "video" and not media.media_id.startswith("asset-")
    ]
    notices: list[str] = []
    kept = []
    for card in cards:
        image = resolve_creator_image_media_ref(manifest, card.visual_id)
        video = (
            resolve_creator_visual_video_media_ref(manifest, card.visual_id)
            if image is None
            else None
        )
        if image is None and video is None:
            notices.append(TIMED_CARD_VISUAL_NOT_FOUND_NOTICE)
            continue
        if image is None and not videos_allowed:
            notices.append(TIMED_VIDEO_CARD_UNAVAILABLE_NOTICE)
            continue
        resolved = image if image is not None else video
        updates: dict[str, object] = {"visual_id": resolved.media_id}
        if card.label is None and resolved.label:
            updates["label"] = resolved.label
        if card.clip_id is not None:
            clip = _match_owned_media_ref(own_clips, card.clip_id)
            if clip is None:
                notices.append(
                    "Couldn't find the clip the small card should follow; left that card out."
                )
                continue
            updates["clip_id"] = clip.media_id
        kept.append(card.model_copy(update=updates))
    return strategy.model_copy(update={"timed_cards": kept or None}), notices


def _repair_creator_ending_clip(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> tuple[CreativeStrategy, list[str]]:
    """KRI-841: keep an ``ending_clip`` only where the phone can append it, honestly.

    Repair (never reject), like the reaction-beat repair: an ending the edit cannot render
    (a montage or voiceover edit, a photo, an unknown id, the only clip, a footage clip while
    multi-clip Talking is off) is dropped with a plain notice, so the brief receipt can say
    "Couldn't" before approval. A kept one carries the manifest's canonical id, and the same
    video is taken out of every reaction beat: it plays at the end with its own sound, never
    also muted in the middle of the talk.
    """

    ending = strategy.ending_clip
    if ending is None:
        return strategy, []
    media, problem = _ending_clip_resolution(manifest, strategy)
    if media is None:
        return (
            strategy.model_copy(update={"ending_clip": None}),
            [ENDING_CLIP_NOTICES[problem or "not_found"]],
        )
    update: dict[str, Any] = {"ending_clip": ending.model_copy(update={"media_id": media.media_id})}
    selected = list(strategy.selected_media_ids)
    if (
        not media.media_id.startswith("asset-")
        and selected
        and strategy.media_scope != "all"
        and media.media_id.casefold() not in {m.casefold() for m in selected}
    ):
        # The phone worker binds only the selected clips: an ending the pick left out would
        # be kept here and then never bound. (A clip the creator left out was refused above.)
        update["selected_media_ids"] = [*selected, media.media_id]
    videos = [item for item in manifest.media if item.kind == "video"]
    beats = []
    removed = False
    for beat in strategy.reaction_beats or ():
        same = beat.visual_id is not None and (
            _match_owned_media_ref(videos, beat.visual_id) == media
        )
        if same:
            removed = True
            if beat.sound is None:
                continue
            beat = beat.model_copy(update={"visual_id": None})
        beats.append(beat)
    notices: list[str] = []
    if removed:
        update["reaction_beats"] = beats or None
        notices.append(ENDING_CLIP_NOT_ALSO_A_POP_IN_NOTICE)
    return strategy.model_copy(update=update), notices


SPEAKER_BACKGROUND_STATE_KEY = "speaker_background"


def canonical_pool_media_id(value: object) -> str | None:
    """A Visuals-pool row id in the manifest's ``asset-<uuid>`` spelling, else None.

    Accepts the bare row id a client knows or the manifest's own spelling; anything that is
    not a UUID (a filename, another id scheme) names no pool row.
    """

    if not isinstance(value, str):
        return None
    raw = value.strip()
    bare = raw[len("asset-") :] if raw.casefold().startswith("asset-") else raw
    try:
        return f"asset-{uuid.UUID(bare)}"
    except ValueError:
        return None


def speaker_background_pick_from_state(state: object) -> str | None:
    """The background the creator picked in the camera, from a thread's ``state``, else None."""

    pick = state.get(SPEAKER_BACKGROUND_STATE_KEY) if isinstance(state, Mapping) else None
    media_id = pick.get("media_id") if isinstance(pick, Mapping) else None
    return canonical_pool_media_id(media_id)


SPEAKER_BACKGROUND_NOT_ALSO_A_POP_IN_NOTICE = (
    "The photo or video behind you is only the background, not also a pop-in on a spoken word."
)


def speaker_background_open(manifest: ResolvedCreatorManifest) -> bool:
    """True when the manifest advertises the background lane (nothing to teach or keep else)."""

    capability = manifest.capabilities.get(CAPABILITY_SPEAKER_BACKGROUND)
    return capability is not None and capability.available


def _speaker_background_media(
    manifest: ResolvedCreatorManifest, requested: str | None
) -> CreatorMediaRef | None:
    """The Visuals-pool photo or video a background names, else None.

    Only ``asset-*`` entries count (the item's own pool, never the speaker's clips); a bare
    pool row id is read as ``asset-<id>`` so a client that knows the row id still resolves.
    """

    pool = [
        item
        for item in manifest.media
        if item.media_id.startswith("asset-") and item.kind in {"image", "video"}
    ]
    needle = str(requested or "").strip()
    found = _match_owned_media_ref(pool, needle)
    if found is None and needle and not needle.startswith("asset-"):
        found = _match_owned_media_ref(pool, f"asset-{needle}")
    return found


def _repair_creator_speaker_background(
    manifest: ResolvedCreatorManifest, strategy: CreativeStrategy
) -> tuple[CreativeStrategy, list[str]]:
    """KRI-1117: keep a Talking background only where the phone can composite it, honestly.

    Repair (never reject), like the ending-clip repair. The creator's explicit pick in the
    camera (``manifest.speaker_background_pick``) fills in a strategy the agent left without
    a ``background_media`` (KRI-129: the creator's own pick wins over silence). An id outside
    THIS item's pool, a closed capability or a kind the phone cannot draw is dropped with a
    plain notice (S18, S19), never silently. A kept background is also taken out of every
    reaction beat and the closing shot: it is the backdrop, never also a pop-in.
    """

    wanted = strategy.background_media
    pick = manifest.speaker_background_pick
    if wanted is None and pick is None:
        return strategy, []
    if not speaker_background_open(manifest):
        return (
            strategy.model_copy(update={"background_media": None}),
            [SPEAKER_BACKGROUND_NOTICES["unavailable"]],
        )
    notices: list[str] = []
    media: CreatorMediaRef | None = None
    start = 0.0
    if wanted is not None:
        media = _speaker_background_media(manifest, wanted.media_id)
        start = wanted.source_start_s
        if media is None:
            notices.append(SPEAKER_BACKGROUND_NOTICES["not_found"])
    if media is None and pick is not None:
        media, start = _speaker_background_media(manifest, pick), 0.0
        if media is None:
            notices.append(SPEAKER_BACKGROUND_NOTICES["pick_not_found"])
    if media is not None and media.kind not in phone_drawable_visual_kinds():
        notices.append(SPEAKER_BACKGROUND_NOTICES["kind_off"])
        media = None
    if media is None:
        return strategy.model_copy(update={"background_media": None}), notices
    update: dict[str, Any] = {
        "background_media": BackgroundMedia(
            media_id=media.media_id, source_start_s=start if media.kind == "video" else 0.0
        )
    }
    popped = False
    beats = []
    for beat in strategy.reaction_beats or ():
        if beat.visual_id is None or _speaker_background_media(manifest, beat.visual_id) != media:
            beats.append(beat)
            continue
        popped = True
        if beat.sound is not None:
            beats.append(beat.model_copy(update={"visual_id": None}))
    if popped:
        update["reaction_beats"] = beats or None
    closing = strategy.closing_media
    if closing is not None:
        if _speaker_background_media(manifest, closing.visual_id) == media:
            update["closing_media"] = None
            popped = True
        elif (
            closing.badge_visual_id is not None
            and _speaker_background_media(manifest, closing.badge_visual_id) == media
        ):
            update["closing_media"] = closing.model_copy(update={"badge_visual_id": None})
            popped = True
    if popped:
        notices.append(SPEAKER_BACKGROUND_NOT_ALSO_A_POP_IN_NOTICE)
    return strategy.model_copy(update=update), notices


def _drop_talking_clip_intents(strategy: CreativeStrategy) -> tuple[CreativeStrategy, list[str]]:
    """Strip footage clip intents from a Talking edit, saying so once.

    Its renderers never draw them (`CLIP_INTENT_FREE_EDIT_FORMATS`), so keeping
    them would approve labels or chapter captions the render silently drops.
    Transcript labels stay for the narration check below. Flag off, visual
    intents are already discarded at the model boundary, so this stays
    byte-identical there.
    """

    if not settings.clip_intents_enabled or strategy.edit_format not in (
        CLIP_INTENT_FREE_EDIT_FORMATS
    ):
        return strategy, []
    intents = strategy.clip_intents or []
    kept = [intent for intent in intents if intent.label_source == "transcript"]
    if len(kept) == len(intents) and not strategy.resolved_clip_intents:
        return strategy, []
    stripped = strategy.model_copy(
        update={"clip_intents": kept or None, "resolved_clip_intents": None}
    )
    return stripped, ([TALKING_CLIP_INTENTS_DROPPED_NOTICE] if len(kept) != len(intents) else [])


def compile_strategy_to_plan(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
) -> CreatorEditPlan:
    """Compile a strategy into only the bounded, currently available commands.

    The strategy is advisory.  The manifest's server policy wins, and an
    audio-led/voiceover item can never be compiled into a guided proposal even
    if an agent asks for one.
    """

    # KRI-118 item 2: repair a chat-picked story shape (day_vlog/single_hero)
    # and any stale/hidden-format strategy BEFORE the render program is
    # resolved below, so this function never raises over those specific
    # cases -- see app.agents._schemas.creator_policy.repair_creator_strategy_shape.
    strategy, shape_notices = repair_creator_strategy_shape(
        manifest,
        strategy,
        shapes_enabled=settings.creator_montage_shapes_enabled,
    )
    # KRI-178 repair (a): beats/closing present but the capability is
    # unavailable for this manifest -- strip them with a plain notice
    # rather than rejecting the whole strategy. Repairs (b)/(c) (resolving
    # visual_id/badge_visual_id/sound against the live manifest) happen in
    # the same pass.
    strategy, beat_notices = _repair_creator_reaction_beats(manifest, strategy)
    shape_notices = [*shape_notices, *beat_notices]
    # KRI-841: a video appended after the talk, with its own sound; same entry as the beats so
    # the dispatched strategy is the repaired one.
    strategy, ending_notices = _repair_creator_ending_clip(manifest, strategy)
    shape_notices = [*shape_notices, *ending_notices]
    # KRI-1117: the photo / looping video behind the speaker (the creator's camera pick fills in
    # an agent that left it out); same entry so receipts judge exactly what the worker resolves.
    strategy, background_notices = _repair_creator_speaker_background(manifest, strategy)
    shape_notices = [*shape_notices, *background_notices]
    # Exact-timed small cards: the same entry, so the dispatched strategy is the repaired one
    # and the receipts judge exactly what the worker will ground.
    strategy, timed_notices = _repair_creator_timed_cards(manifest, strategy)
    shape_notices = [*shape_notices, *timed_notices]
    # KRI-374: validate/repair the uploaded-song fields. A no-op for every
    # strategy that is not `audio_strategy="user_song"`.
    try:
        strategy, song_notices = repair_creator_user_song(manifest, strategy)
    except UserSongUnavailableError as exc:
        raise CreatorCapabilityError(
            str(exc), code=exc.code, edit_format=strategy.edit_format
        ) from exc
    shape_notices = [*shape_notices, *song_notices]
    # KRI-479: a stray `voice_mode` (no camera-audio montage) is dropped; a no-op otherwise.
    strategy, voice_notices = repair_creator_voice_mode(
        strategy,
        route_available=(
            settings.kria_plan_authority_enabled
            if strategy.voice_mode == "per_clip"
            else settings.voice_behind_footage_enabled
        ),
    )
    shape_notices = [*shape_notices, *voice_notices]
    try:
        strategy = normalize_creator_strategy_media(manifest, strategy)
    except UserSongUnavailableError as exc:
        raise CreatorCapabilityError(
            str(exc), code=exc.code, edit_format=strategy.edit_format
        ) from exc
    except (MixedMediaTimingUnavailableError, MontageCadenceUnavailableError):
        # These established, actionable policy errors have dedicated Creator
        # recovery flows. Preserve their types instead of flattening them into
        # the generic invalid-strategy bucket.
        raise
    except ValueError as exc:
        if "edit format" in str(exc) and "unavailable" in str(exc):
            raise CreatorCapabilityError(
                str(exc),
                edit_format=strategy.edit_format,
            ) from exc
        raise CreatorStrategyError(str(exc)) from exc
    strategy, talking_notices = _drop_talking_clip_intents(strategy)
    shape_notices = [*shape_notices, *talking_notices]
    if any(intent.label_source == "transcript" for intent in (strategy.clip_intents or [])) and (
        strategy.execution_contract != "guided_voiceover_v1" or manifest.narration is None
    ):
        raise CreatorStrategyError("Narration labels require a recorded guided voiceover.")
    licensed_sfx = strategy.licensed_sfx
    if licensed_sfx is not None:
        sound_effects = manifest.capabilities.get(
            "sound_effects", _unavailable("not_advertised", "sound effects are unavailable")
        )
        if not sound_effects.available:
            reaction_beats_cap = manifest.capabilities.get(CAPABILITY_REACTION_BEATS)
            if reaction_beats_cap is not None and reaction_beats_cap.available:
                # KRI-178 repair (d): the beats lane places sound at the exact
                # moments the creator named -- drop the unavailable general
                # sound-effect treatment instead of failing the whole strategy.
                strategy = strategy.model_copy(update={"licensed_sfx": None})
                licensed_sfx = None
                shape_notices.append(
                    "Sound effects on iPhone are placed at the moments you named; the "
                    "general sound-effect treatment was left out."
                )
            else:
                raise CreatorSfxUnavailableError(
                    # No other effect would work either, so don't suggest one.
                    "Sound effects can't render on your iPhone yet. "
                    "Ask for this edit without the sound effect."
                    if sound_effects.reason_code == "unsupported_on_phone"
                    else (
                        "The requested licensed sound effect is unavailable. Choose another effect."
                    )
                )
        if licensed_sfx is not None:
            resolved = resolve_creator_sfx_catalog_ref(manifest, licensed_sfx.effect_id)
            if resolved is None:
                raise CreatorSfxUnavailableError(
                    f"The requested licensed sound effect {licensed_sfx.effect_id!r} "
                    "is unavailable."
                )
            # Persist the canonical server-owned id, including when the model or
            # client supplied a different case or the catalog label was used.
            strategy = strategy.model_copy(
                update={
                    "licensed_sfx": licensed_sfx.model_copy(
                        update={"effect_id": resolved.catalog_id}
                    )
                }
            )
    phone_capability = manifest.capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO)
    # KRI-467: a phone Talking edit draws the title through its text lane;
    # `PHONE_SUBTITLED_TITLE_ENABLED=false` restores the refusal below.
    phone_talking_title = bool(
        strategy.opening_title
        and strategy.edit_format == "subtitled"
        and phone_talking_title_lane_open(manifest)
    )
    if strategy.opening_title and strategy.edit_format == "subtitled" and not phone_talking_title:
        # Caption-owned cloud subtitled edits still do not render a hero intro.
        # Fail at the plan boundary rather than silently dropping confirmed copy.
        raise CreatorStrategyError(
            f"opening_title is not supported by the {strategy.edit_format} renderer",
            code="unsupported_treatment",
            edit_format=strategy.edit_format,
        )
    if (
        strategy.opening_title
        and strategy.edit_format in NARRATED_EDIT_FORMATS
        and strategy.render_program != "guided"
        and phone_capability is not None
        and phone_capability.available
        and not phone_narrated_title_supported()
    ):
        # KRI-142: the phone voiceover compiler (`compile_phone_narrated_plan`)
        # had no title lane. KRI-455 added one; this refusal is now only its
        # kill switch (`PHONE_NARRATED_TITLE_ENABLED=false`).
        raise CreatorStrategyError(
            f"opening_title is not supported by the phone {strategy.edit_format} renderer",
            code="unsupported_treatment",
            edit_format=strategy.edit_format,
        )
    # KRI-514: the same text lane draws the creator's closing text ("end on the
    # toast photo with a 'MY PICK' badge"); `PHONE_SUBTITLED_CLOSING_TITLE_ENABLED
    # =false` restores the refusal below.
    phone_talking_closing = bool(
        strategy.closing_title
        and strategy.edit_format == "subtitled"
        and phone_capability is not None
        and phone_capability.available
        and phone_subtitled_closing_title_supported()
    )
    # KRI-526: the strategy parses up to 12 lines / 400 characters so an over-limit ask lands
    # here and becomes a question about the pins (strategy_policy), not a schema failure.
    if len(strategy.pinned_texts or ()) > MAX_CREATOR_PINNED_TEXTS:
        raise CreatorStrategyError(
            "pinned_texts_too_many: more corner lines than fit",
            code="unsupported_treatment",
            edit_format=strategy.edit_format,
        )
    if any(not pin.fits_a_corner for pin in strategy.pinned_texts or ()):
        raise CreatorStrategyError(
            "pinned_texts_too_long: a corner line is longer than one line",
            code="unsupported_treatment",
            edit_format=strategy.edit_format,
        )
    # A recorded voiceover makes the item's program "native", which no cloud renderer draws
    # pins for; the phone montage-family voiceover writer does (KRI-527), so a phone cohort
    # keeps its pins there.
    phone_voiceover_montage = bool(
        phone_capability is not None
        and phone_capability.available
        and manifest.has_voiceover
        and strategy.edit_format in GUIDED_EDIT_FORMATS
    )
    if strategy.pinned_texts and (
        strategy.edit_format == "subtitled"
        or (strategy.render_program != "guided" and not phone_voiceover_montage)
    ):
        # KRI-523: corner text is drawn by the guided text compiler (cloud) and by every phone
        # montage writer (unified, lip-sync, spoken-excerpt, voice-behind-footage and the
        # recorded-voiceover montage - KRI-527). A subtitled talking edit has no free text lane.
        raise CreatorStrategyError(
            f"pinned_texts is not supported by the {strategy.edit_format} renderer",
            code="unsupported_treatment",
            edit_format=strategy.edit_format,
        )
    if strategy.shot_labels or (strategy.closing_title and not phone_talking_closing):
        # Exact per-shot labels and closing copy are burned by the guided
        # story-beat renderer only. Fail visibly anywhere they cannot render
        # instead of approving an edit that silently drops the creator's words.
        exact_copy_field = "shot_labels" if strategy.shot_labels else "closing_title"
        if strategy.edit_format == "subtitled" or strategy.render_program != "guided":
            raise CreatorStrategyError(
                f"{exact_copy_field} is not supported by the {strategy.edit_format} renderer",
                code="unsupported_treatment",
                edit_format=strategy.edit_format,
            )
        if strategy.shot_labels and (
            strategy.mixed_media_timing is not None or strategy.montage_cadence is not None
        ):
            raise CreatorStrategyError(
                "shot_labels cannot be combined with exact photo/video cut timing",
                code="unsupported_treatment",
                edit_format=strategy.edit_format,
            )
        if strategy.shot_labels and strategy.direction == "fast_montage":
            # Fast-cut plans carry no per-shot text lane; a label per shot is
            # a story-beat structure.
            strategy = strategy.model_copy(update={"direction": "guided_story"})
    elif (
        strategy.opening_title_duration_s is not None
        and strategy.render_program != "guided"
        and not phone_talking_title
    ):
        # The native intro owns its own timing; a hold preference it cannot
        # honor must not turn an otherwise renderable title into a failure.
        # The phone Talking title holds for exactly the confirmed seconds.
        strategy = strategy.model_copy(update={"opening_title_duration_s": None})
    strategy_format = coerce_edit_format(strategy.edit_format)
    if strategy.overlay_display in FULLSCREEN_OVERLAY_DISPLAYS:
        fullscreen_cap = manifest.capabilities.get(CAPABILITY_MEDIA_OVERLAY_FULLSCREEN)
        if fullscreen_cap is None or not fullscreen_cap.available:
            phone_cap = manifest.capabilities.get(CAPABILITY_PHONE_SOURCE_AUDIO)
            if phone_cap is not None and phone_cap.available:
                # Honest refusal: never silently fall back to small cards.
                raise CreatorStrategyError(
                    "Full-screen Visuals are only available on iPhone Talking-to-camera "
                    "edits right now, and not for this draft. I can add them as small "
                    "cards instead if you'd like.",
                    code="unsupported_treatment",
                    edit_format=strategy.edit_format,
                )
            # Cloud/web renders have their own per-overlay full-screen toggle.
            strategy = strategy.model_copy(update={"overlay_display": None})
        elif strategy.overlay_display == "fullscreen_videos":
            # KRI-661: videos only play as cards/cutaways while the video-card gate is open;
            # without it every video would land in the receipt as unplaced.
            video_cap = manifest.capabilities.get(CAPABILITY_MEDIA_OVERLAY_VIDEO_CARDS)
            if video_cap is None or not video_cap.available:
                raise CreatorStrategyError(
                    "Videos can't be shown as overlays on this edit yet, so I can't play them "
                    "full screen. I can use your photos as overlays instead if you'd like.",
                    code="unsupported_treatment",
                    edit_format=strategy.edit_format,
                )
    effective_program = strategy.render_program
    selected_media_ids = list(strategy.selected_media_ids)
    treatment_capabilities = {
        "overlays": "media_overlays",
        "sfx": "sound_effects",
        "transitions": "transitions",
        "looks": "wide_looks",
    }
    available_treatments = [
        treatment
        for treatment in strategy.optional_treatments
        if manifest.capabilities.get(
            treatment_capabilities[treatment],
            _unavailable("not_advertised", "capability not advertised"),
        ).available
    ]
    effective_strategy = strategy.model_copy(
        update={
            "edit_format": strategy_format,
            # KRI-118 item 2: `archetype` now carries the chat-picked story
            # SHAPE (day_vlog/single_hero), not a mirror of edit_format --
            # `repair_creator_strategy_shape` above already dropped it when
            # it is not actually renderable for this manifest/program, so
            # whatever survives here is safe to forward as-is.
            "render_program": effective_program,
            # The guided specialist owns exact beat/media selection from the
            # approved item pool. Do not preserve a model-selected subset that
            # the ProposalBrief contract cannot enforce.
            "selected_media_ids": (
                selected_media_ids
                if effective_program == "native"
                else (
                    [media.media_id for media in manifest.media]
                    if strategy.media_scope != "selected"
                    else selected_media_ids
                )
            ),
            "optional_treatments": available_treatments,
        }
    )

    def capability(name: str) -> CapabilityAvailability:
        return manifest.capabilities.get(
            name,
            _unavailable("not_advertised", f"{name} is not advertised by this manifest"),
        )

    commands = []
    if capability(CAPABILITY_SET_ITEM_INTENT).available:
        commands.append(
            SetItemIntentCommand(
                command="set_item_intent",
                edit_format=strategy_format,
                expected_manifest_hash=manifest.manifest_hash,
            )
        )
    if effective_program == "guided" and capability(CAPABILITY_DRAFT_GUIDED_PROPOSAL).available:
        commands.append(
            DraftGuidedProposalCommand(
                command="draft_guided_proposal",
                strategy=effective_strategy,
                expected_manifest_hash=manifest.manifest_hash,
            )
        )
    if capability(CAPABILITY_DISPATCH_RENDER).available:
        commands.append(
            DispatchRenderCommand(
                command="dispatch_render",
                expected_manifest_hash=manifest.manifest_hash,
                expected_context_hash=manifest.context_hash,
            )
        )
    return CreatorEditPlan(
        manifest_hash=manifest.manifest_hash,
        context_hash=manifest.context_hash,
        strategy=effective_strategy,
        commands=commands,
        notices=shape_notices,
    )


def resolve_creator_sfx_catalog_ref(
    manifest: ResolvedCreatorManifest,
    requested: str,
) -> CreatorCatalogRef | None:
    """Resolve an effect id or display name by exact case-insensitive match.

    This helper only resolves the descriptive manifest. The authenticated DB
    boundary still rechecks ready/published/non-archived state and the audio
    path before any placement is materialized.
    """

    needle = " ".join(str(requested or "").split()).casefold()
    if not needle:
        return None
    effects = [item for item in manifest.catalog if item.kind == "sound_effect"]
    matches = [
        item
        for item in effects
        if item.catalog_id.casefold() == needle or (item.label or "").strip().casefold() == needle
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def resolve_creator_image_media_ref(
    manifest: ResolvedCreatorManifest,
    requested: str | None,
) -> CreatorMediaRef | None:
    """Resolve an owned IMAGE media_id or display label by exact
    case-insensitive match (KRI-178 -- reaction-beat/closing-media
    `visual_id`/`badge_visual_id` resolution).

    Mirrors `resolve_creator_sfx_catalog_ref`'s contract exactly, scoped to
    `manifest.media` entries of `kind == "image"` instead of the sound-effect
    catalog: only the descriptive manifest is consulted here, never storage.
    """

    return _match_owned_media_ref(
        [item for item in manifest.media if item.kind == "image"], requested
    )


def resolve_creator_visual_video_media_ref(
    manifest: ResolvedCreatorManifest,
    requested: str | None,
) -> CreatorMediaRef | None:
    """Resolve a Visuals-pool VIDEO by the same exact id/label match (KRI-521).

    Only `asset-*` entries count: a reaction beat can pop a Visuals video in the
    corner, never the speaker's own take.
    """

    return _match_owned_media_ref(
        [
            item
            for item in manifest.media
            if item.kind == "video" and item.media_id.startswith("asset-")
        ],
        requested,
    )


def _match_owned_media_ref(
    candidates: list[CreatorMediaRef], requested: str | None
) -> CreatorMediaRef | None:
    needle = " ".join(str(requested or "").split()).casefold()
    if not needle:
        return None
    matches = [
        item
        for item in candidates
        if item.media_id.casefold() == needle or (item.label or "").strip().casefold() == needle
    ]
    if len(matches) != 1:
        return None
    return matches[0]


# Readable alias for callers that build rather than resolve a manifest.
build_creator_manifest = resolve_creator_manifest
# Public name for the Creative Brief checks (KRI-188), which must judge reaction
# beats exactly as approval will keep them.
repair_creator_reaction_beats = _repair_creator_reaction_beats
# ... and the ending clip (KRI-841), judged by the same checks.
repair_creator_ending_clip = _repair_creator_ending_clip
repair_creator_speaker_background = _repair_creator_speaker_background
# ... and the exact-timed small cards, judged by the same checks.
repair_creator_timed_cards = _repair_creator_timed_cards


__all__ = [
    "CAPABILITY_TIMED_CARDS",
    "TIMED_CARDS_UNAVAILABLE_NOTICE",
    "repair_creator_timed_cards",
    "CAPABILITY_USER_SONG",
    "CAPABILITY_DISPATCH_RENDER",
    "CAPABILITY_CAPTION_STYLE",
    "CAPABILITY_DRAFT_GUIDED_PROPOSAL",
    "CAPABILITY_GUIDED_VOICEOVER",
    "CAPABILITY_GUIDED_STORY",
    "CAPABILITY_NATIVE_RENDER",
    "CAPABILITY_REACTION_BEATS",
    "CAPABILITY_SELECT_READY_VARIANT",
    "ENDING_CLIP_NOTICES",
    "CAPABILITY_SET_ITEM_INTENT",
    "CreatorCapabilityError",
    "CreatorStrategyError",
    "CreatorSfxUnavailableError",
    "MAX_MAIN_CREATOR_SELECTED_MEDIA",
    "build_creator_manifest",
    "compile_strategy_to_plan",
    "effective_render_program",
    "ending_clip_candidates",
    "ending_clip_problem",
    "normalize_creator_strategy_media",
    "phone_talking_title_lane_open",
    "repair_creator_ending_clip",
    "repair_creator_speaker_background",
    "speaker_background_open",
    "speaker_background_pick_from_state",
    "canonical_pool_media_id",
    "SPEAKER_BACKGROUND_STATE_KEY",
    "repair_creator_reaction_beats",
    "resolve_creator_image_media_ref",
    "resolve_creator_visual_video_media_ref",
    "resolve_creator_manifest",
    "resolve_creator_sfx_catalog_ref",
]
