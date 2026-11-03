"""Parent KRI-1151: a clip played while the creator's take is paused lands at the END OF THE
SENTENCE that talks about it, chosen by meaning, not at a matched word.

Failure modes pinned (written before the code):

 1. the pause lands at the matched keyword (or at the nearest pause / caption end) instead of the
    end of the sentence that carries the topic;
 2. a topic that runs across two sentences is cut after the first one;
 3. a clip whose facts are English and whose sentences are Turkish ("alcohol" / "alkol") is never
    matched because the only matcher is lexical;
 4. the model finds nothing and the clip is placed anyway (or dropped without saying why);
 5. a low-confidence guess is trusted over the creator's words, or a lexical match overrides a
    model that said "no sentence is about this";
 6. the meaning check is unavailable and the clip silently vanishes, or silently takes a guess
    that is not labelled as one;
 7. the sentence ends in the first 2.5 s or is the last thing he says: the clip is not dropped,
    it plays after the next sentence / just before the last one and the receipt says so (these
    are defaults: an explicit phrase or second is never moved, see test_phone_talking_inserts);
 8. two clips choose the same sentence, or two points closer than 2 s: the later one plays right
    after the earlier one, said in the receipt;
 9. a sentence is cut at a decimal ("70.000"), runs on for a minute because Whisper wrote no
    punctuation, or is split mid-word.
"""

from __future__ import annotations

import pytest

from app.pipeline.phone_subtitled_plan import PhoneInsert
from app.pipeline.phone_talking_insert_topics import (
    MAX_SENTENCE_WORDS,
    Sentence,
    TopicPick,
    resolve_topics,
    split_sentences,
)
from app.pipeline.phone_talking_inserts import InsertAsk, place_inserts
from app.pipeline.phone_talking_mentions import FootageClip, clip_keys, keyword_mentions
from tests.pipeline.test_phone_subtitled_plan import _binding


def _words(text: str, *, start: float = 0.0, step: float = 0.4, gap_after: dict | None = None):
    """``text`` as timed words, ``step`` seconds each; ``gap_after`` maps a word index to the
    silence that follows it."""
    out = []
    t = start
    for index, token in enumerate(text.split()):
        out.append({"text": token, "start_s": round(t, 3), "end_s": round(t + step, 3)})
        t += step + (gap_after or {}).get(index, 0.0)
    return out


# --- 9. sentences ---


def test_sentences_end_at_terminal_punctuation_and_a_decimal_is_not_one():
    words = _words("Bu 70.000 kişilik stat. Peki neden? Bilmiyorum")
    sentences = split_sentences(words, take_s=20.0)
    assert [s.text for s in sentences] == ["Bu 70.000 kişilik stat.", "Peki neden?", "Bilmiyorum"]
    assert [s.index for s in sentences] == [1, 2, 3]
    assert sentences[0].start_s == pytest.approx(0.0)
    assert sentences[0].end_s == pytest.approx(0.4 * 4)


def test_without_punctuation_a_long_pause_ends_a_sentence():
    words = _words("bir iki üç dört beş altı", gap_after={2: 1.0})
    sentences = split_sentences(words, take_s=20.0)
    assert [s.text for s in sentences] == ["bir iki üç", "dört beş altı"]


def test_a_run_with_no_punctuation_and_no_pause_is_cut_at_its_longest_gap_never_mid_word():
    run = " ".join(f"w{i}" for i in range(MAX_SENTENCE_WORDS * 2 + 5))
    gaps = {MAX_SENTENCE_WORDS - 1: 0.3, MAX_SENTENCE_WORDS * 2 - 1: 0.3}
    sentences = split_sentences(_words(run, gap_after=gaps), take_s=100.0)
    assert len(sentences) >= 3
    assert all(len(s.text.split()) <= MAX_SENTENCE_WORDS for s in sentences)
    assert sum(len(s.text.split()) for s in sentences) == MAX_SENTENCE_WORDS * 2 + 5
    assert sentences[0].text.split()[-1] == f"w{MAX_SENTENCE_WORDS - 1}"


def test_words_that_start_after_the_take_are_not_sentences():
    words = _words("Birinci cümle. İkinci cümle.", step=1.0)
    assert [s.text for s in split_sentences(words, take_s=1.9)] == ["Birinci cümle."]


# --- 1-6. choosing the sentence ---


def _talk(*lines: str, step: float = 0.4) -> list[Sentence]:
    """Sentences laid back to back with a 0.5 s breath between them."""
    out, t = [], 0.0
    for index, line in enumerate(lines, start=1):
        length = len(line.split()) * step
        out.append(Sentence(index, line, round(t, 3), round(t + length, 3)))
        t += length + 0.5
    return out


