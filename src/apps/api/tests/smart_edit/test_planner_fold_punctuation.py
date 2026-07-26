"""Workstream 3a downstream audit: with punctuation now riding the timed word
stream (align_punctuated_text in transcribe.py), `smart_edit/planner.py`'s
`_fold` (and the `normalized_text` it feeds via `_normalize_captions`) must
already strip punctuation to word characters. Per the plan this is a
no-change-expected verification, not a fix — pinned here so a future edit to
`_fold`/`_WORD_RE` can't silently reintroduce punctuation sensitivity.
"""

from __future__ import annotations

from app.smart_edit import planner as sp


def test_fold_already_strips_all_punctuation() -> None:
    assert sp._fold("UserMagick,") == sp._fold("UserMagick")
    assert sp._fold("great.") == sp._fold("great")
    assert sp._fold("200,000") == "200 000"  # non-word separators become spaces
    # Apostrophes (not in _WORD_RE's [a-z0-9]+) split into two space-joined
    # tokens — pre-existing behavior, unrelated to punctuation restoration.
    assert sp._fold("that's") == "that s"


def test_normalize_captions_normalized_text_is_punctuation_insensitive() -> None:
    cues = [
        {
            "text": "Usersmagic, that's great.",
            "start_s": 0.0,
            "end_s": 1.5,
            "words": [
                {"text": "Usersmagic,", "start_s": 0.0, "end_s": 0.5},
                {"text": "that's", "start_s": 0.5, "end_s": 1.0},
                {"text": "great.", "start_s": 1.0, "end_s": 1.5},
            ],
        }
    ]
    words, _baseline, _cue_word_ids = sp._normalize_captions(cues, language="en")
    normalized = [w.normalized_text for w in words]
    assert normalized == ["usersmagic", "that s", "great"]
    # display_text keeps the punctuation for on-screen rendering.
    assert [w.display_text for w in words] == ["Usersmagic,", "that's", "great."]
