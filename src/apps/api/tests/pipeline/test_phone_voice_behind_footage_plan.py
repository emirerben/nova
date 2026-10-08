"""KRI-479: `compile_phone_voice_behind_footage_plan` -- one voice under silent footage.

Written from the ways it can fail, before the code:

* the voice stops early / plays twice / is replaced by another clip's sound;
* a clip's own sound leaks (an unmuted picture clip);
* the picture wraps (a clip shown twice) or is reordered;
* the voice clip's own picture appears although the plan hides it;
* a shot flashes by below the readable floor;
* the timeline silently stretches (voice or title running past the picture);
* the title invents or drops words;
* the voice shorter than the edit quietly renders a different length.

Every composer input is a typed fact: there is no request text to read.
"""

from __future__ import annotations

import inspect

import pytest

from app.kria.render_assets import OriginalRenderAsset
from app.pipeline import phone_speech_montage_plan as plan
from app.pipeline.phone_speech_montage_plan import (
    VOICE_AUDIO_TRACK_ID,
    VOICE_FOOTAGE_TRACK_ID,
    VOICE_TAIL_SLACK_S,
    VoiceWindow,
    compile_phone_voice_behind_footage_plan,
    select_voice_window,
)
from app.services.creator_render_contract import CreatorRenderContractError
from app.services.phone_rollout import validate_phone_pilot_recipe
from tests.services.test_phone_speech_montage_job import _binding

FRAME = 1 / 30


def _picture(count: int, *, duration_s: float = 20.0):
    return tuple(_binding(f"c{i}", duration_s=duration_s) for i in range(count))


def _voice(duration_s: float = 90.0):
    return _binding("talk", duration_s=duration_s)


def _words(count: int = 120, *, step: float = 0.5, sentence: int = 10, start: float = 0.4):
    out, t = [], start
    for i in range(count):
        mark = "." if (i + 1) % sentence == 0 else ""
        out.append({"text": f"w{i}{mark}", "start_s": round(t, 3), "end_s": round(t + 0.3, 3)})
        t += step
    return out


def _compile(duration_s=30.0, picture=None, **kw):
    voice_window = kw.pop("voice_window", VoiceWindow(start_s=0.34, end_s=duration_s - 0.2))
    return compile_phone_voice_behind_footage_plan(
        _voice(),
        voice_window,
        picture if picture is not None else _picture(6),
        duration_s=duration_s,
        **kw,
    )


def _footage(recipe):
    return next(t for t in recipe.tracks if t.id == VOICE_FOOTAGE_TRACK_ID).clips


def _voice_clips(recipe):
    return next(t for t in recipe.tracks if t.id == VOICE_AUDIO_TRACK_ID).clips


def _decline(exc_info) -> CreatorRenderContractError:
    assert isinstance(exc_info.value, CreatorRenderContractError)
    assert exc_info.value.decline_reason and exc_info.value.alternative
    return exc_info.value


# --- recipe shape ---------------------------------------------------------------------


def test_recipe_is_one_voice_clip_over_each_picture_clip_once_in_order(monkeypatch):
    from app.config import settings

    recipe, receipt = _compile(30.0, _picture(6))
    clips = _footage(recipe)
    assert [c.source_asset_id for c in clips] == [f"c{i}" for i in range(6)]
    assert all(c.volume == 0 for c in clips)  # the picture clips' own sound never plays
    voice = _voice_clips(recipe)
    assert len(voice) == 1
    assert voice[0].source_asset_id == "talk" and voice[0].volume == 1
    assert voice[0].timeline_start == 0
    # the voice clip's own picture is on no video track
    assert "talk" not in {c.source_asset_id for c in clips}
    assert abs(recipe.duration - 30.0) <= FRAME / 2
    assert receipt.duration_s == pytest.approx(30.0, abs=FRAME / 2)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)  # no mute windows, only verified features


def test_the_voice_never_extends_the_timeline_and_no_music_bed_is_added():
    recipe, _ = _compile(30.0, _picture(4))
    voice = _voice_clips(recipe)[0]
    assert (
        voice.timeline_start + voice.source_duration
        <= recipe.duration - plan.EXPORT_SAFETY_MARGIN_S + 1e-6
    )
    assert recipe.audio.music_asset_id is None
    assert recipe.audio.mute_windows == []
    assert not {"musicBed", "narrationAudio"} & set(recipe.required_capabilities)
    assert "audioMix" in recipe.required_capabilities


def test_shots_are_frame_aligned_contiguous_and_sum_to_the_duration():
    recipe, _ = _compile(30.0, _picture(7))
    clips = _footage(recipe)
    cursor = 0.0
    for clip in clips:
        assert clip.timeline_start == pytest.approx(cursor, abs=1e-3)
        assert clip.source_duration * 30 == pytest.approx(
            round(clip.source_duration * 30), abs=2e-3
        )
        cursor += clip.source_duration
    assert cursor == pytest.approx(30.0, abs=FRAME / 2)


