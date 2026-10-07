"""KRI-479: `verify_phone_recipe` with composition commitments.

Every test builds the recipe with the REAL composer, then breaks ONE thing the way a
real render path could. The verifier must refuse each break with a typed decline that
names the field, and must not change its verdict for a recipe verified without
commitments (the legacy lanes).
"""

from __future__ import annotations

import pytest

from app.kria.recipes import AssetFingerprint, MediaAsset, TimelineClip, TimelineTrack
from app.kria.render_assets import LibraryRenderAsset, RenderAssetManifest, RenderFingerprint
from app.pipeline.phone_speech_montage_plan import (
    VOICE_AUDIO_TRACK_ID,
    VOICE_FOOTAGE_TRACK_ID,
    VOICE_TAIL_SLACK_S,
    VoiceWindow,
    compile_phone_voice_behind_footage_plan,
)
from app.services.creator_render_contract import (
    CompositionCommitments,
    CreatorRenderContract,
    CreatorRenderContractError,
    TextRequirement,
    verify_phone_recipe,
)
from tests.services.test_phone_speech_montage_job import _binding

FRAME = 1 / 30
IDS = ("c0", "c1", "c2", "c3", "c4", "c5")
HIDDEN = CompositionCommitments(voice_picture="hidden")


def _recipe(duration_s: float = 30.0, *, title: bool = True, **kwargs):
    picture = tuple(_binding(m, duration_s=20.0) for m in IDS)
    recipe, _ = compile_phone_voice_behind_footage_plan(
        _binding("talk", duration_s=90.0),
        kwargs.pop("window", VoiceWindow(start_s=0.34, end_s=duration_s - 0.1)),
        picture,
        duration_s=duration_s,
        opening_title="Summer in Lisbon" if title else None,
        opening_title_hold_s=3.0 if title else None,
        **kwargs,
    )
    return recipe


def _contract(**changes) -> CreatorRenderContract:
    base = {
        "duration_s": 30.0,
        "audio_source_ids": ("talk",),
        "original_audio": "require",
        "order_required": True,
        "order_ids": IDS,
        "order_basis": "capture_time",
        "exact_texts": (TextRequirement(role="opening", text="Summer in Lisbon", duration_s=3.0),),
    }
    return CreatorRenderContract(generation_id="g").rebind(**{**base, **changes})


AUDIO = {"talk": True, **{m: True for m in IDS}}


def _verify(recipe, contract=None, composition=HIDDEN):
    return verify_phone_recipe(
        contract or _contract(), recipe, source_audio=AUDIO, composition=composition
    )


def _track(recipe, track_id):
    return next(t for t in recipe.tracks if t.id == track_id)


def _replace_clips(recipe, track_id, clips):
    tracks = [
        t.model_copy(update={"clips": clips}) if t.id == track_id else t for t in recipe.tracks
    ]
    return recipe.model_copy(update={"tracks": tracks})


def _expect(exc_info, *, reason, field_path):
    exc = exc_info.value
    assert isinstance(exc, CreatorRenderContractError)
    assert exc.decline_reason == reason
    assert exc.field_path == field_path
    assert exc.alternative


def test_the_real_composer_output_passes_with_and_without_commitments():
    recipe = _recipe()
    assert _verify(recipe)[0]["verified"] is True
    assert _verify(recipe, composition=None)[0]["verified"] is True


def test_a_voice_that_stops_early_is_refused():
    recipe = _recipe()
    voice = _track(recipe, VOICE_AUDIO_TRACK_ID).clips[0]
    short = voice.model_copy(update={"source_duration": 12.0})
    broken = _replace_clips(recipe, VOICE_AUDIO_TRACK_ID, [short])
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(broken)
    _expect(info, reason="evidence_missing", field_path="montage_audio.source_media_ids[]")
    # Legacy verification (no commitments) never looked at voice coverage.
    assert _verify(broken, composition=None)


def test_a_voice_with_a_hole_in_it_is_refused_even_if_both_halves_are_long():
    recipe = _recipe()
    voice = _track(recipe, VOICE_AUDIO_TRACK_ID).clips[0]
    first = voice.model_copy(update={"id": "v-a", "source_duration": 13.0})
    second = voice.model_copy(
        update={"id": "v-b", "timeline_start": 14.5, "source_start": 14.0, "source_duration": 15.0}
    )
    broken = _replace_clips(recipe, VOICE_AUDIO_TRACK_ID, [first, second])
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(broken)
    _expect(info, reason="evidence_missing", field_path="montage_audio.source_media_ids[]")


def test_a_voice_that_starts_late_is_refused():
    recipe = _recipe()
    voice = _track(recipe, VOICE_AUDIO_TRACK_ID).clips[0]
    late = voice.model_copy(update={"timeline_start": 2.0, "source_duration": 27.0})
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(_replace_clips(recipe, VOICE_AUDIO_TRACK_ID, [late]))
    _expect(info, reason="evidence_missing", field_path="montage_audio.source_media_ids[]")


def test_a_sentence_snapped_voice_inside_the_slack_is_accepted():
    window = VoiceWindow(start_s=0.34, end_s=30.0 - VOICE_TAIL_SLACK_S + 0.8)
    assert _verify(_recipe(window=window))


def test_a_committed_silent_tail_is_verified_against_the_committed_span():
    window = VoiceWindow(start_s=0.34, end_s=14.0)
    recipe = _recipe(window=window, allow_silent_tail=True)
    tail = CompositionCommitments(voice_picture="hidden", voice_span_s=13.6)
    assert _verify(recipe, composition=tail)
    # Without the commitment the same short voice is a voice that stops early.
    with pytest.raises(CreatorRenderContractError):
        _verify(recipe, composition=HIDDEN)
    # And a voice shorter than the committed span is refused.
    with pytest.raises(CreatorRenderContractError):
        _verify(recipe, composition=CompositionCommitments(voice_picture="hidden", voice_span_s=25))


