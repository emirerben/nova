"""Guard the Fly/Celery shutdown budget that protects late-acknowledged renders."""

import os
import subprocess
import sys
import tomllib
from pathlib import Path
from types import SimpleNamespace

from celery.signals import celeryd_init

import app.worker
from app.config import settings
from app.services.celery_soft_shutdown import wait_for_in_flight_tasks
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
    assert config["kill_timeout"] - soft_shutdown_seconds == 60


def test_soft_shutdown_ends_when_in_flight_work_does() -> None:
    # KRI-294: stock on_idle slept the full 240s on every deploy even with
    # nothing running, so each worker group stopped consuming for ~4m15s. The
    # early exit is installed on the worker instance at celeryd_init; stock
    # on_idle stays off while it is, so an idle worker never sleeps.
    assert settings.celery_soft_shutdown_early_exit_enabled is True
    assert celery_app.conf.worker_enable_soft_shutdown_on_idle is False

    instance = SimpleNamespace()
    celeryd_init.send(sender="guard@test", instance=instance, conf=celery_app.conf, options={})
    assert instance.wait_for_soft_shutdown.__func__ is wait_for_in_flight_tasks


def test_early_exit_kill_switch_restores_the_stock_full_window_wait() -> None:
    probe = (
        "from types import SimpleNamespace\n"
        "from celery.signals import celeryd_init\n"
        "import app.worker\n"
        "from app.worker import celery_app\n"
        "instance = SimpleNamespace()\n"
        "celeryd_init.send(sender='probe', instance=instance, conf=celery_app.conf, options={})\n"
        "print(app.worker.__file__)\n"
        "print(celery_app.conf.worker_enable_soft_shutdown_on_idle,"
        " hasattr(instance, 'wait_for_soft_shutdown'))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO_ROOT / "src/apps/api",
        env={**os.environ, "CELERY_SOFT_SHUTDOWN_EARLY_EXIT_ENABLED": "false"},
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )

    imported_from, policy = result.stdout.strip().splitlines()[-2:]
    # The probe must exercise this checkout, not a sibling worktree's install.
    assert Path(imported_from).resolve() == Path(app.worker.__file__).resolve()
    assert policy == "True False"


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
