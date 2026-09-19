"""Runtime-1 chat admission through the real media-only Save compiler."""

import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock, patch

import pytest

from app.config import settings
from app.routes import creation_threads as routes
from app.routes import generative_jobs as gj
from app.routes import plan_items
from app.services import creation_editor_actions as actions
from app.services.kria_editor_ops import build_editor_snapshot
from tests.routes.test_creation_threads import _request
from tests.routes.test_editor_commit import REGEN, _arm, _job, _narrated_guided_job


@pytest.mark.asyncio
async def test_chat_removal_saves_real_guided_media_only_before_queue(monkeypatch):
    _arm(monkeypatch)
    monkeypatch.setattr(settings, "guided_story_editor_v2_enabled", True)
    monkeypatch.setattr(settings, "visual_blocks_enabled", True)
    job = _narrated_guided_job()
    variant = job.assembly_plan["variants"][0]
    revision = gj._guided_v2_revision(job, variant)
    media = {
        "id": "uploaded-photo",
        "kind": "media",
        "asset_id": "uploaded-asset",
        "src_gcs_path": "users/test/photo.jpg",
        "media_kind": "image",
        "start_s": 0.0,
        "end_s": 2.0,
        "origin": "user",
    }
    revision["visual_blocks"] = [media]
    revision["state_hash"] = ""
    variant["guided_edit_revision"] = revision
    variant["visual_blocks"] = [copy.deepcopy(media)]
    before = copy.deepcopy(gj._guided_v2_revision(job, variant))
    before_generation = gj.variant_render_baseline(variant)
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        revision=4,
        status="active",
        runtime_version=1,
        active_job_id=job.id,
        active_plan_item_id=uuid.uuid4(),
        state={"selected_variant_id": "song_text"},
    )
    db = SimpleNamespace(
        rollback=AsyncMock(),
        commit=AsyncMock(),
        get=AsyncMock(side_effect=[user, SimpleNamespace(current_job_id=job.id), job]),
        execute=AsyncMock(),
    )
    db.execute.return_value = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    monkeypatch.setattr(routes, "_load", AsyncMock(return_value=thread))
    monkeypatch.setattr(routes, "_duplicate", AsyncMock(return_value=None))
    monkeypatch.setattr(
        routes,
        "_load_authorized_projection_rows",
        AsyncMock(return_value=(SimpleNamespace(id=thread.active_plan_item_id), None, job, None)),
    )
    monkeypatch.setattr(plan_items, "_proposal_media_is_current", AsyncMock(return_value=True))
    events = []

    async def append(_db, _thread, **event):
        events.append(copy.deepcopy(event))

    monkeypatch.setattr(routes, "_append", append)
    monkeypatch.setattr(routes, "_response", AsyncMock(return_value=thread))
    planner = AsyncMock()
    monkeypatch.setattr(routes, "_agent_message", planner)

    async def copilot(body, **kwargs):
        assert body.snapshot["allowed_op_families"] == ["visual_media"]
        assert [row["id"] for row in body.snapshot["visual_media"]] == ["uploaded-photo"]
        db.rollback.assert_awaited_once()
        return SimpleNamespace(
            ops=[{"op": "remove_visual_media", "target_ids": ["uploaded-photo"]}],
            outcome="applied",
            reply="Prepared removal",
        )

    monkeypatch.setattr(actions, "run_copilot_turn", copilot)
    committed_plans = []

    async def commit():
        committed_plans.append(copy.deepcopy(job.assembly_plan))

    db.commit.side_effect = commit

    def enqueue(job_id, variant_id, prep):
        db.commit.assert_awaited_once()
        assert job_id == str(job.id)
        assert variant_id == "song_text"
        assert events[-1]["event_type"] == "assistant_response"
        assert events[-1]["payload"]["outcome"] == "saved"
        assert events[-1]["payload"]["generation"] == prep["generation"]
        assert committed_plans[0]["variants"][0]["guided_edit_revision"]["visual_blocks"] == []

    queue = Mock(side_effect=enqueue)
    monkeypatch.setattr(actions, "enqueue_editor_commit_render", queue)
    result = await routes.message_thread(
        _request(),
        str(thread.id),
        routes.MessageBody(
            message="Remove the visual overlays I added",
            client_event_id="remove-visual",
            expected_revision=4,
        ),
        user,
        db,
    )
    assert result is thread
    planner.assert_not_awaited()
    queue.assert_called_once()
    assert [event["event_type"] for event in events] == ["user_message", "assistant_response"]
    saved_variant = committed_plans[0]["variants"][0]
    saved = saved_variant["guided_edit_revision"]
    assert saved["visual_blocks"] == []
    assert not saved_variant["visual_blocks"]
    assert saved["revision_number"] == before["revision_number"] + 1
    assert gj.variant_render_baseline(saved_variant) != before_generation
    assert saved_variant["render_generation_id"] == events[-1]["payload"]["generation"]
    for lane in (
        "text_elements",
        "segments",
        "audio",
        "sources",
        "sound_effects",
        "media_overlays",
    ):
        assert json.dumps(saved[lane], sort_keys=True) == json.dumps(before[lane], sort_keys=True)
    authority = build_editor_snapshot(job, job.assembly_plan["variants"][0])
    assert "visual_media" not in authority["allowed_op_families"]
    assert authority["base_generation"] == saved_variant["render_generation_id"]


