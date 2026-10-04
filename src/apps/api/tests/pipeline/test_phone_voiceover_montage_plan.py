import pytest

from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.generative_decision import (
    GenerativeAssemblyStepDecision,
    GenerativeVariantDecision,
)
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_recipe_shared import PhoneMusicBed, PhoneNarrationBed
from app.pipeline.phone_voiceover_montage_plan import (
    compile_phone_voiceover_montage_plan as _compile,
)
from app.services.phone_sources import PhoneSourceBinding


def _narration(plan_item_id: str = "item-1", *, duration_s: float = 20.0) -> PhoneNarrationBed:
    return PhoneNarrationBed(
        plan_item_id=plan_item_id,
        generation="9",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=999),
        duration_s=duration_s,
    )


_DEFAULT = object()


def compile_phone_voiceover_montage_plan(decision, bindings, *, music=None, narration=_DEFAULT):
    """The writer REQUIRES a narration bed (KRI-220); default to a valid one here."""
    return _compile(
        decision,
        bindings,
        music=music,
        narration=_narration() if narration is _DEFAULT else narration,
    )


def _binding(media_id: str, *, duration_s: float = 10.0) -> PhoneSourceBinding:
    return PhoneSourceBinding(
        media_id=media_id,
        proxy_path=f"user/analysis-proxy-{media_id}.mp4",
        generation="1",
        original=OriginalMediaDescriptor(
            sha256="a" * 64,
            byte_count=1000,
            duration_s=duration_s,
            width=1080,
            height=1920,
            has_audio=True,
        ),
    )


def _step(
    clip_id: str,
    *,
    in_s: float = 0.0,
    duration: float = 4.0,
    transition_in: str | None = None,
    transition_duration_s: float | None = None,
    rate: float | None = None,
) -> GenerativeAssemblyStepDecision:
    return GenerativeAssemblyStepDecision(
        clip_id=clip_id,
        in_s=in_s,
        out_s=in_s + duration,
        target_duration_s=duration,
        rate=rate,
        transition_in=transition_in,
        transition_duration_s=transition_duration_s,
    )


def fixture(
    *,
    steps=None,
    text_mode="agent_text",
    music_track_id=None,
    music_start_s=None,
    extras_overrides=None,
    bindings=None,
    mix=None,
    voiceover_gcs_path="voiceover-uploads/direct/u/i/voice.m4a",
    voiceover_target_s=None,
):
    if steps is None:
        steps = [
            _step("c0"),
            _step("c1", transition_in="crossfade", transition_duration_s=0.3),
            _step("c2", transition_in="crossfade", transition_duration_s=0.3),
        ]
    if bindings is None:
        bindings = tuple(_binding(f"c{i}") for i in range(3))
    clip_id_to_media_id = {f"c{i}": f"c{i}" for i in range(3)}
    extras = {
        "base": {},
        "variant_t0": 0.0,
        "recipe": {"color_grade": "none"},
        "beats": [],
        "canvas": {"width": 1080, "height": 1920},
        "masonry_requested": False,
        "lyrics_rendered": False,
        "assembly_landscape_fit": "fill",
        "clip_id_to_media_id": clip_id_to_media_id,
        "intro_overlay_params": (
            {
                "text": "hello world",
                "effect": "fade-in",
                "layout": "linear",
                "text_color": "#FFFFFF",
                "highlight_color": "#FFD24A",
            }
            if text_mode == "agent_text"
            else None
        ),
    }
    if voiceover_gcs_path:
        extras["voiceover_gcs_path"] = voiceover_gcs_path
        extras["voiceover_target_s"] = voiceover_target_s
    if extras_overrides:
        extras.update(extras_overrides)
    decision = GenerativeVariantDecision(
        variant_id="original_text",
        rank=1,
        text_mode=text_mode,
        orientation="portrait",
        duration_s=sum(s.target_duration_s or 0 for s in steps),
        assembly_steps=steps,
        music_track_id=music_track_id,
        music_start_s=music_start_s,
        mix=mix,
        extras=extras,
    )
    return decision, bindings


