"""KRI-1189: "cut the sentence I restarted" is judged on a typed `cut_restarts` brief fact.

Prod chat "Green 3" (job 9bad2d75, thread 5A650474, TestFlight 1876, phone Talking): "Cut the
pauses and the sentence I restarted." The brief model recorded r2 as
``{"cut_lines": ["So Lumo took its tech from cold hard, no again."], "speech_cleanup": true}``,
copying the take's own words. KRI-1176 (#2090) made that honest ("may still be in your video"), but
once consented retake detection is on, a real retake cut still left the ask at "Partly" while the
finished-render note said "1 restarted sentence". The model that read the message now records
``cut_restarts: true`` (KRI-667: meaning lives in a typed fact, never in a new word pattern), and
the receipts key on it. The rows here are the real prod row with the facts the new prompt writes.

Failure modes, written before the code (full list with sources: `.dev/kri1189-failure-modes.md`
in the KRI-1189 worktree; real asks from the Çiğdem kit prompts, EN and TR):
B1  Fact + detection on, draft: the cleanup lead + "also cuts a sentence you restarted if it finds
    one"; never the named-line promise, never "may still be in your video".
B2  Fact + detection off, draft: "... a sentence you restarted stays in"; never the "also cuts".
B3  Never "met" before the render, even with cleanup already planned.
B4  Cleanup unavailable: no restart promise on top of "the pauses stay".
B5  Fact + a transcript line the ask never says in `cut_lines` (the prod shape): restart-only.
B6  Fact + a line the ask itself names ("the "okay"s and the places I restarted"): both parts.
B7  No fact (every brief written before this change): unchanged (#2090's tests pin it).
B9  Turkish chat: every new sentence in Turkish.
C1  Readiness, detection on: r2 approvable, not "Make it without these".
C2  Readiness, detection off: r2 held, like a named line that may stay.
C3  The Turkish "also cuts" sentence is deferred too.
D1  Finished render with a `retake` row: met, so the reply line reads "Done".
D2  No retake row, detection could run: partial "didn't find a sentence you restarted".
D3  No retake row, detection could not run: partial "stays in".
D4  The receipt and the note read "could detection run" from the same contract-aware helper.
D5  The reply never says "stays in" twice.
D7  A retake row never makes a brief WITHOUT the fact met (KRI-551; #2090 pins the prod row).
D9  Turkish finished reply.
A8  Both prompts (Main Creator and the brief extractor's narrowed copy) carry the instruction.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.config import settings
from app.kria.approval_readiness import APPROVABLE
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.kria.brief_checks import (
    PlanFacts,
    build_receipts,
    is_format_limit,
    needs_creator_choice,
    plan_facts_from_phone_variant,
)
from app.kria.opening_cut_ask import named_cut_asks
from app.kria.reply_language import reply_language_for
from app.tasks import kria_runtime
from tests.kria.test_brief_checks_speaker_background import (
    _gate,  # noqa: F401 - autouse phone Talking gates (prod profile)
)
from tests.kria.test_green3_pr1_receipts import (
    GREEN3_BACKGROUND,
    OTHER,
    PHOTO,
    R1,
    R3,
    R4,
    _manifest,
    _open_windows,
    _readiness,
)

_FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri1176_green3_restart.json").read_text()
)
_GENERATION = "d9ff5ff3a2184bfda7719201a4767d7c"
_ASK = "Cut the pauses and the sentence I restarted."
_TRANSCRIPT_LINE = "So Lumo took its tech from cold hard, no again."

_ALSO_CUTS = (
    "Clean up speech also cuts a sentence you restarted if it finds one, and I'll tell you after"
)
_STAYS = (
    "Clean up speech looks only for pauses and filler sounds, so a sentence you restarted stays in"
)
_NOT_FOUND = "Speech cleanup didn't find a sentence you restarted, so it may still be in your video"
_ALSO_CUTS_TR = (
    "“Konuşmayı temizle” baştan başladığın bir cümle bulursa onu da keser ve sonra sana söylerim"
)
_STAYS_TR = (
    "“Konuşmayı temizle” yalnızca duraklamaları ve dolgu seslerini arar, "
    "o yüzden baştan başladığın bir cümle videoda kalır"
)
_NOT_FOUND_TR = (
    "Konuşma temizliği baştan başladığın bir cümle bulmadı, o yüzden videoda kalmış olabilir"
)
_NAMED_PROMISE = (
    "I'll cut the line you named out of your take if I find it said once, and tell you after"
)
_MAY_STILL_BE = (
    "I can't cut out one exact line yet or confirm it's gone, so it may still be in your video"
)
_NOTE_STAYS_IN = "a sentence you restarted stays in"

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
}
_UNAVAILABLE = "Speech cleanup isn't available for this project yet, so the pauses stay"

_RETAKE_ROW = {"start_s": 16.405, "end_s": 20.14, "reason": "retake"}  # the Green 3 restart
_REQUIRED = {"speech_cleanup_contract": _FIXTURE["speech_cleanup_contract"]}


@pytest.fixture(autouse=True)
def _detection_off_unless_a_test_says_otherwise(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    # Run after the prod profile `_gate` applies: production has both retake levers on.
    request.getfixturevalue("_gate")
    monkeypatch.setattr(settings, "retake_cut_enabled", False)
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", False)
    # On in prod: job 9bad2d75 was stamped with `phone_text_style` (code default is off).
    monkeypatch.setattr(settings, "phone_text_styling_enabled", True)


def _detection(monkeypatch: pytest.MonkeyPatch, on: bool) -> None:
    monkeypatch.setattr(settings, "retake_cut_enabled", on)
    monkeypatch.setattr(settings, "speech_cleanup_retakes_enabled", on)


def _r2(**facts: Any) -> BriefRequirement:
    """The prod Green 3 r2 row with the facts the KRI-1189 prompt records for its words."""
    row = dict(_FIXTURE["brief_r2_turn1"])
    row["facts"] = {"speech_cleanup": True, "cut_restarts": True, **facts}
    return BriefRequirement.model_validate(row)


def _ask(description: str, **facts: Any) -> BriefRequirement:
    return BriefRequirement(
        id="r2", kind="style", scope="global", description=description, facts=facts
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
    req: BriefRequirement,
    *,
    plan: dict[str, Any] | None = None,
    language: str = "en",
) -> str:
    """The finished-render reply for a one-ask brief."""
    thread = SimpleNamespace(id=uuid.uuid4(), creator_id=uuid.uuid4())
    brief = CreativeBrief(version=1, requirements=[req])
    binding = BriefBinding.create(thread.id, brief).model_dump(mode="json")
    execution = SimpleNamespace(result={"brief_binding": binding})
    job = SimpleNamespace(id=uuid.uuid4(), assembly_plan=dict(_REQUIRED if plan is None else plan))
    with reply_language_for(language):
        text, _ = kria_runtime._approved_generation_review(
            None, thread, job, variant, execution, "Your edit is ready."
        )
    return text


def test_the_rows_are_the_prod_ask_with_the_typed_fact():
    prod = BriefRequirement.model_validate(_FIXTURE["brief_r2_turn1"])
    req = _r2()
    assert req.description == prod.description == _ASK
    assert (req.id, req.kind, req.scope) == (prod.id, prod.kind, prod.scope)
    assert prod.facts["cut_lines"] == [_TRANSCRIPT_LINE]
    assert "cut_lines" not in req.facts


# --- B: the draft -------------------------------------------------------------------------------


@pytest.mark.parametrize("on", [True, False], ids=["detection-on", "detection-off"])
@pytest.mark.parametrize("state", list(_LEADS))
def test_b1_b2_b3_the_draft_says_what_cleanup_will_do_with_the_restart(monkeypatch, state, on):
    _detection(monkeypatch, on)

    receipt = _receipt(_r2(), _draft(state))

    assert receipt.reason == f"{_LEADS[state][1]}; {_ALSO_CUTS if on else _STAYS}"
    assert _NAMED_PROMISE not in receipt.reason
    assert _MAY_STILL_BE not in receipt.reason
    _assert_limit_not_choice(receipt)  # B3: never met before the render


@pytest.mark.parametrize("on", [True, False], ids=["detection-on", "detection-off"])
def test_b4_cleanup_unavailable_adds_no_restart_promise(monkeypatch, on):
    _detection(monkeypatch, on)

    receipt = _receipt(
        _r2(),
        PlanFacts(edit_format="subtitled", phone_talking=True, speech_cleanup_offered=False),
    )

    assert receipt.reason == _UNAVAILABLE
    assert receipt.status == "partial"


@pytest.mark.parametrize("on", [True, False], ids=["detection-on", "detection-off"])
def test_b5_a_transcript_line_the_ask_never_says_does_not_turn_it_into_a_named_cut(monkeypatch, on):
    """The model still copies the take's words (the prod shape) next to the fact."""
    _detection(monkeypatch, on)
    req = _r2(cut_lines=[_TRANSCRIPT_LINE])
    assert named_cut_asks([req]) == ()  # the render drops it

    receipt = _receipt(req, _draft("offered"))

    assert receipt.reason == f"{_LEADS['offered'][1]}; {_ALSO_CUTS if on else _STAYS}"


