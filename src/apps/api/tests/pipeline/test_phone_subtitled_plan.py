import pytest

from app.config import settings
from app.kria.media_sources import OriginalMediaDescriptor
from app.kria.recipes import MediaAsset
from app.kria.recipes_v2 import EditRecipeV2
from app.kria.render_assets import LibraryRenderAsset, RenderFingerprint
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan
from app.pipeline.phone_subtitled_lanes import (
    PhoneSubtitledLanes,
    ResolvedSoundEffect,
    SubtitledEndingClip,
    SubtitledLaneError,
    SubtitledOverlayCard,
    SubtitledSoundEffect,
    sfx_path_is_playable,
)
from app.pipeline.phone_subtitled_plan import (
    SFX_SPEECH_DUCK_GAIN,
    compile_phone_subtitled_plan,
    sfx_duck_receipt,
    speech_windows_from_cues,
)
from app.services.phone_rollout import validate_phone_pilot_recipe
from app.services.phone_sources import PhoneSourceBinding, PhoneVisualBinding


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


PHOTO_ID = "photo-1"
PHOTO_PATH = "users/owner/plan/item/pool/photo-1.jpg"
VIDEO_ID = "video-1"
VIDEO_PATH = "users/owner/plan/item/pool/video-1.mov"


def _photo_visual(**changes) -> PhoneVisualBinding:
    return PhoneVisualBinding(
        **{
            "media_id": PHOTO_ID,
            "gcs_path": PHOTO_PATH,
            "generation": "77",
            "sha256": "b" * 64,
            "byte_count": 2048,
        }
        | changes
    )


def _pool_video_visual(**changes) -> PhoneVisualBinding:
    return PhoneVisualBinding(
        **{
            "media_id": VIDEO_ID,
            "gcs_path": VIDEO_PATH,
            "generation": "88",
            "sha256": "c" * 64,
            "byte_count": 4096,
            "kind": "video",
            "duration_s": 6.0,
            "width": 1920,
            "height": 1080,
            "orientation_degrees": 90,
        }
        | changes
    )


def _overlay_card(**changes) -> SubtitledOverlayCard:
    return SubtitledOverlayCard(
        **{
            "id": "card-1",
            "media_id": PHOTO_ID,
            "gcs_path": PHOTO_PATH,
            "generation": "77",
            "start_s": 1.0,
            "end_s": 3.0,
        }
        | changes
    )


def _sfx_asset(catalog_id: str = "pop", *, generation: str = "1") -> LibraryRenderAsset:
    return LibraryRenderAsset(
        id=f"sfx-asset-{catalog_id}",
        catalog="sound_effect",
        catalog_id=catalog_id,
        generation=generation,
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=512),
    )


def _resolved_sfx(**changes) -> ResolvedSoundEffect:
    defaults = {
        "request": SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=1.0),
        "asset": _sfx_asset(),
        "duration_s": 1.0,
    }
    defaults.update(changes)
    return ResolvedSoundEffect(**defaults)


# --- Pre-existing behaviour (unchanged) --------------------------------------


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


# --- KRI-174 Phase 1: byte-identity with no lanes ----------------------------


def test_no_lanes_is_byte_identical_across_default_forms():
    bindings = (_binding(duration_s=10.0),)
    baseline = compile_phone_subtitled_plan(bindings, caption_cues=_CUES)
    explicit_empty_visuals = compile_phone_subtitled_plan(
        bindings, caption_cues=_CUES, visuals=(), lanes=None
    )
    empty_lanes = compile_phone_subtitled_plan(
        bindings, caption_cues=_CUES, visuals=(), lanes=PhoneSubtitledLanes()
    )
    assert baseline.model_dump_json() == explicit_empty_visuals.model_dump_json()
    assert baseline.model_dump_json() == empty_lanes.model_dump_json()
    assert len(baseline.tracks) == 1


# --- Overlays lane ------------------------------------------------------------


