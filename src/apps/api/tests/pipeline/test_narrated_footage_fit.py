"""KRI-1466: a Voiceover clip starts on the moment the creator named, and is never slowed to
fill the rest of the voice.

Kria TikTok video 1 (job c8fcb7c0, 2026-11-14, phone render): "show ... the TOR on the big
screen on "Tor"". The goal clip (7.703 s) came on screen on the word at 9.76 s, from its first
frame, and was the last clip, so it was stretched over the remaining 15.4 s of voice (rate
0.496). The big screen reads TOR at 2.76-4.5 s of the clip (its analysed moment "jumbotron
displays 'TOR'"), which played 5.6 s after the word. Three other clips had 6.5-7 s of footage
nobody saw.

`fit_steps_to_footage` is pure (steps, clip lengths and located moments in; steps out), so it
is pinned here in isolation; `tests/tasks/test_kri1466_narrated_named_moment.py` replays the
job. Ways it can fail, written before the code:

 P1  the clip still starts at its first frame, so the named moment comes late (the incident).
 P2  a clip shorter than its step is slowed instead of other footage coming in (the incident).
 P3  the steps stop tiling the voiceover (a gap, an overlap, or an end that is not the voice's
     end): the compiler refuses the plan and the creator loses the video.
 P4  a step reads past the end of its clip, so the compiler slows it after all.
 P5  the in-point lands so close to the clip's end that almost nothing of it plays.
 P6  a clip that is only a little short (or a gap too short for its own cut) is chopped up.
 P7  a filler is a flash (under the shortest step) or one filler runs on and on.
 P8  footage repeats while unseen footage is left, or the same clip plays twice in a row.
 P9  there is no unseen footage left (the voice is longer than everything filmed): the fit
     loops or raises; or it replays footage the creator never asked to see twice ("only
     creator wording grants reuse"). Without that ask the step stays as it was; with it the
     footage plays again at normal speed.
 P10 a clip the creator asked to end on gets fillers after it.
 P11 a step that already fits, a clip of unknown length, or a moment that is already on screen
     at the start is changed at all (every existing render must stay as it is).
 P12 two steps get the same id (the compiler refuses), or the result differs between runs.
 P13 the receipt's "when does the moment show" is wrong: on time when late, or late when the
     moment is not in the part that plays.
 P14 a long voice over short clips makes more clips than the editor's timeline holds.
 P15 (review) starting on the moment slows a clip that used to fit, when the gap it leaves is
     too short to fill.
"""

from __future__ import annotations

import pytest

from app.pipeline.narrated_footage_fit import (
    MAX_FILL_S,
    MIN_FILL_S,
    fit_steps_to_footage,
)
from app.pipeline.phone_narrated_plan import _EOF_GUARD_S, NarratedPhoneStep

TRAIN, PAN, LIGHTS, GOAL = "train", "pan", "lights", "goal"
# The recorded job: clip lengths (the phone's own measurements) and the aligned steps.
DURATIONS = {TRAIN: 7.835, PAN: 8.533, LIGHTS: 8.035, GOAL: 7.703}
VOICE_END = 25.183
TOR = (2.76, 4.5)  # "jumbotron displays 'TOR'", in the goal clip's own seconds


def _step(step_id: str, media_id: str, start_s: float, end_s: float, **extra) -> NarratedPhoneStep:
    return NarratedPhoneStep(
        step_id=step_id, media_id=media_id, start_s=start_s, end_s=end_s, **extra
    )


def _incident() -> list[NarratedPhoneStep]:
    return [
        _step("seg_0", TRAIN, 0.0, 6.76),
        _step("seg_1", PAN, 6.76, 8.26),
        _step("seg_2", LIGHTS, 8.26, 9.76),
        _step("seg_3", GOAL, 9.76, VOICE_END),
    ]


def _rate(step: NarratedPhoneStep, durations=DURATIONS) -> float:
    """The speed the compiler (`_fit_step_window`) plays this step at."""
    window = step.end_s - step.start_s
    available = durations[step.media_id] - step.source_start_s
    return 1.0 if available >= window else (available - _EOF_GUARD_S) / window


def _assert_tiles(steps: list[NarratedPhoneStep], end_s: float) -> None:
    assert steps[0].start_s == 0.0
    for before, after in zip(steps, steps[1:]):
        assert after.start_s == before.end_s, (before, after)
        assert before.end_s > before.start_s
    assert steps[-1].end_s == end_s


def _played(steps: list[NarratedPhoneStep]) -> dict[str, list[tuple[float, float]]]:
    out: dict[str, list[tuple[float, float]]] = {}
    for step in steps:
        out.setdefault(step.media_id, []).append(
            (step.source_start_s, step.source_start_s + step.end_s - step.start_s)
        )
    return out


