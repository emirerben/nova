"""KRI-479 review fixes: the timing arithmetic of the voice-behind-footage composer.

Written against the failures first:

* the implicit-length shrink counted footage in seconds while the composer allocates in
  whole frames per clip, so a length "equal to the footage" failed with 'add up to 10.9
  seconds, not 10.9, and I won't loop them' (475 of 1198 random footage sets);
* a length the creator never stated (the model's own pick) was declined as a conflict
  instead of being extended so every clip is shown;
* a disclosure said '7 seconds' for 6.53;
* the voice-stops-early slack was a flat 3 s even for a 6 s edit;
* 'ending on a full sentence' was claimed for a cut that was not a sentence end.
"""

from __future__ import annotations

import random

import pytest

from app.pipeline import phone_speech_montage_plan as plan
from app.pipeline.phone_recipe_shared import voice_tail_slack_s
from app.pipeline.phone_speech_montage_plan import (
    VoiceWindow,
    compile_phone_voice_behind_footage_plan,
    implicit_picture_duration,
    picture_frame_bounds,
    select_voice_window,
)
from app.services.creator_render_contract import CreatorRenderContractError
from tests.services.test_phone_speech_montage_job import _binding

FPS = 30


def _clips(lengths):
    return tuple(_binding(f"c{i}", duration_s=length) for i, length in enumerate(lengths))


def _compose(picture, duration_s):
    voice = _binding("talk", duration_s=120.0)
    return compile_phone_voice_behind_footage_plan(
        voice,
        VoiceWindow(start_s=0.3, end_s=0.3 + max(0.5, duration_s - 0.1)),
        picture,
        duration_s=duration_s,
    )


# --- P2-1: the shrink is frame exact --------------------------------------------------


def test_the_reviewers_repro_two_5_51_second_clips_and_no_stated_length_renders():
    picture = _clips([5.51, 5.51])
    length = implicit_picture_duration(picture, speech_s=60.0, target_s=24.0)
    recipe, _ = _compose(picture, length.duration_s)
    assert recipe.duration == pytest.approx(length.duration_s, abs=1 / FPS / 2)
    assert any("footage" in note for note in length.adjustments)


@pytest.mark.parametrize("seed", range(40))
def test_random_footage_never_fails_in_the_implicit_length_path(seed):
    rng = random.Random(seed)
    for _ in range(25):
        n = rng.randint(1, 14)
        picture = _clips([round(rng.uniform(0.3, 20.0), 2) for _ in range(n)])
        speech_s = rng.choice([4.0, 12.5, 30.0, 90.0])
        target_s = rng.choice([5.0, 15.0, 24.0, 30.0, 60.0])
        length = implicit_picture_duration(picture, speech_s=speech_s, target_s=target_s)
        lo, hi = picture_frame_bounds(picture)
        frames = round(length.duration_s * FPS)
        assert frames <= hi
        if lo / FPS <= speech_s:
            assert frames >= lo, "extended to show every clip"
            _compose(picture, length.duration_s)  # must not decline


def test_an_explicit_length_equal_to_the_footage_renders_and_one_beyond_it_declines_honestly():
    picture = _clips([5.51, 5.51])
    lo, hi = picture_frame_bounds(picture)
    recipe, receipt = _compose(picture, hi / FPS)
    assert recipe.duration == pytest.approx(hi / FPS, abs=1 / FPS)
    # one frame or two over what exists (the verifier's tolerance): clamped, and disclosed
    recipe, receipt = _compose(picture, (hi + 2) / FPS)
    assert recipe.duration == pytest.approx(hi / FPS, abs=1 / FPS)
    assert any("footage" in note for note in receipt.adjustments)
    with pytest.raises(CreatorRenderContractError) as info:
        _compose(picture, (hi + 30) / FPS)
    message = str(info.value)
    assert info.value.decline_reason == "requirement_conflict"
    first, second = message.split(" seconds, not ")
    assert first.split()[-1] != second.split()[0], f"the two numbers read the same: {message}"


def test_the_adjustment_names_the_real_length_to_one_decimal():
    picture = _clips([3.3, 3.3])
    length = implicit_picture_duration(picture, speech_s=60.0, target_s=24.0)
    note = next(n for n in length.adjustments if "footage" in n)
    assert f"{length.duration_s:.1f}" in note or f"{length.duration_s:.0f}" in note
    assert "7 seconds" not in note


# --- P2-2: a model-picked length is extended, a stated one is never touched -----------


