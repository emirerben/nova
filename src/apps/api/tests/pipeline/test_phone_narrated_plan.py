import pytest

pytest.importorskip("app.pipeline.phone_captions")

from app.config import settings
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.render_assets import RenderFingerprint
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_narrated_plan import NarratedPhoneStep, compile_phone_narrated_plan
from app.pipeline.phone_recipe_shared import PhoneNarrationBed
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding


def _narration(plan_item_id: str = "item-1", *, duration_s: float = 12.0) -> PhoneNarrationBed:
    return PhoneNarrationBed(
        plan_item_id=plan_item_id,
        generation="9",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=999),
        duration_s=duration_s,
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
    step_id: str,
    media_id: str,
    *,
    start_s: float,
    end_s: float,
    source_start_s: float = 0.0,
) -> NarratedPhoneStep:
    return NarratedPhoneStep(
        step_id=step_id,
        media_id=media_id,
        source_start_s=source_start_s,
        start_s=start_s,
        end_s=end_s,
    )


def _three_steps() -> list[NarratedPhoneStep]:
    return [
        _step("s0", "c0", start_s=0.0, end_s=4.0),
        _step("s1", "c1", start_s=4.0, end_s=8.0),
        _step("s2", "c2", start_s=8.0, end_s=12.0),
    ]


def _three_bindings(**kwargs) -> tuple[PhoneSourceBinding, ...]:
    return tuple(_binding(f"c{i}", **kwargs) for i in range(3))


def test_compiles_three_clips_tiled_contiguously():
    steps = _three_steps()
    bindings = _three_bindings()
    narration = _narration(duration_s=12.0)
    recipe = compile_phone_narrated_plan(steps, bindings, narration, voiceover_duration_s=12.0)

    video_track = next(t for t in recipe.tracks if t.kind == "video")
    assert [clip.id for clip in video_track.clips] == [
        "step-0-s0",
        "step-1-s1",
        "step-2-s2",
    ]
    assert [clip.timeline_start for clip in video_track.clips] == [0.0, 4.0, 8.0]
    assert all(clip.rate == 1.0 for clip in video_track.clips)
    assert recipe.duration == pytest.approx(12.0)
    # Digest stability: serializing twice must produce identical bytes.
    assert recipe.model_dump_json() == recipe.model_dump_json()
    recipe.model_validate(recipe.model_dump(mode="json"))


def test_narration_track_and_asset_present():
    steps = _three_steps()
    bindings = _three_bindings()
    narration = _narration(plan_item_id="item-42", duration_s=12.0)
    recipe = compile_phone_narrated_plan(steps, bindings, narration, voiceover_duration_s=12.0)

    narration_track = next(t for t in recipe.tracks if t.id == "narration")
    assert narration_track.kind == "audio"
    assert narration_track.clips[0].source_asset_id == "voiceover-item-42"
    assert narration_track.clips[0].volume == pytest.approx(1.0)
    assert narration_track.clips[0].source_duration == pytest.approx(12.0)
    assert recipe.audio.narration_asset_id == "voiceover-item-42"
    voiceover_asset = next(a for a in recipe.asset_manifest.assets if a.id == "voiceover-item-42")
    assert voiceover_asset.kind == "voiceover"
    assert voiceover_asset.plan_item_id == "item-42"
    assert {"narrationAudio", "audioMix", "basicComposition", "local1080Export"} <= (
        recipe.required_capabilities
    )


@pytest.mark.parametrize("mix,expected_gain", [(1.0, 0.0), (0.4, 0.6), (0.0, 1.0), (None, 0.0)])
def test_footage_gain_matches_montage_mix_math(mix, expected_gain):
    steps = _three_steps()
    bindings = _three_bindings()
    narration = _narration(duration_s=12.0)
    recipe = compile_phone_narrated_plan(
        steps, bindings, narration, voiceover_duration_s=12.0, mix=mix
    )
    assert recipe.audio.original_volume == pytest.approx(expected_gain)


def test_recipe_passes_phone_pilot_validation(monkeypatch):
    steps = _three_steps()
    bindings = _three_bindings()
    narration = _narration(duration_s=12.0)
    recipe = compile_phone_narrated_plan(steps, bindings, narration, voiceover_duration_s=12.0)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_short_clip_retimes_instead_of_freezing():
    # Clip only has 2s of footage but its step demands 4s -- must slow down
    # (never freeze-hold), exactly like `_fit_clip_segment`'s cloud math.
    steps = [_step("s0", "c0", start_s=0.0, end_s=4.0)]
    bindings = (_binding("c0", duration_s=2.0),)
    narration = _narration(duration_s=4.0)
    recipe = compile_phone_narrated_plan(steps, bindings, narration, voiceover_duration_s=4.0)
    clip = recipe.tracks[0].clips[0]
    # available = 2.0 - 0.05 EOF guard = 1.95; rate = 1.95 / 4.0
    assert clip.source_duration == pytest.approx(1.95)
    assert clip.rate == pytest.approx(1.95 / 4.0)
    assert clip.source_duration / clip.rate == pytest.approx(4.0)
    assert "variableSpeed" in recipe.required_capabilities


