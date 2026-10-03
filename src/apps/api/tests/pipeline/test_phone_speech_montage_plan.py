"""KRI-282: spoken-excerpt montage compiler."""

from __future__ import annotations

import pytest

from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import PhoneMusicBed
from app.pipeline.phone_speech_montage_plan import (
    MAIN_TRACK_ID,
    MUSIC_TRACK_ID,
    SPEECH_AUDIO_TRACK_ID,
    PhoneSpeechSection,
    compile_phone_speech_montage_plan,
)
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding


def _binding(
    media_id: str,
    *,
    duration_s: float = 12.0,
    width: int = 1080,
    height: int = 1920,
    orientation_degrees: int = 0,
) -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"user/analysis-proxy-{media_id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=duration_s,
            width=width,
            height=height,
            orientation_degrees=orientation_degrees,
            has_audio=True,
        ),
    )


SPEAKER = _binding("talk", duration_s=40.0)
BROLL = tuple(_binding(f"b{i}", duration_s=8.0, width=1920, height=1080) for i in range(4))


def _speech(start: float, end: float, visual: str = "speaker", binding=SPEAKER, quote="q"):
    return PhoneSpeechSection(
        kind="speech",
        speaker=binding,
        source_start_s=start,
        source_end_s=end,
        visual=visual,
        quote=quote,
    )


def _montage(duration: float, cut: float | None = None) -> PhoneSpeechSection:
    return PhoneSpeechSection(kind="montage", duration_s=duration, cut_s=cut)


def _compile(sections, broll=BROLL, **kw):
    return compile_phone_speech_montage_plan(tuple(sections), broll, **kw)


def _main(recipe):
    return next(t for t in recipe.tracks if t.id == MAIN_TRACK_ID).clips


def _audio(recipe):
    track = next((t for t in recipe.tracks if t.id == SPEECH_AUDIO_TRACK_ID), None)
    return track.clips if track else []


def test_montage_speech_over_broll_return_to_speaker_then_montage(monkeypatch) -> None:
    """The whole requested pattern: fast cuts, speech over b-roll, cut to the speaker, repeat."""
    recipe, receipt = _compile(
        [
            _montage(4.0, 0.8),
            _speech(3.0, 9.0, "cutaways", quote="first line"),
            _speech(12.0, 17.0, "speaker", quote="second line"),
            _montage(3.2, 0.8),
            _speech(21.0, 26.0, "cutaways", quote="third line"),
        ]
    )
    kinds = [row["kind"] + ":" + row.get("visual", "") for row in receipt.sections]
    assert kinds == [
        "montage:",
        "speech:cutaways",
        "speech:speaker",
        "montage:",
        "speech:cutaways",
    ]
    main = _main(recipe)
    # Timeline is the exact sum of the sections, with no gaps or overlaps.
    cursor = 0.0
    for clip in main:
        assert clip.timeline_start == pytest.approx(cursor, abs=1e-3)
        cursor += clip.source_duration
    assert recipe.duration == pytest.approx(4.0 + 6.0 + 5.0 + 3.2 + 5.0, abs=0.02)
    assert receipt.duration_s == pytest.approx(recipe.duration, abs=1e-6)

    # Montage and cutaway pictures are muted; only the speaker-visual clip carries audio.
    audible = [c for c in main if c.volume > 0]
    assert len(audible) == 1 and audible[0].source_asset_id == SPEAKER.media_id
    assert audible[0].source_start == pytest.approx(12.0)
    assert audible[0].audio_fade_in and audible[0].audio_fade_out
    # The speaker is never used as b-roll.
    muted_assets = {c.source_asset_id for c in main if c.volume == 0}
    assert SPEAKER.media_id not in muted_assets and muted_assets <= {b.media_id for b in BROLL}

    # Speech over b-roll plays from an audio track, aligned to its section.
    audio = _audio(recipe)
    assert [a.source_start for a in audio] == [pytest.approx(3.0), pytest.approx(21.0)]
    assert [a.timeline_start for a in audio] == [pytest.approx(4.0), pytest.approx(18.2)]
    assert all(a.source_asset_id == SPEAKER.media_id and a.volume == 1.0 for a in audio)
    assert all(a.audio_fade_in and a.audio_fade_out for a in audio)
    assert recipe.required_capabilities == {"basicComposition", "local1080Export", "audioMix"}

    monkeypatch.setattr(
        "app.services.phone_rollout.settings.phone_render_verified_features",
        list(recipe.required_capabilities),
    )
    validate_phone_pilot_recipe(recipe)


