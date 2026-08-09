"""Persisted speech-cut review state and timeline remapping.

The detector owns source-timeline ranges.  Everything exposed to the editor is
derived from this module so candidate receipts, revision guards, Director
operations, and render-time lane remapping cannot disagree.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from copy import deepcopy
from typing import Any

from app.pipeline.silence_cut import Removal

SPEECH_CUT_STATE_VERSION = 1
_TIMED_LIST_FIELDS = (
    "text_elements",
    "media_overlays",
    "sound_effects",
    "camera_effects",
    "boundary_effects",
    "motion_scenes",
    "visual_blocks",
    "transcript",
    "overlay_transcript",
)


def _round_time(value: float) -> float:
    return round(float(value), 3)


def candidate_id(
    *, start_s: float, end_s: float, reason: str, source: str, source_fingerprint: str
) -> str:
    payload = (
        f"{source_fingerprint}|{source}|{_round_time(start_s):.3f}|"
        f"{_round_time(end_s):.3f}|{reason}"
    )
    return "cut_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def make_candidate(
    *,
    start_s: float,
    end_s: float,
    reason: str,
    source: str,
    preview: str,
    source_fingerprint: str,
    transcript_hash: str,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id(
            start_s=start_s,
            end_s=end_s,
            reason=reason,
            source=source,
            source_fingerprint=source_fingerprint,
        ),
        "start_s": _round_time(start_s),
        "end_s": _round_time(end_s),
        "reason": str(reason).strip()[:240],
        "source": source,
        "preview": " ".join(str(preview).split())[:160],
        "source_fingerprint": source_fingerprint,
        "transcript_hash": transcript_hash,
        "coordinate_space": "source_v1",
        "status": "pending",
    }


def cut_revision(variant: dict[str, Any]) -> str:
    """Stable optimistic-concurrency token for every cut-affecting state."""
    payload = {
        "disabled": variant.get("speech_cuts_disabled") is True,
        "candidates": [
            {
                "candidate_id": c.get("candidate_id"),
                "start_s": c.get("start_s"),
                "end_s": c.get("end_s"),
                "status": c.get("status"),
            }
            for c in variant.get("speech_cut_candidates") or []
            if isinstance(c, dict)
        ],
        "forced": variant.get("speech_cut_forced_removals") or [],
        "in_flight": variant.get("speech_cut_in_flight"),
        "automatic": (variant.get("silence_cut") or {}).get("removed") or [],
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def public_candidates(variant: dict[str, Any]) -> list[dict[str, Any]]:
    revision = cut_revision(variant)
    return [
        {**c, "revision": revision}
        for c in variant.get("speech_cut_candidates") or []
        if isinstance(c, dict) and c.get("status") == "pending"
    ]


def accept_candidate(
    variant: dict[str, Any], *, candidate_id_value: str, expected_revision: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = cut_revision(variant)
    if expected_revision != current:
        raise ValueError("speech_cut_revision_conflict")
    updated = deepcopy(variant)
    candidate = next(
        (
            c
            for c in updated.get("speech_cut_candidates") or []
            if isinstance(c, dict) and c.get("candidate_id") == candidate_id_value
        ),
        None,
    )
    if candidate is None or candidate.get("status") != "pending":
        raise LookupError("speech_cut_candidate_not_found")
    candidate["status"] = "applying"
    removal = {
        "start_s": _round_time(candidate["start_s"]),
        "end_s": _round_time(candidate["end_s"]),
        "reason": str(candidate.get("source") or "manual_review"),
        "candidate_id": candidate_id_value,
    }
    forced = list(updated.get("speech_cut_forced_removals") or [])
    forced.append(removal)
    updated["speech_cut_in_flight"] = {
        "operation": "apply_speech_cut_candidate",
        "candidate_id": candidate_id_value,
        "desired_forced_removals": forced,
        "desired_disabled": False,
    }
    updated["speech_cut_revision"] = cut_revision(updated)
    receipt = {
        "operation": "apply_speech_cut_candidate",
        "candidate_id": candidate_id_value,
        "removed": removal,
        "time_saved_s": _round_time(removal["end_s"] - removal["start_s"]),
        "revision": updated["speech_cut_revision"],
    }
    return updated, receipt


def restore_original_timing(
    variant: dict[str, Any], *, expected_revision: str
) -> tuple[dict[str, Any], dict[str, Any]]:
    current = cut_revision(variant)
    if expected_revision != current:
        raise ValueError("speech_cut_revision_conflict")
    updated = deepcopy(variant)
    intervals = sorted(
        (float(r.get("start_s", 0.0)), float(r.get("end_s", 0.0)))
        for r in [
            *((variant.get("silence_cut") or {}).get("removed") or []),
            *(variant.get("speech_cut_forced_removals") or []),
        ]
        if isinstance(r, dict) and float(r.get("end_s", 0.0)) > float(r.get("start_s", 0.0))
    )
    restored_s = 0.0
    cursor_start: float | None = None
    cursor_end = 0.0
    for start, end in intervals:
        if cursor_start is None:
            cursor_start, cursor_end = start, end
        elif start <= cursor_end:
            cursor_end = max(cursor_end, end)
        else:
            restored_s += cursor_end - cursor_start
            cursor_start, cursor_end = start, end
    if cursor_start is not None:
        restored_s += cursor_end - cursor_start
    updated["speech_cut_in_flight"] = {
        "operation": "restore_original_timing",
        "desired_forced_removals": [],
        "desired_disabled": True,
    }
    updated["speech_cut_revision"] = cut_revision(updated)
    receipt = {
        "operation": "restore_original_timing",
        "restored_s": _round_time(restored_s),
        "revision": updated["speech_cut_revision"],
    }
    return updated, receipt


def _removed_before(t: float, removals: list[Removal]) -> float:
    return sum(max(0.0, min(r.end_s, t) - r.start_s) for r in removals)


def remap_time(t: float, removals: Iterable[Removal]) -> float | None:
    ordered = sorted(removals, key=lambda r: (r.start_s, r.end_s))
    value = float(t)
    if any(r.start_s <= value < r.end_s for r in ordered):
        return None
    return _round_time(value - _removed_before(value, ordered))


def remap_timed_records(
    records: list[dict[str, Any]] | None, removals: Iterable[Removal]
) -> list[dict[str, Any]]:
    """Remap start/end records; fully removed records are dropped, overlaps clamp."""
    ordered = sorted(removals, key=lambda r: (r.start_s, r.end_s))
    out: list[dict[str, Any]] = []
    for raw in records or []:
        if not isinstance(raw, dict):
            continue
        start_key = "start_s" if "start_s" in raw else "start"
        end_key = "end_s" if "end_s" in raw else "end"
        if start_key not in raw or end_key not in raw:
            if "at_s" in raw:
                mapped_at = remap_time(float(raw["at_s"]), ordered)
                if mapped_at is None:
                    continue
                item = deepcopy(raw)
                item["at_s"] = mapped_at
                out.append(item)
                continue
            out.append(deepcopy(raw))
            continue
        start = float(raw[start_key])
        end = float(raw[end_key])
        kept = [
            (max(start, lo), min(end, hi))
            for lo, hi in _keep_segments(ordered, max(end, 0.0))
            if min(end, hi) > max(start, lo)
        ]
        if not kept:
            continue
        mapped_start = kept[0][0] - _removed_before(kept[0][0], ordered)
        mapped_end = kept[-1][1] - _removed_before(kept[-1][1], ordered)
        item = deepcopy(raw)
        item[start_key] = _round_time(mapped_start)
        item[end_key] = _round_time(mapped_end)
        if isinstance(item.get("words"), list):
            item["words"] = remap_timed_records(item["words"], ordered)
        if isinstance(item.get("source_params"), dict):
            params = dict(item["source_params"])
            schedule = params.get("reveal_schedule_s")
            if isinstance(schedule, list):
                params["reveal_schedule_s"] = [
                    mapped
                    for value in schedule
                    if (mapped := remap_time(float(value), ordered)) is not None
                ]
            item["source_params"] = params
        out.append(item)
    return out


def _keep_segments(removals: list[Removal], duration_s: float) -> list[tuple[float, float]]:
    cursor = 0.0
    keep: list[tuple[float, float]] = []
    for removal in removals:
        if removal.start_s > cursor:
            keep.append((cursor, removal.start_s))
        cursor = max(cursor, removal.end_s)
    if duration_s > cursor:
        keep.append((cursor, duration_s))
    return keep


def remap_variant_timing(variant: dict[str, Any], removals: Iterable[Removal]) -> dict[str, Any]:
    """Remap every persisted final-timeline lane and clear stale AI receipts."""
    ordered = sorted(removals, key=lambda r: (r.start_s, r.end_s))
    updated = deepcopy(variant)
    updated["caption_cues"] = remap_timed_records(updated.get("caption_cues"), ordered)
    for field in _TIMED_LIST_FIELDS:
        if isinstance(updated.get(field), list):
            updated[field] = remap_timed_records(updated[field], ordered)
    # These are tied to the old transcript/timeline and must be regenerated.
    for field in (
        "speech_map",
        "overlay_suggestions",
        "overlay_suggest_hash",
        "director_suggestions",
        "director_revision",
        "smart_compiled_patch",
        "smart_validation_receipts",
    ):
        updated[field] = None
    return updated


def output_to_source_time(
    t: float, removals: Iterable[Removal], *, prefer_post_cut: bool = True
) -> float:
    """Invert a cut timeline coordinate onto the original source timeline."""
    ordered = sorted(removals, key=lambda r: (r.start_s, r.end_s))
    source = float(t)
    removed_before = 0.0
    for removal in ordered:
        cut_start = removal.start_s - removed_before
        if float(t) > cut_start or (prefer_post_cut and float(t) == cut_start):
            duration = removal.end_s - removal.start_s
            source += duration
            removed_before += duration
    return _round_time(source)


def reproject_timed_records(
    records: list[dict[str, Any]] | None,
    *,
    old_removals: Iterable[Removal],
    new_removals: Iterable[Removal],
) -> list[dict[str, Any]]:
    """Project records from an old cut timeline through source into a new one."""
    old = list(old_removals)

    def _to_source(raw_records: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        source_records: list[dict[str, Any]] = []
        for raw in raw_records or []:
            if not isinstance(raw, dict):
                continue
            item = deepcopy(raw)
            start_key = "start_s" if "start_s" in item else "start"
            end_key = "end_s" if "end_s" in item else "end"
            if start_key in item and end_key in item:
                item[start_key] = output_to_source_time(
                    float(item[start_key]), old, prefer_post_cut=True
                )
                item[end_key] = output_to_source_time(
                    float(item[end_key]), old, prefer_post_cut=False
                )
            elif "at_s" in item:
                item["at_s"] = output_to_source_time(float(item["at_s"]), old)
            if isinstance(item.get("words"), list):
                item["words"] = _to_source(item["words"])
            if isinstance(item.get("source_params"), dict):
                params = dict(item["source_params"])
                schedule = params.get("reveal_schedule_s")
                if isinstance(schedule, list):
                    params["reveal_schedule_s"] = [
                        output_to_source_time(float(value), old) for value in schedule
                    ]
                item["source_params"] = params
            source_records.append(item)
        return source_records

    source_records = _to_source(records)
    return remap_timed_records(source_records, list(new_removals))


def reproject_variant_timing(
    variant: dict[str, Any],
    *,
    old_removals: Iterable[Removal],
    new_removals: Iterable[Removal],
) -> dict[str, Any]:
    updated = deepcopy(variant)
    old = list(old_removals)
    new = list(new_removals)
    updated["caption_cues"] = reproject_timed_records(
        updated.get("caption_cues"), old_removals=old, new_removals=new
    )
    for field in _TIMED_LIST_FIELDS:
        if isinstance(updated.get(field), list):
            updated[field] = reproject_timed_records(
                updated[field], old_removals=old, new_removals=new
            )
    for field in (
        "speech_map",
        "overlay_suggestions",
        "overlay_suggest_hash",
        "director_suggestions",
        "director_revision",
    ):
        updated[field] = None
    return updated
