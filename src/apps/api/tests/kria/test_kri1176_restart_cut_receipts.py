"""KRI-1176: a recorded line the creator never said is never promised as a cut.

Prod chat "Green 3" (2026-11-03, TestFlight 1876, phone Talking, ``edit_format="subtitled"``):
"Cut the pauses and the sentence I restarted." The brief model copied the take's own words
into ``cut_lines`` ("So Lumo took its tech from cold hard, no again.") although the creator
never said them. ``_check_speech_cleanup`` saw a non-empty ``named_cut_lines(req)`` and
promised "I'll cut the line you named out of your take if I find it said once", but the render
reads ``opening_cut_ask.named_cut_asks``, which drops every line the request does not say, so
nothing was ever cut. Consented cleanup (``required_v1``) only cuts a restarted sentence when
retake detection is on (``retake_cut_enabled`` AND ``speech_cleanup_retakes_enabled``), and the
finished-render note ignored the second lever. The fixture is the real prod data.

Failure modes, written before the fix:
1. Green 3 r2 on a phone Talking draft promises "I'll cut the line you named" for a line the
   render will drop (in every cleanup state, with retake detection on or off).
2. KRI-821/889 regression: a line the request does say ("the half question 'how are you
   gonna'", a restart that also quotes a line) loses its cut-at-render promise.
3. A recorded `cut_lines` entry the request never says ("what she says about the bread")
   still gets the promise on phone Talking instead of the "may still be in your video" note.
4. After the render, a retake row in the cut record turns the Green 3 ask "met" (KRI-551: a
   retake count cannot prove the line went), or the finished reply fails to count the
   "1 restarted sentence" the cleanup did cut.
5. The finished-render note says "a sentence you restarted stays in" where retake detection
   ran, or drops that sentence where it did not (consented cleanup ignored the second lever).
6. The levers disagree: `speech_cleanup_retakes_enabled()`, `current_detector_policy()` and
   `_cleanup_note_retake_cut_possible` must all read both switches the same way.
7. A Turkish chat gets the Turkish cut-at-render promise for a line the request never said.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    is_format_limit,
    named_cut_lines,
    needs_creator_choice,
    plan_facts_from_phone_variant,
    speech_cleanup_review_note,
)
from app.kria.opening_cut_ask import named_cut_asks
from app.kria.reply_language import reply_language_for
from app.tasks import kria_runtime

_FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri1176_green3_restart.json").read_text()
)
_GENERATION = "d9ff5ff3a2184bfda7719201a4767d7c"

_PROMISE = "I'll cut the line you named out of your take if I find it said once, and tell you after"
_PROMISE_TR = "Söylediğin cümleyi çekiminde bir kez geçiyorsa keseceğim ve sonra sana söyleyeceğim"
_STAYS_NOTE = (
    "I can't cut out one exact line yet or confirm it's gone, so it may still be in your video"
)
_STAYS_NOTE_TR = (
    "belirli bir cümleyi henüz tam olarak kesemiyorum ve çıkarıldığını doğrulayamıyorum, "
    "o yüzden videoda kalmış olabilir"
)
# The prod review note, and the tail of the prod reply (`final_reply`).
_PROD_NOTE = (
    "Speech cleanup cut what it heard as 3 long pauses (1.0 s). "
    "It looks only for pauses and filler sounds, so a sentence you restarted stays in."
)
_NOTE_STAYS_IN = "a sentence you restarted stays in"

# What a draft says first, by cleanup state (`_check_speech_cleanup`).
_LEADS = {
    "on": (
        {"speech_cleanup_enabled": True, "speech_cleanup_offered": True},
        "Speech cleanup cuts the long pauses",
    ),
    "offered": (
        {"speech_cleanup_offered": True},
        "Choose Clean up speech when you approve and the long pauses are cut",
    ),
    "unknown": (
        {},
        "If Clean up speech is offered when you approve, choose it to cut the long pauses",
    ),
    "unavailable": (
        {"speech_cleanup_offered": False},
        "Speech cleanup isn't available for this project yet, so the pauses stay",
    ),
}

# (retake_cut_enabled, speech_cleanup_retakes_enabled)
_DETECTION_OFF = [
    pytest.param(False, False, id="both-off"),
    pytest.param(True, False, id="only-retake-cut"),
    pytest.param(False, True, id="only-cleanup-retakes"),
]
_BOTH_ON = pytest.param(True, True, id="both-on")
_DETECTION_ALL = [*_DETECTION_OFF, _BOTH_ON]
_DETECTION_EDGES = [_DETECTION_OFF[0], _BOTH_ON]

_RETAKE_ROW = {"start_s": 16.9, "end_s": 19.8, "reason": "retake"}  # the Green 3 restart


@pytest.fixture(autouse=True)
def _detection_off_unless_a_test_says_otherwise(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "retake_cut_enabled", False)
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", False)


def _detection(monkeypatch: pytest.MonkeyPatch, cut: bool, retakes: bool) -> None:
    monkeypatch.setattr(settings, "retake_cut_enabled", cut)
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", retakes)


def _r2() -> BriefRequirement:
    """The prod row: a restart ask whose `cut_lines` the creator never said."""
    return BriefRequirement.model_validate(_FIXTURE["brief_r2_turn1"])


def _ask(description: str, *, kind: str = "audio", **facts: Any) -> BriefRequirement:
    return BriefRequirement(
        id="r1", kind=kind, scope="global", description=description, facts=facts
    )


def _draft(state: str = "offered") -> PlanFacts:
    """What the runtime judges a phone Talking draft with (KRI-705)."""
    return PlanFacts(
        edit_format="subtitled", caption_style="karaoke", phone_talking=True, **_LEADS[state][0]
    )


def _variant(*, retakes: int = 0) -> dict[str, Any]:
    """The Green 3 finished phone Talking variant, optionally with a retake row added."""
    cut = dict(_FIXTURE["variant_silence_cut"])
    cut["removed"] = [dict(row) for row in cut["removed"]]
    cut["removed"] += [dict(_RETAKE_ROW) for _ in range(retakes)]
    return {
        "variant_id": "subtitled",
        "render_generation_id": _GENERATION,
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_language": "en",
        "voiceover_caption_style": "sentence",
        "caption_cues": [dict(cue) for cue in _FIXTURE["caption_cues_output_time"]],
        "silence_cut_outcome": "applied",
        "silence_cut": cut,
    }


def _receipt(req: BriefRequirement, facts: PlanFacts) -> Any:
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


def _rendered(req: BriefRequirement, *, retakes: int = 0) -> Any:
    return _receipt(req, plan_facts_from_phone_variant(_variant(retakes=retakes)))


def _assert_limit_not_choice(receipt: Any) -> None:
    """An honest "Partly" about the format: never the "simpler version?" question."""
    assert receipt.status == "partial", receipt
    assert is_format_limit(receipt.reason), receipt.reason
    assert not needs_creator_choice(receipt), receipt


def _review(
    variant: dict[str, Any],
    *,
    brief: CreativeBrief | None = None,
    plan: dict[str, Any] | None = None,
    language: str = "en",
) -> str:
    """The finished-render reply. ``brief=None`` is a lost binding (the KRI-551 prod case)."""
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    binding = BriefBinding.create(thread.id, brief).model_dump(mode="json")
    execution = SimpleNamespace(result={"brief_binding": binding})
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan=dict(plan or {}))
    with reply_language_for(language):
        text, _ = kria_runtime._approved_generation_review(
            None, thread, job, variant, execution, "Your edit is ready."
        )
    return text


def _green3_brief() -> CreativeBrief:
    return CreativeBrief(version=1, requirements=[_r2()])


_REQUIRED = {"speech_cleanup_contract": _FIXTURE["speech_cleanup_contract"]}


# --- the prod row, replayed ----------------------------------------------------------------------


def test_the_fixture_is_the_prod_row_whose_cut_line_the_render_drops():
    req = _r2()
    assert req.description == "Cut the pauses and the sentence I restarted."
    assert _FIXTURE["speech_cleanup_contract"] == "required_v1"
    assert _FIXTURE["variant_silence_cut_outcome"] == "applied"
    # The checker sees a recorded line; the render keeps none of it (the creator never said it).
    assert named_cut_lines(req) == ["So Lumo took its tech from cold hard, no again."]
    assert named_cut_asks([req]) == ()
    assert "line you named" in _FIXTURE["turn1_receipt_r2"]["reason"]


# --- 1: the draft never promises a cut the render drops -------------------------------------------


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_EDGES)
@pytest.mark.parametrize("state", list(_LEADS))
def test_green3_draft_never_promises_the_line_you_named(monkeypatch, state, cut, retakes):
    _detection(monkeypatch, cut, retakes)

    receipt = _receipt(_r2(), _draft(state))

    assert "line you named" not in (receipt.reason or "")
    assert receipt.reason == f"{_LEADS[state][1]}; {_STAYS_NOTE}"
    _assert_limit_not_choice(receipt)


# --- 2: a line the request says keeps the promise (KRI-821 / KRI-889) -----------------------------


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_EDGES)
@pytest.mark.parametrize(
    "req",
    [
        _ask(
            "Cut the half question 'how are you gonna'",
            speech_cleanup=True,
            cut_lines=["how are you gonna"],
        ),
        _ask(
            "Cut the pauses and the sentence I restarted, 'So Lumo took its tech from cold hard'",
            speech_cleanup=True,
            cut_lines=["So Lumo took its tech from cold hard"],
        ),
    ],
    ids=["half-question", "restart-plus-quoted-line"],
)
def test_a_line_the_request_says_keeps_the_cut_at_render_promise(monkeypatch, req, cut, retakes):
    _detection(monkeypatch, cut, retakes)
    assert named_cut_asks([req])  # the render keeps it

    receipt = _receipt(req, _draft("offered"))

    assert receipt.status == "partial"
    assert _PROMISE in receipt.reason
    assert _STAYS_NOTE not in receipt.reason
    _assert_limit_not_choice(receipt)


# --- 3: a recorded line the request never says gets the note, not the promise ---------------------


def test_a_recorded_line_the_request_never_says_gets_the_stays_note_not_the_promise():
    req = _ask(
        "Remove what she says about the bread",
        speech_cleanup=True,
        cut_lines=["with bread, no,"],
    )
    assert named_cut_asks([req]) == ()  # the render drops it

    receipt = _receipt(req, _draft("offered"))

    assert _STAYS_NOTE in receipt.reason
    assert _PROMISE not in receipt.reason
    assert "line you named" not in receipt.reason
    _assert_limit_not_choice(receipt)


# --- 4: after the render --------------------------------------------------------------------------


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_EDGES)
def test_a_retake_row_never_marks_the_green3_ask_met_but_the_reply_counts_it(
    monkeypatch, cut, retakes
):
    _detection(monkeypatch, cut, retakes)

    receipt = _rendered(_r2(), retakes=1)
    text = _review(_variant(retakes=1), brief=_green3_brief(), plan=_REQUIRED)

    assert receipt.status == "partial", receipt  # KRI-551: a retake count cannot prove the line
    assert _STAYS_NOTE in receipt.reason
    assert "line you named" not in receipt.reason
    assert "1 restarted sentence" in text
    assert _STAYS_NOTE in text


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_EDGES)
def test_green3_without_a_retake_row_stays_partial_with_the_stays_note(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)

    receipt = _rendered(_r2())

    assert receipt.reason == f"Speech cleanup cut the long pauses; {_STAYS_NOTE}"
    _assert_limit_not_choice(receipt)


# --- 5: the finished-render note ------------------------------------------------------------------


def test_the_pure_note_is_the_prod_note_and_the_tail_of_the_prod_reply():
    assert speech_cleanup_review_note(_variant()) == _PROD_NOTE
    assert _FIXTURE["final_reply"].endswith(_PROD_NOTE)


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_OFF)
def test_with_detection_off_the_review_note_is_the_prod_note(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)
    possible = kria_runtime._cleanup_note_retake_cut_possible("required_v1")

    assert speech_cleanup_review_note(_variant(), retake_cut_possible=possible) == _PROD_NOTE


def test_with_detection_on_the_review_note_has_no_stays_in_sentence(monkeypatch):
    _detection(monkeypatch, True, True)
    possible = kria_runtime._cleanup_note_retake_cut_possible("required_v1")

    note = speech_cleanup_review_note(_variant(), retake_cut_possible=possible)

    assert note is not None
    assert note.startswith("Speech cleanup cut what it heard as 3 long pauses (1.0 s).")
    assert "stays in" not in note
    assert "looks only for pauses" not in note


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_ALL)
def test_a_retake_row_is_reported_as_a_restarted_sentence(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)
    possible = kria_runtime._cleanup_note_retake_cut_possible("required_v1")

    note = speech_cleanup_review_note(_variant(retakes=1), retake_cut_possible=possible)

    assert note is not None
    assert "1 restarted sentence" in note
    assert "stays in" not in note


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_ALL)
def test_the_finished_reply_says_a_restart_stays_in_only_where_detection_did_not_run(
    monkeypatch, cut, retakes
):
    """The lost-binding prod path (KRI-551): the note alone speaks for the render."""
    _detection(monkeypatch, cut, retakes)

    text = _review(_variant(), plan=_REQUIRED)

    assert (_NOTE_STAYS_IN in text) is not (cut and retakes)
    assert "Speech cleanup cut what it heard as 3 long pauses (1.0 s)." in text


# --- 6: the levers agree --------------------------------------------------------------------------


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_ALL)
def test_the_public_retake_predicate_is_both_levers(monkeypatch, cut, retakes):
    from app.services.plan_item_media import speech_cleanup_retakes_enabled

    _detection(monkeypatch, cut, retakes)

    assert speech_cleanup_retakes_enabled() is (cut and retakes)


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_ALL)
def test_the_detector_policy_names_retakes_exactly_when_both_levers_are_on(
    monkeypatch, cut, retakes
):
    from app.services.plan_item_media import current_detector_policy

    _detection(monkeypatch, cut, retakes)
    policy = current_detector_policy()

    assert ("retakes=enabled" in policy) is (cut and retakes)
    assert ("retakes=disabled" in policy) is not (cut and retakes)


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_ALL)
def test_the_review_note_helper_follows_the_same_two_levers(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)

    assert kria_runtime._cleanup_note_retake_cut_possible("required_v1") is (cut and retakes)
    # The older auto-cleanup path never read the second lever.
    assert kria_runtime._cleanup_note_retake_cut_possible("legacy_auto") is cut
    assert kria_runtime._cleanup_note_retake_cut_possible(None) is cut


# --- 7: Turkish -----------------------------------------------------------------------------------


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_EDGES)
@pytest.mark.parametrize(
    "req",
    [
        _r2(),
        _ask(
            "Baştan başladığım cümleyi kes",
            speech_cleanup=True,
            cut_lines=["Bu cümleyi baştan söyledim"],
        ),
    ],
    ids=["green3-r2", "turkish-ask"],
)
def test_a_turkish_chat_never_gets_the_turkish_promise_for_a_line_the_request_never_says(
    monkeypatch, req, cut, retakes
):
    _detection(monkeypatch, cut, retakes)
    assert named_cut_asks([req]) == ()

    with reply_language_for("tr"):
        receipt = _receipt(req, _draft("offered"))

    assert receipt.status == "partial", receipt
    assert _STAYS_NOTE_TR in receipt.reason
    assert _PROMISE_TR not in receipt.reason
    assert _PROMISE not in receipt.reason
    assert _STAYS_NOTE not in receipt.reason
    _assert_limit_not_choice(receipt)


def test_a_turkish_chat_keeps_the_turkish_promise_for_a_line_the_request_says():
    req = _ask("Şu cümleyi kes: 'ekmekle, yok,'", speech_cleanup=True, cut_lines=["ekmekle, yok,"])
    assert named_cut_asks([req])

    with reply_language_for("tr"):
        receipt = _receipt(req, _draft("offered"))

    assert _PROMISE_TR in receipt.reason
    assert _STAYS_NOTE_TR not in receipt.reason
