"""Real-Postgres proof of the editor draft head rule (``server-failure-modes.md``).

The server never moves a head because a generation moved. A head on an old generation is not
current: ``head_current`` says so, readers ignore it, and the phone converges by itself. The one
server write is replacing a stranded pinned v2 saved draft at Save time. Heads are built through
the real draft-turn path; generations are moved by the real writers (approval claim,
``_complete_device_export``, the editor-commit route, the speech-cut publication).
Assertions are rows and returned documents.
"""

from __future__ import annotations

import asyncio
import copy
import json
import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import select

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.drafts import read_or_bootstrap_draft, undo_draft, write_draft
from app.kria.editor_head import is_head_current
from app.kria.planner import PlannedKriaTurn, adapt_editor_action
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentSession,
    CreatorEditDraft,
    Job,
    PlanItem,
    TemporaryMediaUpload,
)
from app.services.editor_empty_drafts import editor_sections
from app.tasks.kria_runtime import execute_kria_approval, run_kria_turn
from tests.kria.test_editor_state_turns_postgres import _plan_with, _submit
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

VARIANT = "original_text"
HOOK = {
    "id": "hook",
    "text": "Old matcha hook",
    "start_s": 0.0,
    "end_s": 2.0,
    "role": "generative_intro",
    "font_family": "Playfair Display",
    "size_px": 72,
    "color": "#FFFFFF",
    "effect": "static",
    "alignment": "center",
    "position": "middle",
}
A, B = "AI hook", "Creator Save"
EDIT_HOOK = [{"op": "edit_text", "bar_index": 0, "text": "Fresh matcha, finally"}]
V2_EXTRAS = {
    "caption_meta": {"style": "bold", "y_frac": 0.7},
    "caption_text_color": "#FFFFFF",
    "editor_split_screen_origin": "ai",
    "voiceover_caption_style": "karaoke",
    "voiceover_bed_level": 0.4,
    "background_music_treatment": "duck",
}


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.routes.test_editor_commit import _arm

    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(
        "app.tasks.kria_runtime.enqueue_editor_commit_render", lambda *_a, **_k: None
    )
    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", lambda *a, **k: None)
    _arm(monkeypatch)


# --- fixtures and readers -----------------------------------------------------------------


def _timeline() -> dict:
    return {
        "beat_grid": [],
        "slots": [
            {
                "slot_id": f"s{i}",
                "clip_index": i,
                "source_gcs_path": f"users/u/{i}.mp4",
                "source_duration_s": 8.0,
                "in_s": 0.0,
                "duration_beats": None,
                "duration_s": 4.0,
                "removed": False,
                "transition_after": "cut",
            }
            for i in range(2)
        ],
    }


def _variant(
    generation: str | None = "generation-1", text: str = "Old matcha hook", **extra
) -> dict:  # noqa: ANN003
    variant = {
        "variant_id": VARIANT,
        "resolved_archetype": "montage",
        "render_status": "ready",
        "render_finished_at": "2026-09-07T08:00:00Z",
        "video_path": "generative-jobs/test/output.mp4",
        "base_video_path": "generative-jobs/test/base.mp4",
        "text_elements": [{**HOOK, "text": text}],
        "ai_timeline": _timeline(),
        "editor_capabilities": {"mix": True},
        "mix": 0.5,
        **extra,
    }
    if generation is not None:
        variant["render_generation_id"] = generation
    return variant


def _add_job(item_id, user_id, variant: dict, *, current: bool = True) -> uuid.UUID:  # noqa: ANN001
    job_id = uuid.uuid4()
    with sync_session() as db:
        item = db.get(PlanItem, item_id, with_for_update=True)
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="variants_ready",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item_id,
                content_plan_ownership_epoch=0,
                all_candidates={"clip_paths": ["users/u/0.mp4", "users/u/1.mp4"]},
                assembly_plan={"variants": [variant]},
            )
        )
        db.flush()
        if current:
            item.current_job_id = job_id
        db.commit()
    return job_id


