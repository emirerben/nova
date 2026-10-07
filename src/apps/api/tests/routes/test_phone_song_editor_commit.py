"""KRI-374 D2: the phone editor commit keeps a creator song and its lip-sync takes honest.

``prepare_phone_editor_commit`` for a guided variant whose plan carries ``user_song``:
the song window follows the committed duration, a lip-sync take is re-synced to its
pinned song offset, and a retimed or unreachable take is refused with a clear message.
"""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.config import settings
from app.kria.device_render import make_device_request
from app.pipeline.guided_story import (
    GuidedStoryExecutionPlan,
    compile_execution_plan,
    song_reference_variant_fields,
)
from app.pipeline.lipsync_montage import lipsync_sync_error_s, plan_lipsync_montage
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.unified_montage import plan_unified_montage
from app.routes import generative_jobs as gj
from app.schemas.user_song import MIN_PLAYABLE_SONG_S
from app.services.device_render import device_status, pin_device_request
from app.services.kria_editor_ops import compile_editor_ops
from app.services.phone_editor import (
    PHONE_EDITOR_PLAN_FIELD,
    PHONE_EDITOR_SAVED_PLAN_FIELD,
    prepare_phone_editor_commit,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD
from app.services.user_song_projection import user_song_for_variant
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
    song_bed,
    take,
)