SENTENCES = _talk(
    "Fenerbahçe'ye yeni bir stat teklif edildi.",
    "70.000 kişilik Maltepe'de.",
    "Kadıköy'deki tekellere neden maç öncesi ve sonrası alkol satışı yasağı getirildi?",
    "Bunların hepsi tesadüf mü?",
    "Bence değil ama sormak gerekiyor.",
)


TAKE_S = SENTENCES[-1].end_s + 6.0
TAIL_TAKE_S = SENTENCES[-1].end_s + 0.5


def test_a_keyword_in_the_middle_of_a_long_sentence_pauses_after_the_sentence_not_the_word():
    keyword_end = SENTENCES[2].start_s + 2.0  # "alkol" is in the middle of sentence 3
    (row,) = resolve_topics(
        ["a"], SENTENCES, picks=None, keyword_end_s={"a": keyword_end}, take_s=TAKE_S
    )
    assert row.via == "keyword"
    assert (row.first, row.last) == (3, 3)
    assert row.point_s == pytest.approx(SENTENCES[2].end_s)
    assert row.point_s > keyword_end + 1.0


def test_the_model_pick_decides_and_the_pause_is_the_end_of_that_sentence():
    picks = {"a": TopicPick("a", first=3, last=3, confidence="high")}
    (row,) = resolve_topics(
        ["a"], SENTENCES, picks=picks, keyword_end_s={"a": SENTENCES[0].end_s}, take_s=TAKE_S
    )
    assert row.via == "meaning"
    assert row.point_s == pytest.approx(SENTENCES[2].end_s)
    assert row.reason is None


def test_a_topic_spread_across_two_sentences_pauses_after_the_last_of_them():
    picks = {"a": TopicPick("a", first=1, last=2, confidence="high")}
    (row,) = resolve_topics(["a"], SENTENCES, picks=picks, keyword_end_s={}, take_s=TAKE_S)
    assert (row.first, row.last) == (1, 2)
    assert row.point_s == pytest.approx(SENTENCES[1].end_s)


def test_a_clip_with_english_facts_matches_turkish_sentences_by_meaning_where_keywords_cannot():
    clip = FootageClip(
        "a",
        footage_s=15.0,
        keys=clip_keys(
            [
                "A man announces a ban on alcohol sales around the stadium on match days",
                "alcohol",
            ]
        ),
    )
    words = [w for s in SENTENCES for w in _words(s.text, start=s.start_s)]
    assert keyword_mentions([clip], words=_as_objects(words)) == {}
    picks = {"a": TopicPick("a", first=3, last=3, confidence="high")}
    (row,) = resolve_topics(["a"], SENTENCES, picks=picks, keyword_end_s={}, take_s=TAKE_S)
    assert row.via == "meaning"
    assert row.point_s == pytest.approx(SENTENCES[2].end_s)


def _as_objects(words):
    from types import SimpleNamespace

    return [SimpleNamespace(**w) for w in words]


def test_the_model_saying_no_sentence_is_about_it_leaves_the_clip_out_even_with_a_keyword():
    picks = {"a": TopicPick("a", first=None, last=None, confidence="high")}
    (row,) = resolve_topics(["a"], SENTENCES, picks=picks, keyword_end_s={"a": 5.0}, take_s=TAKE_S)
    assert row.point_s is None
    assert row.reason == "no_matching_sentence"
    assert row.via is None


def test_a_clip_the_model_did_not_answer_for_is_left_out_with_the_same_reason():
    (row,) = resolve_topics(["a"], SENTENCES, picks={}, keyword_end_s={"a": 5.0}, take_s=TAKE_S)
    assert (row.point_s, row.reason) == (None, "no_matching_sentence")


def test_an_answer_outside_the_talk_is_not_trusted():
    for bad in ((0, 1), (4, 9), (3, 2), (1, 5)):
        picks = {"a": TopicPick("a", first=bad[0], last=bad[1], confidence="high")}
        (row,) = resolve_topics(["a"], SENTENCES, picks=picks, keyword_end_s={}, take_s=TAKE_S)
        assert (row.point_s, row.reason) == (None, "no_matching_sentence"), bad


def test_a_low_confidence_pick_counts_only_when_the_keywords_land_in_the_same_sentences():
    low = {"a": TopicPick("a", first=3, last=3, confidence="low")}
    keyword_in_3 = SENTENCES[2].start_s + 1.0
    (kept,) = resolve_topics(
        ["a"], SENTENCES, picks=low, keyword_end_s={"a": keyword_in_3}, take_s=TAKE_S
    )
    assert (kept.via, kept.last, kept.confidence) == ("meaning", 3, "low")
    (dropped,) = resolve_topics(
        ["a"], SENTENCES, picks=low, keyword_end_s={"a": 1.0}, take_s=TAKE_S
    )
    assert (dropped.point_s, dropped.reason) == (None, "low_confidence")
    (alone,) = resolve_topics(["a"], SENTENCES, picks=low, keyword_end_s={}, take_s=TAKE_S)
    assert (alone.point_s, alone.reason) == (None, "low_confidence")


