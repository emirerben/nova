"""Plan-item Job.status buckets, shared by the read path and the dispatch path.

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

# Generative variant states (mode="content_plan" reuses them) PLUS
# template_ready / music_ready: POST /me/jobs/{id}/add-to-plan can link ANY
# owned job to a plan item, so a template/music job pinned to a plan day must
# read as terminal — otherwise the item shows "generating" forever and
# Generate reports already_active without ever minting a render (review
# 2026-08-04, CX3; me.py's superset had these, plan_items' copy had drifted).
PLAN_ITEM_JOB_READY: frozenset[str] = frozenset(
    {
        "variants_ready",
        "variants_ready_partial",
        "done",
        "clips_ready",
        "template_ready",
        "music_ready",
    }
)
PLAN_ITEM_JOB_FAILED: frozenset[str] = frozenset(
    {
        "variants_failed",
        "matching_failed",
        "no_labeled_tracks",
        "processing_failed",
        "posting_failed",
        "cancelled",
    }
)
PLAN_ITEM_JOB_TERMINAL: frozenset[str] = PLAN_ITEM_JOB_READY | PLAN_ITEM_JOB_FAILED
