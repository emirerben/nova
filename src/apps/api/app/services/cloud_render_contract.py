"""Fail-closed verification of cloud render outputs against pinned requirements."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_FIELD_PATHS,
    REQUIREMENT_VERSION_FIELD,
    AdapterDeclaration,
    CreatorRenderContract,
    CreatorRenderContractError,
    Decline,
    DeclineReason,
    read_render_contract,
    text_field_path,
)


class CloudRenderContractError(ValueError):
    """A playable cloud artifact lacks proof for a confirmed requirement.

    ``decline_reason`` / ``field_path`` / ``alternative`` are the typed decline
    that survives into the job failure payload and the creator recovery.
    """

    def __init__(
        self,
        message: str,
        *,
        decline_reason: DeclineReason | None = None,
        field_path: str | None = None,
        alternative: str | None = None,
    ) -> None:
        super().__init__(message)
        self.decline_reason = decline_reason
        self.field_path = field_path
        self.alternative = alternative


_ASK_PHONE = (
    "Ask for it to be rendered on your iPhone, where I can check it, "
    "or tell me to drop that requirement."
)
_RETRY = "I can render it again, or you can relax the requirement."

# Requirements no cloud compiler can evidence today.  Preflight refuses them
# before any ingest/model/render spend; publication refuses them again.
CLOUD_UNEVIDENCED: dict[str, Decline] = {
    "exact_texts": Decline("capability_unavailable", _ASK_PHONE),
    "audio_source_ids": Decline("capability_unavailable", _ASK_PHONE),
    "original_audio": Decline("capability_unavailable", _ASK_PHONE),
    "order_required": Decline("capability_unavailable", _ASK_PHONE),
}
_CLOUD_UNRESOLVED = Decline("needs_choice", "Tell me which option you want and I'll continue.")
# A requirement a cloud adapter can in principle prove, but whose renderer did
# not emit the receipt/measurement on this output.
_CLOUD_NO_RECEIPT = Decline("evidence_missing", _RETRY)


def _cloud(adapter: str, consumes: set[str]) -> AdapterDeclaration:
    declines = {req: d for req, d in CLOUD_UNEVIDENCED.items()}
    declines["unresolved"] = _CLOUD_UNRESOLVED
    for requirement in ("duration_s", "require_voiceover"):
        if requirement not in consumes:
            declines[requirement] = _CLOUD_NO_RECEIPT
    return AdapterDeclaration(adapter, frozenset(consumes), declines)


CLOUD_ADAPTER_DECLARATIONS: dict[str, AdapterDeclaration] = {
    declaration.adapter: declaration
    for declaration in (
        # Guided story receipts carry actual_duration_s and narration_applied.
        _cloud("cloud_guided_story", {"duration_s", "require_voiceover"}),
        # Classic renders report a measured duration_s but no narration receipt.
        _cloud("cloud_classic", {"duration_s"}),
        # Slide posts are stills: no measured duration, no voice.
        _cloud("cloud_slides", set()),
    )
}


# Receipt keys that exist for these two: a publication mismatch is missing
# evidence (the renderer reported something else), not a missing capability.
_RECEIPT_BACKED = frozenset({"audio_source_ids", "order_required"})


def _decline(
    requirement: str,
    message: str,
    *,
    field_path: str | None = None,
    publication: bool = False,
) -> CloudRenderContractError:
    if requirement == "unresolved":
        declaration = _CLOUD_UNRESOLVED
    elif publication and requirement in _RECEIPT_BACKED:
        declaration = _CLOUD_NO_RECEIPT
    else:
        declaration = CLOUD_UNEVIDENCED.get(requirement) or _CLOUD_NO_RECEIPT
    return CloudRenderContractError(
        message,
        decline_reason=declaration.reason,
        field_path=field_path or REQUIREMENT_FIELD_PATHS.get(requirement),
        alternative=declaration.alternative or None,
    )


def _text_path(contract: CreatorRenderContract) -> str | None:
    return text_field_path(contract.exact_texts[0]) if contract.exact_texts else None


def _pinned_contract(
    assembly: Mapping[str, Any], candidates: Mapping[str, Any] | None
) -> CreatorRenderContract | None:
    if candidates and candidates.get(REQUIREMENT_VERSION_FIELD) == 1:
        if not isinstance(assembly.get(CONTRACT_FIELD), Mapping):
            raise CloudRenderContractError(
                "This edit's confirmed requirements are missing.",
                decline_reason="evidence_missing",
                field_path=CONTRACT_FIELD,
            )
    try:
        return read_render_contract(assembly)
    except CreatorRenderContractError as exc:
        raise CloudRenderContractError(str(exc)) from exc


def preflight_cloud_contract(
    assembly: Mapping[str, Any], *, candidates: Mapping[str, Any] | None = None
) -> None:
    """Decline requirements current cloud compilers cannot evidence before work."""

    contract = _pinned_contract(assembly, candidates)
    if contract is None:
        return
    if contract.unresolved:
        raise _decline("unresolved", contract.unresolved[0])
    if contract.exact_texts:
        raise _decline(
            "exact_texts",
            "This cloud renderer can't verify confirmed on-screen text yet.",
            field_path=_text_path(contract),
        )
    if contract.audio_source_ids:
        raise _decline(
            "audio_source_ids",
            "This cloud renderer can't verify confirmed camera-audio sources yet.",
        )
    if contract.original_audio is not None:
        raise _decline(
            "original_audio",
            "This cloud renderer can't verify confirmed camera-audio muting or preservation yet.",
        )
    if contract.order_required:
        raise _decline(
            "order_required", "This cloud renderer can't verify the confirmed clip order yet."
        )


def _actual_receipt(variant: Mapping[str, Any]) -> Mapping[str, Any]:
    receipt = variant.get("render_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("verified") is not True:
        raise CloudRenderContractError(
            "This edit has no verified render evidence.",
            decline_reason="evidence_missing",
            alternative=_RETRY,
        )
    return receipt


def _numbers_match(actual: object, required: float) -> bool:
    if isinstance(actual, bool) or not isinstance(actual, int | float) or actual <= 0:
        return False
    return abs(float(actual) - required) / required <= 0.1


def _strings(receipt: Mapping[str, Any], key: str) -> tuple[str, ...] | None:
    raw = receipt.get(key)
    if not isinstance(raw, list) or any(not isinstance(value, str) for value in raw):
        return None
    return tuple(raw)


def verify_cloud_variant(
    assembly: Mapping[str, Any],
    variant: Mapping[str, Any],
    *,
    candidates: Mapping[str, Any] | None = None,
) -> CreatorRenderContract | None:
    """Verify a newly playable variant using renderer-produced receipt evidence.

    Requirements with no standardized cloud evidence decline publication.  Desired
    metadata (intro text, timelines, or request fields) is intentionally never
    used as proof.
    """

    contract = _pinned_contract(assembly, candidates)
    if contract is None:
        return None
    if contract.unresolved:
        raise _decline("unresolved", contract.unresolved[0])
    if not any(
        (
            contract.duration_s is not None,
            contract.require_voiceover,
            contract.original_audio is not None,
            bool(contract.audio_source_ids),
            bool(contract.exact_texts),
            contract.order_required,
        )
    ):
        return contract
    # Exact text evidence needs role/media/timing attribution. The current
    # cloud receipt has no such structured proof, so never accept a loose list
    # of strings (an end-card at the opening would be a false positive).
    if contract.exact_texts:
        raise _decline(
            "exact_texts",
            "This edit couldn't verify confirmed on-screen text.",
            field_path=_text_path(contract),
        )
    if contract.original_audio is not None:
        raise _decline(
            "original_audio",
            "This edit couldn't verify confirmed camera-audio muting or preservation.",
        )
    # Current classic render results report their measured output duration in
    # `duration_s`; guided receipts additionally carry `actual_duration_s`.
    # Neither desired timeline nor requested target is consulted here.
    receipt = variant.get("render_receipt")
    actual_duration = (
        receipt.get("actual_duration_s")
        if isinstance(receipt, Mapping) and receipt.get("verified") is True
        else variant.get("duration_s")
    )
    if contract.duration_s is not None and not _numbers_match(actual_duration, contract.duration_s):
        raise _decline("duration_s", "This edit couldn't keep the confirmed length.")
    if not any(
        (
            contract.require_voiceover,
            bool(contract.audio_source_ids),
            contract.order_required,
        )
    ):
        return contract
    receipt = _actual_receipt(variant)
    if contract.require_voiceover and receipt.get("narration_applied") is not True:
        raise _decline("require_voiceover", "This edit needs verified recorded voice evidence.")
    if contract.audio_source_ids:
        actual = _strings(receipt, "source_audio_ids")
        if actual is None or set(actual) != set(contract.audio_source_ids):
            raise _decline(
                "audio_source_ids",
                "This edit couldn't verify the confirmed camera-audio sources.",
                publication=True,
            )
    if contract.order_required:
        actual_order = _strings(receipt, "actual_clip_order")
        if actual_order is None or actual_order != contract.order_ids:
            raise _decline(
                "order_required",
                "This edit couldn't verify the confirmed clip order.",
                publication=True,
            )
    return contract
