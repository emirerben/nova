"""Live plan & review: section-scoped turns, enforcement and Undo (KRI-441, KRI-442).

Real Postgres, real worker task (`run_kria_turn`), real planner for the manual-edit and
op-filter cases. Only the model call is replaced. Each test names the way it could fail:

* a flagged turn moves an unflagged lane (title, mix, timeline);
* a manual edit reaches the model, or its target drifts;
* a scope is silently dropped when the flag is off;
* a section Undo restores the wrong lanes, restores while a render runs, or accepts a stale
  revision;
* the restoring approval cannot be approved.
"""

from __future__ import annotations

import asyncio
import copy
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select, update

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import plan_review
from app.kria.api_schemas import (
    ApprovalDecisionBody,
    SubmitTurnBody,
)
from app.kria.contracts import KriaTurnPlan
from app.kria.drafts import undo_draft
from app.kria.plan_contract import ManualEdit, PlanSectionUndoBody
from app.kria.planner import PlannedKriaTurn, adapt_editor_action
from app.kria.runtime import (
    RuntimeFailure,
    approval_fingerprint,
    decide_approval,
    request_digest,
    submit_turn,
)
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    CreatorEditDraft,
    Job,
    PlanItem,
)
from app.tasks.kria_runtime import run_kria_turn


def _seed_runtime_project():
    # Keep pure scope checks runnable without importing the Postgres probe.
    from tests.kria.test_runtime_postgres_integration import _seed_runtime_project as seed

    return seed()


TITLE = {
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
NOTE = {
    "id": "kria-note",
    "text": "Ceremonial grade",
    "start_s": 3.0,
    "end_s": 5.0,
    "font_family": "Playfair Display",
    "size_px": 48,
    "color": "#FFFFFF",
    "effect": "static",
    "alignment": "center",
    "position": "middle",
}
CUES = [
    {
        "id": "cue-1",
        "text": "so I whisk the matcha for about thirty seconds",
        "start_s": 0.0,
        "end_s": 3.0,
    },
    {"id": "cue-2", "text": "until it is smooth and frothy on top", "start_s": 3.0, "end_s": 6.0},
]


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(
        "app.tasks.kria_runtime.enqueue_editor_commit_render", lambda *_a, **_k: None
    )


def _seed_job(user_id, session_id, *, generation: str = "generation-1") -> uuid.UUID:  # noqa: ANN001
    job_id = uuid.uuid4()
    with sync_session() as db:
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
                assembly_plan={
                    "variants": [
                        {
                            "variant_id": "original_text",
                            "resolved_archetype": "montage",
                            "render_status": "ready",
                            "render_generation_id": generation,
                            "render_finished_at": "2026-09-07T08:00:00Z",
                            "video_path": "generative-jobs/test/output.mp4",
                            "base_video_path": "generative-jobs/test/base.mp4",
                            "text_elements": [dict(TITLE), dict(NOTE)],
                            "caption_cues": copy.deepcopy(CUES),
                            "mix": 0.6,
                            "music_track_id": None,
                        }
                    ]
                },
            )
        )
        db.flush()
        item.current_job_id = job_id
        session.target_job_id = job_id
        session.target_variant_id = "original_text"
        session.target_generation_id = generation
        session.manifest_hash = "a" * 64
        db.commit()
    return job_id


def _revision(thread_id) -> int:  # noqa: ANN001
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


async def _submit(user_id, thread_id, message="Update: captions", **kwargs):  # noqa: ANN001, ANN003, ANN202
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"scope-{uuid.uuid4().hex}",
                expected_thread_revision=_revision(thread_id),
                **kwargs,
            ),
        )
    return accepted


def _planner_returns(monkeypatch, ops, *, scope_diag=None):  # noqa: ANN001, ANN202
    """Replace the whole planner: the worker still claims, compiles and drafts for real."""

    async def _planned(*_a, **_k):  # noqa: ANN002, ANN003, ANN202
        plan = adapt_editor_action(reply="Done.", ops=ops, request_render=True)
        if scope_diag:
            plan = plan.model_copy(update={"diagnostics": scope_diag})
        return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)


def _drafts(thread_id):  # noqa: ANN001, ANN202
    with sync_session() as db:
        rows = (
            db.execute(
                select(CreatorEditDraft)
                .where(CreatorEditDraft.thread_id == thread_id)
                .order_by(CreatorEditDraft.draft_revision)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": r.id,
                "rev": r.draft_revision,
                "parent": r.parent_draft_id,
                "head": r.is_head,
                "snap": r.snapshot_json,
                "exec": r.source_execution_id,
            }
            for r in rows
        ]


def _head_payload(thread_id) -> dict:  # noqa: ANN001
    (head,) = [d for d in _drafts(thread_id) if d["head"]]
    return head["snap"]["editor_payload"]


def _complete_render(job_id, thread_id, *, generation: str, variant_patch: dict) -> None:  # noqa: ANN001
    """Pretend the approved render finished: the variant carries the new lanes and the
    thread has no active turn or pending approval."""
    with sync_session() as db:
        job = db.get(Job, job_id, with_for_update=True)
        plan = copy.deepcopy(job.assembly_plan)
        plan["variants"][0].update(variant_patch)
        plan["variants"][0]["render_generation_id"] = generation
        job.assembly_plan = plan
        thread = db.get(CreationThread, thread_id)
        session = db.get(CreatorAgentSession, thread.active_creator_agent_session_id)
        session.target_generation_id = generation
        db.execute(
            update(CreatorAgentTurn)
            .where(CreatorAgentTurn.thread_id == thread_id)
            .values(status="completed")
        )
        db.execute(
            update(CreatorAgentApproval)
            .where(CreatorAgentApproval.thread_id == thread_id)
            .values(status="consumed")
        )
        db.commit()


