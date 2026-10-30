"""Immutable phone recipe receipts. Call mutations while holding the Job row lock."""

from __future__ import annotations

import copy
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

from app.kria.device_render import (
    DeviceFailureReasonCode,
    DeviceRenderRequest,
    DeviceRenderStatus,
    make_device_request,
)
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import OriginalRenderAsset, VoiceoverRenderAsset
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    CreatorRenderContractError,
    decline_payload,
    read_render_contract,
    verify_phone_recipe,
)
from app.services.export_duration_policy import approved_export_maximum
from app.services.phone_editor_sources import editor_source_bindings
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding

DEVICE_RENDER_FIELD = "_device_render_v1"
CONTRACT_REVISIONS_FIELD = "creator_render_revisions"
# A typed contract refusal, kept on the device record BESIDE its intact state (the
# pinned request, its contract receipts, the published attempt). Recovery reads it.
CONTRACT_DECLINE_FIELD = "contract_decline"


def approved_runtime_generation(job: Any, execution_generation: str | None) -> str | None:
    """Resolve an initial whole-job intent to its stored original generation.

    Whole-job approval precedes generation creation, so its execution target
    is null. The original creator generation stays pinned on the Job; a
    variant's generation can later identify publication or a manual edit.
    """
    if execution_generation is not None:
        return execution_generation
    generation = (job.assembly_plan or {}).get("creator_generation_id")
    return generation if isinstance(generation, str) and generation else None


# Human-readable fallback when the reporter (phone client, reaper or the server's
# own checks) sends an empty detail string. Keyed by reason_code; each line says
# what is true and what happens next, never a generic "something went wrong".
_DEFAULT_FAILURE_DETAIL: dict[str, str] = {
    "export_failed": "The video couldn't finish on your iPhone. Your edit is saved; try again.",
    "insufficient_storage": (
        "Your iPhone is low on storage. Free up some space and Kria will finish the video."
    ),
    "thermal": "Your iPhone paused rendering to cool down. Kria will continue when it's cooler.",
    "unsupported_recipe": "This edit uses something this iPhone can't render yet.",
    "cancelled_by_user": "You stopped this render. Your edit is saved; resume it any time.",
    "timed_out": (
        "No delivery from your iPhone in the last 24 hours. Open the project to finish it."
    ),
    "renderer_outdated": "This edit needs a newer version of Kria. Update the app to finish it.",
    "device_unavailable": (
        "This iPhone can't render the video right now. Your edit is saved; try again soon."
    ),
    "original_missing": (
        "An original video for this edit isn't on this iPhone anymore. "
        "Find the original file and Kria will finish the video."
    ),
    "output_check_failed": (
        "Kria's quality check didn't pass for this video. Your approved edit is saved; try again."
    ),
    "verification_unavailable": (
        "Kria couldn't check this video. Your approved edit is saved; try again."
    ),
    "contract_changed": (
        "This edit's approved details changed after it was prepared. "
        "Review the updated edit in chat."
    ),
    "unknown": "The video couldn't finish on your iPhone. Your edit is saved; try again.",
}

# A user Stop pauses the record; it never moves the phase or the project label.
PAUSED_FIELD = "paused_by_user"


class DeviceRecipeSuperseded(CreatorRenderContractError):
    """A newer approved attempt owns this variant (KRI-722 fence).

    Not a refusal of the creator's edit: the client should follow the newer attempt,
    so callers answer ``superseded`` instead of recording a contract decline.
    """


# Phases from which a device render may transition to needs_attention — a
# published record is done, and a record already needs_attention has no
# forward transition here (retry re-pins a brand new identity instead).
_FAILABLE_PHASES = ("awaiting_device", "syncing")


def device_record(job: Any, variant_id: str) -> dict:
    records = (job.assembly_plan or {}).get(DEVICE_RENDER_FIELD) or {}
    record = records.get(variant_id)
    if not isinstance(record, dict):
        raise KeyError("device recipe unavailable")
    return copy.deepcopy(record)