def _seed(variant: dict | None = None):  # noqa: ANN202
    """Project, current Job and a session pointing at the variant (as an editor turn expects)."""
    user_id, thread_id, session_id = _seed_runtime_project()
    with sync_session() as db:
        item_id = db.get(CreationThread, thread_id).active_plan_item_id
    variant = variant or _variant()
    job_id = _add_job(item_id, user_id, variant)
    with sync_session() as db:
        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        session.target_job_id = job_id
        session.target_variant_id = VARIANT
        session.target_generation_id = variant.get("render_generation_id")
        session.manifest_hash = "a" * 64
        db.commit()
    return user_id, thread_id, item_id, job_id


def _set_variant(job_id, **changes) -> None:  # noqa: ANN001, ANN003
    """Move the variant the way a render/dispatcher does: replace the JSON dict, never mutate."""
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        plan = copy.deepcopy(job.assembly_plan)
        for key, value in changes.items():
            if value is None:
                plan["variants"][0].pop(key, None)
            else:
                plan["variants"][0][key] = value
        job.assembly_plan = plan
        db.commit()


def _variant_of(job_id) -> dict:  # noqa: ANN001
    with sync_session() as db:
        return copy.deepcopy(db.get(Job, job_id).assembly_plan["variants"][0])


def _rows(item_id) -> list[dict]:  # noqa: ANN001
    with sync_session() as db:
        rows = (
            db.execute(
                select(CreatorEditDraft)
                .where(CreatorEditDraft.item_id == item_id, CreatorEditDraft.variant_key == VARIANT)
                .order_by(CreatorEditDraft.draft_revision)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": row.id,
                "rev": row.draft_revision,
                "parent": row.parent_draft_id,
                "head": row.is_head,
                "job": row.base_job_id,
                "col": row.base_generation_id,
                "payload_base": ((row.snapshot_json or {}).get("editor_payload") or {}).get(
                    "base_generation"
                ),
                "snap": row.snapshot_json,
            }
            for row in rows
        ]


def _head(item_id) -> dict:  # noqa: ANN001
    heads = [row for row in _rows(item_id) if row["head"]]
    assert len(heads) == 1, "exactly one head row per (item, variant)"
    return heads[0]


def _pinned(job_id) -> str | None:  # noqa: ANN001
    return ((_variant_of(job_id).get("editor_draft")) or {}).get("draft_id")


def _pin(job_id, draft_id) -> None:  # noqa: ANN001
    _set_variant(
        job_id,
        editor_state_version=2,
        editor_timeline_mode="authored",
        editor_draft={"draft_id": str(draft_id)},
    )


def _current(item_id, job_id) -> bool:  # noqa: ANN001
    with sync_session() as db:
        job = db.get(Job, job_id)
        head = db.execute(
            select(CreatorEditDraft).where(
                CreatorEditDraft.item_id == item_id,
                CreatorEditDraft.variant_key == VARIANT,
                CreatorEditDraft.is_head.is_(True),
            )
        ).scalar_one()
        return is_head_current(job, job.assembly_plan["variants"][0], head)


def _client_for(user_id):  # noqa: ANN001, ANN202
    import httpx

    from app.auth import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, email="qa@test.local"
    )
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")


def _clear_user_override() -> None:
    from app.auth import get_current_user
    from app.main import app

    app.dependency_overrides.pop(get_current_user, None)


async def _phone_head(user_id, thread_id, text: str = "Phone hook"):  # noqa: ANN001, ANN202
    """A phone-written head: a full-``sections`` body PUT over the bootstrapped draft."""
    async with AsyncSessionLocal() as db:
        initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    payload = copy.deepcopy(initial.snapshot["editor_payload"])
    payload["editor_state_version"] = 2
    payload["sections"]["text_elements"][0]["text"] = text
    async with AsyncSessionLocal() as db:
        return await write_draft(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            expected_revision=initial.draft_revision,
            expected_etag=initial.etag,
            snapshot={**initial.snapshot, "editor_payload": payload, "changes": ["Phone edit"]},
        )


