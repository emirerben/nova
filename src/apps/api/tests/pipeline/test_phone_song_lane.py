"""KRI-374 lane D1: the creator-song audio lane of the guided phone compiler."""

from __future__ import annotations

import pytest

from app.config import settings
from app.kria.recipes_v2 import EditRecipeV2
from app.pipeline.lipsync_montage import lipsync_sync_error_s, plan_lipsync_montage
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.unified_montage import plan_unified_montage
from app.services.phone_rollout import validate_phone_pilot_recipe
from tests._prod_profile import PROD_VERIFIED_FEATURES
from tests.pipeline.user_song_helpers import (
    SONG_DURATION_S,
    SONG_GENERATION,
    SONG_ITEM_ID,
    alignment,
    analysis,
    bindings_for,
    compiled_plan,
    confident,
    photo,
    song_bed,
    take,
    unmatched,
)


def lipsync_plan(*extra_clips, rows=None):
    clips = [take("A"), take("B"), *extra_clips]
    rows = rows or [confident("A", 10), confident("B", 25)]
    return plan_lipsync_montage(clips, alignment(*rows), analysis(), plan_item_id=SONG_ITEM_ID)


def background_plan():
    clips = [take(f"c{i}", 6.0) for i in range(1, 6)]
    return plan_unified_montage(
        clips,
        song_beats=analysis().beats_s,
        song_lines=analysis().lines,
        song_duration_s=SONG_DURATION_S,
        song_plan_item_id=SONG_ITEM_ID,
        song_generation=SONG_GENERATION,
    )


def song_clip(recipe):
    track = next(t for t in recipe.tracks if t.kind == "audio")
    assert track.id == "song" and len(track.clips) == 1
    return track.clips[0]


def test_lipsync_recipe_replaces_camera_audio_and_starts_the_song_at_the_window():
    result = lipsync_plan()
    plan = compiled_plan(result)
    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(plan, footage, visuals, song=song_bed())

    clip = song_clip(recipe)
    assert recipe.audio.original_volume == 0.0
    assert recipe.audio.music_asset_id == "song-" + SONG_ITEM_ID
    assert clip.source_asset_id == recipe.audio.music_asset_id
    assert clip.source_start == pytest.approx(result.user_song.window_start_s)
    assert clip.timeline_start == 0 and clip.rate == 1
    assert clip.source_duration == pytest.approx(
        recipe.tracks[0].clips[-1].timeline_start + recipe.tracks[0].clips[-1].source_duration
    )
    # Lip-sync fades: a near-instant in (the vocal may start at once), a short tail.
    assert (clip.audio_fade_in, clip.audio_fade_out) == (0.05, 0.3)
    assert {"musicBed", "audioMix"} <= recipe.required_capabilities
    assert EditRecipeV2.model_validate_json(recipe.model_dump_json()) == recipe


def test_background_recipe_uses_the_half_second_fades():
    result = background_plan()
    plan = compiled_plan(result)
    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(plan, footage, visuals, song=song_bed())
    clip = song_clip(recipe)
    assert recipe.audio.original_volume == 0.0
    assert clip.source_start == pytest.approx(result.user_song.window_start_s)
    assert (clip.audio_fade_in, clip.audio_fade_out) == (0.5, 0.5)


def test_song_asset_is_a_private_plan_item_asset_in_the_manifest():
    result = lipsync_plan()
    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(compiled_plan(result), footage, visuals, song=song_bed())
    asset = next(a for a in recipe.asset_manifest.assets if a.kind == "song")
    assert (asset.plan_item_id, asset.generation) == (SONG_ITEM_ID, str(SONG_GENERATION))
    assert (asset.fingerprint.sha256, asset.fingerprint.byte_count) == ("e" * 64, 3_000_000)
    projected = next(a for a in recipe.assets if a.id == asset.id)
    assert projected.duration == SONG_DURATION_S


@pytest.mark.parametrize("builder", [lipsync_plan, background_plan])
def test_song_recipes_pass_the_pilot_gate_under_the_prod_verified_features(monkeypatch, builder):
    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    result = builder()
    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(compiled_plan(result), footage, visuals, song=song_bed())
    validate_phone_pilot_recipe(recipe)  # must not raise


def test_song_recipe_is_refused_when_the_device_has_not_verified_the_bed(monkeypatch):
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [f for f in PROD_VERIFIED_FEATURES if f != "musicBed"],
    )
    result = lipsync_plan()
    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(compiled_plan(result), footage, visuals, song=song_bed())
    with pytest.raises(ValueError, match="capability"):
        validate_phone_pilot_recipe(recipe)


