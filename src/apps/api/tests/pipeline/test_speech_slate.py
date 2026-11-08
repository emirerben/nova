"""Production slate at the start of a take (docs/reviews/cigdem-s2-failure-modes.md, A1-A12)."""

from __future__ import annotations

import pytest

from app.pipeline.speech_slate import opening_slate_span


def _words(
    text: str, *, start: float = 0.0, step: float = 0.32, gaps: dict[int, float] | None = None
):
    out, t = [], start
    for i, token in enumerate(text.split()):
        out.append({"text": token, "start_s": round(t, 3), "end_s": round(t + step - 0.04, 3)})
        t += step + (gaps or {}).get(i, 0.0)
    return out


# The real S2 "ss2b" take's opening, as the verbatim transcript stamped it.
_SS2B = [
    {"text": "Okay,", "start_s": 0.0, "end_s": 0.3},
    {"text": "we're", "start_s": 0.36, "end_s": 0.6},
    {"text": "recording.", "start_s": 0.6, "end_s": 1.04},
    {"text": "Kervan", "start_s": 1.54, "end_s": 1.9},
    {"text": "celebrated", "start_s": 1.9, "end_s": 2.4},
    {"text": "its", "start_s": 2.4, "end_s": 2.5},
    {"text": "10th", "start_s": 2.5, "end_s": 2.8},
]


def test_ss2b_opening_is_cut():
    assert opening_slate_span(_SS2B) == (0, 2)


@pytest.mark.parametrize(
    "text",
    [
        "Tamam, kayıttayız. Fenerbahçe Kadıköy'den taşınıyor ve herkes konuşuyor.",
        "TAMAM KAYITTAYIZ. Fenerbahçe Kadıköy'den taşınıyor.",
        "Three, two, one. Apple just dropped a new ad.",
        "Is it recording? Okay. Samsung replied to Apple.",
        "We're rolling. So today we talk about Lumo.",
    ],
)
def test_slate_phrases_en_tr_are_cut(text):
    words = _words(text)
    span = opening_slate_span(words)
    assert span is not None and span[0] == 0
    kept = " ".join(w["text"] for w in words[span[1] + 1 :])
    assert kept and not kept.lower().startswith(("kayıt", "three", "recording", "rolling"))


@pytest.mark.parametrize(
    "text",
    [
        # A1: a real sentence that contains a slate word.
        "We're recording our first album today and it is wild.",
        # A2: generic short hooks are not slates.
        "Ready? Apple did it again with this ad.",
        "Here we go. Samsung answered Apple in public.",
        "Action! That is what this campaign is about.",
        "Let's go. Kervan turned ten this year.",
        # A4: a slate that is not the opening.
        "Kervan turned ten. Okay, we're recording again. Lumo was the partner.",
    ],
)
def test_real_openings_are_never_cut(text):
    assert opening_slate_span(_words(text)) is None


def test_slate_glued_to_the_first_sentence_is_not_cut():
    # A3: no punctuation, no gap, no silence: the utterance has no boundary, so nothing is cut.
    words = _words("okay we're recording kervan celebrated its tenth year")
    assert opening_slate_span(words) is None


def test_boundary_from_a_gap_or_a_silence():
    words = _words("okay we're recording kervan celebrated its tenth year", gaps={2: 0.4})
    assert opening_slate_span(words) == (0, 2)
    # Whisper stamps stretched over the pause: no gap, but a detected silence right after.
    words = _words("okay we're recording kervan celebrated its tenth year")
    end = words[2]["end_s"]
    assert opening_slate_span(words, silences=[(end + 0.02, end + 0.4)]) == (0, 2)


def test_take_that_is_only_a_slate_is_not_cut():
    # A5: at least three words must stay.
    assert opening_slate_span(_words("Okay, we're recording. Hi there.")) is None


def test_slow_preamble_is_not_a_slate():
    # A7: a slate ends within 3 s.
    words = _words("Okay, we're recording. Kervan turned ten this year.", step=1.4)
    assert opening_slate_span(words) is None


@pytest.mark.parametrize("words", [[], [{"text": "", "start_s": 0, "end_s": 1}], [{"x": 1}]])
def test_malformed_words_never_raise(words):
    # A12
    assert opening_slate_span(words) is None


def test_a_later_silence_does_not_end_the_utterance_early():
    # ss2b's real silence (1.03-1.55 s) follows "recording", not "we're": the slate is all three.
    assert opening_slate_span(_SS2B, silences=[(1.03175, 1.54725)]) == (0, 2)
    # Stretched stamps: "recording" runs over the pause; the silence inside it is the boundary.
    stretched = [dict(w) for w in _SS2B]
    stretched[2] = {"text": "recording", "start_s": 0.6, "end_s": 1.54}
    assert opening_slate_span(stretched, silences=[(1.03, 1.54)]) == (0, 2)


@pytest.mark.parametrize(
    ("text", "last"),
    [
        ("Okay. We're recording. Kervan turned ten this year.", 2),
        ("Tamam. Kayıttayız. Fenerbahçe Kadıköy'den taşınıyor.", 1),
    ],
)
def test_filler_said_as_its_own_sentence_before_the_slate(text, last):
    # Review of #2230: Whisper often writes the leading "Okay." as its own sentence. That
    # filler-only utterance must not end the search: the slate after it is still the opening.
    words = _words(text)
    assert opening_slate_span(words) == (0, last)


def test_filler_only_opening_before_a_real_sentence_is_not_cut():
    # Negative control for the above: "Okay." followed by the real talk cuts nothing.
    assert opening_slate_span(_words("Okay. Kervan turned ten this year.")) is None
