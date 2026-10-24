"""KRI-847: the render checks carry a model-recorded "at most N seconds" as a ceiling.

"at_most" only removes the LOWER side of the existing +/- 10 % check (finite and
0 < actual <= N * 1.10), so it can relax what exact already allowed and never tighten it. The
merged KRI-629 strict cap owns exclusive wording ("under N") and is untouched.

Failure modes written down first:

1. The receipt passes a 7.5 s draft, but the render check still says "10 s +/- 10 %": a job in
   ``enforce`` mode declines the 7.5 s render with "You asked for 10 seconds; this edit is
   7.5 seconds.".
2. The ceiling is stricter than exact (a grace constant instead of the exact upper tolerance),
   declining a render exact would have passed.
3. The bound lands in ``CreatorRenderContract`` (or in the stored length-policy payload):
   that model forbids extra keys, so a still-running older worker cannot read the job
   (``test_the_contract_model_schema_is_unchanged_so_old_workers_can_still_read_it``). The
   bound rides in a sibling job key instead, and an exact job's keys never change.
4. A re-stamp after an editor save (the contract is rebound) forgets the ceiling, or a
   timeline save that sets a concrete new length keeps it.
5. The decline for an over-ceiling render says "you asked for 10 seconds", the exact wording.
"""

from __future__ import annotations

import json

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.cloud_render_contract import CloudRenderContractError, verify_cloud_variant
from app.services.creator_render_contract import (
    CONTRACT_FIELD,
    CreatorRenderContract,
    CreatorRenderContractError,
    build_render_contract,
    contract_duration_bound,
    verify_phone_recipe,
)
from app.services.length_policy_state import (
    LENGTH_BOUND_FIELD,
    LENGTH_POLICIES_FIELD,
    LENGTH_POLICY_FIELD,
    check_creator_duration,
    read_length_policy,
    stamp_length_policy,
)

STRATEGY = {"edit_format": "talking_head", "audio_strategy": "original_audio"}


def _brief(**facts) -> CreativeBrief:
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="Under 10 seconds",
                facts={"duration_s": 10, **facts},
            )
        ],
    )


def _speech_recipe():
    from tests.services.test_creator_render_contract import _speech_recipe

    return _speech_recipe()  # a 7 s Talking recipe


def _stamped(contract, bound, mode="enforce"):
    """What dispatch writes on the job, then what a checker reads back from the saved JSON."""
    saved = json.loads(
        json.dumps(stamp_length_policy({}, STRATEGY, contract, mode=mode, duration_bound=bound))
    )
    return saved, read_length_policy(saved, contract)


def _ten() -> CreatorRenderContract:
    return CreatorRenderContract(generation_id="g").rebind(duration_s=10)


# ------------------------------------------------------ which bound a built contract has


def test_a_brief_ceiling_makes_the_contracts_length_a_ceiling():
    brief = _brief(duration_bound="at_most")
    contract = build_render_contract(STRATEGY, generation_id="g", brief=brief)

    assert contract is not None and contract.duration_s == 10
    assert contract_duration_bound(contract, STRATEGY, brief) == "at_most"


def test_a_brief_length_without_the_fact_is_exact():
    brief = _brief()
    contract = build_render_contract(STRATEGY, generation_id="g", brief=brief)

    assert contract_duration_bound(contract, STRATEGY, brief) == "exact"


def test_no_length_or_no_brief_is_exact():
    brief = _brief(duration_bound="at_most")
    assert contract_duration_bound(None, STRATEGY, brief) == "exact"
    no_length = build_render_contract(STRATEGY, generation_id="g")
    assert contract_duration_bound(no_length, STRATEGY, None) == "exact"
    stamped_only = CreatorRenderContract(generation_id="g").rebind(duration_s=10)
    assert contract_duration_bound(stamped_only, STRATEGY, None) == "exact"


def test_a_contract_length_that_is_not_the_briefs_ceiling_is_exact():
    """An editor-set length (a trim) is a concrete number even beside an older ceiling."""
    brief = _brief(duration_bound="at_most")
    trimmed = CreatorRenderContract(generation_id="g").rebind(duration_s=7.5)

    assert contract_duration_bound(trimmed, STRATEGY, brief) == "exact"


# ------------------------------------------- the stored shapes do not change for older workers


def test_the_contract_model_has_no_bound_field():
    """Frozen schema (see the schema-pin test): the bound is never a contract field."""
    assert "duration_bound" not in CreatorRenderContract.model_fields
    assert "duration_bound" not in CreatorRenderContract(generation_id="g").model_dump()


