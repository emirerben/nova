"""KRI-1562: the creator's song under a phone Talking edit, ducked wherever speech is heard.

Real boundaries: the real `compile_phone_subtitled_plan` / `compile_phone_voiceover_montage_plan`
lay the clips out and the real `EditRecipeV2` validates the result. Expected windows are worked
out from the clip layout by hand, never read back from the code. Failure modes guarded here:

F1  no music must compile byte-identically to before (digests pinned from the pre-change code)
F2  no persisted treatment must never produce a bed (KRI-20)
F3  windows must follow the clips actually laid out: a pause cut, a removed passage, an insert,
    an extra speaker, a sound cutaway; a deleted caption line must not move them
F4  windows must stay valid for the device: sorted, apart, at most MAX_SPEECH_WINDOWS
F5  the loop must cover the whole video in at most 100 clips without lengthening it
F6  gain and duck must come from the treatment's dB values (0 dB included)
F11 a legacy variant falls back to its caption words; dropping the source windows must leave
    the speech unducked (negative control for the coverage check)
F12 Voiceover: the level under speech stays today's
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.kria.device_render import recipe_digest
from app.kria.portable_visual import AudioMuteWindow
from app.kria.recipes import MAX_SPEECH_WINDOWS, TimelineClip, TimelineTrack
from app.kria.render_assets import LibraryRenderAsset, RenderFingerprint
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import (
    PhoneMusicBed,
    music_bed_from_treatment,
    speech_spans_from_words,
    speech_windows_on_timeline,
)
from app.pipeline.phone_subtitled_plan import (
    PhoneCutaway,
    PhoneInsert,
    compile_phone_subtitled_plan,
)
from app.pipeline.phone_voiceover_montage_plan import voiceover_music_gain
from tests.pipeline.test_phone_subtitled_plan import _CUES, _binding
from tests.pipeline.test_phone_voiceover_montage_plan import (
    compile_phone_voiceover_montage_plan as compile_voiceover,
)
from tests.pipeline.test_phone_voiceover_montage_plan import fixture as voiceover_fixture

TREATMENT = {
    "track_id": "song-1",
    "src_gcs_path": "music/song-1/audio.m4a",
    "section_start_s": 10.0,
    "section_end_s": 18.0,
    "gain_db": -18.0,
    "speech_duck_db": -12.0,
}


def _pin(track_id: str, path: str):
    assert path == f"music/{track_id}/audio.m4a"
    asset = LibraryRenderAsset(
        id=f"music-{track_id}",
        catalog="music",
        catalog_id=track_id,
        generation="5",
        fingerprint=RenderFingerprint(sha256="e" * 64, byte_count=4000),
    )
    return asset, 60.0


def _bed(**changes) -> PhoneMusicBed:
    return music_bed_from_treatment({**TREATMENT, **changes}, _pin)


def _words(*spans):
    return speech_spans_from_words(SimpleNamespace(start_s=s, end_s=e) for s, e in spans)


def _windows(recipe):
    return [(round(w.start, 6), round(w.end, 6)) for w in recipe.audio.speech_windows]


def _covers(recipe, span) -> bool:
    return any(w.start <= span[0] and span[1] <= w.end for w in recipe.audio.speech_windows)


def _music(recipe):
    return next(track for track in recipe.tracks if track.role == "music")


SPEAKER = _binding("speaker", duration_s=10.0)


def _talking(**kwargs):
    return compile_phone_subtitled_plan((SPEAKER,), caption_cues=_CUES, **kwargs)


# --- F1: byte identity ---------------------------------------------------------------------


def test_no_music_compiles_byte_identical_to_before():
    talking = _talking(
        keep_segments=[(0.0, 3.0), (4.0, 10.0)],
        cutaways=(
            PhoneCutaway(
                binding=_binding("cutaway", duration_s=6.0), start_s=5.0, end_s=7.0, sound=True
            ),
        ),
        additional_speakers=(_binding("extra", duration_s=4.0),),
        inserts=(PhoneInsert(binding=_binding("insert", duration_s=2.0), at_take_s=3.0),),
    )
    assert recipe_digest(talking) == (
        "aae39806550c98c2b7e1b0c91dbb9570b101424070cf51713db76d1e1451b036"
    )
    music = PhoneMusicBed(
        catalog_id="track1",
        generation="7",
        fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=500),
        duration_s=120.0,
        start_s=30.5,
        volume=1.0,
    )
    decision, bindings = voiceover_fixture(music_track_id="track1", music_start_s=30.5, mix=0.7)
    voiceover = compile_voiceover(decision, bindings, music=music)
    assert recipe_digest(voiceover) == (
        "c21bf0d79375103f94c2353eff3dfba7d03df72ed15383fe9885f2bfb5ef4c08"
    )


# --- F2: KRI-20 ------------------------------------------------------------------------------


def test_no_treatment_never_pins_or_plays_a_song():
    def refuse(*_):
        raise AssertionError("no treatment must never reach the catalog")

    assert music_bed_from_treatment(None, refuse) is None
    recipe = _talking(music=music_bed_from_treatment(None, refuse))
    assert not any(track.role == "music" for track in recipe.tracks)
    assert "musicBed" not in recipe.required_capabilities
    assert recipe.audio.speech_windows == []


# --- F3: windows follow the laid-out clips ----------------------------------------------------


def test_windows_follow_a_pause_cut_and_drop_the_removed_passage():
    # Kept source 0-3 plays at 0-3; kept source 5-10 plays at 3-8. The word at 3.5-4.5 is cut.
    recipe = _talking(
        keep_segments=[(0.0, 3.0), (5.0, 10.0)],
        music=_bed(),
        speech_source_windows={"speaker": _words((1.0, 2.0), (3.5, 4.5), (6.0, 7.0))},
    )
    assert _windows(recipe) == [(1.0, 2.0), (4.0, 5.0)]
    assert "speechDuck" in recipe.required_capabilities


def test_windows_follow_an_insert_and_duck_under_its_own_sound():
    # A 2 s insert with its sound at take second 3: it plays 3-5, the rest of the take 5-10.
    recipe = _talking(
        keep_segments=[(0.0, 3.0), (5.0, 10.0)],
        inserts=(PhoneInsert(binding=_binding("insert", duration_s=2.0), at_take_s=3.0),),
        music=_bed(),
        speech_source_windows={"speaker": _words((1.0, 2.0), (6.0, 7.0))},
    )
    assert _windows(recipe) == [(1.0, 2.0), (3.0, 5.0), (6.0, 7.0)]


def test_windows_follow_an_additional_speaker():
    extra = _binding("extra", duration_s=4.0)
    recipe = _talking(
        additional_speakers=(extra,),
        music=_bed(),
        speech_source_windows={"speaker": _words((1.0, 2.0)), "extra": _words((0.5, 1.5))},
    )
    assert _windows(recipe) == [(1.0, 2.0), (10.5, 11.5)]


def test_deleting_a_caption_line_leaves_the_windows_alone():
    windows = {"speaker": _words((0.2, 1.4), (1.6, 2.9))}
    with_line = _talking(music=_bed(), speech_source_windows=windows)
    without_line = compile_phone_subtitled_plan(
        (SPEAKER,), caption_cues=_CUES[:1], music=_bed(), speech_source_windows=windows
    )
    assert _windows(without_line) == _windows(with_line) == [(0.2, 2.9)]


def test_a_sound_cutaway_ducks_for_its_whole_window_and_mutes_only_the_speaker():
    # The speaker is muted 5-7 under the cutaway's own sound; the music ducks under that sound.
    recipe = _talking(
        cutaways=(
            PhoneCutaway(
                binding=_binding("cutaway", duration_s=6.0), start_s=5.0, end_s=7.0, sound=True
            ),
        ),
        music=_bed(),
        speech_source_windows={"speaker": _words((1.0, 2.0), (6.5, 8.0))},
    )
    assert _windows(recipe) == [(1.0, 2.0), (5.0, 8.0)]


def test_a_mute_window_only_silences_the_clips_it_names():
    speaker = TimelineClip(
        id="clip-0",
        source_asset_id="a",
        source_start=0.0,
        source_duration=10.0,
        timeline_start=0.0,
        rate=1.0,
    )
    other = TimelineClip(
        id="clip-1",
        source_asset_id="b",
        source_start=0.0,
        source_duration=10.0,
        timeline_start=10.0,
        rate=1.0,
    )
    tracks = [TimelineTrack(id="main", kind="video", clips=[speaker, other])]
    windows = {"a": ((2.0, 4.0),), "b": ((2.0, 4.0),)}
    muted = [AudioMuteWindow(clip_ids=["clip-1"], start=11.0, end=15.0)]
    got = speech_windows_on_timeline(tracks, windows, muted)
    assert [(w.start, w.end) for w in got] == [(2.0, 4.0)]


def test_windows_follow_a_clip_rate():
    clip = TimelineClip(
        id="c",
        source_asset_id="a",
        source_start=4.0,
        source_duration=6.0,
        timeline_start=1.0,
        rate=2.0,
    )
    got = speech_windows_on_timeline(
        [TimelineTrack(id="main", kind="video", clips=[clip])], {"a": ((5.0, 7.0),)}
    )
    assert [(w.start, w.end) for w in got] == [(1.5, 2.5)]


# --- F4: device-valid windows ---------------------------------------------------------------


def test_too_many_windows_close_the_smallest_gaps_and_still_validate():
    # 450 words 0.1 s long; every tenth gap is 0.3 s, the rest 0.5 s.
    spans, at = [], 0.0
    for index in range(450):
        spans.append((round(at, 6), round(at + 0.1, 6)))
        at += 0.1 + (0.3 if index % 10 == 9 else 0.5)
    speaker = _binding("speaker", duration_s=290.0)
    recipe = compile_phone_subtitled_plan(
        (speaker,), caption_cues=[], music=_bed(), speech_source_windows={"speaker": tuple(spans)}
    )
    windows = recipe.audio.speech_windows
    assert len(windows) == MAX_SPEECH_WINDOWS
    assert all(b.start - a.end >= 0.5 - 1e-6 for a, b in zip(windows, windows[1:]))
    assert all(_covers(recipe, span) for span in spans)


# --- F5 / F6: the bed itself ------------------------------------------------------------------


def test_the_bed_loops_its_section_over_the_whole_video_without_lengthening_it():
    plain = _talking()
    recipe = _talking(music=_bed(), speech_source_windows={"speaker": ()})
    track = _music(recipe)
    assert recipe.duration == plain.duration == 10.0
    clips = track.clips
    assert [(c.timeline_start, c.source_start, c.source_duration) for c in clips] == [
        (0.0, 10.0, 8.0),
        (8.0, 10.0, 2.0),
    ]
    assert clips[0].audio_fade_in == 0.5 and clips[0].audio_fade_out is None
    assert clips[-1].audio_fade_out == 0.5 and clips[-1].audio_fade_in is None
    assert {"musicBed", "audioMix"} <= recipe.required_capabilities
    assert "speechDuck" not in recipe.required_capabilities  # nobody speaks: no duck


def test_a_section_too_short_for_100_repeats_is_refused():
    speaker = _binding("speaker", duration_s=60.0)
    with pytest.raises(UnsupportedPhonePlan) as refused:
        compile_phone_subtitled_plan(
            (speaker,),
            caption_cues=[],
            music=_bed(section_start_s=10.0, section_end_s=10.5),
            speech_source_windows={},
        )
    assert refused.value.reason == "music_section_too_short"


def test_gain_and_duck_come_from_the_treatment():
    assert _bed().volume == pytest.approx(0.125893, abs=1e-6)  # -18 dB
    assert _bed().speech_duck_gain == pytest.approx(0.251189, abs=1e-6)  # -12 dB
    assert _bed(gain_db=0.0).volume == 1.0  # the editor's top level is 0 dB, not -18
    assert _bed(speech_duck_db=-50.0).speech_duck_gain == pytest.approx(0.031623, abs=1e-6)
    recipe = _talking(music=_bed(), speech_source_windows={"speaker": _words((1.0, 2.0))})
    assert recipe.audio.speech_duck_gain == _bed().speech_duck_gain
    assert {c.volume for c in _music(recipe).clips} == {_bed().volume}


# --- F11: legacy fallback and the negative control ----------------------------------------------


def test_a_legacy_variant_ducks_under_its_caption_words():
    recipe = _talking(music=_bed())  # no stored windows
    assert _windows(recipe) == [(0.0, 3.0)]


def test_dropping_the_source_windows_leaves_speech_unducked():
    spoken = (1.0, 2.0)
    known = _talking(music=_bed(), speech_source_windows={"speaker": _words(spoken)})
    dropped = _talking(music=_bed(), speech_source_windows={})
    assert _covers(known, spoken)
    assert not _covers(dropped, spoken)


# --- F12: Voiceover ----------------------------------------------------------------------------


VOICEOVER_MUSIC = PhoneMusicBed(
    catalog_id="track1",
    generation="7",
    fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=500),
    duration_s=120.0,
    start_s=30.5,
    volume=1.0,
)


def _voiceover(mix=0.7, windows=None):
    decision, bindings = voiceover_fixture(music_track_id="track1", music_start_s=30.5, mix=mix)
    from app.pipeline.phone_voiceover_montage_plan import (
        compile_phone_voiceover_montage_plan,
    )
    from tests.pipeline.test_phone_voiceover_montage_plan import _narration

    return compile_phone_voiceover_montage_plan(
        decision,
        bindings,
        music=VOICEOVER_MUSIC,
        narration=_narration(),
        speech_source_windows=windows,
    )


def test_voiceover_keeps_todays_level_under_speech_and_rises_in_the_pauses():
    recipe = _voiceover(windows={"voiceover-item-1": ((0.5, 3.0), (6.0, 9.0))})
    [clip] = _music(recipe).clips
    level_under_speech = clip.volume * recipe.audio.speech_duck_gain
    assert level_under_speech == pytest.approx(voiceover_music_gain(0.7), abs=1e-6)
    assert clip.volume > voiceover_music_gain(0.7)
    assert _windows(recipe) == [(0.5, 3.0), (6.0, 9.0)]


def test_voiceover_loudest_bed_stays_inside_the_clip_limit_with_todays_speech_level():
    recipe = _voiceover(mix=0.0, windows={"voiceover-item-1": ((0.5, 3.0),)})
    [clip] = _music(recipe).clips
    assert clip.volume <= 2.0
    assert clip.volume * recipe.audio.speech_duck_gain == pytest.approx(
        voiceover_music_gain(0.0), abs=1e-6
    )


def test_voiceover_without_windows_or_with_a_silent_bed_is_todays_recipe():
    today = recipe_digest(_voiceover())
    assert recipe_digest(_voiceover(mix=1.0, windows={"voiceover-item-1": ((0.5, 3.0),)})) == (
        recipe_digest(_voiceover(mix=1.0))
    )
    assert recipe_digest(_voiceover(windows=None)) == today
