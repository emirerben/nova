"""Real-Postgres proof that every writer which advances a generation restages the head.

Failure modes 18-24 of ``docs/reviews/latest-wins-convergence/server-failure-modes.md``
("Writers"). Each test drives the real code path (approval execution, phone publish,
speech-cut publication) and asserts rows and returned documents only.
"""

from __future__ import annotations

import asyncio
import copy
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
from app.kria.drafts import read_or_bootstrap_draft, write_draft
from app.kria.editor_head import is_head_current
from app.kria.planner import PlannedKriaTurn, adapt_editor_action
from app.kria.runtime import approval_fingerprint, decide_approval, submit_turn
from app.models import (
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorEditDraft,
    Job,
    PlanItem,
    TemporaryMediaUpload,
)
from app.tasks.kria_runtime import (
    _claim_approval_dispatch,
    execute_kria_approval,
    run_kria_turn,
)
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


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


def _variant(generation: str = "generation-1", **extra) -> dict:  # noqa: ANN003
    return {
        "variant_id": VARIANT,
        "resolved_archetype": "montage",
        "render_status": "ready",
        "render_generation_id": generation,
        "render_finished_at": "2026-09-07T08:00:00Z",
        "video_path": "generative-jobs/test/output.mp4",
        "base_video_path": "generative-jobs/test/base.mp4",
        "text_elements": [dict(HOOK)],
        **extra,
    }


def _seed(variant: dict | None = None, **plan):  # noqa: ANN003, ANN202
    """Project + current Job + session pointing at the variant (as the editor turn expects)."""
    user_id, thread_id, session_id = _seed_runtime_project()
    job_id = uuid.uuid4()
    variant = variant or _variant()
    with sync_session() as db:
        from app.models import CreatorAgentSession

        session = db.get(CreatorAgentSession, session_id, with_for_update=True)
        item = db.get(PlanItem, session.plan_item_id, with_for_update=True)
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="variants_ready",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                all_candidates={"clip_paths": ["users/test/matcha.mp4"]},
                assembly_plan={"variants": [variant], **plan},
            )
        )
        db.flush()
        item.current_job_id = job_id
        session.target_job_id = job_id
        session.target_variant_id = VARIANT
        session.target_generation_id = variant["render_generation_id"]
        session.manifest_hash = "a" * 64
        item_id = item.id
        db.commit()
    return user_id, thread_id, item_id, job_id


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


def _text(row: dict) -> str:
    return row["snap"]["editor_payload"]["sections"]["text_elements"][0]["text"]


def _head_is_current(item_id, job_id) -> bool:  # noqa: ANN001
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


def _variant_of(job_id) -> dict:  # noqa: ANN001
    with sync_session() as db:
        return copy.deepcopy(db.get(Job, job_id).assembly_plan["variants"][0])


async def _stage_ai_edit(user_id, thread_id, text: str = "AI hook"):  # noqa: ANN001, ANN202
    async with AsyncSessionLocal() as db:
        initial = await read_or_bootstrap_draft(db, thread_id=thread_id, creator_id=user_id)
    payload = copy.deepcopy(initial.snapshot["editor_payload"])
    payload["sections"]["text_elements"][0]["text"] = text
    async with AsyncSessionLocal() as db:
        return await write_draft(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            expected_revision=initial.draft_revision,
            expected_etag=initial.etag,
            snapshot={
                **initial.snapshot,
                "editor_payload": payload,
                "changes": ["Retitle the hook"],
                "editor_text_diff": [{"index": 0, "before": "Old matcha hook", "after": text}],
            },
        )


