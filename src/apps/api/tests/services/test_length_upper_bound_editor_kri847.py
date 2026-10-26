"""KRI-847: an editor Save keeps a ceiling a ceiling, and a timeline Save makes it exact.

A Save rebinds the contract (a new digest) and re-stamps the length policy for it. Failure
modes written first:

1. A text Save re-stamps the state as exact, so the next render check calls a correct 7.5 s
   edit a miss of "10 s +/- 10 %" again.
2. A timeline Save, which sets the edit's own new length as the target, keeps "under 10" on
   that new number.
"""

from __future__ import annotations

import pytest

from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContractError,
    build_render_contract,
    read_render_contract,
)
from app.services.device_render import CONTRACT_REVISIONS_FIELD
from app.services.length_policy_state import (
    check_creator_duration,
    read_length_policy,
    stamp_length_policy,
)
from tests.routes.test_phone_editor_commit import _enable_guided_v2, phone_job, save

# The fixture job's recipe is 3 s long, so an exact 3 s ask passes the Save's own check too.
STRATEGY = {
    "edit_format": "montage",
    "audio_strategy": "original_audio",
    "creator_duration_s": 3.0,
}


def _ceiling_job(monkeypatch, bound: str):
    job = phone_job(monkeypatch)
    contract = build_render_contract(STRATEGY, generation_id="first")
    assert contract is not None and contract.duration_s == 3
    job.all_candidates = {"creator_strategy": STRATEGY}
    job.assembly_plan = stamp_length_policy(
        {**job.assembly_plan, CONTRACT_FIELD: contract.model_dump(mode="json")},
        STRATEGY,
        contract,
        mode="enforce",
        duration_bound=bound,
    )
    return job, contract


def _revised(job):
    return read_render_contract(
        {CONTRACT_FIELD: job.assembly_plan[CONTRACT_REVISIONS_FIELD]["guided_story"]}
    )


@pytest.mark.parametrize("bound", ["at_most", "exact"])
def test_a_text_save_keeps_the_bound_of_the_length_it_did_not_touch(monkeypatch, bound):
    job, contract = _ceiling_job(monkeypatch, bound)

    save(job, text="After", start_s=0, end_s=2)

    revised = _revised(job)
    assert revised.digest != contract.digest and revised.duration_s == 3
    state = read_length_policy(job.assembly_plan, revised)
    assert state.requested_duration_bound == bound
    if bound == "at_most":
        assert check_creator_duration(revised, 2.5, state=state)["outcome"] == "passed"
        # 3.2 s is inside 3 s + 10 %: met, exactly as for an exact length.
        assert check_creator_duration(revised, 3.2, state=state)["outcome"] == "passed"
        with pytest.raises(CreatorRenderContractError, match="at most 3 seconds"):
            check_creator_duration(revised, 3.4, state=state)
    # The approved contract's own state is untouched.
    assert read_length_policy(job.assembly_plan, contract).requested_duration_bound == bound


def test_a_timeline_save_names_a_concrete_new_length_so_the_ceiling_ends(monkeypatch):
    from app.routes import generative_jobs as gj

    job = phone_job(monkeypatch)
    _enable_guided_v2(job, monkeypatch)
    strategy = STRATEGY
    contract = build_render_contract(strategy, generation_id="first")
    job.all_candidates = {"creator_strategy": strategy}
    job.assembly_plan = stamp_length_policy(
        {**job.assembly_plan, CONTRACT_FIELD: contract.model_dump(mode="json")},
        strategy,
        contract,
        mode="enforce",
        duration_bound="at_most",
    )
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    segment = revision["segments"][0]

    gj.prepare_editor_commit(
        job,
        "guided_story",
        gj.EditorCommitRequest(
            base_generation="first",
            guided_revision_number=revision["revision_number"],
            timeline_slots=[
                gj.TimelineSlotEdit(
                    slot_id=segment["segment_id"], clip_index=0, in_s=2.2, duration_s=0.8
                )
            ],
        ),
    )

    revised = _revised(job)
    assert revised.duration_s == pytest.approx(0.8)
    # The trimmed edit is 0.8 s on purpose: an exact target, not "under 0.8 s".
    assert read_length_policy(job.assembly_plan, revised).requested_duration_bound == "exact"
    assert read_length_policy(job.assembly_plan, contract).requested_duration_bound == "at_most"
