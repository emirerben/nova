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


# KRI-1348 (yorum3): pre-roll is read by word role, not by phrase
# (docs/reviews/kri-1348-preroll-failure-modes.md).

# The yorum3 take's opening as production whisper-1 stamped it (job 7eba9903, IMG_7212).
_YORUM3 = [
    {"text": "Okay,", "start_s": 0.0, "end_s": 0.46},
    {"text": "starting.", "start_s": 0.84, "end_s": 1.26},
    {"text": "Ten", "start_s": 2.16, "end_s": 2.28},
    {"text": "minutes", "start_s": 2.28, "end_s": 2.62},
    {"text": "after", "start_s": 2.62, "end_s": 2.98},
    {"text": "Lumo", "start_s": 2.98, "end_s": 3.18},
    # Later in the take: "Sorry, let me start that again." must not save the opening (F5).
    {"text": "Sorry,", "start_s": 15.0, "end_s": 15.3},
    {"text": "let", "start_s": 15.3, "end_s": 15.5},
    {"text": "me", "start_s": 15.5, "end_s": 15.6},
    {"text": "start", "start_s": 15.6, "end_s": 15.9},
    {"text": "again.", "start_s": 15.9, "end_s": 16.2},
]


def test_yorum3_okay_starting_is_cut():
    assert opening_slate_span(_YORUM3) == (0, 1)


@pytest.mark.parametrize(
    ("text", "cut"),
    [
        ("Okay, let's start. Apple just dropped a new ad.", "Okay, let's start."),
        ("Alright, let's get started. Apple just dropped a new ad.", "Alright, let's get started."),
        ("Testing, testing. One, two. Apple just dropped a new ad.", "Testing, testing. One, two."),
        ("Wait, the light... okay. Why do brands do food marketing?", "Wait, the light... okay."),
        ("Okay, camera rolling. Apple just dropped a new ad.", "Okay, camera rolling."),
        ("We're recording. Okay. So today we talk about Lumo.", "We're recording. Okay."),
        ("Başlıyorum. Fenerbahçe Kadıköy'den taşınıyor.", "Başlıyorum."),
        ("Hadi başlayalım. Fenerbahçe Kadıköy'den taşınıyor.", "Hadi başlayalım."),
        (
            "Tamam, kayıt başladı mı? Evet. Fenerbahçe Kadıköy'den taşınıyor.",
            "Tamam, kayıt başladı mı? Evet.",
        ),
        ("Bir, iki, üç. Fenerbahçe Kadıköy'den taşınıyor.", "Bir, iki, üç."),
        ("Ses kontrol, bir iki. Fenerbahçe Kadıköy'den taşınıyor.", "Ses kontrol, bir iki."),
        ("Çekiyor mu? Tamam. Fenerbahçe Kadıköy'den taşınıyor.", "Çekiyor mu? Tamam."),
    ],
)
def test_new_preroll_phrasings_en_tr_are_cut(text, cut):
    words = _words(text)
    span = opening_slate_span(words)
    assert span is not None and span[0] == 0
    assert " ".join(w["text"] for w in words[: span[1] + 1]) == cut


@pytest.mark.parametrize(
    "text",
    [
        "Is this on? Apple just dropped a new ad.",
        "Let's go. Apple just dropped a new ad.",
        "Hazır mıyız? Fenerbahçe Kadıköy'den taşınıyor.",
        "Bir saniye. Fenerbahçe Kadıköy'den taşınıyor.",
    ],
)
def test_cue_only_opening_needs_a_fresh_start_pause(text):
    # F3: a cue-only utterance flowing into the content is a hook; after a clear pause it is
    # pre-roll.
    assert opening_slate_span(_words(text)) is None
    n_cue = len(text.split("?")[0].split(".")[0].split())
    assert opening_slate_span(_words(text, gaps={n_cue - 1: 0.8})) == (0, n_cue - 1)


