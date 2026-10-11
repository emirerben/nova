from types import SimpleNamespace

import pytest

from app.services.contract_check_rollout import GenerationCheckRollout, run_contract_check
from app.services.creator_render_contract import CreatorRenderContractError
from app.services.length_policy_state import (
    DURATION_CHECK,
    check_creator_duration,
    read_length_policy,
    stamp_length_policy,
)

STRATEGY = {"edit_format": "montage", "audio_strategy": "licensed_music"}


def contract(*, generation="g1", digest="d1", duration=10.0):
    return SimpleNamespace(generation_id=generation, digest=digest, duration_s=duration)


def test_log_only_duration_mismatch_returns_a_receipt() -> None:
    value = stamp_length_policy({}, STRATEGY, contract(), mode="log_only")
    state = read_length_policy(value, contract())
    receipt = check_creator_duration(contract(), 12.0, state=state)
    assert receipt == {
        "check": DURATION_CHECK,
        "generation_id": "g1",
        "contract_digest": "d1",
        "length_owner": "creator",
        "requested_duration_s": 10.0,
        "actual_duration_s": 12.0,
        "mode": "log_only",
        "outcome": "declined",
        "blocking": False,
    }


def test_enforce_rejects_more_than_ten_percent_drift() -> None:
    value = stamp_length_policy({}, STRATEGY, contract(), mode="enforce")
    with pytest.raises(CreatorRenderContractError, match="asked for 10 seconds"):
        check_creator_duration(contract(), 11.01, state=read_length_policy(value, contract()))
    assert (
        check_creator_duration(contract(), 11.0, state=read_length_policy(value, contract()))[
            "outcome"
        ]
        == "passed"
    )


def test_derived_noncreator_duration_never_becomes_a_hard_requirement() -> None:
    derived = contract(duration=None)
    value = stamp_length_policy({}, STRATEGY, derived, mode="enforce")
    state = read_length_policy(value, derived)
    assert state.owner != "creator"
    assert check_creator_duration(derived, 999.0, state=state) is None


@pytest.mark.parametrize("changed", [contract(generation="g2"), contract(digest="d2")])
def test_stale_generation_or_digest_is_rejected(changed) -> None:
    value = stamp_length_policy({}, STRATEGY, contract(), mode="log_only")
    with pytest.raises(
        CreatorRenderContractError, match="approved length policy|different approved revision"
    ):
        read_length_policy(value, changed)


def test_rebound_generation_replaces_state_but_retains_rollout_mode() -> None:
    assembly = stamp_length_policy({}, STRATEGY, contract(), mode="log_only")
    rebound = contract(generation="editor-g2", digest="editor-d2")
    rebound_assembly = stamp_length_policy(assembly, STRATEGY, rebound, mode="log_only")
    state = read_length_policy(rebound_assembly, rebound)
    assert state.generation_id == "editor-g2"
    assert state.check_mode == "log_only"
    assert read_length_policy(rebound_assembly, contract()).generation_id == "g1"


def test_legacy_contract_without_policy_retains_existing_enforcement() -> None:
    assert read_length_policy({}, contract()) is None
    with pytest.raises(CreatorRenderContractError, match="asked for 10 seconds"):
        check_creator_duration(contract(), 11.01, state=None)


def test_unexpected_noncontract_exception_propagates() -> None:
    rollout = GenerationCheckRollout("g1", True, {DURATION_CHECK: "log_only"})
    with pytest.raises(RuntimeError, match="programming error"):
        run_contract_check(
            DURATION_CHECK,
            lambda: (_ for _ in ()).throw(RuntimeError("programming error")),
            rollout=rollout,
            decline_exceptions=CreatorRenderContractError,
        )
