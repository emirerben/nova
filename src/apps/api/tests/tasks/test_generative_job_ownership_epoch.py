"""Regression tests for the durable content-plan Job ownership epoch fence."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.models import ContentPlan, Job, PlanItem
from app.services.generative_jobs import build_generative_job
from app.tasks import generative_build as gb


class _GraphSession:
    """Small identity-map stand-in for the fence's canonical lock traversal."""

    def __init__(self, *, job, item=None, plan=None):  # noqa: ANN001
        self.job = job
        self.item = item
        self.plan = plan
        self.get_calls: list[tuple[type, object, bool, bool]] = []

    def get(self, model, key, **kwargs):  # noqa: ANN001, ANN201
        self.get_calls.append(
            (
                model,
                key,
                bool(kwargs.get("with_for_update")),
                bool(kwargs.get("populate_existing")),
            )
        )
        if model is Job and key == self.job.id:
            return self.job
        if model is PlanItem and self.item is not None and key == self.item.id:
            return self.item
        if model is ContentPlan and self.plan is not None and key == self.plan.id:
            return self.plan
        return None


class _StaleAwareSession:
    """Fakes the real staleness rule from #813/#845: the FIRST ``.get()`` for
    a primary key in a session wins and is cached; every later ``.get()`` for
    that same key returns the SAME cached object unless ``populate_existing``
    is also passed -- ``with_for_update`` alone does not refresh it. This is
    what a real SQLAlchemy ``Session`` does, and it is precisely what lets a
    genuine row lock hand back pre-lock data.
    """

    def __init__(self, *, plan, job_seed, job_fresh, item_seed, item_fresh):  # noqa: ANN001
        self._plan = plan
        self._seed = {Job: job_seed, PlanItem: item_seed}
        self._fresh = {Job: job_fresh, PlanItem: item_fresh}
        self._cache: dict[tuple[type, object], object] = {}

    def get(self, model, key, **kwargs):  # noqa: ANN001, ANN201
        if model is ContentPlan:
            return self._plan if key == self._plan.id else None
        cache_key = (model, key)
        if cache_key not in self._cache:
            self._cache[cache_key] = self._seed[model]
        elif kwargs.get("populate_existing"):
            self._cache[cache_key] = self._fresh[model]
        return self._cache[cache_key]


def _plan_job_graph(*, live_epoch: int, bound_epoch: int | None):
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()
    job = SimpleNamespace(
        id=job_id,
        user_id=user_id,
        status="queued",
        mode="content_plan",
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=bound_epoch,
    )
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=job_id,
    )
    plan = SimpleNamespace(
        id=plan_id,
        user_id=user_id,
        ownership_epoch=live_epoch,
        ownership_quarantined_at=None,
    )
    return job, item, plan, _GraphSession(job=job, item=item, plan=plan)


@pytest.mark.parametrize(
    ("live_epoch", "bound_epoch", "accepted_epoch"),
    [
        pytest.param(0, None, 0, id="legacy-null-is-epoch-zero"),
        pytest.param(4, 4, 4, id="matching-bound-epoch"),
    ],
)
def test_plan_job_epoch_fence_accepts_only_the_live_bound_generation(
    live_epoch: int,
    bound_epoch: int | None,
    accepted_epoch: int,
) -> None:
    job, _item, _plan, session = _plan_job_graph(
        live_epoch=live_epoch,
        bound_epoch=bound_epoch,
    )

    with patch(
        "app.services.content_plan_persona.load_owned_plan_persona_sync",
        return_value=SimpleNamespace(),
    ):
        entry = gb._lock_owned_entry_job(session, str(job.id))

    assert entry == (job, accepted_epoch)


@pytest.mark.parametrize(
    ("live_epoch", "bound_epoch"),
    [
        pytest.param(1, None, id="legacy-null-after-epoch-advance"),
        pytest.param(8, 7, id="bound-job-after-quarantine-repair"),
        pytest.param(0, -1, id="negative-bound-epoch"),
    ],
)
def test_plan_job_epoch_fence_rejects_stale_or_negative_generation(
    live_epoch: int,
    bound_epoch: int | None,
) -> None:
    job, _item, plan, session = _plan_job_graph(
        live_epoch=live_epoch,
        bound_epoch=bound_epoch,
    )
    # The epoch must remain authoritative after an operator clears quarantine.
    plan.ownership_quarantined_at = None

    with patch(
        "app.services.content_plan_persona.load_owned_plan_persona_sync",
        return_value=SimpleNamespace(),
    ):
        entry = gb._lock_owned_entry_job(session, str(job.id))

    assert entry is None


def test_public_non_plan_job_bypasses_the_plan_epoch_graph() -> None:
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="queued",
        mode="generative",
        content_plan_item_id=None,
        content_plan_ownership_epoch=None,
    )
    session = _GraphSession(job=job)

    entry = gb._lock_owned_entry_job(session, str(job.id))

    assert entry == (job, None)
    assert session.get_calls == [
        (Job, job.id, False, False),
        (Job, job.id, True, True),
    ]


