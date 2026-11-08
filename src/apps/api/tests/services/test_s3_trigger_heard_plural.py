"""KRI-1327 (Çiğdem S3): the photo trigger "Nar" was heard as "NARS" and "café" as "cafes.",
so two of four full-screen photos never showed although the creator said both words.

Failure modes (written before the code):

P1  A brand or noun the creator names in the singular is heard with a plural / possessive "s"
    ("NARS", "cafes") and never matches.
P2  The tolerance turns a short word into a different word ("is" for "i", "bus" for "bu").
P3  An exact hearing elsewhere loses to a plural hearing (exact always wins).
P4  A multi-word trigger matches with only its last word pluralised but another word wrong.
"""

from __future__ import annotations

import pytest

from app.services.phone_reaction_grounding import _phrase_windows, _token_stream


def _words(*spans):
    return [{"text": t, "start_s": s, "end_s": e, "confidence": 1.0} for t, s, e in spans]


S3 = _words(
    ("Like", 11.2, 11.4),
    ("NARS", 11.5, 11.9),
    ("lip", 11.9, 12.1),
    ("tint", 12.1, 12.4),
    ("luxury", 28.7, 29.0),
    ("brands", 29.0, 29.3),
    ("opening", 29.3, 29.6),
    ("cafes.", 29.6, 30.0),
)


@pytest.mark.parametrize(("trigger", "start"), [("Nar", 11.5), ("café", 29.6), ("Nar lip tint", 11.5)])
def test_a_trigger_heard_with_a_plural_s_is_still_heard(trigger, start):
    windows = _phrase_windows(_token_stream(S3), S3, trigger)
    assert [round(w[0], 2) for w in windows] == [start]


def test_an_exact_hearing_wins_over_a_plural_one():
    words = _words(("cafes", 1.0, 1.4), ("the", 5.0, 5.1), ("café", 5.1, 5.5))
    assert [w[0] for w in _phrase_windows(_token_stream(words), words, "café")] == [5.1]


@pytest.mark.parametrize(
    ("heard", "trigger"),
    [("is", "i"), ("bus", "bu"), ("Mars", "Nar"), ("tins", "tint"), ("lips tint", "lip tent")],
)
def test_no_other_word_is_read_as_the_trigger(heard, trigger):
    words = _words(*[(w, i * 0.5, i * 0.5 + 0.4) for i, w in enumerate(heard.split())])
    assert _phrase_windows(_token_stream(words), None, trigger) == []
