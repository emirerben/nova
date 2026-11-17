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


# ------------------------------------------------------------------ KRI-1512: stretched heads
# T2 around pauses B and C, source seconds: whisper stretched the head of the word after each
# pause back over it ("1." over 5.582-6.547, voice from 6.565; "sertlikten" over 9.241-10.243,
# voice from 10.305), so the stamped gap stayed under MAX_PAUSE_S and rule 3 never saw it.
# Failure modes `hN`/`sN` as in docs/reviews/kri-1512-stretched-words-failure-modes.md.

from app.pipeline.late_word_realign import (  # noqa: E402
    find_stretched_heads,
    realign_stretched_heads,
)

H_DURATION_S = 12.0
H_WORDS = [
    ("öğrendi.", 4.74, 5.16),
    ("Ne", 5.20, 5.30),
    ("bunlar?", 5.30, 5.64),
    ("1.", 6.20, 6.84),
    ("Eşeklik", 7.74, 8.20),
    ("mütemadiyen", 8.20, 9.50),
    ("sertlikten", 9.50, 10.82),
    ("daha", 10.82, 11.02),
]
H_SILENCES = [(5.582, 6.547), (6.83, 7.516), (8.13, 8.464), (9.241, 10.243), (10.512, 10.643)]
H_VOICE = _voice(
    (4.895, 5.545), (6.565, 6.715), (7.60, 8.10), (8.595, 9.265), (10.305, 10.385), (10.405, 11.0)
)
B_PAUSE, C_PAUSE = (5.582, 6.547), (9.241, 10.243)
B_WORD, C_WORD = 3, 6  # "1.", "sertlikten"


def _heads(words=H_WORDS, silences=H_SILENCES, voicing=H_VOICE):
    return realign_stretched_heads(words, silences, H_DURATION_S, voicing=voicing)


def _replace(words, index, start, end):
    return [*words[:index], (words[index][0], start, end), *words[index + 1 :]]


def test_the_t2_heads_start_at_the_pause_end():
    times, heads = _heads()
    assert [head.index for head in heads] == [B_WORD, C_WORD]
    assert times[B_WORD] == (B_PAUSE[1], 6.84) and times[C_WORD] == (C_PAUSE[1], 10.82)
    # The previous word's stamped end inside the pause goes back to the pause start.
    assert times[B_WORD - 1] == (5.30, B_PAUSE[0]) and times[C_WORD - 1] == (8.20, C_PAUSE[0])
    untouched = set(range(len(H_WORDS))) - {B_WORD - 1, B_WORD, C_WORD - 1, C_WORD}
    assert all(times[i] == H_WORDS[i][1:] for i in untouched)
    assert heads[0].to_diagnostic() == {
        "start_s": [6.2, 6.547],
        "pause_s": [5.582, 6.547],
        "previous_end_s": [5.64, 5.582],
    }


def test_h1_voice_inside_the_pause_trims_nothing():
    """KRI-969's quiet onset: a voice the silence floor missed is in the "pause"."""
    voicing = _voice((4.895, 5.545), (6.0, 6.2), (6.565, 6.715), (8.595, 9.265), (9.9, 10.1))
    voicing_after = _voice((10.305, 11.0))

    def both(lo, hi):
        return voicing(lo, hi) + voicing_after(lo, hi)

    _times, heads = _heads(voicing=both)
    assert heads == []


def test_h2_the_trim_stops_at_the_silence_end_not_at_the_voice():
    """An unvoiced onset before the voice stays: the word starts at the silence end (rule 3 then
    keeps KEPT_GAP_S/2 before it), never at the later voice onset."""
    times, _heads_ = _heads()
    assert times[C_WORD][0] == C_PAUSE[1] < 10.305


def test_h3_sound_between_the_pause_and_the_voice_trims_nothing():
    late_voice = _voice((4.895, 5.545), (6.80, 6.84), (8.595, 9.265), (10.50, 11.0))
    _times, heads = _heads(voicing=late_voice)
    assert heads == []  # the voice starts 0.25 s after each pause


def test_h4_no_voice_in_the_rest_of_the_stamp_trims_nothing():
    _times, heads = _heads(voicing=_voice((4.895, 5.545), (8.595, 9.265), (11.1, 11.5)))
    assert heads == []


def test_h5_a_pause_rule_3_already_sees_is_left_alone():
    words = _replace(H_WORDS, B_WORD - 1, 5.30, 5.55)  # stamped gap 0.65 s
    assert B_WORD not in [head.index for head in find_stretched_heads(words, H_SILENCES, 12.0)]