def test_all_locked_gets_carry_populate_existing() -> None:
    """Every ``with_for_update=True`` call on ``Job``/``PlanItem`` -- the two
    models this function also reads unlocked earlier in the same session --
    must also pass ``populate_existing=True``, or the lock hands back the
    stale cached row instead of the freshly-locked one (#813/#845). Covers
    both the content-plan graph path and the public non-plan-job bypass.

    ``ContentPlan`` is deliberately excluded: its locked read is this
    function's FIRST read of that model in the session, so there is nothing
    stale to refresh.
    """
    plan_job, _item, _plan, plan_session = _plan_job_graph(live_epoch=3, bound_epoch=3)
    with patch(
        "app.services.content_plan_persona.load_owned_plan_persona_sync",
        return_value=SimpleNamespace(),
    ):
        gb._lock_owned_entry_job(plan_session, str(plan_job.id))

    public_job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="queued",
        mode="generative",
        content_plan_item_id=None,
        content_plan_ownership_epoch=None,
    )
    public_session = _GraphSession(job=public_job)
    gb._lock_owned_entry_job(public_session, str(public_job.id))

    for session in (plan_session, public_session):
        locked_calls = [
            call for call in session.get_calls if call[2] and call[0] in (Job, PlanItem)
        ]
        assert locked_calls, "expected at least one Job/PlanItem with_for_update=True call"
        for model, key, _with_for_update, populate_existing in locked_calls:
            assert populate_existing, (
                f"{model.__name__}[{key}] was locked without populate_existing -- "
                "it will return the stale pre-lock cached row if this session "
                "already read that primary key unlocked earlier (#813/#845)"
            )


def test_gate_depends_on_populate_existing_refreshed_value_not_stale_cache() -> None:
    """Simulates SQLAlchemy's real staleness rule directly: a locked re-read
    of an already-cached PK returns the STALE cached object unless
    ``populate_existing=True`` is also passed. Proves the ownership gate
    (``item.current_job_id != job.id``) is decided by the FRESH, post-lock
    row -- not whatever ``job_ref``/``item_ref`` looked like before the lock.

    This reproduces the #813 shape: a concurrent writer binds
    ``item.current_job_id`` to this job between the unlocked pre-read and the
    lock. Fails if ``populate_existing`` is ever dropped from the locked
    ``PlanItem``/``Job`` reads in ``_lock_owned_entry_job``.
    """
    user_id = uuid.uuid4()
    job_id = uuid.uuid4()
    item_id = uuid.uuid4()
    plan_id = uuid.uuid4()

    plan = SimpleNamespace(
        id=plan_id, user_id=user_id, ownership_epoch=2, ownership_quarantined_at=None
    )
    # Seed: what the UNLOCKED pre-reads (job_ref/item_ref) see -- a concurrent
    # writer hasn't finished binding the item to this job yet.
    job_seed = SimpleNamespace(
        id=job_id,
        user_id=user_id,
        status="queued",
        mode="content_plan",
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=None,
    )
    item_seed = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=None)
    # Fresh: what the DB actually holds once the lock is acquired -- the
    # concurrent writer finished binding item -> job first.
    job_fresh = SimpleNamespace(
        id=job_id,
        user_id=user_id,
        status="queued",
        mode="content_plan",
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=2,
    )
    item_fresh = SimpleNamespace(id=item_id, content_plan_id=plan_id, current_job_id=job_id)

    session = _StaleAwareSession(
        plan=plan,
        job_seed=job_seed,
        job_fresh=job_fresh,
        item_seed=item_seed,
        item_fresh=item_fresh,
    )

    with patch(
        "app.services.content_plan_persona.load_owned_plan_persona_sync",
        return_value=SimpleNamespace(),
    ):
        entry = gb._lock_owned_entry_job(session, str(job_id))

    # Correct behavior reads the FRESH, post-lock binding and accepts. Without
    # populate_existing on the locked re-reads, item/job stay at their stale
    # seed values (current_job_id=None), the ownership gate rejects, and this
    # assertion fails.
    assert entry == (job_fresh, 2)


def test_wrapped_tasks_do_not_leak_content_plan_epoch_between_calls() -> None:
    plan_job = SimpleNamespace(id=uuid.uuid4(), status="queued")
    public_job = SimpleNamespace(id=uuid.uuid4(), status="queued")
    entries = iter([(plan_job, 11), (public_job, None)])
    observed: list[tuple[str, tuple[str, int] | None]] = []

    @gb._with_owned_job_fence
    def _probe(_self, job_id: str) -> None:  # noqa: ANN001
        observed.append((job_id, gb._CONTENT_PLAN_FENCE.get()))

    @contextmanager
    def _session():
        yield SimpleNamespace()

    outer_token = gb._CONTENT_PLAN_FENCE.set(("outer-task", 99))
    try:
        with (
            patch.object(gb, "_sync_session", _session),
            patch.object(gb, "_lock_owned_entry_job", side_effect=lambda *_args: next(entries)),
        ):
            _probe(None, str(plan_job.id))
            assert gb._CONTENT_PLAN_FENCE.get() == ("outer-task", 99)
            _probe(None, str(public_job.id))
            assert gb._CONTENT_PLAN_FENCE.get() == ("outer-task", 99)
    finally:
        gb._CONTENT_PLAN_FENCE.reset(outer_token)

    assert observed == [
        (str(plan_job.id), (str(plan_job.id), 11)),
        (str(public_job.id), None),
    ]


@pytest.mark.parametrize("epoch", [None, -1, True, 1.5, "1"])
def test_content_plan_builder_rejects_missing_malformed_or_negative_epoch(
    epoch: object,
) -> None:
    with pytest.raises(ValueError, match="non-negative ownership epoch"):
        build_generative_job(
            user_id=uuid.uuid4(),
            clip_paths=["users/u/plan/i/a.mp4"],
            mode="content_plan",
            content_plan_item_id=uuid.uuid4(),
            content_plan_ownership_epoch=epoch,
        )


def test_content_plan_builder_rejects_missing_plan_item() -> None:
    with pytest.raises(ValueError, match="requires a plan item"):
        build_generative_job(
            user_id=uuid.uuid4(),
            clip_paths=["users/u/plan/i/a.mp4"],
            mode="content_plan",
            content_plan_ownership_epoch=0,
        )
