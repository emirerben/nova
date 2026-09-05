"""Pure rollout selection and content-safe mixed-gap diagnostic receipts."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from typing import Any, Literal

from app.services.speech_cleanup_identity import (
    SpeechCleanupAssignment,
    speech_cleanup_source_tag,
)

DETECTOR_VERSION = "mixed-gap-v1"
_MAX_ASR_SPANS = 128
_MAX_SILENCE_SPANS = 128
_MAX_LEXICAL_SPANS = 32
_MAX_ISLANDS = 32
_MAX_ATOMIC_DISPOSITIONS = 64
_MAX_PLAN_SPANS = 100
_RECEIPT_TARGET_BYTES = 16 * 1024


@dataclass(frozen=True)
class MixedGapSelection:
    configured_mode: Literal["off", "shadow", "apply"]
    effective_mode: Literal["off", "shadow", "apply"]
    rollout_percent: int
    rollout_bucket: int | None
    source_tag: str | None
    assignment: SpeechCleanupAssignment


def select_mixed_gap_mode(
    *,
    analysis_policy: str,
    configured_mode: Literal["off", "shadow", "apply"],
    rollout_percent: int,
    assignment: SpeechCleanupAssignment,
) -> MixedGapSelection:
    """Resolve deterministic treatment without persisting its raw fingerprint."""

    if not 0 <= rollout_percent <= 100:
        raise ValueError("rollout percent must be between 0 and 100")
    if analysis_policy != "required_v1" or configured_mode == "off":
        return MixedGapSelection(
            configured_mode=configured_mode,
            effective_mode="off",
            rollout_percent=rollout_percent,
            rollout_bucket=None,
            source_tag=None,
            assignment=assignment,
        )
    fingerprint = assignment.rollout_fingerprint
    if assignment.status != "assigned" or not fingerprint:
        return MixedGapSelection(
            configured_mode=configured_mode,
            effective_mode="shadow",
            rollout_percent=rollout_percent,
            rollout_bucket=None,
            source_tag=None,
            assignment=assignment,
        )
    bucket = (
        int.from_bytes(
            hashlib.sha256(f"mixed-gap-v1:{fingerprint}".encode()).digest()[:4],
            "big",
        )
        % 100
    )
    effective: Literal["shadow", "apply"] = (
        "apply" if configured_mode == "apply" and bucket < rollout_percent else "shadow"
    )
    return MixedGapSelection(
        configured_mode=configured_mode,
        effective_mode=effective,
        rollout_percent=rollout_percent,
        rollout_bucket=bucket,
        source_tag=speech_cleanup_source_tag(fingerprint),
        assignment=assignment,
    )


def _ms(value: float) -> int:
    if not math.isfinite(float(value)):
        raise ValueError("non-finite timing")
    return max(0, int(round(float(value) * 1000)))


def _bounded_spans(values: list[tuple[float, float]], limit: int) -> tuple[list[list[int]], int]:
    shown = [[_ms(lo), _ms(hi)] for lo, hi in values[:limit]]
    return shown, max(0, len(values) - len(shown))


def _plan_payload(plan: Any | None) -> dict[str, Any] | None:
    if plan is None:
        return None
    removals = list(getattr(plan, "removed", []) or [])
    spans, omitted = _bounded_spans(
        [(float(item.start_s), float(item.end_s)) for item in removals],
        _MAX_PLAN_SPANS,
    )
    diagnostics = getattr(plan, "diagnostics", None)
    return {
        "removed_count": len(removals),
        "removed_ms": _ms(float(getattr(plan, "time_saved_s", 0.0) or 0.0)),
        "removed_spans_ms": spans,
        "removed_spans_omitted": omitted,
        "clamped": bool(getattr(plan, "clamped", False)),
        "bailout_reason": getattr(plan, "bailout_reason", None),
        "mixed_gap_full": int(getattr(diagnostics, "mixed_gap_full_total", 0) or 0),
        "mixed_gap_partial": int(getattr(diagnostics, "mixed_gap_partial_total", 0) or 0),
        "mixed_gap_dropped": int(getattr(diagnostics, "mixed_gap_dropped_total", 0) or 0),
    }


def _receipt_size(value: dict[str, Any]) -> int:
    return len(json.dumps(value, ensure_ascii=True).encode())


def _fit_receipt_to_cap(receipt: dict[str, Any]) -> dict[str, Any]:
    """Deterministically trim only diagnostic tails to stay below the DB cap."""

    scan = receipt["mixed_gap_scan"]
    inputs = receipt["inputs"]

    # Rejected tails are the lowest-value evidence; retain every earlier item
    # and all eligible records while possible.
    while _receipt_size(receipt) > _RECEIPT_TARGET_BYTES:
        records = scan["records"]
        rejected_index = next(
            (
                index
                for index in range(len(records) - 1, -1, -1)
                if records[index]["detection"] == "rejected"
            ),
            None,
        )
        if rejected_index is None:
            break
        records.pop(rejected_index)
        scan["records_omitted"] += 1

    input_lists = (
        ("asr_word_spans_ms", "asr_word_spans_omitted"),
        ("silence_spans_ms", "silence_spans_omitted"),
    )
    while _receipt_size(receipt) > _RECEIPT_TARGET_BYTES and any(
        inputs[key] for key, _omitted in input_lists
    ):
        key, omitted_key = max(
            input_lists,
            key=lambda names: (len(inputs[names[0]]), names[0] == "asr_word_spans_ms"),
        )
        if not inputs[key]:
            key, omitted_key = next(names for names in input_lists if inputs[names[0]])
        inputs[key].pop()
        inputs[omitted_key] += 1

    # Eligible detection records are more valuable than raw input bands, but
    # they too are bounded tails rather than a reason to drop the whole event.
    while _receipt_size(receipt) > _RECEIPT_TARGET_BYTES and scan["records"]:
        scan["records"].pop()
        scan["records_omitted"] += 1

    allocator = receipt["allocator"]
    while _receipt_size(receipt) > _RECEIPT_TARGET_BYTES and allocator["atomic_dispositions"]:
        allocator["atomic_dispositions"].pop()
        allocator["atomic_dispositions_omitted"] += 1

    while _receipt_size(receipt) > _RECEIPT_TARGET_BYTES and inputs["lexical_candidate_spans_ms"]:
        inputs["lexical_candidate_spans_ms"].pop()
        inputs["lexical_candidates_omitted"] += 1

    # MAX_REMOVALS normally keeps the plan bands comfortably inside the cap.
    # This final guard is intentionally last because the UI needs exact output
    # geometry whenever it can be retained.
    for plan_key in ("candidate_plan", "baseline_plan"):
        plan = receipt.get(plan_key)
        while (
            _receipt_size(receipt) > _RECEIPT_TARGET_BYTES
            and isinstance(plan, dict)
            and plan["removed_spans_ms"]
        ):
            plan["removed_spans_ms"].pop()
            plan["removed_spans_omitted"] += 1
    return receipt


def build_mixed_gap_receipt(
    *,
    selection: MixedGapSelection,
    analysis_attempt_id: str | None,
    analysis_view: Literal["full_clip", "talking_head_spine_capped"],
    analysis_policy: str,
    candidate_status: str,
    silence_detection_status: str,
    duration_s: float,
    words: list[Any],
    silence_spans: list[tuple[float, float]],
    baseline_plan: Any,
    candidate_plan: Any | None,
    selected_plan: Literal["baseline", "candidate"],
) -> dict[str, Any]:
    """Serialize bounded timing/provenance evidence without speech content."""

    from app.pipeline.silence_cut import (  # noqa: PLC0415
        ACOUSTIC_GAP_MAX_S,
        ACOUSTIC_GAP_MIN_S,
        MIN_CUT_S,
    )

    word_spans, word_omitted = _bounded_spans(
        [(float(word.start_s), float(word.end_s)) for word in words],
        _MAX_ASR_SPANS,
    )
    silence_values, silence_omitted = _bounded_spans(silence_spans, _MAX_SILENCE_SPANS)
    diagnostics = getattr(candidate_plan, "diagnostics", None)
    lexical = list(getattr(diagnostics, "lexical_candidates", ()) or ())
    lexical_spans, lexical_omitted = _bounded_spans(
        [(float(item.start_s), float(item.end_s)) for item in lexical],
        _MAX_LEXICAL_SPANS,
    )
    decisions = list(getattr(diagnostics, "acoustic_decisions", ()) or ())
    dispositions = list(getattr(diagnostics, "atomic_dispositions", ()) or ())
    decision_dispositions = list(getattr(diagnostics, "mixed_gap_decision_dispositions", ()) or ())

    def disposition_for(decision: Any) -> str:
        for island_start, island_end, disposition in decision_dispositions:
            if (
                abs(float(island_start) - float(decision.island_start_s)) <= 1e-7
                and abs(float(island_end) - float(decision.island_end_s)) <= 1e-7
            ):
                return str(disposition)
        for item in dispositions:
            if item.atom_kind != "filler_acoustic":
                continue
            if (
                abs(float(item.atom_start_s) - float(decision.island_start_s)) <= 1e-7
                and abs(float(item.atom_end_s) - float(decision.island_end_s)) <= 1e-7
            ):
                return str(item.disposition)
        return "not_candidate"

    records = [
        {
            "window_start_ms": _ms(item.window_start_s),
            "window_end_ms": _ms(item.window_end_s),
            "island_start_ms": _ms(item.island_start_s),
            "island_end_ms": _ms(item.island_end_s),
            "left_silence_ms": _ms(item.left_silence_s),
            "right_silence_ms": _ms(item.right_silence_s),
            "detection": item.detection,
            "reason": item.reason,
            "plan_disposition": disposition_for(item),
        }
        for item in decisions[:_MAX_ISLANDS]
    ]
    atomic_records = [
        [
            _ms(item.atom_start_s),
            _ms(item.atom_end_s),
            _ms(item.group_start_s),
            _ms(item.group_end_s),
            item.atom_kind,
            item.priority,
            item.disposition,
        ]
        for item in dispositions[:_MAX_ATOMIC_DISPOSITIONS]
    ]
    decision_total = int(getattr(diagnostics, "acoustic_decisions_total", len(decisions)) or 0)
    disposition_total = int(
        getattr(diagnostics, "atomic_dispositions_total", len(dispositions)) or 0
    )
    receipt = {
        "schema_version": 1,
        "detector_version": DETECTOR_VERSION,
        "analysis_attempt_id": analysis_attempt_id,
        "analysis_view": analysis_view,
        "source_slot": selection.assignment.source_slot,
        "assignment_status": selection.assignment.status,
        "source_tag": selection.source_tag,
        "analysis_policy": analysis_policy,
        "configured_mode": selection.configured_mode,
        "effective_mode": selection.effective_mode,
        "candidate_status": candidate_status,
        "rollout_percent": selection.rollout_percent,
        "rollout_bucket": selection.rollout_bucket,
        "duration_ms": _ms(duration_s),
        "thresholds_ms": {
            "silence_min": 100,
            "island_min": _ms(ACOUSTIC_GAP_MIN_S),
            "island_max": _ms(ACOUSTIC_GAP_MAX_S),
            "flank_silence_min": 100,
            "min_cut": _ms(MIN_CUT_S),
        },
        "inputs": {
            "silence_detection_status": silence_detection_status,
            "asr_word_count": len(words),
            "asr_word_spans_ms": word_spans,
            "asr_word_spans_omitted": word_omitted,
            "silence_spans_total": len(silence_spans),
            "silence_spans_ms": silence_values,
            "silence_spans_omitted": silence_omitted,
            "lexical_candidate_spans_ms": lexical_spans,
            "lexical_candidates_omitted": lexical_omitted
            + int(getattr(diagnostics, "lexical_candidates_omitted", 0) or 0),
        },
        "mixed_gap_scan": {
            "word_windows_total": len(words) + 1,
            "islands_total": decision_total,
            "eligible_total": int(getattr(diagnostics, "acoustic_eligible_total", 0) or 0),
            "records": records,
            "records_omitted": max(0, decision_total - len(records)),
        },
        "allocator": {
            "atomic_disposition_fields": [
                "atom_start_ms",
                "atom_end_ms",
                "group_start_ms",
                "group_end_ms",
                "atom_kind",
                "priority",
                "disposition",
            ],
            "atomic_dispositions_total": disposition_total,
            "atomic_dispositions": atomic_records,
            "atomic_dispositions_omitted": max(0, disposition_total - len(atomic_records)),
        },
        "baseline_plan": _plan_payload(baseline_plan),
        "candidate_plan": _plan_payload(candidate_plan),
        "selected_plan": selected_plan,
    }
    return _fit_receipt_to_cap(receipt)


def build_minimal_mixed_gap_receipt(
    *,
    selection: MixedGapSelection,
    analysis_attempt_id: str | None,
    analysis_view: Literal["full_clip", "talking_head_spine_capped"],
    analysis_policy: str,
    candidate_status: str,
    silence_detection_status: str,
    duration_s: float | None = None,
) -> dict[str, Any]:
    """Fixed-shape fallback when media gates or detailed serialization fail."""

    return {
        "schema_version": 1,
        "detector_version": DETECTOR_VERSION,
        "analysis_attempt_id": analysis_attempt_id,
        "analysis_view": analysis_view,
        "source_slot": selection.assignment.source_slot,
        "assignment_status": selection.assignment.status,
        "source_tag": selection.source_tag,
        "analysis_policy": analysis_policy,
        "configured_mode": selection.configured_mode,
        "effective_mode": selection.effective_mode,
        "candidate_status": candidate_status,
        "rollout_percent": selection.rollout_percent,
        "rollout_bucket": selection.rollout_bucket,
        "duration_ms": _ms(duration_s) if duration_s is not None else None,
        "inputs": {"silence_detection_status": silence_detection_status},
        "selected_plan": "baseline",
    }