async def _chat_head(monkeypatch, user_id, thread_id, text: str = A) -> None:  # noqa: ANN001
    """An AI chat head from a real draft turn: flat lanes AND a ``sections`` object."""
    async with AsyncSessionLocal() as db:
        await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    _plan_with(
        monkeypatch,
        [
            {"op": "edit_text", "bar_index": 0, "text": text},
            {"op": "set_clip_duration", "slot_index": 0, "duration_s": 3.0},
            {"op": "set_mix", "music_level": 0.2},
        ],
    )
    accepted = await _submit(user_id, thread_id, "Retitle it")
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "completed", result


async def _approved_ai_edit(monkeypatch, user_id, thread_id, ops):  # noqa: ANN001, ANN202
    """A real editor turn that proposes ``ops`` and a creator approval of it."""
    editor_plan = adapt_editor_action(reply="I prepared that edit.", request_render=True, ops=ops)

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(plan=editor_plan, manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    with sync_session() as db:
        revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Apply the edit",
                client_event_id=f"editor-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "awaiting_approval"
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
            )
        ).scalar_one()
        fingerprint = approval_fingerprint(approval)
        thread_revision = db.get(CreationThread, thread_id).revision
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval.id,
            creator_id=user_id,
            decision="approve",
            body=ApprovalDecisionBody(
                expected_thread_revision=thread_revision,
                expected_draft_revision=approval.draft_revision,
                expected_approval_fingerprint=fingerprint,
            ),
        )
    return approval.id


def _publish_device_export(  # noqa: ANN202
    job_id,  # noqa: ANN001
    user_id,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
    *,
    base_generation: str = "generation-1",
):
    """Pin a device recipe on the real Job; return (attempt id, call that publishes it)."""
    from app.kria.device_render import make_device_request
    from app.kria.recipes import EditRecipeV1
    from app.routes import device_render as routes
    from app.services.device_render import device_record, pin_device_request, save_device_record

    recipe = EditRecipeV1(
        assets=[{"id": "a", "relative_path": "a"}],
        tracks=[
            {
                "id": "v",
                "kind": "video",
                "clips": [
                    {
                        "id": "c",
                        "source_asset_id": "a",
                        "source_start": 0,
                        "source_duration": 2,
                        "timeline_start": 0,
                        "rate": 1,
                    }
                ],
            }
        ],
    )
    request = make_device_request(job_id=job_id, variant_id=VARIANT, revision=1, recipe=recipe)
    attempt = str(uuid.uuid4())
    path = f"{user_id}/{job_id}/device/{attempt}.mp4"
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        job.assembly_plan = copy.deepcopy(job.assembly_plan)
        job.assembly_plan["variants"][0]["render_destination"] = "device"
        pin_device_request(job, request, base_generation=base_generation)
        record = device_record(job, VARIANT)
        record["status"]["phase"] = "syncing"
        record["attempts"][attempt] = {"path": path, "size": 12, "sha256": "a" * 64}
        save_device_record(job, VARIANT, record)
        db.add(
            TemporaryMediaUpload(
                user_id=user_id,
                object_path=path,
                purpose="device_render",
                status="reserved",
                retention_expires_at=datetime.now(UTC) + timedelta(hours=1),
            )
        )
        db.commit()
    monkeypatch.setattr(
        routes.storage,
        "object_metadata",
        lambda _: SimpleNamespace(size=12, content_type="video/mp4", generation="42"),
    )
    monkeypatch.setattr(
        routes.storage, "signed_get_url", lambda *_a, **_k: "https://storage.example/video"
    )
    monkeypatch.setattr(routes, "_verify_export", MagicMock(return_value=None))
    monkeypatch.setattr(routes, "_nudge_kria_observer", lambda *_a, **_k: None)

    async def publish() -> None:
        async with AsyncSessionLocal() as db:
            await routes._complete_device_export(
                job_id,
                SimpleNamespace(identity=request.identity, attempt_id=uuid.UUID(attempt)),
                user_id,
                db,
            )

    return attempt, publish


