"""KRI-1562: `_run_phone_subtitled_job` plays the creator's chosen song under the talk.

Real boundaries: the worker runs end to end on the subtitled harness (ingest, transcription
and device pin faked), the real compiler builds the recipe. Only the two I/O edges are stood
in: the persisted treatment read (`_persisted_music_treatment`) and the catalog pin.

Failure modes: F2 a bed without a persisted treatment (KRI-20); F11 windows from the
transcript, not the captions; F14 a render that forgets the speech windows or the creator's
choice on the variant; F1 flag off changes the render; F5 a section too short for the video
must be refused in words, never shortened or exceeded.
"""

from __future__ import annotations

from app.kria.render_assets import LibraryRenderAsset, RenderFingerprint
from app.services.client_protocol import SPEECH_DUCK_PROTOCOL
from app.services.device_render import device_status
from app.tasks import generative_build as gb
from tests.tasks.test_phone_subtitled_narrated_dispatch import _setup_subtitled, _words

TREATMENT = {
    "track_id": "song-1",
    "src_gcs_path": "music/song-1/audio.m4a",
    "section_start_s": 10.0,
    "section_end_s": 30.0,
    "gain_db": -18.0,
    "speech_duck_db": -12.0,
    "final_lufs": -14.0,
}
WORDS = (("Hello", 0.5, 1.0), ("there.", 1.1, 1.6), ("Bye.", 4.0, 4.6))


def _pin(track_id, _path=None):
    return (
        LibraryRenderAsset(
            id=f"music-{track_id}",
            catalog="music",
            catalog_id=track_id,
            generation="5",
            fingerprint=RenderFingerprint(sha256="e" * 64, byte_count=4000),
        ),
        120.0,
    )


def _run(monkeypatch, *, flag: bool, treatment: dict | None):
    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=_words(*WORDS)
    )
    monkeypatch.setattr(gb.settings, "talking_music_enabled", flag)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "musicBed", "speechDuck"],
    )
    monkeypatch.setattr(gb, "_persisted_music_treatment", lambda **_: treatment)
    monkeypatch.setattr(gb, "_pin_catalog_music", _pin)
    gb._run_generative_job(str(job.id))
    assert job.status == "awaiting_device"
    return device_status(job, "subtitled").request.recipe, job.assembly_plan["variants"][0]


def test_a_persisted_song_plays_under_the_talk_ducked_on_the_spoken_words(monkeypatch):
    recipe, variant = _run(monkeypatch, flag=True, treatment=dict(TREATMENT))
    music = next(track for track in recipe.tracks if track.role == "music")
    assert music.clips[0].source_asset_id == "music-song-1"
    assert [(w.start, w.end) for w in recipe.audio.speech_windows] == [(0.5, 1.6), (4.0, 4.6)]
    assert "speechDuck" in recipe.required_capabilities
    assert variant["speech_source_windows"] == {"c0": [[0.5, 1.6], [4.0, 4.6]]}
    assert variant["smart_music_treatment"] == TREATMENT


def test_no_persisted_song_plays_nothing_but_keeps_the_windows_for_later(monkeypatch):
    recipe, variant = _run(monkeypatch, flag=True, treatment=None)
    assert not any(track.role == "music" for track in recipe.tracks)
    assert recipe.audio.speech_windows == []
    assert "smart_music_treatment" not in variant
    assert variant["speech_source_windows"] == {"c0": [[0.5, 1.6], [4.0, 4.6]]}


def test_flag_off_renders_and_stores_exactly_what_it_did_before(monkeypatch):
    baseline_recipe, baseline_variant = _run(monkeypatch, flag=False, treatment=None)
    recipe, variant = _run(monkeypatch, flag=False, treatment=dict(TREATMENT))
    assert recipe.model_dump_json() == baseline_recipe.model_dump_json()
    for row in (baseline_variant, variant):
        assert "speech_source_windows" not in row and "smart_music_treatment" not in row
    assert {k: v for k, v in variant.items() if k != "render_generation_id"} == {
        k: v for k, v in baseline_variant.items() if k != "render_generation_id"
    }


def test_a_section_too_short_for_the_video_is_refused_never_cut_short(monkeypatch):
    from unittest.mock import Mock

    fail = Mock(return_value=True)
    monkeypatch.setattr(gb, "_fail_job", fail)
    monkeypatch.setattr(gb, "mark_failed_phase", Mock())
    job, _snapshot, _session, _binding = _setup_subtitled(
        monkeypatch, transcript_words=_words(*WORDS)
    )
    monkeypatch.setattr(gb.settings, "talking_music_enabled", True)
    monkeypatch.setattr(
        gb.settings,
        "phone_render_verified_features",
        [*gb.settings.phone_render_verified_features, "musicBed", "speechDuck"],
    )
    short = {**TREATMENT, "section_start_s": 10.0, "section_end_s": 10.05}
    monkeypatch.setattr(gb, "_persisted_music_treatment", lambda **_: short)
    monkeypatch.setattr(gb, "_pin_catalog_music", _pin)
    gb._run_generative_job(str(job.id))
    assert job.status != "awaiting_device"
    assert "too short to play under this whole video" in str(fail.call_args)


# --- F12: a Voiceover job's matched bed ducks only for an approving build at protocol 53 ---


def _voiceover_recipe(monkeypatch, *, protocol: int | None):
    from tests.tasks.test_phone_montage_dispatch import _voiceover_with_track_setup

    monkeypatch.setattr(gb.settings, "creator_jobs_never_auto_match_catalog_song_enabled", False)
    monkeypatch.setattr(gb.settings, "talking_music_enabled", True)
    job, snapshot, candidates, _cloud = _voiceover_with_track_setup(
        monkeypatch,
        verified_features=[
            "basicComposition",
            "local1080Export",
            "crossfade",
            "audioMix",
            "narrationAudio",
            "musicBed",
            "speechDuck",
        ],
    )
    candidates["creator_client_protocol"] = protocol
    gb._run_phone_voiceover_montage_job(str(job.id), snapshot, candidates, ownership_epoch=3)
    variant = job.assembly_plan["variants"][0]
    return device_status(job, variant["variant_id"]).request.recipe


def test_a_voiceover_bed_ducks_for_a_build_at_protocol_53(monkeypatch):
    recipe = _voiceover_recipe(monkeypatch, protocol=SPEECH_DUCK_PROTOCOL)
    bed = next(track for track in recipe.tracks if track.id == "music")
    assert bed.role == "music"
    assert recipe.audio.speech_windows and "speechDuck" in recipe.required_capabilities


def test_an_older_build_keeps_todays_one_level_voiceover_bed(monkeypatch):
    for protocol in (SPEECH_DUCK_PROTOCOL - 1, None):
        recipe = _voiceover_recipe(monkeypatch, protocol=protocol)
        bed = next(track for track in recipe.tracks if track.id == "music")
        assert bed.role is None
        assert recipe.audio.speech_windows == []
        assert "speechDuck" not in recipe.required_capabilities