def test_compiles_three_clips_with_crossfades_intro_and_voiceover_music_bed():
    music = PhoneMusicBed(
        catalog_id="track1",
        generation="7",
        fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=500),
        duration_s=120.0,
        start_s=30.5,
        volume=1.0,
    )
    decision, bindings = fixture(music_track_id="track1", music_start_s=30.5, mix=0.7)
    recipe = compile_phone_voiceover_montage_plan(decision, bindings, music=music)

    video_track = recipe.tracks[0]
    assert [clip.id for clip in video_track.clips] == [
        "step-0-c0",
        "step-1-c1",
        "step-2-c2",
    ]
    assert video_track.clips[0].transition is None
    assert video_track.clips[1].transition.kind == "crossfade"
    assert video_track.clips[1].timeline_start == pytest.approx(3.7)
    assert video_track.clips[2].timeline_start == pytest.approx(7.4)
    assert recipe.duration == pytest.approx(11.4)

    music_track = next(t for t in recipe.tracks if t.id == "music")
    assert music_track.clips[0].source_asset_id == "music-track1"
    assert music_track.clips[0].source_start == pytest.approx(30.5)
    # voiceover_music: the bed sits under the voice (1 - mix), footage never mixed.
    assert recipe.audio.music_volume == pytest.approx(0.3)
    assert recipe.audio.original_volume == pytest.approx(0.0)
    assert any(t.id == "narration" for t in recipe.tracks)

    assert len(recipe.text_layers) == 2  # reveal + hold
    assert {"crossfade", "musicBed", "audioMix", "positionedText", "animatedText"} <= (
        recipe.required_capabilities
    )
    # Digest stability: serializing twice must produce identical bytes.
    assert recipe.model_dump_json() == recipe.model_dump_json()
    recipe.model_validate(recipe.model_dump(mode="json"))  # validate() round trip


def test_refit_clamps_a_window_that_overruns_the_original_duration():
    decision, bindings = fixture(
        steps=[_step("c0", in_s=7.0, duration=4.0)],
        bindings=(_binding("c0", duration_s=8.0),),
    )
    recipe = compile_phone_voiceover_montage_plan(decision, bindings, music=None)
    clip = recipe.tracks[0].clips[0]
    # available = 8.0 - 0.05 safety margin = 7.95; the 4s duration fits by
    # shifting the start back rather than truncating.
    assert clip.source_start == pytest.approx(3.95)
    assert clip.source_duration == pytest.approx(4.0)


def test_rejects_unsupported_transition():
    decision, bindings = fixture(
        steps=[_step("c0"), _step("c1", transition_in="whip-pan")],
    )
    with pytest.raises(UnsupportedPhonePlan, match="unsupported transition"):
        compile_phone_voiceover_montage_plan(decision, bindings, music=None)


@pytest.mark.parametrize(
    "overrides,match",
    [
        ({"masonry_requested": True}, "masonry"),
        ({"lyrics_rendered": True}, "lyric"),
        ({"duck_original_during_music": True}, "ducking"),
    ],
)
def test_rejects_lanes_the_recipe_cannot_express(overrides, match):
    decision, bindings = fixture(extras_overrides=overrides)
    with pytest.raises(UnsupportedPhonePlan, match=match):
        compile_phone_voiceover_montage_plan(decision, bindings, music=None)


def test_rejects_carousel_moment():
    decision, bindings = fixture(extras_overrides={"base": {"carousel_moment": {"clip_id": "c0"}}})
    with pytest.raises(UnsupportedPhonePlan, match="carousel"):
        compile_phone_voiceover_montage_plan(decision, bindings, music=None)


def test_rejects_lyrics_text_mode():
    decision, bindings = fixture(
        text_mode="lyrics", extras_overrides={"intro_overlay_params": None}
    )
    with pytest.raises(UnsupportedPhonePlan, match="lyric"):
        compile_phone_voiceover_montage_plan(decision, bindings, music=None)


def test_rejects_when_a_step_has_no_phone_source_binding():
    decision, bindings = fixture(extras_overrides={"clip_id_to_media_id": {"c0": "c0"}})
    with pytest.raises(UnsupportedPhonePlan, match="no phone source binding"):
        compile_phone_voiceover_montage_plan(decision, bindings, music=None)


def test_rejects_when_music_bed_metadata_is_missing():
    decision, _bindings = fixture(music_track_id="track1", music_start_s=0.0)
    with pytest.raises(UnsupportedPhonePlan, match="music bed"):
        compile_phone_voiceover_montage_plan(decision, _bindings, music=None)


def test_rejects_unexpressible_intro_style():
    decision, bindings = fixture(extras_overrides={"intro_overlay_params": {"effect": "fade-in"}})
    with pytest.raises(UnsupportedPhonePlan, match="unable to compile intro text"):
        compile_phone_voiceover_montage_plan(decision, bindings, music=None)


