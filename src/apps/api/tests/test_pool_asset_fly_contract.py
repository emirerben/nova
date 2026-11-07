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