# --- 1. deploy safety: AI text A, then a real manual Save B ---------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["legacy", "v2"])
async def test_save_over_an_ai_head_never_presents_the_ai_text_as_current(
    monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id, A)
        staged = _head(item_id)
        body = {
            "base_generation": "generation-1",
            "editor_state_version": 1 if path == "legacy" else 2,
            "text_elements": [{**HOOK, "text": B}],
        }
        if path == "v2":
            _pin(job_id, staged["id"])
        base = f"/plan-items/{item_id}/variants/{VARIANT}"
        async with _client_for(user_id) as client:
            save = await client.post(f"{base}/editor-commit", json=body)
            assert save.status_code == 200, save.text
            saved = _variant_of(job_id)["render_generation_id"]
            assert saved != "generation-1"

            draft = (await client.get(f"/creation-threads/{thread_id}/draft")).json()
            timeline = await client.get(f"{base}/timeline")
            assert timeline.status_code == 200, timeline.text
            timeline = timeline.json()
            if path == "legacy":
                # As on main: nothing moved the head, so it is not current and says so.
                assert draft["head_current"] is False
                assert draft["draft_id"] == str(staged["id"])
                assert not timeline.get("draft")
            else:
                assert draft["head_current"] is True, "a v2 Save writes its own head"
                assert A not in json.dumps(draft)
            assert A not in json.dumps(timeline)

        # A new chat turn builds on the saved text B, not on the older AI edit.
        _plan_with(monkeypatch, [{"op": "set_mix", "music_level": 0.9}])
        accepted = await _submit(user_id, thread_id, "More music")
        assert (await asyncio.to_thread(run_kria_turn.run, accepted.turn_id))[
            "status"
        ] == "completed"
        turn_head = _head(item_id)
        assert A not in json.dumps(turn_head["snap"]), "the turn built on the saved video"
        assert turn_head["payload_base"] == saved

        # Undo is as on main: it restores the draft before the turn; none of it presents A as
        # current.
        async with AsyncSessionLocal() as db:
            undone, _ = await undo_draft(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=turn_head["rev"]
            )
        assert A not in json.dumps(undone.snapshot) or undone.head_current is False
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 2. approval execution and phone publish do not move the head ---------------------------