@pytest.fixture(autouse=True)
def _phone_profile(monkeypatch):
    monkeypatch.setattr(gj.settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_verified_features", list(PROD_VERIFIED_FEATURES))
    monkeypatch.setattr(gj.settings, "guided_story_editor_v2_enabled", True)


def _job(result, *, song_duration_s=SONG_DURATION_S):
    guided = result.guided_edit()
    plan = compile_execution_plan(guided, track=None)
    bindings, _visuals = bindings_for(result)
    job = SimpleNamespace(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        status="awaiting_device",
        current_phase=None,
        mode="content_plan",
        all_candidates={"clip_paths": []},
        assembly_plan={
            "guided_edit": guided,
            "guided_story_execution_plan": plan,
            PHONE_SOURCES_FIELD: [b.model_dump(mode="json") for b in bindings],
            "variants": [
                {
                    "variant_id": "guided_story",
                    "resolved_archetype": "guided_story",
                    "render_destination": "device",
                    "render_status": "awaiting_device",
                    "render_generation_id": "first",
                    "duration_s": plan["resolved_duration_s"],
                    "text_mode": "agent_text",
                    "intro_mode": "linear",
                    "intro_layout": "linear",
                    "text_elements": copy.deepcopy(plan["text_elements"]),
                    # Every guided v6+ plan is reference-only in prod, and that is the
                    # gate KRI-428 had to open for the creator's own song.
                    **song_reference_variant_fields(plan),
                }
            ],
        },
    )
    pin_device_request(
        job,
        make_device_request(
            job_id=job.id,
            variant_id="guided_story",
            revision=1,
            recipe=compile_phone_guided_plan(
                GuidedStoryExecutionPlan.model_validate(plan),
                bindings,
                song=song_bed(duration_s=song_duration_s),
            ),
        ),
        base_generation="first",
    )
    return job


def lipsync_job():
    clips = [take("A"), take("B")]
    result = plan_lipsync_montage(
        clips,
        alignment(confident("A", 10), confident("B", 25)),
        analysis(),
        plan_item_id=SONG_ITEM_ID,
    )
    return _job(result), result


def background_job():
    info = analysis()
    clips = [take(f"c{i}", 6.0) for i in range(1, 6)]
    result = plan_unified_montage(
        clips,
        song_beats=info.beats_s,
        song_lines=info.lines,
        song_duration_s=SONG_DURATION_S,
        song_plan_item_id=SONG_ITEM_ID,
        song_generation=SONG_GENERATION,
    )
    return _job(result), result


def _text_only_commit(job, *, plan=None):
    """A Save that only touched text, optionally with a replaced editor plan."""

    def prepare(staged):
        variant = staged.assembly_plan["variants"][0]
        if plan is not None:
            variant[PHONE_EDITOR_PLAN_FIELD] = plan
        return {
            "has_render_section": True,
            "guided_revision": {"revision_number": 2},
            "sections": {"text_elements": True, "timeline": False},
            "generation": "second",
        }

    return prepare_phone_editor_commit(job, "guided_story", prepare=prepare)


def _recipe(job):
    return device_status(job, "guided_story").request.recipe


def _song_clip(recipe):
    (clip,) = next(t for t in recipe.tracks if t.id == "song").clips
    return clip


def _take_offsets_ok(job, result):
    recipe = _recipe(job)
    song = result.user_song
    cut_media = {cut.cut_id: cut.media_id for cut in result.snapshot.fast_cuts}
    checked = 0
    for clip in next(t for t in recipe.tracks if t.kind == "video").clips:
        media_id = cut_media.get(clip.id)
        pinned = song.takes.get(media_id)
        if pinned is None:
            continue
        assert (
            lipsync_sync_error_s(
                output_start_s=clip.timeline_start,
                source_start_s=clip.source_start,
                delta_s=pinned.delta_s,
                window_start_s=song.window_start_s,
            )
            <= 0.001
        )
        checked += 1
    return checked


def test_a_text_only_save_keeps_the_song_and_every_take_in_sync():
    job, result = lipsync_job()
    before = _song_clip(_recipe(job))
    _text_only_commit(job)
    recipe = _recipe(job)
    assert device_status(job, "guided_story").request.identity.recipe_revision == 2
    song = _song_clip(recipe)
    assert song.source_start == pytest.approx(before.source_start)
    assert recipe.audio.original_volume == 0.0
    assert _take_offsets_ok(job, result) == 2


def test_the_commit_resyncs_a_take_that_drifted_off_its_song_position():
    job, result = lipsync_job()
    plan = copy.deepcopy(job.assembly_plan["guided_story_execution_plan"])
    moment = plan["story_timeline"][0]
    moment["source_start_s"] = round(moment["source_start_s"] + 0.2, 3)
    moment["source_end_s"] = round(moment["source_end_s"] + 0.2, 3)
    _text_only_commit(job, plan=plan)  # without the resync the compiler refuses this plan
    assert _take_offsets_ok(job, result) == 2


def test_the_commit_refuses_a_retimed_lipsync_take_with_a_clear_message():
    job, _result = lipsync_job()
    plan = copy.deepcopy(job.assembly_plan["guided_story_execution_plan"])
    plan["story_timeline"][0]["playback_rate"] = 2.0
    with pytest.raises(HTTPException) as caught:
        _text_only_commit(job, plan=plan)
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "unsupported_phone_edit"
    assert "normal speed" in caught.value.detail["reason"]
    assert device_status(job, "guided_story").request.identity.recipe_revision == 1


def test_the_commit_rejects_a_take_that_can_no_longer_reach_its_song_position():
    job, _result = lipsync_job()
    plan = copy.deepcopy(job.assembly_plan["guided_story_execution_plan"])
    # B now belongs 30 s later in the song than its place in the cut allows.
    plan["user_song"]["takes"]["B"]["delta_s"] += 30.0
    with pytest.raises(HTTPException) as caught:
        _text_only_commit(job, plan=plan)
    assert caught.value.status_code == 422
    assert "before it was filmed" in caught.value.detail["reason"]


def test_a_retime_is_fine_for_background_clips():
    job, _result = background_job()
    plan = copy.deepcopy(job.assembly_plan["guided_story_execution_plan"])
    plan["story_timeline"][0]["playback_rate"] = 1.0
    _text_only_commit(job, plan=plan)
    assert _song_clip(_recipe(job)).source_asset_id == f"song-{SONG_ITEM_ID}"


@pytest.mark.parametrize("target_s", [4, 12])
def test_retiming_a_background_montage_rewindows_the_song_from_the_same_start(target_s):
    job, result = background_job()
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    compiled = compile_editor_ops(
        job,
        variant,
        [{"op": "set_total_duration", "target_s": target_s, "strategy": "proportional"}],
    )
    payload = compiled.payload
    payload.guided_revision_number = revision["revision_number"]
    gj.prepare_editor_commit(job, "guided_story", payload)

    saved = job.assembly_plan["variants"][0][PHONE_EDITOR_SAVED_PLAN_FIELD]
    song = saved["user_song"]
    assert song["window_start_s"] == pytest.approx(result.user_song.window_start_s)
    assert song["window_end_s"] == pytest.approx(
        song["window_start_s"] + saved["resolved_duration_s"], abs=0.001
    )
    assert song["window_end_s"] - song["window_start_s"] != result.user_song.window_duration_s
    recipe = _recipe(job)
    clip = _song_clip(recipe)
    assert clip.source_start == pytest.approx(result.user_song.window_start_s)
    assert clip.source_duration <= recipe.duration + 1e-6
    assert recipe.duration == pytest.approx(target_s, abs=1 / 30 + 0.001)


def test_squeezing_a_lipsync_montage_is_refused_rather_than_sent_off_the_song():
    job, _result = lipsync_job()
    before = device_status(job, "guided_story").request
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    compiled = compile_editor_ops(
        job, variant, [{"op": "set_total_duration", "target_s": 20, "strategy": "proportional"}]
    )
    compiled.payload.guided_revision_number = revision["revision_number"]
    with pytest.raises(HTTPException) as caught:
        gj.prepare_editor_commit(job, "guided_story", compiled.payload)
    assert caught.value.detail["code"] == "unsupported_phone_edit"
    assert "starts before it was filmed" in caught.value.detail["reason"]
    assert device_status(job, "guided_story").request == before


def _push_take_past_its_footage(job, media_id="A"):
    """Move a take's pinned song offset so its cut needs footage past the take's end."""
    plan = copy.deepcopy(job.assembly_plan["guided_story_execution_plan"])
    moment = next(m for m in plan["story_timeline"] if m["media_id"] == media_id)
    source_len = 20.0  # take() default duration
    overshoot = moment["source_start_s"] + moment["duration_s"] - source_len
    # Lower delta => later source start. Push the window end 1 s past the footage.
    plan["user_song"]["takes"][media_id]["delta_s"] -= max(0.0, -overshoot) + 1.0
    return plan


def test_the_editor_commit_refuses_a_take_that_runs_past_its_footage():
    """Probe (KRI-374 review): no source duration reached the resync, so the compiler's
    END-only refit silently shortened the clip and left a black hole under the song."""
    job, _result = lipsync_job()
    plan = _push_take_past_its_footage(job)
    with pytest.raises(HTTPException) as caught:
        _text_only_commit(job, plan=plan)
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "unsupported_phone_edit"
    assert "runs past its end" in caught.value.detail["reason"]
    assert device_status(job, "guided_story").request.identity.recipe_revision == 1


def test_the_phone_compiler_refuses_to_truncate_a_lipsync_take_by_more_than_a_frame():
    result = plan_lipsync_montage(
        [take("A"), take("B")],
        alignment(confident("A", 10), confident("B", 25)),
        analysis(),
        plan_item_id=SONG_ITEM_ID,
    )
    plan = compiled_plan(result)
    # The phone measures take A 7 s shorter than the server did when it matched it.
    bindings, _visuals = bindings_for(result, original_duration={"A": 10.0, "B": 20.0})
    moment_a = next(m for m in plan.story_timeline if m.media_id == "A")
    assert moment_a.source_end_s > 10.1
    with pytest.raises(UnsupportedPhonePlan, match="runs past the end"):
        compile_phone_guided_plan(plan, bindings, song=song_bed())


# ── KRI-428: volume, start point and remove, through the real Save path ──────────────
#
# Every test drives `gj.prepare_editor_commit` -> `prepare_phone_editor_commit` ->
# `compile_phone_guided_plan`. The first prod attempt at KRI-374 passed because each test
# stubbed this step, so nothing here may.


def _song_save(job, **section):
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    payload = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        guided_revision_number=revision["revision_number"],
        user_song=gj.EditorCommitUserSong(**section),
    )
    gj.require_guided_story_editor_commit(job, "guided_story", payload)
    return gj.prepare_editor_commit(job, "guided_story", payload)