def test_p1_p2_the_goal_clip_starts_on_tor_and_nothing_is_slowed() -> None:
    fit = fit_steps_to_footage(_incident(), durations=DURATIONS, moments={"seg_3": TOR})

    by_id = {step.step_id: step for step in fit.steps}
    goal = by_id["seg_3"]
    # P1: on the word (9.76 s) the clip is at the TOR screen, not at its first frame.
    assert goal.start_s == 9.76
    assert goal.source_start_s == pytest.approx(TOR[0])
    assert fit.moment_at_s == {"seg_3": pytest.approx(9.76)}
    # P2: every step plays at normal speed.
    assert [_rate(step) for step in fit.steps] == [1.0] * len(fit.steps)
    # ... and the goal clip plays what is left of it, then other footage comes in.
    assert goal.end_s - goal.start_s == pytest.approx(DURATIONS[GOAL] - TOR[0] - _EOF_GUARD_S)
    fillers = [step for step in fit.steps if step.step_id in fit.fill_step_ids]
    assert fillers and fillers[0].start_s == goal.end_s
    assert {step.media_id for step in fillers} == {PAN, LIGHTS}
    # P3: the steps still tile the whole voiceover.
    _assert_tiles(fit.steps, VOICE_END)
    # The three steps before the goal are untouched.
    assert fit.steps[:3] == _incident()[:3]


def test_p4_p7_p8_fillers_are_unseen_footage_in_whole_cuts() -> None:
    fit = fit_steps_to_footage(_incident(), durations=DURATIONS, moments={"seg_3": TOR})

    fillers = [step for step in fit.steps if step.step_id in fit.fill_step_ids]
    for step in fit.steps:
        # P4: no step reads past its clip.
        played_until = step.source_start_s + (step.end_s - step.start_s)
        assert played_until <= DURATIONS[step.media_id] - _EOF_GUARD_S + 1e-6, step
    for step in fillers:
        # P7: a filler is a real cut, never a flash and never the whole rest.
        assert MIN_FILL_S - 1e-6 <= step.end_s - step.start_s <= MAX_FILL_S + 1e-6, step
    # P8: nothing is shown twice ...
    for media_id, spans in _played(fit.steps).items():
        ordered = sorted(spans)
        for (_a, end), (start, _b) in zip(ordered, ordered[1:]):
            assert start >= end - 1e-6, (media_id, ordered)
    # ... and no clip follows itself.
    for before, after in zip(fit.steps, fit.steps[1:]):
        assert before.media_id != after.media_id, (before, after)


def test_p5_a_moment_at_the_very_end_still_leaves_a_real_cut() -> None:
    steps = [_step("seg_0", TRAIN, 0.0, 4.0), _step("seg_1", GOAL, 4.0, 8.0)]

    fit = fit_steps_to_footage(steps, durations=DURATIONS, moments={"seg_1": (7.2, 7.6)})

    goal = next(step for step in fit.steps if step.step_id == "seg_1")
    # The clip starts as late as it can while still playing a whole shortest step.
    assert goal.source_start_s == pytest.approx(DURATIONS[GOAL] - _EOF_GUARD_S - MIN_FILL_S)
    assert goal.end_s - goal.start_s >= MIN_FILL_S - 1e-6
    # P13: the moment then shows a little after the clip comes on, and the receipt says when.
    assert fit.moment_at_s["seg_1"] == pytest.approx(4.0 + 7.2 - goal.source_start_s)
    _assert_tiles(fit.steps, 8.0)


def test_p6_a_mild_stretch_and_a_tiny_gap_are_unchanged() -> None:
    mild = [_step("seg_0", TRAIN, 0.0, 5.0), _step("seg_1", GOAL, 5.0, 5.0 + 7.703 / 0.85)]
    tiny = [_step("seg_0", TRAIN, 0.0, 5.0), _step("seg_1", "short", 5.0, 8.4)]

    assert fit_steps_to_footage(mild, durations=DURATIONS).steps == mild
    # 2.0 s of footage for 3.4 s: slowed below the limit, but 1.4 s is no cut of its own.
    fit = fit_steps_to_footage(tiny, durations={**DURATIONS, "short": 2.05})
    assert fit.steps == tiny and not fit.fill_step_ids


def test_p9_with_no_unseen_footage_left_nothing_repeats_unless_the_creator_asked() -> None:
    durations = {"a": 4.0, "b": 4.0}
    steps = [_step("seg_0", "a", 0.0, 3.95), _step("seg_1", "b", 3.95, 30.0)]

    # "Only creator wording grants reuse": without it the step is slowed, as it always was.
    assert fit_steps_to_footage(steps, durations=durations).steps == steps
    # Unseen footage that covers only part of the gap is not used up for half a fix either.
    partly = [_step("seg_0", "a", 0.0, 2.0), _step("seg_1", "b", 2.0, 30.0)]
    assert fit_steps_to_footage(partly, durations=durations).steps == partly

    fit = fit_steps_to_footage(steps, durations=durations, allow_replay=True)

    _assert_tiles(fit.steps, 30.0)
    assert len(fit.steps) > 4
    assert min(_rate(step, durations) for step in fit.steps) == 1.0
    for before, after in zip(fit.steps, fit.steps[1:]):
        assert before.media_id != after.media_id


