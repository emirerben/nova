"""KRI-720: a phone Talking edit of several takes never promises a cut it cannot make.

Prod 2026-11-08 (TestFlight 1929, Fly 9d4ba22b5, thread cc0e3706, job 7416c94b, S3 "Yorum2"):
three talking takes, "Cut the long pauses, the 'okay' and the restart." The draft said "Will do
at render: ... If Clean up speech is offered when you approve, choose it ...; I'll cut the line
you named ...; Clean up speech also cuts a sentence you restarted ...". Approval offered no
choice, the job ran `off_v1`, and the video kept "Wait, the light, okay" and "oh sorry, again".

Ways the fix can fail, each pinned below:

 1. the draft still reads as a render promise, so readiness approves it unseen (the bug);
 2. the named line or restart promise survives next to an honest cleanup line;
 3. the single-take draft loses its promise (over-fix);
 4. the finished video blames missing word timings for a line the render never looked for;
 5. the readiness check (no `speech_cleanup_offered`) and the draft turn disagree.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria import brief_checks as bc
from app.kria.approval_readiness import _promised_at_render
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, check_requirement

# The approved brief's r2, verbatim from job 7416c94b's `creator_brief_binding`.
R2 = BriefRequirement(
    id="r2",
    kind="style",
    scope="global",
    description="Cut the long pauses, the 'okay' and the restart.",
    facts={"cut_lines": ["okay"], "cut_restarts": True, "speech_cleanup": True},
)
PROMISES = (bc._CLEANUP_IF_OFFERED, bc._NAMED_CUT_AT_RENDER, bc._RESTART_CUT_AT_APPROVAL)


@pytest.fixture(autouse=True)
def _prod(monkeypatch: pytest.MonkeyPatch) -> None:
    # Retake detection was on in prod: the draft promised the restart cut.
    monkeypatch.setattr(settings, "retake_cut_enabled", True)
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", True)


def _draft(takes: int, offered: bool | None = None) -> PlanFacts:
    return PlanFacts(
        edit_format="subtitled",
        phone_talking=True,
        video_clip_count=takes,
        speech_cleanup_enabled=False,
        speech_cleanup_offered=offered,
    )


@pytest.mark.parametrize("offered", [None, False])
def test_s3_three_takes_draft_promises_nothing(offered):
    facts = _draft(3, offered)
    receipt = check_requirement(R2, facts)
    assert receipt.status == "partial"
    assert not bc._planned_at_render(receipt), receipt.full_reason
    assert not _promised_at_render(R2, receipt, facts), receipt.full_reason
    for promise in PROMISES:
        assert promise not in receipt.full_reason, promise
    assert bc.is_format_limit(receipt.full_reason)


def test_one_take_draft_keeps_its_promise():
    receipt = check_requirement(R2, _draft(1))
    assert bc._planned_at_render(receipt), receipt.full_reason


def test_finished_video_says_several_takes_not_missing_timings():
    facts = PlanFacts(
        edit_format="subtitled",
        phone_talking=True,
        video_clip_count=3,
        rendered_variant=True,
        speech_cleanup_outcome="not_run",
        named_cuts=[
            {
                "version": 1,
                "quote": "okay",
                "requirement_ids": ["r2"],
                "status": "not_applied",
                "reason": "several_takes",
            }
        ],
    )
    reason = check_requirement(R2, facts).full_reason
    assert "no word timings" not in reason
    assert "several takes" in reason


def test_worker_reports_several_takes_for_a_multi_take_miss():
    import uuid

    from app.kria.brief import CreativeBrief
    from app.kria.brief_binding import BriefBinding
    from app.tasks.generative_build import _phone_opening_cut

    binding = BriefBinding.create(uuid.uuid4(), CreativeBrief(version=1, requirements=[R2]))
    _plan, receipts = _phone_opening_cut(
        {"creator_brief_binding": binding.model_dump(mode="json")},
        None,
        (),
        duration_s=20.1,
        off_words=None,
        multiple_takes=True,
    )
    assert receipts is not None
    assert [(r["status"], r["reason"]) for r in receipts["named_cuts"]] == [
        ("not_applied", "several_takes")
    ]


# --- KRI-720 with several-take cleanup ON ----------------------------------------------
#
# 6. the honest "can't cut anything" line survives once every take IS cut (under-promise);
# 7. the named "okay" is promised although no line is cut from several takes (over-promise);
# 8. the pause cut is promised before approval offers the choice ("Done" too early).


@pytest.fixture
def _multi_take_cleanup_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.phone_rollout as rollout

    monkeypatch.setattr(rollout, "phone_talking_multi_take_cleanup_supported", lambda: True)


@pytest.mark.usefixtures("_multi_take_cleanup_on")
def test_s3_with_cleanup_on_offers_the_pause_cut_but_never_the_named_line():
    receipt = check_requirement(R2, _draft(3, offered=True))
    reason = receipt.full_reason
    assert bc._CLEANUP_SEVERAL_TAKES not in reason
    assert bc._CLEANUP_AT_APPROVAL in reason
    assert bc._NAMED_CUTS_SEVERAL_TAKES in reason
    assert bc._NAMED_CUT_AT_RENDER not in reason
    assert receipt.status == "partial"


@pytest.mark.usefixtures("_multi_take_cleanup_on")
def test_s3_with_cleanup_chosen_plans_the_pause_cut():
    facts = PlanFacts(
        edit_format="subtitled",
        phone_talking=True,
        video_clip_count=3,
        speech_cleanup_enabled=True,
        speech_cleanup_offered=True,
    )
    reason = check_requirement(R2, facts).full_reason
    assert bc._CLEANUP_PLANNED in reason and bc._NAMED_CUTS_SEVERAL_TAKES in reason
