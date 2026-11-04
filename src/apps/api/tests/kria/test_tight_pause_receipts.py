"""Ways the tight-pause receipts can lie (Qendresa 13, "Cut out all my pauses so it's tight").

Prod/kit: the render cut only pauses >= 0.6 s and the finished receipt said a bare "Done".

 1. Bare Done with long pauses left: flag on, ask is tight, render has NO ``tight_pauses``
    report -> never a bare met.
 2. Report says ``pauses_over_limit > 0`` -> partial with the count (singular/plural) and the
    longest pause; report ``unavailable`` -> partial, "couldn't measure".
 3. Report applied/no_change with 0 over limit -> met (measured, so truthful), bare.
 4. Flag OFF -> byte-identical to today (met, no reason), for finished and for every draft.
 5. Draft promises: planned / offered / offer unknown / unavailable / not a phone Talking edit.
 6. Named-cut and restart notes must survive next to the tight lead.
 7. A plain "cut the long pauses" ask is not touched by the flag.
 8. Turkish twins exist for every new English line; the report is read only when a dict.
 9. Cleanup not run / declined -> today's logic (no tight claim).
"""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria import brief_checks
from app.kria.brief import BriefRequirement
from app.kria.brief_checks import PlanFacts, check_requirement, plan_facts_from_phone_variant
from app.kria.reply_language import reply_language_for

ASK = "Cut out all my pauses so it's tight"


def _req(text=ASK, **facts) -> BriefRequirement:
    facts.setdefault("speech_cleanup", True)
    if text == ASK:
        facts.setdefault("pauses", "tight")
    return BriefRequirement(id="tight", kind="audio", scope="global", description=text, facts=facts)


def _variant(report=None, outcome="applied") -> dict:
    v = {
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "ok": True,
        "render_status": "ready",
        "caption_cues": [{"start_s": 0.0, "end_s": 1.0, "text": "hi"}],
        "silence_cut_outcome": outcome,
        "silence_cut": {
            "removed": [{"reason": "silence", "start_s": 1.0, "end_s": 1.8}],
        },
    }
    if report is not None:
        v["tight_pauses"] = report
    return v


