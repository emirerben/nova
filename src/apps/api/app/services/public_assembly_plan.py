"""Projection of persisted assembly JSON onto creator-visible API surfaces."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

SPEECH_CLEANUP_INTERNAL_FIELD = "_speech_cleanup_internal"

_PRIVATE_IDENTITY_FIELDS = frozenset({"clip_source_instance_ids", "source_tag"})
_PRIVATE_IDENTITY_PREFIXES = ("clip_metadata_identity_index_v",)
_PRIVATE_SPEECH_CONTROL_FIELDS = frozenset(
    {
        "_speech_cleanup_outcome_context",
        "speech_cleanup_outcome_context",
        "speech_cut_control",
        "speech_cut_previous_variant",
        "speech_cut_previous_variants",
    }
)
_ACTIVE_SPEECH_INTERNAL_FIELDS = frozenset(
    {
        "required_speech_generation_locks",
        "staged_render_results",
        "working_render_variants",
        "terminal_pending",
    }
)
_IN_FLIGHT_PUBLIC_FIELDS = (
    "render_started_at",
    "render_generation_id",
    "speech_cut_in_flight",
    "speech_cut_candidates",
    "speech_cut_last_error",
)
_MEDIA_REFERENCE_FIELDS = frozenset(
    {
        "artifact",
        "artifacts",
        "background_music",
        "background_music_treatment",
        "download",
        "downloads",
        "media",
        "media_overlays",
        "output",
        "outputs",
        "smart_music_treatment",
        "sound_effects",
        "source_audio_options",
        "source_references",
        "uploaded_lane_artifact_paths",
        "uploaded_lane_artifacts",
        "visual_block_preview_urls",
        "visual_blocks",
        "visual_previews",
    }
)
_MEDIA_IDENTITY_FIELDS = frozenset(
    {
        "artifact_id",
        "artifact_ids",
        "asset_id",
        "asset_ids",
        "clip_id",
        "clip_ids",
        "etag",
        "generation",
        "generation_id",
        "generation_ids",
        "media_id",
        "media_ids",
        "music_track_id",
        "render_generation_id",
        "render_generation_ids",
        "sound_effect_id",
        "source_id",
        "source_ids",
        "source_instance_id",
        "source_instance_ids",
        "source_tag",
        "spine_clip_id",
        "storage_generation",
        "storage_generation_id",
        "track_id",
        "track_title",
        "upload_id",
        "upload_ids",
    }
)
_MEDIA_LOCATOR_PREFIXES = (
    "http://",
    "https://",
    "gs://",
    "s3://",
    "file://",
    "data:audio/",
    "data:image/",
    "data:video/",
    "generative-jobs/",
    "music-jobs/",
    "template-jobs/",
    "music-uploads/",
    "slot-uploads/",
    "users/",
)
_MEDIA_EXTENSIONS = (
    ".aac",
    ".avif",
    ".gif",
    ".heic",
    ".heif",
    ".jpeg",
    ".jpg",
    ".m4a",
    ".m4v",
    ".mov",
    ".mp3",
    ".mp4",
    ".ogg",
    ".png",
    ".wav",
    ".webm",
    ".webp",
)


@dataclass(frozen=True)
class PublicAssemblyPlanProjection:
    """Creator-safe value plus server-only facts learned while projecting it.

    None of the metadata enters the JSON value.  Response serializers use the
    last-good set to refresh proved rollback media and the unavailable/active
    facts to avoid reconstructing URLs from unprojected job state.
    """

    value: Any
    masked_last_good_variant_ids: frozenset[str]
    media_unavailable_variant_ids: frozenset[str]
    active_speech_projection: bool


@dataclass(frozen=True)
class _ActiveSpeechProjectionState:
    active: bool
    target_ids: frozenset[str]
    generations: frozenset[str]
    target_uncertain: bool
    generation_uncertain: bool


def _is_private_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    return (
        key == SPEECH_CLEANUP_INTERNAL_FIELD
        or key in _PRIVATE_IDENTITY_FIELDS
        or key in _PRIVATE_SPEECH_CONTROL_FIELDS
        or key.startswith(_PRIVATE_IDENTITY_PREFIXES)
    )


def _nonblank_token(value: object) -> str | None:
    return value if isinstance(value, str) and value and value == value.strip() else None


def _stage_key_owner(value: object) -> tuple[str | None, str | None]:
    if not isinstance(value, str):
        return None, None
    variant_id, separator, generation = value.rpartition(":")
    if not separator:
        return None, None
    return _nonblank_token(variant_id), _nonblank_token(generation)


def _active_speech_projection_state(plan: dict[str, Any]) -> _ActiveSpeechProjectionState:
    """Collect every independently persisted hint about a private generation.

    This is deliberately tolerant enough to identify affected rows in corrupt
    plans, but records uncertainty whenever a malformed value prevents exact
    ownership proof.  Uncertainty is consumed only by the fail-closed public
    projection; it never repairs or mutates persisted state.
    """

    active = False
    target_ids: set[str] = set()
    generations: set[str] = set()
    target_uncertain = False
    generation_uncertain = False

    control = plan.get("speech_cut_control")
    if control not in (None, {}):
        active = True
        if not isinstance(control, dict):
            target_uncertain = True
            generation_uncertain = True
        else:
            variant_id = _nonblank_token(control.get("variant_id"))
            generation = _nonblank_token(control.get("render_generation_id"))
            if variant_id is None:
                target_uncertain = True
            else:
                target_ids.add(variant_id)
            if generation is None:
                generation_uncertain = True
            else:
                generations.add(generation)

            claim = control.get("finalizer_claim")
            if claim not in (None, {}):
                if not isinstance(claim, dict):
                    generation_uncertain = True
                else:
                    claim_generation = _nonblank_token(claim.get("render_generation_id"))
                    if claim_generation is None:
                        generation_uncertain = True
                    else:
                        generations.add(claim_generation)

    # The required-v1 discriminator survives normal terminal publication, so
    # it cannot make every completed job private by itself.  It *does* provide
    # independent evidence of an unfinished private generation when a variant
    # is explicitly nonterminal (or still carries its in-flight edit capsule).
    # This closes the corrupt-row case where the control/lock vanished before
    # the public row was restored.  A malformed/missing variant vector under a
    # required contract is likewise ambiguous and must not leave a top-level
    # output mirror exposed.
    if plan.get("speech_cleanup_contract") == "required_v1":
        variants = plan.get("variants")
        if not isinstance(variants, list) or not variants:
            active = True
            target_uncertain = True
            generation_uncertain = True
        else:
            for row in variants:
                if not isinstance(row, dict):
                    active = True
                    target_uncertain = True
                    generation_uncertain = True
                    continue
                if row.get("render_status") not in {"pending", "rendering"} and row.get(
                    "speech_cut_in_flight"
                ) in (None, {}):
                    continue
                active = True
                variant_id = _nonblank_token(row.get("variant_id"))
                generation = _nonblank_token(row.get("render_generation_id"))
                if variant_id is None:
                    target_uncertain = True
                else:
                    target_ids.add(variant_id)
                if generation is None:
                    generation_uncertain = True
                else:
                    generations.add(generation)

    internal = plan.get(SPEECH_CLEANUP_INTERNAL_FIELD)
    if internal not in (None, {}):
        if not isinstance(internal, dict):
            active = True
            target_uncertain = True
            generation_uncertain = True
        else:
            locks = internal.get("required_speech_generation_locks")
            if locks not in (None, {}):
                active = True
                if not isinstance(locks, dict):
                    target_uncertain = True
                    generation_uncertain = True
                else:
                    for raw_variant_id, raw_generation in locks.items():
                        variant_id = _nonblank_token(raw_variant_id)
                        generation = _nonblank_token(raw_generation)
                        if variant_id is None:
                            target_uncertain = True
                        else:
                            target_ids.add(variant_id)
                        if generation is None:
                            generation_uncertain = True
                        else:
                            generations.add(generation)

            for field in _ACTIVE_SPEECH_INTERNAL_FIELDS - {"required_speech_generation_locks"}:
                container = internal.get(field)
                if container in (None, {}):
                    continue
                active = True
                if not isinstance(container, dict):
                    target_uncertain = True
                    generation_uncertain = True
                    continue
                for raw_key, raw_row in container.items():
                    key_variant_id, key_generation = _stage_key_owner(raw_key)
                    row_variant_id = (
                        _nonblank_token(raw_row.get("variant_id"))
                        if isinstance(raw_row, dict)
                        else None
                    )
                    row_generation = (
                        _nonblank_token(raw_row.get("render_generation_id"))
                        if isinstance(raw_row, dict)
                        else None
                    )
                    found_variant_ids = {
                        value for value in (key_variant_id, row_variant_id) if value is not None
                    }
                    found_generations = {
                        value for value in (key_generation, row_generation) if value is not None
                    }
                    target_ids.update(found_variant_ids)
                    generations.update(found_generations)
                    if not found_variant_ids:
                        target_uncertain = True
                    if not found_generations:
                        generation_uncertain = True
                    if not isinstance(raw_row, dict):
                        target_uncertain = target_uncertain or not found_variant_ids
                        generation_uncertain = generation_uncertain or not found_generations

    previous_variant = plan.get("speech_cut_previous_variant")
    previous_variants = plan.get("speech_cut_previous_variants")
    if previous_variant not in (None, {}) or previous_variants not in (None, []):
        active = True
    if isinstance(previous_variant, dict):
        prior_variant_id = _nonblank_token(previous_variant.get("variant_id"))
        if prior_variant_id is not None:
            target_ids.add(prior_variant_id)
    elif previous_variant not in (None, {}):
        target_uncertain = True
    if previous_variants not in (None, []) and not isinstance(previous_variants, list):
        target_uncertain = True

    if active and not target_ids:
        target_uncertain = True
    if active and not generations:
        generation_uncertain = True
    return _ActiveSpeechProjectionState(
        active=active,
        target_ids=frozenset(target_ids),
        generations=frozenset(generations),
        target_uncertain=target_uncertain,
        generation_uncertain=generation_uncertain,
    )


def _variant_map(variants: object) -> dict[str, dict[str, Any]] | None:
    if not isinstance(variants, list) or any(not isinstance(item, dict) for item in variants):
        return None
    mapped: dict[str, dict[str, Any]] = {}
    for item in variants:
        variant_id = _nonblank_token(item.get("variant_id"))
        if variant_id is None or variant_id in mapped:
            return None
        mapped[variant_id] = item
    return mapped


def _value_references_generation(value: object, generations: frozenset[str]) -> bool:
    if isinstance(value, str):
        return any(
            value == generation
            or f"/render-generations/{generation}/" in value
            or f"%2Frender-generations%2F{generation}%2F" in value
            for generation in generations
        )
    if isinstance(value, dict):
        return any(_value_references_generation(item, generations) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_value_references_generation(item, generations) for item in value)
    return False


def _safe_previous_variant_vector(
    plan: dict[str, Any],
    *,
    current: dict[str, dict[str, Any]],
    state: _ActiveSpeechProjectionState,
) -> list[dict[str, Any]] | None:
    if state.generation_uncertain or not state.generations:
        return None
    previous = plan.get("speech_cut_previous_variants")
    previous_map = _variant_map(previous)
    if previous_map is None or set(previous_map) != set(current):
        return None
    affected_ids = set(current) if state.target_uncertain else set(state.target_ids)
    if not affected_ids or not affected_ids.issubset(previous_map):
        return None
    if any(
        previous_map[variant_id].get("render_status") in {"pending", "rendering"}
        for variant_id in affected_ids
    ):
        return None
    if _value_references_generation(previous, state.generations):
        return None
    return copy.deepcopy(previous)


def _safe_legacy_previous_variant(
    plan: dict[str, Any],
    *,
    current: dict[str, dict[str, Any]],
    state: _ActiveSpeechProjectionState,
) -> dict[str, Any] | None:
    # When a creator bundle has a public-vector snapshot, its singular field is
    # the private editor-lane input for the new generation and is never rollback
    # authority.  The singular snapshot is supported only for legacy dispatches.
    if plan.get("speech_cut_previous_variants") not in (None, []):
        return None
    if state.target_uncertain or state.generation_uncertain or len(state.target_ids) != 1:
        return None
    variant_id = next(iter(state.target_ids))
    prior = plan.get("speech_cut_previous_variant")
    if (
        not isinstance(prior, dict)
        or prior.get("variant_id") != variant_id
        or variant_id not in current
        or prior.get("render_status") in {"pending", "rendering"}
        or not state.generations
        or _value_references_generation(prior, state.generations)
    ):
        return None
    return copy.deepcopy(prior)


def _looks_like_media_locator(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered.startswith(_MEDIA_LOCATOR_PREFIXES) or "/render-generations/" in lowered:
        return True
    path_without_query = lowered.split("?", 1)[0].split("#", 1)[0]
    return path_without_query.endswith(_MEDIA_EXTENSIONS)


def _is_media_reference_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    lowered = key.lower()
    return (
        lowered in _MEDIA_REFERENCE_FIELDS
        or lowered in _MEDIA_IDENTITY_FIELDS
        or lowered.endswith(
            (
                "_artifact_id",
                "_artifact_ids",
                "_asset_id",
                "_asset_ids",
                "_clip_id",
                "_clip_ids",
                "_filename",
                "_media_id",
                "_media_ids",
                "_path",
                "_paths",
                "_source_id",
                "_source_ids",
                "_track_id",
                "_track_ids",
                "_upload_id",
                "_upload_ids",
                "_url",
                "_urls",
            )
        )
        or lowered in {"filename", "file_name"}
    )


def _strip_media_references(value: Any) -> Any:
    """Remove every recognizable media locator/identity from an unsafe row."""

    if isinstance(value, dict):
        for key in list(value):
            nested = value[key]
            if _is_media_reference_key(key) or (
                isinstance(nested, str) and _looks_like_media_locator(nested)
            ):
                value.pop(key, None)
            else:
                value[key] = _strip_media_references(nested)
    elif isinstance(value, list):
        value[:] = [
            _strip_media_references(item)
            for item in value
            if not (isinstance(item, str) and _looks_like_media_locator(item))
        ]
    return value


def _strip_top_level_media_references(plan: dict[str, Any]) -> None:
    """Remove alternate media mirrors while a private speech generation exists."""

    for key in list(plan):
        if key == "variants":
            continue
        if _is_private_key(key):
            # Proof snapshots and generation controls are consumed below, then
            # removed wholesale by the private-state projection.  Sanitizing
            # them here would erase the last-good evidence before validation.
            continue
        value = plan[key]
        if _is_media_reference_key(key) or (
            isinstance(value, str) and _looks_like_media_locator(value)
        ):
            plan.pop(key, None)
        else:
            # Containers such as candidate snapshots and omni asset maps can
            # mirror source/output locators without a media-looking top-level
            # key.  Recurse so they cannot bypass the active-state projection.
            plan[key] = _strip_media_references(value)


def _mark_last_good_in_flight(
    restored: list[dict[str, Any]],
    *,
    current: dict[str, dict[str, Any]],
    affected_ids: set[str],
    masked_last_good_variant_ids: set[str] | None,
) -> list[dict[str, Any]]:
    visible_variants: list[dict[str, Any]] = []
    for prior in restored:
        variant_id = str(prior["variant_id"])
        visible = copy.deepcopy(prior)
        if variant_id in affected_ids:
            current_row = current.get(variant_id, {})
            for field in _IN_FLIGHT_PUBLIC_FIELDS:
                if field in current_row:
                    visible[field] = copy.deepcopy(current_row[field])
            visible["render_status"] = "rendering"
            visible["ok"] = False
            if masked_last_good_variant_ids is not None:
                masked_last_good_variant_ids.add(variant_id)
        visible_variants.append(visible)
    return visible_variants


def _mark_media_unavailable(
    variants: list[Any],
    *,
    affected_ids: set[str],
    redact_all: bool,
    media_unavailable_variant_ids: set[str] | None,
) -> list[dict[str, Any]]:
    visible_variants: list[dict[str, Any]] = []
    for item in variants:
        if not isinstance(item, dict):
            continue
        variant_id = _nonblank_token(item.get("variant_id"))
        if variant_id is None:
            continue
        visible = copy.deepcopy(item)
        if redact_all or variant_id in affected_ids:
            if media_unavailable_variant_ids is not None:
                media_unavailable_variant_ids.add(variant_id)
            visible = _strip_media_references(visible)
            visible["variant_id"] = variant_id
            visible["render_status"] = "rendering"
            visible["ok"] = False
        visible_variants.append(visible)
    return visible_variants


def _mask_active_speech_rerender(
    plan: Any,
    *,
    masked_last_good_variant_ids: set[str] | None = None,
    media_unavailable_variant_ids: set[str] | None = None,
) -> Any:
    """Expose a proved rollback snapshot or no media while speech state is active.

    Public reads are a privacy boundary, not a recovery mechanism.  Corrupt,
    missing, or mutually inconsistent generation metadata must therefore fail
    closed: it can make media temporarily unavailable, but can never make a
    generation-owned working artifact public.
    """

    if not isinstance(plan, dict):
        return plan
    # The collector recognizes independent persisted ownership markers rather
    # than trusting the contract discriminator.  That keeps legacy speech-cut
    # controls working while a missing/drifted contract still fails closed.
    # Cleanup receipts alone intentionally do not make the state active.
    state = _active_speech_projection_state(plan)
    if not state.active:
        return plan
    _strip_top_level_media_references(plan)
    if "variants" not in plan:
        return plan
    variants = plan.get("variants")
    if not isinstance(variants, list):
        # The public schema expects a list.  A malformed container cannot be
        # safely traversed for paths or signed URLs, so expose no variants.
        plan["variants"] = []
        return plan
    current = _variant_map(variants)
    if current is None:
        plan["variants"] = _mark_media_unavailable(
            variants,
            affected_ids=set(),
            redact_all=True,
            media_unavailable_variant_ids=media_unavailable_variant_ids,
        )
        return plan

    affected_ids = set(state.target_ids)
    for variant_id, row in current.items():
        if _value_references_generation(row, state.generations):
            affected_ids.add(variant_id)
    if not affected_ids.issubset(current):
        state = _ActiveSpeechProjectionState(
            active=state.active,
            target_ids=state.target_ids,
            generations=state.generations,
            target_uncertain=True,
            generation_uncertain=state.generation_uncertain,
        )

    restored = _safe_previous_variant_vector(plan, current=current, state=state)
    if restored is not None:
        visible_ids = set(current) if state.target_uncertain else affected_ids
        plan["variants"] = _mark_last_good_in_flight(
            restored,
            current=current,
            affected_ids=visible_ids,
            masked_last_good_variant_ids=masked_last_good_variant_ids,
        )
        return plan

    legacy_prior = _safe_legacy_previous_variant(plan, current=current, state=state)
    if legacy_prior is not None:
        variant_id = str(legacy_prior["variant_id"])
        restored = [
            legacy_prior if item_id == variant_id else copy.deepcopy(item)
            for item_id, item in current.items()
        ]
        plan["variants"] = _mark_last_good_in_flight(
            restored,
            current=current,
            affected_ids={variant_id},
            masked_last_good_variant_ids=masked_last_good_variant_ids,
        )
        return plan

    plan["variants"] = _mark_media_unavailable(
        variants,
        affected_ids=affected_ids,
        redact_all=state.target_uncertain or not affected_ids,
        media_unavailable_variant_ids=media_unavailable_variant_ids,
    )
    return plan


def _strip_private_state(value: Any) -> Any:
    if isinstance(value, dict):
        for key in list(value):
            if _is_private_key(key):
                value.pop(key, None)
            else:
                value[key] = _strip_private_state(value[key])
    elif isinstance(value, list):
        for index, item in enumerate(value):
            value[index] = _strip_private_state(item)
    return value


def project_public_assembly_plan_with_metadata(
    assembly_plan: Any,
) -> PublicAssemblyPlanProjection:
    """Return a creator-safe copy plus non-serialized projection metadata.

    The projection is recursive because candidate snapshots can be embedded in
    variants or editor state.  Ordinary user-authored fields are preserved
    byte-for-byte at the JSON-value level.
    """

    masked_last_good_variant_ids: set[str] = set()
    media_unavailable_variant_ids: set[str] = set()
    active_speech_projection = bool(
        isinstance(assembly_plan, dict) and _active_speech_projection_state(assembly_plan).active
    )
    projected = _mask_active_speech_rerender(
        copy.deepcopy(assembly_plan),
        masked_last_good_variant_ids=masked_last_good_variant_ids,
        media_unavailable_variant_ids=media_unavailable_variant_ids,
    )
    return PublicAssemblyPlanProjection(
        value=_strip_private_state(projected),
        masked_last_good_variant_ids=frozenset(masked_last_good_variant_ids),
        media_unavailable_variant_ids=frozenset(media_unavailable_variant_ids),
        active_speech_projection=active_speech_projection,
    )


def project_public_assembly_plan(assembly_plan: Any) -> Any:
    """Return only the side-effect-free creator-visible JSON value."""

    return project_public_assembly_plan_with_metadata(assembly_plan).value


def project_admin_debug_candidates(all_candidates: Any) -> Any:
    """Return the admin-debug projection needed for exact source audition.

    Creator and export projections must never expose stable source identities.
    The authenticated admin debug endpoint is the one narrow exception: its
    local audition tool needs the top-level ordered UUID vector to prove which
    current durable object produced a non-reversible receipt ``source_tag``.
    Indexed metadata envelopes and every nested private control remain stripped.
    """

    projected = project_public_assembly_plan(all_candidates)
    if not isinstance(all_candidates, dict) or not isinstance(projected, dict):
        return projected
    if "clip_source_instance_ids" in all_candidates:
        projected["clip_source_instance_ids"] = copy.deepcopy(
            all_candidates["clip_source_instance_ids"]
        )
    return projected
