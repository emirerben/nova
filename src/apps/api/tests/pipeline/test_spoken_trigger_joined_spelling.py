"""KRI-1281: a trigger matches the take whichever way its spaces fall."""

import pytest

from app.pipeline.phone_spoken_text import find_spoken_word_matches


def _w(text: str, start: float) -> dict:
    return {"text": text, "start_s": start, "end_s": start + 0.4}


@pytest.mark.parametrize("trigger", ["SpecKit", "Spec-kit", "Spec Kit", "spec kit"])
def test_two_spoken_words_match_every_spelling(trigger):
    words = [_w("Number", 1), _w("one,", 1.5), _w("Spec", 2), _w("Kit.", 2.4)]
    assert find_spoken_word_matches(words, [trigger]) == [(trigger, 2, pytest.approx(2.8))]


@pytest.mark.parametrize("trigger", ["SpecKit", "Spec Kit"])
def test_one_spoken_word_matches_spaced_trigger(trigger):
    assert len(find_spoken_word_matches([_w("speckit", 4)], [trigger])) == 1


def test_short_pairs_do_not_collide():
    assert find_spoken_word_matches([_w("a", 0), _w("bc", 1)], ["abc"]) == []