def _report(**over) -> dict:
    r = {
        "version": 1,
        "status": "applied",
        "target_s": 0.3,
        "limit_s": 0.35,
        "added_cuts": 4,
        "added_removed_ms": 900,
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


def _rendered(req, report=None, outcome="applied"):
    return check_requirement(req, plan_facts_from_phone_variant(_variant(report, outcome)))


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", True, raising=False)


# --- finished renders -------------------------------------------------------------------


def test_kit_scenario_no_report_is_not_bare_met(flag_on):
    receipt = _rendered(_req())
    assert receipt.status == "partial"
    assert receipt.reason == "Speech cleanup cut only the long pauses, so shorter pauses stay"


def test_kit_scenario_flag_off_is_todays_bare_met(monkeypatch):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", False, raising=False)
    receipt = _rendered(_req(), _report(pauses_over_limit=3))
    assert receipt.status == "met"
    assert receipt.reason is None


def test_measured_clean_is_bare_met(flag_on):
    for status in ("applied", "no_change"):
        receipt = _rendered(_req(), _report(status=status))
        assert (receipt.status, receipt.reason) == ("met", None), status


def test_over_limit_plural_and_singular(flag_on):
    many = _rendered(_req(), _report(pauses_over_limit=3, longest_pause_s=0.52))
    assert many.status == "partial"
    assert many.reason == (
        "Speech cleanup cut your pauses tight, but 3 still last longer than 0.35 s "
        "(the longest is 0.5 s)"
    )
    one = _rendered(_req(), _report(pauses_over_limit=1, longest_pause_s=1.24))
    assert one.reason == (
        "Speech cleanup cut your pauses tight, but 1 still lasts longer than 0.35 s "
        "(the longest is 1.2 s)"
    )


def test_unavailable_report(flag_on):
    receipt = _rendered(_req(), _report(status="unavailable", pauses_over_limit=None, reason="x"))
    assert receipt.status == "partial"
    assert receipt.reason == (
        "I couldn't measure the quiet parts of your take, so I didn't shorten the shorter pauses"
    )


def test_noisy_take_report(flag_on):
    report = _report(status="no_change", reason="noisy_take", pauses_over_limit=None)
    receipt = _rendered(_req(), report)
    assert receipt.status == "partial"
    assert receipt.reason == (
        "There's background sound in your take, so I only cut the long pauses"
    )
    with reply_language_for("tr"):
        tr = _rendered(_req(), report)
    assert tr.reason == brief_checks._REASON_TR[brief_checks._TIGHT_NOISY]


def test_malformed_report_is_treated_as_missing(flag_on):
    receipt = _rendered(_req(), "oops")
    assert receipt.status == "partial"
    assert "only the long pauses" in receipt.reason


def test_not_run_keeps_todays_logic(flag_on):
    v = _variant(None, outcome=None)
    v.pop("silence_cut")
    receipt = check_requirement(_req(), plan_facts_from_phone_variant(v))
    assert receipt.status == "partial"
    assert receipt.reason == "Speech cleanup didn't run on this video, so the long pauses stay"


def test_tight_words_without_the_fact_are_plain_cleanup(flag_on):
    receipt = _rendered(_req(pauses=None))
    assert (receipt.status, receipt.reason) == ("met", None)


def test_plain_pause_ask_untouched_by_flag(flag_on):
    receipt = _rendered(_req("cut the long pauses"))
    assert (receipt.status, receipt.reason) == ("met", None)


def test_restart_note_survives_tight_lead(flag_on, monkeypatch):
    monkeypatch.setattr(brief_checks, "_retake_detection_on", lambda facts=None: False)
    req = _req(cut_restarts=True)
    receipt = _rendered(req, _report(pauses_over_limit=2, longest_pause_s=0.6))
    assert receipt.status == "partial"
    assert "still last longer than 0.35 s" in receipt.reason
    assert "restarted" in receipt.reason


def test_named_cut_note_survives_tight_lead(flag_on):
    req = _req(ASK + " and cut the part where I say hello", cut_lines=["hello"])
    receipt = _rendered(req, _report())
    assert receipt.status == "partial"
    assert "may still be in your video" in receipt.reason


# --- drafts -----------------------------------------------------------------------------


def _draft(**over) -> PlanFacts:
    base = {
        "edit_format": "subtitled",
        "phone_talking": True,
        "video_clip_count": 1,
        "speech_cleanup_enabled": True,
    }
    base.update(over)
    return PlanFacts(**base)


DRAFT_CASES = [
    (
        {},
        "Speech cleanup cuts every pause down to about 0.3 s, and I'll tell you after if any "
        "stay longer",
    ),
    (
        {"speech_cleanup_enabled": False, "speech_cleanup_offered": True},
        "Choose Clean up speech when you approve and every pause is cut down to about 0.3 s",
    ),
    (
        {"speech_cleanup_enabled": False},
        "If Clean up speech is offered when you approve, choose it and every pause is cut down "
        "to about 0.3 s",
    ),
    (
        {"speech_cleanup_enabled": False, "speech_cleanup_offered": False},
        "Speech cleanup isn't available for this project yet, so the pauses stay",
    ),
    (
        {"phone_talking": False},
        "Speech cleanup cuts only the long pauses in this kind of video, so shorter pauses stay",
    ),
]


@pytest.mark.parametrize(("over", "reason"), DRAFT_CASES)
def test_draft_receipts(flag_on, over, reason):
    receipt = check_requirement(_req(), _draft(**over))
    assert receipt.status == "partial"
    assert receipt.reason == reason


@pytest.mark.parametrize(("over", "reason"), DRAFT_CASES)
def test_draft_flag_off_is_todays_copy(monkeypatch, over, reason):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", False, raising=False)
    receipt = check_requirement(_req(), _draft(**over))
    assert "0.3 s" not in (receipt.reason or "")
    assert "shorter pauses" not in (receipt.reason or "")


def test_draft_flag_off_planned_is_met(monkeypatch):
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", False, raising=False)
    receipt = check_requirement(_req(), _draft())
    assert receipt.status == "met"


def test_turkish_twins_exist_for_every_static_line():
    for text in brief_checks._TIGHT_STATIC_LINES:
        assert brief_checks._REASON_TR[text] != text
        assert brief_checks.is_format_limit(text)
        assert brief_checks.is_format_limit(brief_checks._REASON_TR[text])


def test_turkish_dynamic_and_draft_receipts(flag_on):
    with reply_language_for("tr"):
        draft = check_requirement(_req(), _draft())
        over = _rendered(_req(), _report(pauses_over_limit=2, longest_pause_s=0.6))
    assert draft.reason == brief_checks._REASON_TR[brief_checks._TIGHT_PLANNED]
    assert over.reason.startswith(brief_checks._REASON_TR[brief_checks._TIGHT_OVER_LIMIT])
    assert "0,35 sn" in over.reason and "0,6 sn" in over.reason
    assert brief_checks.is_format_limit(over.reason)


def test_no_change_with_pauses_over_limit_is_honest(flag_on):
    report = _report(status="no_change", pauses_over_limit=2, longest_pause_s=0.84)
    assert _rendered(_req(), report).reason == (
        "I couldn't safely shorten 2 pauses (the longest is 0.8 s), so they stay"
    )
    one = _report(status="no_change", pauses_over_limit=1, longest_pause_s=0.84)
    assert _rendered(_req(), one).reason == (
        "I couldn't safely shorten 1 pause (the longest is 0.8 s), so it stays"
    )
    with reply_language_for("tr"):
        tr = _rendered(_req(), report)
    assert "kısaltamadım" in tr.reason and "0,8 sn" in tr.reason


def test_unmeasured_reason_maps_to_unmeasured_line(flag_on):
    report = _report(
        status="no_change", reason="unmeasured", pauses_over_limit=None, longest_pause_s=None
    )
    assert _rendered(_req(), report).reason == (
        "I couldn't measure the quiet parts of your take, so I didn't shorten the shorter pauses"
    )


def test_multi_clip_draft_does_not_promise_tight(flag_on):
    for count in (2, 3, None):
        receipt = check_requirement(_req(), _draft(video_clip_count=count))
        assert receipt.reason == (
            "Speech cleanup cuts only the long pauses in this kind of video, so shorter pauses stay"
        ), count


def _cut_variant(rows, report=None):
    v = _variant(report)
    v["silence_cut"] = {"removed": rows}
    return v


def test_touching_rows_count_as_one_pause_and_say_pauses(flag_on):
    from app.kria.brief_checks import speech_cleanup_review_note

    rows = [
        {"reason": "silence", "start_s": 1.0, "end_s": 1.8},
        {"reason": "silence", "start_s": 1.8, "end_s": 1.9},  # sliver touching the first
        {"reason": "silence", "start_s": 4.0, "end_s": 4.2},  # small tight cut
        {"reason": "filler_lexical", "start_s": 6.0, "end_s": 6.2},
    ]
    note = speech_cleanup_review_note(_cut_variant(rows, _report(added_cuts=2)))
    assert "2 pauses and 1 filler sound (1.3 s)" in note
    assert "long pause" not in note
    plain = speech_cleanup_review_note(_cut_variant(rows))
    assert "2 long pauses and 1 filler sound" in plain


def test_consented_rows_unchanged_by_merging():
    from app.kria.brief_checks import speech_cleanup_review_note

    rows = [
        {"reason": "silence", "start_s": 1.0, "end_s": 1.8},
        {"reason": "silence", "start_s": 2.0, "end_s": 2.9},
        {"reason": "silence", "start_s": 2.9005, "end_s": 3.0},  # within 1 ms: same pause
    ]
    two_apart = speech_cleanup_review_note(_cut_variant(rows[:2]))
    assert "2 long pauses (1.7 s)" in two_apart
