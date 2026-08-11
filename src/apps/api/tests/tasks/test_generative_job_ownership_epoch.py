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
        self.get_calls: list[tuple[type, object, bool]] = []

    def get(self, model, key, **kwargs):  # noqa: ANN001, ANN201
        self.get_calls.append((model, key, bool(kwargs.get("with_for_update"))))
        if model is Job and key == self.job.id:
            return self.job
        if model is PlanItem and self.item is not None and key == self.item.id:
            return self.item
        if model is ContentPlan and self.plan is not None and key == self.plan.id:
            return self.plan
        return None


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
        (Job, job.id, False),
        (Job, job.id, True),
    ]


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