def test_overlay_card_compiles_onto_a_silent_overlay_track():
    bindings = (_binding(duration_s=10.0),)
    visual = _photo_visual()
    card = _overlay_card()
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )
    overlay_track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    assert overlay_track.kind == "overlay"
    assert len(overlay_track.clips) == 1
    clip = overlay_track.clips[0]
    assert clip.id == "subtitled-overlay-card-1"
    assert clip.volume == 0
    assert clip.source_start == pytest.approx(0.0)
    assert clip.source_duration == pytest.approx(2.0)
    assert clip.timeline_start == pytest.approx(1.0)
    placement = clip.visual_placement
    assert placement is not None
    assert placement.order == 1
    assert placement.width_fraction == pytest.approx(0.35)
    assert placement.x_fraction == pytest.approx(0.5)
    assert placement.y_fraction == pytest.approx(0.4)
    assert placement.window_start == pytest.approx(1.0)
    assert placement.window_end == pytest.approx(3.0)
    assert placement.fade_in is False and placement.fade_out is False
    asset = visual.render_asset()
    assert asset in recipe.asset_manifest.assets
    projected = next(a for a in recipe.assets if a.id == asset.id)
    assert isinstance(projected, MediaAsset)
    assert projected.duration is None and projected.natural_size is None
    assert {"visualBlocks", "alphaOverlay", "audioMix"} <= recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_overlay_fade_flag_sets_both_fade_in_and_fade_out():
    bindings = (_binding(duration_s=10.0),)
    visual = _photo_visual()
    card = _overlay_card(fade=True)
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )
    overlay_track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    placement = overlay_track.clips[0].visual_placement
    assert placement.fade_in is True and placement.fade_out is True


def test_overlay_y_frac_clamped_into_caption_band_top():
    bindings = (_binding(duration_s=10.0),)
    visual = _photo_visual()
    card = _overlay_card(y_frac=0.9)
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )
    overlay_track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    assert overlay_track.clips[0].visual_placement.y_fraction == pytest.approx(0.62)


def test_overlays_sorted_by_z_then_start_then_id():
    bindings = (_binding(duration_s=10.0),)
    visual = _photo_visual()
    cards = [
        _overlay_card(id="c-last", z=1, start_s=0.5, end_s=1.5),
        _overlay_card(id="a-first", z=0, start_s=2.0, end_s=3.0),
        _overlay_card(id="b-second", z=0, start_s=0.0, end_s=1.0),
    ]
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=cards),
    )
    overlay_track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    assert [clip.id for clip in overlay_track.clips] == [
        "subtitled-overlay-b-second",
        "subtitled-overlay-a-first",
        "subtitled-overlay-c-last",
    ]
    assert [clip.visual_placement.order for clip in overlay_track.clips] == [1, 2, 3]


def test_overlay_card_outside_timeline_is_dropped_silently():
    bindings = (_binding(duration_s=10.0),)
    visual = _photo_visual()
    inside = _overlay_card(id="inside", start_s=1.0, end_s=2.0)
    outside = _overlay_card(id="outside", start_s=20.0, end_s=25.0)
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[inside, outside]),
    )
    overlay_track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    assert [clip.id for clip in overlay_track.clips] == ["subtitled-overlay-inside"]


def test_overlay_video_visual_is_rejected_as_a_lane_error():
    bindings = (_binding(duration_s=10.0),)
    video_visual = _pool_video_visual()
    card = _overlay_card(media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88")
    with pytest.raises(SubtitledLaneError) as excinfo:
        compile_phone_subtitled_plan(
            bindings,
            caption_cues=[],
            visuals=(video_visual,),
            lanes=PhoneSubtitledLanes(overlays=[card]),
        )
    assert excinfo.value.lane == "overlays"
    assert excinfo.value.capability == "visualBlocks"


def test_overlay_unknown_media_id_is_rejected_as_a_lane_error():
    bindings = (_binding(duration_s=10.0),)
    card = _overlay_card()
    with pytest.raises(SubtitledLaneError) as excinfo:
        compile_phone_subtitled_plan(
            bindings,
            caption_cues=[],
            visuals=(),
            lanes=PhoneSubtitledLanes(overlays=[card]),
        )
    assert excinfo.value.lane == "overlays"


def test_no_overlay_track_when_all_cards_drop_outside_timeline():
    bindings = (_binding(duration_s=10.0),)
    visual = _photo_visual()
    outside = _overlay_card(start_s=20.0, end_s=25.0)
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[outside]),
    )
    assert not any(t.id == "subtitled-overlays" for t in recipe.tracks)
    assert "visualBlocks" not in recipe.required_capabilities


