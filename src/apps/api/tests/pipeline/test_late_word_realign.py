"""Guards of the late-word move (KRI-1506), one per failure mode the move must refuse.

The incident itself, the switch-off control and the wrong-audio controls run end to end in
`tests/tasks/test_kri1502_cusco_t2_replay.py`. These pin the geometry the move must NOT accept:
each shape below is the T2 shape with one condition broken. Failure modes are numbered as in
docs/reviews/kri-1502-late-words-failure-modes.md.
"""

from __future__ import annotations

import pytest

from app.pipeline.late_word_realign import find_late_runs, realign_late_words

DURATION_S = 20.0
# T2 around the incident, in source seconds: "güçlü." ends, a pause, the wordless voice of
# "Kilitli sistemler" (12.306-13.384), the pause whisper stamped them in, "yıkılmayı".
WORDS = [
    ("daha", 10.82, 11.02),
    ("güçlü.", 11.02, 11.44),
    ("Kilitli", 13.96, 14.60),
    ("sistemler", 14.60, 15.24),
    ("yıkılmayı", 15.24, 15.66),
    ("zorlaştırıyor.", 15.66, 16.22),
]
SILENCES = [(10.512, 10.643), (11.343, 12.306), (13.384, 15.288), (17.852, 19.065)]
ISLAND = (12.306, 13.384)


def _voice(*spans):
    """A voicing reader that hears sustained voice exactly in ``spans``."""

    def voicing(lo, hi):
        return sum(max(0.0, min(hi, b) - max(lo, a)) for a, b in spans)

    return voicing


VOICE = _voice((10.0, 11.34), ISLAND, (15.30, 17.80))


def _moved(words=WORDS, silences=SILENCES, voicing=VOICE):
    times, runs = realign_late_words(words, silences, DURATION_S, voicing=voicing)
    return times, runs


def test_the_t2_shape_moves_onto_the_island():
    times, [run] = _moved()
    assert (run.first, run.last) == (2, 3)
    assert times[2][0] == pytest.approx(ISLAND[0]) and times[3][1] == pytest.approx(ISLAND[1])
    assert times[:2] == [(s, e) for _t, s, e in WORDS[:2]]
    assert times[4:] == [(s, e) for _t, s, e in WORDS[4:]]


def test_fm4_the_next_word_stamped_in_the_pause_too_moves_nothing():
    words = [*WORDS[:4], ("yıkılmayı", 14.90, 15.66), WORDS[5]]
    assert find_late_runs(words, SILENCES, DURATION_S) == []


def test_fm5_a_run_broken_by_a_word_that_is_not_in_the_pause_moves_nothing():
    # "ve" starts in the pause but runs past it (overlapping stamps): the words in the pause are
    # not one run, so nothing moves.
    words = [
        *WORDS[:2],
        ("Kilitli", 13.50, 13.90),
        ("ve", 13.90, 15.60),
        ("sistemler", 14.50, 15.00),
        ("yıkılmayı", 15.60, 15.90),
    ]
    assert find_late_runs(words, SILENCES, DURATION_S) == []
    # Without "ve" the same two words are one run.
    assert find_late_runs([w for w in words if w[0] != "ve"], SILENCES, DURATION_S)


def test_fm3_an_earlier_word_reaching_into_the_voice_moves_nothing():
    words = [WORDS[0], ("güçlü.", 11.02, 12.60), *WORDS[2:]]
    assert find_late_runs(words, SILENCES, DURATION_S) == []


@pytest.mark.parametrize(
    "island",
    [
        (13.10, 13.384),  # 0.28 s: too short for 1.28 s of stamps (ratio)
        (12.306, 12.50),  # under the minimum island length
    ],
)
def test_fm6_an_island_the_run_cannot_fit_moves_nothing(island):
    silences = [(11.343, island[0]), (island[1], 15.288)]
    assert find_late_runs(WORDS, silences, DURATION_S) == []


def test_fm6_an_island_far_longer_than_the_run_moves_nothing():
    words = [*WORDS[:2], ("Kilitli", 14.9, 15.0), ("sistemler", 15.0, 15.2), *WORDS[4:]]
    # 1.08 s island, 0.3 s of stamps: 3.6x.
    assert find_late_runs(words, SILENCES, DURATION_S) == []


def test_fm7_more_syllables_than_the_island_can_hold_moves_nothing():
    silences = [(11.343, 12.60), (13.384, 15.288)]  # a 0.78 s island: ratio fits 1.28 s
    fits = [*WORDS[:2], ("Kilitli", 13.96, 14.60), ("sistemler", 14.60, 15.24), *WORDS[4:]]
    assert find_late_runs(fits, silences, DURATION_S)  # 6 syllables: 7.7 per second
    crowded = [*WORDS[:2], ("kilitlisistemlerikilitli", 13.96, 14.60)]
    crowded += [("sistemlerikilitlisistemler", 14.60, 15.24), *WORDS[4:]]
    assert find_late_runs(crowded, silences, DURATION_S) == []  # 20 syllables: 25 per second


def test_fm8_a_run_of_zero_length_stamps_moves_nothing():
    words = [*WORDS[:2], ("bir", 14.0, 14.0), ("iki", 14.5, 14.5), *WORDS[4:]]
    assert find_late_runs(words, SILENCES, DURATION_S) == []


def test_fm1_voice_under_the_stamps_moves_nothing():
    _times, runs = _moved(voicing=_voice((10.0, 11.34), ISLAND, (14.0, 14.2), (15.3, 17.8)))
    assert runs == []


def test_fm2_an_island_that_is_not_a_voice_moves_nothing():
    _times, runs = _moved(voicing=_voice((10.0, 11.34), (15.3, 17.8)))
    assert runs == []


def test_fm9_the_moved_run_never_starts_before_the_word_before_it_ends():
    # "güçlü." drifts 0.08 s into the island (inside the slack): the run starts after it.
    words = [WORDS[0], ("güçlü.", 11.02, 12.386), *WORDS[2:]]
    times, [run] = _moved(words)
    assert times[run.first][0] == pytest.approx(12.386)
    starts = [start for start, _end in times]
    assert starts == sorted(starts)


def test_fm13_a_run_at_the_very_start_of_the_take_can_move():
    words = [("Merhaba", 1.2, 1.9), ("arkadaşlar.", 1.9, 2.5), ("Bugün", 2.8, 3.1)]
    silences = [(0.9, 2.6)]
    times, [run] = realign_late_words(words, silences, 5.0, voicing=_voice((0.0, 0.9)))
    assert (run.first, run.last) == (0, 1)
    assert times[0][0] == pytest.approx(0.0) and times[1][1] == pytest.approx(0.9)


def test_a_short_pause_is_never_a_late_run_pause():
    silences = [(11.343, 12.306), (13.384, 13.9), (13.9, 15.288)]
    # Merged spans are one pause; two spans that do not touch are not.
    split = [(11.343, 12.306), (13.384, 13.90), (13.95, 15.288)]
    assert find_late_runs(WORDS, silences, DURATION_S)  # touching spans merge
    assert find_late_runs(WORDS, split, DURATION_S) == []