def published_source_generation(job: Any, variant: dict) -> str | None:
    """The saved generation the variant's current phone-published video was rendered from.

    Publishing a device export replaces ``render_generation_id`` with the upload
    attempt id, while a Save pinned its draft to the generation the phone was
    asked to render. The published status keeps that pre-publish
    ``base_generation`` (``_complete_device_export``), so the two name the same
    saved document. None unless the variant's CURRENT generation is that
    published attempt, so a later Save or cloud render never inherits it.
    """
    record = ((job.assembly_plan or {}).get(DEVICE_RENDER_FIELD) or {}).get(
        str(variant.get("variant_id"))
    )
    if not isinstance(record, dict):
        return None
    status = record.get("status")
    current = str(variant.get("render_generation_id") or "")
    if (
        not isinstance(status, dict)
        or status.get("phase") != "published"
        or not current
        or str(record.get("published_attempt") or "") != current
    ):
        return None
    return str(status.get("base_generation") or "") or None


_DELIVERY_PHASES = frozenset({"awaiting_device", "syncing", "published", "needs_attention"})


def variant_delivery_phase(job: Any, variant_id: str | None) -> str | None:
    """The device delivery phase of the variant's CURRENT generation, else None.

    Pure read for status projections (KRI-823). The device record is reported only
    when its ``base_generation`` equals the variant's ``render_generation_id`` (the
    same fence ``routes/device_render._record`` applies), so a late failure from an
    older generation can never outrank a newer accepted export. Malformed records
    and cloud jobs (no record) yield None so a list projection can never fail.
    """
    if not variant_id:
        return None
    try:
        assembly = job.assembly_plan or {}
        record = (assembly.get(DEVICE_RENDER_FIELD) or {}).get(variant_id)
        if not isinstance(record, dict):
            return None
        variant = next(
            (
                v
                for v in assembly.get("variants") or []
                if isinstance(v, dict) and v.get("variant_id") == variant_id
            ),
            None,
        )
        generation = record.get("base_generation")
        if variant is None or not generation:
            return None
        if str(variant.get("render_generation_id") or "") != str(generation):
            return None
        phase = (record.get("status") or {}).get("phase")
    except (AttributeError, KeyError, TypeError, ValueError):
        return None
    return phase if phase in _DELIVERY_PHASES else None


def device_status(job: Any, variant_id: str) -> DeviceRenderStatus:
    record = device_record(job, variant_id)
    status = DeviceRenderStatus.model_validate(record["status"])
    # The record owns this binding. A stored response field cannot override it.
    status = status.model_copy(update={"base_generation": record.get("base_generation")})
    if status.phase != "published":
        return status.model_copy(update={"published_generation": None})
    published_attempt = record.get("published_attempt")
    return status.model_copy(
        update={"published_generation": str(published_attempt) if published_attempt else None}
    )


def save_device_record(job: Any, variant_id: str, record: dict) -> None:
    assembly = copy.deepcopy(job.assembly_plan or {})
    records = assembly.setdefault(DEVICE_RENDER_FIELD, {})
    records[variant_id] = copy.deepcopy(record)
    job.assembly_plan = assembly


def record_contract_decline(
    job: Any, variant_id: str, exc: CreatorRenderContractError, *, stage: str
) -> dict | None:
    """Record a refused contract check beside the variant's last accepted state.

    A refusal (an edit that fails ``verify_phone_recipe``, a publication that no
    longer proves its approved authority, a retry that cannot be re-pinned) NEVER
    replaces the last accepted artifact: the pinned request, its receipts and the
    published attempt stay as they were, and so do the variant's video, poster and
    URL. Only this typed note is added, so recovery can say what was refused and
    why while the last good output stays live. An untyped refusal is recorded as
    ``evidence_missing``: the output no longer proves its approved authority.
    """
    try:
        record = device_record(job, variant_id)
    except KeyError:
        return None
    payload = decline_payload(exc) or {"decline_reason": "evidence_missing"}
    decline = {
        **payload,
        "stage": stage,
        "message": str(exc)[:500],
        "recorded_at": datetime.now(UTC).isoformat(),
    }
    record[CONTRACT_DECLINE_FIELD] = decline
    save_device_record(job, variant_id, record)
    return decline


