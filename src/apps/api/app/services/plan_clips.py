"""Single-writer helper for PlanItem clip data (D16).

All code that writes item.clip_gcs_paths or item.clip_assignments MUST go
through set_item_clips — this is the sole writer contract. Enforcing it here
(in a neutral services module, importable by both routes and tasks) prevents
the task→route import antipattern.

IMPORTANT: This module does NOT perform prefix validation. Prefix checks are
the responsibility of the caller (e.g., the attach route validates the
users/{user_id}/plan/{item_id}/ prefix). The seed write in content_plan_build
uses a different prefix (plan/{plan_id}/seed/) and must not be broken.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from app.models import PlanItem

_MAX_CLIPS_PER_ITEM = 50  # mirrors routes/plan_items.py; kept in sync, not imported (avoids circ)


@dataclass
class ClipAssignment:
    """One clip linked (or not) to a filming guide shot.

    shot_id = None  →  extra-footage pool
    shot_id = str   →  linked to filming_guide[*].shot_id
    user_note       →  optional creator context about the clip ("famous vegan
                       restaurant in Buenos Aires"); untrusted free-text, capped
                       by the route, rendered as DATA in every prompt.
    machine_matched →  True when the footage pool matcher placed this clip (not
                       the user). Suppresses the conformance judge until the
                       user touches the slot, and renders as a provisional chip.
    """

    gcs_path: str
    shot_id: str | None = None
    user_note: str = ""
    machine_matched: bool = False
    # Stable proposal-level identity. Legacy assignments receive one when the
    # creator first clicks Plan edit; normal re-attaches preserve it by path.
    media_id: str | None = None
    # Server-probed duration. Never accepted from the attach request body.
    duration_s: float | None = None
    duration_probe_status: str | None = None
    duration_probe_generation: str | None = None
    duration_probe_attempted_at: str | None = None


class ClipAssignmentError(ValueError):
    """Raised by set_item_clips on constraint violations (caller maps to 422)."""


def ensure_clip_media_ids(item: PlanItem) -> bool:
    """Backfill stable proposal IDs without bypassing clip-assignment ownership."""

    changed = False
    assignments: list[dict] = []
    raw_assignments = item.clip_assignments or []
    if not raw_assignments and item.clip_gcs_paths:
        raw_assignments = [
            {
                "gcs_path": path,
                "shot_id": None,
                "user_note": "",
                "machine_matched": False,
            }
            for path in item.clip_gcs_paths
        ]
        changed = True
    for raw in raw_assignments:
        if not isinstance(raw, dict) or not raw.get("gcs_path"):
            continue
        entry = dict(raw)
        if not entry.get("media_id"):
            entry["media_id"] = str(uuid.uuid4())
            changed = True
        assignments.append(entry)
    if changed:
        item.clip_assignments = assignments
    return changed


def set_item_clips(item: PlanItem, assignments: list[ClipAssignment]) -> None:
    """Atomically update clip_assignments and clip_gcs_paths on *item*.

    Ordering contract: shot-slot clips come first (in assignment order),
    pool clips (shot_id=None) come after. This is free signal for the render
    engine, which currently treats clips as an unordered pool.

    Validates (raises ClipAssignmentError on violation):
      - Total count ≤ _MAX_CLIPS_PER_ITEM (50)
      - No duplicate gcs_path within the batch

    Does NOT validate GCS path prefixes — that is the caller's responsibility.
    """
    total = len(assignments)
    if total > _MAX_CLIPS_PER_ITEM:
        raise ClipAssignmentError(f"Too many clips: {total} > {_MAX_CLIPS_PER_ITEM} maximum")

    # Dupe-check gcs_paths.
    seen_paths: set[str] = set()
    for a in assignments:
        if a.gcs_path in seen_paths:
            raise ClipAssignmentError(f"Duplicate gcs_path in one request: {a.gcs_path}")
        seen_paths.add(a.gcs_path)

    # Multiple clips per shot_id are allowed (a shot may request several clips,
    # e.g. "5+ clips from the run"). Only gcs_path must be unique.

    # Derive clip_gcs_paths: shot-slot clips first, pool after.
    slot_clips = [a.gcs_path for a in assignments if a.shot_id is not None]
    pool_clips = [a.gcs_path for a in assignments if a.shot_id is None]

    from app.services.speech_cleanup import main_footage_identity, reconcile_consent

    previous_identity = main_footage_identity(item)
    item.clip_assignments = [
        {
            "gcs_path": a.gcs_path,
            "shot_id": a.shot_id,
            "user_note": a.user_note or "",
            "machine_matched": bool(a.machine_matched),
            **({"media_id": a.media_id} if a.media_id else {}),
            **({"duration_s": a.duration_s} if a.duration_s is not None else {}),
            **(
                {"duration_probe_status": a.duration_probe_status}
                if a.duration_probe_status
                else {}
            ),
            **(
                {"duration_probe_generation": a.duration_probe_generation}
                if a.duration_probe_generation
                else {}
            ),
            **(
                {"duration_probe_attempted_at": a.duration_probe_attempted_at}
                if a.duration_probe_attempted_at
                else {}
            ),
        }
        for a in assignments
    ]
    item.clip_gcs_paths = slot_clips + pool_clips
    if main_footage_identity(item) != previous_identity:
        reconcile_consent(item, previous_identity)
        # Import locally to keep this low-level sole writer free of a module
        # cycle at import time. No proposal means this is a cheap no-op.
        from app.services.edit_proposals import mark_edit_proposal_stale  # noqa: PLC0415

        mark_edit_proposal_stale(item)