def test_an_exact_job_stores_exactly_what_it_stored_before():
    contract = _ten()
    saved, _ = _stamped(contract, "exact")

    assert LENGTH_BOUND_FIELD not in saved
    assert set(saved) == {LENGTH_POLICY_FIELD, LENGTH_POLICIES_FIELD}
    assert set(saved[LENGTH_POLICY_FIELD]) == {
        "version",
        "generation_id",
        "contract_digest",
        "owner",
        "natural_owner",
        "requested_duration_s",
        "check_mode",
    }


def test_a_ceiling_job_keeps_the_policy_payload_and_adds_one_sibling_key():
    contract = _ten()
    exact_saved, _ = _stamped(contract, "exact")
    saved, state = _stamped(contract, "at_most")

    # The payload an older worker validates (extra keys forbidden) is the same as for exact.
    assert saved[LENGTH_POLICY_FIELD] == exact_saved[LENGTH_POLICY_FIELD]
    assert saved[LENGTH_POLICIES_FIELD] == exact_saved[LENGTH_POLICIES_FIELD]
    assert saved[LENGTH_BOUND_FIELD] == {contract.digest: "at_most"}
    assert state.requested_duration_bound == "at_most"


def test_reading_a_state_with_no_sibling_key_is_exact():
    _, state = _stamped(_ten(), "exact")

    assert state.requested_duration_bound == "exact"


def test_each_contract_digest_keeps_its_own_bound():
    """An editor rebind (a new digest) re-stamps; the earlier digest keeps its own bound."""
    first = _ten()
    saved = stamp_length_policy({}, STRATEGY, first, mode="enforce", duration_bound="at_most")
    second = first.rebind(generation_id="editor-2")
    saved = stamp_length_policy(saved, STRATEGY, second, mode="enforce", duration_bound="exact")

    assert read_length_policy(saved, first).requested_duration_bound == "at_most"
    assert read_length_policy(saved, second).requested_duration_bound == "exact"


# ---------------------------------------- the duration check (log-only and enforce)


def test_enforce_passes_a_draft_under_the_ceiling():
    contract = _ten()
    _, state = _stamped(contract, "at_most")

    observation = check_creator_duration(contract, 7.5, state=state)

    assert observation is not None
    assert observation["outcome"] == "passed"
    assert observation["requested_duration_bound"] == "at_most"


@pytest.mark.parametrize("actual", [10.0, 11.0])
def test_enforce_keeps_the_exact_upper_tolerance_for_a_ceiling(actual):
    """11.0 s is 10 s + 10 %: met for exact, so met for at_most (it only relaxes the low side)."""
    contract = _ten()
    _, state = _stamped(contract, "at_most")

    assert check_creator_duration(contract, actual, state=state)["outcome"] == "passed"


@pytest.mark.parametrize("actual", [11.5, 12.0, 25.0])
def test_enforce_declines_a_draft_over_the_ceiling_and_names_the_ceiling(actual):
    contract = _ten()
    _, state = _stamped(contract, "at_most")

    with pytest.raises(CreatorRenderContractError) as raised:
        check_creator_duration(contract, actual, state=state)

    assert str(raised.value).endswith(
        f"You asked for at most 10 seconds; this edit is {actual:g} seconds."
    )


def test_log_only_records_the_ceiling_miss_without_declining():
    contract = _ten()
    _, state = _stamped(contract, "at_most", mode="log_only")

    observation = check_creator_duration(contract, 11.5, state=state)

    assert observation["outcome"] == "declined"
    assert observation["blocking"] is False
    assert observation["requested_duration_bound"] == "at_most"


def test_guard_an_exact_job_keeps_its_ten_percent_target_and_its_wording():
    contract = _ten()
    _, state = _stamped(contract, "exact")

    with pytest.raises(CreatorRenderContractError) as raised:
        check_creator_duration(contract, 7.5, state=state)
    assert str(raised.value).endswith("You asked for 10 seconds; this edit is 7.5 seconds.")
    assert "at most" not in str(raised.value)
    observation = check_creator_duration(contract, 11.0, state=state)
    assert observation["outcome"] == "passed"
    assert "requested_duration_bound" not in observation  # byte-identical to before


def test_guard_a_job_with_no_state_is_judged_exactly_as_before():
    """Older jobs have no length-policy state: no ceiling can exist for them."""
    contract = _ten()

    assert check_creator_duration(contract, 11.0, state=None) is None
    with pytest.raises(CreatorRenderContractError, match="asked for 10 seconds"):
        check_creator_duration(contract, 7.5, state=None)


def test_the_duration_check_takes_no_grace_argument():
    import inspect

    assert "grace_s" not in inspect.signature(check_creator_duration).parameters