def _plan_block(thread_id, job_id, section, *, revision, changed) -> None:  # noqa: ANN001
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        sequence = (
            db.execute(
                select(CreationThreadEvent.sequence)
                .where(CreationThreadEvent.thread_id == thread_id)
                .order_by(CreationThreadEvent.sequence.desc())
            )
            .scalars()
            .first()
            or 0
        ) + 1
        thread.revision = int(thread.revision) + 1
        db.add(
            CreationThreadEvent(
                thread_id=thread_id,
                sequence=sequence,
                revision=thread.revision,
                role="system",
                event_type="plan_block",
                content=None,
                payload={
                    "turn_id": None,
                    "job_id": str(job_id),
                    "blocks": [
                        {
                            "section_id": section,
                            "state": "decided",
                            "summary": "x",
                            "revision": revision,
                            "changed": changed,
                        }
                    ],
                },
            )
        )
        db.commit()


# ----------------------------------------------------------------------------------------
# Submit-time contract
# ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_scope_with_the_flag_off_is_refused_not_dropped(monkeypatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", False)
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    try:
        for extra in ({"scope": ["captions"]}, {"manual_edits": []}):
            with pytest.raises(RuntimeFailure) as caught:
                await _submit(user_id, thread_id, **extra)
            assert (caught.value.status_code, caught.value.code) == (
                404,
                "live_plan_review_unavailable",
            )
        accepted = await _submit(user_id, thread_id, "Make it warmer")
        assert accepted.status == "pending"
        with sync_session() as db:
            event = db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "user_message",
                )
            ).scalar_one()
            assert "scope" not in event.payload and "manual_edits" not in event.payload
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kwargs", "status", "code"),
    [
        ({"scope": []}, 422, "scope_invalid"),
        ({"scope": ["captions", "captions"]}, 422, "scope_invalid"),
        ({"scope": ["captions", "nonsense"]}, 422, "scope_invalid"),
        ({"scope": ["captions", "post_caption"]}, 422, "scope_section_unsupported"),
        (
            {"manual_edits": [ManualEdit(kind="set_mix", music_level=0.2)]},
            422,
            "manual_edit_out_of_scope",
        ),
        (
            {
                "scope": ["captions"],
                "manual_edits": [ManualEdit(kind="set_mix", music_level=0.2)],
            },
            422,
            "manual_edit_out_of_scope",
        ),
        (
            {
                "scope": ["title"],
                "manual_edits": [ManualEdit(kind="rewrite_text", target_id="gone", text="x")],
            },
            409,
            "scope_target_missing",
        ),
    ],
)
async def test_scoped_turn_validation_errors(kwargs, status, code) -> None:  # noqa: ANN001
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    try:
        with pytest.raises(RuntimeFailure) as caught:
            await _submit(user_id, thread_id, **kwargs)
        assert (caught.value.status_code, caught.value.code) == (status, code)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_scope_without_a_rendered_variant_is_409_scope_target_missing() -> None:
    user_id, thread_id, _session_id = _seed_runtime_project()
    try:
        with pytest.raises(RuntimeFailure) as caught:
            await _submit(user_id, thread_id, scope=["captions"])
        assert (caught.value.status_code, caught.value.code, caught.value.recovery) == (
            409,
            "scope_target_missing",
            "refresh_replan",
        )
    finally:
        await async_engine.dispose()


def test_malformed_manual_edits_are_a_pydantic_422() -> None:
    from pydantic import ValidationError

    base = {"message": "x", "client_event_id": "e", "expected_thread_revision": 0}
    for bad in (
        {"kind": "rewrite_text", "target_id": "hook"},
        {"kind": "rewrite_text", "text": "x"},
        {"kind": "set_mix"},
    ):
        with pytest.raises(ValidationError):
            SubmitTurnBody(**base, scope=["title"], manual_edits=[bad])


def test_request_digest_is_unchanged_when_scope_is_absent_and_sensitive_when_present() -> None:
    plain = SubmitTurnBody(message="hi", client_event_id="e1", expected_thread_revision=1)
    scoped = SubmitTurnBody(
        message="hi", client_event_id="e1", expected_thread_revision=1, scope=["captions"]
    )
    other = SubmitTurnBody(
        message="hi", client_event_id="e1", expected_thread_revision=1, scope=["music"]
    )
    # Verified against the pre-metadata parent, rather than rebuilt from today's schema.
    assert (
        request_digest(plain) == "b94913df11c85497295f8c4c32bb5ec4b30642d1dbe9d4a440d30be47652d33c"
    )
    assert len({request_digest(plain), request_digest(scoped), request_digest(other)}) == 3


