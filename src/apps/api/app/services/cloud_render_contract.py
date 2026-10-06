"""Fail-closed verification of cloud render outputs against pinned requirements."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    REQUIREMENT_VERSION_FIELD,
    CreatorRenderContract,
    CreatorRenderContractError,
    read_render_contract,
)


class CloudRenderContractError(ValueError):
    """A playable cloud artifact lacks proof for a confirmed requirement."""


def _pinned_contract(
    assembly: Mapping[str, Any], candidates: Mapping[str, Any] | None
) -> CreatorRenderContract | None:
    if candidates and candidates.get(REQUIREMENT_VERSION_FIELD) == 1:
        if not isinstance(assembly.get(CONTRACT_FIELD), Mapping):
            raise CloudRenderContractError("This edit's confirmed requirements are missing.")
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
        raise CloudRenderContractError(contract.unresolved[0])
    if contract.exact_texts:
        raise CloudRenderContractError(
            "This cloud renderer can't verify confirmed on-screen text yet."
        )
    if contract.audio_source_ids:
        raise CloudRenderContractError(
            "This cloud renderer can't verify confirmed camera-audio sources yet."
        )
    if contract.original_audio is not None:
        raise CloudRenderContractError(
            "This cloud renderer can't verify confirmed camera-audio muting or preservation yet."
        )
    if contract.order_required:
        raise CloudRenderContractError(
            "This cloud renderer can't verify the confirmed clip order yet."
        )


def _actual_receipt(variant: Mapping[str, Any]) -> Mapping[str, Any]:
    receipt = variant.get("render_receipt")
    if not isinstance(receipt, Mapping) or receipt.get("verified") is not True:
        raise CloudRenderContractError("This edit has no verified render evidence.")
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
        raise CloudRenderContractError(contract.unresolved[0])
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
        raise CloudRenderContractError("This edit couldn't verify confirmed on-screen text.")
    if contract.original_audio is not None:
        raise CloudRenderContractError(
            "This edit couldn't verify confirmed camera-audio muting or preservation."
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
        raise CloudRenderContractError("This edit couldn't keep the confirmed length.")
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
        raise CloudRenderContractError("This edit needs verified recorded voice evidence.")
    if contract.audio_source_ids:
        actual = _strings(receipt, "source_audio_ids")
        if actual is None or set(actual) != set(contract.audio_source_ids):
            raise CloudRenderContractError(
                "This edit couldn't verify the confirmed camera-audio sources."
            )
    if contract.order_required:
        actual_order = _strings(receipt, "actual_clip_order")
        if actual_order is None or actual_order != contract.order_ids:
            raise CloudRenderContractError("This edit couldn't verify the confirmed clip order.")
    return contract