def _text_save(job):
    """A Save that touches only text, through the same real path."""
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    element = copy.deepcopy(variant["text_elements"][0]) if variant["text_elements"] else None
    payload = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        guided_revision_number=revision["revision_number"],
        text_elements=[element] if element else None,
        caption_meta=None if element else gj.EditorCommitCaptionMeta(),
    )
    return gj.prepare_editor_commit(job, "guided_story", payload)


def _video_length(job):
    return job.assembly_plan["variants"][0]["duration_s"]


def _projection(job):
    return user_song_for_variant(job, job.assembly_plan["variants"][0], song_filename="a.mp3")


def test_the_gate_lets_the_creators_song_through_but_not_catalog_music():
    job, _result = background_job()
    variant = job.assembly_plan["variants"][0]
    assert variant["music_playback_mode"] == "reference_only"
    base = gj.variant_render_baseline(variant)
    ok = gj.EditorCommitRequest(base_generation=base, user_song=gj.EditorCommitUserSong(volume=0.5))
    gj.require_guided_story_editor_commit(job, "guided_story", ok)
    catalog = gj.EditorCommitRequest(base_generation=base, music_track_id="another-song")
    with pytest.raises(HTTPException) as caught:
        gj.require_guided_story_editor_commit(job, "guided_story", catalog)
    assert caught.value.detail == "song_added_when_posting"