# ----------------------------------------------------------------------------------------
# Enforcement
# ----------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_layer3_strips_every_lane_outside_the_scope_from_the_stored_draft(
    monkeypatch,
) -> None:
    """The planner (replaced) hands over a leaky bundle: caption edit + retitle + mix."""
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    _planner_returns(
        monkeypatch,
        [
            {"op": "edit_caption", "cue_index": 0, "text": "I whisk the matcha"},
            {"op": "edit_text", "bar_index": 0, "text": "Leaked retitle"},
            {"op": "set_mix", "music_level": 0.2},
        ],
    )
    try:
        accepted = await _submit(user_id, thread_id, scope=["captions"])
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        with sync_session() as db:
            assert db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id)).status == (
                "awaiting_approval"
            )
        payload = _head_payload(thread_id)
        assert payload["caption_cues"][0]["text"] == "I whisk the matcha"
        assert "text_elements" not in payload  # the hook retitle was reverted
        assert "mix" not in payload
        drafts = _drafts(thread_id)
        # A baseline revision sits under the update so "Undo all" has a parent.
        assert [d["snap"]["intent"] for d in drafts][0] == "Before update"
        assert drafts[-1]["parent"] == drafts[0]["id"]
        with sync_session() as db:
            execution = db.get(CreatorAgentExecution, drafts[-1]["exec"])
            record = plan_review.head_restore_record(execution.result)
        assert plan_review.record_sections(record) == ["captions"]
        assert (
            record["sections"]["captions"]["caption_cues"]["rows"]["cue-1"]["text"]
            == (CUES[0]["text"])
        )
        # The receipt and the reply must not claim the retitle and the mix change that layer 3
        # took back.
        receipt = " ".join(drafts[-1]["snap"]["changes"]).lower()
        assert "caption" in receipt
        assert "retitle" not in receipt and "mix" not in receipt and "leaked" not in receipt
        with sync_session() as db:
            replies = (
                db.execute(
                    select(CreationThreadEvent.content).where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.role == "assistant",
                    )
                )
                .scalars()
                .all()
            )
        assert any("left" in (r or "") and "alone" in (r or "") for r in replies)
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_scope_stored_at_submit_still_holds_if_the_flag_flips_off_before_planning(
    monkeypatch,
) -> None:
    """Flag on at submit, off before the worker plans: the stored scope must still be
    enforced, never planned as an unscoped re-plan."""
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    _planner_returns(
        monkeypatch,
        [
            {"op": "edit_caption", "cue_index": 0, "text": "I whisk the matcha"},
            {"op": "set_mix", "music_level": 0.2},
        ],
    )
    try:
        accepted = await _submit(user_id, thread_id, scope=["captions"])
        monkeypatch.setattr(settings, "live_plan_review_enabled", False)
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        payload = _head_payload(thread_id)
        assert payload["caption_cues"][0]["text"] == "I whisk the matcha"
        assert "mix" not in payload
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_bundle_that_only_touches_unflagged_sections_changes_nothing(monkeypatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    _planner_returns(monkeypatch, [{"op": "set_mix", "music_level": 0.2}])
    try:
        accepted = await _submit(user_id, thread_id, scope=["captions"])
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert _drafts(thread_id) == []
        with sync_session() as db:
            reply = (
                db.execute(
                    select(CreationThreadEvent)
                    .where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.event_type == "assistant_response",
                    )
                    .order_by(CreationThreadEvent.sequence.desc())
                )
                .scalars()
                .first()
            )
            assert "Nothing was changed" in reply.content
    finally:
        await async_engine.dispose()


def _copilot_double(monkeypatch, ops, reply="Shortened."):  # noqa: ANN001, ANN202
    calls: list[dict] = []

    async def _run(body, *, job_id):  # noqa: ANN001, ANN202, ARG001
        calls.append(body.snapshot)
        return SimpleNamespace(ops=copy.deepcopy(ops), reply=reply, outcome="proposed")

    monkeypatch.setattr("app.kria.planner.run_copilot_turn", _run)
    return calls