def has_accepted_artifact(job: Any, variant_id: str | None) -> bool:
    """True when this variant (any variant when None) carries a published artifact."""
    return any(
        isinstance(row, dict)
        and (variant_id is None or row.get("variant_id") == variant_id)
        and bool(row.get("video_path") or row.get("output_url"))
        for row in (job.assembly_plan or {}).get("variants") or []
    )


CONTRACT_REFUSED_FAILURE = "creator_render_contract_unverified"


def fail_first_render_on_refusal(job: Any, variant_id: str, decline: dict) -> None:
    """A refused FIRST render has no last good artifact to keep: fail it visibly.

    Without this the variant and job stay ``awaiting_device`` while the record is
    ``needs_attention``, the reaper never rescans it, and the creator waits for a
    phone that can no longer publish. The typed decline rides beside the failure.
    """
    apply_device_failure_variant_update(
        job,
        variant_id,
        reason_code=CONTRACT_REFUSED_FAILURE,
        detail=str(decline.get("message") or "")[:1000],
    )
    typed = {
        key: decline[key]
        for key in ("decline_reason", "field_path", "alternative")
        if isinstance(decline.get(key), str) and decline[key]
    }
    assembly = dict(job.assembly_plan or {})
    assembly["variants"] = [
        {**row, **typed} if row.get("variant_id") == variant_id else row
        for row in assembly.get("variants", [])
    ]
    job.assembly_plan = assembly


def contract_decline(job: Any, variant_id: str) -> dict | None:
    """The typed refusal recorded on this variant's device record, if any."""
    try:
        decline = device_record(job, variant_id).get(CONTRACT_DECLINE_FIELD)
    except KeyError:
        return None
    return decline if isinstance(decline, dict) else None


def device_contract_blocks_retry(record: dict) -> bool:
    """An unsupported recipe needs a change; failed measured output can be retried.

    This does not approve dispatch: KRI-674 still locks/revalidates the saved
    draft, source generation, immutable recipe, capabilities and fresh consent.
    """
    decline = record.get(CONTRACT_DECLINE_FIELD)
    if not decline:
        return False
    policy = record.get("actual_export_policy")
    return not (
        isinstance(policy, dict)
        and policy.get("version") == 1
        and isinstance(decline, dict)
        and decline.get("stage") == "actual_export"
        and decline.get("field_path") == "export_verification"
        and decline.get("decline_reason") == "evidence_missing"
    )


