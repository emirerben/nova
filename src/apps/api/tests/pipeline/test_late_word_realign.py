"""Guards of the late-word move (KRI-1506), one per failure mode the move must refuse.

The incident itself, the switch-off control and the wrong-audio controls run end to end in
`tests/tasks/test_kri1502_cusco_t2_replay.py`. These pin the geometry the move must NOT accept:
each shape below is the T2 shape with one condition broken. Failure modes are numbered as in
docs/reviews/kri-1502-late-words-failure-modes.md.
"""

from __future__ import annotations

import pytest

from app.pipeline.late_word_realign import find_late_runs, follow_late_runs, realign_late_words

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


def test_fm14_gates_judge_the_island_left_after_the_word_before_it():
    """Review of #2374: the word before the run may reach 0.1 s into the island. The size and
    syllable gates must judge what is LEFT of the island, never the whole of it: here the whole
    is 0.30 s (10 syllables/s) but only 0.21 s is left (14 syllables/s), so nothing moves."""
    words = [("ve", 0.5, 1.79), ("kilitli", 2.1, 2.4), ("son", 3.05, 3.4)]
    silences = [(1.0, 1.7), (2.0, 3.0)]
    assert find_late_runs(words, silences, 4.0) == []
    _times, runs = realign_late_words(words, silences, 4.0, voicing=_voice((1.7, 2.0)))
    assert runs == []


# ------------------------------------------------------------------ another pass of the same take

# What the T2 analysis records for its one moved run (`diagnostics.late_words`).
T2_RUN = {
    "words": 2,
    "stamped_s": [13.96, 15.24],
    "moved_to_s": [12.306, 13.384],
    "pause_s": [13.384, 15.288],
}


def _pass(*rows):
    return [{"text": t, "start_s": s, "end_s": e} for t, s, e in rows]


def test_fm15_a_second_pass_stamped_into_the_same_pause_follows_the_move():
    """A caption pass in another language transcribes the take again; whisper can stamp the same
    words into the same proven-dead pause. They move onto the voice like the analysis's own."""
    second = _pass(("strong.", 11.0, 11.44), ("Locked", 13.9, 14.5), ("systems", 14.5, 15.2))
    second += _pass(("make", 15.3, 15.6))
    moved = follow_late_runs(second, [T2_RUN])
    assert [w["text"] for w in moved] == ["strong.", "Locked", "systems", "make"]
    assert moved[0] == second[0] and moved[3] == second[3]
    assert ISLAND[0] - 1e-6 <= moved[1]["start_s"] < moved[2]["start_s"] <= ISLAND[1]
    assert moved[2]["end_s"] <= ISLAND[1] + 1e-6


def test_fm15_a_second_pass_already_on_the_voice_is_left_alone():
    second = _pass(("Locked", 12.3, 12.9), ("systems", 12.9, 13.4), ("make", 15.3, 15.6))
    assert follow_late_runs(second, [T2_RUN]) == second
    assert follow_late_runs(second, None) == second
    assert follow_late_runs(second, []) == second


def test_fm15_a_moved_word_never_starts_before_the_word_before_it():
    # The pass put "Locked" on the voice but "systems" in the pause: it lands after "Locked".
    second = _pass(("Locked", 12.3, 13.3), ("systems", 13.9, 14.6), ("make", 15.3, 15.6))
    moved = follow_late_runs(second, [T2_RUN])
    starts = [w["start_s"] for w in moved]
    assert starts == sorted(starts)
    assert moved[1]["start_s"] >= moved[0]["end_s"] - 1e-9
    assert moved[1]["end_s"] >= moved[1]["start_s"]


def test_fm16_a_snapshot_without_recorded_moves_has_nothing_to_follow():
    """An opening-cut snapshot can carry an analysis with no diagnostics at all (CI #2374:
    `tests/tasks/test_title_look_worker.py`): nothing to follow, never a crash."""
    from types import SimpleNamespace

    from app.pipeline.late_word_realign import recorded_late_runs

    assert recorded_late_runs(SimpleNamespace(analysis=SimpleNamespace(language="en"))) == []
    assert recorded_late_runs(SimpleNamespace(analysis=SimpleNamespace(diagnostics={}))) == []
    assert recorded_late_runs(None) == []
    with_move = SimpleNamespace(analysis=SimpleNamespace(diagnostics={"late_words": [T2_RUN]}))
    assert recorded_late_runs(with_move) == [T2_RUN]
