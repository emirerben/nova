"""Collapse byte-identical iPhone uploads to one clip before a montage renders (KRI-544).

A creator who attaches the same video twice (IMG_4400 and IMG_4400_copy) under a
``video_reuse_policy`` of ``"once"`` ("each source plays once", the server default
unless the creator asked to repeat footage) must not see that footage twice. The iPhone
hashes every original before it uploads the analysis proxy, so two uploads whose
``upload_contract.proxy.original.sha256`` match are the same bytes, not two takes that
merely look alike. Web uploads carry no such hash and are never collapsed.

Pure functions only: dispatch decides (``duplicate_aliases``), records the decision on
the job (``DROPPED_DUPLICATES_FIELD``), and every reader that names clips by id (the
render contract, the unified montage planner) remaps the approved strategy through
``collapse_strategy`` so a caption, label or order intent the resolver put on the
dropped copy lands on the kept one instead of pointing at a clip that is not there.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from app.kria.media_sources import ClipCapture, MediaUploadContract

# Job.all_candidates key written at dispatch: [{"media_id", "kept_media_id"}], in
# attachment order of the dropped copies. Absent when nothing was collapsed.
DROPPED_DUPLICATES_FIELD = "dropped_duplicate_clips"

# The strategy fields ``collapse_strategy`` rewrites. A duplicate named anywhere else in
# the strategy (a camera-audio source, a song take, a hero) is left alone: both copies
# stay, exactly as before this change.
_REMAPPED_FIELDS = frozenset({"resolved_clip_intents", "selected_media_ids"})


def original_sha256(assignment: Mapping[str, Any]) -> str | None:
    """The device original's sha256 from an iPhone upload receipt, else None.

    Only an analysis-proxy receipt of a video original carries one; a cloud (web) upload,
    a malformed receipt or an audio original yields None and is never collapsed.
    """

    raw = assignment.get("upload_contract")
    if not isinstance(raw, Mapping) or not raw:
        return None
    try:
        contract = MediaUploadContract.model_validate(dict(raw))
    except ValidationError:
        return None
    if contract.purpose != "analysis_proxy" or contract.proxy is None:
        return None
    original = contract.proxy.original
    return original.sha256 if original.kind == "video" else None


def _capture_time(assignment: Mapping[str, Any]) -> datetime | None:
    raw = assignment.get("capture")
    if isinstance(raw, Mapping) and raw:
        try:
            capture = ClipCapture.model_validate(dict(raw))
        except ValidationError:
            capture = None
        if capture is not None and capture.capture_time is not None:
            return capture.capture_time
    contract_raw = assignment.get("upload_contract")
    if isinstance(contract_raw, Mapping) and contract_raw:
        try:
            contract = MediaUploadContract.model_validate(dict(contract_raw))
        except ValidationError:
            return None
        original = contract.proxy.original if contract.proxy is not None else None
        if original is not None and original.capture is not None:
            return original.capture.capture_time
    return None


def _strings(value: object) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list | tuple):
        for item in value:
            yield from _strings(item)


def duplicate_aliases(
    strategy: Mapping[str, Any] | None,
    assignments: Sequence[Mapping[str, Any]],
    *,
    blocked_refs: Iterable[str] = (),
) -> dict[str, str]:
    """``{dropped media_id: kept media_id}`` for the byte-identical copies in ``assignments``.

    ``assignments`` are the clip assignments that would render, in attachment order. Each
    group of rows sharing an original sha256 keeps ONE copy: the earliest filmed (capture
    time), ties and missing capture times broken by attachment order. Nothing collapses
    unless the approved strategy says ``video_reuse_policy == "once"``; nor when it carries
    positional ``shot_labels`` (dropping a clip would shift every later label onto the
    wrong clip) or a round-robin ``montage_cadence``. A group is left whole when any copy
    is named outside the remapped intent/selection fields (a camera-audio source, a song
    take) or by a ``blocked_refs`` entry (a clip-scoped brief requirement, the speech
    cleanup source): equal to the media id or contained in it, so a short id blocks too.
    """

    if not isinstance(strategy, Mapping) or strategy.get("video_reuse_policy") != "once":
        return {}
    if strategy.get("shot_labels") or strategy.get("montage_cadence"):
        return {}
    pinned = {
        value
        for key, field in strategy.items()
        if key not in _REMAPPED_FIELDS
        for value in _strings(field)
    }
    blocked = [str(ref) for ref in blocked_refs if str(ref or "").strip()]
    # Rank key per copy: (no capture time, capture timestamp, attachment index, media id).
    groups: dict[str, list[tuple[bool, float, int, str]]] = {}
    seen: set[str] = set()
    for index, row in enumerate(assignments):
        if not isinstance(row, Mapping):
            continue
        media_id = str(row.get("media_id") or "")
        if not media_id or media_id in seen:
            continue
        seen.add(media_id)
        digest = original_sha256(row)
        if digest is None:
            continue
        taken = _capture_time(row)
        stamp = taken.timestamp() if taken is not None else 0.0
        groups.setdefault(digest, []).append((taken is None, stamp, index, media_id))
    aliases: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        ids = [member[3] for member in members]
        if any(media_id in pinned for media_id in ids) or any(
            ref == media_id or ref in media_id for ref in blocked for media_id in ids
        ):
            continue
        ranked = sorted(members)
        kept = ranked[0][3]
        for member in ranked[1:]:
            aliases[member[3]] = kept
    return aliases


def _remap_ids(values: object, aliases: Mapping[str, str]) -> object:
    if not isinstance(values, list):
        return values
    out: list[object] = []
    for value in values:
        mapped = aliases.get(value, value) if isinstance(value, str) else value
        if mapped not in out:
            out.append(mapped)
    return out


def collapse_strategy(strategy: Mapping[str, Any], aliases: Mapping[str, str]) -> dict[str, Any]:
    """A copy of ``strategy`` whose clip intents and selection name the kept copies.

    Every ``resolved_clip_intents`` assignment on a dropped copy moves to its kept copy
    (an intent that already names the kept copy keeps its first assignment only), and
    ``selected_media_ids`` loses the dropped ids. Idempotent; without aliases it is a
    plain deep copy.
    """

    out = copy.deepcopy(dict(strategy))
    if not aliases:
        return out
    if "selected_media_ids" in out:
        out["selected_media_ids"] = _remap_ids(out["selected_media_ids"], aliases)
    intents = out.get("resolved_clip_intents")
    if isinstance(intents, list):
        for intent in intents:
            if not isinstance(intent, dict) or not isinstance(intent.get("assignments"), list):
                continue
            kept_rows: list[object] = []
            named: set[str] = set()
            for assignment in intent["assignments"]:
                if not isinstance(assignment, dict):
                    kept_rows.append(assignment)
                    continue
                media_id = str(assignment.get("media_id") or "")
                target = aliases.get(media_id, media_id)
                if target in named:
                    continue
                named.add(target)
                kept_rows.append(
                    {**assignment, "media_id": target} if target != media_id else assignment
                )
            intent["assignments"] = kept_rows
    return out


def receipt(aliases: Mapping[str, str], assignments: Sequence[Mapping[str, Any]]) -> list[dict]:
    """The ``DROPPED_DUPLICATES_FIELD`` rows, in attachment order of the dropped copies."""

    order = [str(row.get("media_id") or "") for row in assignments if isinstance(row, Mapping)]
    dropped = [media_id for media_id in dict.fromkeys(order) if media_id in aliases]
    dropped.extend(media_id for media_id in aliases if media_id not in dropped)
    return [{"media_id": media_id, "kept_media_id": aliases[media_id]} for media_id in dropped]


def aliases_from_candidates(all_candidates: Mapping[str, Any] | None) -> dict[str, str]:
    """The dispatch decision a job carries, read defensively (``{}`` when absent)."""

    rows = (all_candidates or {}).get(DROPPED_DUPLICATES_FIELD)
    aliases: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping):
            continue
        dropped, kept = row.get("media_id"), row.get("kept_media_id")
        if isinstance(dropped, str) and dropped and isinstance(kept, str) and kept:
            aliases.setdefault(dropped, kept)
    return aliases


def montage_record_fields(aliases: Mapping[str, str]) -> dict[str, Any]:
    """Receipt facts for ``assembly_plan["unified_montage"]`` (empty without a collapse).

    ``dropped_duplicate_clip_ids``: the byte-identical copies left out of the montage, in
    the dispatch order; ``duplicate_kept_clip_ids``: each dropped id -> the copy that
    plays in its place. A requirement checker reads them as proof that "use one of the
    duplicate videos" was done.
    """

    if not aliases:
        return {}
    return {
        "dropped_duplicate_clip_ids": list(aliases),
        "duplicate_kept_clip_ids": dict(aliases),
    }


__all__ = [
    "DROPPED_DUPLICATES_FIELD",
    "aliases_from_candidates",
    "collapse_strategy",
    "duplicate_aliases",
    "montage_record_fields",
    "original_sha256",
    "receipt",
]