# ------------------------------- the phone recipe check, with the stamped state (enforce)


def test_a_seven_second_recipe_meets_a_ten_second_ceiling_in_enforce_mode():
    contract = _ten()
    _, state = _stamped(contract, "at_most")

    receipts = verify_phone_recipe(
        contract, _speech_recipe(), source_audio={"talk": True}, length_policy=state
    )

    assert any(row.get("outcome") == "passed" for row in receipts)


def test_guard_the_same_recipe_misses_an_exact_ten_seconds_in_enforce_mode():
    contract = _ten()
    _, state = _stamped(contract, "exact")

    with pytest.raises(CreatorRenderContractError, match="asked for 10 seconds"):
        verify_phone_recipe(
            contract, _speech_recipe(), source_audio={"talk": True}, length_policy=state
        )


@pytest.mark.parametrize("ceiling_s", [5.0, 6.0])
def test_a_recipe_over_the_ceiling_tolerance_is_refused(ceiling_s):
    """The 7 s recipe is past 5 s + 10 % and 6 s + 10 %."""
    contract = CreatorRenderContract(generation_id="g").rebind(duration_s=ceiling_s)
    _, state = _stamped(contract, "at_most")

    with pytest.raises(CreatorRenderContractError, match=f"at most {ceiling_s:g} seconds"):
        verify_phone_recipe(
            contract, _speech_recipe(), source_audio={"talk": True}, length_policy=state
        )


def test_a_recipe_inside_the_exact_upper_tolerance_meets_a_ceiling_as_it_meets_exact():
    """7 s against 6.8 s: exact passes (inside +10 %), so at_most passes; never stricter."""
    contract = CreatorRenderContract(generation_id="g").rebind(duration_s=6.8)
    for bound in ("exact", "at_most"):
        _, state = _stamped(contract, bound)
        assert verify_phone_recipe(
            contract, _speech_recipe(), source_audio={"talk": True}, length_policy=state
        )


# ----------------------------- measured cloud exports: the same check, the same tolerance
#
# The cloud compares the ffprobe'd export (`verify_cloud_variant`) through the same state-driven
# `check_creator_duration`: no separate grace, so nothing the exact path passed can be declined.

CLOUD_STRATEGY = {
    "target_duration_s": 10,
    "target_duration_requested": True,
    "creator_duration_s": 10,
}


def _cloud_assembly(bound: str, mode: str = "enforce") -> dict:
    contract = build_render_contract(CLOUD_STRATEGY, generation_id="generation-1")
    assert contract is not None
    assembly = {CONTRACT_FIELD: contract.model_dump(mode="json")}
    return json.loads(
        json.dumps(
            stamp_length_policy(assembly, CLOUD_STRATEGY, contract, mode=mode, duration_bound=bound)
        )
    )


def _cloud_variant(actual_s: float) -> dict:
    return {
        "ok": True,
        "render_status": "ready",
        "video_path": "jobs/x/output.mp4",
        "render_receipt": {"verified": True, "actual_duration_s": actual_s},
    }


@pytest.mark.parametrize("measured", [7.5, 10.0, 10.3, 11.0])
def test_a_measured_cloud_export_at_or_below_the_exact_tolerance_meets_the_ceiling(measured):
    assembly = _cloud_assembly("at_most")

    assert verify_cloud_variant(assembly, _cloud_variant(measured)) is not None


@pytest.mark.parametrize("measured", [11.5, 12.0, 25.0])
def test_a_measured_cloud_export_past_the_tolerance_declines_naming_the_ceiling(measured):
    assembly = _cloud_assembly("at_most")

    with pytest.raises(CloudRenderContractError) as raised:
        verify_cloud_variant(assembly, _cloud_variant(measured))

    assert f"You asked for at most 10 seconds; this edit is {measured:g} seconds." in str(
        raised.value
    )


def test_guard_an_exact_cloud_export_keeps_its_ten_percent_band():
    assembly = _cloud_assembly("exact")

    assert verify_cloud_variant(assembly, _cloud_variant(11.0)) is not None
    with pytest.raises(CloudRenderContractError, match="asked for 10 seconds"):
        verify_cloud_variant(assembly, _cloud_variant(7.5))
    with pytest.raises(CloudRenderContractError, match="asked for 10 seconds"):
        verify_cloud_variant(assembly, _cloud_variant(11.2))


def test_the_cloud_contract_module_is_untouched_by_this_ticket():
    """No grace plumbing: `cloud_render_contract` has no ceiling-specific code at all."""
    import inspect

    from app.services import cloud_render_contract

    source = inspect.getsource(cloud_render_contract)
    assert "GRACE" not in source and "grace" not in source
