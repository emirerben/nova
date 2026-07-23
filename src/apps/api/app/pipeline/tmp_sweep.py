"""Sweep orphaned nova temp dirs from RAM-backed /tmp before each task.

Why (2026-07-21 OOM incident follow-up, review finding): /tmp is tmpfs on Fly
Firecracker — every byte counts against the worker's RAM budget. When the
prefork child is SIGKILL'd mid-render (the OOM class itself), its
``tempfile.TemporaryDirectory`` cleanup never runs, the surviving parent
requeues via ``task_reject_on_worker_lost``, and the redelivered attempt lands
on the SAME machine whose tmpfs still holds attempt 1's downloaded originals
and intermediates — so the retry starts with strictly LESS headroom than the
attempt that just died. ``worker_max_memory_per_child`` cannot see or reclaim
tmpfs pages (they are not process RSS).

Hooked via Celery's ``task_prerun`` signal (worker.py): it fires in the child
before every task, including the respawned child after an OOM kill and the
redelivered attempt ~1900s later.

Age-cutoff invariant: prod runs ``--concurrency=1``, so at task_prerun no
OTHER task is live on this worker and any nova dir is orphaned — the cutoff
exists for the local-dev case where sibling worktree workers share one /tmp.
_MAX_AGE_S must stay above every render task's hard ``time_limit`` (1800s —
a live sibling task cannot own a dir older than its own time limit) and at or
below the broker ``visibility_timeout`` (1900s — so the dead attempt's dirs
are sweepable by the time its redelivery arrives). ``batch_import_from_drive``
(time_limit=2400) breaks the invariant only under concurrency>1, which prod
never runs; revisit this constant if that changes.
"""

from __future__ import annotations

import glob
import os
import shutil
import tempfile
import time

import structlog

log = structlog.get_logger()

_NOVA_TMP_GLOB = "nova*"
_MAX_AGE_S = 1850


def sweep_stale_nova_tmpdirs(*, max_age_s: int = _MAX_AGE_S) -> int:
    """Delete nova-prefixed temp dirs older than ``max_age_s``. Returns count.

    Best-effort by contract: any single failure is skipped, never raised —
    a sweep must not fail the task it runs ahead of.
    """
    cutoff = time.time() - max_age_s
    removed = 0
    for path in glob.glob(os.path.join(tempfile.gettempdir(), _NOVA_TMP_GLOB)):
        try:
            if not os.path.isdir(path) or os.path.islink(path):
                continue
            if os.path.getmtime(path) >= cutoff:
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except OSError:
            continue
    if removed:
        log.info("tmp_sweep_removed_stale_dirs", removed=removed)
    return removed