@pytest.mark.asyncio
async def test_approval_execution_leaves_every_head_row_as_main_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        staged = _rows(item_id)
        assert _head(item_id)["col"] == "generation-1"

        outcome = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert outcome["status"] == "dispatched"
        assert _variant_of(job_id)["render_generation_id"] != "generation-1"

        assert _rows(item_id) == staged, "same rows, same stamps: nothing moved the head"
        assert not _current(item_id, job_id), "head_current reports what the stamps say"
        again = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert again["status"] == "ignored"
        assert _rows(item_id) == staged
        async with _client_for(user_id) as client:
            draft = await client.get(f"/creation-threads/{thread_id}/draft")
            assert draft.status_code == 200 and draft.json()["head_current"] is False
        assert _rows(item_id) == staged, "a read writes nothing"
    finally:
        _clear_user_override()
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_phone_publish_leaves_every_head_row_as_main_does(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        await _chat_head(monkeypatch, user_id, thread_id, A)
        rows = _rows(item_id)
        attempt, publish = _publish_device_export(job_id, user_id, monkeypatch)
        await publish()
        assert _variant_of(job_id)["render_generation_id"] == attempt
        assert _rows(item_id) == rows, "same rows, same stamps"
        assert _current(item_id, job_id), "a head on the published source is current"
        async with _client_for(user_id) as client:
            draft = (await client.get(f"/creation-threads/{thread_id}/draft")).json()
        assert draft["head_current"] is True and draft["base_generation"] == "generation-1"
        assert _rows(item_id) == rows
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 3. head_current ------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_head_current_is_true_only_for_a_head_on_the_variants_generation() -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        head = await _phone_head(user_id, thread_id)
        assert head.head_current is True and head.base_generation == "generation-1"
        assert _current(item_id, job_id)

        def stamp(column: str | None, payload: str) -> None:
            with sync_session() as db:
                row = db.execute(
                    select(CreatorEditDraft).where(
                        CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True)
                    )
                ).scalar_one()
                snapshot = copy.deepcopy(row.snapshot_json)
                snapshot["editor_payload"]["base_generation"] = payload
                row.snapshot_json = snapshot
                row.base_generation_id = column
                db.commit()

        stamp("generation-1", "generation-0")
        assert not _current(item_id, job_id), "payload disagrees with the column"
        stamp("generation-0", "generation-1")
        assert not _current(item_id, job_id), "column disagrees with the payload"
        stamp("generation-0", "generation-0")
        assert not _current(item_id, job_id), "a head on an old generation"
        stamp("generation-1", "generation-1")
        assert _current(item_id, job_id)

        _set_variant(job_id, render_generation_id="generation-2")
        assert not _current(item_id, job_id)
        async with AsyncSessionLocal() as db:
            old = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        assert old.head_current is False and old.base_generation == "generation-1"

        # A head of another job is never current for this one.
        stamp("generation-2", "generation-2")
        assert _current(item_id, job_id)
        with sync_session() as db:
            row = db.execute(
                select(CreatorEditDraft).where(
                    CreatorEditDraft.item_id == item_id, CreatorEditDraft.is_head.is_(True)
                )
            ).scalar_one()
            row.base_job_id = _add_job(item_id, user_id, _variant("generation-2"), current=False)
            db.commit()
        assert not _current(item_id, job_id)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_head_written_for_a_render_finished_at_only_variant_is_current() -> None:
    user_id, thread_id, item_id, job_id = _seed(_variant(None))
    try:
        served = None
        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        head = _head(item_id)
        assert head["col"] == head["payload_base"] == "2026-09-07T08:00:00Z"
        assert served.head_current is True
        assert _current(item_id, job_id)
        written = await _phone_head(user_id, thread_id)
        assert written.head_current is True, "a PUT onto it is accepted and stays current"
        _set_variant(job_id, render_finished_at="2026-09-07T09:00:00Z")
        assert not _current(item_id, job_id), "a moved baseline leaves the head behind"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_head_main_bootstrapped_for_a_finished_at_only_variant_keeps_mains_acceptance() -> (
    None
):
    """``main`` stamps such a head column None / payload "": its PUT and saved-draft attach
    accept it, the planner ignores it. Each answer is unchanged."""
    from app.kria.drafts import _variant as draft_variant
    from app.services.editor_empty_drafts import attach_saved_editor_drafts
    from app.services.kria_editor_ops import fresh_editor_head_payload

    user_id, thread_id, item_id, job_id = _seed(_variant(None))
    try:
        async with AsyncSessionLocal() as db:
            served = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
        with sync_session() as db:
            row = db.get(CreatorEditDraft, served.draft_id)
            snapshot = copy.deepcopy(row.snapshot_json)
            snapshot["editor_payload"]["base_generation"] = ""
            row.snapshot_json = snapshot
            row.base_generation_id = None
            job = db.get(Job, job_id, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan)
            plan["variants"][0].update(
                editor_state_version=2, editor_draft={"draft_id": served.draft_id}
            )
            job.assembly_plan = plan
            db.commit()
        with sync_session() as db:
            job, head = db.get(Job, job_id), db.get(CreatorEditDraft, served.draft_id)
            variant = draft_variant(job, VARIANT)
            assert fresh_editor_head_payload(head, variant, job) == {}, "planner: ignored"
        async with AsyncSessionLocal() as db:
            job = await db.get(Job, job_id)
            await attach_saved_editor_drafts(db, job)
            assert VARIANT in job._saved_editor_drafts, "attach: accepted"
        async with AsyncSessionLocal() as db:
            written = await write_draft(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                expected_revision=served.draft_revision,
                expected_etag=served.etag,
                snapshot=copy.deepcopy(served.snapshot) | {"changes": ["Phone edit"]},
            )
        assert written.draft_revision == served.draft_revision + 1, "PUT: accepted"
    finally:
        await async_engine.dispose()