# --- Sound-effects lane -------------------------------------------------------


def test_sfx_playable_extension_helper():
    assert sfx_path_is_playable("sound-effects/pop.M4A") is True
    assert sfx_path_is_playable("sound-effects/pop.wav") is True
    assert sfx_path_is_playable("sound-effects/pop.mp3") is True
    assert sfx_path_is_playable("sound-effects/pop.aac") is True
    assert sfx_path_is_playable("sound-effects/pop.ogg") is False


def test_sfx_compiles_onto_a_shared_audio_track_with_volume_forwarded():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx()
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        lanes=PhoneSubtitledLanes(sound_effects=[resolved]),
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    assert sfx_track.kind == "audio"
    assert len(sfx_track.clips) == 1
    clip = sfx_track.clips[0]
    assert clip.id == "sfx-sfx-1"
    assert clip.timeline_start == pytest.approx(1.0)
    assert clip.source_duration == pytest.approx(1.0)
    assert clip.volume == pytest.approx(1.0)
    assert {"soundEffects", "audioMix"} <= recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_sfx_volume_is_forwarded_from_the_request():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=1.0, volume=0.4)
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[resolved])
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    assert sfx_track.clips[0].volume == pytest.approx(0.4)


def test_sfx_is_clamped_to_the_timeline_end():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=9.5), duration_s=3.0
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[resolved])
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    assert sfx_track.clips[0].source_duration == pytest.approx(0.5)


def test_sfx_starting_after_timeline_end_is_skipped():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx(request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=10.0))
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[resolved])
    )
    assert not any(t.id == "sfx" for t in recipe.tracks)
    assert "soundEffects" not in recipe.required_capabilities


# --- Sound-effect trim (KRI-182 step 1) --------------------------------------


def test_sfx_with_no_trim_is_byte_identical_to_before_trim_existed():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=1.0), duration_s=2.0
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[resolved])
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    clip = sfx_track.clips[0]
    assert clip.source_start == pytest.approx(0.0)
    assert clip.source_duration == pytest.approx(2.0)


def test_sfx_trim_start_shifts_the_source_start_and_shortens_duration():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=1.0, trim_start_s=0.5),
        duration_s=2.0,
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[resolved])
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    clip = sfx_track.clips[0]
    assert clip.source_start == pytest.approx(0.5)
    assert clip.source_duration == pytest.approx(1.5)


def test_sfx_trim_end_shortens_the_playable_duration():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx(
        request=SubtitledSoundEffect(
            id="sfx-1", catalog_id="pop", at_s=1.0, trim_start_s=0.2, trim_end_s=0.8
        ),
        duration_s=2.0,
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[resolved])
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    clip = sfx_track.clips[0]
    assert clip.source_start == pytest.approx(0.2)
    assert clip.source_duration == pytest.approx(0.6)


def test_sfx_trim_end_past_resolved_duration_is_ignored():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx(
        request=SubtitledSoundEffect(
            id="sfx-1", catalog_id="pop", at_s=1.0, trim_start_s=0.0, trim_end_s=50.0
        ),
        duration_s=2.0,
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[resolved])
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    clip = sfx_track.clips[0]
    assert clip.source_duration == pytest.approx(2.0)


def test_sfx_trim_still_clamps_to_the_timeline_end():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=9.5, trim_start_s=0.0),
        duration_s=3.0,
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[resolved])
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    assert sfx_track.clips[0].source_duration == pytest.approx(0.5)


def test_sfx_trim_start_at_or_past_duration_raises_named_lane_error():
    bindings = (_binding(duration_s=10.0),)
    resolved = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=1.0, trim_start_s=2.0),
        duration_s=2.0,
    )
    with pytest.raises(SubtitledLaneError) as error:
        compile_phone_subtitled_plan(
            bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[resolved])
        )
    assert error.value.lane == "sound_effects"