@pytest.mark.parametrize(
    ("text", "gaps"),
    [
        # F1: filler opening of a real sentence.
        ("Okay, so here's the thing. I tried it for a week.", None),
        ("Tamam, bugün size Lumo'yu anlatacağım. Çok güzel.", None),
        # F2: a production word inside a real sentence.
        ("Starting today I'm vegan. Here's why it matters.", None),
        ("Testing a new camera. It is really small.", None),
        # F5: the video is about the production noun.
        ("Mic check. This mic costs fifty dollars and it is great.", None),
        ("Kamera hazır. Bu kamera gerçekten çok küçük.", None),
        # F6: a lone number is a hook, even after a pause.
        ("Five. Five things nobody tells you about Lisbon.", {0: 0.9}),
        # F7/F8: Turkish number word and stem collisions inside real sentences.
        ("Bir şey söyleyeceğim. Lumo çok iyi.", None),
        ("Çekici bir kampanya. Herkes bunu konuştu.", None),
        ("Sesli düşünüyorum. Lumo çok iyi bir telefon.", None),
        # F9: greetings are the creator's real intro.
        ("Hi guys! Today we talk about Lumo.", {1: 0.9}),
        ("Selam! Bugün Lumo'yu konuşuyoruz.", {0: 0.9}),
        # F2: a production word used as a noun, after an article.
        ("That's a record. Nobody has done this before.", {2: 0.9}),
        ("It's a start. Here is what we built.", {2: 0.9}),
        ("Bu bir kayıt. Herkes bunu dinlemeli.", {2: 0.9}),
        # F3: hooks and apologies are not cues, even after a dramatic pause.
        ("Wait. This changes everything about Lumo.", {0: 0.9}),
        ("Ready? Apple just dropped a new ad.", {0: 0.9}),
        ("Sorry. I owe you all an explanation.", {0: 0.9}),
        ("I'm sorry. I owe you all an explanation.", {1: 0.9}),
        ("Dur! Bunu bilmeden telefon alma.", {0: 0.9}),
        # F10: pre-roll after a real first sentence.
        ("Lumo launched a phone. Okay, starting again. It folds.", None),
    ],
)
def test_real_first_sentences_are_never_cut(text, gaps):
    assert opening_slate_span(_words(text, gaps=gaps)) is None


def test_multi_utterance_preroll_is_cut_as_one_span():
    # F12: several short production utterances in a row ("Is this on? … Okay. Three, two, one.").
    words = _words("Is this on? Okay. Three, two, one. Apple just dropped a new ad.", gaps={2: 0.8})
    assert opening_slate_span(words) == (0, 6)


def test_preroll_after_a_silent_lead_in_is_still_cut():
    # Press record, look at the screen for 3 s, then "Okay... starting." (Çiğdem T1 script).
    words = _words("Okay... starting. Ten minutes after Lumo launched.", start=3.0)
    assert opening_slate_span(words) == (0, 1)


def test_preroll_must_end_early_in_the_take():
    words = _words("Okay... starting. Ten minutes after Lumo launched.", start=8.5)
    assert opening_slate_span(words) is None


# Found by the synthetic-opening replay (macOS `say` + whisper.cpp, KRI-1348): the shapes whisper
# really gives when each word of the opening is its own little utterance.


def _stamped(rows):
    return [{"text": t, "start_s": s, "end_s": e} for t, s, e in rows]


_LUMO = [("Ten", 2.6, 2.8), ("minutes", 2.8, 3.2), ("after", 3.2, 3.4), ("Lumo", 3.4, 3.7)]


def test_countdown_split_by_pauses_adds_up():
    words = _stamped([("Three,", 0.07, 0.29), ("two,", 0.29, 0.85), ("one.", 0.85, 1.54), *_LUMO])
    silences = [(0.39, 0.68), (0.99, 1.23), (1.59, 2.44)]
    assert opening_slate_span(words, silences=silences) == (0, 2)


def test_cue_is_settled_by_the_pause_before_the_real_talk():
    # "Is this on? Okay. [1 s] Ten minutes…": the pause follows "Okay.", not "on?".
    rows = [("Is", 0.0, 0.18), ("this", 0.18, 0.54), ("on?", 0.54, 1.0), ("Okay.", 1.0, 2.0)]
    lumo = [(t, s - 0.6, e - 0.6) for t, s, e in _LUMO]  # "Ten" stamped over the pause at 2.0
    words = _stamped([*rows, *lumo])
    assert opening_slate_span(words, silences=[(0.72, 0.97), (1.49, 2.54)]) == (0, 3)
    # Same words, no pause before the talk: a hook, kept.
    assert opening_slate_span(words, silences=[(0.72, 0.97)]) is None


def test_lets_go_with_stamps_stretched_over_the_pause():
    # Whisper put "go." over the 1 s pause; the silence spans both words' stamps.
    words = _stamped([("Let's", 0.01, 1.0), ("go.", 1.0, 1.7), *_LUMO])
    words[2:] = [{**w, "start_s": w["start_s"] - 0.9, "end_s": w["end_s"] - 0.9} for w in words[2:]]
    assert opening_slate_span(words, silences=[(0.65, 1.7)]) == (0, 1)
    assert opening_slate_span(words, silences=[(0.65, 0.78)]) is None


def test_question_to_the_viewer_is_a_hook_even_after_a_pause():
    # F3: "you" / "siz" / "mısınız" address the audience; no production role.
    hooks = ("Hazır mısınız? Fenerbahçe Kadıköy'den taşınıyor.", "Are you ready? Apple did it.")
    for text in hooks:
        n = len(text.split("?")[0].split())
        assert opening_slate_span(_words(text, gaps={n - 1: 1.0})) is None


def test_repeated_check_word_is_not_the_videos_topic():
    # "Testing, testing, one, two." — the second "testing" is still the opening, not the topic.
    words = _words("Testing, testing, one, two. Apple just dropped a new ad.", gaps={3: 0.8})
    assert opening_slate_span(words) == (0, 3)