async def _approved_ai_edit(  # noqa: ANN202
    monkeypatch: pytest.MonkeyPatch,
    user_id,
    thread_id,
    ops,
    *,
    request_render=True,  # noqa: ANN001
):
    """A real editor turn that proposes ``ops`` and a creator approval of it."""
    editor_plan = adapt_editor_action(
        reply="I prepared that edit.", request_render=request_render, ops=ops
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(plan=editor_plan, manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    monkeypatch.setattr(
        "app.tasks.kria_runtime.enqueue_editor_commit_render", lambda *_a, **_k: None
    )
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Apply the edit",
                client_event_id=f"editor-{uuid.uuid4().hex}",
                expected_thread_revision=2,
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


EDIT_HOOK = [{"op": "edit_text", "bar_index": 0, "text": "Fresh matcha, finally"}]


@pytest.mark.asyncio
async def test_approval_execution_restages_the_head_on_the_new_generation_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        staged = _head(item_id)
        assert staged["col"] == staged["payload_base"] == "generation-1"
        assert staged["snap"]["changes"], "the AI edit is a pending change before it renders"

        outcome = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert outcome["status"] == "dispatched"

        new_generation = _variant_of(job_id)["render_generation_id"]
        assert new_generation != "generation-1"
        head = _head(item_id)
        assert head["parent"] == staged["id"]
        assert head["job"] == job_id
        assert head["col"] == head["payload_base"] == new_generation
        assert _head_is_current(item_id, job_id)
        assert _text(head) == "Fresh matcha, finally", "the render holds the approved edit"
        assert head["snap"]["changes"] == [], "contained: committed state, no pending diff"
        assert "editor_text_diff" not in head["snap"]
        rows_after_first = _rows(item_id)

        # The same task delivered again after it dispatched: nothing more to restage.
        again = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert again["status"] == "ignored"
        assert _rows(item_id) == rows_after_first
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_claim_retried_between_accept_and_dispatch_keeps_the_render_and_adds_no_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)

        # A process dying after the claim committed and before broker publication.
        first = await asyncio.to_thread(_claim_approval_dispatch, approval_id)
        assert first is not None
        rows_after_claim = _rows(item_id)
        assert _head(item_id)["col"] == first.target_generation_id

        second = await asyncio.to_thread(_claim_approval_dispatch, approval_id)
        assert second is not None, "the accepted execution is recovered, not cancelled as stale"
        assert second.target_generation_id == first.target_generation_id
        assert _rows(item_id) == rows_after_claim

        outcome = await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert outcome["status"] == "dispatched"
        assert _rows(item_id) == rows_after_claim
        with sync_session() as db:
            approval = db.get(CreatorAgentApproval, approval_id)
            execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
            assert approval.status == "consumed"
            assert execution.status == "dispatched"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_approval_that_leaves_the_generation_alone_inserts_no_head_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        # A title-only commit stages nothing and keeps the generation.
        approval_id = await _approved_ai_edit(
            monkeypatch,
            user_id,
            thread_id,
            [{"op": "set_title", "title": "Matcha, finally"}],
        )
        before = _rows(item_id)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert _variant_of(job_id)["render_generation_id"] == "generation-1"
        assert _rows(item_id) == before
        assert len([row for row in _rows(item_id) if row["head"]]) == 1
        assert _head_is_current(item_id, job_id)
    finally:
        await async_engine.dispose()


def _publish_device_export(  # noqa: ANN202
    job_id,  # noqa: ANN001
    user_id,  # noqa: ANN001
    monkeypatch,  # noqa: ANN001
    *,
    base_generation: str = "generation-1",
):
    """Pin a device recipe on the real Job and return (attempt id, call that publishes it)."""
    from app.kria.device_render import make_device_request
    from app.kria.recipes import EditRecipeV1
    from app.routes import device_render as routes
    from app.services.device_render import (
        device_record,
        pin_device_request,
        save_device_record,
    )

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


@pytest.mark.asyncio
async def test_phone_publish_moves_the_head_to_the_attempt_id_and_keeps_its_lanes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        staged = await _stage_ai_edit(user_id, thread_id, "AI hook")
        attempt, publish = _publish_device_export(job_id, user_id, monkeypatch)

        await publish()

        assert _variant_of(job_id)["render_generation_id"] == attempt
        head = _head(item_id)
        assert head["parent"] == uuid.UUID(staged.draft_id)
        assert head["col"] == head["payload_base"] == attempt
        assert _head_is_current(item_id, job_id)
        assert _text(head) == "AI hook", "same document, new id: the staged edit survives"
        assert head["snap"]["changes"] == ["Retitle the hook"]
        rows_after = _rows(item_id)

        await publish()  # a retried completion answers from the record: no second row
        assert _rows(item_id) == rows_after
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_phone_publish_without_an_editor_head_creates_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, _thread_id, item_id, job_id = _seed()
    try:
        attempt, publish = _publish_device_export(job_id, user_id, monkeypatch)

        await publish()

        assert _variant_of(job_id)["render_generation_id"] == attempt
        assert _rows(item_id) == []
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_speech_cut_publication_restages_the_head_as_the_committed_cut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import generative_build as gb

    operation_id, attempt_id, cut_generation = "op-1", "attempt-1", "generation-cut"
    control = {
        "operation_id": operation_id,
        "variant_id": VARIANT,
        "render_generation_id": cut_generation,
        "desired_disabled": True,
        "forced_removals": [],
        "operation": {"operation": "apply_speech_cut_candidate", "candidate_id": "cut-1"},
        "finalizer_claim": {
            "operation_id": operation_id,
            "attempt_id": attempt_id,
            "render_generation_id": cut_generation,
        },
    }
    user_id, thread_id, item_id, job_id = _seed(
        _variant(cut_generation, silence_cut={"removed": []}),
        speech_cut_control=control,
    )
    try:
        # The head was staged on the generation the cut replaces.
        with sync_session() as db:
            job = db.get(Job, job_id, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan)
            plan["variants"][0]["render_generation_id"] = "generation-1"
            job.assembly_plan = plan
            db.commit()
        staged = await _stage_ai_edit(user_id, thread_id, "AI hook")
        with sync_session() as db:
            job = db.get(Job, job_id, with_for_update=True)
            plan = copy.deepcopy(job.assembly_plan)
            plan["variants"][0]["render_generation_id"] = cut_generation
            plan["variants"][0]["text_elements"] = [{**HOOK, "text": "Cut hook"}]
            job.assembly_plan = plan
            db.commit()
        monkeypatch.setattr(
            gb, "_reconcile_retired_variant_posters_after_terminal_commit", lambda *_a, **_k: None
        )

        await asyncio.to_thread(
            gb._publish_speech_cut_rerender,
            str(job_id),
            expected_operation_id=operation_id,
            expected_attempt_id=attempt_id,
        )

        published = _variant_of(job_id)
        assert published["render_generation_id"] == cut_generation
        assert published["speech_cut_in_flight"] is None
        head = _head(item_id)
        assert head["parent"] == uuid.UUID(staged.draft_id)
        assert head["col"] == head["payload_base"] == cut_generation
        assert _head_is_current(item_id, job_id)
        assert _text(head) == "Cut hook", "latest wins on a lane both changed"
        assert head["snap"]["changes"] == []
    finally:
        await async_engine.dispose()


# The Ataturk regression: one document, whichever step the variant is on.


async def _observe_one_document(user_id, thread_id, item_id, job_id) -> str:  # noqa: ANN001
    """Open the draft, the timeline and Save the way the iPhone does; all must agree."""
    import httpx

    from app.auth import get_current_user
    from app.main import app

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        id=user_id, email="qa@test.local"
    )
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            current = _variant_of(job_id)["render_generation_id"]
            base = f"/plan-items/{item_id}/variants/{VARIANT}"
            before = await client.get(f"{base}/timeline")
            assert before.status_code == 200, before.text

            draft = await client.get(f"/creation-threads/{thread_id}/draft")
            assert draft.status_code == 200, draft.text
            snapshot = draft.json()
            assert snapshot["head_current"] is True
            assert snapshot["base_generation"] == current
            assert snapshot["snapshot"]["editor_payload"]["base_generation"] == current

            after = await client.get(f"{base}/timeline")
            assert after.status_code == 200, after.text
            for timeline in (before.json(), after.json()):
                assert timeline.get("base_generation") in (None, current)

            save = await client.post(
                f"{base}/editor-commit",
                json={
                    "base_generation": snapshot["base_generation"],
                    "editor_state_version": 1,
                    "text_elements": [{**HOOK, "text": "Saved on the one document"}],
                },
            )
            assert save.status_code == 200, save.text
            # The Save moved the variant to a new generation; the head must not lag it.
            saved = _variant_of(job_id)["render_generation_id"]
            assert saved != current
            assert _head_is_current(item_id, job_id), "the head lags a Save until someone reads it"
            again = await client.get(f"/creation-threads/{thread_id}/draft")
            assert again.status_code == 200, again.text
            assert again.json()["head_current"] is True
            assert again.json()["base_generation"] == saved
            assert _head_is_current(item_id, job_id)
            assert _head(item_id)["payload_base"] == saved
            return current
    finally:
        app.dependency_overrides.pop(get_current_user, None)