def test_b6_a_named_line_and_a_restart_in_one_ask_report_both_parts(monkeypatch):
    """Çiğdem kit S3/T3: 'Cut the pauses, the "okay"s and the places I restarted.'"""
    _detection(monkeypatch, True)
    req = _ask(
        'Cut the pauses, the "okay"s and the places I restarted.',
        speech_cleanup=True,
        cut_restarts=True,
        cut_lines=["okay"],
    )
    assert named_cut_asks([req])  # the render keeps the named word

    receipt = _receipt(req, _draft("offered"))

    assert receipt.reason == f"{_LEADS['offered'][1]}; {_NAMED_PROMISE}; {_ALSO_CUTS}"
    _assert_limit_not_choice(receipt)


@pytest.mark.parametrize("on", [True, False], ids=["detection-on", "detection-off"])
def test_b9_a_turkish_chat_gets_the_turkish_restart_sentence(monkeypatch, on):
    """Çiğdem kit T2 (TR): "Boşlukları ve tekrar aldığım cümleyi kes."."""
    _detection(monkeypatch, on)
    req = _ask("Boşlukları ve tekrar aldığım cümleyi kes.", speech_cleanup=True, cut_restarts=True)

    with reply_language_for("tr"):
        receipt = _receipt(req, _draft("offered"))

    assert receipt.reason.endswith(_ALSO_CUTS_TR if on else _STAYS_TR), receipt.reason
    assert _ALSO_CUTS not in receipt.reason and _STAYS not in receipt.reason
    _assert_limit_not_choice(receipt)


