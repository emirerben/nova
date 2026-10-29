"""KRI-978 + KRI-970: the finished-render cleanup note says what cleanup did, and only that.

Don Julio lunch T3 (job 0d8d644f, 2026-10-27): the take was cut to the creator's quoted
start/end lines AND cleaned up. ``variants[0].silence_cut`` carries all three removals in
one list (opening 0-3.41, filler 5.12-5.44, end 10.53-48.168), and the review read the
duration off ``time_saved_s``: "Speech cleanup cut 1 filler sound (41.4 s)" for a 0.32 s
cut. Both runs also told the creator "It only cuts pauses and filler sounds" right after
it had cut the middle of "provolone" (KRI-970).

How this could fail (written before the code):
- the seconds still include the quoted-line trim (opening_cut / end_cut);
- a record with no trims changes its number (T1 said 0.8 s and was right);
- a record holding only trims, no cleanup row, starts claiming a cleanup cut;
- the Turkish copy keeps the old figure or the old claim;
- the note still states as fact that cleanup only ever cuts pauses and filler sounds;
- the restart limit (KRI-551) disappears with the wording change.
"""

from __future__ import annotations

from app.kria.brief_checks import speech_cleanup_review_note
from app.kria.reply_language import bind_reply_language, release_reply_language

# variants[0].silence_cut exactly as prod stored them.
_T3_CUT = {
    "removed": [
        {"end_s": 3.41, "reason": "opening_cut", "start_s": 0.0},
        {"end_s": 5.44, "reason": "filler_lexical", "start_s": 5.12},
        {"end_s": 48.168, "reason": "end_cut", "start_s": 10.53},
    ],
    "version": 2,
    "time_saved_s": 41.368,
    "original_duration_s": 48.168,
}
_T1_CUT = {
    "removed": [
        {"end_s": 2.355, "reason": "silence", "start_s": 2.13},
        {"end_s": 3.58, "reason": "filler_lexical", "start_s": 3.02},
    ],
    "version": 2,
    "time_saved_s": 0.785,
    "original_duration_s": 8.2,
}


def _variant(cut: dict) -> dict:
    return {
        "variant_id": "subtitled",
        "resolved_archetype": "subtitled",
        "silence_cut_outcome": "applied",
        "silence_cut": cut,
    }


def _note(cut: dict, language: str = "en") -> str:
    token = bind_reply_language(language)
    try:
        note = speech_cleanup_review_note(_variant(cut))
    finally:
        release_reply_language(token)
    assert note is not None
    return note


def test_t3_reports_the_filler_cut_not_the_quoted_line_trim():
    note = _note(_T3_CUT)
    assert "(0.3 s)" in note
    assert "41.4" not in note
    assert "1 filler sound" in note


def test_t1_without_a_trim_keeps_its_number():
    assert "(0.8 s)" in _note(_T1_CUT)


def test_turkish_copy_reports_the_same_figure():
    note = _note(_T3_CUT, "tr")
    assert "(0,3 sn)" in note
    assert "41,4" not in note


def test_trims_alone_are_not_reported_as_a_cleanup_cut():
    cut = {**_T3_CUT, "removed": [_T3_CUT["removed"][0], _T3_CUT["removed"][2]]}
    note = _note(cut)
    assert "filler" not in note.split(".")[0]
    assert not any(char.isdigit() for char in note)


def test_the_note_never_states_cleanup_only_cuts_pauses_as_fact():
    for language in ("en", "tr"):
        note = _note(_T3_CUT, language)
        assert "It only cuts" not in note
        assert "Yalnızca duraklamaları ve dolgu seslerini keser" not in note
    note = _note(_T3_CUT)
    assert "what it heard as" in note
    assert "a sentence you restarted stays in" in note