def test_sfx_trim_window_validator_rejects_end_before_start():
    with pytest.raises(Exception):
        SubtitledSoundEffect(
            id="sfx-1", catalog_id="pop", at_s=1.0, trim_start_s=1.0, trim_end_s=0.5
        )


def test_shared_catalog_id_reuses_one_manifest_entry():
    bindings = (_binding(duration_s=10.0),)
    asset = _sfx_asset("pop")
    first = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=1.0), asset=asset
    )
    second = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-2", catalog_id="pop", at_s=3.0), asset=asset
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[first, second])
    )
    sfx_assets = [a for a in recipe.asset_manifest.assets if isinstance(a, LibraryRenderAsset)]
    assert len(sfx_assets) == 1
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    assert {clip.source_asset_id for clip in sfx_track.clips} == {asset.id}


def test_sfx_ordered_by_at_s_then_id():
    bindings = (_binding(duration_s=10.0),)
    later = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-later", catalog_id="pop", at_s=5.0),
        asset=_sfx_asset("pop"),
    )
    earlier = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-earlier", catalog_id="ding", at_s=1.0),
        asset=_sfx_asset("ding"),
    )
    recipe = compile_phone_subtitled_plan(
        bindings, caption_cues=[], lanes=PhoneSubtitledLanes(sound_effects=[later, earlier])
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    assert [clip.id for clip in sfx_track.clips] == ["sfx-sfx-earlier", "sfx-sfx-later"]


# --- Ending-clip lane ---------------------------------------------------------


def test_ending_clip_appends_a_second_main_track_clip_muted():
    bindings = (_binding(duration_s=10.0),)
    visual = _pool_video_visual(duration_s=6.0)
    ending = SubtitledEndingClip(media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88")
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(ending_clip=ending),
    )
    main_track = next(t for t in recipe.tracks if t.id == "subtitled")
    assert len(main_track.clips) == 2
    ending_clip = main_track.clips[1]
    assert ending_clip.id == "clip-ending"
    assert ending_clip.volume == 0
    assert ending_clip.timeline_start == pytest.approx(10.0)
    assert ending_clip.source_start == pytest.approx(0.0)
    assert ending_clip.source_duration == pytest.approx(6.0)
    assert recipe.duration == pytest.approx(16.0)
    assert {"visualVideos", "audioMix"} <= recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_ending_clip_honours_trim_start_and_max_duration():
    bindings = (_binding(duration_s=10.0),)
    visual = _pool_video_visual(duration_s=6.0)
    ending = SubtitledEndingClip(
        media_id=VIDEO_ID,
        gcs_path=VIDEO_PATH,
        generation="88",
        trim_start_s=1.0,
        max_duration_s=2.0,
    )
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(ending_clip=ending),
    )
    main_track = next(t for t in recipe.tracks if t.id == "subtitled")
    ending_clip = main_track.clips[1]
    assert ending_clip.source_start == pytest.approx(1.0)
    assert ending_clip.source_duration == pytest.approx(2.0)
    assert recipe.duration == pytest.approx(12.0)


def test_ending_clip_requires_a_video_visual():
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    ending = SubtitledEndingClip(media_id=PHOTO_ID, gcs_path=PHOTO_PATH, generation="77")
    with pytest.raises(SubtitledLaneError) as excinfo:
        compile_phone_subtitled_plan(
            bindings,
            caption_cues=[],
            visuals=(photo,),
            lanes=PhoneSubtitledLanes(ending_clip=ending),
        )
    assert excinfo.value.lane == "ending_clip"
    assert excinfo.value.capability == "visualVideos"


def test_ending_clip_trim_past_source_duration_is_rejected():
    bindings = (_binding(duration_s=10.0),)
    visual = _pool_video_visual(duration_s=6.0)
    ending = SubtitledEndingClip(
        media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88", trim_start_s=10.0
    )
    with pytest.raises(SubtitledLaneError) as excinfo:
        compile_phone_subtitled_plan(
            bindings,
            caption_cues=[],
            visuals=(visual,),
            lanes=PhoneSubtitledLanes(ending_clip=ending),
        )
    assert excinfo.value.lane == "ending_clip"


