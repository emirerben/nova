import pytest

from app.config import settings
from app.kria.media_sources import OriginalMediaDescriptor
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_subtitled_plan import compile_phone_subtitled_plan
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding


def _binding(
    media_id: str = "clip-0",
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


_CUES = [
    {"text": "Hello everyone", "start_s": 0.0, "end_s": 1.5},
    {"text": "welcome back", "start_s": 1.5, "end_s": 3.0},
]


def test_compiles_single_clip_with_captions_and_original_audio():
    bindings = (_binding(duration_s=10.0),)
    recipe = compile_phone_subtitled_plan(bindings, caption_cues=_CUES)

    assert recipe.canvas.width == 1080
    assert recipe.canvas.height == 1920
    video_track = next(t for t in recipe.tracks if t.kind == "video")
    assert len(video_track.clips) == 1
    clip = video_track.clips[0]
    assert clip.source_start == pytest.approx(0.0)
    assert clip.source_duration == pytest.approx(10.0)
    assert clip.rate == pytest.approx(1.0)
    assert recipe.duration == pytest.approx(10.0)

    assert recipe.audio.original_volume == pytest.approx(1.0)
    assert recipe.audio.music_asset_id is None
    assert recipe.audio.narration_asset_id is None
    assert not any(t.id in {"music", "narration"} for t in recipe.tracks)

    assert len(recipe.text_layers) == 2
    assert {"basicComposition", "local1080Export", "positionedText"} <= (
        recipe.required_capabilities
    )
    # Digest stability: serializing twice must produce identical bytes.
    assert recipe.model_dump_json() == recipe.model_dump_json()
    recipe.model_validate(recipe.model_dump(mode="json"))


def test_passes_phone_pilot_validation(monkeypatch):
    bindings = (_binding(duration_s=10.0),)
    recipe = compile_phone_subtitled_plan(bindings, caption_cues=_CUES)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_word_style_passes_phone_pilot_validation(monkeypatch):
    cues = [
        {
            "text": "Hello everyone",
            "start_s": 0.0,
            "end_s": 1.5,
            "words": [
                {"text": "Hello", "start_s": 0.0, "end_s": 0.6},
                {"text": "everyone", "start_s": 0.6, "end_s": 1.5},
            ],
        }
    ]
    bindings = (_binding(duration_s=10.0),)
    recipe = compile_phone_subtitled_plan(bindings, caption_cues=cues, caption_style="word")

    layer = recipe.text_layers[0]
    assert layer.effect == "karaoke-line"
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)


def test_empty_caption_cues_still_renders_the_clip():
    bindings = (_binding(duration_s=10.0),)
    recipe = compile_phone_subtitled_plan(bindings, caption_cues=[])

    assert recipe.text_layers == []
    assert "positionedText" not in recipe.required_capabilities
    assert len(recipe.tracks[0].clips) == 1


def test_rejects_zero_clips():
    with pytest.raises(UnsupportedPhonePlan, match="exactly one clip"):
        compile_phone_subtitled_plan((), caption_cues=_CUES)


def test_rejects_more_than_one_clip():
    bindings = (_binding("clip-0"), _binding("clip-1"))
    with pytest.raises(UnsupportedPhonePlan, match="exactly one clip"):
        compile_phone_subtitled_plan(bindings, caption_cues=_CUES)


def test_rejects_landscape_source_clip():
    bindings = (_binding(width=1920, height=1080),)
    with pytest.raises(UnsupportedPhonePlan, match="portrait") as exc:
        compile_phone_subtitled_plan(bindings, caption_cues=_CUES)
    assert exc.value.capability == "semanticCamera"


def test_rejects_square_source_clip():
    bindings = (_binding(width=1080, height=1080),)
    with pytest.raises(UnsupportedPhonePlan, match="portrait"):
        compile_phone_subtitled_plan(bindings, caption_cues=_CUES)


def test_accepts_rotated_landscape_pixels_that_display_portrait():
    # 1920x1080 pixels flagged 90 degrees display as 1080x1920 (portrait).
    bindings = (_binding(width=1920, height=1080, orientation_degrees=90),)
    recipe = compile_phone_subtitled_plan(bindings, caption_cues=_CUES)
    assert recipe.tracks[0].clips[0].source_duration == pytest.approx(12.0)


def test_rejects_clip_over_five_minutes():
    bindings = (_binding(duration_s=301.0),)
    with pytest.raises(UnsupportedPhonePlan, match="5 minutes"):
        compile_phone_subtitled_plan(bindings, caption_cues=_CUES)


def test_unexpected_caption_compilation_failure_fails_closed(monkeypatch):
    """A caption-compiler crash (bad transcript/edit content, a font that
    can't resolve a glyph, ...) must fail closed as `UnsupportedPhonePlan`,
    never propagate a raw exception -- mirrors `compile_phone_montage_plan`'s
    identical wrapping of `build_persistent_intro_overlays`/
    `compile_text_overlay` failures."""
    import app.pipeline.phone_subtitled_plan as module

    def _boom(*args, **kwargs):
        raise ValueError("font cannot resolve every legacy text glyph")

    monkeypatch.setattr(module, "compile_caption_layers", _boom)
    bindings = (_binding(duration_s=10.0),)
    with pytest.raises(UnsupportedPhonePlan, match="unable to compile captions"):
        compile_phone_subtitled_plan(bindings, caption_cues=_CUES)
