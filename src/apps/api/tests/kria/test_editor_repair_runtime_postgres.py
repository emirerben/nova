"""Real draft transactions and compiler; planner and reviewer are offline selections.

Failure matrix: confirmation, unavailable, deadline, old preservation, stale generation,
head and brief, missing capability, lock scope, unreadable span. No native render claim.
"""

# ruff: noqa: F811

import asyncio
import copy
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents._runtime import RunContext
from app.config import settings
from app.database import sync_session
from app.kria.brief import BriefUpdate
from app.kria.planner import PlannedKriaTurn, adapt_editor_action
from app.models import CreationThread, CreativeBriefVersion, CreatorEditDraft, Job
from app.tasks import kria_runtime
from tests.kria.test_duration_text_preservation_postgres import project  # noqa: F401
from tests.kria.test_editor_state_turns_postgres import _drafts, _submit


def setup_plan(monkeypatch, project, *, target=12, updates=None, capability=True, retime=True):
    user_id, thread_id, session_id, job_id, rows, _, _ = project
    ops = [{"op": "set_total_duration", "target_s": target, "strategy": "proportional"}]
    if retime:
        ops.append(
            {
                "op": "retime_text_sequence",
                "selector": {"ids": [r["id"] for r in rows]},
                "target_ids": [r["id"] for r in rows],
                "expected_count": len(rows),
                "start_s": 0,
                "end_s": target,
                "fit_to_video": True,
                "gap_policy": "scale",
            }
        )
    ops.append(
        {
            "op": "rewrite_text",
            "selector": {"ids": [rows[0]["id"]]},
            "target_ids": [rows[0]["id"]],
            "expected_count": 1,
            "text": "Bad replacement",
        }
    )

    async def planner(*args, **kwargs):
        return PlannedKriaTurn(
            plan=adapt_editor_action(reply="Changed all wording", ops=ops),
            manifest_hash="a" * 64,
            context_hash="b" * 64,
            brief_updates=tuple(
                updates
                or [
                    BriefUpdate(
                        kind="timing",
                        scope="global",
                        description="Timing only",
                        facts={"editor_timing_only": True},
                    )
                ]
            ),
            editor_repair_run_context=RunContext(
                job_id=str(job_id),
                creator_agent_session_id=str(session_id),
                request_id=str(kwargs["turn_id"]),
            )
            if capability
            else None,
        )

    monkeypatch.setattr(kria_runtime, "_plan_with_live_agent", planner)
    monkeypatch.setattr(settings, "creator_repair_reviewer_enabled", True)


async def run(project):
    user_id, thread_id, *_ = project
    accepted = await _submit(user_id, thread_id, "Fill the whole video with the original sequence")
    return await asyncio.to_thread(kria_runtime.run_kria_turn.run, accepted.turn_id)


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["requires_confirmation", "unavailable", "keep_plan", "timeout"])
async def test_review_does_not_apply_rejected_candidate(project, monkeypatch, status):
    setup_plan(monkeypatch, project)
    before = _drafts(project[1])
    calls = []

    def review(context, **kwargs):
        calls.append(context)
        if status == "timeout":
            raise TimeoutError()
        return SimpleNamespace(status=status, candidate_id=None, evidence_ids=())

    monkeypatch.setattr("app.services.editor_repair_reviewer.review_editor_repair", review)
    assert (await run(project))["status"] == "completed"
    assert len(calls) == 1
    assert _drafts(project[1]) == before
    project[-1].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["generation", "head", "brief"])
