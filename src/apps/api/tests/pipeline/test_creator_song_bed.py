"""KRI-1578: the creator's own uploaded song as the music bed under a phone Talking edit.

Real boundaries: the real `compile_phone_subtitled_plan` lays the clips out, the real bed
compiler loops the song and the real `EditRecipeV2` validates the result. Expected clip spans
are worked out by hand from the song and video lengths, never read back from the code.

Failure modes guarded here:

B1  every bed that already ships (Talking library song, Voiceover and speech-montage library
    beds, guided background and lip-sync songs) compiles byte-identically after the bed gained
    a second source (digests pinned from the pre-change code)
B2  the song plays twice: `audio.music_asset_id` set makes the engine play the asset as a second
    bed from 0 s (KRI-481), so the song rides only a `role="music"` track
B3  the song is not her upload (a library asset), or not pinned to the generation she uploaded
B4  a song shorter than the video stops early instead of looping, or the loop lengthens the video
B5  a song longer than the video plays past its end, or ignores her start and end points
B6  a window too short to loop under the video in 100 clips is played cut short, not refused
B7  her level or the duck is not the Talking one (-18 dB gap, -12 dB under speech), the voice is
    turned down, or the song never ducks on her words
B8  choices made for one upload play over another (a replaced song keeps an old window)
"""

from __future__ import annotations

import pytest

from app.kria.render_assets import RenderFingerprint, SongRenderAsset
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.phone_recipe_shared import (
    CreatorSongSource,
    LibraryMusicSource,
    PhoneMusicBed,
    music_bed_from_user_song,
)
from app.services.talking_music import UploadedSong, default_talking_song
from tests.pipeline.test_phone_song_lane import background_plan, lipsync_plan
from tests.pipeline.test_phone_speech_montage_plan import _compile as compile_speech_montage
from tests.pipeline.test_phone_speech_montage_plan import _montage, _speech
from tests.pipeline.test_talking_music_bed import (
    _bed,
    _digest_without_font_measurements,
    _music,
    _talking,
    _words,
)
from tests.pipeline.user_song_helpers import bindings_for, compiled_plan, song_bed

# --- B1: byte identity -------------------------------------------------------------------------


def test_every_shipped_bed_compiles_byte_identical_to_before():
    talking_library = _talking(
        keep_segments=[(0.0, 3.0), (5.0, 10.0)],
        music=_bed(),
        speech_source_windows={"speaker": _words((1.0, 2.0), (6.0, 7.0))},
    )
    speech_montage, _ = compile_speech_montage(
        [_montage(4.0), _speech(0.0, 5.0, "speaker"), _montage(3.0), _speech(8.0, 12.0)],
        music=PhoneMusicBed(
            source=LibraryMusicSource(
                catalog_id="song-1",
                generation="7",
                fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=500),
            ),
            duration_s=120.0,
            start_s=10.0,
            volume=0.9,
        ),
    )
    guided = {}
    for name, make_plan in (("lipsync", lipsync_plan), ("background", background_plan)):
        result = make_plan()
        footage, visuals = bindings_for(result)
        guided[name] = compile_phone_guided_plan(
            compiled_plan(result), footage, visuals, song=song_bed()
        )
    assert {
        "talking_library": _digest_without_font_measurements(talking_library),
        "speech_montage": _digest_without_font_measurements(speech_montage),
        "guided_lipsync": _digest_without_font_measurements(guided["lipsync"]),
        "guided_background": _digest_without_font_measurements(guided["background"]),
    } == {
        "talking_library": "d1641c69651610ab5e67125d010ddd308b419c0e1cc394b642f7694aa3f5b9ae",
        "speech_montage": "6a1775cd9055a670ff74ea00448e329e2948ee109a8c6e676bc14767262e37f8",
        "guided_lipsync": "ec572445b2626b5d7571921c7a5be5c5fb5475d96b6a3111ed32a903e2298092",
        "guided_background": "690b9ca0efe681e111442f7f9ea9ce59c00ec8a5a4d3c170e19e029cb1d386f7",
    }


# --- the creator's own song under the talk -----------------------------------------------------

