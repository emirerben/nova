"""The replay compares only the agent runs production's trace can also show (KRI-1375).

Production's side of the route comparison is the plan item's proposal trace (runs linked to
the item) plus the job's own runs. The clip-intent agents run on the Creator session alone, so
production never lists them. Verifier, 19f61092: the replay counted every run in its scratch
database and named `nova.plan.clip_request_resolver` as an agent production never ran.

Ways this can go wrong, checked on Postgres with real agent run rows:
* a session-only run is compared, so its failure relabels a reproduced failure as a divergence;
* the item's own runs, or the replay job's render runs, drop out of the comparison;
* a run of an earlier replay render is compared, though production lists only the job it reads;
* the spend total drops the runs left out of the comparison.
"""

from __future__ import annotations

import importlib.util
import sys
import uuid
from pathlib import Path

from app.database import sync_session
from app.models import AgentRun, CreationThread, Job, PlanItem
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

SCRIPTS = Path(__file__).parents[2] / "scripts"
spec = importlib.util.spec_from_file_location("replay_job", SCRIPTS / "replay_job.py")
assert spec and spec.loader
replay_job = importlib.util.module_from_spec(spec)
sys.modules.setdefault("replay_job", replay_job)
spec.loader.exec_module(replay_job)


def _seed_replay_runs() -> uuid.UUID:
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
        earlier, current = uuid.uuid4(), uuid.uuid4()
        for job_id in (earlier, current):
            db.add(
                Job(
                    id=job_id,
                    user_id=user_id,
                    status="processing_failed",
                    job_type="generative",
                    raw_storage_path=f"users/{user_id}/jobs/{job_id}/source.mp4",
                    content_plan_item_id=item_id,
                )
            )
            db.flush()
        db.get(PlanItem, item_id).current_job_id = current
        rows = [
            ("nova.creator.main", "ok", {"plan_item_id": item_id}),
            ("nova.plan.clip_request_resolver", "terminal_transient", {}),
            ("nova.video.clip_question", "terminal_rule_based", {"job_id": earlier}),
            ("nova.edit.intro_writer", "ok", {"job_id": current}),
        ]
        for name, outcome, owner in rows:
            db.add(
                AgentRun(
                    agent_name=name,
                    prompt_version=f"v-{item_id}",
                    model="gemini-2.5-flash",
                    outcome=outcome,
                    cost_usd=0.01,
                    creator_agent_session_id=session_id,
                    **owner,
                )
            )
        db.commit()
    return item_id


def test_only_runs_production_can_list_are_compared() -> None:
    item_id = _seed_replay_runs()
    recorded = replay_job.recorded_agent_runs(str(item_id))
    # The shared test database holds other tests' runs; a replay's scratch database does not.
    runs = [run for run in recorded["runs"] if run["prompt_version"] == f"v-{item_id}"]
    assert {run["agent_name"]: run["comparable"] for run in runs} == {
        "nova.creator.main": True,
        "nova.plan.clip_request_resolver": False,
        "nova.video.clip_question": False,
        "nova.edit.intro_writer": True,
    }
    production = replay_job.ProductionAgents(
        models={"nova.creator.main": frozenset({"gemini-2.5-flash"})},
        prompt_versions={},
        trace_complete=True,
    )
    route = replay_job.compare_routes(production, {**recorded, "runs": runs})
    assert route["diverged_failures"] == []
    assert route["replay_only_agents"] == ["nova.edit.intro_writer"]
    assert route["replay_spend_usd"] == 0.04