def test_volume_is_kept_across_two_saves():
    job, _result = background_job()
    _song_save(job, volume=0.3)
    assert _song_clip(_recipe(job)).volume == pytest.approx(0.3)
    assert _projection(job)["volume"] == pytest.approx(0.3)
    # A second Save of something else must not reset it to 1.0.
    _song_save(
        job,
        window_start_s=job.assembly_plan["variants"][0][PHONE_EDITOR_SAVED_PLAN_FIELD]["user_song"][
            "window_start_s"
        ],
    )
    assert _song_clip(_recipe(job)).volume == pytest.approx(0.3)
    assert device_status(job, "guided_story").request.identity.recipe_revision == 3


def test_a_text_only_save_after_a_volume_change_keeps_the_volume():
    job, _result = background_job()
    _song_save(job, volume=0.4)
    _text_save(job)
    recipe = _recipe(job)
    assert device_status(job, "guided_story").request.identity.recipe_revision == 3
    assert _song_clip(recipe).volume == pytest.approx(0.4)
    assert recipe.audio.original_volume == 0.0


@pytest.mark.parametrize("make_job", [background_job, lipsync_job])
def test_an_editor_save_never_turns_the_silenced_song_bed_back_on(make_job):
    """KRI-470 PR-G: the editor recompiles through the same song lane, so every Save
    must keep the song audible once (KRI-481). Judged by the contract verifier, the
    check the pin runs, not by re-reading one field."""
    from app.services.creator_render_contract import (
        CreatorRenderContract,
        doubled_soundtrack_assets,
        verify_phone_recipe,
    )

    job, _result = make_job()
    for save in (lambda: _song_save(job, volume=0.5), lambda: _text_save(job)):
        save()
        recipe = _recipe(job)
        assert doubled_soundtrack_assets(recipe) == []
        assert verify_phone_recipe(CreatorRenderContract(generation_id="g"), recipe)


def test_a_background_start_move_follows_into_the_recipe():
    job, result = background_job()
    old = result.user_song.window_start_s
    new = old + 5.0
    before = _recipe(job)
    _song_save(job, window_start_s=new)
    clip = _song_clip(_recipe(job))
    assert clip.source_start == pytest.approx(new)
    saved = job.assembly_plan["variants"][0][PHONE_EDITOR_SAVED_PLAN_FIELD]["user_song"]
    assert saved["window_start_s"] == pytest.approx(new)
    assert saved["window_end_s"] == pytest.approx(new + _video_length(job), abs=0.01)
    projection = _projection(job)
    assert projection["window_start_s"] == pytest.approx(new)
    # The song moved; the cuts did not (no beat re-snap), and the video is the same length.
    after = _recipe(job)
    assert after.duration == pytest.approx(before.duration)

    def cuts(recipe):
        return [
            (c.timeline_start, c.source_duration)
            for c in next(t for t in recipe.tracks if t.kind == "video").clips
        ]

    assert cuts(after) == cuts(before)


