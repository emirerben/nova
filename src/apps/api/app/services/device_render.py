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
from app.services.phone_editor_sources import editor_source_bindings
from app.services.phone_sources import PHONE_SOURCES_FIELD, PhoneSourceBinding

DEVICE_RENDER_FIELD = "_device_render_v1"
CONTRACT_REVISIONS_FIELD = "creator_render_revisions"
# A typed contract refusal, kept on the device record BESIDE its intact state (the
# pinned request, its contract receipts, the published attempt). Recovery reads it.
CONTRACT_DECLINE_FIELD = "contract_decline"

# Human-readable fallback when the reporter (phone client or reaper) sends an
# empty detail string. Keyed by reason_code; "timed_out" is the reaper's own
# stale-timeout report (app/tasks/device_render_reaper.py).
_DEFAULT_FAILURE_DETAIL: dict[str, str] = {
    "export_failed": "The export failed on your device. Open the project to retry.",
    "insufficient_storage": "Not enough storage on your device to finish the export.",
    "thermal": "Your device paused rendering to cool down. Open the project to retry.",
    "unsupported_recipe": "This edit isn't supported by on-device rendering yet.",
    "cancelled_by_user": "The export was cancelled on your device.",
    "timed_out": "No delivery from your device in the last 24 hours. Open the project to retry.",
    "unknown": "Something went wrong rendering on your device. Open the project to retry.",
}

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


def device_status(job: Any, variant_id: str) -> DeviceRenderStatus:
    record = device_record(job, variant_id)
    status = DeviceRenderStatus.model_validate(record["status"])
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


def _verify_contract_pin(
    job: Any, request: DeviceRenderRequest, base_generation: str
) -> tuple[str, list[dict]] | None:
    """Validate a new v2 request against its immutable creator requirements."""
    assembly = job.assembly_plan or {}
    _require_marked_root_authority(job)
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
    return contract.digest, verify_phone_recipe(contract, request.recipe, source_audio=source_audio)


def verify_device_record_contract(job: Any, record: dict, status: DeviceRenderStatus) -> None:
    """Recheck the receipt authority before publishing a device export."""
    _require_marked_root_authority(job)
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
    _verify_contract_pin(job, status.request, generation)


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
    save_device_record(
        job,
        request.identity.variant_id,
        record,
    )
    return True


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
