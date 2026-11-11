"""Ratchet: Job.status lists live in app/services/job_status.py only.

A literal of 3+ job statuses elsewhere is a hand copy that can drift (a copy
missing `template_ready` once left plan items spinning forever). Derive from
job_status.py instead. BASELINE holds the copies that exist today and may only
shrink.
"""

from __future__ import annotations

import ast
from pathlib import Path

from app.services.job_status import JobStatus

APP = Path(__file__).resolve().parents[1] / "app"
OWNER = "services/job_status.py"
STATUSES = {s.value for s in JobStatus}
BASELINE: dict[str, int] = {
    "kria/planner.py": 1,
    "routes/creation_threads.py": 2,
    "routes/template_jobs.py": 1,
    "services/cloud_render_policy.py": 1,
    "services/creation_editor_actions.py": 1,
    "services/creator_render_projection.py": 1,
    "services/job_cancel.py": 1,
    "services/speech_cleanup_preflight.py": 1,
    "services/tiktok_publishable.py": 1,
    "tasks/content_plan_build.py": 1,
    "tasks/generative_build.py": 2,
}


def _count_sites() -> dict[str, int]:
    found: dict[str, int] = {}
    for path in APP.rglob("*.py"):
        rel = path.relative_to(APP).as_posix()
        if rel == OWNER:
            continue
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Tuple | ast.List | ast.Set):
                continue
            vals = [e.value for e in node.elts if isinstance(e, ast.Constant)]
            if len(vals) >= 3 and len(vals) == len(node.elts) and set(vals) <= STATUSES:
                found[rel] = found.get(rel, 0) + 1
    return found


def test_no_new_hand_copied_status_lists():
    found = _count_sites()
    grown = {f: n for f, n in found.items() if n > BASELINE.get(f, 0)}
    assert not grown, (
        f"New hand-copied Job.status literal in {grown}. "
        f"Derive it from app/services/job_status.py instead."
    )