def _set_total(job, target_s):
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    compiled = compile_editor_ops(
        job,
        variant,
        [{"op": "set_total_duration", "target_s": target_s, "strategy": "proportional"}],
    )
    compiled.payload.guided_revision_number = revision["revision_number"]
    return gj.prepare_editor_commit(job, "guided_story", compiled.payload)


def _saved_song(job):
    return job.assembly_plan["variants"][0][PHONE_EDITOR_SAVED_PLAN_FIELD]["user_song"]


def test_a_start_that_leaves_under_a_second_of_song_is_refused():
    job, _result = background_job()
    before = device_status(job, "guided_story").request
    with pytest.raises(HTTPException) as caught:
        _song_save(job, window_start_s=SONG_DURATION_S - 0.5)
    assert caught.value.status_code == 422
    assert caught.value.detail == {
        "code": "user_song_window_out_of_range",
        "reason": "That start point leaves less than a second of your song. Slide it earlier.",
    }
    assert device_status(job, "guided_story").request == before


def test_a_start_leaving_exactly_the_minimum_plays_that_last_second():
    job, _result = background_job()
    _song_save(job, window_start_s=SONG_DURATION_S - MIN_PLAYABLE_SONG_S)
    clip = _song_clip(_recipe(job))
    assert clip.source_duration == pytest.approx(MIN_PLAYABLE_SONG_S)
    assert _projection(job)["window_end_s"] == pytest.approx(SONG_DURATION_S)


def test_extending_past_the_remaining_song_is_accepted_and_the_song_stops_at_its_end():
    job, _result = background_job()
    length = _video_length(job)
    start = SONG_DURATION_S - length  # the song exactly fits today
    _song_save(job, window_start_s=start)
    _set_total(job, length + 5)
    recipe = _recipe(job)
    clip = _song_clip(recipe)
    assert recipe.duration == pytest.approx(length + 5, abs=0.1)
    assert clip.source_start == pytest.approx(start)
    assert clip.source_duration == pytest.approx(SONG_DURATION_S - start)
    assert clip.source_duration < recipe.duration
    # It fades out at its REAL end (the clip length), not at the video's end.
    assert clip.audio_fade_out > 0
    song = _saved_song(job)
    assert song["window_end_s"] == pytest.approx(SONG_DURATION_S)
    assert song["window_end_s"] - song["window_start_s"] < _video_length(job)
    assert _projection(job)["window_end_s"] == pytest.approx(SONG_DURATION_S)


def test_extending_within_the_song_grows_the_song_with_the_video():
    job, _result = background_job()
    length = _video_length(job)
    start = _song_clip(_recipe(job)).source_start
    assert start + length + 3 < SONG_DURATION_S
    _set_total(job, length + 3)
    clip = _song_clip(_recipe(job))
    assert clip.source_start == pytest.approx(start)
    assert clip.source_duration == pytest.approx(_recipe(job).duration, abs=0.05)
    assert _saved_song(job)["window_end_s"] == pytest.approx(start + length + 3, abs=0.1)


def test_shortening_then_extending_recomputes_the_window_from_the_start():
    job, _result = background_job()
    length = _video_length(job)
    start = SONG_DURATION_S - length
    _song_save(job, window_start_s=start)
    _set_total(job, length - 3)
    assert _song_clip(_recipe(job)).source_duration == pytest.approx(length - 3, abs=0.1)
    assert _saved_song(job)["window_end_s"] == pytest.approx(start + length - 3, abs=0.1)
    _set_total(job, length + 4)
    clip = _song_clip(_recipe(job))
    # Grew back up to the song end, then stopped: never from the previous window end.
    assert clip.source_duration == pytest.approx(SONG_DURATION_S - start)
    assert _saved_song(job)["window_end_s"] == pytest.approx(SONG_DURATION_S)
    _set_total(job, length - 2)
    assert _song_clip(_recipe(job)).source_duration == pytest.approx(length - 2, abs=0.1)