def test_sfx_may_extend_over_the_ending_clip():
    bindings = (_binding(duration_s=10.0),)
    visual = _pool_video_visual(duration_s=6.0)
    ending = SubtitledEndingClip(media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88")
    resolved = _resolved_sfx(
        request=SubtitledSoundEffect(id="sfx-1", catalog_id="pop", at_s=14.0), duration_s=3.0
    )
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(ending_clip=ending, sound_effects=[resolved]),
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    assert len(sfx_track.clips) == 1
    # timeline_end = 10 (speaker) + 6 (ending) = 16, so a 3s effect at 14s fits.
    assert sfx_track.clips[0].source_duration == pytest.approx(2.0)


# --- Full recipe: all three lanes together -----------------------------------


def test_all_three_lanes_together_pass_phone_pilot_validation(monkeypatch):
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    video = _pool_video_visual(duration_s=6.0)
    card = _overlay_card()
    ending = SubtitledEndingClip(media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88")
    resolved = _resolved_sfx()
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=_CUES,
        visuals=(photo, video),
        lanes=PhoneSubtitledLanes(overlays=[card], sound_effects=[resolved], ending_clip=ending),
    )
    monkeypatch.setattr(settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe)
    assert recipe.model_validate(recipe.model_dump(mode="json")) == recipe


# --- SFX-under-speech duck (KRI-181 follow-up) -------------------------------

# "Hello everyone" [0.0, 1.2], a 1.3 s pause, "welcome back" [2.5, 3.4].
_WORD_CUES = [
    {
        "text": "Hello everyone",
        "start_s": 0.0,
        "end_s": 1.2,
        "words": [
            {"text": "Hello", "start_s": 0.0, "end_s": 0.5},
            {"text": "everyone", "start_s": 0.6, "end_s": 1.2},
        ],
    },
    {
        "text": "welcome back",
        "start_s": 2.5,
        "end_s": 3.4,
        "words": [
            {"text": "welcome", "start_s": 2.5, "end_s": 3.0},
            {"text": "back", "start_s": 3.1, "end_s": 3.4},
        ],
    },
]


def _sfx_volumes(effects, *, duck: bool, cues=_WORD_CUES) -> dict[str, float]:
    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),),
        caption_cues=cues,
        lanes=PhoneSubtitledLanes(sound_effects=effects),
        duck_sfx_under_speech=duck,
    )
    sfx_track = next(t for t in recipe.tracks if t.id == "sfx")
    return {clip.id: clip.volume for clip in sfx_track.clips}


def _effect(effect_id: str, at_s: float, *, volume: float = 1.0, duration_s: float = 0.5):
    return _resolved_sfx(
        request=SubtitledSoundEffect(id=effect_id, catalog_id="pop", at_s=at_s, volume=volume),
        duration_s=duration_s,
    )


def test_speech_windows_merge_short_word_gaps_and_keep_pauses():
    assert speech_windows_from_cues(_WORD_CUES) == ((0.0, 1.2), (2.5, 3.4))


def test_speech_windows_fall_back_to_cue_span_and_skip_malformed_entries():
    cues = [
        {"text": "no words", "start_s": 4.0, "end_s": 5.0},
        {"text": "bad", "start_s": "x", "end_s": 6.0},
        {"text": "inverted", "start_s": 7.0, "end_s": 6.5},
        {"text": "bool", "start_s": True, "end_s": 8.0},
        "not-a-dict",
        {"text": "bad word", "words": [{"start_s": None, "end_s": 1.0}, "junk"]},
    ]
    assert speech_windows_from_cues(cues) == ((4.0, 5.0),)
    assert speech_windows_from_cues([]) == ()


def test_duck_lowers_an_effect_on_speech_and_keeps_one_in_a_pause():
    volumes = _sfx_volumes(
        [_effect("on-word", 0.3), _effect("in-pause", 1.5), _effect("between-words", 2.95)],
        duck=True,
    )
    assert volumes["sfx-on-word"] == pytest.approx(SFX_SPEECH_DUCK_GAIN)
    assert volumes["sfx-in-pause"] == pytest.approx(1.0)
    # A 0.1 s gap between two words is still speech.
    assert volumes["sfx-between-words"] == pytest.approx(SFX_SPEECH_DUCK_GAIN)


