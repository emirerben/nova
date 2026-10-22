"""KRI-760: "<quoted line> dediği cümle baştan sona duyulsun" keeps a spoken line whole.

Incident (prod thread A9100235, Merope café M1, 2026-10-19): the brief filed r5 as
``kind=audio, facts={"triggers": ["Hi, I'm Karolina, I'm from Ecuador"], "keep_whole_take":
True}``. ``_wants_beats`` saw the triggers fact and sent it to the pop-in checker, which
refused it on montage ("Photo and sound pop-ins timed to your words aren't available").

Ways this can fail, each pinned below (written before the code):
1. The ask as the model filed it (audio + triggers + keep_whole_take) is judged as a pop-in.
2. The same ask filed as another kind, or with the line only in quotes, Turkish or English.
3. A real pop-in ask loses its beat check: a photo or sound on a word, a word-triggered cue,
   or a pop-in that also says "keep my whole take".
4. A recorded ``keep_whole_take: false`` is read as "keep it whole".
5. Speech cleanup, captions or source-sound checkers swallow the ask ("kesilmesin" has `kes`).
6. The draft (nothing rendered yet) reports a failure for something only the render can judge.

KRI-803 (prod thread F4083F1D, Don Julio kit M1, 2026-10-21): "keep his name exchange audible
("What's your name?" "Lucas.")" was filed as ``kind=audio, facts={"audio_intent":
"source_unchanged", "triggers": [...]}`` with no ``keep_whole_take``, and refused as a pop-in.
7. The source-sound fact with the lines recorded as `triggers` is still judged as a pop-in.
8. A pop-in ask that also records `source_unchanged` (a sound or photo on a word) loses its
   beat check, or quoted words the model did NOT record as spoken (a clip's name) become a line.
"""

from __future__ import annotations

import pytest

from app.kria.brief import BriefRequirement
from app.kria.brief_checks import (
    PlanFacts,
    _wants_beats,
    _wants_cleanup,
    _wants_closing_speech,
    check_requirement,
    is_judged,
    wants_whole_line,
)

LINE = "Hi, I'm Karolina, I'm from Ecuador"
TR_ASK = f'"{LINE}" dediği cümle baştan sona duyulsun'
# What phone montage reports (reaction beats are not offered there).
MONTAGE = {"reaction_beats_available": False, "reaction_beats": (), "edit_format": "montage"}


def _req(kind: str, description: str, *, scope: str = "global", **facts) -> BriefRequirement:
    return BriefRequirement(id="r5", kind=kind, scope=scope, description=description, facts=facts)


def _filed_by_prod() -> BriefRequirement:
    return _req("audio", TR_ASK, triggers=[LINE], keep_whole_take=True)


WHOLE_LINE_ASKS = [
    pytest.param(_filed_by_prod, id="prod-r5-audio-triggers-keep-whole-take"),
    pytest.param(
        lambda: _req("timing", TR_ASK, triggers=[LINE], keep_whole_take=True), id="as-timing"
    ),
    pytest.param(
        lambda: _req("style", TR_ASK, triggers=[LINE], keep_whole_take=True), id="as-style"
    ),
    pytest.param(
        lambda: _req("select", TR_ASK, triggers=[LINE], keep_whole_take=True), id="as-select"
    ),
    pytest.param(
        lambda: _req(
            "audio", f"Let the line “{LINE}” play in full", triggers=[LINE], keep_whole_take=True
        ),
        id="en-with-facts",
    ),
    # The model recorded the flag but not the triggers: the quoted words are the line.
    pytest.param(
        lambda: _req("audio", f'Let the line "{LINE}" be heard in full', keep_whole_take=True),
        id="quoted-words-with-the-flag",
    ),
    pytest.param(
        lambda: _req("audio", TR_ASK, keep_whole_take="true"), id="flag-spelled-as-a-string"
    ),
    # 7: KRI-803, r6 / r7 exactly as prod filed them.
    pytest.param(
        lambda: _req(
            "audio",
            "Keep waiter's name exchange audible",
            audio_intent="source_unchanged",
            triggers=["What's your name?", "Lucas."],
        ),
        id="kri803-r6-source-unchanged-triggers",
    ),
    pytest.param(
        lambda: _req(
            "audio",
            'Keep that "Cheers!" audible too',
            audio_intent="source_unchanged",
            triggers=["Cheers!"],
        ),
        id="kri803-r7-one-word-line",
    ),
    pytest.param(
        lambda: _req("audio", TR_ASK, audio_intent="source_unchanged", triggers=[LINE]),
        id="kri803-shape-in-turkish",
    ),
]


@pytest.mark.parametrize("make", WHOLE_LINE_ASKS)
def test_a_named_line_heard_in_full_is_a_whole_line_ask_never_a_pop_in(make) -> None:
    req = make()
    assert wants_whole_line(req)
    assert not _wants_beats(req)
    assert not _wants_cleanup(req)  # "kesilmesin" must not read as a cleanup ask


@pytest.mark.parametrize("make", WHOLE_LINE_ASKS)
def test_the_montage_receipt_never_calls_it_a_pop_in_or_not_possible(make) -> None:
    receipt = check_requirement(make(), PlanFacts(**MONTAGE))
    assert receipt.status != "not_possible"
    assert "pop-in" not in (receipt.reason or "").lower()
    assert "fotoğraf" not in (receipt.reason or "").lower()
    # Nothing is rendered yet: the draft stays neutral instead of reporting a miss (6).
    assert not is_judged(make(), receipt)


