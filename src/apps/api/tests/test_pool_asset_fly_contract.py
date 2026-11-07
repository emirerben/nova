from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[4]


def _fly() -> dict:
    return tomllib.loads((REPO_ROOT / "fly.toml").read_text())


def test_release_requires_alembic_head_before_process_start() -> None:
    assert _fly()["deploy"]["release_command"] == "python -m alembic upgrade head"


def _queues(process_line: str) -> list[str]:
    return process_line.split("-Q ", 1)[1].split()[0].split(",")


def test_topology_is_exactly_api_worker_light_visuals() -> None:
    """Release 2 dropped the dedicated autoplace/speech_analysis groups and the
    unified worker owns every analysis queue; KRI-1240 adds back ONE small
    always-on group for creator-facing Visuals analysis only."""
    fly = _fly()
    groups = {"api", "worker", "light", "visuals"}
    assert set(fly["processes"]) == groups
    assert {row["processes"][0] for row in fly["vm"]} == groups
    for group in ("autoplace", "speech_analysis"):
        assert not any(row.get("processes") == [group] for row in fly["services"])

    worker_queues = set(_queues(fly["processes"]["worker"]))
    assert {"autoplace-jobs", "speech-analysis", "visuals-analysis"}.issubset(worker_queues)


def test_visuals_group_is_one_small_always_on_parallel_analysis_machine() -> None:
    """KRI-1240: 10 Visuals were analysed one at a time on the worker's single
    slot. The visuals group drains only visuals-analysis, several at a time, on a
    small always-on VM, and recycles children below its own memory."""
    fly = _fly()
    line = fly["processes"]["visuals"]
    assert _queues(line) == ["visuals-analysis"]
    assert " -B" not in line  # Beat stays on light only (it does not de-duplicate)

    concurrency = int(line.split("--concurrency=", 1)[1].split()[0])
    assert concurrency >= 3, "the whole point is analysing several Visuals at once"

    vm = next(row for row in fly["vm"] if row.get("processes") == ["visuals"])
    assert vm["cpu_kind"] == "shared"
    assert vm["memory_mb"] <= 4096

    # The app-wide 3GB recycle threshold (settings.worker_max_memory_per_child_kb)
    # can never fire on this VM, so the process line carries its own. It compares
    # a child's peak RSS, which on Linux includes the parent's copy-on-write pages,
    # so threshold x concurrency over-counts; the pin is that one ballooned child is
    # recycled well before it alone could take half the machine.
    per_child_kb = int(line.split("--max-memory-per-child=", 1)[1].split()[0])
    assert 256 * 1024 <= per_child_kb < vm["memory_mb"] * 1024 // 2

    # Always-on (no cold start for a creator's burst). A [[services]] block also
    # makes flyctl's first-deploy HA spare a started machine instead of a
    # stopped standby, which the Fly Deploy topology guard rejects.
    services = [row for row in fly["services"] if row.get("processes") == ["visuals"]]
    assert len(services) == 1
    assert services[0]["auto_stop_machines"] is False
    assert "ports" not in services[0]


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
    queues = _queues(fly["processes"]["worker"])
    assert "visuals-analysis" in queues
    assert "VISUALS_ANALYSIS_QUEUE" not in fly.get("env", {})
    assert Settings.model_fields["visuals_analysis_queue"].default == ""