def test_h6_both_edges_in_one_pause_trims_nothing():
    words = _replace(H_WORDS, C_WORD - 1, 8.20, 9.55)  # "mütemadiyen" 0.31 s into the pause
    words = _replace(words, C_WORD, 9.55, 10.82)
    assert C_WORD not in [head.index for head in find_stretched_heads(words, H_SILENCES, 12.0)]


def test_h7_another_word_in_the_pause_trims_nothing():
    # A zero-length "şey" stamped after "1." starts, inside the pause.
    after = [*H_WORDS[: B_WORD + 1], ("şey", 6.3, 6.3), *H_WORDS[B_WORD + 1 :]]
    assert B_WORD not in [head.index for head in find_stretched_heads(after, H_SILENCES, 12.0)]
    # "Ne" reaching into the pause past "bunlar?" (overlapping stamps).
    reaching = _replace(H_WORDS, B_WORD - 2, 5.20, 5.70)
    assert B_WORD not in [head.index for head in find_stretched_heads(reaching, H_SILENCES, 12.0)]
    # The previous word lies in the pause itself (0.2 s of it: under the both-edges minimum).
    inside = _replace(H_WORDS, B_WORD - 1, 5.60, 5.80)
    assert B_WORD not in [head.index for head in find_stretched_heads(inside, H_SILENCES, 12.0)]


def test_h8_a_remainder_that_cannot_hold_the_word_trims_nothing():
    short = _replace(H_WORDS, B_WORD, 6.20, 6.66)  # 0.11 s after the pause
    assert B_WORD not in [head.index for head in find_stretched_heads(short, H_SILENCES, 12.0)]
    crowded = [*H_WORDS[:C_WORD], ("sertliktendahagüçlüsertlikten", 9.50, 10.82), H_WORDS[-1]]
    assert C_WORD not in [head.index for head in find_stretched_heads(crowded, H_SILENCES, 12.0)]


def test_h9_two_long_quiet_patches_in_the_word_trim_nothing():
    words = _replace(H_WORDS, C_WORD, 9.50, 11.9)
    silences = [*H_SILENCES[:-1], (10.9, 11.6)]
    assert C_WORD not in [head.index for head in find_stretched_heads(words, silences, 12.0)]


def test_h10_the_first_word_of_the_take_is_never_trimmed():
    words = [("Merhaba", 0.4, 1.4), ("arkadaşlar.", 1.4, 2.0)]
    assert find_stretched_heads(words, [(0.3, 1.0)], 3.0) == []


def test_h11_trimmed_words_keep_their_order():
    times, _heads_ = _heads()
    starts = [start for start, _end in times]
    assert starts == sorted(starts)
    assert all(end >= start for start, end in times)
    assert all(times[i][1] <= times[i + 1][0] + 1e-9 for i in (B_WORD - 1, C_WORD - 1))


def test_h14_a_second_pass_head_in_the_cut_survives_the_remap():
    """Another caption pass stamps "sertlikten" from 9.50 s again: the cut that now removes the
    pause only takes its head, so the word stays and starts at the cut."""
    from app.pipeline.silence_cut import CutPlan, Removal, remap_words

    plan = CutPlan(
        keep_segments=[(0.0, 9.366), (10.118, 12.0)],
        removed=[Removal(9.366, 10.118, "silence")],
        time_saved_s=0.752,
    )
    [word] = remap_words([{"text": "sertlikten", "start_s": 9.50, "end_s": 10.82}], plan)
    assert word["start_s"] == pytest.approx(9.366) and word["end_s"] == pytest.approx(10.068)


# ------------------------------------------------------------------ KRI-1512: straddling runs
# T2 around pause D: "Ve" starts on the wordless voice (36.839-37.413, "Ve dörtte") and runs into
# the pause, "4'te" lies wholly in it, "İNKA'lar" starts at its end.

S_DURATION_S = 41.0
S_WORDS = [
    ("önemli", 35.30, 35.56),
    ("oluyor.", 35.56, 35.84),
    ("Ve", 37.00, 37.60),
    ("4'te", 37.60, 38.20),
    ("İNKA'lar", 38.20, 39.02),
]
S_SILENCES = [(34.38, 34.951), (35.785, 36.839), (37.413, 38.486)]
S_ISLAND = (36.839, 37.413)
S_VOICE = _voice((35.0, 35.70), (36.82, 37.325), (38.475, 39.0))


def _straddled(words=S_WORDS, silences=S_SILENCES, voicing=S_VOICE, straddle=True):
    return realign_late_words(words, silences, S_DURATION_S, voicing=voicing, straddle=straddle)