def test_a_video_longer_than_the_whole_song_plays_it_to_the_end_without_error():
    info = analysis(duration_s=40.0, line_starts=tuple(float(i) for i in range(2, 38, 4)))
    result = plan_unified_montage(
        [take(f"c{i}", 20.0) for i in range(1, 6)],
        song_beats=info.beats_s,
        song_lines=info.lines,
        song_duration_s=40.0,
        song_plan_item_id=SONG_ITEM_ID,
        song_generation=SONG_GENERATION,
    )
    job = _job(result, song_duration_s=40.0)
    _song_save(job, window_start_s=0.0)
    _set_total(job, 50)
    clip = _song_clip(_recipe(job))
    assert _recipe(job).duration == pytest.approx(50, abs=0.1)
    assert clip.source_start == 0.0
    assert clip.source_duration == pytest.approx(40.0)
    assert _saved_song(job)["window_end_s"] == pytest.approx(40.0)


def _lipsync_job_with_song_ending_at_the_cut():
    """A lip-sync montage whose song ends exactly where its cut does."""
    first = plan_lipsync_montage(
        [take("A"), take("B")],
        alignment(confident("A", 10), confident("B", 25)),
        analysis(),
        plan_item_id=SONG_ITEM_ID,
    )
    end = round(first.user_song.window_end_s, 3)
    short = analysis(duration_s=end, line_starts=tuple(float(i) for i in range(2, int(end), 4)))
    result = plan_lipsync_montage(
        [take("A"), take("B")],
        alignment(confident("A", 10), confident("B", 25)),
        short,
        plan_item_id=SONG_ITEM_ID,
    )
    assert result.user_song.window_end_s == pytest.approx(result.user_song.duration_s, abs=0.01)
    return _job(result, song_duration_s=result.user_song.duration_s), result


def test_a_lipsync_video_that_outruns_the_song_keeps_the_strict_song_error():
    job, _result = _lipsync_job_with_song_ending_at_the_cut()
    before = device_status(job, "guided_story").request
    with pytest.raises(HTTPException) as caught:
        _set_total(job, _video_length(job) + 3)
    assert caught.value.status_code == 422
    assert caught.value.detail == {
        "code": "user_song_window_out_of_range",
        "reason": "That edit runs past the end of your song.",
    }
    assert device_status(job, "guided_story").request == before


def test_a_lipsync_extension_inside_the_song_is_refused_by_the_resync_instead():
    job, _result = lipsync_job()  # a 120 s song: the +3 s stays inside it
    before = device_status(job, "guided_story").request
    with pytest.raises(HTTPException) as caught:
        _set_total(job, _video_length(job) + 3)
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "unsupported_phone_edit"
    assert "runs past its end" in caught.value.detail["reason"]
    assert device_status(job, "guided_story").request == before


def test_removing_a_lipsync_song_ignores_a_moved_start_and_volume():
    job, result = lipsync_job()
    _song_save(
        job,
        removed=True,
        window_start_s=result.user_song.window_start_s + 9.0,
        volume=0.2,
    )
    recipe = _recipe(job)
    assert not any(t.id == "song" for t in recipe.tracks)
    assert recipe.audio.original_volume > 0
    assert _projection(job) is None


def test_a_user_song_section_on_a_non_guided_variant_is_unavailable_up_front():
    from tests.routes.test_generative_jobs import _resign_job

    job = _resign_job()
    payload = gj.EditorCommitRequest(
        base_generation="first", user_song=gj.EditorCommitUserSong(volume=0.5)
    )
    with pytest.raises(HTTPException) as caught:
        gj.require_guided_story_editor_commit(job, "song_lyrics", payload)
    assert caught.value.status_code == 422
    assert caught.value.detail == {"code": "user_song_unavailable"}


