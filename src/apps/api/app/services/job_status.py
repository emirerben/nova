"""Single owner of the `Job.status` vocabulary: members, terminal buckets,
cancellable and reapable sets.

Hand-copied status lists drift apart (a copy missing `template_ready` left plan
items spinning forever), so every consumer derives from this module and
tests/test_job_status_vocabulary.py fails on a new literal copy.

Plan-item Job.status buckets, shared by the read path and the dispatch path.

`derive_item_status` (routes/plan_items.py) and the dispatch-time
active-render re-check (`dispatch_item_render_for` in
tasks/content_plan_build.py) MUST agree on which Job statuses are terminal —
if they drift, a failed item could refuse to re-generate (dispatch thinks the
old job is still active) or a live render could be double-dispatched
(dispatch thinks it's terminal). Single-sourced here for that reason
(plans/014).

routes/me.py derives its sets from these constants (same buckets — the
library tiles and the plan dashboard must agree across every job mode).
"""

from __future__ import annotations

from enum import StrEnum


class JobStatus(StrEnum):
    IMPORTING = "importing"
    QUEUED = "queued"
    PROCESSING = "processing"
    MATCHING = "matching"
    RENDERING = "rendering"
    POSTING = "posting"
    POSTING_PARTIAL = "posting_partial"
    POSTING_FAILED = "posting_failed"
    CLIPS_READY = "clips_ready"
    CLIPS_READY_PARTIAL = "clips_ready_partial"
    TEMPLATE_READY = "template_ready"
    MUSIC_READY = "music_ready"
    VARIANTS_READY = "variants_ready"
    VARIANTS_READY_PARTIAL = "variants_ready_partial"
    VARIANTS_FAILED = "variants_failed"
    MATCHING_FAILED = "matching_failed"
    NO_LABELED_TRACKS = "no_labeled_tracks"
    PROCESSING_FAILED = "processing_failed"
    DONE = "done"
    CANCELLED = "cancelled"
    DRAFT = "draft"
    AWAITING_DEVICE = "awaiting_device"


def _values(*members: JobStatus) -> tuple[str, ...]:
    return tuple(m.value for m in members)


# Generative variant states (mode="content_plan" reuses them) PLUS
# template_ready / music_ready: POST /me/jobs/{id}/add-to-plan can link ANY
# owned job to a plan item, so a template/music job pinned to a plan day must
# read as terminal — otherwise the item shows "generating" forever and
# Generate reports already_active without ever minting a render (review
# 2026-08-04, CX3; me.py's superset had these, plan_items' copy had drifted).
PLAN_ITEM_JOB_READY: frozenset[str] = frozenset(
    _values(
        JobStatus.VARIANTS_READY,
        JobStatus.VARIANTS_READY_PARTIAL,
        JobStatus.DONE,
        JobStatus.CLIPS_READY,
        JobStatus.TEMPLATE_READY,
        JobStatus.MUSIC_READY,
    )
)
PLAN_ITEM_JOB_FAILED: frozenset[str] = frozenset(
    _values(
        JobStatus.VARIANTS_FAILED,
        JobStatus.MATCHING_FAILED,
        JobStatus.NO_LABELED_TRACKS,
        JobStatus.PROCESSING_FAILED,
        JobStatus.POSTING_FAILED,
        JobStatus.CANCELLED,
    )
)
PLAN_ITEM_JOB_TERMINAL: frozenset[str] = PLAN_ITEM_JOB_READY | PLAN_ITEM_JOB_FAILED

# Tuples, not sets: used in SQL IN clauses and error messages, order is stable.
# Keep intentional, broader is dangerous. Mirrored in the web admin job page.
JOB_CANCELLABLE_STATUSES: tuple[str, ...] = _values(
    JobStatus.IMPORTING,
    JobStatus.QUEUED,
    JobStatus.PROCESSING,
    JobStatus.MATCHING,
    JobStatus.RENDERING,
    JobStatus.POSTING,
)

# Worker-owned, non-terminal: the reaper fails these when stale. Excludes
# `queued` (invisible to celery inspect) and `template_ready` (a success state).
JOB_REAPER_NON_TERMINAL_STATUSES: tuple[str, ...] = _values(
    JobStatus.PROCESSING,
    JobStatus.MATCHING,
    JobStatus.RENDERING,
    JobStatus.POSTING,
)