def test_a_pop_in_ask_on_montage_is_still_refused_with_the_beat_message() -> None:
    # The verdict this ticket took AWAY from the whole-line ask stays for a real pop-in.
    other = _req("style", f"Show the photo when I say '{LINE}'", triggers=[LINE])
    reason = check_requirement(other, PlanFacts(**MONTAGE)).reason or ""
    assert "Photo and sound pop-ins timed to your words aren't available" in reason


def test_baştan_sona_is_not_an_ending() -> None:
    # The closing-line checker read "baştan sona" ("from start to finish") as "at the end".
    assert not _wants_closing_speech(_filed_by_prod())
    assert _wants_closing_speech(
        _req("order", f'En sonda "{LINE}" dediği klip olsun', last_clip="Karolina dediği klip")
    )


POP_IN_ASKS = [
    pytest.param(
        lambda: _req("style", "'pasta' dediğimde fotoğraf çıksın", triggers=["pasta"]),
        id="tr-photo-on-word",
    ),
    pytest.param(
        lambda: _req("style", "When I say 'pasta' show the photo", triggers=["pasta"]),
        id="en-photo-on-word-facts",
    ),
    pytest.param(lambda: _req("style", "When I say 'pasta' show the photo"), id="en-photo-words"),
    pytest.param(
        lambda: _req("audio", "her 'kahve' kelimesinde fincan sesi çalsın", triggers=["kahve"]),
        id="tr-sound-on-word",
    ),
    pytest.param(
        lambda: _req("audio", "'zil' dediğimde zil sesi efekti çalsın", triggers=["zil"]),
        id="tr-sound-effect",
    ),
    pytest.param(
        lambda: _req(
            "style",
            "Show the medal photo when I say 'the medal' and keep the whole take",
            triggers=["the medal"],
            keep_whole_take=True,
        ),
        id="pop-in-that-also-keeps-the-take",
    ),
    pytest.param(
        lambda: _req(
            "style", "'Karolina' dediğimde çıkartma çıksın, baştan sona", triggers=["Karolina"]
        ),
        id="word-triggered-cue-with-whole-wording",
    ),
    pytest.param(
        # The model said "no": a trigger fact with keep_whole_take false is a pop-in ask.
        lambda: _req("style", TR_ASK, triggers=[LINE], keep_whole_take=False),
        id="keep-whole-take-false",
    ),
    # 8: a sound or photo on a word stays a pop-in even with the source sound kept.
    pytest.param(
        lambda: _req(
            "audio",
            "When I say 'cheers' play a glass clink, keep the original audio",
            audio_intent="source_unchanged",
            triggers=["cheers"],
        ),
        id="kri803-sound-on-word-with-source-kept",
    ),
    pytest.param(
        lambda: _req(
            "audio",
            "her 'kahve' kelimesinde fincan sesi çalsın",
            audio_intent="source_unchanged",
            triggers=["kahve"],
        ),
        id="kri803-tr-sound-on-word-with-source-kept",
    ),
    pytest.param(
        lambda: _req(
            "audio",
            "Pop up the medal photo on 'the medal', original sound on",
            audio_intent="source_unchanged",
            triggers=["the medal"],
        ),
        id="kri803-photo-on-word-with-source-kept",
    ),
]


@pytest.mark.parametrize("make", POP_IN_ASKS)
def test_real_pop_in_asks_still_route_to_the_beat_checker(make) -> None:
    req = make()
    assert not wants_whole_line(req)
    assert _wants_beats(req)
    receipt = check_requirement(req, PlanFacts(**MONTAGE))
    assert receipt.status == "not_possible"
    assert "pop-in" in (receipt.reason or "").lower()


def test_a_line_with_no_flag_stays_what_it_was() -> None:
    # No `keep_whole_take`: no new wording is read (KRI-667); the ask keeps its old path.
    for req in (
        _req("audio", f'"{LINE}" dediği cümle'),
        _req("audio", TR_ASK),
        _req("audio", f'Let the line "{LINE}" be heard in full'),
        _req("audio", TR_ASK, keep_whole_take=False),
    ):
        assert not wants_whole_line(req)
        assert not _wants_beats(req)  # words alone never made this a pop-in either


def test_a_whole_take_with_no_named_line_is_the_old_timing_ask() -> None:
    req = _req("timing", "Videonun hepsi kalsın", keep_whole_take=True)
    assert not wants_whole_line(req)


def test_the_source_sound_fact_needs_the_lines_recorded_as_spoken() -> None:
    # 8: KRI-803 routes on the recorded `triggers`, never on quoted words alone: a quoted
    # clip name is not a line. A recorded keep_whole_take false still says no.
    for req in (
        _req(
            "audio",
            'Keep the original audio on the "Beach day" clip',
            audio_intent="source_unchanged",
        ),
        _req(
            "audio",
            TR_ASK,
            audio_intent="source_unchanged",
            triggers=[LINE],
            keep_whole_take=False,
        ),
        _req("audio", TR_ASK, audio_intent="processed", triggers=[LINE]),
    ):
        assert not wants_whole_line(req)
