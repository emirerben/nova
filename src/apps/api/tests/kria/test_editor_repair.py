"""Failure matrix: authority, unrelated operations, preservation and exact retention."""

from dataclasses import replace

import pytest

from app.kria.brief import BriefUpdate
from app.kria.editor_repair import candidate_operations
from app.kria.planner import PlannedKriaTurn, adapt_editor_action


def planned(ops, updates=None):
    return PlannedKriaTurn(
        plan=adapt_editor_action(reply="Timing", ops=ops, request_render=False),
        manifest_hash=None,
        context_hash=None,
        brief_updates=tuple(
            updates
            or [
                BriefUpdate(
                    kind="timing",
                    scope="global",
                    description="Timing only",
                    facts={"editor_timing_only": True},
                )
            ]
        ),
    )


RETIME = {
    "op": "retime_text_sequence",
    "source_id": "opening",
    "target_ids": ["opening::sequence-1"],
    "start_s": 0,
    "end_s": 12,
    "gap_policy": "close",
}
DURATION = {"op": "set_total_duration", "target_s": 12, "strategy": "proportional"}
BAD = {"op": "add_text", "text": "Replacement", "start_s": 0, "end_s": 1}


def test_retains_exact_existing_timing_operations():
    assert candidate_operations(planned([RETIME, BAD, DURATION])) == [RETIME, DURATION]


@pytest.mark.parametrize(
    "ops",
    [
        [DURATION, BAD],
        [RETIME],
        [RETIME, BAD, {"op": "set_music", "asset_id": "x"}],
        [RETIME, RETIME, BAD],
    ],
)
def test_no_new_authority_or_unrelated_removals(ops):
    assert candidate_operations(planned(ops)) is None


def test_mixed_requirement_or_preservation_denies():
    base = planned([RETIME, BAD])
    assert (
        candidate_operations(
            replace(
                base,
                brief_updates=base.brief_updates
                + (BriefUpdate(kind="text", scope="title", literal="Replacement"),),
            )
        )
        is None
    )
    assert (
        candidate_operations(
            planned(
                [RETIME, BAD],
                [
                    BriefUpdate(
                        kind="timing",
                        scope="global",
                        description="Timing only",
                        facts={"editor_timing_only": True, "preserve_editor_text": True},
                    )
                ],
            )
        )
        is None
    )


def test_enabled_without_explicit_run_capability_does_not_review(monkeypatch):
    import time
    import uuid
    from unittest.mock import Mock

    from app.config import settings
    from app.kria.editor_repair import with_editor_review

    for name in (
        "creator_repair_reviewer_enabled",
        "credit_quality_review_enabled",
        "credit_quality_review_billing_verified",
        "ai_cost_control_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    session = Mock(side_effect=AssertionError("No capability must not read or review"))
    monkeypatch.setattr("app.kria.editor_repair.sync_session", session)
    original = planned([RETIME, BAD])
    assert (
        with_editor_review(original, turn_id=uuid.uuid4(), deadline=time.monotonic() + 20)
        is original
    )
    session.assert_not_called()


@pytest.mark.parametrize(
    "changed", ["variant", "head", "client", "brief", "operations", "authority"]
)
def test_locked_fence_refuses_changed_basis(changed):
    from types import SimpleNamespace

    from app.kria.brief import CreativeBrief
    from app.kria.editor_repair import (
        PreparedEditorRepair,
        _authority,
        _json,
        basis_digest,
        verify_editor_repair,
    )
    from app.services.kria_editor_ops import KriaEditorOpError

    original = planned([RETIME, BAD])
    intent = original.plan.intents[0].model_copy(update={"arguments": {"operations": [RETIME]}})
    accepted = replace(original, plan=original.plan.model_copy(update={"intents": [intent]}))
    values = dict(
        job=SimpleNamespace(id="job"),
        variant={"generation_id": "one"},
        head=None,
        client_state=None,
        projected={"text_elements": []},
        brief=None,
    )
    accepted = replace(
        accepted,
        editor_repair=PreparedEditorRepair(
            basis_digest(**values), "id", _json([RETIME]), _authority(accepted)
        ),
    )
    verify_editor_repair(accepted, **values)
    if changed == "variant":
        values["variant"] = {"generation_id": "two"}
    elif changed == "head":
        values["head"] = SimpleNamespace(
            id="head", draft_revision=2, snapshot_json={}, base_generation_id="one"
        )
    elif changed == "client":
        values["client_state"] = {"changed": True}
    elif changed == "brief":
        values["brief"] = CreativeBrief(version=2)
    elif changed == "operations":
        changed_intent = intent.model_copy(update={"arguments": {"operations": [DURATION]}})
        accepted = replace(
            accepted, plan=accepted.plan.model_copy(update={"intents": [changed_intent]})
        )
    else:
        accepted = replace(accepted, brief_updates=())
    with pytest.raises(KriaEditorOpError, match="draft changed"):
        verify_editor_repair(accepted, **values)


def test_completion_reserve_never_starts_review(monkeypatch):
    import time
    import uuid
    from unittest.mock import Mock

    from app.agents._runtime import RunContext
    from app.config import settings
    from app.kria.editor_repair import with_editor_review

    monkeypatch.setattr(settings, "creator_repair_reviewer_enabled", True)
    original = replace(planned([RETIME, BAD]), editor_repair_run_context=RunContext())
    session = Mock(side_effect=AssertionError("Completion reserve must not start review"))
    monkeypatch.setattr("app.kria.editor_repair.sync_session", session)
    assert (
        with_editor_review(original, turn_id=uuid.uuid4(), deadline=time.monotonic() + 1)
        is original
    )
    session.assert_not_called()


def test_explicit_word_hold_is_never_discarded():
    ops = [RETIME, {"op": "patch_text", "patch": {"start_s": 0, "end_s": 4}}, BAD]
    assert candidate_operations(planned(ops)) is None