def test_speaker_only_excerpts_need_no_audio_track_or_broll() -> None:
    recipe, receipt = _compile([_speech(1.0, 6.0), _speech(10.0, 14.0)], broll=())
    assert _audio(recipe) == []
    assert recipe.required_capabilities == {"basicComposition", "local1080Export"}
    assert [c.source_start for c in _main(recipe)] == [1.0, 10.0]
    assert receipt.cut_count == 2


def test_back_to_back_excerpts_get_independent_sentence_safe_fades() -> None:
    recipe, _ = _compile([_speech(2.0, 4.0), _speech(4.0, 6.0)], broll=())
    first, second = _main(recipe)
    assert first.audio_fade_out and second.audio_fade_in
    assert first.audio_fade_out <= first.source_duration / 3 + 1e-9
    assert second.audio_fade_in <= second.source_duration / 4 + 1e-9


def test_cutaway_windows_cover_the_speech_exactly_even_with_short_broll() -> None:
    short = tuple(_binding(f"s{i}", duration_s=1.0, width=1920, height=1080) for i in range(3))
    recipe, _ = _compile([_speech(0.0, 6.0, "cutaways")], broll=short)
    covered = sum(c.source_duration for c in _main(recipe))
    assert covered == pytest.approx(6.0, abs=0.12)
    assert all(c.volume == 0 for c in _main(recipe))
    assert recipe.duration == pytest.approx(6.0, abs=0.12)


def test_long_montage_stays_within_the_track_clip_budget() -> None:
    recipe, receipt = _compile([_montage(20.0, 0.4)] * 4 + [_speech(0.0, 5.0)])
    assert len(_main(recipe)) <= 100
    assert receipt.cut_count == len(_main(recipe))


def test_repeated_broll_clip_advances_its_source_window() -> None:
    one = (_binding("only", duration_s=20.0, width=1920, height=1080),)
    recipe, _ = _compile([_montage(3.0, 1.0), _speech(0.0, 3.0)], broll=one)
    starts = [c.source_start for c in _main(recipe) if c.volume == 0]
    assert starts == sorted(starts) and len(set(starts)) == len(starts)


def test_music_plays_only_under_montage_and_resumes_where_it_left_off() -> None:
    music = PhoneMusicBed(
        catalog_id="song-1",
        generation="7",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=500),
        duration_s=120.0,
        start_s=10.0,
        volume=0.9,
    )
    recipe, receipt = _compile(
        [_montage(4.0), _speech(0.0, 5.0, "speaker"), _montage(3.0), _speech(8.0, 12.0)],
        music=music,
    )
    clips = next(t for t in recipe.tracks if t.id == MUSIC_TRACK_ID).clips
    assert receipt.music and len(clips) == 2
    assert [c.timeline_start for c in clips] == [pytest.approx(0.0), pytest.approx(9.0)]
    assert clips[0].source_start == pytest.approx(10.0)
    assert clips[1].source_start == pytest.approx(10.0 + clips[0].source_duration)
    assert all(c.volume <= 0.5 for c in clips)
    assert "musicBed" in recipe.required_capabilities


def test_landscape_speaker_is_refused_with_an_actionable_reason() -> None:
    wide = _binding("wide", width=1920, height=1080)
    with pytest.raises(UnsupportedPhonePlan, match="vertical"):
        _compile([_speech(0.0, 3.0, binding=wide)])
    rotated = _binding("rot", width=1920, height=1080, orientation_degrees=90)
    recipe, _ = _compile([_speech(0.0, 3.0, binding=rotated)], broll=())
    assert recipe.duration == pytest.approx(3.0)


def test_broll_needed_but_absent_names_what_to_add() -> None:
    with pytest.raises(UnsupportedPhonePlan, match="no other video clip"):
        _compile([_speech(0.0, 3.0, "cutaways")], broll=(SPEAKER,))
    with pytest.raises(UnsupportedPhonePlan, match="no other video clip"):
        _compile([_montage(3.0), _speech(0.0, 3.0)], broll=())


def test_plan_without_speech_or_sections_is_refused() -> None:
    with pytest.raises(UnsupportedPhonePlan):
        _compile([])
    with pytest.raises(UnsupportedPhonePlan, match="no speech"):
        _compile([_montage(3.0)])


def test_excerpt_running_past_the_clip_end_is_trimmed_or_refused() -> None:
    recipe, receipt = _compile([_speech(38.0, 40.0)], broll=())
    assert recipe.duration < 2.0 and receipt.adjustments
    with pytest.raises(UnsupportedPhonePlan, match="past the end"):
        _compile([_speech(39.9, 41.0)], broll=())


def test_section_validation_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError):
        PhoneSpeechSection(kind="speech", source_start_s=0, source_end_s=3)
    with pytest.raises(ValueError):
        _speech(1.0, 1.3)
    with pytest.raises(ValueError):
        PhoneSpeechSection(kind="montage", duration_s=0)