# --- 4. Save refusals carry a machine code --------------------------------------------------


@pytest.mark.asyncio
async def test_save_refusals_carry_a_code_and_only_stale_copies_ask_for_a_reload() -> None:
    user_id, _thread_id, item_id, job_id = _seed()
    try:
        url = f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit"
        body = {"base_generation": "generation-0", "editor_state_version": 1}
        async with _client_for(user_id) as client:
            moved = await client.post(url, json=body)
            assert moved.status_code == 409
            assert moved.json() == {
                "detail": "baseline_conflict",
                "problem": {
                    "code": "baseline_conflict",
                    "recovery": "reload",
                    "current_base_generation": "generation-1",
                },
            }

            # An empty-state variant refuses a clip deletion of a clip it no longer holds.
            _set_variant(job_id, editor_state="empty")
            stale_target = await client.post(
                url,
                json={
                    "base_generation": "generation-1",
                    "editor_state_version": 2,
                    "deletions": [{"kind": "clip", "id": "s0"}],
                },
            )
            assert stale_target.status_code == 409, stale_target.text
            assert stale_target.json()["detail"] == "deletion_target_stale"
            assert stale_target.json()["problem"]["code"] == "deletion_target_stale"
            assert stale_target.json()["problem"]["recovery"] == "reload"
            _set_variant(job_id, editor_state=None)

            with sync_session() as db:
                db.get(Job, job_id).status = "cancelled"
                db.commit()
            cancelled = await client.post(url, json=body)
            assert cancelled.status_code == 409
            assert cancelled.json()["detail"] == "Cancelled videos cannot be edited."
            assert cancelled.json()["problem"]["code"] == "video_cancelled"
            assert cancelled.json()["problem"]["recovery"] == "none"
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 5. a stranded pinned v2 saved draft ----------------------------------------------------


async def _stranded_pinned(*, empty: bool):  # noqa: ANN202
    """A pinned v2 draft on generation-1; a dispatcher then moves the variant to generation-2."""
    user_id, thread_id, item_id, job_id = _seed(_variant(**V2_EXTRAS))
    staged = await _phone_head(user_id, thread_id, "Pinned hook")
    _set_variant(
        job_id,
        editor_draft={"draft_id": staged.draft_id},
        editor_state="empty" if empty else None,
        editor_state_version=2,
        editor_timeline_mode="authored",
    )
    _set_variant(job_id, render_generation_id="generation-2")
    return user_id, thread_id, item_id, job_id, staged