def test_the_t2_straddling_run_moves_onto_its_voice():
    times, [run] = _straddled()
    assert (run.first, run.last, run.straddle) == (2, 3, True)
    assert times[2][0] == pytest.approx(S_ISLAND[0]) and times[3][1] == pytest.approx(S_ISLAND[1])
    assert times[2][1] == pytest.approx(times[3][0])
    assert times[:2] == [w[1:] for w in S_WORDS[:2]] and times[4] == S_WORDS[4][1:]
    assert run.to_diagnostic()["straddle"] is True


def test_s0_without_straddle_the_late_word_move_refuses_it_as_before():
    _times, runs = _straddled(straddle=False)
    assert runs == []


def test_s1_voice_under_the_stamps_in_the_pause_moves_nothing():
    _times, runs = _straddled(voicing=_voice((35.0, 35.70), (36.82, 37.325), (37.8, 38.0)))
    assert runs == []


def test_s2_the_word_before_reaching_into_the_island_moves_nothing():
    words = _replace(S_WORDS, 1, 35.56, 36.95)
    assert find_late_runs(words, S_SILENCES, S_DURATION_S, straddle=True) == []


def test_s3_a_straddler_that_starts_before_the_island_is_not_one():
    # The previous pause ends at 36.60 s: "Ve" from 36.40 s starts inside it, not on the voice
    # (1.8 s of stamps on a 0.81 s island would pass the straddle length gate).
    silences = [(34.38, 34.951), (35.785, 36.60), (37.413, 38.486)]
    words = _replace(S_WORDS, 2, 36.40, 37.60)
    assert find_late_runs(words, silences, S_DURATION_S, straddle=True) == []
    assert find_late_runs(_replace(S_WORDS, 2, 36.55, 37.60), silences, S_DURATION_S, straddle=True)


def test_s4_a_word_that_ends_on_the_island_is_not_a_straddler():
    words = _replace(S_WORDS, 2, 37.00, 37.30)
    assert find_late_runs(words, S_SILENCES, S_DURATION_S, straddle=True) == []


def test_s5_a_straddle_may_be_stretched_further_than_a_run_wholly_in_the_pause():
    # T2 itself: 1.20 s of stamps on a 0.574 s island (2.09x) moves as a straddle ...
    assert find_late_runs(S_WORDS, S_SILENCES, S_DURATION_S, straddle=True)
    # ... but not past LATE_RUN_STRADDLE_LENGTH_RATIO (2.5): 1.5 s on 0.574 s is 2.6x.
    stretched = _replace(_replace(S_WORDS, 2, 36.90, 37.60), 3, 37.60, 38.40)
    stretched = _replace(stretched, 4, 38.40, 39.02)
    assert find_late_runs(stretched, S_SILENCES, S_DURATION_S, straddle=True) == []
    # A run wholly in the pause keeps #2374's 2.0. On a 0.463 s island (the previous pause
    # running to 36.95 s), 0.95 s of stamps (2.05x) is refused, 0.90 s (1.94x) moves.
    silences = [(34.38, 34.951), (35.785, 36.95), (37.413, 38.486)]
    whole = [*S_WORDS[:2], ("Ve", 37.45, 37.95), ("4'te", 37.95, 38.40), ("İNKA'lar", 38.4, 39.0)]
    assert find_late_runs(whole, silences, S_DURATION_S, straddle=True) == []
    whole = [*S_WORDS[:2], ("Ve", 37.45, 37.95), ("4'te", 37.95, 38.35), ("İNKA'lar", 38.4, 39.0)]
    assert find_late_runs(whole, silences, S_DURATION_S, straddle=True)


def test_s6_a_second_pass_follows_a_recorded_straddle():
    _times, [run] = _straddled()
    record = run.to_diagnostic()
    second = _pass(("and", 36.98, 37.62), ("fourth", 37.62, 38.25), ("Incas", 38.25, 39.0))
    moved = follow_late_runs(second, [record])
    assert S_ISLAND[0] - 1e-6 <= moved[0]["start_s"] < moved[1]["start_s"] <= S_ISLAND[1]
    assert moved[1]["end_s"] <= S_ISLAND[1] + 1e-6 and moved[2] == second[2]
    # The same words against a run NOT recorded as a straddle: #2374's follow, unchanged.
    plain = {key: value for key, value in record.items() if key != "straddle"}
    followed = follow_late_runs(second, [plain])
    assert followed[0] == second[0]


def test_s7_the_straddling_run_never_starts_before_the_word_before_it_ends():
    words = _replace(S_WORDS, 1, 35.56, 36.90)  # "oluyor." drifts 0.06 s into the island
    times, [run] = _straddled(words=words)
    assert times[run.first][0] == pytest.approx(36.90)
    starts = [start for start, _end in times]
    assert starts == sorted(starts)