def test_a_lipsync_start_is_locked_but_its_volume_is_not():
    job, result = lipsync_job()
    before = device_status(job, "guided_story").request
    with pytest.raises(HTTPException) as caught:
        _song_save(job, window_start_s=result.user_song.window_start_s + 3.0)
    assert caught.value.status_code == 422
    assert caught.value.detail["code"] == "user_song_lipsync_locked"
    assert device_status(job, "guided_story").request == before
    # Echoing the unchanged start is not a move.
    _song_save(job, window_start_s=result.user_song.window_start_s, volume=0.6)
    clip = _song_clip(_recipe(job))
    assert clip.volume == pytest.approx(0.6)
    assert clip.source_start == pytest.approx(result.user_song.window_start_s)
    assert _take_offsets_ok(job, result) == 2


@pytest.mark.parametrize("make_job", [background_job, lipsync_job])
def test_removing_the_song_brings_back_camera_audio(make_job):
    job, _result = make_job()
    assert any(t.id == "song" for t in _recipe(job).tracks)
    _song_save(job, removed=True)
    recipe = _recipe(job)
    assert not any(t.id == "song" for t in recipe.tracks)
    assert recipe.audio.music_asset_id is None
    assert recipe.audio.original_volume > 0
    assert "musicBed" not in recipe.required_capabilities
    assert "audioMix" in recipe.required_capabilities
    variant = job.assembly_plan["variants"][0]
    assert "user_song" not in variant[PHONE_EDITOR_SAVED_PLAN_FIELD]
    assert _projection(job) is None
    assert variant["source_audio_preserved"] is True
    # Later Saves stay song-free, and the song controls are gone.
    _text_save(job)
    assert not any(t.id == "song" for t in _recipe(job).tracks)
    assert _recipe(job).audio.original_volume > 0
    assert "user_song" not in gj._editor_capabilities(job, variant)
    with pytest.raises(HTTPException) as caught:
        _song_save(job, volume=0.5)
    assert caught.value.detail["code"] == "user_song_unavailable"


def test_the_song_capabilities_lock_the_start_for_lipsync_only():
    job, _result = background_job()
    caps = gj._editor_capabilities(job, job.assembly_plan["variants"][0])["user_song"]
    assert caps == {
        "volume": {"editable": True, "reason": None},
        "window": {"editable": True, "reason": None},
        "remove": {"editable": True, "reason": None},
    }
    job, _result = lipsync_job()
    caps = gj._editor_capabilities(job, job.assembly_plan["variants"][0])["user_song"]
    assert caps["volume"]["editable"] is True
    assert caps["remove"]["editable"] is True
    assert caps["window"] == {"editable": False, "reason": "user_song_lipsync_locked"}


def test_a_projection_carries_the_volume_and_defaults_to_full():
    job, _result = background_job()
    assert _projection(job)["volume"] == 1.0
    _song_save(job, volume=0.25)
    assert _projection(job)["volume"] == 0.25


# ── Original (camera) audio: one level for the video, and a per-clip switch ───────────
#
# A song video silences the camera by default. The creator can now ask to hear it (to debug
# a take) through `mix.original_level` and per-slot `muted`; the song keeps its own level and
# a lip-sync take's pinned song offset must not move.


def _mix_save(job, **mix):
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    payload = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        guided_revision_number=revision["revision_number"],
        mix=gj.EditorCommitMix(**mix),
    )
    gj.require_guided_story_editor_commit(job, "guided_story", payload)
    return gj.prepare_editor_commit(job, "guided_story", payload)


def _slots_save(job, muted_indexes=(), unmute_indexes=()):
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    segment_layout, _ = gj._guided_v2_layouts(job, variant)
    rows = gj._guided_v2_slot_rows(revision, segment_layout)
    slots = []
    for index, row in enumerate(rows):
        slot = gj.TimelineSlotEdit(
            slot_id=row["slot_id"],
            clip_index=row["clip_index"],
            in_s=row["in_s"],
            duration_s=row["duration_s"],
            transition_after=row["transition_after"],
            transition_duration_s=row["transition_duration_s"],
            **({"muted": True} if index in muted_indexes else {}),
            **({"muted": False} if index in unmute_indexes else {}),
        )
        slots.append(slot)
    payload = gj.EditorCommitRequest(
        base_generation=gj.variant_render_baseline(variant),
        guided_revision_number=revision["revision_number"],
        timeline_slots=slots,
    )
    gj.require_guided_story_editor_commit(job, "guided_story", payload)
    return gj.prepare_editor_commit(job, "guided_story", payload)