@pytest.mark.asyncio
@pytest.mark.parametrize("through", ["ai_draft", "approval_render", "phone_publish"])
async def test_ataturk_draft_then_generation_advance_serves_one_document(
    monkeypatch: pytest.MonkeyPatch, through: str
) -> None:
    """AI draft on G1 -> approval executes (G2) -> phone publish (G3).

    After each step the draft, the timeline and a Save all succeed on the same generation,
    the head is current, and its payload base equals the variant's ``render_generation_id``.
    Each parameter runs the sequence up to its step on its own project, because a Save
    itself advances the generation.
    """
    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", lambda *a, **k: None)
    from tests.routes.test_editor_commit import _arm

    _arm(monkeypatch)
    user_id, thread_id, item_id, job_id = _seed()
    try:
        approval_id = await _approved_ai_edit(monkeypatch, user_id, thread_id, EDIT_HOOK)
        if through != "ai_draft":
            await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
            rows = _rows(item_id)
            await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
            assert _rows(item_id) == rows, "the approval task run twice adds no draft row"
        if through == "phone_publish":
            _attempt, publish = _publish_device_export(
                job_id,
                user_id,
                monkeypatch,
                base_generation=_variant_of(job_id)["render_generation_id"],
            )
            await publish()
            # The phone Save compiler needs a guided story plan (see test_phone_editor_commit);
            # the head invariant does not depend on it, so the Save runs on the cloud compiler.
            with sync_session() as db:
                job = db.get(Job, job_id, with_for_update=True)
                plan = copy.deepcopy(job.assembly_plan)
                plan["variants"][0]["render_destination"] = "cloud"
                job.assembly_plan = plan
                db.commit()

        before = _head(item_id)
        generation = await _observe_one_document(user_id, thread_id, item_id, job_id)

        assert before["payload_base"] == generation
        assert _head_is_current(item_id, job_id)
    finally:
        await async_engine.dispose()


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