# --- C: readiness -------------------------------------------------------------------------------


def test_c1_with_detection_on_green3_turn1_holds_nothing(monkeypatch):
    _open_windows(monkeypatch)
    _detection(monkeypatch, True)

    rows, report = _readiness(
        [R1, _r2(), R3, R4], _manifest(PHOTO, OTHER), background_media=GREEN3_BACKGROUND
    )

    assert rows["r2"]["disposition"] in APPROVABLE, rows["r2"]
    assert rows["r2"]["reason"].endswith(_ALSO_CUTS), rows["r2"]
    assert report["ready"] is True, report


def test_c2_with_detection_off_the_restart_still_holds_the_draft(monkeypatch):
    _open_windows(monkeypatch)
    _detection(monkeypatch, False)

    rows, report = _readiness(
        [R1, _r2(), R3, R4], _manifest(PHOTO, OTHER), background_media=GREEN3_BACKGROUND
    )

    assert rows["r2"]["disposition"] == "needs_choice", rows["r2"]
    assert rows["r2"]["reason"].endswith(_STAYS), rows["r2"]
    assert report["ready"] is False


def test_c3_the_turkish_also_cuts_sentence_is_deferred_too(monkeypatch):
    _detection(monkeypatch, True)
    req = _ask(
        "Baştaki sondaki boşlukları ve yanlış başladığım cümleleri kes.",
        speech_cleanup=True,
        cut_restarts=True,
    )

    with reply_language_for("tr"):
        rows, report = _readiness([req], _manifest())

    assert rows["r2"]["disposition"] in APPROVABLE, rows["r2"]
    assert rows["r2"]["reason"].endswith(_ALSO_CUTS_TR), rows["r2"]
    assert report["ready"] is True