@pytest.mark.asyncio
async def test_planner_scopes_the_snapshot_and_drops_out_of_scope_ops(monkeypatch) -> None:
    """Layers 1 and 2 through the real planner: only the flagged families are offered and a
    title edit riding the shared `text` family is dropped and reported."""
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    calls = _copilot_double(
        monkeypatch,
        [
            {"op": "edit_caption", "cue_index": 0, "text": "I whisk the matcha"},
            {"op": "edit_text", "bar_index": 0, "text": "Leaked retitle"},
        ],
    )
    try:
        accepted = await _submit(
            user_id, thread_id, "make the captions shorter", scope=["captions"]
        )
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        (snapshot,) = calls
        assert snapshot["scope"] == ["captions"]
        assert set(snapshot["allowed_op_families"]) <= {"caption", "text"}
        assert "caption" in snapshot["allowed_op_families"]
        payload = _head_payload(thread_id)
        assert payload["caption_cues"][0]["text"] == "I whisk the matcha"
        assert "text_elements" not in payload
        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            assert turn.status == "awaiting_approval"
            plan = KriaTurnPlan.model_validate(turn.plan_json)
            assert [i.tool_name for i in plan.intents] == [
                "draft.apply_editor_ops",
                "render.request",
            ]
            assert plan.diagnostics["scope"] == ["captions"]
            assert plan.diagnostics["scope_dropped"] == [{"op": "edit_text", "section": "title"}]
            reply = db.execute(
                select(CreationThreadEvent).where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == "draft_applied",
                )
            ).scalar_one()
            assert "title alone" in reply.content
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_scoped_turn_whose_ops_are_all_out_of_scope_replies_and_drafts_nothing(
    monkeypatch,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    _copilot_double(monkeypatch, [{"op": "edit_text", "bar_index": 0, "text": "Leaked"}])
    try:
        accepted = await _submit(user_id, thread_id, "tighten it", scope=["captions"])
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert _drafts(thread_id) == []
        with sync_session() as db:
            reply = (
                db.execute(
                    select(CreationThreadEvent)
                    .where(
                        CreationThreadEvent.thread_id == thread_id,
                        CreationThreadEvent.event_type == "assistant_response",
                    )
                    .order_by(CreationThreadEvent.sequence.desc())
                )
                .scalars()
                .first()
            )
            assert "which you did not flag" in reply.content
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_manual_edits_make_no_model_call_and_change_only_their_lanes(monkeypatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    calls = _copilot_double(monkeypatch, [{"op": "set_mix", "music_level": 0.1}])
    try:
        accepted = await _submit(
            user_id,
            thread_id,
            "Update: title, captions",
            scope=["title", "captions"],
            manual_edits=[
                ManualEdit(kind="rewrite_text", target_id="hook", text="Matcha mornings"),
                ManualEdit(kind="rewrite_text", target_id="cue-2", text="until smooth"),
            ],
        )
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        assert calls == []  # every flagged section had a deterministic edit
        payload = _head_payload(thread_id)
        titles = {row["id"]: row["text"] for row in payload["text_elements"]}
        assert titles["hook"] == "Matcha mornings"
        assert titles["kria-note"] == NOTE["text"]
        assert {c["id"]: c["text"] for c in payload["caption_cues"]}["cue-2"] == "until smooth"
        assert "mix" not in payload and "timeline_slots" not in payload
        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(accepted.turn_id))
            assert turn.status == "awaiting_approval"
            event = db.get(CreationThreadEvent, turn.source_event_id)
            assert event.payload["scope"] == ["title", "captions"]
            assert len(event.payload["manual_edits"]) == 2
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_a_flagged_section_without_a_manual_edit_still_gets_the_model_but_scoped(
    monkeypatch,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    calls = _copilot_double(
        monkeypatch, [{"op": "edit_caption", "cue_index": 1, "text": "until smooth"}]
    )
    try:
        accepted = await _submit(
            user_id,
            thread_id,
            "shorten the captions",
            scope=["title", "captions"],
            manual_edits=[
                ManualEdit(kind="rewrite_text", target_id="hook", text="Matcha mornings")
            ],
        )
        await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        (snapshot,) = calls
        assert snapshot["scope"] == ["captions"]  # title was covered by the manual edit
        payload = _head_payload(thread_id)
        assert {r["id"]: r["text"] for r in payload["text_elements"]}["hook"] == "Matcha mornings"
        assert {c["id"]: c["text"] for c in payload["caption_cues"]}["cue-2"] == "until smooth"
    finally:
        await async_engine.dispose()


# ----------------------------------------------------------------------------------------
# Undo
# ----------------------------------------------------------------------------------------

EDITED_CUES = [
    {**CUES[0], "text": "I whisk the matcha"},
    CUES[1],
]


async def _update_captions(monkeypatch, user_id, thread_id, session_id):  # noqa: ANN001, ANN202
    """Run a captions-only update and finish its (simulated) render. Returns the job id."""
    job_id = _seed_job(user_id, session_id)
    _planner_returns(
        monkeypatch, [{"op": "edit_caption", "cue_index": 0, "text": "I whisk the matcha"}]
    )
    accepted = await _submit(user_id, thread_id, scope=["captions"])
    await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    return job_id, accepted


def _undo_body(thread_id, *, block_revision=2, draft_revision=None) -> PlanSectionUndoBody:  # noqa: ANN001
    head = [d for d in _drafts(thread_id) if d["head"]][0]
    return PlanSectionUndoBody(
        expected_thread_revision=_revision(thread_id),
        expected_block_revision=block_revision,
        expected_draft_revision=head["rev"] if draft_revision is None else draft_revision,
    )


@pytest.mark.asyncio
async def test_section_undo_round_trip_restores_only_that_section_and_toggles(monkeypatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    try:
        job_id, _ = await _update_captions(monkeypatch, user_id, thread_id, session_id)
        _complete_render(
            job_id,
            thread_id,
            generation="generation-2",
            variant_patch={"caption_cues": copy.deepcopy(EDITED_CUES)},
        )
        _plan_block(thread_id, job_id, "captions", revision=2, changed=True)
        before_drafts = _drafts(thread_id)

        async with AsyncSessionLocal() as db:
            out = await plan_review.undo_section(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                section_id="captions",
                body=_undo_body(thread_id),
            )
        assert out.section_id == "captions" and out.turn_id
        drafts = _drafts(thread_id)
        assert len(drafts) == len(before_drafts) + 1
        restored = drafts[-1]
        assert restored["head"] and restored["parent"] == before_drafts[-1]["id"]
        lanes = restored["snap"]["editor_payload"]
        assert lanes["base_generation"] == "generation-2"
        assert {c["id"]: c["text"] for c in lanes["caption_cues"]} == {
            "cue-1": CUES[0]["text"],
            "cue-2": CUES[1]["text"],
        }
        assert set(lanes) == {"base_generation", "caption_cues"}  # no other lane moved

        with sync_session() as db:
            turn = db.get(CreatorAgentTurn, uuid.UUID(out.turn_id))
            assert turn.status == "awaiting_approval"
            approval = db.execute(
                select(CreatorAgentApproval).where(CreatorAgentApproval.turn_id == turn.id)
            ).scalar_one()
            assert approval.status == "pending" and approval.draft_id == restored["id"]
            source = db.get(CreationThreadEvent, turn.source_event_id)
            assert source.payload["scope"] == ["captions"]
            kinds = [
                e.event_type
                for e in db.execute(
                    select(CreationThreadEvent)
                    .where(CreationThreadEvent.thread_id == thread_id)
                    .order_by(CreationThreadEvent.sequence)
                ).scalars()
            ]
            assert kinds[-2:] == ["draft_applied", "approval_requested"]

        # Undo is itself undoable (toggles): the restoring draft carries the way back.
        with sync_session() as db:
            execution = db.get(CreatorAgentExecution, restored["exec"])
            toggle = plan_review.head_restore_record(execution.result)
        assert plan_review.record_sections(toggle) == ["captions"]
        rows = toggle["sections"]["captions"]["caption_cues"]["rows"]
        assert rows["cue-1"]["text"] == "I whisk the matcha"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_the_restoring_approval_can_be_approved(monkeypatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    try:
        job_id, _ = await _update_captions(monkeypatch, user_id, thread_id, session_id)
        _complete_render(
            job_id,
            thread_id,
            generation="generation-2",
            variant_patch={"caption_cues": copy.deepcopy(EDITED_CUES)},
        )
        _plan_block(thread_id, job_id, "captions", revision=2, changed=True)
        async with AsyncSessionLocal() as db:
            out = await plan_review.undo_section(
                db,
                thread_id=thread_id,
                creator_id=user_id,
                section_id="captions",
                body=_undo_body(thread_id),
            )
        with sync_session() as db:
            approval = db.execute(
                select(CreatorAgentApproval).where(
                    CreatorAgentApproval.turn_id == uuid.UUID(out.turn_id)
                )
            ).scalar_one()
            fingerprint = approval_fingerprint(approval)
            approval_id = approval.id
            draft_revision = approval.draft_revision
        async with AsyncSessionLocal() as db:
            decided, _ = await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="approve",
                body=ApprovalDecisionBody(
                    expected_thread_revision=_revision(thread_id),
                    expected_draft_revision=draft_revision,
                    expected_approval_fingerprint=fingerprint,
                ),
            )
        assert decided.status == "approved"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_section_undo_conflicts(monkeypatch) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    try:
        job_id, _ = await _update_captions(monkeypatch, user_id, thread_id, session_id)

        async def attempt(section="captions", **body_kwargs):  # noqa: ANN003, ANN202
            async with AsyncSessionLocal() as db:
                with pytest.raises(RuntimeFailure) as caught:
                    await plan_review.undo_section(
                        db,
                        thread_id=thread_id,
                        creator_id=user_id,
                        section_id=section,
                        body=_undo_body(thread_id, **body_kwargs),
                    )
            return caught.value

        # A render (the approval) is still pending: nothing to undo yet.
        failure = await attempt()
        assert (failure.status_code, failure.code) == (409, "plan_section_not_undoable")

        _complete_render(
            job_id,
            thread_id,
            generation="generation-2",
            variant_patch={"caption_cues": copy.deepcopy(EDITED_CUES)},
        )
        # No decided block yet.
        assert (await attempt()).code == "plan_section_not_undoable"

        _plan_block(thread_id, job_id, "captions", revision=2, changed=True)
        _plan_block(thread_id, job_id, "music", revision=1, changed=False)
        stale = await attempt(block_revision=1)
        assert (stale.status_code, stale.code, stale.current_revision) == (
            409,
            "plan_section_stale",
            2,
        )
        assert (await attempt(draft_revision=99)).code == "draft_stale"
        # Unchanged / never-updated sections have no previous value.
        assert (
            await attempt(section="music", block_revision=1)
        ).code == "plan_section_not_undoable"
        assert (await attempt(section="sfx", block_revision=1)).code == "plan_section_not_undoable"
        unknown = await attempt(section="nope")
        assert (unknown.status_code, unknown.code) == (404, "plan_section_not_found")
        post = await attempt(section="post_caption")
        assert (post.status_code, post.code) == (422, "scope_section_unsupported")

        # The thread moved on after the client last read it.
        body = _undo_body(thread_id)
        _plan_block(thread_id, job_id, "look", revision=1, changed=False)
        with sync_session() as db:
            db.add(
                CreationThreadEvent(
                    thread_id=thread_id,
                    sequence=900,
                    revision=_revision(thread_id) + 1,
                    role="assistant",
                    event_type="assistant_response",
                    content="hi",
                )
            )
            db.get(CreationThread, thread_id).revision = _revision(thread_id) + 1
            db.commit()
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as caught:
                await plan_review.undo_section(
                    db, thread_id=thread_id, creator_id=user_id, section_id="captions", body=body
                )
        assert caught.value.code == "thread_revision_stale"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_undo_all_with_render_restores_every_updated_section_and_plain_undo_is_unchanged(
    monkeypatch,
) -> None:
    user_id, thread_id, session_id = _seed_runtime_project()
    try:
        job_id, _ = await _update_captions(monkeypatch, user_id, thread_id, session_id)
        _complete_render(
            job_id,
            thread_id,
            generation="generation-2",
            variant_patch={"caption_cues": copy.deepcopy(EDITED_CUES)},
        )
        head = [d for d in _drafts(thread_id) if d["head"]][0]

        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as caught:
                await plan_review.undo_all_with_render(
                    db, thread_id=thread_id, creator_id=user_id, expected_revision=head["rev"] + 5
                )
        assert caught.value.code == "draft_stale"

        async with AsyncSessionLocal() as db:
            restored = await plan_review.undo_all_with_render(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=head["rev"]
            )
        assert restored.can_undo is True
        payload = restored.snapshot["editor_payload"]
        assert payload["caption_cues"][0]["text"] == CUES[0]["text"]
        with sync_session() as db:
            kinds = [
                e.event_type
                for e in db.execute(
                    select(CreationThreadEvent)
                    .where(CreationThreadEvent.thread_id == thread_id)
                    .order_by(CreationThreadEvent.sequence)
                ).scalars()
            ]
            assert kinds[-2:] == ["draft_undone", "approval_requested"]

        # render=false keeps today's behaviour: restore the parent snapshot, render nothing.
        _complete_render(job_id, thread_id, generation="generation-3", variant_patch={})
        head = [d for d in _drafts(thread_id) if d["head"]][0]
        parent = next(d for d in _drafts(thread_id) if d["id"] == head["parent"])
        with sync_session() as db:
            turns_before = len(
                db.execute(select(CreatorAgentTurn).where(CreatorAgentTurn.thread_id == thread_id))
                .scalars()
                .all()
            )
        async with AsyncSessionLocal() as db:
            plain, successor = await undo_draft(
                db, thread_id=thread_id, creator_id=user_id, expected_revision=head["rev"]
            )
        assert successor is None and plain.snapshot == parent["snap"]
        with sync_session() as db:
            turns_after = len(
                db.execute(select(CreatorAgentTurn).where(CreatorAgentTurn.thread_id == thread_id))
                .scalars()
                .all()
            )
        assert turns_after == turns_before  # no render turn minted
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_undo_all_render_flag_off_is_404(monkeypatch) -> None:
    monkeypatch.setattr(settings, "live_plan_review_enabled", False)
    user_id, thread_id, session_id = _seed_runtime_project()
    _seed_job(user_id, session_id)
    try:
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as caught:
                await plan_review.undo_all_with_render(
                    db, thread_id=thread_id, creator_id=user_id, expected_revision=0
                )
            assert (caught.value.status_code, caught.value.code) == (
                404,
                "live_plan_review_unavailable",
            )
        async with AsyncSessionLocal() as db:
            with pytest.raises(RuntimeFailure) as caught:
                await plan_review.undo_section(
                    db,
                    thread_id=thread_id,
                    creator_id=user_id,
                    section_id="captions",
                    body=PlanSectionUndoBody(
                        expected_thread_revision=0,
                        expected_block_revision=1,
                        expected_draft_revision=0,
                    ),
                )
            assert caught.value.code == "live_plan_review_unavailable"
    finally:
        await async_engine.dispose()


# ----------------------------------------------------------------------------------------
# Map drift guard
# ----------------------------------------------------------------------------------------


def test_every_editor_op_has_a_section_or_is_explicitly_never_scoped() -> None:
    """A new copilot op that nobody mapped would be silently refused inside every scope (or,
    worse, allowed): force a conscious decision."""
    from app.agents import edit_copilot
    from app.agents.editor_ops_v2 import REGISTRY, lane_specs

    plan_review_ops = {name for names in plan_review.SECTION_OPS.values() for name in names}
    mapped = (
        plan_review_ops
        | plan_review.TEXT_BAR_OPS
        | plan_review.NEVER_IN_SCOPE
        | {plan_review.CAPTION_META_OP}
    )
    known = (
        set(edit_copilot._VALID_OPS) | {spec.name for spec in lane_specs()} | set(REGISTRY.new_ops)
    )
    assert known - mapped == set()


def test_every_op_of_a_section_is_reachable_with_that_sections_families() -> None:
    """Layer 1 must not make a section's own op impossible (it was: set_look_preset under
    `look`, motion blocks under `overlays`): the model would never see it and the creator's
    flagged section would silently do nothing."""
    from app.agents import edit_copilot

    # Server-side ops with their own path / flag, not a family question.
    exempt = {"apply_speech_cut_candidate", "apply_custom_effect"}
    for section, ops in plan_review.SECTION_OPS.items():
        snapshot = {
            "allowed_op_families": sorted(plan_review.SECTION_FAMILIES[section]),
            "editor_ops_version": 2,
            "label_facts": True,
        }
        unreachable = sorted(
            op for op in ops - exempt if not edit_copilot._family_allowed(op, snapshot)
        )
        assert unreachable == [], (section, unreachable)


def test_text_bar_sections() -> None:
    cases = {
        "narration-caption-1": "captions",
        "guided-title": "title",
        "hook": "overlays",  # no role, no clip link: a free text
        "clip-label-3": "clips",
        "montage-text-1": "clips",
        "kria-x": "overlays",
    }
    for bar_id, section in cases.items():
        assert plan_review.section_of_text_bar({"id": bar_id}) == section
    assert plan_review.section_of_text_bar({"id": "a", "caption_cue": True}) == "captions"
    assert plan_review.section_of_text_bar({"id": "a", "role": "hook"}) == "title"
    assert plan_review.section_of_text_bar({"id": "a", "clip_id": "c1"}) == "clips"


@pytest.mark.parametrize(
    "op",
    [
        "create_split_screen",
        "update_split_screen",
        "assign_split_panel",
        "swap_split_panels",
        "remove_split_screen",
        "duplicate_clip",
        "replace_clip_source",
        "move_split_clip",
        "move_split_screen",
    ],
)
def test_split_commands_belong_to_clips_not_unrelated_sections(op):
    # A supported clip edit must be reachable, but cannot escape a title/look-only scope.
    command = {"op": op}
    assert plan_review.filter_ops_to_scope([command], {}, ["clips"]) == ([command], [])
    for scope in (["title"], ["look"], ["overlays"]):
        kept, dropped = plan_review.filter_ops_to_scope([command], {}, scope)
        assert kept == []
        assert dropped == [{"op": op, "section": "clips"}]


def test_split_definitions_and_rows_are_scoped_and_undone_together():
    # Stripping only rows leaves dangling definitions; Undo of first creation must
    # explicitly clear definitions even when the prior document omitted the key.
    before = {"timeline_slots": [{"slot_id": "a", "duration_s": 4}]}
    data = {
        "timeline_slots": [
            {
                "slot_id": "a",
                "duration_s": 4,
                "split_section_id": "s",
                "split_panel_index": 0,
                "split_offset_s": 0,
            }
        ],
        "split_screen_sections": [{"id": "s", "duration_s": 4, "panel_count": 2}],
    }
    allowed, _ = plan_review.repair_payload(data, before, ["clips"])
    assert allowed == data
    for definitions in (data["split_screen_sections"], []):
        denied, _ = plan_review.repair_payload(
            {**data, "split_screen_sections": definitions}, before, ["title"]
        )
        assert "timeline_slots" not in denied
        assert "split_screen_sections" not in denied
    record = plan_review.build_restore_record(data, before, ["clips"], turn_id="t", base_job_id="j")
    restored, toggle = plan_review.restore_section(record, "clips", data)
    assert restored == {**before, "split_screen_sections": []}
    redone, _ = plan_review.restore_section(toggle, "clips", restored)
    assert redone == data


@pytest.mark.parametrize("canonical_clear", [{}, {"split_screen_sections": None}])
def test_unstack_undo_redo_keeps_explicit_clear_after_durable_normalization(canonical_clear):
    split = {
        "timeline_slots": [
            {
                "slot_id": "a",
                "clip_index": 0,
                "in_s": 0,
                "split_section_id": "s",
                "split_panel_index": 0,
                "split_offset_s": 0,
                "duration_s": 4,
            }
        ],
        "split_screen_sections": [{"id": "s", "duration_s": 4, "panel_count": 2}],
    }
    unstacked = {
        "timeline_slots": [{"slot_id": "a", "clip_index": 0, "in_s": 0, "duration_s": 4}],
        "split_screen_sections": [],
    }
    record = plan_review.build_restore_record(
        unstacked, split, ["clips"], turn_id="t", base_job_id="j"
    )
    head = {"timeline_slots": unstacked["timeline_slots"], **canonical_clear}
    restored, redo = plan_review.restore_section(record, "clips", head)
    assert restored == split
    reapplied, _ = plan_review.restore_section(redo, "clips", restored)
    assert reapplied == unstacked
    # Real commit DTO serialization must retain the clear: None would be omitted and
    # the server would inherit the just-restored split definitions.
    from app.routes.generative_jobs import EditorCommitRequest

    wire = EditorCommitRequest.model_validate(reapplied).model_dump(mode="json", exclude_none=True)
    assert wire["split_screen_sections"] == []


def test_clip_scope_carries_only_trusted_rebase_and_undo_preserves_later_content():
    from app.routes.generative_jobs import EditorCommitRequest
    from app.services.kria_editor_ops import CompiledEditorDraft

    before = {
        "text_elements": [{"id": "global", "text": "Original", "start_s": 0, "end_s": 1}],
        "caption_cues": [
            {
                "id": "cue",
                "text": "Words",
                "start_s": 0,
                "end_s": 1,
                "words": [{"text": "Words", "start_s": 0, "end_s": 1}],
            }
        ],
        "drawings": [
            {
                "id": "drawing",
                "start_s": 0,
                "end_s": 1,
                "canvas_aspect": 0.5625,
                "strokes": [
                    {"color": "#FF0000", "width_frac": 0.01, "points": [[0.1, 0.1], [0.9, 0.9]]}
                ],
            }
        ],
        "speaker_background": {
            "kind": "color",
            "color": "#000000",
            "full_screen_windows": [{"start_s": 0, "end_s": 1}],
        },
    }
    auto_before = copy.deepcopy(before)
    auto_after = copy.deepcopy(before)
    for lane in ("text_elements", "caption_cues", "drawings"):
        auto_after[lane][0].update(start_s=2, end_s=3)
    auto_after["caption_cues"][0]["words"][0].update(start_s=2, end_s=3)
    auto_after["speaker_background"]["full_screen_windows"] = [{"start_s": 2, "end_s": 3}]
    payload = copy.deepcopy(auto_after)
    payload["text_elements"][0]["text"] = "Unauthorized rewrite"
    payload["speaker_background"]["color"] = "#FFFFFF"
    compiled = CompiledEditorDraft(
        payload=EditorCommitRequest.model_validate(payload),
        changes=[],
        before=before,
        timeline_rebase={
            k: (auto_before[k], auto_after[k])
            for k in auto_after
            if auto_before[k] != auto_after[k]
        },
    )
    repaired, _ = plan_review.repair_compiled(compiled, ["clips"])
    data = repaired.payload.model_dump(mode="json", exclude_none=True)
    assert data["text_elements"][0]["text"] == "Original"
    assert data["text_elements"][0]["start_s"] == 2
    assert data["caption_cues"][0]["words"][0]["start_s"] == 2
    assert data["speaker_background"]["color"] == "#000000"
    record = plan_review.build_restore_record(
        data,
        repaired.before,
        ["clips"],
        turn_id="t",
        base_job_id="j",
        timeline_rebase=repaired.timeline_rebase,
    )
    current = copy.deepcopy(data)
    current["text_elements"][0]["text"] = "Later wording"
    current["speaker_background"]["color"] = "#123456"
    current["drawings"][0]["strokes"][0]["color"] = "#00FF00"
    undone, redo = plan_review.restore_section(record, "clips", current)
    assert undone["text_elements"][0]["text"] == "Later wording"
    assert undone["text_elements"][0]["start_s"] == 0
    assert undone["speaker_background"]["color"] == "#123456"
    assert undone["drawings"][0]["start_s"] == 0
    assert undone["drawings"][0]["strokes"][0]["color"] == "#00FF00"
    redone, _ = plan_review.restore_section(redo, "clips", undone)
    assert redone["text_elements"][0]["start_s"] == 2
    assert redone["text_elements"][0]["text"] == "Later wording"


def test_dependent_collapsed_windows_restore_and_redo_without_replacing_background():
    before = {
        "kind": "color",
        "color": "#000000",
        "full_screen_windows": [{"start_s": 1, "end_s": 2}],
    }
    after = {**before, "full_screen_windows": []}
    record = plan_review.build_restore_record(
        {"speaker_background": after},
        {"speaker_background": before},
        ["clips"],
        turn_id="t",
        base_job_id="j",
        timeline_rebase={"speaker_background": (before, after)},
    )
    restored, redo = plan_review.restore_section(
        record, "clips", {"speaker_background": {**after, "color": "#112233"}}
    )
    assert restored["speaker_background"] == {**before, "color": "#112233"}
    cleared, _ = plan_review.restore_section(redo, "clips", restored)
    assert cleared["speaker_background"] == {**after, "color": "#112233"}


@pytest.mark.parametrize("order", [("clips", "title"), ("title", "clips")])
def test_mixed_title_and_clip_undo_orders_keep_independent_timing(order):
    before = {
        "text_elements": [
            {"id": "hook", "role": "generative_intro", "text": "Old", "start_s": 0, "end_s": 1}
        ]
    }
    pre_rebase = copy.deepcopy(before)
    pre_rebase["text_elements"][0]["text"] = "New"
    after = copy.deepcopy(pre_rebase)
    after["text_elements"][0].update(start_s=2, end_s=3)
    record = plan_review.build_restore_record(
        after,
        before,
        ["clips", "title"],
        turn_id="t",
        base_job_id="j",
        timeline_rebase={"text_elements": (pre_rebase["text_elements"], after["text_elements"])},
    )
    current = copy.deepcopy(after)
    toggles = {}
    for section in order:
        patch, toggles[section] = plan_review.restore_section(record, section, current)
        current.update(patch)
    assert current == before
    for section in reversed(order):
        patch, _ = plan_review.restore_section(toggles[section], section, current)
        current.update(patch)
    assert current == after


def test_redo_collapsed_anonymous_window_does_not_delete_replacement():
    a = {"start_s": 1, "end_s": 2, "media_id": "A"}
    b = {"start_s": 1, "end_s": 2, "media_id": "B"}
    record = {
        "sections": {
            "clips": {"speaker_background": {"timing": [{"windows": []}, {"windows": [a]}]}}
        }
    }
    restored, redo = plan_review.restore_section(
        record, "clips", {"speaker_background": {"windows": []}}
    )
    assert restored["speaker_background"]["windows"] == [a]
    replaced = {"speaker_background": {"windows": [b]}}
    replay, _ = plan_review.restore_section(redo, "clips", replaced)
    assert replay == replaced


def test_collapsed_window_undo_does_not_insert_old_entry_into_replacement_list():
    a = {"start_s": 1, "end_s": 2, "media_id": "A"}
    b = {"start_s": 1, "end_s": 2, "media_id": "B"}
    record = {
        "sections": {
            "clips": {"speaker_background": {"timing": [{"windows": []}, {"windows": [a]}]}}
        }
    }
    current = {"speaker_background": {"windows": [b]}}
    restored, _ = plan_review.restore_section(record, "clips", current)
    assert restored == current


def test_absent_collapsed_window_key_survives_scope_repair_and_undo():
    from app.routes.generative_jobs import EditorCommitRequest
    from app.services.kria_editor_ops import CompiledEditorDraft

    old = {"kind": "color", "color": "#000000", "full_screen_windows": [{"start_s": 1, "end_s": 2}]}
    new = {"kind": "color", "color": "#000000"}
    compiled = CompiledEditorDraft(
        payload=EditorCommitRequest(speaker_background=new),
        changes=[],
        before={"speaker_background": old},
        timeline_rebase={"speaker_background": (old, new)},
    )
    repaired, _ = plan_review.repair_compiled(compiled, ["clips"])
    from app.services.kria_editor_ops import editor_payload_dump

    data = editor_payload_dump(repaired.payload)
    assert not data["speaker_background"].get("full_screen_windows")
    record = plan_review.build_restore_record(
        data,
        repaired.before,
        ["clips"],
        turn_id="t",
        base_job_id="j",
        timeline_rebase=repaired.timeline_rebase,
    )
    data["speaker_background"]["color"] = "#112233"
    restored, redo = plan_review.restore_section(record, "clips", data)
    assert restored["speaker_background"]["full_screen_windows"] == old["full_screen_windows"]
    cleared, _ = plan_review.restore_section(redo, "clips", restored)
    assert not cleared["speaker_background"].get("full_screen_windows")
    assert cleared["speaker_background"]["color"] == "#112233"


@pytest.mark.parametrize("order", [("clips", "look"), ("look", "clips")])
def test_mixed_background_style_undo_preserves_distinct_anonymous_windows(order):
    before = {
        "speaker_background": {
            "kind": "color",
            "color": "#000000",
            "full_screen_windows": [{"start_s": 0, "end_s": 1}, {"start_s": 2, "end_s": 3}],
        }
    }
    pre_rebase = copy.deepcopy(before)
    pre_rebase["speaker_background"]["color"] = "#FFFFFF"
    after = copy.deepcopy(pre_rebase)
    after["speaker_background"]["full_screen_windows"] = [
        {"start_s": 4, "end_s": 5},
        {"start_s": 6, "end_s": 7},
    ]
    record = plan_review.build_restore_record(
        after,
        before,
        ["clips", "look"],
        turn_id="t",
        base_job_id="j",
        timeline_rebase={
            "speaker_background": (pre_rebase["speaker_background"], after["speaker_background"])
        },
    )
    current = copy.deepcopy(after)
    toggles = {}
    for section in order:
        patch, toggles[section] = plan_review.restore_section(record, section, current)
        current.update(patch)
    assert current == before
    for section in reversed(order):
        patch, _ = plan_review.restore_section(toggles[section], section, current)
        current.update(patch)
    assert current == after


@pytest.mark.parametrize("order", [("clips", "look"), ("look", "clips")])
def test_mixed_background_color_and_omitted_collapse_undo_orders(order):
    from app.routes.generative_jobs import EditorCommitRequest
    from app.services.kria_editor_ops import editor_payload_dump

    before = {
        "speaker_background": {
            "kind": "color",
            "color": "#000000",
            "full_screen_windows": [{"start_s": 1, "end_s": 2}],
        }
    }
    pre_rebase = copy.deepcopy(before)
    pre_rebase["speaker_background"]["color"] = "#FFFFFF"
    after = editor_payload_dump(
        EditorCommitRequest(speaker_background={"kind": "color", "color": "#FFFFFF"})
    )
    after = {"speaker_background": after["speaker_background"]}
    # Canonical DTO output omits the collapsed temporal list entirely.
    assert "full_screen_windows" not in after["speaker_background"]
    record = plan_review.build_restore_record(
        after,
        before,
        ["clips", "look"],
        turn_id="t",
        base_job_id="j",
        timeline_rebase={
            "speaker_background": (pre_rebase["speaker_background"], after["speaker_background"])
        },
    )
    current = copy.deepcopy(after)
    toggles = {}
    for section in order:
        patch, toggles[section] = plan_review.restore_section(record, section, current)
        current.update(patch)
    assert current["speaker_background"]["color"] == "#000000"
    assert (
        current["speaker_background"]["full_screen_windows"]
        == before["speaker_background"]["full_screen_windows"]
    )
    for section in reversed(order):
        patch, _ = plan_review.restore_section(toggles[section], section, current)
        current.update(patch)
    assert current["speaker_background"]["color"] == "#FFFFFF"
    assert not current["speaker_background"].get("full_screen_windows")