async def _strand_pinned_v2_draft(monkeypatch, user_id, thread_id, job_id):  # noqa: ANN001, ANN202
    """A v2 variant pinned to a saved draft, then a legacy dispatcher moves its generation."""
    from tests.routes.test_editor_commit import _arm

    _arm(monkeypatch)
    monkeypatch.setattr("app.routes.plan_items.enqueue_editor_commit_render", lambda *a, **k: None)
    staged = await _stage_ai_edit(user_id, thread_id, "Pinned hook")
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        plan = copy.deepcopy(job.assembly_plan)
        variant = plan["variants"][0]
        variant.update(
            editor_state_version=2,
            editor_timeline_mode="authored",
            editor_draft={"draft_id": staged.draft_id},
        )
        job.assembly_plan = plan
        db.commit()
        plan = copy.deepcopy(job.assembly_plan)
        plan["variants"][0]["render_generation_id"] = "generation-2"
        job.assembly_plan = plan
        db.commit()
    return staged


@pytest.mark.asyncio
async def test_stranded_pinned_draft_is_served_from_the_committed_variant_and_save_heals_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, item_id, job_id = _seed()
    try:
        staged = await _strand_pinned_v2_draft(monkeypatch, user_id, thread_id, job_id)
        base = f"/plan-items/{item_id}/variants/{VARIANT}"
        rows_before = len(_rows(item_id))
        async with _client_for(user_id) as client:
            timeline = await client.get(f"{base}/timeline")
            # GET stays write-free and serves the pinned draft rebased in memory.
            assert timeline.status_code == 200, timeline.text
            served = timeline.json()["draft"]
            assert served["snapshot"]["editor_payload"]["base_generation"] == "generation-2"
            assert (
                served["snapshot"]["editor_payload"]["sections"]["text_elements"][0]["text"]
                == "Pinned hook"
            )
            assert _head(item_id)["id"] == uuid.UUID(staged.draft_id)
            assert len(_rows(item_id)) == rows_before

            save = await client.post(
                f"{base}/editor-commit",
                json={
                    "base_generation": "generation-2",
                    "editor_state_version": 2,
                    "text_elements": [{**HOOK, "text": "Saved over the stranded draft"}],
                },
            )
            assert save.status_code == 200, save.text
        assert _head_is_current(item_id, job_id)
    finally:
        _clear_user_override()
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_save_refusals_name_their_cause_and_only_a_moved_baseline_asks_for_a_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests.routes.test_editor_commit import _arm

    _arm(monkeypatch)
    user_id, _thread_id, item_id, job_id = _seed()
    try:
        base = f"/plan-items/{item_id}/variants/{VARIANT}/editor-commit"
        body = {"base_generation": "generation-0", "editor_state_version": 1}
        async with _client_for(user_id) as client:
            moved = await client.post(base, json=body)
            assert moved.status_code == 409
            assert moved.json() == {
                "detail": "baseline_conflict",
                "problem": {
                    "code": "baseline_conflict",
                    "recovery": "reload",
                    "current_base_generation": "generation-1",
                },
            }

            with sync_session() as db:
                db.get(Job, job_id).status = "cancelled"
                db.commit()
            cancelled = await client.post(base, json=body)
            assert cancelled.status_code == 409
            assert cancelled.json()["detail"] == "Cancelled videos cannot be edited."
            assert cancelled.json()["problem"]["code"] == "video_cancelled"
            assert cancelled.json()["problem"]["recovery"] == "none"
    finally:
        _clear_user_override()
        await async_engine.dispose()
