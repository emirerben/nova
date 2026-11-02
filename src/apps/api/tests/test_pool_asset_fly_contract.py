from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[4]


def _fly() -> dict:
    return tomllib.loads((REPO_ROOT / "fly.toml").read_text())


def test_release_requires_alembic_head_before_process_start() -> None:
    assert _fly()["deploy"]["release_command"] == "python -m alembic upgrade head"


def test_fly_toml_declares_exactly_the_three_production_process_groups() -> None:
    """2026-11-02: autoplace and speech_analysis Machines were destroyed on purpose.
    `fly deploy` launches a Machine for every declared group that has none, so a
    leftover declaration would silently re-create them. Every services/vm block must
    also name only a declared group."""
    fly = _fly()
    groups = {"api", "worker", "light"}
    assert set(fly["processes"]) == groups
    for key in ("services", "vm"):
        for row in fly[key]:
            assert set(row["processes"]) <= groups, row


def test_worker_consumes_every_queue_the_retired_groups_served() -> None:
    queues = set(_fly()["processes"]["worker"].split("-Q ", 1)[1].split()[0].split(","))
    assert {"autoplace-jobs", "visuals-analysis", "speech-analysis"} <= queues


def test_worker_is_always_on_and_sized_for_the_media_analysis_queues() -> None:
    fly = _fly()
    service = next(row for row in fly["services"] if row.get("processes") == ["worker"])
    assert service["auto_start_machines"] is True
    vm = next(row for row in fly["vm"] if row.get("processes") == ["worker"])
    # The retired autoplace Machine was 2 vCPU / 2 GB; the worker must not be smaller.
    assert vm["memory_mb"] >= 2048
    assert vm["cpus"] >= 2


def test_initial_deploy_does_not_switch_analysis_queue() -> None:
    assert "POOL_ASSET_ANALYSIS_QUEUE" not in _fly().get("env", {})


def test_visuals_queue_is_consumed_before_any_dispatch_can_switch_to_it() -> None:
    """Deploy order: the worker process consumes `visuals-analysis` in the same
    release that adds the setting, and the switch is a later Fly secret, so a
    rolling deploy can never publish Visuals analysis to a queue nobody drains."""
    from app.config import Settings

    fly = _fly()
    queues = fly["processes"]["worker"].split("-Q ", 1)[1].split()[0].split(",")
    assert "visuals-analysis" in queues
    assert "VISUALS_ANALYSIS_QUEUE" not in fly.get("env", {})
    assert Settings.model_fields["visuals_analysis_queue"].default == ""
