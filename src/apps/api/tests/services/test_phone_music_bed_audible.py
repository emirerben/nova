"""KRI-470 PR-G: the separate music bed counts as audible in the phone verifier.

The device plays ``audio.music_asset_id`` as its own bed from source 0 at
``music_volume`` ON TOP of every audio-track clip. A recipe that names the same
asset in both therefore plays it twice (prod job 934811f3, KRI-481), and the old
verifier only looked at track clips, so the contract could not see it.

Failure modes, written before the code:

* the song lane with its bed switched back on (#1441 reverted)  -> refused
* the speech-montage compiler with a music bed (clip AND bed)   -> never emitted
* a bed that is the camera's own audio while camera audio is off -> refused
* a lone bed (no track clip of that asset)                       -> NOT a double
* a track clip muted to 0 + a bed                                -> NOT a double
* a bed at volume 0 (the silenced legacy reference)              -> NOT audible
"""

from __future__ import annotations

import pytest

from app.kria.render_assets import RenderFingerprint
from app.pipeline.phone_recipe_shared import PhoneMusicBed
from app.services.creator_render_contract import (
    CreatorRenderContract,
    CreatorRenderContractError,
    doubled_soundtrack_assets,
    verify_phone_recipe,
)
from tests.pipeline.test_phone_song_lane import background_plan, lipsync_plan
from tests.pipeline.user_song_helpers import bindings_for, compiled_plan, song_bed


def _song_recipe(make_plan):
    from app.pipeline.phone_guided_plan import compile_phone_guided_plan

    result = make_plan()
    footage, visuals = bindings_for(result)
    return compile_phone_guided_plan(compiled_plan(result), footage, visuals, song=song_bed())


def _contract(**changes) -> CreatorRenderContract:
    return CreatorRenderContract(generation_id="g").rebind(**changes)


def _with_bed_volume(recipe, volume: float):
    return recipe.model_copy(
        update={"audio": recipe.audio.model_copy(update={"music_volume": volume})}
    )


@pytest.mark.parametrize("make_plan", [background_plan, lipsync_plan])
def test_the_real_song_recipe_passes_and_a_revived_bed_is_refused(make_plan):
    recipe = _song_recipe(make_plan)
    assert doubled_soundtrack_assets(recipe) == []
    assert verify_phone_recipe(_contract(), recipe)

    doubled = _with_bed_volume(recipe, 1.0)  # #1441 reverted
    assert doubled_soundtrack_assets(doubled) == [recipe.audio.music_asset_id]
    with pytest.raises(CreatorRenderContractError) as caught:
        verify_phone_recipe(_contract(), doubled)
    assert caught.value.decline_reason == "requirement_conflict"
    assert caught.value.field_path == "audio_strategy"
    assert caught.value.alternative


def test_a_bed_is_audible_at_any_volume_above_zero():
    recipe = _song_recipe(background_plan)
    with pytest.raises(CreatorRenderContractError):
        verify_phone_recipe(_contract(), _with_bed_volume(recipe, 0.05))


def test_a_lone_bed_with_no_track_clip_is_not_a_double():
    recipe = _song_recipe(background_plan)
    song_id = recipe.audio.music_asset_id
    tracks = [t for t in recipe.tracks if t.kind != "audio"]
    lone = recipe.model_copy(update={"tracks": tracks})
    lone = _with_bed_volume(lone, 1.0)
    assert lone.audio.music_asset_id == song_id
    assert doubled_soundtrack_assets(lone) == []
    assert verify_phone_recipe(_contract(), lone)


def test_a_muted_track_clip_beside_a_bed_is_not_a_double():
    recipe = _song_recipe(background_plan)
    tracks = []
    for track in recipe.tracks:
        if track.kind == "audio":
            clips = [clip.model_copy(update={"volume": 0}) for clip in track.clips]
            track = track.model_copy(update={"clips": clips})
        tracks.append(track)
    muted = _with_bed_volume(recipe.model_copy(update={"tracks": tracks}), 1.0)
    assert doubled_soundtrack_assets(muted) == []


def test_the_speech_montage_compiler_never_plays_its_music_twice():
    from app.pipeline.phone_speech_montage_plan import (
        PhoneSpeechSection,
        compile_phone_speech_montage_plan,
    )
    from tests.services.test_phone_speech_montage_job import _binding

    music = PhoneMusicBed(
        catalog_id="track",
        generation="1",
        fingerprint=RenderFingerprint(sha256="c" * 64, byte_count=1000),
        duration_s=60.0,
        start_s=0.0,
        volume=0.3,
    )
    recipe, receipt = compile_phone_speech_montage_plan(
        (
            PhoneSpeechSection(
                kind="speech",
                speaker=_binding("talk"),
                source_start_s=0,
                source_end_s=7,
                visual="cutaways",
            ),
            PhoneSpeechSection(kind="montage", duration_s=6.0),
        ),
        tuple(_binding(f"b{i}", duration_s=9) for i in range(3)),
        music=music,
    )
    assert receipt.music is True
    assert any(t.id == "music" for t in recipe.tracks)  # the bed is its track clips
    assert doubled_soundtrack_assets(recipe) == []
    assert verify_phone_recipe(_contract(), recipe, source_audio={"talk": True})


def test_a_bed_that_is_the_camera_audio_counts_as_camera_audio():
    from tests.services.test_creator_render_contract import _speech_recipe

    recipe = _speech_recipe()
    camera = next(a for a in recipe.asset_manifest.assets if a.kind == "original")
    # No camera sound from the picture clips (volume 0 on video, speech track muted)...
    tracks = []
    for track in recipe.tracks:
        clips = [clip.model_copy(update={"volume": 0}) for clip in track.clips]
        tracks.append(track.model_copy(update={"clips": clips}))
    silent = recipe.model_copy(update={"tracks": tracks})
    forbid = _contract(original_audio="forbid")
    assert verify_phone_recipe(forbid, silent, source_audio={camera.media_id: True})
    # ...but a bed pointing at the camera file plays it anyway.
    leaking = silent.model_copy(
        update={
            "audio": silent.audio.model_copy(
                update={"music_asset_id": camera.id, "music_volume": 0.5}
            )
        }
    )
    with pytest.raises(CreatorRenderContractError) as caught:
        verify_phone_recipe(forbid, leaking, source_audio={camera.media_id: True})
    assert caught.value.decline_reason == "evidence_missing"
    assert caught.value.field_path == "montage_audio.preserve_source_audio"