def test_when_the_meaning_check_is_unavailable_keywords_are_used_and_labelled_as_such():
    (found,) = resolve_topics(["a"], SENTENCES, picks=None, keyword_end_s={"a": 1.0}, take_s=TAKE_S)
    assert found.via == "keyword"
    (missing,) = resolve_topics(["a"], SENTENCES, picks=None, keyword_end_s={}, take_s=TAKE_S)
    assert (missing.point_s, missing.reason) == (None, "topic_check_unavailable")


def test_rows_come_back_in_the_order_asked():
    picks = {
        "b": TopicPick("b", first=2, last=2, confidence="high"),
        "a": TopicPick("a", first=4, last=4, confidence="medium"),
    }
    rows = resolve_topics(["b", "a"], SENTENCES, picks=picks, keyword_end_s={}, take_s=TAKE_S)
    assert [r.media_id for r in rows] == ["b", "a"]


# --- 1, 7, 8. placing at the sentence end ---


def _ask(media_id: str, point: float) -> InsertAsk:
    return InsertAsk(media_id, at_s=point, basis="topic")


def _place(asks, *, take_s=60.0, words=()):
    bindings = {a.media_id: _binding(a.media_id, duration_s=6.0) for a in asks}
    return place_inserts(asks, bindings=bindings, words=list(words), take_s=take_s)


def test_a_sentence_end_is_used_exactly_and_never_moved_to_a_later_pause_or_caption_end():
    words = _words("bir iki üç dört", start=9.0, step=0.5, gap_after={3: 1.0}) + _words(
        "beş altı", start=12.0
    )
    (placed,) = _place([_ask("m", 11.0)], words=words)
    assert placed.insert is not None
    assert placed.insert.at_take_s == pytest.approx(11.0)
    assert (placed.basis, placed.snap) == ("topic", "sentence_end")
    assert placed.overrode == ()


def test_the_last_sentence_plays_just_before_it_and_the_receipt_says_so():
    last = len(SENTENCES)
    picks = {"a": TopicPick("a", first=last, last=last, confidence="high")}
    (row,) = resolve_topics(["a"], SENTENCES, picks=picks, keyword_end_s={}, take_s=TAIL_TAKE_S)
    assert row.adjusted == "tail_window"
    assert row.point_s == pytest.approx(SENTENCES[last - 2].end_s)
    assert (row.first, row.last) == (last, last)


def test_a_sentence_that_ends_inside_the_hook_plays_after_the_next_one_and_says_so():
    early = [
        Sentence(1, "Merhaba.", 0.2, 1.2),
        Sentence(2, "Bugün kongreyi anlatacağım.", 1.6, 4.0),
        Sentence(3, "Başlayalım.", 4.5, 5.5),
    ]
    picks = {"a": TopicPick("a", first=1, last=1, confidence="high")}
    (row,) = resolve_topics(["a"], early, picks=picks, keyword_end_s={}, take_s=30.0)
    assert (row.adjusted, row.point_s) == ("hook_window", 4.0)


def test_a_talk_that_is_one_sentence_long_keeps_its_point_rather_than_dropping_the_clip():
    only = [Sentence(1, "Tek cümle.", 0.2, 2.0)]
    picks = {"a": TopicPick("a", first=1, last=1, confidence="high")}
    (row,) = resolve_topics(["a"], only, picks=picks, keyword_end_s={}, take_s=2.5)
    assert row.point_s == pytest.approx(2.0) and row.reason is None


def test_two_clips_on_the_same_sentence_both_play_the_second_right_after_the_first():
    first, second = _place([_ask("a", 20.0), _ask("b", 20.0)])
    assert first.insert is not None and second.insert is not None
    assert second.insert.at_take_s == pytest.approx(first.insert.at_take_s)
    assert second.adjusted == "near_another_clip"


def test_inserts_two_seconds_apart_are_both_kept_where_they_were_chosen():
    first, second = _place([_ask("a", 20.0), _ask("b", 22.0)])
    assert first.insert is not None and second.insert is not None
    assert second.insert.at_take_s == pytest.approx(22.0)
    assert isinstance(second.insert, PhoneInsert)


def test_a_topic_ask_the_worker_never_resolved_is_refused_not_guessed():
    (placed,) = _place([InsertAsk("m", at_topic=True)])
    assert (placed.insert, placed.reason) == (None, "no_position")