@pytest.mark.parametrize("count", [1, 2, 11, 30])
def test_no_shot_flashes_below_the_readable_floor(count):
    duration = max(30.0, count * 0.8 + 1)
    recipe, _ = _compile(duration, _picture(count, duration_s=duration / count + 5))
    assert len(_footage(recipe)) == count
    assert all(c.source_duration >= 0.8 - 1e-6 for c in _footage(recipe))


def test_a_clip_shorter_than_the_floor_is_shown_whole_not_stretched_or_wrapped():
    picture = (*_picture(3), _binding("tiny", duration_s=0.7), *_picture(2, duration_s=20.0))
    recipe, _ = _compile(20.0, picture)
    tiny = [c for c in _footage(recipe) if c.source_asset_id == "tiny"]
    assert len(tiny) == 1 and tiny[0].source_duration <= 0.7


def test_single_picture_clip_holds_the_whole_edit():
    recipe, _ = _compile(10.0, _picture(1, duration_s=15.0))
    assert len(_footage(recipe)) == 1
    assert _footage(recipe)[0].source_duration == pytest.approx(10.0, abs=FRAME)


def test_each_clip_is_used_once_even_when_the_footage_is_barely_long_enough():
    # 5 clips x 6 s usable = ~29.75 s: a wrap-around planner would repeat; this must not.
    recipe, _ = _compile(29.0, _picture(5, duration_s=6.0))
    ids = [c.source_asset_id for c in _footage(recipe)]
    assert ids == [f"c{i}" for i in range(5)]


def test_picture_order_is_exactly_the_order_given_not_a_sort():
    picture = tuple(_binding(m, duration_s=20.0) for m in ("z", "a", "m", "b"))
    recipe, _ = _compile(24.0, picture)
    assert [c.source_asset_id for c in _footage(recipe)] == ["z", "a", "m", "b"]


# --- declines (typed, with an alternative) -----------------------------------------------


def test_too_many_clips_for_the_floor_declines_instead_of_flash_cutting():
    with pytest.raises(CreatorRenderContractError) as info:
        _compile(15.0, _picture(30))
    exc = _decline(info)
    assert exc.decline_reason == "requirement_conflict"
    assert exc.field_path == "target_duration_s"
    assert "24" in exc.alternative  # 30 x 0.8 s: the length that would fit


def test_footage_shorter_than_the_edit_declines_instead_of_looping():
    with pytest.raises(CreatorRenderContractError) as info:
        _compile(60.0, _picture(3, duration_s=5.0))
    exc = _decline(info)
    assert exc.decline_reason == "requirement_conflict"
    assert exc.field_path == "target_duration_s"


def test_a_voice_shorter_than_the_edit_declines_unless_a_silent_tail_was_chosen():
    short = VoiceWindow(start_s=0.3, end_s=12.0)
    with pytest.raises(CreatorRenderContractError) as info:
        _compile(30.0, voice_window=short)
    exc = _decline(info)
    assert exc.decline_reason == "requirement_conflict" and exc.field_path == "target_duration_s"
    recipe, receipt = _compile(30.0, voice_window=short, allow_silent_tail=True)
    assert abs(recipe.duration - 30.0) <= FRAME / 2  # the picture keeps the asked length
    assert _voice_clips(recipe)[0].source_duration == pytest.approx(11.7, abs=0.01)
    assert any("without" in a for a in receipt.adjustments)


def test_voice_within_the_sentence_slack_of_the_end_is_accepted_without_a_tail_choice():
    window = VoiceWindow(start_s=0.3, end_s=0.3 + 30.0 - VOICE_TAIL_SLACK_S + 0.2)
    recipe, _ = _compile(30.0, voice_window=window)
    assert recipe.duration == pytest.approx(30.0, abs=FRAME)


def test_a_long_voice_is_trimmed_to_the_edit_and_the_trim_is_disclosed():
    window = VoiceWindow(start_s=0.3, end_s=120.0)
    recipe, receipt = _compile(30.0, voice_window=window)
    voice = _voice_clips(recipe)[0]
    assert voice.source_duration <= 30.0 - plan.EXPORT_SAFETY_MARGIN_S + 1e-6
    assert abs(recipe.duration - 30.0) <= FRAME / 2  # the voice did not stretch the timeline
    assert any("first" in a for a in receipt.adjustments)
    assert (voice.audio_fade_out or 0) >= 0.18  # a cut mid-speech never clicks


def test_the_voice_clip_inside_the_picture_is_refused():
    picture = (_binding("talk", duration_s=20.0), *_picture(3))
    with pytest.raises(CreatorRenderContractError) as info:
        _compile(20.0, picture)
    assert _decline(info).decline_reason == "requirement_conflict"


def test_no_other_clips_declines():
    with pytest.raises(CreatorRenderContractError) as info:
        _compile(10.0, ())
    assert _decline(info).decline_reason == "capability_unavailable"