def test_duck_scales_the_requested_volume_instead_of_replacing_it():
    volumes = _sfx_volumes(
        [_effect("loud", 0.3, volume=2.0), _effect("soft", 3.0, volume=0.4)], duck=True
    )
    assert volumes["sfx-loud"] == pytest.approx(2.0 * SFX_SPEECH_DUCK_GAIN)
    assert volumes["sfx-soft"] == pytest.approx(0.4 * SFX_SPEECH_DUCK_GAIN)


def test_duck_ignores_a_tail_that_only_brushes_the_next_word():
    # [1.7, 2.53] overlaps "welcome" (2.5) by 0.03 s: below the trigger.
    volumes = _sfx_volumes([_effect("tail", 1.7, duration_s=0.83)], duck=True)
    assert volumes["sfx-tail"] == pytest.approx(1.0)


def test_duck_without_captions_leaves_every_effect_at_full_volume():
    volumes = _sfx_volumes([_effect("a", 0.3)], duck=True, cues=[])
    assert volumes["sfx-a"] == pytest.approx(1.0)


def test_duck_off_is_byte_identical_to_the_default_call():
    effects = [_effect("on-word", 0.3), _effect("in-pause", 1.5)]
    lanes = PhoneSubtitledLanes(sound_effects=effects)
    bindings = (_binding(duration_s=10.0),)
    default = compile_phone_subtitled_plan(bindings, caption_cues=_WORD_CUES, lanes=lanes)
    explicit_off = compile_phone_subtitled_plan(
        bindings, caption_cues=_WORD_CUES, lanes=lanes, duck_sfx_under_speech=False
    )
    assert default.model_dump_json() == explicit_off.model_dump_json()
    assert {clip.volume for t in default.tracks if t.id == "sfx" for clip in t.clips} == {1.0}


def test_duck_changes_only_sfx_clip_volumes():
    """The duck needs no new recipe field or capability, so every installed
    app build renders it and the phone route decision is unchanged."""
    effects = [_effect("on-word", 0.3), _effect("in-pause", 1.5)]
    lanes = PhoneSubtitledLanes(sound_effects=effects)
    bindings = (_binding(duration_s=10.0),)
    off = compile_phone_subtitled_plan(bindings, caption_cues=_WORD_CUES, lanes=lanes)
    on = compile_phone_subtitled_plan(
        bindings, caption_cues=_WORD_CUES, lanes=lanes, duck_sfx_under_speech=True
    )
    assert on.required_capabilities == off.required_capabilities
    assert on.audio == off.audio
    assert [t.id for t in on.tracks] == [t.id for t in off.tracks]
    speaker_on = next(t for t in on.tracks if t.id == "subtitled")
    speaker_off = next(t for t in off.tracks if t.id == "subtitled")
    assert speaker_on == speaker_off
    off_sfx = next(t for t in off.tracks if t.id == "sfx")
    on_sfx = next(t for t in on.tracks if t.id == "sfx")
    assert [c.model_copy(update={"volume": 1.0}) for c in on_sfx.clips] == list(off_sfx.clips)


def test_ducked_recipe_passes_phone_pilot_validation(monkeypatch):
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [
            "basicComposition",
            "local1080Export",
            "positionedText",
            "animatedText",
            "soundEffects",
            "audioMix",
        ],
    )
    recipe = compile_phone_subtitled_plan(
        (_binding(duration_s=10.0),),
        caption_cues=_WORD_CUES,
        lanes=PhoneSubtitledLanes(sound_effects=[_effect("on-word", 0.3)]),
        duck_sfx_under_speech=True,
    )
    validate_phone_pilot_recipe(recipe)
    EditRecipeV2.model_validate_json(recipe.model_dump_json())


