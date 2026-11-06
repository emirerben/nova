"""Real database snapshots keep editable panels across manual and AI saves."""

import copy

import pytest
from sqlalchemy import select

from app.agents.edit_copilot import _ParseState, parse_editor_operation
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.models import CreatorAgentSession, Job, PlanItem
from app.routes import generative_jobs as gj
from app.services.device_render import device_status
from app.services.editor_empty_drafts import (
    attach_saved_editor_drafts,
    public_saved_editor_variant,
    stage_prepared_editor_draft,
    stage_saved_draft_baseline,
)
from app.services.kria_editor_ops import build_editor_snapshot, compile_editor_ops
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.routes.test_authored_playback_speed import _authored_phone_job


@pytest.mark.asyncio
async def test_manual_ai_manual_saves_reopen_independent_panels(monkeypatch):
    user_id, _, session_id = _seed_runtime_project()
    fixture, _ = _authored_phone_job(monkeypatch)
    monkeypatch.setattr(
        settings,
        "phone_render_verified_features",
        [*settings.phone_render_verified_features, "splitScreen", "sourceCrop", "clipTilt"],
    )
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id)
        item = db.get(PlanItem, session.plan_item_id)
        item.edit_format = "montage"
        job = Job(
            user_id=user_id,
            status="variants_ready",
            mode="generative",
            raw_storage_path="",
            selected_platforms=["tiktok"],
            content_plan_item_id=item.id,
            content_plan_ownership_epoch=0,
            all_candidates=copy.deepcopy(fixture.all_candidates),
            assembly_plan=copy.deepcopy(fixture.assembly_plan),
        )
        db.add(job)
        db.flush()
        item.current_job_id = job.id
        job_id, item_id = job.id, item.id
        db.commit()

    async def save(body):
        async with AsyncSessionLocal() as db:
            job = await db.get(Job, job_id)
            item = await db.get(PlanItem, item_id)
            await attach_saved_editor_drafts(db, job)
            staged = (
                stage_saved_draft_baseline(job, "original_text", body)
                if job.assembly_plan["variants"][0].get("editor_state_version") == 2
                else job
            )
            gj.prepare_editor_commit(staged, "original_text", body)
            job.assembly_plan = copy.deepcopy(staged.assembly_plan)
            draft = await stage_prepared_editor_draft(
                db, item=item, job=job, variant_id="original_text", creator_id=user_id, body=body
            )
            await db.commit()
            return draft.draft_id

    async def reopen():
        async with AsyncSessionLocal() as db:
            job = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
            await attach_saved_editor_drafts(db, job)
            variant = public_saved_editor_variant(job, job.assembly_plan["variants"][0])
            return job, variant

    payload = gj.EditorCommitRequest.model_validate(
        {
            "base_generation": "first",
            "editor_state_version": 2,
            "timeline_slots": [
                {
                    "slot_id": "a632f012-c767-4482-90ed-14775165a895",
                    "clip_index": 1,
                    "in_s": 2,
                    "duration_s": 3,
                    "source_end_s": 8,
                    "playback_rate": 2,
                    "source_audio_volume": 0.5,
                    "media_transform": {
                        "scale": 1.1,
                        "rotation_degrees": 7,
                        "position_x": 12,
                        "position_y": -3,
                    },
                    "split_section_id": "s",
                    "split_panel_index": 0,
                    "split_offset_s": 0,
                    "source_crop": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.8},
                },
                {
                    "slot_id": "cad274b0-413c-44e3-b795-2df74b1d2372",
                    "clip_index": 1,
                    "in_s": 7,
                    "duration_s": 2,
                    "muted": True,
                    "split_section_id": "s",
                    "split_panel_index": 1,
                    "split_offset_s": 1,
                },
            ],
            "split_screen_sections": [{"id": "s", "duration_s": 4, "panel_count": 2}],
        }
    )
    first_id = await save(payload)
    job, variant = await reopen()
    assert variant["user_timeline"]["split_screen_sections"][0]["duration_s"] == 4
    snapshot = build_editor_snapshot(job, variant)
    snapshot["editor_ops_version"] = 2
    operation = parse_editor_operation(
        {"op": "update_split_screen", "section_id": "s", "gutter": 0.04, "panel_weights": [2, 1]},
        snapshot,
        _ParseState(0.99),
    )
    assert operation is not None
    ai_payload = compile_editor_ops(job, variant, [operation]).payload
    ai_payload.base_generation = variant["render_generation_id"]
    ai_payload.editor_state_version = 2
    second_id = await save(ai_payload)
    assert first_id != second_id

    job, variant = await reopen()
    slots = variant["user_timeline"]["slots"]
    assert [(row["in_s"], row["split_offset_s"]) for row in slots] == [(2, 0), (7, 1)]
    assert [row["slot_id"] for row in slots] == [
        "a632f012-c767-4482-90ed-14775165a895",
        "cad274b0-413c-44e3-b795-2df74b1d2372",
    ]
    assert slots[0]["playback_rate"] == 2
    assert slots[0]["source_audio_volume"] == 0.5
    assert slots[0]["media_transform"]["position_x"] == 12
    assert slots[1]["muted"] is True
    sections = variant["user_timeline"]["split_screen_sections"]
    assert sections[0]["panel_weights"] == [2, 1]
    assert sections[0]["gutter"] == 0.04

    third_id = await save(
        gj.EditorCommitRequest(
            base_generation=variant["render_generation_id"],
            editor_state_version=2,
            text_elements=[
                {"id": "manual-title", "text": "Manual title", "start_s": 0, "end_s": 4}
            ],
        )
    )
    assert third_id != second_id
    job, variant = await reopen()
    assert variant["user_timeline"]["split_screen_sections"] == sections
    assert variant["text_elements"][0]["text"] == "Manual title"
    recipe = device_status(job, "original_text").request.recipe
    members = [clip for track in recipe.tracks for clip in track.clips if clip.split_screen]
    assert recipe.duration == 4
    assert [
        (clip.source_asset_id, clip.source_start, clip.timeline_start, clip.rate, clip.volume)
        for clip in members
    ] == [("new", 2, 0, 2, 0.5), ("new", 7, 1, 1, 0)]
    assert members[0].transform.position_x == 12
    assert members[0].transform.position_y == -3
    assert members[0].transform.scale == 1.1