def test_a_voice_clip_without_audio_declines():
    quiet = _binding("talk", duration_s=40.0)
    quiet = quiet.model_copy(
        update={"original": quiet.original.model_copy(update={"has_audio": False})}
    )
    with pytest.raises(CreatorRenderContractError) as info:
        compile_phone_voice_behind_footage_plan(
            quiet, VoiceWindow(start_s=0.3, end_s=20.0), _picture(3), duration_s=20.0
        )
    assert _decline(info).decline_reason == "capability_unavailable"


# --- opening text ---------------------------------------------------------------------


def test_opening_text_is_the_approved_words_for_the_approved_hold():
    recipe, receipt = _compile(
        30.0, _picture(5), opening_title="Summer in Lisbon", opening_title_hold_s=3.0
    )
    assert len(recipe.text_layers) == 1
    layer = recipe.text_layers[0]
    assert " ".join(run.text for run in layer.runs).split() == [
        "Summer",
        "in",
        "Lisbon",
    ]
    assert layer.start <= FRAME and layer.end - layer.start + FRAME >= 3.0
    assert layer.end <= recipe.duration
    assert {"positionedText"} <= set(recipe.required_capabilities)
    assert receipt.title_hold_s == pytest.approx(3.0, abs=FRAME)


def test_no_title_means_no_text_layer_and_no_invented_words():
    recipe, _ = _compile(30.0, _picture(5))
    assert recipe.text_layers == []
    assert "positionedText" not in recipe.required_capabilities


def test_a_title_hold_longer_than_the_edit_cannot_stretch_the_timeline():
    recipe, _ = _compile(
        6.0, _picture(3, duration_s=10.0), opening_title="Hi", opening_title_hold_s=20.0
    )
    assert recipe.text_layers[0].end <= recipe.duration + 1e-6
    assert recipe.duration == pytest.approx(6.0, abs=FRAME)


# --- voice window selection ---------------------------------------------------------------


def test_window_starts_at_the_first_word_and_ends_on_a_sentence_inside_the_cap():
    words = _words(count=120, sentence=5)  # ~60 s of speech, a sentence every ~2.5 s
    window = select_voice_window(words, source_duration_s=90.0, max_length_s=29.95)
    assert window.start_s == pytest.approx(0.4 - 0.06, abs=0.01)
    assert window.length_s <= 29.95 + 1e-6
    assert 29.95 - window.length_s <= VOICE_TAIL_SLACK_S
    assert not window.hard_cut
    ends = {round(w["end_s"], 3) for w in words if w["text"].endswith(".")}
    assert any(abs(window.end_s - (e + 0.22)) < 0.3 for e in ends)


def test_window_without_sentences_cuts_on_a_word_and_says_so():
    words = _words(count=120, sentence=500)  # no punctuation at all
    window = select_voice_window(words, source_duration_s=90.0, max_length_s=20.0)
    assert window.hard_cut
    assert window.length_s <= 20.0 + 1e-6
    assert any("first" in a for a in window.adjustments)
    boundaries = {round(w["end_s"], 2) for w in words}
    assert any(abs(window.end_s - 0.22 - b) < 0.02 for b in boundaries)


def test_window_of_a_short_take_plays_all_of_the_speech():
    words = _words(count=20)
    window = select_voice_window(words, source_duration_s=30.0, max_length_s=29.95)
    assert window.end_s == pytest.approx(words[-1]["end_s"] + 0.22, abs=0.02)
    assert not window.hard_cut


def test_window_never_runs_past_the_source():
    words = _words(count=20)
    window = select_voice_window(words, source_duration_s=6.0, max_length_s=None)
    assert window.end_s <= 6.0 - plan.EXPORT_SAFETY_MARGIN_S + 1e-6


def test_a_clip_with_next_to_no_speech_is_not_a_voice():
    with pytest.raises(CreatorRenderContractError) as info:
        select_voice_window(_words(count=3), source_duration_s=30.0, max_length_s=20.0)
    exc = _decline(info)
    assert exc.decline_reason == "capability_unavailable"
    assert exc.field_path == "montage_audio.source_media_ids[]"
    with pytest.raises(CreatorRenderContractError):
        select_voice_window([], source_duration_s=30.0, max_length_s=20.0)


# --- purity -----------------------------------------------------------------------------


def test_the_composer_signature_takes_no_request_text():
    names = set(inspect.signature(compile_phone_voice_behind_footage_plan).parameters)
    assert not {n for n in names if "request" in n or "message" in n or "prompt" in n}
    assert "creator_request" not in inspect.getsource(compile_phone_voice_behind_footage_plan)


def test_assets_are_the_originals_not_copies():
    recipe, _ = _compile(20.0, _picture(3))
    manifest = {a.id: a for a in recipe.asset_manifest.assets}
    for media_id in ("talk", "c0", "c1", "c2"):
        assert isinstance(manifest[media_id], OriginalRenderAsset)
        assert manifest[media_id].media_id == media_id