def _video_clips(recipe):
    return next(t for t in recipe.tracks if t.kind == "video").clips


@pytest.mark.parametrize("make_job", [background_job, lipsync_job])
def test_a_song_video_keeps_its_camera_silent_until_the_creator_asks(make_job):
    job, _result = make_job()
    _text_save(job)
    recipe = _recipe(job)
    assert recipe.audio.original_volume == 0.0
    assert all(clip.volume == 1 for clip in _video_clips(recipe))


@pytest.mark.parametrize("make_job", [background_job, lipsync_job])
def test_original_level_plays_the_camera_with_the_song_at_each_own_level(make_job):
    from app.services.creator_render_contract import (
        CreatorRenderContract,
        doubled_soundtrack_assets,
        verify_phone_recipe,
    )

    job, result = make_job()
    _song_save(job, volume=0.4)
    _mix_save(job, original_level=0.6)
    recipe = _recipe(job)
    assert recipe.audio.original_volume == pytest.approx(0.6)
    assert _song_clip(recipe).volume == pytest.approx(0.4)
    assert "audioMix" in recipe.required_capabilities
    # The song lane stays single-play and the recipe passes the same contract check as a save.
    assert doubled_soundtrack_assets(recipe) == []
    assert verify_phone_recipe(CreatorRenderContract(generation_id="g"), recipe)
    if result.user_song.mode == "lipsync":
        assert _take_offsets_ok(job, result) > 0
    # A later save of something else keeps both levels.
    _text_save(job)
    recipe = _recipe(job)
    assert recipe.audio.original_volume == pytest.approx(0.6)
    assert _song_clip(recipe).volume == pytest.approx(0.4)
    # Back to zero restores the silent default.
    _mix_save(job, original_level=0.0)
    assert _recipe(job).audio.original_volume == 0.0


def test_a_music_level_is_still_refused_on_a_reference_only_song_variant():
    job, _result = background_job()
    with pytest.raises(HTTPException) as caught:
        _mix_save(job, music_level=0.5)
    assert caught.value.detail == "song_added_when_posting"


def test_the_creators_original_level_is_not_a_cloud_render_feature():
    job, _result = background_job()
    job.assembly_plan["variants"][0]["render_destination"] = "cloud"
    with pytest.raises(HTTPException) as caught:
        _mix_save(job, original_level=0.5)
    assert caught.value.detail == "original_audio_phone_only"
    caps = gj._editor_capabilities(job, job.assembly_plan["variants"][0])
    assert caps["original_audio"] == {"editable": False, "reason": "original_audio_phone_only"}
    assert caps["clips"]["audio"]["editable"] is False


def test_a_phone_variant_advertises_the_original_audio_controls():
    job, _result = lipsync_job()
    caps = gj._editor_capabilities(job, job.assembly_plan["variants"][0])
    assert caps["original_audio"] == {"editable": True, "reason": None}
    assert caps["clips"]["audio"] == {"editable": True, "reason": None}


def test_muting_one_lipsync_clip_silences_only_that_clip_and_keeps_every_take_in_sync():
    job, result = lipsync_job()
    _mix_save(job, original_level=1.0)
    _slots_save(job, muted_indexes={0})
    clips = _video_clips(_recipe(job))
    assert clips[0].volume == 0
    assert all(clip.volume == 1 for clip in clips[1:])
    assert _take_offsets_ok(job, result) > 0
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    assert gj._guided_v2_slot_rows(revision, lambda _s: "fullscreen")[0]["muted"] is True
    # The mute survives an unrelated save, and an explicit false clears it.
    _text_save(job)
    assert _video_clips(_recipe(job))[0].volume == 0
    _slots_save(job, unmute_indexes={0})
    assert all(clip.volume == 1 for clip in _video_clips(_recipe(job)))
