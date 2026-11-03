"""KRI-1176: a "sentence I restarted" ask never gets a promise the render cannot keep.

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
1. Green 3 r2 on a phone Talking draft promises "I'll cut the line you named" (or says "line
   you named") for a line the render will drop.
2. Retake detection off: the restart ask's pre-render reason does not say the restart stays in.
3. Retake detection on: the pre-render reason does not say cleanup also cuts a restart, says
   "stays in", or is "met".
4. KRI-792/821 regression: a line the request does say ("the half question 'how are you
   gonna'") keeps the cut-at-render promise; a recorded line the request never says ("what
   she says about the bread") gets the "may still be in your video" note, never the promise.
5. A new restart sentence is not registered as a format limit, or turns into the "simpler
   version?" question (`needs_creator_choice`).
6. After the render: a retake row in the cut record leaves the ask partial; no retake row with
   detection on does not say it may still be in the video; with detection off it does not say
   the restart stays in (or still says "line you named").
7. After the render: a quoted line the request says, plus a retake row and no ``named_cuts``
   record, becomes "met" (a retake count cannot prove the quoted line went; KRI-551).
8. The finished-render note says "a sentence you restarted stays in" where detection ran, or
   drops the sentence where it did not; a retake row is not reported as "1 restarted sentence".
9. The levers disagree: `speech_cleanup_retakes_enabled()`, `current_detector_policy()`, the
   draft receipt and the review-note helper must all read both switches the same way.
10. A Turkish chat gets the English restart sentences.
11. A mixed ask ("the sentence I restarted and the part where I talk about prices") gets a
    restart sentence and loses the "may still be in your video" note for the other stretch.
12. "Restart the music at the chorus" is read as a restarted sentence.
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
_STAYS_NOTE = (
    "I can't cut out one exact line yet or confirm it's gone, so it may still be in your video"
)
_RESTART_STAYS = (
    "Clean up speech looks only for pauses and filler sounds, so a sentence you restarted stays in"
)
_RESTART_CUT_AT_APPROVAL = (
    "Clean up speech also cuts a sentence you restarted if it finds one, and I'll tell you after"
)
_RESTART_NOT_FOUND = (
    "Speech cleanup didn't find a sentence you restarted, so it may still be in your video"
)
_RESTART_STAYS_TR = (
    "“Konuşmayı temizle” yalnızca duraklamaları ve dolgu seslerini arar, "
    "o yüzden baştan başladığın bir cümle videoda kalır"
)
_RESTART_CUT_AT_APPROVAL_TR = (
    "“Konuşmayı temizle” baştan başladığın bir cümle bulursa onu da keser ve sonra sana söylerim"
)
_RESTART_NOT_FOUND_TR = (
    "Konuşma temizliği baştan başladığın bir cümle bulmadı, o yüzden videoda kalmış olabilir"
)
_ALL_RESTART_SENTENCES = (
    _RESTART_STAYS,
    _RESTART_CUT_AT_APPROVAL,
    _RESTART_NOT_FOUND,
    _RESTART_STAYS_TR,
    _RESTART_CUT_AT_APPROVAL_TR,
    _RESTART_NOT_FOUND_TR,
)
# The prod review note, and the tail of the prod reply (`final_reply`).
_PROD_NOTE = (
    "Speech cleanup cut what it heard as 3 long pauses (1.0 s). "
    "It looks only for pauses and filler sounds, so a sentence you restarted stays in."
)
_NOTE_STAYS_IN = "a sentence you restarted stays in"

# What a draft says first, by cleanup state (`_check_speech_cleanup`): cleanup on, offered at
# approval, or not known to be offered. "unavailable" is only used where the contract is silent.
_STATES: dict[str, tuple[dict[str, Any], str]] = {
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
}
_UNAVAILABLE = {"speech_cleanup_offered": False}
_UNAVAILABLE_LEAD = "Speech cleanup isn't available for this project yet, so the pauses stay"
_STATE_IDS = list(_STATES)

# (retake_cut_enabled, speech_cleanup_retakes_enabled)
_DETECTION_OFF = [
    pytest.param(False, False, id="both-off"),
    pytest.param(True, False, id="only-retake-cut"),
    pytest.param(False, True, id="only-cleanup-retakes"),
]
_DETECTION_ALL = [
    *_DETECTION_OFF,
    pytest.param(True, True, id="both-on"),
]

_RETAKE_ROWS = (
    {"start_s": 16.9, "end_s": 19.8, "reason": "retake"},  # the Green 3 restart (words 44-53)
    {"start_s": 24.0, "end_s": 25.5, "reason": "retake"},
)

_RESTART_WORDINGS = [
    "Cut the pauses and the sentence I restarted.",
    "Cut the pauses and my retakes",
    "Cut my false start",
    "Cut the pauses and anywhere I start over",
    "Cut the pauses and the times I start again",
    "Cut the pauses and the sentence before I say scratch that",
    "Cut the pauses and the sentence before I say let me start again",
]
_RESTART_WORDINGS_TR = [
    "Baştan başladığım cümleyi kes",
    "Baştan aldığım cümleyi çıkar",
    "Tekrar başladığım cümleyi kes",
    "Yeniden başladığım cümleyi kes",
]


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
    cleanup = _UNAVAILABLE if state == "unavailable" else _STATES[state][0]
    return PlanFacts(
        edit_format="subtitled", caption_style="karaoke", phone_talking=True, **cleanup
    )


def _lead(state: str) -> str:
    return _STATES[state][1]


def _variant(*, outcome: str = "applied", retakes: int = 0) -> dict[str, Any]:
    """The Green 3 finished phone Talking variant, optionally with retake rows added."""
    variant: dict[str, Any] = {
        "variant_id": "subtitled",
        "render_generation_id": _GENERATION,
        "resolved_archetype": "subtitled",
        "render_destination": "device",
        "caption_language": "en",
        "voiceover_caption_style": "sentence",
        "caption_cues": [dict(cue) for cue in _FIXTURE["caption_cues_output_time"]],
        "silence_cut_outcome": outcome,
    }
    if outcome == "applied":
        cut = dict(_FIXTURE["variant_silence_cut"])
        cut["removed"] = [dict(row) for row in cut["removed"]]
        cut["removed"] += [dict(row) for row in _RETAKE_ROWS[:retakes]]
        variant["silence_cut"] = cut
    return variant


def _receipt(req: BriefRequirement, facts: PlanFacts) -> Any:
    (receipt,) = build_receipts([req], facts, include_unchecked=True)
    return receipt


def _rendered(req: BriefRequirement, *, outcome: str = "applied", retakes: int = 0) -> Any:
    return _receipt(req, plan_facts_from_phone_variant(_variant(outcome=outcome, retakes=retakes)))


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


@pytest.mark.parametrize(("cut", "retakes"), [*_DETECTION_OFF[:1], _DETECTION_ALL[-1]])
@pytest.mark.parametrize("state", _STATE_IDS)
def test_green3_draft_never_promises_the_line_you_named(monkeypatch, state, cut, retakes):
    _detection(monkeypatch, cut, retakes)

    receipt = _receipt(_r2(), _draft(state))

    assert receipt.status == "partial", receipt
    assert "line you named" not in (receipt.reason or "")
    assert _PROMISE not in (receipt.reason or "")


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_when_cleanup_is_unavailable_the_draft_adds_no_restart_sentence(monkeypatch, cut, retakes):
    """Cleanup cannot run, so it is not said to cut a restart, nor to leave one in."""
    _detection(monkeypatch, cut, retakes)

    receipt = _receipt(_r2(), _draft("unavailable"))

    assert receipt.status == "partial", receipt
    assert receipt.reason == _UNAVAILABLE_LEAD
    _assert_limit_not_choice(receipt)


# --- 2 + 3: the restart sentence, by retake detection ---------------------------------------------


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_OFF)
@pytest.mark.parametrize("state", _STATE_IDS)
def test_with_detection_off_the_draft_says_the_restart_stays_in(monkeypatch, state, cut, retakes):
    _detection(monkeypatch, cut, retakes)

    receipt = _receipt(_r2(), _draft(state))

    assert receipt.reason == f"{_lead(state)}; {_RESTART_STAYS}"
    _assert_limit_not_choice(receipt)


@pytest.mark.parametrize("state", _STATE_IDS)
def test_with_detection_on_the_draft_says_cleanup_also_cuts_a_restart(monkeypatch, state):
    _detection(monkeypatch, True, True)

    receipt = _receipt(_r2(), _draft(state))

    assert receipt.reason == f"{_lead(state)}; {_RESTART_CUT_AT_APPROVAL}"
    assert "stays in" not in receipt.reason
    assert receipt.status == "partial"  # only the render can say it found one: never "met"
    _assert_limit_not_choice(receipt)


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
@pytest.mark.parametrize("description", _RESTART_WORDINGS)
def test_every_wording_of_a_restarted_sentence_gets_the_restart_sentence(
    monkeypatch, description, cut, retakes
):
    _detection(monkeypatch, cut, retakes)
    expected = _RESTART_CUT_AT_APPROVAL if cut and retakes else _RESTART_STAYS

    receipt = _receipt(_ask(description, speech_cleanup=True), _draft("offered"))

    assert receipt.reason == f"{_lead('offered')}; {expected}"
    _assert_limit_not_choice(receipt)


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_the_words_alone_make_a_restart_ask_without_the_cleanup_fact(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)
    expected = _RESTART_CUT_AT_APPROVAL if cut and retakes else _RESTART_STAYS

    receipt = _receipt(_ask("Cut the pauses and the sentence I restarted."), _draft("offered"))

    assert receipt.reason == f"{_lead('offered')}; {expected}"


# --- 4: KRI-792 / KRI-821 -------------------------------------------------------------------------


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_a_line_the_request_says_keeps_the_cut_at_render_promise(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)
    req = _ask(
        "Cut the half question 'how are you gonna'",
        speech_cleanup=True,
        cut_lines=["how are you gonna"],
    )
    assert named_cut_asks([req])  # the render keeps it

    receipt = _receipt(req, _draft("offered"))

    assert receipt.status == "partial"
    assert _PROMISE in receipt.reason
    assert _STAYS_NOTE not in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES)
    _assert_limit_not_choice(receipt)


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


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_a_restart_ask_that_quotes_a_line_keeps_the_promise_and_no_restart_sentence(
    monkeypatch, cut, retakes
):
    _detection(monkeypatch, cut, retakes)
    req = _ask(
        "Cut the pauses and the sentence I restarted, 'So Lumo took its tech from cold hard'",
        speech_cleanup=True,
        cut_lines=["So Lumo took its tech from cold hard"],
    )
    assert named_cut_asks([req])

    receipt = _receipt(req, _draft("offered"))

    assert _PROMISE in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES)


# --- 5: every restart sentence is a format limit --------------------------------------------------


@pytest.mark.parametrize("sentence", _ALL_RESTART_SENTENCES)
def test_each_restart_sentence_is_a_format_limit_never_a_creator_choice(sentence):
    assert is_format_limit(sentence)
    receipt = {"verification": "checked", "status": "partial", "reason": sentence}
    assert not needs_creator_choice(receipt)


# --- 6: after the render --------------------------------------------------------------------------


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_a_restart_the_cleanup_cut_makes_the_ask_met(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)

    receipt = _rendered(_r2(), retakes=1)

    assert receipt.status == "met", receipt
    assert "line you named" not in (receipt.reason or "")
    assert _STAYS_NOTE not in (receipt.reason or "")


@pytest.mark.parametrize("outcome", ["applied", "no_change"])
def test_no_restart_found_with_detection_on_says_it_may_still_be_in_the_video(monkeypatch, outcome):
    _detection(monkeypatch, True, True)

    receipt = _rendered(_r2(), outcome=outcome)

    assert receipt.status == "partial", receipt
    assert _RESTART_NOT_FOUND in receipt.reason
    assert "line you named" not in receipt.reason
    assert _STAYS_NOTE not in receipt.reason
    _assert_limit_not_choice(receipt)


@pytest.mark.parametrize("outcome", ["applied", "no_change"])
@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_OFF)
def test_no_restart_found_with_detection_off_says_the_restart_stays_in(
    monkeypatch, outcome, cut, retakes
):
    _detection(monkeypatch, cut, retakes)

    receipt = _rendered(_r2(), outcome=outcome)

    assert receipt.status == "partial", receipt
    assert _RESTART_STAYS in receipt.reason
    assert "line you named" not in receipt.reason
    assert _STAYS_NOTE not in receipt.reason
    assert _RESTART_NOT_FOUND not in receipt.reason
    _assert_limit_not_choice(receipt)


@pytest.mark.parametrize(
    ("outcome", "retakes", "expected"),
    [("applied", 0, 0), ("applied", 1, 1), ("applied", 2, 2), ("no_change", 0, 0)],
)
def test_the_rendered_facts_count_the_retake_rows_of_the_cut_record(outcome, retakes, expected):
    facts = plan_facts_from_phone_variant(_variant(outcome=outcome, retakes=retakes))

    assert getattr(facts, "speech_cleanup_retakes", "missing") == expected


def test_a_draft_has_no_retake_count():
    assert getattr(PlanFacts(), "speech_cleanup_retakes", "missing") is None


# --- 7: a quoted line is never proven by a retake count (KRI-551) ---------------------------------


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
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
    ids=["quoted-line", "restart-plus-quoted-line"],
)
def test_a_quoted_line_with_a_retake_row_and_no_receipt_stays_partial(
    monkeypatch, req, cut, retakes
):
    _detection(monkeypatch, cut, retakes)

    receipt = _rendered(req, retakes=1)

    assert receipt.status == "partial", receipt
    assert _STAYS_NOTE in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES)
    _assert_limit_not_choice(receipt)


# --- 8: the finished-render note ------------------------------------------------------------------


def test_the_pure_note_is_the_prod_note_and_the_tail_of_the_prod_reply():
    note = speech_cleanup_review_note(_variant())

    assert note == _PROD_NOTE
    assert _FIXTURE["final_reply"].endswith(_PROD_NOTE)


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_OFF)
def test_with_detection_off_the_review_note_is_the_prod_note(monkeypatch, cut, retakes):
    from app.tasks.kria_runtime import _cleanup_note_retake_cut_possible

    _detection(monkeypatch, cut, retakes)
    possible = _cleanup_note_retake_cut_possible("required_v1")

    assert speech_cleanup_review_note(_variant(), retake_cut_possible=possible) == _PROD_NOTE


def test_with_detection_on_the_review_note_has_no_stays_in_sentence(monkeypatch):
    from app.tasks.kria_runtime import _cleanup_note_retake_cut_possible

    _detection(monkeypatch, True, True)
    possible = _cleanup_note_retake_cut_possible("required_v1")

    note = speech_cleanup_review_note(_variant(), retake_cut_possible=possible)

    assert note is not None
    assert note.startswith("Speech cleanup cut what it heard as 3 long pauses (1.0 s).")
    assert "stays in" not in note
    assert "looks only for pauses" not in note


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_ALL)
def test_a_retake_row_is_reported_as_a_restarted_sentence(monkeypatch, cut, retakes):
    from app.tasks.kria_runtime import _cleanup_note_retake_cut_possible

    _detection(monkeypatch, cut, retakes)
    possible = _cleanup_note_retake_cut_possible("required_v1")

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


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_OFF)
def test_the_green3_reply_with_detection_off_says_the_restart_stays_in(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)

    text = _review(_variant(), brief=_green3_brief(), plan=_REQUIRED)

    assert _RESTART_STAYS in text
    assert "line you named" not in text
    assert _STAYS_NOTE not in text


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_OFF)
def test_the_green3_reply_with_detection_off_says_the_restart_stays_in_only_once(
    monkeypatch, cut, retakes
):
    """The receipt line already says it: the cleanup note must not repeat it."""
    _detection(monkeypatch, cut, retakes)

    text = _review(_variant(), brief=_green3_brief(), plan=_REQUIRED)

    assert text.count(_NOTE_STAYS_IN) == 1
    assert "Speech cleanup cut what it heard as 3 long pauses (1.0 s)." in text


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_OFF)
def test_a_turkish_reply_says_the_restart_stays_in_only_once(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)
    brief = CreativeBrief(
        version=1, requirements=[_ask("Baştan başladığım cümleyi kes", speech_cleanup=True)]
    )

    text = _review(_variant(), brief=brief, plan=_REQUIRED, language="tr")

    assert text.count("baştan başladığın bir cümle videoda kalır") == 1
    assert _RESTART_STAYS_TR in text


def test_the_review_note_skips_its_stays_in_sentence_when_the_reply_already_says_it():
    note = speech_cleanup_review_note(_variant(), text=f"Partly: x ({_RESTART_STAYS})")

    assert note == "Speech cleanup cut what it heard as 3 long pauses (1.0 s)."


def test_the_turkish_review_note_skips_its_stays_in_sentence_when_the_reply_already_says_it():
    with reply_language_for("tr"):
        note = speech_cleanup_review_note(_variant(), text=_RESTART_STAYS_TR)

    assert note is not None
    assert "baştan başladığın" not in note
    assert note.startswith("Konuşma temizliği duyduğu şu sesleri kesti")


def test_the_green3_reply_with_detection_on_and_no_restart_found_says_so(monkeypatch):
    _detection(monkeypatch, True, True)

    text = _review(_variant(), brief=_green3_brief(), plan=_REQUIRED)

    assert _RESTART_NOT_FOUND in text
    assert _NOTE_STAYS_IN not in text
    assert "line you named" not in text
    assert _STAYS_NOTE not in text


def test_the_green3_reply_with_a_cut_restart_is_done_and_counts_it(monkeypatch):
    _detection(monkeypatch, True, True)

    text = _review(_variant(retakes=1), brief=_green3_brief(), plan=_REQUIRED)

    assert "1 restarted sentence" in text
    assert "Partly" not in text
    assert _STAYS_NOTE not in text
    assert _NOTE_STAYS_IN not in text


# --- 9: the levers agree --------------------------------------------------------------------------


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
def test_the_draft_restart_sentence_follows_the_same_two_levers(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)

    reason = _receipt(_r2(), _draft("offered")).reason or ""

    assert (_RESTART_CUT_AT_APPROVAL in reason) is (cut and retakes)
    assert (_RESTART_STAYS in reason) is not (cut and retakes)


@pytest.mark.parametrize(("cut", "retakes"), _DETECTION_ALL)
def test_the_review_note_helper_follows_the_same_two_levers(monkeypatch, cut, retakes):
    from app.tasks.kria_runtime import _cleanup_note_retake_cut_possible

    _detection(monkeypatch, cut, retakes)

    assert _cleanup_note_retake_cut_possible("required_v1") is (cut and retakes)
    # The older auto-cleanup path never read the second lever.
    assert _cleanup_note_retake_cut_possible("legacy_auto") is cut
    assert _cleanup_note_retake_cut_possible(None) is cut


# --- 10: Turkish ----------------------------------------------------------------------------------


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
@pytest.mark.parametrize("description", _RESTART_WORDINGS_TR)
def test_a_turkish_chat_gets_the_turkish_restart_sentence_before_render(
    monkeypatch, description, cut, retakes
):
    _detection(monkeypatch, cut, retakes)
    req = _ask(description, speech_cleanup=True)
    expected_tr = _RESTART_CUT_AT_APPROVAL_TR if cut and retakes else _RESTART_STAYS_TR

    with reply_language_for("tr"):
        receipt = _receipt(req, _draft("offered"))

    assert receipt.status == "partial", receipt
    assert "baştan başladığın" in receipt.reason
    assert expected_tr in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES[:3])
    assert "line you named" not in receipt.reason


@pytest.mark.parametrize(
    ("cut", "retakes", "expected_tr"),
    [
        pytest.param(True, True, _RESTART_NOT_FOUND_TR, id="on-none-found"),
        pytest.param(False, False, _RESTART_STAYS_TR, id="off-none-found"),
    ],
)
def test_a_turkish_chat_gets_the_turkish_restart_sentence_after_render(
    monkeypatch, cut, retakes, expected_tr
):
    _detection(monkeypatch, cut, retakes)
    req = _ask("Baştan başladığım cümleyi kes", speech_cleanup=True)

    with reply_language_for("tr"):
        receipt = _rendered(req)

    assert receipt.status == "partial", receipt
    assert expected_tr in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES[:3])


# --- 11: a restart plus another stretch -----------------------------------------------------------


_MIXED = "Cut the sentence I restarted and the part where I talk about prices"


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
@pytest.mark.parametrize("state", _STATE_IDS)
def test_a_restart_plus_another_stretch_keeps_the_stays_note_before_render(
    monkeypatch, state, cut, retakes
):
    _detection(monkeypatch, cut, retakes)

    receipt = _receipt(_ask(_MIXED, speech_cleanup=True), _draft(state))

    assert receipt.status == "partial", receipt
    assert _STAYS_NOTE in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES)
    _assert_limit_not_choice(receipt)


@pytest.mark.parametrize("outcome", ["applied", "no_change"])
@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_a_restart_plus_another_stretch_keeps_the_stays_note_after_render(
    monkeypatch, outcome, cut, retakes
):
    _detection(monkeypatch, cut, retakes)

    receipt = _rendered(_ask(_MIXED, speech_cleanup=True), outcome=outcome)

    assert receipt.status == "partial", receipt
    assert _STAYS_NOTE in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES)
    _assert_limit_not_choice(receipt)


# A restart "of" or "about" something names a specific sentence (KRI-551: the N3 marathon ask),
# and a retake count cannot prove that one went: it is not a restart-only ask.
_MARATHON = "cut long pauses and the restart of the kilometer thirty sentence"


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
@pytest.mark.parametrize(
    "description",
    [
        _MARATHON,
        "Cut the pauses and the retake about kilometer thirty",
        "Cut the pauses and my false start of the kilometer thirty line",
        "Cut the pauses and where I restart the kilometer thirty sentence",
    ],
)
def test_a_restart_of_a_specific_sentence_keeps_the_stays_note_before_render(
    monkeypatch, description, cut, retakes
):
    _detection(monkeypatch, cut, retakes)

    receipt = _receipt(_ask(description), _draft("offered"))

    assert receipt.status == "partial", receipt
    assert _STAYS_NOTE in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES)
    _assert_limit_not_choice(receipt)


@pytest.mark.parametrize("restart_rows", [0, 1])
@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_a_restart_of_a_specific_sentence_keeps_the_stays_note_after_render(
    monkeypatch, cut, retakes, restart_rows
):
    _detection(monkeypatch, cut, retakes)

    receipt = _rendered(_ask(_MARATHON), retakes=restart_rows)

    assert receipt.status == "partial", receipt
    assert _STAYS_NOTE in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES)
    _assert_limit_not_choice(receipt)


def test_a_restart_plus_a_quoted_line_without_a_recorded_line_keeps_the_stays_note(monkeypatch):
    _detection(monkeypatch, True, True)
    req = _ask("Cut the sentence I restarted and 'how are you gonna'", speech_cleanup=True)

    receipt = _receipt(req, _draft("offered"))

    assert _STAYS_NOTE in receipt.reason
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES)


# --- "start over" / "start again" about the edit, not the speaker's sentence ---------------------

_START_OVER_EDIT = "Cut the pauses and start over with the beach clip"


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_start_over_with_a_clip_is_not_a_restarted_sentence_before_render(
    monkeypatch, cut, retakes
):
    _detection(monkeypatch, cut, retakes)

    receipt = _receipt(_ask(_START_OVER_EDIT, speech_cleanup=True), _draft("offered"))

    assert receipt.status == "partial", receipt
    assert not any(sentence in receipt.reason for sentence in _ALL_RESTART_SENTENCES)


@pytest.mark.parametrize("restart_rows", [0, 1])
@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_start_over_with_a_clip_is_never_met_by_a_retake_row_after_render(
    monkeypatch, cut, retakes, restart_rows
):
    _detection(monkeypatch, cut, retakes)

    receipt = _rendered(_ask(_START_OVER_EDIT, speech_cleanup=True), retakes=restart_rows)

    assert receipt.status == "partial", receipt
    assert not any(sentence in (receipt.reason or "") for sentence in _ALL_RESTART_SENTENCES)


# --- 12: "restart" about something that is not a spoken sentence ----------------------------------


@pytest.mark.parametrize(("cut", "retakes"), [_DETECTION_OFF[0], _DETECTION_ALL[-1]])
def test_restart_the_music_is_never_a_restarted_sentence(monkeypatch, cut, retakes):
    _detection(monkeypatch, cut, retakes)
    req = _ask("Restart the music at the chorus")

    draft = _receipt(req, _draft("offered"))
    rendered = _rendered(req)

    for receipt in (draft, rendered):
        assert not any(sentence in (receipt.reason or "") for sentence in _ALL_RESTART_SENTENCES)