def test_p9_one_short_clip_alone_keeps_the_old_stretch() -> None:
    """Nothing else to show: the single clip is slowed as before (never a refusal)."""
    steps = [_step("seg_0", "a", 0.0, 12.0)]

    assert fit_steps_to_footage(steps, durations={"a": 4.0}).steps == steps


def test_p10_a_clip_the_creator_ends_on_gets_no_fillers_after_it() -> None:
    fit = fit_steps_to_footage(
        _incident(), durations=DURATIONS, moments={"seg_3": TOR}, keep_whole={"seg_3"}
    )

    # Nothing may follow it, so it is slowed over the rest exactly as before: starting on the
    # moment would leave less footage and slow it more.
    assert fit.steps == _incident() and not fit.fill_step_ids
    # P13: the receipt hears how late the moment then plays (2.76 s in, at 0.496x).
    rate = (DURATIONS[GOAL] - _EOF_GUARD_S) / (VOICE_END - 9.76)
    assert fit.moment_at_s == {"seg_3": pytest.approx(9.76 + TOR[0] / rate, abs=1e-3)}
    # When the clip (nearly) fits from the moment on, it does start there.
    short = [*_incident()[:3], _step("seg_3", GOAL, 9.76, 9.76 + 5.5)]
    fit = fit_steps_to_footage(
        short, durations=DURATIONS, moments={"seg_3": TOR}, keep_whole={"seg_3"}
    )
    assert fit.steps[-1].source_start_s == pytest.approx(TOR[0])
    assert fit.moment_at_s == {"seg_3": pytest.approx(9.76)}


def test_p11_steps_that_fit_are_returned_as_they_came() -> None:
    steps = [
        _step("seg_0", TRAIN, 0.0, 4.0),
        _step("seg_1", PAN, 4.0, 9.0),
        _step("seg_2", "unknown", 9.0, 40.0),
    ]

    fit = fit_steps_to_footage(
        steps,
        durations=DURATIONS,
        # Already on screen when the clip comes on, and a moment for a clip of unknown length.
        moments={"seg_0": (0.3, 2.0), "seg_2": (5.0, 6.0)},
    )

    assert fit.steps == steps
    assert not fit.fill_step_ids
    assert fit.moment_at_s == {"seg_0": pytest.approx(0.3)}


def test_p12_ids_are_unique_and_the_fit_is_stable() -> None:
    first = fit_steps_to_footage(_incident(), durations=DURATIONS, moments={"seg_3": TOR})
    again = fit_steps_to_footage(_incident(), durations=DURATIONS, moments={"seg_3": TOR})

    ids = [step.step_id for step in first.steps]
    assert len(set(ids)) == len(ids)
    assert first == again
    assert all(step_id.startswith("seg_3_fill") for step_id in first.fill_step_ids)


def test_p13_a_moment_that_never_plays_is_reported_so() -> None:
    # Kept whole, and the moment (7.2 s in) is past the 3.1 s of the clip that plays.
    late = fit_steps_to_footage(
        [_step("seg_0", TRAIN, 0.0, 4.0), _step("seg_1", GOAL, 4.0, 7.106)],
        durations=DURATIONS,
        moments={"seg_1": (7.2, 7.6)},
        keep_whole={"seg_1"},
    )
    assert late.steps[1].source_start_s == 0.0
    assert late.moment_at_s == {"seg_1": None}
    # An in-point somebody else set (a voice tail) is never moved; a moment before it never
    # plays, and the receipt hears that (None), not "on time".
    gone = fit_steps_to_footage(
        [_step("seg_0", GOAL, 0.0, 2.0, source_start_s=5.0), _step("seg_1", TRAIN, 2.0, 6.0)],
        durations=DURATIONS,
        moments={"seg_0": TOR},
    )
    assert gone.steps[0].source_start_s == 5.0
    assert gone.moment_at_s == {"seg_0": None}


def test_p14_a_fill_never_passes_the_editors_clip_limit() -> None:
    fit = fit_steps_to_footage(
        _incident(), durations=DURATIONS, moments={"seg_3": TOR}, max_steps=6
    )

    # Four fillers would make eight clips: the step stays as it came, slowed as before.
    assert fit.steps == _incident() and not fit.fill_step_ids
    # ... and the footage it would have used is still free for a fit that has room.
    roomy = fit_steps_to_footage(
        _incident(), durations=DURATIONS, moments={"seg_3": TOR}, max_steps=8
    )
    assert len(roomy.steps) == 8


def test_p15_starting_on_the_moment_never_slows_a_clip_that_fitted() -> None:
    """Review finding: the moment (6 s in) leaves 1.65 s for a 3 s step and 1.35 s is no cut
    of its own, so the moved clip would play at 0.55x where it played at normal speed."""
    steps = [_step("seg_0", TRAIN, 0.0, 4.0), _step("seg_1", GOAL, 4.0, 7.0)]

    fit = fit_steps_to_footage(steps, durations=DURATIONS, moments={"seg_1": (6.0, 6.5)})

    assert fit.steps == steps
    assert [_rate(step) for step in fit.steps] == [1.0, 1.0]
    # The receipt hears that the part that plays (0-3 s) never reaches the moment.
    assert fit.moment_at_s == {"seg_1": None}