# --- D: the finished render ---------------------------------------------------------------------


@pytest.mark.parametrize("on", [True, False], ids=["detection-on", "detection-off"])
def test_d1_a_retake_row_makes_the_restart_ask_done(monkeypatch, on):
    _detection(monkeypatch, on)

    receipt = _rendered(_r2(), retakes=1)
    text = _review(_variant(retakes=1), _r2())

    assert receipt.status == "met", receipt
    assert receipt.reason is None
    assert f"Done: {_ASK}" in text, text
    assert "Speech cleanup cut what it heard as 3 long pauses and 1 restarted sentence" in text
    assert _NOTE_STAYS_IN not in text


def test_d2_no_retake_row_with_detection_on_says_it_did_not_find_one(monkeypatch):
    """The take with no restart: only pauses go, and the chat says none was found."""
    _detection(monkeypatch, True)

    receipt = _rendered(_r2())
    text = _review(_variant(), _r2())

    assert receipt.reason == f"Speech cleanup cut the long pauses; {_NOT_FOUND}"
    _assert_limit_not_choice(receipt)
    assert _NOT_FOUND in text
    assert _NOTE_STAYS_IN not in text
    assert f"Done: {_ASK}" not in text


def test_d3_d5_no_retake_row_with_detection_off_says_stays_in_once(monkeypatch):
    receipt = _rendered(_r2())
    text = _review(_variant(), _r2())

    assert receipt.reason == f"Speech cleanup cut the long pauses; {_STAYS}"
    _assert_limit_not_choice(receipt)
    assert text.count(_NOTE_STAYS_IN) == 1, text


@pytest.mark.parametrize(
    ("contract", "expected"),
    [("required_v1", _STAYS), ("legacy_auto", _NOT_FOUND)],
)
def test_d4_receipt_and_note_read_detection_from_the_same_contract(monkeypatch, contract, expected):
    """Only RETAKE_CUT_ENABLED on (prod today): the older auto path detects retakes, the
    consented one does not, and the reply must not contradict itself either way."""
    monkeypatch.setattr(settings, "retake_cut_enabled", True)

    text = _review(_variant(), _r2(), plan={"speech_cleanup_contract": contract})

    assert expected in text, text
    assert (_NOTE_STAYS_IN in text) is (expected == _STAYS)
    assert text.count(_NOTE_STAYS_IN) <= 1, text


@pytest.mark.parametrize("on", [True, False], ids=["detection-on", "detection-off"])
def test_d7_a_retake_row_never_makes_a_brief_without_the_fact_met(monkeypatch, on):
    _detection(monkeypatch, on)
    prod = BriefRequirement.model_validate(_FIXTURE["brief_r2_turn1"])

    receipt = _rendered(prod, retakes=1)

    assert receipt.status == "partial", receipt
    assert _MAY_STILL_BE in receipt.reason


def test_d9_a_turkish_finished_reply(monkeypatch):
    _detection(monkeypatch, True)
    req = _ask("Boşlukları ve tekrar aldığım cümleyi kes.", speech_cleanup=True, cut_restarts=True)

    with reply_language_for("tr"):
        missed = _rendered(req)
        done = _rendered(req, retakes=1)

    assert missed.reason.endswith(_NOT_FOUND_TR), missed.reason
    assert done.status == "met"
    text = _review(_variant(retakes=1), req, language="tr")
    assert "1 baştan başlanan cümle" in text, text


# --- A8: both prompts carry the instruction -----------------------------------------------------


def test_a8_main_creator_and_the_brief_extractor_both_record_cut_restarts():
    from app.agents.brief_extractor import _NARROW_BRIEF_SECTION
    from app.agents.main_creator import _BRIEF_PROMPT_SECTION

    for section in (_BRIEF_PROMPT_SECTION, _NARROW_BRIEF_SECTION):
        assert '"cut_restarts": true' in section
        # The transcript is never a source of cut lines (the Green 3 r2 shape).
        assert "never copy words from the take" in section