@pytest.mark.asyncio
async def test_stranded_v2_timeline_serves_the_committed_state_and_writes_nothing() -> None:
    user_id, thread_id, item_id, job_id, staged = await _stranded_pinned(empty=False)
    try:
        rows = _rows(item_id)
        base = f"/plan-items/{item_id}/variants/{VARIANT}"
        async with _client_for(user_id) as client:
            timeline = await client.get(f"{base}/timeline")
            assert timeline.status_code == 200, timeline.text
            served = timeline.json()["draft"]
            with sync_session() as db:
                committed = editor_sections(db.get(Job, job_id), VARIANT)
            assert served["snapshot"]["editor_payload"]["sections"] == committed
            for key in V2_EXTRAS:
                assert key in served["snapshot"]["editor_payload"]["sections"], key
            assert served["head_current"] is False
            assert served["base_generation"] == "generation-2"
            assert "Pinned hook" not in json.dumps(timeline.json())
        assert _rows(item_id) == rows, "the read writes nothing"
        assert _pinned(job_id) == staged.draft_id
    finally:
        _clear_user_override()
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_save_over_a_stranded_pinned_draft_replaces_the_pinned_row_and_succeeds() -> None:
    user_id, thread_id, item_id, job_id, staged = await _stranded_pinned(empty=False)
    try:
        async with _client_for(user_id) as client:
            save = await client.post(
                f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit",
                json={
                    "base_generation": "generation-2",
                    "editor_state_version": 2,
                    "text_elements": [{**HOOK, "text": B}],
                },
            )
            assert save.status_code == 200, save.text
        rows = _rows(item_id)
        replacement = next(row for row in rows if str(row["parent"]) == staged.draft_id)
        assert replacement["col"] == replacement["payload_base"] == "generation-2"
        assert replacement["head"] is False and replacement["job"] == job_id
        assert replacement["snap"]["editor_payload"]["editor_state_version"] == 2
        assert "Pinned hook" not in json.dumps(replacement["snap"]), "nothing old comes back"
        head = _head(item_id)
        assert head["parent"] != replacement["id"] or head["id"] != replacement["id"]
        assert _pinned(job_id) == str(head["id"]), "the Save then pinned its own draft"
        assert _current(item_id, job_id)
    finally:
        _clear_user_override()
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_save_beside_a_chat_head_never_commits_the_ai_title_and_never_pins_the_chat_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id, staged = await _stranded_pinned(empty=False)
    try:
        # A chat turn writes an AI title head on the variant's NEW generation, next to the pin.
        await _chat_head(monkeypatch, user_id, thread_id, A)
        chat_head = _head(item_id)
        assert str(chat_head["id"]) != staged.draft_id and chat_head["col"] == "generation-2"
        with sync_session() as db:
            sections = copy.deepcopy(editor_sections(db.get(Job, job_id), VARIANT))
        sections["text_elements"][0]["text"] = A
        with sync_session() as db:
            row = db.get(CreatorEditDraft, chat_head["id"])
            snapshot = copy.deepcopy(row.snapshot_json)
            snapshot["editor_payload"]["sections"] = sections
            row.snapshot_json = snapshot
            db.commit()
        pinned_before = _pinned(job_id)
        assert pinned_before == staged.draft_id

        variant = _variant_of(job_id)
        async with _client_for(user_id) as client:
            save = await client.post(
                f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit",
                json={
                    "base_generation": variant["render_generation_id"],
                    "editor_state_version": 2,
                    "timeline_slots": [
                        {
                            "slot_id": row["slot_id"],
                            "clip_index": row["clip_index"],
                            "in_s": row["in_s"],
                            "duration_s": 2.5 if row["slot_id"] == "s0" else row["duration_s"],
                        }
                        for row in variant["ai_timeline"]["slots"]
                    ],
                },
            )
            assert save.status_code == 200, save.text
            committed = _variant_of(job_id)
            assert A not in json.dumps(committed.get("text_elements")), "the AI title is not saved"
            assert _pinned(job_id) != str(chat_head["id"]), "the pin never names the chat draft"
            with sync_session() as db:
                pinned_row = db.get(CreatorEditDraft, _pinned(job_id))
                assert A not in json.dumps(pinned_row.snapshot_json)
            timeline = await client.get(f"/plan-items/{item_id}/variants/{VARIANT}/timeline")
            assert timeline.status_code == 200 and A not in json.dumps(timeline.json())
    finally:
        _clear_user_override()
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_stranded_empty_state_variant_is_exactly_main() -> None:
    """Its content exists only in its draft, so an unattachable pin answers 409 as on main."""
    user_id, thread_id, item_id, job_id, _staged = await _stranded_pinned(empty=True)
    try:
        rows = _rows(item_id)
        async with _client_for(user_id) as client:
            timeline = await client.get(f"/plan-items/{item_id}/variants/{VARIANT}/timeline")
            assert timeline.status_code == 409
            assert timeline.json()["detail"] == "editor_draft_unavailable"
            save = await client.post(
                f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit",
                json={
                    "base_generation": "generation-2",
                    "editor_state_version": 2,
                    "text_elements": [{**HOOK, "text": B}],
                },
            )
            assert save.status_code == 409, save.text
            assert save.json()["detail"] == "editor_draft_unavailable"
            assert save.json()["problem"]["recovery"] == "none"
        assert _rows(item_id) == rows
    finally:
        _clear_user_override()
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_another_variants_unbuildable_fallback_does_not_fail_the_requested_variant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    other = "song_text"
    user_id, thread_id, item_id, job_id, staged = await _stranded_pinned(empty=False)
    try:
        with sync_session() as db:
            source = db.get(CreatorEditDraft, staged.draft_id)
            twin = CreatorEditDraft(
                creator_id=source.creator_id,
                thread_id=source.thread_id,
                item_id=source.item_id,
                variant_key=other,
                base_job_id=source.base_job_id,
                base_generation_id=source.base_generation_id,
                draft_revision=0,
                snapshot_json=copy.deepcopy(source.snapshot_json),
                snapshot_hash=source.snapshot_hash,
                is_head=True,
            )
            db.add(twin)
            db.flush()
            job = db.get(Job, job_id, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan)
            second = copy.deepcopy(plan["variants"][0])
            second.update(variant_id=other, editor_draft={"draft_id": str(twin.id)})
            plan["variants"].append(second)
            job.assembly_plan = plan
            db.commit()

        from app.services import editor_empty_drafts as module

        real = module.committed_document

        def broken_for_other(*, variant_key, **kwargs):  # noqa: ANN003, ANN202
            if variant_key == other:
                raise ValueError("cannot project this variant")
            return real(variant_key=variant_key, **kwargs)

        monkeypatch.setattr(module, "committed_document", broken_for_other)
        async with _client_for(user_id) as client:
            requested = await client.get(f"/plan-items/{item_id}/variants/{VARIANT}/timeline")
            assert requested.status_code == 200, "the other variant is skipped, as on main"
            assert requested.json()["draft"] is not None
    finally:
        _clear_user_override()
        await async_engine.dispose()


