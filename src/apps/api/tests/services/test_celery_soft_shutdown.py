"""Early exit from Celery's deploy soft-shutdown wait (KRI-294).

The real-signal drill (prefork worker + Redis, SIGTERM remapped to SIGQUIT)
is recorded in agents/DECISIONS.md "[2026-10-03] Celery soft shutdown"; these
tests pin the decision logic and the cross-process plumbing it relies on.
"""

import inspect
import mmap
from dataclasses import dataclass
from types import SimpleNamespace

import billiard.pool
import pytest
from billiard.process import current_process
from celery.apps import worker as celery_apps_worker
from celery.worker import state

from app.services import celery_soft_shutdown as soft


@dataclass(frozen=True)
class _Request:
    id: str


class _Clock:
    """Fake monotonic clock; ``on_sleep`` lets a test finish tasks mid-wait."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []
        self.on_sleep = lambda elapsed: None

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
        self.on_sleep(self.now - 1000.0)


@pytest.fixture
def clock(monkeypatch) -> _Clock:
    fake = _Clock()
    monkeypatch.setattr(soft, "monotonic", fake.monotonic)
    monkeypatch.setattr(soft, "sleep", fake.sleep)
    return fake


@pytest.fixture
def slots(monkeypatch) -> None:
    monkeypatch.setattr(
        soft, "_finished_slots", mmap.mmap(-1, soft._MAX_POOL_SLOTS * soft._SLOT_BYTES)
    )


@pytest.fixture
def in_flight(monkeypatch):
    reserved: list[_Request] = []
    active: list[_Request] = []
    monkeypatch.setattr(state, "reserved_requests", reserved)
    monkeypatch.setattr(state, "active_requests", active)

    def add(task_id: str, *, started: bool = True) -> None:
        reserved.append(_Request(task_id))
        if started:
            active.append(_Request(task_id))

    return add


def _record_in_slot(index: int, task_id: str) -> None:
    start = index * soft._SLOT_BYTES
    soft._finished_slots[start : start + soft._SLOT_BYTES] = task_id.encode().ljust(
        soft._SLOT_BYTES, b"\0"
    )


def _worker(timeout: float = 240.0):
    return SimpleNamespace(
        app=SimpleNamespace(conf=SimpleNamespace(worker_soft_shutdown_timeout=timeout))
    )


def test_idle_worker_skips_the_wait(clock, slots, in_flight) -> None:
    soft.wait_for_in_flight_tasks(_worker())

    assert clock.sleeps == []


def test_wait_ends_once_every_in_flight_task_has_finished(clock, slots, in_flight) -> None:
    in_flight("task-a")
    in_flight("task-b", started=False)  # accepted by the child, ack not yet processed

    def finish(elapsed: float) -> None:
        if elapsed >= 3:
            _record_in_slot(0, "task-a")
        if elapsed >= 6:
            _record_in_slot(1, "task-b")

    clock.on_sleep = finish
    soft.wait_for_in_flight_tasks(_worker())

    assert clock.sleeps[-1] == soft._RESULT_SETTLE_SECONDS
    assert sum(clock.sleeps) == pytest.approx(6 + soft._RESULT_SETTLE_SECONDS)


def test_unfinished_task_keeps_the_full_window(clock, slots, in_flight) -> None:
    in_flight("task-a")
    in_flight("task-b")
    clock.on_sleep = lambda elapsed: _record_in_slot(0, "task-a")

    soft.wait_for_in_flight_tasks(_worker())

    assert sum(clock.sleeps) == pytest.approx(240.0)


def test_an_older_finished_id_in_the_slot_does_not_end_the_wait(clock, slots, in_flight) -> None:
    _record_in_slot(0, "earlier-task")
    in_flight("task-a")

    soft.wait_for_in_flight_tasks(_worker())

    assert sum(clock.sleeps) == pytest.approx(240.0)


def test_without_shared_slots_the_wait_is_the_stock_full_window(
    clock, monkeypatch, in_flight
) -> None:
    monkeypatch.setattr(soft, "_finished_slots", None)
    in_flight("task-a")

    soft.wait_for_in_flight_tasks(_worker())

    assert sum(clock.sleeps) == pytest.approx(240.0)


def test_disabled_soft_shutdown_never_waits(clock, slots, in_flight) -> None:
    in_flight("task-a")

    soft.wait_for_in_flight_tasks(_worker(timeout=0.0))

    assert clock.sleeps == []


def _record_from_pool_child(task_id: str) -> int:
    soft._record_finished_task(task_id=task_id)
    return current_process().index


def test_pool_child_records_finished_task_in_parent_visible_memory(monkeypatch) -> None:
    monkeypatch.setattr(soft, "_finished_slots", None)
    soft._install_on_worker(instance=SimpleNamespace())  # MainProcess, before the fork

    pool = billiard.pool.Pool(processes=1)
    try:
        index = pool.apply(_record_from_pool_child, ("5f97e02f-3397-45cd-9366-46fcaa1c2dcd",))
    finally:
        pool.terminate()
        pool.join()

    assert index == 0
    assert soft._finished_task_ids() == {"5f97e02f-3397-45cd-9366-46fcaa1c2dcd"}


def test_recording_is_a_no_op_outside_a_pool_child(slots) -> None:
    soft._record_finished_task(task_id="task-in-main-process")
    soft._record_finished_task(task_id=None)

    assert soft._finished_task_ids() == set()


def test_install_binds_the_early_exit_onto_the_worker_instance(monkeypatch) -> None:
    monkeypatch.setattr(soft, "_finished_slots", None)
    instance = SimpleNamespace()

    soft._install_on_worker(instance=instance)

    assert instance.wait_for_soft_shutdown.__func__ is soft.wait_for_in_flight_tasks
    assert soft._finished_slots is not None


def test_celery_still_calls_the_hooks_this_relies_on() -> None:
    # Tripwire for Celery upgrades (pyproject allows any 5.5+): the cold
    # shutdown handler must look the wait up on the worker instance, and the
    # worker must hand itself to celeryd_init before the pool forks. If either
    # changes, the override is silently ignored and every deploy waits again.
    assert "worker.wait_for_soft_shutdown()" in inspect.getsource(
        celery_apps_worker.on_cold_shutdown
    )
    on_before_init = inspect.getsource(celery_apps_worker.Worker.on_before_init)
    assert "celeryd_init.send" in on_before_init
    assert "instance=self" in on_before_init
