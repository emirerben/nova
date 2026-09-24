"""Server-owned capability resolution for the Main Creator Agent.

This module only describes what is available for a snapshot.  It does not mint
storage URLs, mutate rows, dispatch Celery work, or execute agent commands.
Execution remains the responsibility of a separately authenticated route.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from app.agents._schemas.creator_agent import (
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
    canonical_context_hash,
    canonical_manifest_hash,
)
from app.agents._schemas.creator_policy import (
    CAPABILITY_DRAFT_GUIDED_PROPOSAL,
    CAPABILITY_GUIDED_VOICEOVER,
    CAPABILITY_PHONE_SOURCE_AUDIO,
    CAPABILITY_PHONE_STILL_IMAGES,
    CAPABILITY_PHONE_VISUAL_VIDEOS,
    MAX_MAIN_CREATOR_SELECTED_MEDIA,
    MixedMediaTimingUnavailableError,
    MontageCadenceUnavailableError,
    effective_render_program,
    normalize_creator_strategy_media,
    repair_creator_strategy_shape,
)
from app.agents._schemas.edit_format import (
    EDIT_FORMATS,
    GUIDED_EDIT_FORMATS,
    NARRATED_EDIT_FORMATS,
    coerce_edit_format,
    guided_edit_applicable,
    render_program_for_intent,
)
from app.config import settings
from app.services.creator_errors import CreatorCapabilityError, CreatorStrategyError
from app.services.phone_destination import phone_drawable_visual_kinds
from app.services.phone_rollout import (
    phone_guided_narration_supported,
    phone_render_supported_formats,
    phone_subtitled_overlays_supported,
    phone_subtitled_reaction_beats_supported,
    phone_subtitled_video_overlays_supported,
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

# Treatments compile_phone_guided_plan rejects: the SFX, overlay, visual-block and
# motion-scene lanes outright, and an edit-wide look on any source that is not
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
) -> ResolvedCreatorManifest:
    """Resolve a descriptive v1 manifest from server state and policy.

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
                    phone_format_capability = (
                        _available()
                        if (
                            candidate_format in supported_now
                            and clip_count == 1
                            and not has_voiceover
                        )
                        else _unavailable(
                            "phone_format_unavailable",
                            "talking-to-camera rendering on this iPhone requires exactly "
                            "one clip and no recorded voiceover",
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
    if is_phone_subtitled:
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
    context_hash = canonical_context_hash(context_payload)
    manifest = ResolvedCreatorManifest(
        item_id=item_id,
        edit_format=normalized_format,
        render_program=render_program,
        has_voiceover=has_voiceover,
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
    when it doesn't resolve) and every `sound` against the sound-effect
    catalog (canonical id when it matches, otherwise left as the creator's own
    words for the worker to resolve by description).
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

    beats = []
    for beat in strategy.reaction_beats or []:
        visual_id = beat.visual_id
        if visual_id is not None:
            resolved_media = resolve_creator_image_media_ref(manifest, visual_id)
            if resolved_media is None:
                notices.append(f"Couldn't find \"{beat.trigger}\"'s photo/sticker; left it out.")
                continue
            visual_id = resolved_media.media_id
        beats.append(
            beat.model_copy(update={"visual_id": visual_id, "sound": _resolve_sound(beat.sound)})
        )
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
    try:
        strategy = normalize_creator_strategy_media(manifest, strategy)
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
    if strategy.opening_title and strategy.edit_format == "subtitled":
        # Caption-owned subtitled edits still do not render a hero intro. Fail
        # at the plan boundary rather than silently dropping confirmed copy.
        raise CreatorStrategyError(
            f"opening_title is not supported by the {strategy.edit_format} renderer",
            code="unsupported_treatment",
            edit_format=strategy.edit_format,
        )
    if strategy.shot_labels or strategy.closing_title:
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
    elif strategy.opening_title_duration_s is not None and strategy.render_program != "guided":
        # The native intro owns its own timing; a hold preference it cannot
        # honor must not turn an otherwise renderable title into a failure.
        strategy = strategy.model_copy(update={"opening_title_duration_s": None})
    strategy_format = coerce_edit_format(strategy.edit_format)
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

    needle = " ".join(str(requested or "").split()).casefold()
    if not needle:
        return None
    images = [item for item in manifest.media if item.kind == "image"]
    matches = [
        item
        for item in images
        if item.media_id.casefold() == needle or (item.label or "").strip().casefold() == needle
    ]
    if len(matches) != 1:
        return None
    return matches[0]


# Readable alias for callers that build rather than resolve a manifest.
build_creator_manifest = resolve_creator_manifest


__all__ = [
    "CAPABILITY_DISPATCH_RENDER",
    "CAPABILITY_CAPTION_STYLE",
    "CAPABILITY_DRAFT_GUIDED_PROPOSAL",
    "CAPABILITY_GUIDED_VOICEOVER",
    "CAPABILITY_GUIDED_STORY",
    "CAPABILITY_NATIVE_RENDER",
    "CAPABILITY_REACTION_BEATS",
    "CAPABILITY_SELECT_READY_VARIANT",
    "CAPABILITY_SET_ITEM_INTENT",
    "CreatorCapabilityError",
    "CreatorStrategyError",
    "CreatorSfxUnavailableError",
    "MAX_MAIN_CREATOR_SELECTED_MEDIA",
    "build_creator_manifest",
    "compile_strategy_to_plan",
    "effective_render_program",
    "normalize_creator_strategy_media",
    "resolve_creator_image_media_ref",
    "resolve_creator_manifest",
    "resolve_creator_sfx_catalog_ref",
]