# --- 6. the Ataturk sequence ----------------------------------------------------------------


async def _observe(user_id, thread_id, item_id, job_id, approved_text: str | None) -> None:  # noqa: ANN001
    """Open the draft and the timeline and Save, the way the iPhone does; all must succeed."""
    async with _client_for(user_id) as client:
        current = _variant_of(job_id)["render_generation_id"]
        base = f"/plan-items/{item_id}/variants/{VARIANT}"
        draft = await client.get(f"/creation-threads/{thread_id}/draft")
        assert draft.status_code == 200, draft.text
        timeline = await client.get(f"{base}/timeline")
        assert timeline.status_code == 200, timeline.text
        if approved_text is not None:
            committed = _variant_of(job_id)
            assert approved_text in json.dumps(committed["text_elements"]), "the video holds it"
        save = await client.post(
            f"{base}/editor-commit",
            json={
                "base_generation": current,
                "editor_state_version": 1,
                "text_elements": [{**HOOK, "text": f"Saved after {current[:8]}"}],
            },
        )
        assert save.status_code == 200, save.text
        assert _variant_of(job_id)["render_generation_id"] != current


@pytest.mark.asyncio
@pytest.mark.parametrize("through", ["ai_draft", "approval_render", "phone_publish"])
async def test_ataturk_ai_draft_then_approval_then_phone_publish_stays_usable(
    monkeypatch: pytest.MonkeyPatch, through: str
) -> None:
    """AI draft on G1 -> approval executes (G2) -> phone publish (G3).

    After each step draft GET, timeline GET and Save succeed and the committed document served
    holds the approved edit. The head is not moved, so ``head_current`` may be false after G2.
    Each parameter runs the sequence up to its step on its own project, because a Save advances
    the generation.
    """
    user_id, thread_id, item_id, job_id = _seed()
    try:
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        approved = None
        if through != "ai_draft":
            await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
            approved = "Fresh matcha, finally"
        if through == "phone_publish":
            _attempt, publish = _publish_device_export(
                job_id,
                user_id,
                monkeypatch,
                base_generation=_variant_of(job_id)["render_generation_id"],
            )
            await publish()
            _set_variant(job_id, render_destination="cloud")
        await _observe(user_id, thread_id, item_id, job_id, approved)
    finally:
        _clear_user_override()
        await async_engine.dispose()