def test_unusable_clip_fails_closed():
    # duration_s itself is below the usable floor, so even the "restart at
    # source 0" fallback (for a source_start_s past EOF) still leaves nothing
    # usable -- must fail closed rather than emit a zero-length clip.
    steps = [_step("s0", "c0", start_s=0.0, end_s=4.0, source_start_s=10.0)]
    bindings = (_binding("c0", duration_s=0.02),)
    narration = _narration(duration_s=4.0)
    with pytest.raises(UnsupportedPhonePlan, match="no usable footage"):
        compile_phone_narrated_plan(steps, bindings, narration, voiceover_duration_s=4.0)


def test_rejects_step_with_no_phone_source_binding():
    steps = _three_steps()
    bindings = (_binding("c0"), _binding("c1"))  # missing c2
    narration = _narration(duration_s=12.0)
    with pytest.raises(UnsupportedPhonePlan, match="no phone source binding"):
        compile_phone_narrated_plan(steps, bindings, narration, voiceover_duration_s=12.0)


def test_rejects_non_contiguous_steps():
    steps = [
        _step("s0", "c0", start_s=0.0, end_s=4.0),
        _step("s1", "c1", start_s=5.0, end_s=9.0),  # gap: 4.0 -> 5.0
    ]
    bindings = (_binding("c0"), _binding("c1"))
    narration = _narration(duration_s=9.0)
    with pytest.raises(UnsupportedPhonePlan, match="contiguously"):
        compile_phone_narrated_plan(steps, bindings, narration, voiceover_duration_s=9.0)


def test_rejects_steps_that_do_not_cover_the_full_voiceover():
    steps = _three_steps()  # tiles [0, 12]
    bindings = _three_bindings()
    narration = _narration(duration_s=20.0)
    with pytest.raises(UnsupportedPhonePlan, match="full voiceover"):
        compile_phone_narrated_plan(steps, bindings, narration, voiceover_duration_s=20.0)


def test_rejects_empty_step_list():
    narration = _narration(duration_s=1.0)
    with pytest.raises(ValueError, match="no steps"):
        compile_phone_narrated_plan([], (), narration, voiceover_duration_s=1.0)


def test_duplicate_binding_media_ids_rejected():
    steps = _three_steps()
    bindings = (_binding("c0"), _binding("c0"))
    narration = _narration(duration_s=12.0)
    with pytest.raises(ValueError, match="unique media identities"):
        compile_phone_narrated_plan(steps, bindings, narration, voiceover_duration_s=12.0)


def test_no_caption_cues_has_no_text_layers():
    steps = _three_steps()
    bindings = _three_bindings()
    narration = _narration(duration_s=12.0)
    recipe = compile_phone_narrated_plan(
        steps, bindings, narration, voiceover_duration_s=12.0, caption_cues=None
    )
    assert recipe.text_layers == []
    assert "positionedText" not in recipe.required_capabilities


def test_caption_cues_compile_into_text_layers_with_registered_fonts():
    steps = _three_steps()
    bindings = _three_bindings()
    narration = _narration(duration_s=12.0)
    cues = [
        {"text": "Welcome to the tour", "start_s": 0.0, "end_s": 2.0},
        {"text": "Let's get started", "start_s": 2.0, "end_s": 4.0},
    ]
    recipe = compile_phone_narrated_plan(
        steps,
        bindings,
        narration,
        voiceover_duration_s=12.0,
        caption_cues=cues,
        caption_style="sentence",
    )
    assert len(recipe.text_layers) == 2
    assert all(layer.effect == "pop-in" for layer in recipe.text_layers)
    assert {"positionedText", "animatedText"} <= recipe.required_capabilities
    font_assets = [a for a in recipe.asset_manifest.assets if a.kind == "library"]
    assert font_assets, "caption font must be registered in the asset manifest"
    assert all(a.catalog == "font" for a in font_assets)
    referenced_font_ids = {run.font_asset_id for layer in recipe.text_layers for run in layer.runs}
    assert referenced_font_ids <= {a.id for a in recipe.asset_manifest.assets}
    # Digest stability + round trip, same as the no-caption case.
    assert recipe.model_dump_json() == recipe.model_dump_json()
    recipe.model_validate(recipe.model_dump(mode="json"))


def test_caption_recipe_passes_phone_pilot_validation(monkeypatch):
    steps = _three_steps()
    bindings = _three_bindings()
    narration = _narration(duration_s=12.0)
    cues = [{"text": "Hello there", "start_s": 0.0, "end_s": 2.0}]
    recipe = compile_phone_narrated_plan(
        steps, bindings, narration, voiceover_duration_s=12.0, caption_cues=cues
    )
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)
