"""KRI-720 G1: the draft and the finished-video review of a several-take tight cut tell the truth.

Failure modes (docs/reviews/kri-720-multi-take-cleanup.md T5, T6, T8, T9, T11), each pinned:

1. The draft of several takes says "only the long pauses" while the render cuts every take tight,
   or promises tight to a plain ask on ONE take (over-promise).
2. A plain ask gets no tight line on several takes (the creator never said "tight").
3. The finished video reads only the first take's report: a later take that stayed long, or a join
   that stayed long, is "Done".
4. A noisy / unmeasured take is summed away into a clean total.
5. The cleanup count ("N pauses cut") and the review note cover only the first take.
6. Flag off / one take: unchanged copy. Every new static line has its Turkish twin.
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria import brief_checks as bc
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, check_requirement, plan_facts_from_phone_variant
from app.kria.reply_language import reply_language_for

PLAIN = BriefRequirement(
    id="r1",
    kind="audio",
    scope="global",
    description="Cut the pauses.",
    facts={"speech_cleanup": True},
)
TIGHT = BriefRequirement(
    id="r1",
    kind="audio",
    scope="global",
    description="Cut out all my pauses so it's tight",
    facts={"speech_cleanup": True, "pauses": "tight"},
)


@pytest.fixture
def several_takes_on(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.phone_rollout as rollout

    monkeypatch.setattr(rollout, "phone_talking_multi_take_cleanup_supported", lambda: True)
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True, raising=False)


def _draft(takes: int, **over) -> PlanFacts:
    base = {
        "edit_format": "subtitled",
        "phone_talking": True,
        "video_clip_count": takes,
        "speech_cleanup_enabled": True,
        "speech_cleanup_offered": True,
    }
    base.update(over)
    return PlanFacts(**base)


@pytest.mark.usefixtures("several_takes_on")
@pytest.mark.parametrize("req", [PLAIN, TIGHT], ids=["plain", "tight"])
def test_draft_of_several_takes_promises_every_pause_tight(req):
    reason = check_requirement(req, _draft(3)).full_reason
    assert bc._TIGHT_PLANNED in reason
    assert bc._TIGHT_KIND_LONG_ONLY not in reason


@pytest.mark.usefixtures("several_takes_on")
def test_draft_of_several_takes_before_approval_says_to_choose_cleanup():
    reason = check_requirement(
        PLAIN, _draft(3, speech_cleanup_enabled=False, speech_cleanup_offered=True)
    ).full_reason
    assert bc._TIGHT_AT_APPROVAL in reason


@pytest.mark.usefixtures("several_takes_on")
def test_one_take_plain_draft_is_not_promised_tight():
    reason = check_requirement(PLAIN, _draft(1)).full_reason or ""
    assert bc._TIGHT_PLANNED not in reason and bc._TIGHT_AT_APPROVAL not in reason
    # One take and a tight ask is today's copy.
    assert bc._TIGHT_PLANNED in check_requirement(TIGHT, _draft(1)).full_reason


def test_tight_flag_off_keeps_todays_draft(monkeypatch):
    import app.services.phone_rollout as rollout

    monkeypatch.setattr(rollout, "phone_talking_multi_take_cleanup_supported", lambda: True)
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", False, raising=False)
    for req in (PLAIN, TIGHT):
        reason = check_requirement(req, _draft(3)).full_reason or ""
        assert bc._TIGHT_PLANNED not in reason and bc._TIGHT_KIND_LONG_ONLY not in reason


def test_several_takes_with_cleanup_off_still_say_so(monkeypatch):
    import app.services.phone_rollout as rollout

    monkeypatch.setattr(rollout, "phone_talking_multi_take_cleanup_supported", lambda: False)
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True, raising=False)
    reason = check_requirement(PLAIN, _draft(3, speech_cleanup_enabled=False)).full_reason
    assert bc._CLEANUP_SEVERAL_TAKES in reason and bc._TIGHT_PLANNED not in reason


# --- finished renders --------------------------------------------------------------------


def _report(**over) -> dict:
    r = {
        "version": 1,
        "status": "applied",
        "target_s": 0.3,
        "limit_s": 0.35,
        "added_cuts": 3,
        "added_removed_ms": 700,
        "longest_pause_s": 0.3,
        "longest_pause_before_s": 0.9,
        "pauses_over_limit": 0,
        "guard_dropped": 0,
        "guard_trimmed": 0,
        "capped": False,
        "reason": None,
    }
    r.update(over)
    return r


def _variant(first=None, others=None, gaps=None, **extra) -> dict:
    v = {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "ok": True,
        "render_status": "ready",
        "caption_cues": [{"start_s": 0.0, "end_s": 1.0, "text": "hi"}],
        "silence_cut_outcome": "applied",
        "silence_cut": {"removed": [{"reason": "silence", "start_s": 1.0, "end_s": 1.8}]},
        "tight_pauses": first or _report(),
        "tight_pauses_other_takes": others
        if others is not None
        else [
            {"take": 1, **_report()},
            {"take": 2, **_report()},
        ],
        "take_join_gaps": gaps
        if gaps is not None
        else [
            {"after_take": 0, "gap_s": 0.26},
            {"after_take": 1, "gap_s": 0.27},
        ],
    }
    v.update(extra)
    return v


def _review(variant, req=PLAIN):
    return check_requirement(req, plan_facts_from_phone_variant(variant))


@pytest.mark.usefixtures("several_takes_on")
@pytest.mark.parametrize("req", [PLAIN, TIGHT], ids=["plain", "tight"])
def test_every_take_and_join_clean_is_done(req):
    receipt = _review(_variant(), req)
    assert (receipt.status, receipt.reason) == ("met", None)


@pytest.mark.usefixtures("several_takes_on")
def test_a_later_take_that_stayed_long_is_counted_with_the_longest():
    others = [
        {"take": 1, **_report(pauses_over_limit=2, longest_pause_s=0.52)},
        {"take": 2, **_report()},
    ]
    receipt = _review(_variant(others=others))
    assert receipt.status == "partial"
    assert receipt.reason == (
        "Speech cleanup cut your pauses tight, but 2 still last longer than 0.35 s "
        "(the longest is 0.5 s)"
    )


@pytest.mark.usefixtures("several_takes_on")
def test_a_join_that_stayed_long_counts_and_sets_the_longest():
    gaps = [{"after_take": 0, "gap_s": 0.26}, {"after_take": 1, "gap_s": 0.81}]
    receipt = _review(_variant(gaps=gaps))
    assert receipt.status == "partial"
    assert "1 still lasts longer than 0.35 s" in receipt.reason
    assert "the longest is 0.8 s" in receipt.reason


@pytest.mark.usefixtures("several_takes_on")
def test_a_noisy_take_is_named_not_summed_away():
    others = [
        {"take": 1, **_report(status="no_change", added_cuts=0, reason="noisy_take")},
        {"take": 2, **_report()},
    ]
    receipt = _review(_variant(others=others))
    assert receipt.status == "partial"
    assert receipt.reason == bc._TIGHT_TAKES_NOISY
    with reply_language_for("tr"):
        assert _review(_variant(others=others)).reason == bc._REASON_TR[bc._TIGHT_TAKES_NOISY]


@pytest.mark.usefixtures("several_takes_on")
def test_an_unmeasured_take_or_join_is_named_not_summed_away():
    others = [
        {"take": 1, **_report(status="unavailable", reason="unmeasured", pauses_over_limit=None)},
        {"take": 2, **_report()},
    ]
    assert _review(_variant(others=others)).reason == bc._TIGHT_TAKES_UNMEASURED
    unmeasured_join = [{"after_take": 0, "gap_s": None}, {"after_take": 1, "gap_s": 0.2}]
    assert _review(_variant(gaps=unmeasured_join)).reason == bc._TIGHT_TAKES_UNMEASURED


@pytest.mark.usefixtures("several_takes_on")
def test_the_cleanup_count_covers_every_take():
    variant = _variant(
        phone_talking_multi_clip={
            "version": 1,
            "speaker_removed": [
                {
                    "media_id": "b",
                    "removed": [
                        {"start_s": 0.0, "end_s": 0.2, "reason": "silence"},
                        {"start_s": 1.0, "end_s": 1.3, "reason": "filler_lexical"},
                    ],
                },
                {"media_id": "c", "removed": [{"start_s": 2.0, "end_s": 2.4, "reason": "silence"}]},
            ],
        }
    )
    pauses, fillers, retakes, seconds = bc._cleanup_counts(variant)
    # One pause from the first take's record, one from each later take's.
    assert (pauses, fillers, retakes) == (3, 1, 0)
    assert seconds == pytest.approx(0.8 + 0.2 + 0.3 + 0.4)
    note = bc.speech_cleanup_review_note(variant)
    assert note is not None and "3 pauses" in note and "long pauses" not in note


def test_a_one_take_render_reads_only_its_own_report(monkeypatch):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True, raising=False)
    variant = _variant(others=[], gaps=[])
    variant.pop("tight_pauses_other_takes")
    variant.pop("take_join_gaps")
    facts = plan_facts_from_phone_variant(variant)
    assert facts.speech_cleanup_tight_takes is None
    assert check_requirement(TIGHT, facts).status == "met"


def test_turkish_twins_exist_for_the_new_static_lines():
    for line in (bc._TIGHT_TAKES_NOISY, bc._TIGHT_TAKES_UNMEASURED):
        assert line in bc._TIGHT_STATIC_LINES
        assert bc._REASON_TR[line] != line
        assert bc.is_format_limit(line) and bc.is_format_limit(bc._REASON_TR[line])
