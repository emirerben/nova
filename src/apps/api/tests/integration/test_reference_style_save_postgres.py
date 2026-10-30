"""Manual reference style -> chat apply -> local Undo -> durable native Save.

The planner answer is a fixture; runtime, receipt binding, the Save transaction,
reopen and device recipe compilation are real. No provider or media download.
"""

import asyncio
import copy
import json
import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.device_render import make_device_request
from app.models import (
    CreationThreadEvent,
    CreativeBriefVersion,
    CreatorAgentSession,
    CreatorEditDraft,
    Job,
)
from app.routes import generative_jobs as gj
from app.routes.plan_items import _editor_commit_item
from app.services.device_render import device_status, pin_device_request
from app.services.editor_empty_drafts import attach_saved_editor_drafts, saved_editor_draft
from app.tasks.kria_runtime import run_kria_turn
from tests.kria.test_editor_state_turns_postgres import _plan_with, _seed_job, _submit
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project
from tests.routes.test_authored_empty_caption_metadata import META
from tests.routes.test_authored_empty_caption_metadata import project as phone_project


@pytest.mark.asyncio
@pytest.mark.parametrize("local_undo", [False, True])
async def test_reference_style_feedback_then_save_reopens_exact_selected_document(
    monkeypatch, local_undo
):
    def fixture_metadata(path):
        assert path == "user/analysis-proxy-source.mp4"
        return SimpleNamespace(generation="123")

    monkeypatch.setattr("app.routes.editor_sources.storage.object_metadata", fixture_metadata)
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "kria_editor_state_turns_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_guided_timeline_ops", True)
    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = _seed_job(user_id, session_id)
    seed, _ = phone_project(monkeypatch)
    variant = gj._find_variant(seed, "guided_story")
    rows = [
        {
            "id": f"word-{i}",
            "text": text,
            "start_s": float(i),
            "end_s": float(i + 1),
            "role": "generative_intro",
            "position": "middle",
            "font_family": "Montserrat",
            "size_px": 64,
        }
        for i, text in enumerate(["Our", "Lisbon", "trip"])
    ]
    variant["text_elements"] = copy.deepcopy(rows)
    revision = gj._guided_v2_revision(seed, variant)
    revision.update(text_elements=copy.deepcopy(rows), state_hash="")
    variant["guided_edit_revision"] = gj.normalize_guided_editor_revision(revision)
    variant.update(render_status="ready", video_path="fixture/last-good.mp4")
    previous_recipe = device_status(seed, "guided_story").request.recipe
    seed.id, seed.user_id = job_id, user_id
    pin_device_request(
        seed,
        make_device_request(
            job_id=job_id, variant_id="guided_story", revision=3, recipe=previous_recipe
        ),
        base_generation="first",
    )
    with sync_session() as db:
        stored = db.get(Job, job_id)
        stored.assembly_plan = copy.deepcopy(seed.assembly_plan)
        stored.status, stored.current_phase = "variants_ready", None
        stored.all_candidates = {"clip_paths": ["user/analysis-proxy-source.mp4"]}
        session = db.get(CreatorAgentSession, session_id)
        session.target_variant_id, session.target_generation_id = "guided_story", "first"
        db.commit()
        item_id = stored.content_plan_item_id
    manual = copy.deepcopy(rows)
    manual[1].update(font_family="Hanken Grotesk", size_px=84)
    ids = [row["id"] for row in rows]
    _plan_with(
        monkeypatch,
        [
            {
                "op": "patch_text",
                "selector": {"ids": ids},
                "target_ids": ids,
                "expected_count": 3,
                "patch": {"font_family": "Hanken Grotesk", "size_px": 84},
            }
        ],
    )
    accepted = await _submit(
        user_id,
        thread_id,
        "Use the font and size of the Lisbon text for all the other text blocks",
        {
            "base_generation": "first",
            "client_state_id": "manual-reference",
            "lanes": {"text_elements": manual, "caption_meta": META},
        },
    )
    with sync_session() as db:
        db.add(
            CreativeBriefVersion(
                thread_id=thread_id,
                version=1,
                source_turn_id=uuid.UUID(accepted.turn_id),
                requirements=[
                    {
                        "id": "r1",
                        "kind": "style",
                        "scope": "global",
                        "status": "open",
                        "facts": {},
                        "description": (
                            "Use the font and size of the Lisbon text for all the other text blocks"
                        ),
                        "source_turn_id": accepted.turn_id,
                    }
                ],
            )
        )
        db.commit()
    try:
        assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
            "status"
        ] == "completed"
        with sync_session() as db:
            event = db.scalar(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "draft_applied",
                )
                .order_by(CreationThreadEvent.sequence.desc())
            )
            assert event is not None
            assert "couldn't" not in (event.payload.get("reply_lead") or "").lower()
            assert "done" not in (event.payload.get("reply_lead") or "").lower()
            assert "Couldn't:" not in event.content
            head = db.scalar(
                select(CreatorEditDraft).where(
                    CreatorEditDraft.thread_id == thread_id, CreatorEditDraft.is_head.is_(True)
                )
            )
            applied = head.snapshot_json["editor_payload"]["text_elements"]
            assert all(
                row["font_family"] == "Hanken Grotesk" and row["size_px"] == 84 for row in applied
            )
            for before, after in zip(manual, applied, strict=True):
                for key in ("id", "text", "start_s", "end_s"):
                    assert after[key] == before[key]
        selected = manual if local_undo else applied
        async with AsyncSessionLocal() as db:
            response = await _editor_commit_item(
                str(item_id),
                "guided_story",
                gj.EditorCommitRequest(
                    base_generation="first",
                    editor_state_version=2,
                    text_elements=copy.deepcopy(selected),
                    caption_meta=META,
                ),
                SimpleNamespace(id=user_id),
                db,
                native_client=True,
            )
            assert response.ok and response.sections.caption_meta
            db.expire_all()
            saved = await db.get(Job, job_id)
            await attach_saved_editor_drafts(db, saved)
            draft = saved_editor_draft(saved, "guided_story")
            sections = draft.snapshot["editor_payload"]["sections"]
            for expected, actual in zip(selected, sections["text_elements"], strict=True):
                for key in ("id", "text", "start_s", "end_s", "font_family", "size_px"):
                    assert actual[key] == expected[key]
            assert sections["caption_meta"] == META
            recipe = device_status(saved, "guided_story").request.recipe
            assert len(recipe.text_layers) == 3
            assert [(row.start, row.end) for row in recipe.text_layers] == [(0, 1), (1, 2), (2, 3)]
            if output_dir := os.environ.get("KRIA_REFERENCE_STYLE_FIXTURE_DIR"):
                output = Path(output_dir)
                output.mkdir(parents=True, exist_ok=True)
                name = "localUndo" if local_undo else "styledAll"
                (output / f"{name}.json").write_text(
                    json.dumps({"name": name, "recipe": recipe.model_dump(mode="json")}, indent=2)
                    + "\n"
                )
    finally:
        await async_engine.dispose()
