"""KRI-847: the approval dispatch stamps "under N seconds" as a ceiling on the job.

Through the REAL dispatch (``_dispatch_item_render`` over the REAL ``build_generative_job``),
the harness of KRI-788. Failure modes written first:

1. The dispatch pins the brief's 35 s but never records that it is a ceiling, so the render
   check later declines a correct 20 s edit ("You asked for 35 seconds; this edit is 20").
2. An exact ask is stamped as a ceiling (a brief with no ``duration_bound``).
3. The job keys an older worker reads change for an exact job.
"""

from __future__ import annotations

import uuid

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding, snapshot_media
from app.services.creator_render_contract import CreatorRenderContractError, read_render_contract
from app.services.length_policy_state import (
    LENGTH_BOUND_FIELD,
    check_creator_duration,
    read_length_policy,
)
from tests.tasks.test_kri788_order_answer_dispatch import (
    REQUEST,
    T1_STRATEGY,
    _dispatch,
    _item,
)


def _binding(item, **facts) -> dict:
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="At most 35 seconds",
                facts={"duration_s": 35, **facts},
            )
        ],
    )
    return BriefBinding.create(
        uuid.uuid4(),
        brief,
        latest_message="At most 35 seconds",
        full_creator_request=REQUEST,
        media_snapshot=snapshot_media(item),
        choice_answers=None,
    ).model_dump(mode="json")


def _dispatched(monkeypatch, **facts):
    monkeypatch.setattr(settings, "creator_duration_check_mode", "enforce")
    item = _item()
    job = _dispatch(monkeypatch, item, T1_STRATEGY, _binding(item, **facts))
    contract = read_render_contract(job.assembly_plan)
    assert contract is not None and contract.duration_s == 35
    return job, contract, read_length_policy(job.assembly_plan, contract)


def test_a_ceiling_ask_is_stamped_as_a_ceiling_and_a_shorter_render_passes(monkeypatch):
    job, contract, state = _dispatched(monkeypatch, duration_bound="at_most")

    assert job.assembly_plan[LENGTH_BOUND_FIELD] == {contract.digest: "at_most"}
    assert state is not None and state.check_mode == "enforce"
    assert state.requested_duration_bound == "at_most"
    assert check_creator_duration(contract, 20.0, state=state)["outcome"] == "passed"
    # 38.5 s is 35 s + 10 %: met, exactly as for an exact length.
    assert check_creator_duration(contract, 38.5, state=state)["outcome"] == "passed"
    with pytest.raises(CreatorRenderContractError, match="at most 35 seconds"):
        check_creator_duration(contract, 39.0, state=state)


def test_guard_an_exact_ask_is_stamped_exactly_as_before(monkeypatch):
    job, contract, state = _dispatched(monkeypatch)

    assert LENGTH_BOUND_FIELD not in job.assembly_plan
    assert state is not None and state.requested_duration_bound == "exact"
    with pytest.raises(CreatorRenderContractError, match="asked for 35 seconds"):
        check_creator_duration(contract, 20.0, state=state)
    assert check_creator_duration(contract, 38.5, state=state)["outcome"] == "passed"
