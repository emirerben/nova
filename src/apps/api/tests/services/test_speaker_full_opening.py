"""Qendresa item 16: full screen in her own room, then the corner (S72-S74, S80).

Pure arithmetic over a Qendresa-shaped talk (a ~7 s intro sentence, then "number one is spec kit
..." and four more topics, ~230 wpm). Failure modes:
``docs/reviews/speaker-background-windows-failure-modes.md``.
"""

from __future__ import annotations

import random

import pytest

from app.config import settings
from app.services import phone_background_sequence as seq
from tests.tasks.test_background_follows_talk import (
    AS_SPOKEN,
    SAID,
    TAKE_S,
    WORDS,
    _lay,
    _rows,
)


@pytest.fixture(autouse=True)
def _flag_on(monkeypatch):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", True)


def _entries(phrases):
    return [seq.SequenceEntry(index=i, spoken_phrase=p) for i, p in enumerate(phrases)]


def _kept_words(words, keep):
    out = []
    for w in words:
        at = seq.to_output_time(w["start_s"], keep)
        if at is not None:
            out.append((at, at + (w["end_s"] - w["start_s"])))
    return out


def _inside_a_word(x, words, keep):
    return any(s + 1e-6 < x < e - 1e-6 for s, e in _kept_words(words, keep))


def _open(words=WORDS, keep=None, take_s=TAKE_S, stated=None, heard=None, title=None):
    return seq.full_screen_opening_end(
        words=words,
        keep_segments=keep,
        take_s=take_s,
        stated_s=stated,
        first_heard_s=heard,
        title_end_s=title,
    )


def test_the_opening_ends_on_the_first_recordings_word():
    heard = seq.first_heard_at(_entries(AS_SPOKEN), WORDS, None)
    assert heard == pytest.approx(SAID[0], abs=0.01)
    x, why = _open(heard=heard)
    assert why is None and x is not None
    # Her V5 switches on "spec kit": the switch is in the gap right before that word.
    assert abs(x - SAID[0]) <= 0.1 and x <= SAID[0] + 1e-6
    assert not _inside_a_word(x, WORDS, None)


def test_the_switch_never_lands_inside_a_word():
    rng = random.Random(16)
    for trial in range(40):
        t, words = 0.0, []
        for _ in range(rng.randint(30, 90)):
            dur = rng.uniform(0.1, 0.9)
            words.append({"text": "w.", "start_s": round(t, 3), "end_s": round(t + dur, 3)})
            t += dur + rng.choice([0.0, 0.0, 0.02, 0.05, 0.3, 0.9])
        take = t + 0.2
        for target in [x / 10 for x in range(10, 150, 3)]:
            x, why = _open(words=words, take_s=take, stated=target)
            if x is None:
                assert why == "take_too_short"
                continue
            assert x >= 1.0 - 1e-6 and take - x >= 1.0 - 1e-6, (trial, target, x)
            assert not _inside_a_word(x, words, None), (trial, target, x)


def test_a_stated_length_wins():
    x, _ = _open(stated=4.0, heard=SAID[0])
    assert x is not None and abs(x - 4.0) <= 1.0
    assert not _inside_a_word(x, WORDS, None)


def test_a_first_word_outside_two_to_eight_seconds_falls_back_to_five():
    x, _ = _open(heard=12.0)
    assert x is not None and abs(x - 5.0) <= 1.0
    x, _ = _open(heard=None)
    assert x is not None and abs(x - 5.0) <= 1.0


def test_the_opening_covers_the_title():
    x, _ = _open(heard=None, title=7.0)
    assert x is not None and x >= 6.7
    # A title longer than eight seconds is not honoured (it would eat the take).
    x, _ = _open(heard=None, title=9.5)
    assert x is not None and abs(x - 5.0) <= 1.0


def test_a_short_take_gets_no_opening_and_says_so():
    words = [
        {"text": "hi.", "start_s": 0.1, "end_s": 0.4},
        {"text": "there", "start_s": 0.5, "end_s": 0.9},
    ]
    assert _open(words=words, take_s=1.8) == (None, "take_too_short")
    assert _open(words=words, take_s=2.6, stated=5.0)[0] is not None
    x, _ = _open(words=words, take_s=2.6, stated=5.0)
    assert 1.0 <= x <= 1.6


def test_the_first_window_starts_at_the_switch():
    heard = seq.first_heard_at(_entries(AS_SPOKEN), WORDS, None)
    x, _ = _open(heard=heard)
    on_the_word = _lay(AS_SPOKEN, opening_s=x)
    assert on_the_word.windows[0].start_s == pytest.approx(x)
    assert all(w.start_s >= x - 1e-9 for w in on_the_word.windows)
    # Rotation (no phrases): the cycle starts filling at the switch, contiguous to the end.
    rotation = seq.lay_out(
        [seq.SequenceEntry(index=i) for i in range(3)],
        cadence_s=4.0,
        take_s=TAKE_S,
        words=WORDS,
        keep_segments=None,
        opening_s=x,
    )
    assert rotation.windows[0].start_s == pytest.approx(x) and rotation.windows[0].index == 0
    assert rotation.windows[-1].end_s == pytest.approx(TAKE_S)
    # A window laid out inside the opening is clipped, or dropped when under 0.5 s remains.
    late = _lay(AS_SPOKEN, opening_s=SAID[1] - 0.2)
    assert late.windows[0].start_s == pytest.approx(SAID[1] - 0.2)
    assert {"lane": "background", "window": 0, "reason": "inside_full_screen_opening"} in late.notes


def test_the_opening_survives_a_speech_cut():
    # A 1.5 s pause inside the intro is cut out of the kept take.
    words = []
    for w in WORDS:
        shifted = dict(w)
        if w["start_s"] >= 3.0:
            shifted["start_s"] = round(w["start_s"] + 1.5, 2)
            shifted["end_s"] = round(w["end_s"] + 1.5, 2)
        words.append(shifted)
    keep = [(0.0, 3.0), (4.5, words[-1]["end_s"] + 0.3)]
    take_s = seq.kept_duration(keep, 0.0)
    heard = seq.first_heard_at(_entries(AS_SPOKEN), words, keep)
    assert heard == pytest.approx(SAID[0], abs=0.01)  # kept time equals the uncut talk
    x, why = _open(words=words, keep=keep, take_s=take_s, heard=heard)
    assert why is None and abs(x - SAID[0]) <= 0.1
    assert not _inside_a_word(x, words, keep)
    source = seq.to_source_time(x, keep, as_end=True)
    assert not any(w["start_s"] + 1e-6 < source < w["end_s"] - 1e-6 for w in words)
    assert not (3.0 + 1e-6 < source < 4.5 - 1e-6)


def test_a_word_inside_a_removed_span_does_not_exist():
    words = [dict(w) for w in WORDS]
    keep = [(0.0, 2.0), (3.0, TAKE_S)]
    kept = _kept_words(words, keep)
    assert len(kept) < len(words)


@pytest.mark.parametrize("follows", [True, False])
def test_no_opening_is_byte_identical(monkeypatch, follows):
    monkeypatch.setattr(settings, "background_follows_talk_enabled", follows)
    for phrases in (AS_SPOKEN, ["spec kit", "impeccable"]):
        plain = _lay(phrases)
        explicit = _lay(phrases, opening_s=None)
        assert _rows(plain) == _rows(explicit) and plain.notes == explicit.notes
        assert plain == explicit
