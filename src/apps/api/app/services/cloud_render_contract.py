"""Fail-closed verification of cloud render outputs against pinned requirements."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ValidationError

from app.pipeline.cloud_render_evidence import (
    TEXT_WINDOW_TOLERANCE_S,
    CloudPictureSegment,
    CloudTextEvidence,
    normalize_text,
)
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CONTRACT_REQUIREMENTS,
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
_ASK_SPEECH_MONTAGE = (
    "Ask for a spoken-excerpt montage on your iPhone, which can keep only that camera audio, "
    "or tell me to drop that requirement."
)

# What each cloud requirement declines with when an adapter does not prove it.
# Preflight refuses these before any ingest/model/render spend; publication
# refuses them again.  An adapter lifts one only by listing it as consumed AND
# emitting the receipt evidence for it (see CLOUD_ADAPTER_DECLARATIONS).
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
# A requirement that can never be proven by this adapter at all.
_CLOUD_NEVER = Decline("capability_unavailable", _ASK_PHONE)


def _cloud(
    adapter: str,
    consumes: set[str],
    *,
    never: frozenset[str] = frozenset(),
    **overrides: Decline,
) -> AdapterDeclaration:
    """Declare a cloud adapter: everything not consumed is declined, typed.

    ``never`` lists requirements the adapter can structurally never prove
    (a still-image post has no length or voice); the rest decline as the
    shared defaults unless ``overrides`` supplies an adapter-specific
    alternative.
    """

    declines: dict[str, Decline] = {}
    for requirement in CONTRACT_REQUIREMENTS:
        if requirement in consumes or requirement == "unresolved":
            continue
        if requirement in overrides:
            declines[requirement] = overrides[requirement]
        elif requirement in never:
            declines[requirement] = _CLOUD_NEVER
        else:
            declines[requirement] = CLOUD_UNEVIDENCED.get(requirement, _CLOUD_NO_RECEIPT)
    declines["unresolved"] = _CLOUD_UNRESOLVED
    return AdapterDeclaration(adapter, frozenset(consumes), declines)


CLOUD_ADAPTER_DECLARATIONS: dict[str, AdapterDeclaration] = {
    declaration.adapter: declaration
    for declaration in (
        # Guided story (``GuidedStoryRenderReceipt``): the receipt carries the
        # measured duration, whether the narration was mixed, the picture order
        # actually rendered, the camera-audio sources actually mixed and every
        # text layer's role/window as burned (pixel-checked).  It mixes every
        # clip's own sound, so it can restrict to a *named* set of sources no
        # better than it can promise one: that stays declined.
        _cloud(
            "cloud_guided_story",
            {"duration_s", "require_voiceover", "original_audio", "exact_texts", "order_required"},
            audio_source_ids=Decline("capability_unavailable", _ASK_SPEECH_MONTAGE),
        ),
        # Classic montage/voiceover/narrated renders (``classic_render_receipt``):
        # measured duration, narration actually mixed, picture order and audible
        # camera-audio sources from the timeline/mix the renderer built.  Their
        # intro text is composed from agent blocks whose exact words are not
        # evidenced separately, so exact text stays declined; talking-head and
        # subtitled renders emit no receipt (see ``check_classic_archetype``).
        _cloud(
            "cloud_classic",
            {"duration_s", "require_voiceover", "original_audio", "order_required"},
            audio_source_ids=Decline("capability_unavailable", _ASK_SPEECH_MONTAGE),
        ),
        # Slide posts are stills: no length, no voice, no camera audio, no order.
        _cloud(
            "cloud_slides",
            set(),
            never=frozenset({"duration_s", "require_voiceover"}),
        ),
    )
}

# Classic archetypes whose renderers emit ``classic_render_receipt`` evidence.
CLASSIC_EVIDENCE_ARCHETYPES = frozenset(
    {"montage", "day_vlog", "single_hero", "voiceover", "narrated"}
)
# The requirements only the receipt can prove for a classic render.
_CLASSIC_RECEIPT_REQUIREMENTS = ("require_voiceover", "original_audio", "order_required")

_MESSAGE_PREFLIGHT = {
    "exact_texts": "This cloud renderer can't verify confirmed on-screen text yet.",
    "audio_source_ids": "This cloud renderer can't verify confirmed camera-audio sources yet.",
    "original_audio": (
        "This cloud renderer can't verify confirmed camera-audio muting or preservation yet."
    ),
    "order_required": "This cloud renderer can't verify the confirmed clip order yet.",
    "duration_s": "This cloud renderer can't produce a verified length for this edit.",
    "require_voiceover": "This cloud renderer can't carry your recorded voice for this edit.",
}


def cloud_adapter_for_variant(variant: Mapping[str, Any]) -> str:
    """The cloud adapter that rendered ``variant`` (classic unless it says otherwise)."""

    archetype = variant.get("resolved_archetype")
    if archetype == "guided_story":
        return "cloud_guided_story"
    if archetype == "slides":
        return "cloud_slides"
    return "cloud_classic"


def cloud_adapter_for_job(assembly: Mapping[str, Any], candidates: Mapping[str, Any]) -> str | None:
    """The adapter the worker will dispatch this job to, or ``None`` if undecidable.

    Mirrors the dispatch in ``_run_generative_job_impl``: a guided snapshot that
    applies to the declared intent wins, then slide posts, then the classic
    renderers.  ``None`` (an inconsistent guided binding the dispatcher itself
    rejects) makes preflight fall back to the conservative no-adapter rule.
    """

    from app.agents._schemas.edit_format import guided_edit_applicable  # noqa: PLC0415
    from app.services.creator_execution_contract import validate_execution_binding  # noqa: PLC0415

    render_intent = candidates.get("declared_edit_format", candidates.get("edit_format"))
    voiceover = candidates.get("voiceover_gcs_path") or None
    snapshot = assembly.get("guided_edit")
    try:
        applicable = guided_edit_applicable(render_intent, has_voiceover=bool(voiceover))
        if validate_execution_binding(snapshot, candidates.get("creator_strategy"), voiceover):
            applicable = True
    except ValueError:
        return None
    if snapshot is not None and applicable:
        return "cloud_guided_story"
    if render_intent == "slides":
        return "cloud_slides"
    return "cloud_classic"


def _decline(
    requirement: str,
    message: str,
    *,
    field_path: str | None = None,
    declaration: AdapterDeclaration | None = None,
    evidence: bool = False,
) -> CloudRenderContractError:
    """The typed refusal for ``requirement``.

    ``evidence`` marks a requirement the adapter does consume but whose receipt
    evidence is absent or contradicts the contract (``evidence_missing``).
    Otherwise the reason is the adapter's declared decline for it.
    """

    if requirement == "unresolved":
        decline = _CLOUD_UNRESOLVED
    elif evidence:
        decline = _CLOUD_NO_RECEIPT
    elif declaration is not None and requirement in declaration.declines:
        decline = declaration.declines[requirement]
    else:
        decline = CLOUD_UNEVIDENCED.get(requirement) or _CLOUD_NO_RECEIPT
    return CloudRenderContractError(
        message,
        decline_reason=decline.reason,
        field_path=field_path or REQUIREMENT_FIELD_PATHS.get(requirement),
        alternative=decline.alternative or None,
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
    assembly: Mapping[str, Any],
    *,
    candidates: Mapping[str, Any] | None = None,
    adapter: str | None = None,
) -> None:
    """Decline requirements the dispatched cloud adapter cannot evidence, before work.

    With ``adapter`` the job's own adapter declaration decides: a requirement it
    consumes passes (publication verifies it against the render's receipt) and
    everything else declines with its declared reason.  Without an adapter the
    rule stays the conservative pre-PR one -- a requirement passes only if every
    cloud adapter proves it, so nothing is lifted on an unknown route.
    """

    contract = _pinned_contract(assembly, candidates)
    if contract is None:
        return
    if contract.unresolved:
        raise _decline("unresolved", contract.unresolved[0])
    declaration = CLOUD_ADAPTER_DECLARATIONS[adapter] if adapter else None

    def declined(requirement: str) -> bool:
        if declaration is not None:
            return requirement in declaration.declines
        # Unknown adapter: only requirements the receipt-less default always refused.
        return requirement in CLOUD_UNEVIDENCED

    checks = (
        ("exact_texts", bool(contract.exact_texts), _text_path(contract)),
        ("audio_source_ids", bool(contract.audio_source_ids), None),
        ("original_audio", contract.original_audio is not None, None),
        ("order_required", contract.order_required, None),
        # Length and voice are verified at publication; preflight only refuses
        # them for an adapter that structurally can never prove them.
        ("duration_s", contract.duration_s is not None and declaration is not None, None),
        ("require_voiceover", contract.require_voiceover and declaration is not None, None),
    )
    for requirement, present, field_path in checks:
        if present and declined(requirement):
            raise _decline(
                requirement,
                _MESSAGE_PREFLIGHT[requirement],
                field_path=field_path,
                declaration=declaration,
            )


def check_classic_archetype(
    assembly: Mapping[str, Any], *, candidates: Mapping[str, Any] | None, archetype: str
) -> None:
    """Decline a classic archetype that emits no receipt for a receipt-only requirement.

    The classic adapter is declared as proving voice, camera audio and order, but
    only the montage/voiceover/narrated renderers emit that receipt.  The
    archetype is known once the footage is analysed; this runs before any variant
    renders so a talking-head or subtitled edit is refused up front instead of
    rendering and then failing publication for lack of evidence.
    """

    contract = _pinned_contract(assembly, candidates)
    if contract is None or archetype in CLASSIC_EVIDENCE_ARCHETYPES:
        return
    present = {
        "require_voiceover": contract.require_voiceover,
        "original_audio": contract.original_audio is not None,
        "order_required": contract.order_required,
    }
    for requirement in _CLASSIC_RECEIPT_REQUIREMENTS:
        if present[requirement]:
            raise CloudRenderContractError(
                f"A {archetype.replace('_', ' ')} edit can't prove this confirmed requirement "
                "in the cloud yet.",
                decline_reason="capability_unavailable",
                field_path=REQUIREMENT_FIELD_PATHS.get(requirement),
                alternative=_ASK_PHONE,
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


def _rows(receipt: Mapping[str, Any], key: str, model: type[BaseModel]) -> list[Any] | None:
    """Receipt rows parsed through their evidence model, or ``None`` if absent/invalid."""

    raw = receipt.get(key)
    if not isinstance(raw, list):
        return None
    try:
        return [model.model_validate(row) for row in raw]
    except ValidationError:
        return None


def _verify_text(
    contract: CreatorRenderContract, receipt: Mapping[str, Any], duration_s: object
) -> None:
    """Each required text must be a burned layer of the right role, in a plausible window."""

    texts = _rows(receipt, "text_evidence", CloudTextEvidence)
    timeline = _rows(receipt, "picture_timeline", CloudPictureSegment)
    tolerance = TEXT_WINDOW_TOLERANCE_S
    for requirement in contract.exact_texts:
        path = text_field_path(requirement)

        def refuse(message: str, path: str = path) -> CloudRenderContractError:
            return _decline("exact_texts", message, field_path=path, evidence=True)

        if texts is None:
            raise refuse("This edit couldn't verify confirmed on-screen text.")
        wanted = normalize_text(requirement.text)
        matches = [row for row in texts if normalize_text(row.text) == wanted]
        if requirement.role != "any":
            matches = [row for row in matches if row.role == requirement.role]
        if not matches:
            raise refuse("This edit is missing confirmed on-screen text.")
        if requirement.duration_s is not None:
            matches = [
                row
                for row in matches
                if row.end_s - row.start_s + tolerance >= requirement.duration_s
            ]
            if not matches:
                raise _decline(
                    "exact_texts",
                    "This edit couldn't keep confirmed text on screen long enough.",
                    field_path=(
                        "opening_title_duration_s"
                        if requirement.role == "opening"
                        else text_field_path(requirement)
                    ),
                    evidence=True,
                )
        if requirement.role == "opening":
            matches = [row for row in matches if row.start_s <= tolerance]
        elif requirement.role == "closing":
            if not isinstance(duration_s, int | float) or isinstance(duration_s, bool):
                raise refuse("This edit couldn't measure where its closing text sits.")
            matches = [row for row in matches if row.end_s >= float(duration_s) - tolerance]
        elif requirement.role == "clip":
            if timeline is None:
                raise refuse("This edit couldn't verify which shot carries confirmed text.")
            targets = timeline
            if requirement.shot_index is not None:
                targets = timeline[requirement.shot_index : requirement.shot_index + 1]
            elif requirement.media_id:
                targets = [seg for seg in timeline if seg.media_id == requirement.media_id]
            if not targets or any(
                not any(
                    row.start_s >= seg.start_s - tolerance and row.end_s <= seg.end_s + tolerance
                    for row in matches
                )
                for seg in targets
            ):
                matches = []
        if not matches:
            raise refuse("This edit put confirmed text in the wrong place.")


def verify_cloud_variant(
    assembly: Mapping[str, Any],
    variant: Mapping[str, Any],
    *,
    candidates: Mapping[str, Any] | None = None,
    adapter: str | None = None,
) -> CreatorRenderContract | None:
    """Verify a newly playable variant using renderer-produced receipt evidence.

    The variant's adapter declaration decides what is provable: a requirement the
    adapter declines is refused with its declared reason, one it consumes is
    checked against the evidence its renderer emitted, and a missing or
    contradicting receipt field is ``evidence_missing``.  Desired metadata (intro
    text, timelines, or request fields) is intentionally never used as proof.
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
    declaration = CLOUD_ADAPTER_DECLARATIONS[adapter or cloud_adapter_for_variant(variant)]
    # A requirement the adapter never proves is refused with its declared reason
    # before any receipt is read: a loose list of strings must not pass for
    # opening/closing/per-clip text (an end-card at the opening would be a false
    # positive), and a copied ``source_audio_preserved`` is not camera-audio proof.
    if contract.exact_texts and "exact_texts" in declaration.declines:
        raise _decline(
            "exact_texts",
            "This edit couldn't verify confirmed on-screen text.",
            field_path=_text_path(contract),
            declaration=declaration,
        )
    if contract.audio_source_ids and "audio_source_ids" in declaration.declines:
        raise _decline(
            "audio_source_ids",
            "This edit couldn't verify the confirmed camera-audio sources.",
            declaration=declaration,
        )
    if contract.original_audio is not None and "original_audio" in declaration.declines:
        raise _decline(
            "original_audio",
            "This edit couldn't verify confirmed camera-audio muting or preservation.",
            declaration=declaration,
        )
    if contract.order_required and "order_required" in declaration.declines:
        raise _decline(
            "order_required",
            "This edit couldn't verify the confirmed clip order.",
            declaration=declaration,
        )
    if contract.require_voiceover and "require_voiceover" in declaration.declines:
        raise _decline(
            "require_voiceover",
            "This edit needs verified recorded voice evidence.",
            declaration=declaration,
        )
    # Renderers report their measured output duration in `duration_s`; receipts
    # additionally carry `actual_duration_s`.  Neither desired timeline nor the
    # requested target is consulted here.
    receipt = variant.get("render_receipt")
    actual_duration = (
        receipt.get("actual_duration_s")
        if isinstance(receipt, Mapping) and receipt.get("verified") is True
        else variant.get("duration_s")
    )
    if contract.duration_s is not None:
        if "duration_s" in declaration.declines:
            raise _decline(
                "duration_s",
                "This edit couldn't keep the confirmed length.",
                declaration=declaration,
            )
        if not _numbers_match(actual_duration, contract.duration_s):
            raise _decline("duration_s", "This edit couldn't keep the confirmed length.")
    if not any(
        (
            contract.require_voiceover,
            contract.original_audio is not None,
            bool(contract.exact_texts),
            contract.order_required,
        )
    ):
        return contract
    receipt = _actual_receipt(variant)
    if contract.require_voiceover and receipt.get("narration_applied") is not True:
        raise _decline(
            "require_voiceover",
            "This edit needs verified recorded voice evidence.",
            evidence=True,
        )
    if contract.original_audio is not None:
        audible = _strings(receipt, "source_audio_ids")
        wanted = contract.original_audio
        if (
            audible is None
            or (wanted == "forbid" and audible)
            or (wanted == "require" and not audible)
        ):
            raise _decline(
                "original_audio",
                (
                    "This edit couldn't verify confirmed camera-audio muting or preservation."
                    if audible is None
                    else "This edit's camera audio does not match what you confirmed."
                ),
                evidence=True,
            )
    if contract.exact_texts:
        _verify_text(contract, receipt, actual_duration)
    if contract.order_required:
        actual_order = _strings(receipt, "actual_clip_order")
        if actual_order is None or actual_order != contract.order_ids:
            raise _decline(
                "order_required",
                "This edit couldn't verify the confirmed clip order.",
                evidence=True,
            )
    return contract