ITEM = "item-1"
SOURCE = CreatorSongSource(
    plan_item_id=ITEM,
    generation="7",
    fingerprint=RenderFingerprint(sha256="a" * 64, byte_count=900),
)


def _song(duration_s: float, **choices):
    upload = UploadedSong(
        plan_item_id=ITEM, gcs_path="songs/x.m4a", generation=7, duration_s=duration_s
    )
    return default_talking_song(upload).model_copy(update=choices)


def _bed_for(song, source=SOURCE):
    return music_bed_from_user_song(song, source, speech_duck_db=-12.0)


def _spans(track):
    return [(c.timeline_start, c.source_start, round(c.source_duration, 6)) for c in track.clips]


def test_her_song_rides_one_music_track_never_the_legacy_bed():
    recipe = _talking(
        music=_bed_for(_song(60.0)), speech_source_windows={"speaker": _words((1.0, 2.0))}
    )
    assert recipe.audio.music_asset_id is None
    assert recipe.audio.music_volume == 1.0
    audio_tracks = [t for t in recipe.tracks if t.kind == "audio"]
    assert [t.role for t in audio_tracks] == ["music"]
    [asset] = [
        a for a in recipe.asset_manifest.assets if a.id == _music(recipe).clips[0].source_asset_id
    ]
    assert isinstance(asset, SongRenderAsset)
    assert (asset.plan_item_id, asset.generation) == (ITEM, "7")
    assert "song" not in {t.id for t in recipe.tracks}


def test_a_song_shorter_than_the_video_loops_its_window_back_to_back():
    # A 4 s song under the 10 s talk: the window 0-3.95 (export margin) repeats from the top.
    recipe = _talking(music=_bed_for(_song(4.0)))
    track = _music(recipe)
    assert _spans(track) == [(0.0, 0.0, 3.95), (3.95, 0.0, 3.95), (7.9, 0.0, 2.1)]
    assert recipe.duration == 10.0
    assert (track.clips[0].audio_fade_in, track.clips[-1].audio_fade_out) == (0.5, 0.5)
    assert not any(c.audio_fade_out for c in track.clips[:-1])


def test_a_song_longer_than_the_video_plays_from_her_start_and_loops_her_end():
    trimmed = _music(_talking(music=_bed_for(_song(120.0, window_start_s=30.0))))
    assert _spans(trimmed) == [(0.0, 30.0, 10.0)]
    looped = _music(_talking(music=_bed_for(_song(120.0, window_start_s=30.0, window_end_s=34.0))))
    assert _spans(looped) == [(0.0, 30.0, 4.0), (4.0, 30.0, 4.0), (8.0, 30.0, 2.0)]


def test_a_window_too_short_to_loop_under_the_video_is_refused_never_cut_short():
    with pytest.raises(UnsupportedPhonePlan) as refused:
        _talking(music=_bed_for(_song(120.0, window_start_s=30.0, window_end_s=30.09)))
    assert refused.value.reason == "music_section_too_short"


def test_her_song_sits_at_the_talking_level_ducks_on_her_words_and_keeps_her_voice():
    recipe = _talking(
        music=_bed_for(_song(60.0)), speech_source_windows={"speaker": _words((1.0, 2.0))}
    )
    assert {c.volume for c in _music(recipe).clips} == {round(10 ** (-18 / 20), 6)}
    assert recipe.audio.speech_duck_gain == round(10 ** (-12 / 20), 6)
    assert [(w.start, w.end) for w in recipe.audio.speech_windows] == [(1.0, 2.0)]
    assert {"speechDuck", "musicBed", "audioMix"} <= recipe.required_capabilities
    assert recipe.audio.original_volume == 1.0
    assert all(c.volume == 1.0 for c in recipe.tracks[0].clips)
    louder = _talking(music=_bed_for(_song(60.0, volume=0.4)))
    assert {c.volume for c in _music(louder).clips} == {0.4}


def test_choices_made_for_another_upload_never_play():
    replaced = SOURCE.model_copy(update={"generation": "8"})
    with pytest.raises(ValueError):
        _bed_for(_song(60.0, window_start_s=12.0), source=replaced)
    with pytest.raises(ValueError):
        _bed_for(_song(60.0).model_copy(update={"mode": "lipsync"}))