async def test_changed_review_basis_cannot_write_draft(project, monkeypatch, change):
    setup_plan(monkeypatch, project)
    before = _drafts(project[1])

    calls = []

    def review(context, **kwargs):
        calls.append(context)
        with sync_session() as db:
            # NOWAIT proves review runs after the reader transaction closes, outside writer locks.
            job = db.scalar(select(Job).where(Job.id == project[3]).with_for_update(nowait=True))
            head = db.scalar(
                select(CreatorEditDraft)
                .where(CreatorEditDraft.thread_id == project[1], CreatorEditDraft.is_head.is_(True))
                .with_for_update(nowait=True)
            )
            db.scalar(
                select(CreationThread)
                .where(CreationThread.id == project[1])
                .with_for_update(nowait=True)
            )
            if change == "generation":
                assembly = copy.deepcopy(job.assembly_plan)
                assembly["variants"][0]["generation_id"] = "new-generation"
                job.assembly_plan = assembly
            elif change == "head":
                head.snapshot_json = {**head.snapshot_json, "intent": "Concurrent draft"}
            else:
                db.add(CreativeBriefVersion(thread_id=project[1], version=1, requirements=[]))
            db.commit()
        return SimpleNamespace(
            status="accept_candidate",
            candidate_id=context.candidate_id,
            evidence_ids=("timing-only-compiler",),
        )

    monkeypatch.setattr("app.services.editor_repair_reviewer.review_editor_repair", review)
    assert (await run(project))["status"] == "completed"
    assert len(calls) == 1
    assert len(_drafts(project[1])) == len(before)
    project[-1].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "guard", ["capability", "duration_only", "preserve", "short", "missing_sources"]
)
async def test_ineligible_candidate_never_calls_reviewer(project, monkeypatch, guard):
    updates = None
    if guard == "missing_sources":
        with sync_session() as db:
            job = db.get(Job, project[3])
            assembly = copy.deepcopy(job.assembly_plan)
            assembly["variants"][0]["guided_edit_revision"]["sources"] = []
            job.assembly_plan = assembly
            db.commit()
    if guard == "preserve":
        updates = [
            BriefUpdate(
                kind="timing",
                scope="global",
                description="Preserve",
                facts={"editor_timing_only": True, "preserve_editor_text": True},
            )
        ]
    setup_plan(
        monkeypatch,
        project,
        target=1 if guard == "short" else 12,
        updates=updates,
        capability=guard != "capability",
        retime=guard != "duration_only",
    )

    def forbidden(*args, **kwargs):
        pytest.fail("Ineligible candidate called reviewer")

    monkeypatch.setattr("app.services.editor_repair_reviewer.review_editor_repair", forbidden)
    assert (await run(project))["status"] == "completed"
    project[-1].assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "facts",
    [
        {"preserve_editor_text": True},
        {"sequence_timing": {"mode": "absolute", "source_id": "opening"}},
    ],
)
async def test_live_prior_preservation_cannot_be_overridden_by_candidate(
    project, monkeypatch, facts
):
    from app.kria.brief import BriefRequirement

    setup_plan(monkeypatch, project)
    with sync_session() as db:
        req = BriefRequirement(
            id="r1",
            kind="timing",
            scope="global",
            description="Preserve existing timing",
            facts=facts,
        )
        db.add(
            CreativeBriefVersion(
                thread_id=project[1], version=1, requirements=[req.model_dump(mode="json")]
            )
        )
        db.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Prior preservation called reviewer")

    monkeypatch.setattr("app.services.editor_repair_reviewer.review_editor_repair", forbidden)
    before = _drafts(project[1])
    assert (await run(project))["status"] == "completed"
    assert _drafts(project[1]) == before


@pytest.mark.asyncio
async def test_missing_head_never_reviews_or_promises_undo(project, monkeypatch):
    setup_plan(monkeypatch, project)
    with sync_session() as db:
        for head in db.scalars(
            select(CreatorEditDraft).where(CreatorEditDraft.thread_id == project[1])
        ):
            head.is_head = False
        db.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Missing Undo parent called reviewer")

    monkeypatch.setattr("app.services.editor_repair_reviewer.review_editor_repair", forbidden)
    before = _drafts(project[1])
    assert (await run(project))["status"] == "completed"
    assert _drafts(project[1]) == before


@pytest.mark.asyncio
async def test_changed_ownership_epoch_never_calls_reviewer(project, monkeypatch):
    setup_plan(monkeypatch, project)
    with sync_session() as db:
        job = db.get(Job, project[3])
        job.content_plan_ownership_epoch = 99
        db.commit()

    def forbidden(*args, **kwargs):
        pytest.fail("Stale ownership called reviewer")

    monkeypatch.setattr("app.services.editor_repair_reviewer.review_editor_repair", forbidden)
    await run(project)


@pytest.mark.asyncio
async def test_scoped_compiler_cannot_publish_partial_repair(project, monkeypatch):
    import uuid

    from app.database import AsyncSessionLocal
    from app.kria.api_schemas import SubmitTurnBody
    from app.kria.runtime import submit_turn

    setup_plan(monkeypatch, project)
    calls = []

    def review(context, **kwargs):
        calls.append(context)
        return SimpleNamespace(
            status="accept_candidate",
            candidate_id=context.candidate_id,
            evidence_ids=("timing-only-compiler",),
        )

    monkeypatch.setattr("app.services.editor_repair_reviewer.review_editor_repair", review)
    before = _drafts(project[1])
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    with sync_session() as db:
        revision = db.get(CreationThread, project[1]).revision
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=project[1],
            creator_id=project[0],
            body=SubmitTurnBody(
                message="Adjust only clip timing",
                client_event_id=str(uuid.uuid4()),
                expected_thread_revision=revision,
                scope=["clips"],
            ),
        )
    assert (await asyncio.to_thread(kria_runtime.run_kria_turn.run, accepted.turn_id))[
        "status"
    ] == "completed"
    assert len(calls) == 1
    assert _drafts(project[1]) == before
    project[-1].assert_not_called()