def test_rejects_unsupported_color_grade():
    decision, bindings = fixture(extras_overrides={"recipe": {"color_grade": "moody"}})
    with pytest.raises(UnsupportedPhonePlan, match="color grade"):
        compile_phone_voiceover_montage_plan(decision, bindings, music=None)


def test_golden_hour_requires_exact_canvas_unrotated_source():
    decision, _bindings = fixture(extras_overrides={"recipe": {"color_grade": "golden_hour"}})
    mismatched = (
        PhoneSourceBinding(
            media_id="c0",
            proxy_path="user/analysis-proxy-c0.mp4",
            generation="1",
            original=OriginalMediaDescriptor(
                sha256="a" * 64,
                byte_count=1000,
                duration_s=10.0,
                width=1920,  # landscape original, not the 1080x1920 portrait canvas
                height=1080,
                has_audio=True,
            ),
        ),
        *(_binding(f"c{i}") for i in (1, 2)),
    )
    with pytest.raises(ValueError, match="exact-canvas"):
        compile_phone_voiceover_montage_plan(decision, mismatched, music=None)
    exact_bindings = tuple(_binding(f"c{i}", duration_s=10.0) for i in range(3))
    recipe = compile_phone_voiceover_montage_plan(decision, exact_bindings, music=None)
    assert recipe.tracks[0].clips[0].look == "golden_hour"
    assert "goldenHourLook" in recipe.required_capabilities


def test_variable_speed_capability_and_rejects_non_positive_rate():
    decision, bindings = fixture(steps=[_step("c0", rate=1.5)])
    recipe = compile_phone_voiceover_montage_plan(decision, bindings, music=None)
    assert "variableSpeed" in recipe.required_capabilities

    decision, bindings = fixture(steps=[_step("c0", rate=0)])
    with pytest.raises(UnsupportedPhonePlan, match="positive"):
        compile_phone_voiceover_montage_plan(decision, bindings, music=None)


def test_no_text_variant_has_no_layers():
    decision, bindings = fixture(text_mode="none", extras_overrides={"intro_overlay_params": None})
    recipe = compile_phone_voiceover_montage_plan(decision, bindings, music=None)
    assert recipe.text_layers == []
    assert "positionedText" not in recipe.required_capabilities


def test_voiceover_mix_1_0_fully_ducks_footage_audio():
    """Default voiceover_only mix (1.0): footage bed silent, voice full."""
    decision, bindings = fixture(
        voiceover_gcs_path="voiceover-uploads/direct/u/i/voice.m4a",
        voiceover_target_s=11.4,
        mix=1.0,
    )
    narration = _narration()
    recipe = compile_phone_voiceover_montage_plan(
        decision, bindings, music=None, narration=narration
    )

    narration_track = next(t for t in recipe.tracks if t.id == "narration")
    assert narration_track.kind == "audio"
    assert narration_track.clips[0].source_asset_id == f"voiceover-{narration.plan_item_id}"
    assert narration_track.clips[0].volume == pytest.approx(1.0)
    assert narration_track.clips[0].source_start == pytest.approx(0.0)
    assert narration_track.clips[0].source_duration == pytest.approx(11.4)
    assert recipe.audio.narration_asset_id == f"voiceover-{narration.plan_item_id}"
    assert recipe.audio.original_volume == pytest.approx(0.0)
    assert not any(t.id == "music" for t in recipe.tracks)
    assert {"narrationAudio", "audioMix"} <= recipe.required_capabilities
    voiceover_asset = next(
        a for a in recipe.asset_manifest.assets if a.id == f"voiceover-{narration.plan_item_id}"
    )
    assert voiceover_asset.kind == "voiceover"
    assert voiceover_asset.plan_item_id == narration.plan_item_id
    assert voiceover_asset.generation == narration.generation
    # Digest stability: serializing twice must produce identical bytes.
    assert recipe.model_dump_json() == recipe.model_dump_json()
    recipe.model_validate(recipe.model_dump(mode="json"))