def _brief_digest_for_generation(assembly: dict, generation: str) -> str | None:
    """Return the contract-format digest of the authority pinned for this revision."""
    raw = (assembly.get("creator_brief_bindings") or {}).get(generation)
    if raw is None:
        raw = assembly.get("creator_brief_binding")
    if not isinstance(raw, dict):
        return None
    brief = raw.get("brief")
    if brief is None:
        return None
    return hashlib.sha256(
        json.dumps(brief, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def _recipe_digest(recipe: Any) -> str:
    data = recipe.model_dump(mode="json")
    # Capabilities are a set: iteration order can change after persistence or
    # on a different worker. All timeline/track lists remain ordered evidence.
    data["required_capabilities"] = sorted(recipe.required_capabilities)
    payload = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _contract_for_variant(assembly: dict, variant_id: str):
    """Select immutable root authority or that variant's editor-approved revision."""
    revisions = assembly.get(CONTRACT_REVISIONS_FIELD)
    if revisions is not None:
        if not isinstance(revisions, dict):
            raise CreatorRenderContractError("I couldn't read this edit's confirmed requirements.")
        if variant_id in revisions:
            # Presence is authority: a null/tampered entry must fail closed,
            # never silently downgrade to the initial root contract.
            raw = revisions[variant_id]
            if not isinstance(raw, dict):
                raise CreatorRenderContractError(
                    "I couldn't read this edit's confirmed requirements."
                )
            return read_render_contract({CONTRACT_FIELD: raw})
    return read_render_contract(assembly)


def _require_marked_root_authority(job: Any) -> None:
    if (getattr(job, "all_candidates", None) or {}).get(REQUIREMENT_VERSION_FIELD) != 1:
        return
    if read_render_contract(job.assembly_plan or {}) is None:
        raise CreatorRenderContractError(
            "This edit's confirmed requirements are unavailable; please re-approve it."
        )


def _approved_source_bindings(assembly: dict, variant_id: str) -> dict[str, PhoneSourceBinding]:
    variant = next(
        (
            row
            for row in assembly.get("variants", [])
            if isinstance(row, dict) and row.get("variant_id") == variant_id
        ),
        {},
    )
    try:
        bindings = tuple(
            PhoneSourceBinding.model_validate(row)
            for row in (assembly.get(PHONE_SOURCES_FIELD) or [])
        ) + editor_source_bindings(variant)
    except (TypeError, ValueError) as exc:
        raise CreatorRenderContractError(
            "I couldn't verify this edit's approved source files."
        ) from exc
    result = {binding.media_id: binding for binding in bindings}
    if len(result) != len(bindings):
        raise CreatorRenderContractError("This edit has conflicting approved source files.")
    return result


def approved_export_sources(
    job: Any, request: DeviceRenderRequest
) -> dict[str, PhoneSourceBinding]:
    """Only the immutable source receipts referenced by this exact request."""
    bindings = _approved_source_bindings(job.assembly_plan or {}, request.identity.variant_id)
    manifest = getattr(request.recipe, "asset_manifest", None)
    ids = (
        {asset.media_id for asset in manifest.assets if isinstance(asset, OriginalRenderAsset)}
        if manifest
        else set()
    )
    if not ids <= bindings.keys():
        raise CreatorRenderContractError("This export's approved source evidence is unavailable.")
    return {media_id: bindings[media_id] for media_id in sorted(ids)}


def export_source_digest(bindings: dict[str, PhoneSourceBinding]) -> str:
    payload = {key: value.model_dump(mode="json") for key, value in sorted(bindings.items())}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def checked_export_report(record: dict, status: DeviceRenderStatus, attempt_id: str) -> bool:
    """No ready-state fast path may revive missing, stale or rejected evidence."""
    if "actual_export_policy" not in record:
        return True  # historical/unpromoted adapter; not a verified-output claim
    from app.services.actual_export_verification import (  # noqa: PLC0415
        HISTORICAL_VERIFIER_VERSIONS,
        VERIFIER_ORDER,
    )

    policy = record.get("actual_export_policy")
    attempt = (record.get("attempts") or {}).get(attempt_id) or {}
    report = attempt.get("actual_export_verification") or {}
    return bool(
        isinstance(policy, dict)
        and policy.get("version") == 1
        and policy.get("verifier_version", "phone-simple-cut-v1") in HISTORICAL_VERIFIER_VERSIONS
        and report.get("status") == "checked"
        # A pinned policy accepts its own verifier or any later one: a later
        # verifier keeps every earlier wrong-output control (KRI-810, KRI-1042
        # replay those controls), so a request pinned before a verifier repair
        # can still publish exact bytes re-checked by the repaired verifier. An
        # older report never satisfies a newer pin.
        and report.get("verifier_version")
        in (
            set(VERIFIER_ORDER[VERIFIER_ORDER.index(policy["verifier_version"]) :])
            if policy.get("verifier_version")
            else HISTORICAL_VERIFIER_VERSIONS
        )
        and report.get("identity") == status.request.identity.model_dump(mode="json")
        and report.get("sha256") == attempt.get("sha256")
        and report.get("file_size_bytes") == attempt.get("size")
        and report.get("artifact_path") == attempt.get("path")
        and report.get("storage_generation")
        and policy.get("source_receipts_digest") == report.get("source_receipts_digest")
        and report.get("checks")
        and all(row.get("status") == "passed" for row in report["checks"])
    )


def _verify_recipe_source_identity(
    assembly: dict, variant_id: str, recipe: EditRecipeV2
) -> dict[str, bool]:
    """Make originals in the portable recipe prove the server-pinned receipts."""
    bindings = _approved_source_bindings(assembly, variant_id)
    for asset in recipe.asset_manifest.assets:
        if not isinstance(asset, OriginalRenderAsset):
            continue
        binding = bindings.get(asset.media_id)
        if binding is None or (
            asset.fingerprint.sha256 != binding.original.sha256
            or asset.fingerprint.byte_count != binding.original.byte_count
        ):
            raise CreatorRenderContractError(
                "This edit no longer matches its approved source files."
            )
    return {media_id: binding.original.has_audio for media_id, binding in bindings.items()}


def _verify_processed_audio_identity(
    job: Any, request: DeviceRenderRequest, base_generation: str
) -> None:
    """Refuse a forged/stale derivative before it becomes a device receipt."""
    from app.kria.render_assets import ProcessedAudioRenderAsset  # noqa: PLC0415
    from app.pipeline.phone_processed_audio import (  # noqa: PLC0415
        processed_audio_asset,
        processed_audio_receipt_for,
    )

    manifest = request.recipe.asset_manifest
    if not any(isinstance(asset, ProcessedAudioRenderAsset) for asset in manifest.assets):
        return
    bindings = _approved_source_bindings(job.assembly_plan or {}, request.identity.variant_id)
    for asset in manifest.assets:
        if not isinstance(asset, ProcessedAudioRenderAsset):
            continue
        receipt = processed_audio_receipt_for(
            job.assembly_plan or {},
            variant_id=request.identity.variant_id,
            source_media_id=asset.source_media_id,
        )
        source = bindings.get(asset.source_media_id)
        if (
            receipt is None
            or asset != processed_audio_asset(receipt, asset_id=asset.id)
            or asset.job_id != str(job.id)
            or asset.variant_id != request.identity.variant_id
            or asset.render_generation != base_generation
            or source is None
            or source.generation != asset.source_generation
        ):
            raise CreatorRenderContractError(
                "This edit's processed audio no longer matches its approved source files."
            )


def _verify_contract_pin(
    job: Any,
    request: DeviceRenderRequest,
    base_generation: str,
    *,
    reserve_standard_ending: bool = True,
) -> tuple[str, list[dict]] | None:
    """Validate a new v2 request against its immutable creator requirements."""
    assembly = job.assembly_plan or {}
    _require_marked_root_authority(job)
    # Asset authorization is independent of creator-contract rollout. A legacy
    # job must not be able to pin a client-forged processed-audio identity and
    # merely discover the refusal later while downloading it.
    if isinstance(request.recipe, EditRecipeV2):
        _verify_processed_audio_identity(job, request, base_generation)
    contract = _contract_for_variant(assembly, request.identity.variant_id)
    if contract is None:
        return None
    if contract.generation_id != base_generation:
        raise CreatorRenderContractError("This edit belongs to a different approved revision.")
    if not isinstance(request.recipe, EditRecipeV2):
        raise CreatorRenderContractError("This approved edit requires the current phone renderer.")
    if contract.brief_digest is not None:
        actual = _brief_digest_for_generation(assembly, base_generation)
        if actual != contract.brief_digest:
            raise CreatorRenderContractError("This edit no longer matches its approved brief.")
    source_audio = _verify_recipe_source_identity(
        assembly, request.identity.variant_id, request.recipe
    )
    from app.services.length_policy_state import read_length_policy  # noqa: PLC0415

    return contract.digest, verify_phone_recipe(
        contract,
        request.recipe,
        source_audio=source_audio,
        length_policy=read_length_policy(assembly, contract),
        max_export_duration_s=approved_export_maximum(assembly, contract),
        brand_tail_s=None if reserve_standard_ending else 0.0,
    )


def verify_device_record_contract(job: Any, record: dict, status: DeviceRenderStatus) -> None:
    """Recheck the receipt authority before publishing a device export."""
    _require_marked_root_authority(job)
    from app.kria.prepared_execution import variant_fence  # noqa: PLC0415

    # KRI-722: a stamped Job publishes only the recipe pinned under its current
    # prepared snapshot and attempt. A late completion of an older attempt (or a
    # recipe pinned before the stamp) is refused; the last-good output is kept.
    fence = variant_fence(job.assembly_plan, status.request.identity.variant_id)
    if (fence is not None or record.get("prepared_execution") is not None) and record.get(
        "prepared_execution"
    ) != fence:
        raise DeviceRecipeSuperseded(
            "A newer approved attempt replaced this render; please use the latest version."
        )
    contract = _contract_for_variant(job.assembly_plan or {}, status.request.identity.variant_id)
    expected = record.get("contract_digest")
    if expected is None:
        if contract is not None:
            raise CreatorRenderContractError(
                "This approved edit must be pinned again before publishing."
            )
        return
    if contract is None or contract.digest != expected:
        raise CreatorRenderContractError(
            "This edit's confirmed requirements changed; please try again."
        )
    generation = record.get("requirement_generation", record.get("base_generation"))
    if contract.generation_id != generation:
        raise CreatorRenderContractError("This edit belongs to a different approved revision.")
    if record.get("recipe_digest") != _recipe_digest(status.request.recipe):
        raise CreatorRenderContractError("This approved recipe changed; please try again.")
    # A stored request may belong to an older client that exported with no ending.
    # Reserve the standard ending only at new pin time. Publication verifies the
    # actual declared tail and complete file independently in `_verify_export`.
    _verify_contract_pin(job, status.request, generation, reserve_standard_ending=False)


def pin_device_request(
    job: Any,
    request: DeviceRenderRequest,
    *,
    base_generation: str,
    narration_binding: dict | None = None,
) -> bool:
    """Pin one approved recipe once. A redelivery cannot rewrite that revision's decisions."""
    if request.identity.job_id != job.id:
        raise ValueError("recipe job identity mismatch")
    contract_receipt = _verify_contract_pin(job, request, base_generation)
    try:
        previous = device_status(job, request.identity.variant_id).request
    except KeyError:
        previous = None
    previous_record: dict | None = None
    if previous is not None:
        if previous_record is None:
            previous_record = device_record(job, request.identity.variant_id)
        if previous_record.get("contract_digest") and contract_receipt is None:
            raise CreatorRenderContractError(
                "This edit's confirmed requirements are unavailable; please re-approve it."
            )
        if previous == request:
            return False
        if request.identity.recipe_revision <= previous.identity.recipe_revision:
            raise ValueError("recipe revision already pinned")
        previous_record = device_record(job, request.identity.variant_id)
    if narration_binding is None and previous_record is not None:
        prior_binding = previous_record.get("narration_binding")
        manifest = getattr(request.recipe, "asset_manifest", None)
        voiceover = next(
            (
                asset
                for asset in (manifest.assets if manifest else ())
                if isinstance(asset, VoiceoverRenderAsset)
            ),
            None,
        )
        if (
            isinstance(prior_binding, dict)
            and voiceover is not None
            and (
                prior_binding.get("asset_id") == voiceover.id
                and prior_binding.get("plan_item_id") == voiceover.plan_item_id
                and prior_binding.get("sha256") == voiceover.fingerprint.sha256
                and prior_binding.get("byte_count") == voiceover.fingerprint.byte_count
                and (prior_binding.get("narration") or {}).get("generation") == voiceover.generation
            )
        ):
            narration_binding = prior_binding
    record = {
        "status": DeviceRenderStatus(phase="awaiting_device", request=request).model_dump(
            mode="json"
        ),
        "base_generation": base_generation,
        "attempts": {},
        # ISO pin timestamp — the reaper's staleness fallback when a record
        # has never been polled (`last_polled_at` absent). See
        # app/tasks/device_render_reaper.py.
        "pinned_at": datetime.now(UTC).isoformat(),
    }
    if contract_receipt is not None:
        contract_digest, receipts = contract_receipt
        record.update(
            requirement_generation=base_generation,
            contract_digest=contract_digest,
            recipe_digest=_recipe_digest(request.recipe),
            contract_receipts=copy.deepcopy(receipts),
            contract_snapshot=_contract_for_variant(
                job.assembly_plan or {}, request.identity.variant_id
            ).model_dump(mode="json"),
        )
    if narration_binding is not None:
        record["narration_binding"] = copy.deepcopy(narration_binding)
    from app.kria.prepared_execution import variant_fence  # noqa: PLC0415

    fence = variant_fence(job.assembly_plan, request.identity.variant_id)
    if fence is not None:
        # KRI-722: the attempt this recipe belongs to (absent on legacy Jobs).
        record["prepared_execution"] = fence
    from app.config import settings  # noqa: PLC0415

    # Record-only envelope: strict recipe/status schemas stay readable by old workers.
    # Existing requests are not silently enrolled during a rolling deployment.
    prepared_initial = bool(
        isinstance((job.assembly_plan or {}).get("prepared_collection_story"), dict)
        and base_generation == (job.assembly_plan or {}).get("creator_generation_id")
    )
    if prepared_initial and contract_receipt is None:
        raise CreatorRenderContractError("Prepared stories require verified render obligations")
    if prepared_initial or (
        settings.phone_actual_export_verification_enabled and contract_receipt is not None
    ):
        from app.services.actual_export_verification import (  # noqa: PLC0415
            POLICY_FIELD,
            VERIFIER_VERSION,
            unsupported_reasons,
        )

        reasons = unsupported_reasons(request.recipe)
        if prepared_initial and reasons:
            raise CreatorRenderContractError(
                "This prepared story exceeds actual-output verification coverage"
            )
        if not reasons:
            record[POLICY_FIELD] = {
                "version": 1,
                "verifier_version": VERIFIER_VERSION,
                "source_receipts_digest": export_source_digest(
                    approved_export_sources(job, request)
                ),
            }
    save_device_record(
        job,
        request.identity.variant_id,
        record,
    )
    return True


def device_export_maximum(job: Any, variant_id: str) -> float | None:
    """Read the same immutable authority at delivery and again at publication."""
    assembly = job.assembly_plan or {}
    return approved_export_maximum(assembly, _contract_for_variant(assembly, variant_id))


def mark_device_failed(
    job: Any,
    variant_id: str,
    *,
    reason_code: DeviceFailureReasonCode,
    detail: str,
) -> DeviceRenderStatus:
    """Transition a device render to needs_attention from a reported/detected failure.

    Callable from `awaiting_device` or `syncing` only — a published record is
    already terminal-success and a record already `needs_attention` should be
    reported idempotently by the caller rather than re-failed here.
    """
    record = device_record(job, variant_id)
    status = DeviceRenderStatus.model_validate(record["status"])
    if status.phase not in _FAILABLE_PHASES:
        raise ValueError(f"device render cannot fail from phase '{status.phase}'")
    resolved_detail = detail or _DEFAULT_FAILURE_DETAIL.get(reason_code, "")
    status.phase = "needs_attention"
    status.reason = resolved_detail
    status.reason_code = reason_code
    record["status"] = status.model_dump(mode="json")
    failed_at = datetime.now(UTC).isoformat()
    record["failed_at"] = failed_at
    record["failure"] = {
        "reason_code": reason_code,
        "detail": resolved_detail,
        "failed_at": failed_at,
    }
    save_device_record(job, variant_id, record)
    return status


def mark_device_paused(job: Any, variant_id: str, now: datetime) -> DeviceRenderStatus:
    """Record a user Stop as a resumable pause.

    The phase stays where it was (``awaiting_device``/``syncing``) and the variant and
    Job are untouched: nothing failed, the same phone will resume the same pinned
    recipe. Only a live record can pause; published / needs_attention raise.
    """
    record = device_record(job, variant_id)
    status = DeviceRenderStatus.model_validate(record["status"])
    if status.phase not in _FAILABLE_PHASES:
        raise ValueError(f"device render cannot pause from phase '{status.phase}'")
    record[PAUSED_FIELD] = {
        "paused_at": now.isoformat(),
        "identity": status.request.identity.model_dump(mode="json"),
    }
    save_device_record(job, variant_id, record)
    return status


def device_paused(record: dict) -> bool:
    """True while a user Stop holds a live (not yet delivered) record."""
    phase = (record.get("status") or {}).get("phase")
    return bool(record.get(PAUSED_FIELD)) and phase in _FAILABLE_PHASES


def clear_device_pause(job: Any, variant_id: str) -> bool:
    """Resume: drop the pause marker. Returns whether there was one."""
    record = device_record(job, variant_id)
    if record.pop(PAUSED_FIELD, None) is None:
        return False
    save_device_record(job, variant_id, record)
    return True


def touch_device_poll(job: Any, variant_id: str, now: datetime) -> None:
    """Record the latest client poll time — the reaper's staleness signal.

    Callers are expected to throttle their own write cadence (see the
    60s-per-record throttle in `app/routes/device_render.py::get_device_render`);
    this function itself always writes when called.
    """
    record = device_record(job, variant_id)
    record["last_polled_at"] = now.isoformat()
    save_device_record(job, variant_id, record)


def retry_device_render(job: Any, variant_id: str) -> DeviceRenderStatus:
    """Re-pin a needs_attention device recipe under a fresh identity.

    Preserves the exact recipe and `base_generation` from the failed record
    verbatim — this is a pure re-delivery vehicle, never a decision point.
    `pin_device_request` always starts the new record with empty `attempts`,
    so a retry naturally forgets the prior (failed) upload attempts.

    Caller must have already fenced ownership/identity and is responsible for
    deciding whether a currently `awaiting_device`/`syncing` record should be
    force-failed first (the admin unstick path) or rejected (the user path).
    """
    record = device_record(job, variant_id)
    status = DeviceRenderStatus.model_validate(record["status"])
    if status.phase != "needs_attention":
        raise ValueError("device render is not awaiting a retry")
    new_request = make_device_request(
        job_id=job.id,
        variant_id=variant_id,
        revision=status.request.identity.recipe_revision + 1,
        recipe=status.request.recipe,
    )
    pin_device_request(
        job,
        new_request,
        base_generation=record["base_generation"],
        narration_binding=record.get("narration_binding"),
    )
    # A kill-switch change can stop enrollment, never waive an accepted check.
    if "actual_export_policy" in record:
        retried = device_record(job, variant_id)
        retried["actual_export_policy"] = copy.deepcopy(record["actual_export_policy"])
        save_device_record(job, variant_id, retried)
    return device_status(job, variant_id)


def apply_device_failure_variant_update(
    job: Any, variant_id: str, *, reason_code: str, detail: str
) -> None:
    """Mirror a failed device record onto the public `variants[]` + job failure surface."""
    assembly = dict(job.assembly_plan or {})
    assembly["variants"] = [
        {**v, "ok": False, "render_status": "needs_attention"}
        if v.get("variant_id") == variant_id
        else v
        for v in assembly.get("variants", [])
    ]
    job.assembly_plan = assembly
    job.failure_reason = reason_code
    job.error_detail = detail[:1000] or None


def apply_retry_variant_reset(job: Any, variant_id: str) -> None:
    """Flip the assembly_plan variant + job failure fields back to awaiting_device.

    Call AFTER `retry_device_render` has re-pinned the new identity so the
    per-variant status and the job-level failure surface reset together.
    """
    assembly = dict(job.assembly_plan or {})
    assembly["variants"] = [
        {**v, "ok": False, "render_status": "awaiting_device"}
        if v.get("variant_id") == variant_id
        else v
        for v in assembly.get("variants", [])
    ]
    job.assembly_plan = assembly
    job.failure_reason = None
    job.error_detail = None
