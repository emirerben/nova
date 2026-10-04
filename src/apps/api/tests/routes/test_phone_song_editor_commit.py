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
from app.pipeline.guided_story import GuidedStoryExecutionPlan, compile_execution_plan
from app.pipeline.lipsync_montage import lipsync_sync_error_s, plan_lipsync_montage
from app.pipeline.phone_guided_plan import UnsupportedPhonePlan, compile_phone_guided_plan
from app.pipeline.unified_montage import plan_unified_montage
from app.routes import generative_jobs as gj
from app.services.device_render import device_status, pin_device_request
from app.services.kria_editor_ops import compile_editor_ops
from app.services.phone_editor import (
    PHONE_EDITOR_PLAN_FIELD,
    PHONE_EDITOR_SAVED_PLAN_FIELD,
    prepare_phone_editor_commit,
)
from app.services.phone_sources import PHONE_SOURCES_FIELD
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


def _job(result):
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
                song=song_bed(),
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