def test_ios_level_test_pins_the_server_duck_gain():
    """`SfxSpeechDuckLevelTests.swift` exports a speech-plus-effect recipe
    through the native exporter at this exact gain and asserts the effect no
    longer drowns the speech band. Keep the two constants in lockstep."""
    import re
    from pathlib import Path

    swift = (
        Path(__file__).resolve().parents[3]
        / "ios/Packages/KriaMediaEngine/Tests/KriaMediaEngineTests/SfxSpeechDuckLevelTests.swift"
    )
    match = re.search(r"static let serverDuckGain = ([0-9.]+)", swift.read_text())
    assert match is not None
    assert float(match.group(1)) == SFX_SPEECH_DUCK_GAIN


def test_duck_receipt_records_only_the_effects_the_duck_lowered():
    lanes = PhoneSubtitledLanes(
        sound_effects=[_effect("on-word", 0.3, volume=0.8), _effect("in-pause", 1.5)]
    )
    bindings = (_binding(duration_s=10.0),)
    on = compile_phone_subtitled_plan(
        bindings, caption_cues=_WORD_CUES, lanes=lanes, duck_sfx_under_speech=True
    )
    off = compile_phone_subtitled_plan(bindings, caption_cues=_WORD_CUES, lanes=lanes)
    assert sfx_duck_receipt(lanes, on) == {
        "version": 1,
        "gain": SFX_SPEECH_DUCK_GAIN,
        "volumes": {"on-word": 0.8},
    }
    assert sfx_duck_receipt(lanes, off) is None
    assert sfx_duck_receipt(None, on) is None


# --- Video overlay cards (KRI-183) --------------------------------------------


def _video_overlay_card(**changes) -> SubtitledOverlayCard:
    return _overlay_card(
        **{
            "media_id": VIDEO_ID,
            "gcs_path": VIDEO_PATH,
            "generation": "88",
            "kind": "video",
        }
        | changes
    )


def test_video_overlay_card_compiles_muted_onto_the_overlay_track():
    bindings = (_binding(duration_s=10.0),)
    visual = _pool_video_visual(duration_s=6.0)
    card = _video_overlay_card(start_s=1.0, end_s=3.0, source_start_s=0.5)
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )
    overlay_track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    assert len(overlay_track.clips) == 1
    clip = overlay_track.clips[0]
    assert clip.id == "subtitled-overlay-card-1"
    assert clip.volume == 0
    assert clip.source_start == pytest.approx(0.5)
    # requested window (2.0s) fits inside the 5.5s available footage.
    assert clip.source_duration == pytest.approx(2.0)
    assert clip.timeline_start == pytest.approx(1.0)
    placement = clip.visual_placement
    assert placement is not None
    assert placement.window_start == pytest.approx(1.0)
    assert placement.window_end == pytest.approx(3.0)
    assert "visualVideos" in recipe.required_capabilities
    assert {"visualBlocks", "alphaOverlay", "audioMix"} <= recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_video_overlay_card_shortens_window_when_footage_runs_out():
    bindings = (_binding(duration_s=10.0),)
    # Only 1.5s of footage remains after the 4.5s source_start.
    visual = _pool_video_visual(duration_s=6.0)
    card = _video_overlay_card(start_s=1.0, end_s=4.0, source_start_s=4.5)
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )
    overlay_track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    clip = overlay_track.clips[0]
    # requested window is 3.0s but only 1.5s of footage is available.
    assert clip.source_duration == pytest.approx(1.5)
    assert clip.timeline_start == pytest.approx(1.0)
    placement = clip.visual_placement
    # the on-screen window is shortened to match the footage, not frozen.
    assert placement.window_start == pytest.approx(1.0)
    assert placement.window_end == pytest.approx(2.5)


def test_video_overlay_source_start_past_footage_is_a_lane_error():
    bindings = (_binding(duration_s=10.0),)
    visual = _pool_video_visual(duration_s=6.0)
    card = _video_overlay_card(start_s=1.0, end_s=3.0, source_start_s=6.0)
    with pytest.raises(SubtitledLaneError) as excinfo:
        compile_phone_subtitled_plan(
            bindings,
            caption_cues=[],
            visuals=(visual,),
            lanes=PhoneSubtitledLanes(overlays=[card]),
        )
    assert excinfo.value.lane == "overlays"
    assert excinfo.value.capability == "visualVideos"


