"""KRI-881 rolling deploy, end to end: drafted by a KRI-881 worker, approved and run here.

The Merope T1 chat through the real runtime (``submit_turn`` + ``run_kria_turn`` over scratch
Postgres, as in ``test_kri787_merope_t1_runtime_postgres``). The draft turn's binding writer
is KRI-881's: ``BriefBinding.create`` is given the ``prepared_research`` a KRI-881 worker
pins (the fixture written by that code, ``test_kri881_reader_tolerance``). Everything after
the draft is this code: the approval decision, the prepared-execution snapshot, the
dispatch claim, the real dispatch and the worker's first gate. Failure mode 6 of
``test_kri881_reader_tolerance``: the chat must reach the worker with the research intact.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from app.database import engine as async_engine
from app.kria.brief_binding import BriefBinding
from tests.kria.test_kri787_merope_t1_replay import RENDERS, dispatch, run_worker
from tests.kria.test_kri787_merope_t1_runtime_postgres import (
    _approve,
    _chat_until_approval,
    _flags,  # noqa: F401 - the Merope prod flags, autouse
)
from tests.kria.test_kri881_reader_tolerance import FIXTURE


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


def _kri881_writer(monkeypatch: pytest.MonkeyPatch) -> None:
    """The draft turn mints its binding the way a KRI-881 worker does, research pinned."""
    original = BriefBinding.create.__func__

    def create(cls, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        # KRI-881's draft turn passes `prepared_research=None` when nothing was researched.
        if kwargs.get("prepared_research") is None:
            kwargs["prepared_research"] = FIXTURE["prepared_research"]
        return original(cls, *args, **kwargs)

    monkeypatch.setattr(BriefBinding, "create", classmethod(create))


@pytest.mark.asyncio
async def test_a_kri881_draft_is_approved_dispatched_and_run_here(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with monkeypatch.context() as writer:
        _kri881_writer(writer)
        chat = await _chat_until_approval(monkeypatch)
    claim = await _approve(chat)

    binding = BriefBinding.model_validate(claim.brief_binding)
    assert binding.prepared_research == FIXTURE["prepared_research"]
    assert claim.brief_binding["prepared_research"] == FIXTURE["prepared_research"]

    job = dispatch(monkeypatch, RENDERS[0], strategy=claim.strategy, binding=claim.brief_binding)
    pinned = job.assembly_plan["creator_brief_binding"]
    assert pinned["prepared_research"] == FIXTURE["prepared_research"]
    assert BriefBinding.model_validate(pinned).digest == binding.digest

    outcome = run_worker(monkeypatch, job)
    assert (outcome.failure_reason, outcome.message) == (None, None)
    assert outcome.route == "subtitled"