def test_voiceover_mix_0_4_mixes_footage_audio_under_the_voice():
    """A lower mix brings the clips' own audio up under the voice."""
    decision, bindings = fixture(
        voiceover_gcs_path="voiceover-uploads/direct/u/i/voice.m4a",
        voiceover_target_s=11.4,
        mix=0.4,
    )
    narration = _narration()
    recipe = compile_phone_voiceover_montage_plan(
        decision, bindings, music=None, narration=narration
    )

    assert recipe.audio.original_volume == pytest.approx(0.6)
    narration_track = next(t for t in recipe.tracks if t.id == "narration")
    assert narration_track.clips[0].volume == pytest.approx(1.0)


def test_voiceover_music_bed_caps_gain_and_never_mixes_footage():
    """voiceover_music: matched track plays as a low bed, footage never mixed."""
    music = PhoneMusicBed(
        catalog_id="track1",
        generation="7",
        fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=500),
        duration_s=120.0,
        start_s=10.0,
        volume=1.0,
    )
    narration = _narration()
    for mix, expected_gain in [(0.7, 0.3), (0.0, 0.5)]:
        decision, bindings = fixture(
            voiceover_gcs_path="voiceover-uploads/direct/u/i/voice.m4a",
            voiceover_target_s=11.4,
            mix=mix,
            music_track_id="track1",
            music_start_s=10.0,
        )
        recipe = compile_phone_voiceover_montage_plan(
            decision, bindings, music=music, narration=narration
        )
        music_track = next(t for t in recipe.tracks if t.id == "music")
        assert music_track.clips[0].volume == pytest.approx(expected_gain)
        assert recipe.audio.music_volume == pytest.approx(expected_gain)
        # Footage audio is never referenced at all in the music-bed branch.
        assert recipe.audio.original_volume == pytest.approx(0.0)
        assert {"narrationAudio", "musicBed", "audioMix"} <= recipe.required_capabilities


def test_voiceover_requires_a_phone_narration_binding():
    decision, bindings = fixture(
        voiceover_gcs_path="voiceover-uploads/direct/u/i/voice.m4a",
        voiceover_target_s=11.4,
    )
    with pytest.raises(UnsupportedPhonePlan, match="narration binding") as exc:
        compile_phone_voiceover_montage_plan(decision, bindings, music=None, narration=None)
    assert exc.value.capability == "narrationAudio"


def test_no_recorded_voiceover_is_never_this_writers_job():
    """KRI-220: every non-voiceover montage goes to the unified planner."""
    decision, bindings = fixture(voiceover_gcs_path=None)
    with pytest.raises(UnsupportedPhonePlan, match="narration binding") as exc:
        _compile(decision, bindings, music=None, narration=None)
    assert exc.value.capability == "narrationAudio"
    # Even with a music track and no voice, there is no music-only branch any more.
    decision, bindings = fixture(voiceover_gcs_path=None, music_track_id="t", music_start_s=0.0)
    with pytest.raises(UnsupportedPhonePlan, match="narration binding"):
        _compile(decision, bindings, music=None, narration=None)


def test_voice_fades_out_and_music_bed_fades_both_ends():
    """KRI-139: fades ride the audio clips; the loudness target rides the mix."""
    music = PhoneMusicBed(
        catalog_id="track1",
        generation="7",
        fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=500),
        duration_s=120.0,
        start_s=10.0,
        volume=1.0,
    )
    decision, bindings = fixture(
        voiceover_target_s=11.4, mix=0.7, music_track_id="track1", music_start_s=10.0
    )
    recipe = _compile(decision, bindings, music=music, narration=_narration(), target_lufs=-14.0)
    bed = next(t for t in recipe.tracks if t.id == "music").clips[0]
    voice = next(t for t in recipe.tracks if t.id == "narration").clips[0]
    assert (bed.audio_fade_in, bed.audio_fade_out) == (0.5, 0.5)
    assert voice.audio_fade_out == pytest.approx(0.5)
    assert voice.audio_fade_in is None
    assert recipe.audio.target_lufs == pytest.approx(-14.0)
    # The bed plays through its audio-track clip, never the legacy
    # `music_asset_id` whole-file path (that would play it twice).
    assert recipe.audio.music_asset_id is None
    assert recipe.model_validate(recipe.model_dump(mode="json")) == recipe


def test_short_voice_fade_never_covers_more_than_half_the_clip():
    decision, bindings = fixture(voiceover_target_s=0.6, steps=[_step("c0", duration=0.6)])
    recipe = compile_phone_voiceover_montage_plan(decision, bindings)
    voice = next(t for t in recipe.tracks if t.id == "narration").clips[0]
    assert voice.audio_fade_out == pytest.approx(0.3)
