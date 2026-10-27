"""KRI-961: a phrase the creator named, heard with one word more or fewer.

Don Julio N1 (prod job bdb0c798, phone Voiceover): the creator says "ojo de bife" and asked
to spell it so and to show the ribeye photo while saying it. Whisper heard two words,
"Olloda bife", so the caption kept "Olloda bife" (the KRI-793 phrase match needs the same
word count) and the corner card was "never heard" (the beat matcher is exact).

One deterministic matcher, ``caption_spelling.find_named_phrase``, now finds the phrase in
the heard words for both the captions and the card. Failure modes, written before the code:

FM1  Miss: the heard span has one word fewer ("Olloda bife") or more ("ojo de bi fe") than
     the phrase, so captions keep the misspelling and the card never shows (the bug).
FM2  False rewrite of an unrelated span ("the butcher cuts"), or of a span that only shares
     a short word with the phrase.
FM3  A phrase that is not said at all is "heard" somewhere.
FM4  A one-word phrase swallows a neighbour ("Mero pe", "Meropa how"): a one-word phrase
     only ever matches one heard word.
FM5  Turkish: the dotless/dotted i and diacritics fold differently from the shared fold, a
     Turkish case ending is lost, or a Turkish sentence that does not say the phrase is
     rewritten.
FM6  Timings: after a split or merge the words overlap, run backwards, leave the heard
     span, lose their other fields, or the cue text no longer equals the words joined.
FM7  A span runs across punctuation ("Olloda, bife") or a case ending mid-phrase.
FM8  Already right ("ojo de bife", "Ojo de Bife") is touched: no case-only edits, and
     applying twice changes nothing more.
FM9  A near span overlapping an exact one, or two candidate sizes at one place, both count.
FM10 Draft and render disagree: the card matcher and the caption respelling hear different
     things (covered end to end in tests/tasks/test_kri961_narrated_named_phrase.py).
FM11 Capitalisation: mid-sentence "the Olloda bife" must not become "the Ojo de bife".
FM12 The only exact word shared with the phrase is a short function word ("de"): no anchor.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.pipeline.caption_spelling import (
    apply_spelling_to_cues,
    find_named_phrase,
    is_mishearing,
    respell_text,
)

RUN = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "kri961_don_julio_n1_narrated.json").read_text(
        "utf-8"
    )
)
PHRASE = "ojo de bife"
WHISPER = RUN["whisper_words"]
GEMINI = RUN["gemini_words"]
HEARD_LINE = RUN["provenance"]["rendered_caption"]


def _texts(words):
    return [w["text"] for w in words]


def _cue(words):
    return {
        "text": " ".join(w["text"] for w in words),
        "start_s": words[0]["start_s"],
        "end_s": words[-1]["end_s"],
        "words": [dict(w) for w in words],
    }


def _line_words(first: str, last: str) -> list[dict]:
    texts = _texts(WHISPER)
    start = texts.index(first)
    stop = texts.index(last, start) + 1
    return WHISPER[start:stop]


# --- FM1: this run ------------------------------------------------------------


def test_this_runs_whisper_words_say_the_phrase_once_as_two_words():
    [hit] = find_named_phrase(_texts(WHISPER), PHRASE)
    assert _texts(WHISPER)[hit.start : hit.stop] == ["Olloda", "bife"]
    assert hit.exact is False


def test_gemini_heard_it_right_and_that_is_an_exact_hit():
    [hit] = find_named_phrase(_texts(GEMINI), PHRASE)
    assert _texts(GEMINI)[hit.start : hit.stop] == ["Ojo", "de", "Bife"]
    assert hit.exact is True


def test_this_runs_caption_line_is_written_the_creators_way():
    assert respell_text(HEARD_LINE, (PHRASE,)) == (
        "Inside, the butcher cuts the ojo de bife right in front of you."
    )


def test_this_runs_cue_words_split_inside_the_heard_span():
    words = _line_words("Inside,", "you.")
    cue = _cue(words)
    [out] = apply_spelling_to_cues([cue], (PHRASE,))

    assert out["text"] == "Inside, the butcher cuts the ojo de bife right in front of you."
    assert " ".join(_texts(out["words"])) == out["text"]
    by_text = {w["text"]: w for w in out["words"]}
    olloda = next(w for w in words if w["text"] == "Olloda")
    bife = next(w for w in words if w["text"] == "bife")
    # "Olloda" is cut in two by letters (ojo:de = 3:2); "bife" keeps its own timing.
    assert by_text["ojo"]["start_s"] == olloda["start_s"]
    assert by_text["de"]["end_s"] == olloda["end_s"]
    assert by_text["ojo"]["end_s"] == by_text["de"]["start_s"]
    assert olloda["start_s"] < by_text["ojo"]["end_s"] < olloda["end_s"]
    assert (by_text["bife"]["start_s"], by_text["bife"]["end_s"]) == (
        bife["start_s"],
        bife["end_s"],
    )
    # FM6: still in time order, nothing outside the cue moved.
    starts = [w["start_s"] for w in out["words"]]
    assert starts == sorted(starts)
    assert all(w["end_s"] >= w["start_s"] for w in out["words"])
    assert (out["start_s"], out["end_s"]) == (cue["start_s"], cue["end_s"])
    untouched = [w for w in out["words"] if w["text"] not in {"ojo", "de", "bife"}]
    assert untouched == [w for w in words if w["text"] not in {"Olloda", "bife"}]
    assert cue["words"][5]["text"] == "Olloda"  # input not mutated


def test_one_word_more_merges_two_heard_words_into_one():
    words = [
        {"text": "the", "start_s": 1.0, "end_s": 1.2, "confidence": 0.9},
        {"text": "ojo", "start_s": 1.2, "end_s": 1.5, "confidence": 0.9},
        {"text": "de", "start_s": 1.5, "end_s": 1.6, "confidence": 0.8},
        {"text": "bi", "start_s": 1.6, "end_s": 1.8, "confidence": 0.7},
        {"text": "fe.", "start_s": 1.8, "end_s": 2.1, "confidence": 0.6},
    ]
    [out] = apply_spelling_to_cues([_cue(words)], (PHRASE,))
    assert out["text"] == "the ojo de bife."
    assert out["words"][-1] == {"text": "bife.", "start_s": 1.6, "end_s": 2.1, "confidence": 0.6}
    assert out["words"][:3] == words[:3]


def test_a_sentence_start_keeps_its_capital():
    assert respell_text("Olloda bife is the cut.", (PHRASE,)) == "Ojo de bife is the cut."
    assert respell_text("Great. Olloda bife!", (PHRASE,)) == "Great. Ojo de bife!"


def test_mid_sentence_capital_from_whisper_is_not_kept():
    """FM11: whisper capitalised "Olloda" as a name; the creator wrote it lower case."""
    assert respell_text("the Olloda bife", (PHRASE,)) == "the ojo de bife"


def test_is_mishearing_calls_the_heard_span_a_hearing_of_the_phrase():
    assert is_mishearing("Olloda bife", PHRASE) is True
    assert is_mishearing("ojo de bi fe", PHRASE) is True
    assert is_mishearing("ojo de bife", PHRASE) is False
    assert is_mishearing("the butcher cuts", PHRASE) is False


# --- FM2 / FM3 / FM12: never a false rewrite -----------------------------------


@pytest.mark.parametrize(
    "line",
    [
        "the butcher cuts the meat",  # unrelated span
        "Dried flowers hang from the ceiling.",  # the phrase is not said
        "One question before we eat.",
        "ojode vife",  # sounds close, but no word of the phrase heard exactly (FM12)
        "de bife",  # a word short, and "de bife" is not "ojo de bife"
        "the ojo de bife right",  # already right
        "the Ojo de Bife right",  # only the case differs: never a case-only edit (FM8)
        "Olloda, bife",  # punctuation inside the span (FM7)
    ],
)
def test_lines_that_do_not_say_the_phrase_another_way_stay_as_heard(line):
    assert respell_text(line, (PHRASE,)) == line
    tokens = line.replace(",", " ,").split()
    assert [h for h in find_named_phrase(tokens, PHRASE) if not h.exact] == []


def test_the_whole_run_outside_the_heard_span_is_untouched():
    cues = [_cue(WHISPER[:9]), _cue(WHISPER[9:15]), _cue(WHISPER[15:])]
    out = apply_spelling_to_cues(cues, (PHRASE,))
    assert out[:2] == cues[:2]
    assert out[2]["text"].count("ojo de bife") == 1
    assert "Olloda" not in out[2]["text"]


def test_applying_twice_changes_nothing_more():
    once = apply_spelling_to_cues([_cue(WHISPER)], (PHRASE,))
    assert apply_spelling_to_cues(once, (PHRASE,)) == once


def test_an_exact_hit_wins_over_a_near_one_at_the_same_words():
    """FM9: "o ojo de bife" folds close to the phrase too; only the exact words count."""
    hits = find_named_phrase(["o", "ojo", "de", "bife"], PHRASE)
    assert [(h.start, h.stop, h.exact) for h in hits] == [(1, 4, True)]
    assert respell_text("o ojo de bife", (PHRASE,)) == "o ojo de bife"


# --- FM4: a one-word phrase is one heard word ----------------------------------


def test_a_one_word_phrase_matches_only_one_heard_word():
    assert respell_text("we sat at Meropa how nice", ("Merope",)) == ("we sat at Merope how nice")
    assert respell_text("we sat at Mero pe", ("Merope",)) == "we sat at Mero pe"
    assert find_named_phrase(["Mero", "pe"], "Merope") == []
    [hit] = find_named_phrase(["at", "Meropa"], "Merope")
    assert (hit.start, hit.stop, hit.exact) == (1, 2, False)


# --- FM5: Turkish ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("heard", "written"),
    [
        ("Kızkulesi'ne gittik", "Kız Kulesi'ne gittik"),  # one word for two, ending kept
        ("KIZKULESİ çok güzel", "Kız Kulesi çok güzel"),  # capitals: İ/I fold like the rest
        ("akşam Kızkulesi", "akşam Kız Kulesi"),
    ],
)
def test_turkish_phrase_heard_as_one_word_is_written_as_named(heard, written):
    assert respell_text(heard, ("Kız Kulesi",)) == written


@pytest.mark.parametrize(
    "line",
    [
        "Kızlar kuleye çıktı",  # shares letters, says something else
        "KIZ KULESİ",  # already right, in capitals
        "kız kulesi",  # already right, lower case
        "Kız, Kulesi",  # punctuation inside
    ],
)
def test_turkish_lines_that_do_not_mishear_the_phrase_stay(line):
    assert respell_text(line, ("Kız Kulesi",)) == line


# --- FM9 / FM10: the card hears what the captions respell ----------------------


def _ground(monkeypatch, words, *beats, closing=None):
    import app.services.phone_reaction_grounding as rg

    monkeypatch.setattr(
        rg,
        "_load_ready_pool_assets",
        lambda *a, **k: [
            {
                "id": "p1",
                "gcs_path": "users/u1/plan/item1/pool/p1.jpg",
                "gcs_generation": "7",
                "kind": "image",
                "source_filename": "ribeye.jpg",
                "duration_s": None,
                "aspect": 1.0,
                "user_context": "",
                "analysis": {},
            }
        ],
    )
    monkeypatch.setattr(rg, "_load_sfx_entries", lambda *a, **k: [])
    return rg.ground_phone_reaction_beats(
        lambda: pytest.fail("the DB seams are patched"),
        job_id="j1",
        beats=[{"visual_id": "p1", "visual_role": "photo", **beat} for beat in beats],
        closing=closing,
        words=words,
        duration_s=max(w["end_s"] for w in words) + 3.0,
        clip_path=None,
    )


def _timed(*texts: str, step: float = 0.5) -> list[dict]:
    return [
        {"text": text, "start_s": i * step, "end_s": i * step + step * 0.9}
        for i, text in enumerate(texts)
    ]


def test_the_card_lands_where_this_run_says_the_phrase(monkeypatch):
    result = _ground(monkeypatch, WHISPER, {"beat_id": "b", "trigger": PHRASE})
    [placed] = result.receipt["placed"]
    olloda = next(w for w in WHISPER if w["text"] == "Olloda")
    assert placed["at_s"] == olloda["start_s"]
    assert placed["heard_as"] == "Olloda bife"
    assert result.receipt["unplaced"] == []


def test_a_phrase_heard_exactly_carries_no_heard_as(monkeypatch):
    result = _ground(monkeypatch, GEMINI, {"beat_id": "b", "trigger": PHRASE})
    [placed] = result.receipt["placed"]
    assert placed["at_s"] == next(w for w in GEMINI if w["text"] == "Ojo")["start_s"]
    assert "heard_as" not in placed


def test_an_exact_hearing_anywhere_wins_over_an_earlier_near_one(monkeypatch):
    """FM9: "the metal gate" sounds like "the medal"; the voice also says "the medal" later,
    so the card is placed there, and an "every" beat never adds the near hearing."""
    words = _timed("the", "metal", "gate", "and", "then", "the", "medal")
    for occurrence in ("first", "every"):
        result = _ground(
            monkeypatch, words, {"beat_id": "b", "trigger": "the medal", "occurrence": occurrence}
        )
        assert [p["at_s"] for p in result.receipt["placed"]] == [2.5]
        assert all("heard_as" not in p for p in result.receipt["placed"])


@pytest.mark.parametrize(
    "texts",
    [
        ("the", "butcher", "cuts", "the", "meat"),  # FM2: unrelated
        ("ojode", "vife", "here"),  # FM12: no word of the phrase heard exactly
        ("Olloda,", "bife"),  # FM7: punctuation inside
    ],
)
def test_the_card_is_never_placed_on_a_span_the_captions_would_not_respell(monkeypatch, texts):
    result = _ground(monkeypatch, _timed(*texts), {"beat_id": "b", "trigger": PHRASE})
    assert result.receipt["placed"] == []
    assert result.receipt["unplaced"] == [
        {"beat_id": "b", "trigger": PHRASE, "reason": "never_heard"}
    ]


def test_a_one_word_trigger_hears_one_word_only(monkeypatch):
    """FM4: "Meropa" is a hearing of "Merope"; "Mero pe" is not."""
    near = _ground(
        monkeypatch, _timed("at", "Meropa", "now"), {"beat_id": "b", "trigger": "Merope"}
    )
    assert [p["heard_as"] for p in near.receipt["placed"]] == ["Meropa"]
    split = _ground(monkeypatch, _timed("at", "Mero", "pe"), {"beat_id": "b", "trigger": "Merope"})
    assert split.receipt["placed"] == []


def test_the_after_anchor_and_the_closing_cue_hear_the_same_way(monkeypatch):
    words = _timed("first", "Olloda", "bife", "then", "the", "grill", "and", "bye")
    result = _ground(
        monkeypatch,
        words,
        {"beat_id": "b", "trigger": "the grill", "after": PHRASE},
        closing={"visual_id": "p1", "from_trigger": PHRASE},
    )
    assert [p["at_s"] for p in result.receipt["placed"]] == [2.0]
    assert result.receipt["closing"]["status"] == "placed"
    assert result.receipt["closing"]["from_s"] == pytest.approx(words[2]["end_s"])
