"""End Celery's deploy soft-shutdown wait as soon as in-flight work is done.

Celery 5.5/5.6 implements the soft-shutdown window as one
``sleep(worker_soft_shutdown_timeout)`` inside the SIGQUIT handler, and with
``worker_enable_soft_shutdown_on_idle`` it sleeps the whole window even when
nothing is running. On Fly every deploy therefore parked each worker process
group for the full 240s before its replacement machine could start (KRI-294).

That sleep runs on the MainProcess thread that also drives the event loop, so
nothing in ``celery.worker.state`` changes while it waits: a task that finishes
in a prefork child still looks active until the wait returns. Each child
therefore records the id of the task it just finished in shared memory (one
slot per pool process), and the replacement wait polls those slots.

Exit rules. Every fallback is the stock full-window wait, never a shorter one:

- Nothing reserved or executing at signal time: no wait. ETA/countdown messages
  whose time has not come are not reserved requests; kombu holds them unacked
  and restores them to their queue when the broker channel closes during the
  cold shutdown that follows. That restore does not depend on this wait.
- Otherwise wait until every request that was reserved at signal time has
  finished in its child, plus a short settle so the child's result reaches the
  pool pipe (the late ack is sent from it during cold shutdown), or until the
  timeout. A request that never reached a child keeps the full wait.

Whatever is still unfinished at the end is handled exactly as before: Celery
cancels it and the late-acknowledged message is restored for the next worker.
"""

from __future__ import annotations

import logging
import mmap
import types
from time import monotonic, sleep

from billiard.process import current_process
from celery.signals import celeryd_init, task_postrun
from celery.worker import state

logger = logging.getLogger(__name__)

# One slot per prefork process index. A pool larger than this simply never
# reports completions for the extra indexes, which keeps the full wait.
_MAX_POOL_SLOTS = 32
# Task ids are uuid4 strings (36 bytes); a longer id is not recorded.
_SLOT_BYTES = 64
_POLL_SECONDS = 0.5
# Time for a child to write its result to the pool pipe after task_postrun.
_RESULT_SETTLE_SECONDS = 1.0

# Anonymous MAP_SHARED memory created in the worker MainProcess before the pool
# forks, so every prefork child (including memory-limit replacements) shares it.
# None in every other process (API, tests, Beat), where recording is a no-op.
_finished_slots: mmap.mmap | None = None


def _record_finished_task(task_id: str | None = None, **_: object) -> None:
    """task_postrun handler; runs in the prefork child that executed the task."""
    if _finished_slots is None or not task_id:
        return
    index = getattr(current_process(), "index", None)
    encoded = task_id.encode()
    if index is None or not 0 <= index < _MAX_POOL_SLOTS or len(encoded) > _SLOT_BYTES:
        return
    start = index * _SLOT_BYTES
    _finished_slots[start : start + _SLOT_BYTES] = encoded.ljust(_SLOT_BYTES, b"\0")


def _finished_task_ids() -> set[str]:
    if _finished_slots is None:
        return set()
    ids = set()
    for index in range(_MAX_POOL_SLOTS):
        raw = _finished_slots[index * _SLOT_BYTES : (index + 1) * _SLOT_BYTES].rstrip(b"\0")
        if raw:
            # A torn read during a concurrent write cannot equal a pending id;
            # it only delays the exit to the next poll.
            ids.add(raw.decode(errors="replace"))
    return ids


def wait_for_in_flight_tasks(worker) -> None:
    """Drop-in replacement for ``WorkController.wait_for_soft_shutdown``."""
    timeout = float(worker.app.conf.worker_soft_shutdown_timeout or 0)
    if timeout <= 0:
        return
    in_flight = {request.id for request in (*state.reserved_requests, *state.active_requests)}
    if not in_flight:
        logger.warning("Soft shutdown: no task in flight, skipping the %ss wait", timeout)
        return

    logger.warning(
        "Initiating Soft Shutdown: waiting up to %ss for %d in-flight task(s)",
        timeout,
        len(in_flight),
    )
    started = monotonic()
    deadline = started + timeout
    while (remaining := deadline - monotonic()) > 0:
        if _finished_slots is not None and in_flight <= _finished_task_ids():
            sleep(min(_RESULT_SETTLE_SECONDS, remaining))
            logger.warning(
                "Soft shutdown: in-flight task(s) finished after %.1fs, ending the wait early",
                monotonic() - started,
            )
            return
        sleep(min(_POLL_SECONDS, remaining))
    logger.warning("Soft shutdown: %ss elapsed, restoring unfinished task(s)", timeout)


def _install_on_worker(sender=None, instance=None, **_: object) -> None:
    """celeryd_init handler; runs in the worker MainProcess before the pool forks."""
    global _finished_slots
    if instance is None:
        return
    if _finished_slots is None:
        _finished_slots = mmap.mmap(-1, _MAX_POOL_SLOTS * _SLOT_BYTES)
    instance.wait_for_soft_shutdown = types.MethodType(wait_for_in_flight_tasks, instance)


def install_soft_shutdown_early_exit() -> None:
    """Make workers started in this process end the soft-shutdown wait early."""
    celeryd_init.connect(_install_on_worker, weak=False)
    task_postrun.connect(_record_finished_task, weak=False)