def test_the_hidden_voice_pictures_appearing_on_the_picture_track_is_refused():
    recipe = _recipe()
    clips = list(_track(recipe, VOICE_FOOTAGE_TRACK_ID).clips)
    clips[2] = clips[2].model_copy(update={"source_asset_id": "talk"})
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(
            _replace_clips(recipe, VOICE_FOOTAGE_TRACK_ID, clips), _contract(order_required=False)
        )
    _expect(info, reason="evidence_missing", field_path="ordering_choice")


def test_a_shot_below_the_readable_floor_is_refused():
    recipe = _recipe(30.0, min_shot_s=0.4)  # the composer was told it may flash-cut
    clips = list(_track(recipe, VOICE_FOOTAGE_TRACK_ID).clips)
    clips[1] = clips[1].model_copy(update={"source_duration": 0.4})
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(
            _replace_clips(recipe, VOICE_FOOTAGE_TRACK_ID, clips),
            _contract(duration_s=None),
        )
    _expect(info, reason="evidence_missing", field_path="target_duration_s")
    # A committed creator cadence lowers the floor: the commitment decides, not the clip.
    assert _verify(
        _replace_clips(recipe, VOICE_FOOTAGE_TRACK_ID, clips),
        _contract(duration_s=None),
        CompositionCommitments(voice_picture="hidden", min_shot_s=0.4),
    )


def test_a_whole_short_clip_is_not_a_floor_violation():
    picture = (*(_binding(m, duration_s=20.0) for m in IDS[:5]), _binding("c5", duration_s=0.6))
    recipe, _ = compile_phone_voice_behind_footage_plan(
        _binding("talk", duration_s=90.0),
        VoiceWindow(start_s=0.34, end_s=29.9),
        picture,
        duration_s=30.0,
    )
    assert _verify(recipe, _contract(exact_texts=()))


def test_an_unrequested_music_bed_is_refused_as_a_requirement_conflict():
    recipe = _recipe()
    asset = LibraryRenderAsset(
        id="music-1",
        catalog="music",
        catalog_id="song-1",
        generation="1",
        fingerprint=RenderFingerprint(sha256="d" * 64, byte_count=500),
    )
    track = TimelineTrack(
        id="music",
        kind="audio",
        clips=[
            TimelineClip(
                id="m0",
                source_asset_id="music-1",
                source_start=0,
                source_duration=30,
                timeline_start=0,
                rate=1,
                volume=0.5,
            )
        ],
    )
    with_music = recipe.model_copy(
        update={
            "tracks": [*recipe.tracks, track],
            "assets": [
                *recipe.assets,
                MediaAsset(
                    id="music-1",
                    relative_path="music-1",
                    fingerprint=AssetFingerprint(hex="d" * 64, byte_count=500),
                    duration=120.0,
                ),
            ],
            "asset_manifest": RenderAssetManifest(assets=(*recipe.asset_manifest.assets, asset)),
        }
    )
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(with_music)
    _expect(info, reason="requirement_conflict", field_path="audio_strategy")


def test_a_voice_running_past_the_picture_is_refused_even_without_a_stated_duration():
    recipe = _recipe()
    voice = _track(recipe, VOICE_AUDIO_TRACK_ID).clips[0]
    long_voice = voice.model_copy(update={"source_duration": 38.0})
    broken = _replace_clips(recipe, VOICE_AUDIO_TRACK_ID, [long_voice])
    assert broken.duration > 37  # recipe.duration is the max end across ALL tracks
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(broken, _contract(duration_s=None))
    _expect(info, reason="evidence_missing", field_path="target_duration_s")


def test_composer_routes_use_a_one_frame_duration_tolerance_others_keep_ten_percent():
    recipe = _recipe(30.0)
    off_by_two = _contract(duration_s=32.0)  # 6 % off: fine for legacy lanes
    assert _verify(recipe, off_by_two, composition=None)
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(recipe, off_by_two)
    _expect(info, reason="evidence_missing", field_path="target_duration_s")
    assert _verify(recipe, _contract(duration_s=30.0 + FRAME / 2))
    assert _verify(recipe, _contract(duration_s=30.09))  # within the 0.1 s floor


def test_unmuting_a_picture_clip_is_still_caught_by_the_existing_audio_checks():
    recipe = _recipe()
    clips = list(_track(recipe, VOICE_FOOTAGE_TRACK_ID).clips)
    clips[3] = clips[3].model_copy(update={"volume": 1.0})
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(_replace_clips(recipe, VOICE_FOOTAGE_TRACK_ID, clips))
    _expect(info, reason="evidence_missing", field_path="montage_audio.source_media_ids[]")


def test_a_wrapped_or_reordered_picture_is_caught_by_the_existing_order_check():
    recipe = _recipe()
    clips = list(_track(recipe, VOICE_FOOTAGE_TRACK_ID).clips)
    clips[4] = clips[4].model_copy(update={"source_asset_id": "c0"})  # c0 shown again
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(_replace_clips(recipe, VOICE_FOOTAGE_TRACK_ID, clips))
    _expect(info, reason="evidence_missing", field_path="ordering_choice")


def test_a_text_layer_held_past_the_picture_is_refused():
    recipe = _recipe()
    layer = recipe.text_layers[0].model_copy(update={"end": recipe.duration + 1.0})
    broken = recipe.model_copy(update={"text_layers": [layer]})
    with pytest.raises(CreatorRenderContractError) as info:
        _verify(broken)
    _expect(info, reason="evidence_missing", field_path="opening_title_duration_s")
    assert _verify(recipe)  # the composed title is fine
