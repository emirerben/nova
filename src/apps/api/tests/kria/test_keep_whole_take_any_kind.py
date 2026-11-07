"""KRI-971: "Use the whole clip as it is, don't trim anything." filed as ``kind=style``.

Incident (Don Julio lunch T1, thread 867D2F93): the model filed the ask as
``style/global {"keep_whole_take": true}``. ``_wants_whole_take`` only took ``kind=timing``, so
the receipt was "I can't verify this one automatically yet." and nothing told the creator whether
the take was kept.

Ways this can fail, each pinned below (written before the code):
1. The recorded fact under any kind is not read as keeping the take whole (receipt unchecked).
2. The words alone ("whole clip") under a non-timing kind start to count: no fact, no ask.
3. A pop-in that also says "keep the take" (the fact plus `triggers`) loses its beat check.
4. A false fact, a title-scoped row, a row with its own length, or a clip-scoped row is read as a
   whole-video keep-whole ask.
5. Receipt verdicts drift: met only for a one-clip Talking edit with cleanup off; cleanup on is
   partial; a montage is partial; an edit with no known format is partial, never "can't verify".
6. The recorded ask is read as a cleanup ask, which would arm the cleanup offer it contradicts.

Pure functions from ``brief_checks``: no model, DB or network.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    _wants_cleanup,
    _wants_whole_take,
    check_requirement,
    is_judged,
)

ASK = "Use the whole clip as it is, don't trim anything"


def _req(kind: str = "style", *, scope: str = "global", **facts) -> BriefRequirement:
    return BriefRequirement(id="r1", kind=kind, scope=scope, description=ASK, facts=facts)


@pytest.mark.parametrize("kind", ["style", "audio", "select", "text", "timing"])
def test_recorded_fact_is_a_whole_take_ask_under_any_kind(kind):
    assert _wants_whole_take(_req(kind, keep_whole_take=True))


@pytest.mark.parametrize(
    ("req", "why"),
    [
        (_req("style"), "words alone under a non-timing kind"),
        (_req("style", keep_whole_take=False), "recorded false"),
        (_req("style", keep_whole_take="false"), "recorded false as a string"),
        (_req("style", keep_whole_take=True, triggers=["the provoleta"]), "a pop-in on a word"),
        (_req("style", scope="title", keep_whole_take=True), "title scope"),
        (_req("style", scope="per_clip", keep_whole_take=True), "per-clip scope"),
        (_req("style", scope="clip:2", keep_whole_take=True), "one named clip"),
        (_req("style", keep_whole_take=True, duration_s=30), "has its own length"),
    ],
)
def test_not_a_whole_video_keep_whole_ask(req, why):
    assert not _wants_whole_take(req), why


def test_string_true_fact_counts_under_style():
    assert _wants_whole_take(_req("style", keep_whole_take="true"))


def test_a_keep_whole_ask_is_never_a_cleanup_ask():
    req = BriefRequirement(
        id="r1",
        kind="style",
        scope="global",
        description="use the whole clip, don't cut my pauses",
        facts={"keep_whole_take": True},
    )
    assert not _wants_cleanup(req)


def _facts(**kw) -> PlanFacts:
    return PlanFacts(**{"edit_format": "subtitled", "video_clip_count": 1, **kw})


@pytest.mark.parametrize("kind", ["style", "audio", "select"])
def test_one_clip_talking_edit_without_cleanup_is_met_and_checked(kind):
    req = _req(kind, keep_whole_take=True)
    receipt = check_requirement(req, _facts())
    assert receipt.status == "met"
    assert is_judged(req, receipt)


def test_cleanup_on_is_partial_and_names_the_cut():
    req = _req("style", keep_whole_take=True)
    receipt = check_requirement(req, _facts(speech_cleanup_enabled=True))
    assert receipt.status == "partial"
    assert is_judged(req, receipt)
    assert "cleanup" in (receipt.reason or "").casefold()


def test_montage_is_partial_and_says_it_cuts_the_footage():
    req = _req("style", keep_whole_take=True)
    receipt = check_requirement(req, _facts(edit_format="montage", video_clip_count=3))
    assert receipt.status == "partial"
    assert is_judged(req, receipt)
    assert "cuts your footage" in (receipt.reason or "")


def test_the_words_alone_keep_the_old_cannot_verify_receipt():
    req = _req("style")
    receipt = check_requirement(req, _facts())
    assert not is_judged(req, receipt)


@pytest.mark.parametrize(
    ("req", "expected"),
    [
        (_req("style", keep_whole_take=True), True),
        (_req("audio", speech_cleanup=False), True),
        (_req("style", scope="per_clip", keep_whole_take=True), False),
        (_req("audio", scope="clip:2", speech_cleanup=False), False),
        (_req("timing", scope="per_clip", keep_whole_take=True), False),
        (_req("style", keep_whole_take=True).model_copy(update={"status": "superseded"}), False),
    ],
)
def test_original_speech_recommendation_is_whole_video_and_live_only(req, expected):
    from app.kria.tight_pause_ask import wants_original_speech

    assert wants_original_speech(req) is expected