def test_lipsync_recipe_keeps_every_take_on_the_song_clock():
    result = lipsync_plan()
    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(compiled_plan(result), footage, visuals, song=song_bed())
    clips = {c.id: c for c in recipe.tracks[0].clips}
    song = result.user_song
    assert len(clips) == len(song.takes) == 2
    for cut in result.snapshot.fast_cuts:
        clip = next(c for c in recipe.tracks[0].clips if c.id == cut.cut_id)
        pinned = song.takes[cut.media_id]
        assert (
            lipsync_sync_error_s(
                output_start_s=clip.timeline_start,
                source_start_s=clip.source_start,
                delta_s=pinned.delta_s,
                window_start_s=song.window_start_s,
            )
            <= 0.001
        )


def test_broll_gap_compiles_muted_under_the_song():
    clips = [take("A", 12), take("B", 20), take("U", 10), photo()]
    rows = [confident("A", 10), confident("B", 30), unmatched("U")]
    result = plan_lipsync_montage(clips, alignment(*rows), analysis(), plan_item_id=SONG_ITEM_ID)
    footage, visuals = bindings_for(result)
    recipe = compile_phone_guided_plan(compiled_plan(result), footage, visuals, song=song_bed())
    assert recipe.audio.original_volume == 0.0
    assert recipe.duration == pytest.approx(result.snapshot.duration_s)


# ── identity guards ─────────────────────────────────────────────────────────


def test_a_song_plan_without_a_bed_fails_closed():
    result = lipsync_plan()
    footage, visuals = bindings_for(result)
    with pytest.raises(UnsupportedPhonePlan, match="song binding") as caught:
        compile_phone_guided_plan(compiled_plan(result), footage, visuals)
    assert caught.value.capability == "musicBed"


def test_a_bed_for_a_plan_without_a_song_fails_closed():
    clips = [take(f"c{i}", 6.0) for i in range(1, 4)]
    result = plan_unified_montage(clips)
    footage, visuals = bindings_for(result)
    with pytest.raises(UnsupportedPhonePlan, match="no creator song"):
        compile_phone_guided_plan(compiled_plan(result), footage, visuals, song=song_bed())


@pytest.mark.parametrize(
    "changes",
    [
        {"generation": "stale"},
        {"plan_item_id": "someone-elses-item"},
        {"duration_s": 20.0},  # the song is shorter than the approved window needs
        {"window_start_s": 3.0},
    ],
)
def test_a_replaced_song_fails_closed(changes):
    result = lipsync_plan()
    footage, visuals = bindings_for(result)
    with pytest.raises(UnsupportedPhonePlan, match="replaced since approval"):
        compile_phone_guided_plan(compiled_plan(result), footage, visuals, song=song_bed(**changes))


# ── lip-sync refit guard ────────────────────────────────────────────────────


def test_lipsync_refit_truncates_the_end_and_never_shifts_the_start():
    result = lipsync_plan()
    plan = compiled_plan(result)
    footage, visuals = bindings_for(result, original_duration={"A": 14.0})
    recipe = compile_phone_guided_plan(plan, footage, visuals, song=song_bed())
    first = next(c for c in recipe.tracks[0].clips if c.id == plan.story_timeline[0].moment_id)
    moment = plan.story_timeline[0]
    assert first.source_start == pytest.approx(moment.source_start_s)  # unchanged
    assert first.source_duration == pytest.approx(14.0 - 0.05 - moment.source_start_s)
    assert first.source_duration < moment.duration_s


def test_lipsync_refit_fails_closed_when_the_start_no_longer_fits():
    result = lipsync_plan()
    plan = compiled_plan(result)
    # B's cut starts ~1 s into the take; a 1.04 s original leaves no room for it.
    footage, visuals = bindings_for(result, original_duration={"B": 1.04})
    with pytest.raises(UnsupportedPhonePlan, match="can no longer stay in sync"):
        compile_phone_guided_plan(plan, footage, visuals, song=song_bed())


def test_background_clips_keep_the_shared_start_shifting_refit():
    result = background_plan()
    plan = compiled_plan(result)
    footage, visuals = bindings_for(result, original_duration={"c1": 1.5})
    recipe = compile_phone_guided_plan(plan, footage, visuals, song=song_bed())
    first = recipe.tracks[0].clips[0]
    assert first.source_start + first.source_duration <= 1.5 - 0.05 + 1e-6


def test_a_plan_that_drifted_off_its_song_position_is_refused():
    result = lipsync_plan()
    plan = compiled_plan(result)
    drifted = plan.model_copy(
        update={
            "story_timeline": [
                plan.story_timeline[0].model_copy(
                    update={
                        "source_start_s": plan.story_timeline[0].source_start_s + 0.2,
                        "source_end_s": plan.story_timeline[0].source_end_s + 0.2,
                    }
                ),
                *plan.story_timeline[1:],
            ]
        }
    )
    footage, visuals = bindings_for(result)
    with pytest.raises(UnsupportedPhonePlan, match="drifted off its song position"):
        compile_phone_guided_plan(drifted, footage, visuals, song=song_bed())
