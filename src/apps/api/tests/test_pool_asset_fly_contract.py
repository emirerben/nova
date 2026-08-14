from __future__ import annotations

import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).parents[4]


def _fly() -> dict:
    return tomllib.loads((REPO_ROOT / "fly.toml").read_text())


def test_release_requires_alembic_head_before_process_start() -> None:
    assert _fly()["deploy"]["release_command"] == "python -m alembic upgrade head"


def test_dedicated_autoplace_worker_is_always_on_and_bounded() -> None:
    fly = _fly()
    command = fly["processes"]["autoplace"]
    assert "--concurrency=2" in command
    assert "-Q autoplace-jobs" in command

    service = next(row for row in fly["services"] if row.get("processes") == ["autoplace"])
    assert service["auto_stop_machines"] is False
    assert service["auto_start_machines"] is True

    vm = next(row for row in fly["vm"] if row.get("processes") == ["autoplace"])
    assert vm == {
        "processes": ["autoplace"],
        "cpu_kind": "shared",
        "cpus": 2,
        "memory_mb": 2048,
    }


def test_initial_deploy_does_not_switch_analysis_queue() -> None:
    assert "POOL_ASSET_ANALYSIS_QUEUE" not in _fly().get("env", {})