def _beat_text_job():
    """A ready cloud (non-guided) montage variant: one title bar + three
    per-beat caption bars, matching the 2026-09-19 incident's shape."""
    return _job(
        text_elements=[
            {
                "id": "title-1",
                "text": "My Title",
                "start_s": 0.0,
                "end_s": 2.0,
                "role": "generative_intro",
                "position": "middle",
            },
            *[
                {
                    "id": f"beat-{i}",
                    "text": f"Friends compete in an energetic game of football {i}",
                    "start_s": 2.0 + i,
                    "end_s": 3.0 + i,
                    "role": "generative_sequence",
                    "position": "middle",
                }
                for i in range(3)
            ],
        ]
    )


@pytest.mark.asyncio
async def test_chat_edit_removes_non_title_text_and_keeps_title(monkeypatch):
    """2026-09-19 incident: 'remove the random texts that aren't the title' on
    an already-rendered cloud cut applies in place through the real
    compile_editor_ops + prepare_editor_commit path and kicks the same
    fast-reburn task PUT /text-elements uses -- never the Main Creator."""

    _arm(monkeypatch)
    job = _beat_text_job()
    variant = job.assembly_plan["variants"][0]
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        active_plan_item_id=uuid.uuid4(),
        state={"selected_variant_id": "song_text"},
    )
    body = SimpleNamespace(
        message="remove the random texts that aren't the title",
        client_event_id="remove-text-1",
    )
    db = SimpleNamespace(get=AsyncMock(return_value=job), execute=AsyncMock())
    db.execute.return_value = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = []
    events = []

    async def append(_db, _thread, **event):
        events.append(copy.deepcopy(event))

    monkeypatch.setattr(routes, "_append", append)
    # Highest bar_index first: `remove_text` pops in place, so removing
    # low-to-high would shift later indices out from under the next op.
    ops = [
        {"op": "remove_text", "bar_index": 3},
        {"op": "remove_text", "bar_index": 2},
        {"op": "remove_text", "bar_index": 1},
    ]
    response = SimpleNamespace(
        ops=ops,
        outcome="proposed",
        reply="I prepared this edit for the editor to validate and stage.",
    )

    async def copilot(body_arg, **kwargs):
        assert body_arg.snapshot["allowed_op_families"]
        assert kwargs["job_id"] == job.id
        return response

    monkeypatch.setattr(actions, "run_copilot_turn", copilot)

    result = await actions.execute_copilot_edit(db, thread, body, user, job=job)

    assert result is not None
    assert result["thread"] is thread
    assert result["job_id"] == job.id
    assert result["variant_id"] == "song_text"
    saved_variant = job.assembly_plan["variants"][0]
    assert [row["id"] for row in saved_variant["text_elements"]] == ["title-1"]
    assert saved_variant["text_elements"][0]["text"] == "My Title"
    assert [event["event_type"] for event in events] == ["assistant_response"]
    assert events[-1]["payload"]["outcome"] == "saved"
    assert events[-1]["payload"]["sections"]["text_elements"] is True
    assert gj.variant_render_baseline(saved_variant) != gj.variant_render_baseline(variant)

    with patch(REGEN) as regen:
        regen.apply_async = MagicMock()
        await actions.finalize_copilot_edit_render(db, thread, user, result)
    regen.apply_async.assert_called_once()
    assert regen.apply_async.call_args.kwargs["args"] == [str(job.id), "song_text"]
    assert regen.apply_async.call_args.kwargs["queue"] == "overlay-jobs"


@pytest.mark.asyncio
async def test_copilot_clarification_reports_without_applying_or_replanning(monkeypatch):
    _arm(monkeypatch)
    job = _beat_text_job()
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        active_plan_item_id=uuid.uuid4(),
        state={"selected_variant_id": "song_text"},
    )
    body = SimpleNamespace(message="remove that", client_event_id="clarify-1")
    db = SimpleNamespace(get=AsyncMock(return_value=job), execute=AsyncMock())
    events = []

    async def append(_db, _thread, **event):
        events.append(copy.deepcopy(event))

    monkeypatch.setattr(routes, "_append", append)
    response = SimpleNamespace(ops=[], outcome="clarification", reply="Which text should I remove?")
    monkeypatch.setattr(actions, "run_copilot_turn", AsyncMock(return_value=response))

    result = await actions.execute_copilot_edit(db, thread, body, user, job=job)

    assert result == {"thread": thread}
    before = copy.deepcopy(job.assembly_plan)
    assert job.assembly_plan == before  # nothing mutated
    assert events[-1]["content"] == "Which text should I remove?"
    assert events[-1]["payload"]["outcome"] == "clarification"


@pytest.mark.asyncio
async def test_copilot_unsupported_falls_through_without_side_effects(monkeypatch):
    """The only signal that hands a ready-job message back to the Main
    Creator: no chat event, no mutation, nothing to enqueue."""

    _arm(monkeypatch)
    job = _beat_text_job()
    user = SimpleNamespace(id=uuid.uuid4())
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        active_plan_item_id=uuid.uuid4(),
        state={"selected_variant_id": "song_text"},
    )
    body = SimpleNamespace(
        message="make this a completely different, longer story", client_event_id="oos-1"
    )
    db = SimpleNamespace(get=AsyncMock(return_value=job), execute=AsyncMock())
    append = AsyncMock()
    monkeypatch.setattr(routes, "_append", append)
    response = SimpleNamespace(
        ops=[], outcome="unsupported", reply="That kind of edit isn't available for this draft."
    )
    monkeypatch.setattr(actions, "run_copilot_turn", AsyncMock(return_value=response))

    result = await actions.execute_copilot_edit(db, thread, body, user, job=job)

    assert result is None
    append.assert_not_awaited()
