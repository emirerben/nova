"""Guard the Fly/Celery shutdown budget that protects late-acknowledged renders."""

import tomllib
from pathlib import Path

from app.worker import celery_app

REPO_ROOT = Path(__file__).resolve().parents[4]


def _fly_config() -> dict:
    return tomllib.loads((REPO_ROOT / "fly.toml").read_text())


def test_deploy_shutdown_budget_restores_work_before_fly_hard_stop() -> None:
    config = _fly_config()

    assert config["kill_signal"] == "SIGTERM"
    assert config["kill_timeout"] == 300
    assert config["env"]["REMAP_SIGTERM"] == "SIGQUIT"

    soft_shutdown_seconds = celery_app.conf.worker_soft_shutdown_timeout
    assert soft_shutdown_seconds == 240.0
    assert celery_app.conf.worker_enable_soft_shutdown_on_idle is True
    assert config["kill_timeout"] - soft_shutdown_seconds == 60


def test_runtime_supports_celery_soft_shutdown() -> None:
    pyproject = tomllib.loads((REPO_ROOT / "src/apps/api/pyproject.toml").read_text())
    celery_requirement = next(
        dependency
        for dependency in pyproject["project"]["dependencies"]
        if dependency.startswith("celery[")
    )

    assert celery_requirement == "celery[redis]>=5.5"


def test_deploy_shutdown_runtime_keys_are_not_scoped_to_vm_blocks() -> None:
    for vm in _fly_config()["vm"]:
        assert "kill_signal" not in vm
        assert "kill_timeout" not in vm


def test_broker_recovery_backstops_remain_enabled() -> None:
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.broker_transport_options["visibility_timeout"] == 1900


def test_idle_broker_polling_is_throttled() -> None:
    # Upstash bills per command; kombu's default re-arms BRPOP every 1s per
    # idle consumer. 10s keeps push-delivery instant and cuts idle polling 10x.
    assert celery_app.conf.broker_transport_options["polling_interval"] == 10