def test_video_kind_card_bound_to_an_image_visual_is_rejected():
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    card = _overlay_card(media_id=PHOTO_ID, gcs_path=PHOTO_PATH, generation="77", kind="video")
    with pytest.raises(SubtitledLaneError) as excinfo:
        compile_phone_subtitled_plan(
            bindings,
            caption_cues=[],
            visuals=(photo,),
            lanes=PhoneSubtitledLanes(overlays=[card]),
        )
    assert excinfo.value.lane == "overlays"
    assert excinfo.value.capability == "visualVideos"
    assert "video overlay card requires a video visual" in str(excinfo.value)


def test_image_card_bound_to_a_video_visual_still_rejected_as_before():
    # Byte-identical to the pre-KRI-183 rejection: an image (default `kind`)
    # card still cannot bind a video visual.
    bindings = (_binding(duration_s=10.0),)
    video_visual = _pool_video_visual()
    card = _overlay_card(media_id=VIDEO_ID, gcs_path=VIDEO_PATH, generation="88")
    with pytest.raises(SubtitledLaneError) as excinfo:
        compile_phone_subtitled_plan(
            bindings,
            caption_cues=[],
            visuals=(video_visual,),
            lanes=PhoneSubtitledLanes(overlays=[card]),
        )
    assert excinfo.value.lane == "overlays"
    assert excinfo.value.capability == "visualBlocks"
    assert "overlay card requires an image visual" in str(excinfo.value)


def test_mixed_image_and_video_cards_both_compile_in_one_lane():
    bindings = (_binding(duration_s=10.0),)
    photo = _photo_visual()
    video = _pool_video_visual(duration_s=6.0)
    photo_card = _overlay_card(id="photo-card", start_s=0.5, end_s=1.5)
    video_card = _video_overlay_card(id="video-card", start_s=2.0, end_s=4.0)
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(photo, video),
        lanes=PhoneSubtitledLanes(overlays=[photo_card, video_card]),
    )
    overlay_track = next(t for t in recipe.tracks if t.id == "subtitled-overlays")
    assert {clip.id for clip in overlay_track.clips} == {
        "subtitled-overlay-photo-card",
        "subtitled-overlay-video-card",
    }
    assert "visualVideos" in recipe.required_capabilities


def test_image_only_lane_has_no_visual_videos_capability():
    bindings = (_binding(duration_s=10.0),)
    visual = _photo_visual()
    card = _overlay_card()
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )
    assert "visualVideos" not in recipe.required_capabilities


def test_default_kind_card_changes_nothing_byte_identical_to_image_kind():
    bindings = (_binding(duration_s=10.0),)
    visual = _photo_visual()
    default_card = _overlay_card()
    explicit_image_card = _overlay_card(kind="image")
    default_recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[default_card]),
    )
    explicit_recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[explicit_image_card]),
    )
    assert default_recipe.model_dump_json() == explicit_recipe.model_dump_json()


def test_image_card_dump_drops_kind_and_source_start_fields():
    # A photo card's persisted shape must stay byte-identical to before
    # KRI-183 (mirrors `PhoneVisualBinding._photo_shape`).
    card = _overlay_card()
    dumped = card.model_dump(mode="json")
    assert "kind" not in dumped
    assert "source_start_s" not in dumped


def test_video_card_dump_carries_kind_and_source_start_fields():
    card = _video_overlay_card(source_start_s=1.5)
    dumped = card.model_dump(mode="json")
    assert dumped["kind"] == "video"
    assert dumped["source_start_s"] == pytest.approx(1.5)


def test_video_overlay_recipe_passes_phone_pilot_validation_when_verified(monkeypatch):
    bindings = (_binding(duration_s=10.0),)
    visual = _pool_video_visual(duration_s=6.0)
    card = _video_overlay_card()
    recipe = compile_phone_subtitled_plan(
        bindings,
        caption_cues=[],
        visuals=(visual,),
        lanes=PhoneSubtitledLanes(overlays=[card]),
    )
    monkeypatch.setattr(settings, "phone_editor_media_enabled", True)
    monkeypatch.setattr(
        settings, "phone_render_verified_features", list(recipe.required_capabilities)
    )
    validate_phone_pilot_recipe(recipe, allow_editor_media=True)
