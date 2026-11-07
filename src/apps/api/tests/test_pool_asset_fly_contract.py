from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[4]


def _fly() -> dict:
    return tomllib.loads((REPO_ROOT / "fly.toml").read_text())


def test_release_requires_alembic_head_before_process_start() -> None:
    assert _fly()["deploy"]["release_command"] == "python -m alembic upgrade head"


def test_topology_is_exactly_api_worker_light() -> None:
    """Release 2: the dedicated autoplace/speech_analysis groups are gone and the
    unified worker owns every analysis queue."""
    fly = _fly()
    assert set(fly["processes"]) == {"api", "worker", "light"}
    assert {row["processes"][0] for row in fly["vm"]} == {"api", "worker", "light"}
    for group in ("autoplace", "speech_analysis"):
        assert not any(row.get("processes") == [group] for row in fly["services"])

    worker_queues = set(fly["processes"]["worker"].split("-Q ", 1)[1].split()[0].split(","))
    assert {"autoplace-jobs", "speech-analysis", "visuals-analysis"}.issubset(worker_queues)


def test_worker_is_shared_cpu_now_cloud_render_is_retired() -> None:
    vm = next(row for row in _fly()["vm"] if row.get("processes") == ["worker"])
    assert vm["cpu_kind"] == "shared"
    assert vm["memory_mb"] <= 8192


def test_initial_deploy_does_not_switch_analysis_queue() -> None:
    assert "POOL_ASSET_ANALYSIS_QUEUE" not in _fly().get("env", {})


def test_visuals_queue_is_consumed_before_any_dispatch_can_switch_to_it() -> None:
    """Deploy order: the worker consumes `visuals-analysis` in the same release that
    adds the setting, and the switch is a later Fly secret, so a rolling deploy
    can never publish Visuals analysis to a queue nobody drains."""
    from app.config import Settings

    fly = _fly()
    queues = fly["processes"]["worker"].split("-Q ", 1)[1].split()[0].split(",")
    assert "visuals-analysis" in queues
    assert "VISUALS_ANALYSIS_QUEUE" not in fly.get("env", {})
    assert Settings.model_fields["visuals_analysis_queue"].default == ""


# --- #2183 (KRI-1240 revert): the Visuals deadline must describe the real lane.
# Failure modes this pins (written before the change):
#   L1 _QUEUE_CONCURRENCY claims more parallel slots than fly.toml runs, so a
#      healthy burst that is only waiting in line times out (the 2026-11-02..
#      11-07 state: constant 2, one concurrency=1 consumer).
#   L2 one queued analysis ahead is budgeted shorter than it can hold the slot.
#   L3 the lane's one in-progress non-analysis task is longer than budgeted.


def _visuals_consumers() -> dict[str, int]:
    """fly.toml process -> --concurrency, for every process draining visuals-analysis."""
    out: dict[str, int] = {}
    for name, command in _fly()["processes"].items():
        if "-Q " not in command:
            continue
        queues = command.split("-Q ", 1)[1].split()[0].split(",")
        if "visuals-analysis" in queues:
            out[name] = int(command.split("--concurrency=", 1)[1].split()[0])
    return out


def _worker_queues() -> set[str]:
    command = _fly()["processes"]["worker"]
    return set(command.split("-Q ", 1)[1].split()[0].split(","))


def test_visuals_deadline_concurrency_matches_fly_toml_consumers() -> None:
    from app.services import pool_analysis_deadline as bound

    consumers = _visuals_consumers()
    assert consumers == {"worker": 1}
    assert bound._QUEUE_CONCURRENCY == sum(consumers.values())


def test_visuals_queue_slot_covers_a_whole_attempt_chain() -> None:
    from app.services import pool_analysis_deadline as bound
    from app.tasks.autoplace import _AUTOPLACE_TASK_LIMITS

    gap = _AUTOPLACE_TASK_LIMITS["time_limit"] - _AUTOPLACE_TASK_LIMITS["soft_time_limit"]
    for kind in ("image", "video"):
        assert bound.queue_slot_s(kind) >= bound.run_window(kind).total_seconds() + gap


def test_lane_occupant_covers_the_longest_task_the_worker_can_be_running() -> None:
    from app.services import pool_analysis_deadline as bound
    from app.worker import celery_app

    celery_app.loader.import_default_modules()
    routes = celery_app.conf.task_routes or {}
    worker_queues = _worker_queues()
    longest = 0
    for name, task in celery_app.tasks.items():
        if name.startswith("celery.") or name == "tasks.analyze_pool_asset":
            continue
        queue = (routes.get(name) or {}).get("queue") or getattr(task, "queue", None) or "celery"
        # Tasks dispatched with an explicit queue= kwarg may land on any worker
        # queue; only tasks routed off the worker (maintenance, agent-control)
        # can never occupy its slot.
        if queue in worker_queues or queue not in {"maintenance", "agent-control"}:
            longest = max(longest, int(task.time_limit or 0))
    assert longest >= 1800  # the render orchestrators are on the worker
    assert bound._LANE_OCCUPANT.total_seconds() >= longest