def test_forty_one_clips_and_a_model_picked_24_seconds_are_extended_not_declined():
    picture = _clips([10.0] * 41)
    length = implicit_picture_duration(picture, speech_s=147.7, target_s=24.0)
    assert length.duration_s == pytest.approx(41 * 24 / FPS)  # 32.8 s
    assert length.adjustments == ["Extended the edit to 32.8 seconds so every clip is shown."]
    recipe, _ = _compose(picture, length.duration_s)
    assert len(recipe.tracks[0].clips) == 41


def test_a_stated_length_is_never_extended_by_the_composer():
    with pytest.raises(CreatorRenderContractError) as info:
        _compose(_clips([10.0] * 41), 30.0)
    assert info.value.decline_reason == "requirement_conflict"
    assert "32.8" in info.value.alternative


def test_the_extension_is_capped_by_the_voice():
    picture = _clips([10.0] * 41)
    length = implicit_picture_duration(picture, speech_s=20.0, target_s=24.0)
    assert length.duration_s <= 20.0 + 1 / FPS
    assert not any("Extended" in n for n in length.adjustments)


# --- P3-1: slack scales with the picture ----------------------------------------------


def test_the_tail_slack_is_three_seconds_or_fifteen_percent_of_the_picture():
    assert voice_tail_slack_s(30.0) == 3.0
    assert voice_tail_slack_s(20.0) == 3.0
    assert voice_tail_slack_s(10.0) == pytest.approx(1.5)
    assert voice_tail_slack_s(6.0) == pytest.approx(0.9)


def test_a_voice_that_stops_well_short_of_a_short_edit_is_not_forgiven():
    picture = _clips([8.0] * 2)
    window = VoiceWindow(start_s=0.3, end_s=0.3 + 6.0)  # 6 s of voice, 10 s edit
    with pytest.raises(CreatorRenderContractError):
        compile_phone_voice_behind_footage_plan(
            _binding("talk", duration_s=60.0), window, picture, duration_s=10.0
        )


def test_any_silent_gap_over_a_second_is_disclosed():
    picture = _clips([20.0] * 2)
    window = VoiceWindow(start_s=0.3, end_s=0.3 + 18.2)  # stops 1.8 s early: within slack
    _recipe, receipt = compile_phone_voice_behind_footage_plan(
        _binding("talk", duration_s=60.0), window, picture, duration_s=20.0
    )
    assert any("without voice" in note for note in receipt.adjustments)
    _recipe, quiet = compile_phone_voice_behind_footage_plan(
        _binding("talk", duration_s=60.0),
        VoiceWindow(start_s=0.3, end_s=0.3 + 19.5),
        picture,
        duration_s=20.0,
    )
    assert not any("without voice" in note for note in quiet.adjustments)


# --- P3-2: only a real sentence end is called one -------------------------------------


def _words(texts, *, step=0.5, gaps=None):
    out, t = [], 0.4
    for i, text in enumerate(texts):
        out.append({"text": text, "start_s": round(t, 3), "end_s": round(t + 0.3, 3)})
        t += step + (gaps or {}).get(i, 0.0)
    return out


def test_a_cut_on_a_word_cap_is_not_called_a_full_sentence():
    # 120 unpunctuated words: the segmenter breaks at its word cap, not at a sentence.
    window = select_voice_window(
        _words([f"w{i}" for i in range(120)]), source_duration_s=90.0, max_length_s=20.0
    )
    assert not any("full sentence" in note for note in window.adjustments)


def test_a_real_sentence_end_inside_the_slack_is_labelled_one():
    texts = [f"w{i}" + ("." if (i + 1) % 5 == 0 else "") for i in range(120)]
    window = select_voice_window(_words(texts), source_duration_s=90.0, max_length_s=20.0)
    assert any("full sentence" in note for note in window.adjustments)
    assert not window.hard_cut


def test_a_pause_inside_the_slack_is_a_natural_pause_not_a_sentence():
    texts = [f"w{i}" for i in range(120)]
    window = select_voice_window(
        _words(texts, gaps={33: 1.0}), source_duration_s=90.0, max_length_s=20.0
    )
    assert any("natural pause" in note for note in window.adjustments)
    assert not any("full sentence" in note for note in window.adjustments)


def test_the_planner_constant_is_shared_with_the_verifier():
    from app.services import creator_render_contract as contract

    assert contract.voice_tail_slack_s is plan.voice_tail_slack_s
