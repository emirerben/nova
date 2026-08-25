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
    DispatchRenderCommand,
    DraftGuidedProposalCommand,
    ResolvedCreatorManifest,
    SetItemIntentCommand,
    canonical_context_hash,
    canonical_manifest_hash,
)
from app.agents._schemas.edit_format import (
    AUDIO_LED_EDIT_FORMATS,
    EDIT_FORMATS,
    coerce_edit_format,
    guided_edit_applicable,
    render_program_for_intent,
)
from app.config import settings

CAPABILITY_SET_ITEM_INTENT = "set_item_intent"
CAPABILITY_GUIDED_STORY = "guided_story"
CAPABILITY_NATIVE_RENDER = "native_render"
CAPABILITY_DRAFT_GUIDED_PROPOSAL = "draft_guided_proposal"
CAPABILITY_DISPATCH_RENDER = "dispatch_render"
CAPABILITY_SELECT_READY_VARIANT = "select_ready_variant"

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
        if has_voiceover:
            return _unavailable(
                "native_render_required",
                "day_vlog uses the guided renderer and cannot carry a voiceover",
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
) -> ResolvedCreatorManifest:
    """Resolve a descriptive v1 manifest from server state and policy.

    ``render_program_for_intent`` and ``guided_edit_applicable`` are the same
    policy used by the existing plan-item/generative routes.  In particular,
    audio-led formats and voiceover items cannot be advertised as guided edits.
    """

    resolved_media = _as_media_refs(media)
    resolved_catalog = _as_catalog_refs(catalog)
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
    guided_executable = settings.guided_edit_capability_enabled and guided_applicable_now
    has_dispatchable_media = has_native_media or (
        render_program == "guided" and guided_executable and has_media
    )

    if not settings.guided_edit_capability_enabled:
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
    }

    for capability_name, setting_name in _FEATURE_SETTINGS.items():
        if getattr(settings, setting_name, False):
            capabilities[capability_name] = _available()
        else:
            capabilities[capability_name] = _unavailable(
                "disabled_by_setting", f"{capability_name} is disabled by the server"
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
        "current_edit": resolved_edit,
        "media": resolved_media,
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
        current_edit=resolved_edit,
        media=resolved_media,
        catalog=resolved_catalog,
        capabilities=capabilities,
        limits=resolved_limits,
        context_hash=context_hash,
        manifest_hash="0" * 64,
    )
    return manifest.model_copy(update={"manifest_hash": canonical_manifest_hash(manifest)})


def compile_strategy_to_plan(
    manifest: ResolvedCreatorManifest,
    strategy: CreativeStrategy,
) -> CreatorEditPlan:
    """Compile a strategy into only the bounded, currently available commands.

    The strategy is advisory.  The manifest's server policy wins, and an
    audio-led/voiceover item can never be compiled into a guided proposal even
    if an agent asks for one.
    """

    media_ids = {media.media_id for media in manifest.media}
    if any(media_id not in media_ids for media_id in strategy.selected_media_ids):
        raise ValueError("strategy selected_media_ids must reference manifest media")

    strategy_format = coerce_edit_format(strategy.edit_format)
    format_capability = manifest.capabilities.get(f"edit_format:{strategy_format}")
    if format_capability is None or not format_capability.available:
        raise ValueError(f"edit format {strategy_format!r} is unavailable")
    audio_requires_native = strategy.audio_strategy in {"original_audio", "voiceover"}
    native_required = (
        manifest.has_voiceover
        or audio_requires_native
        or strategy_format in AUDIO_LED_EDIT_FORMATS
        or not guided_edit_applicable(strategy_format, has_voiceover=manifest.has_voiceover)
    )
    if native_required:
        effective_program = "native"
    elif (
        strategy.render_program == "guided"
        and manifest.capabilities.get(CAPABILITY_DRAFT_GUIDED_PROPOSAL)
        and manifest.capabilities[CAPABILITY_DRAFT_GUIDED_PROPOSAL].available
    ):
        effective_program = "guided"
    else:
        # Native is the safe universal fallback for a strategy that explicitly
        # chooses it or when guided planning is unavailable. The compiler never
        # upgrades a native request into guided execution.
        effective_program = "native"
    selected_media_ids = list(strategy.selected_media_ids)
    if effective_program == "native":
        native_ids = {
            media.media_id for media in manifest.media if not media.media_id.startswith("asset-")
        }
        selected_media_ids = [media_id for media_id in selected_media_ids if media_id in native_ids]
        if not selected_media_ids:
            raise ValueError("native rendering requires at least one attached clip")
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
            # The renderer resolves its archetype from edit_format. Keeping a
            # divergent advisory archetype would promise a format it cannot use.
            "archetype": strategy_format,
            "render_program": effective_program,
            # The guided specialist owns exact beat/media selection from the
            # approved item pool. Do not preserve a model-selected subset that
            # the ProposalBrief contract cannot enforce.
            "selected_media_ids": (
                selected_media_ids
                if effective_program == "native"
                else [media.media_id for media in manifest.media]
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
    )


# Readable alias for callers that build rather than resolve a manifest.
build_creator_manifest = resolve_creator_manifest


__all__ = [
    "CAPABILITY_DISPATCH_RENDER",
    "CAPABILITY_DRAFT_GUIDED_PROPOSAL",
    "CAPABILITY_GUIDED_STORY",
    "CAPABILITY_NATIVE_RENDER",
    "CAPABILITY_SELECT_READY_VARIANT",
    "CAPABILITY_SET_ITEM_INTENT",
    "build_creator_manifest",
    "compile_strategy_to_plan",
    "resolve_creator_manifest",
]
