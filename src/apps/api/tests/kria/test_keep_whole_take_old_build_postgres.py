"""KRI-1291: an app build without the "Keep original speech" card keeps the take whole.

Creator request (Don Julio lunch T1, KRI-971): "Use the whole clip as it is, don't trim
anything." on one 8.2 s Talking take. The brief records it as r1 ``{kind: style, scope: global,
facts: {keep_whole_take: true}}``. A build below protocol 44 ignores
``speech_cleanup.recommended_choice`` and leads with "Clean up speech and create", which sends an
explicit ``clean``. The thread read now offers that build no choice, so its card shows the plain
"Create this video" (analysis id, no choice), and the approval answers keep_original from that
approval's own pinned brief.

Real Postgres, real turn/approval/claim code, real thread read (`_response`) and the real
dispatch step that records the decision (`_speech_cleanup_dispatch_snapshot`). Faked: the
planner's output (the incident's recorded brief and strategy), the speech-check worker (the row
is marked ready the way it finishes) and the render enqueue.

Failure modes, written before the code:

1. An old build still sees the choice, and one tap cuts the take (the incident).
2. A protocol-44 build loses the choice or its "Keep original speech" lead.
3. The web card (no protocol header) changes.
4. The old build's plain button dead-ends on 409 speech_cleanup_choice_required (KRI-292).
5. The server answers "clean", or overrides an explicit tap.
6. A polled thread read records a decision.
7. A clip-scoped "keep this clip whole" answers for the whole take.
8. A later "clean up my pauses" after a kept take is answered by the old keep, so the card
   never asks again.
9. A later "keep the whole clip" after a recorded clean is still cut.
10. A retried approval, or a re-run of the dispatch claim, records a different answer.
11. The audit says the creator answered the cleanup question at the card.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria.api_schemas import ApprovalDecisionBody, SubmitTurnBody
from app.kria.brief import BriefUpdate
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.runtime import RuntimeFailure, approval_fingerprint, decide_approval, submit_turn
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorPreparedExecution,
    Job,
    Persona,
    PlanItem,
    SpeechCleanupAnalysis,
    User,
)
from app.routes.creation_threads import _response
from app.services.client_protocol import clear_request_context, set_client_protocol
from app.tasks.content_plan_build import DispatchResult, _speech_cleanup_dispatch_snapshot
from app.tasks.kria_runtime import execute_kria_approval, run_kria_turn

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

INCIDENT_BUILD = 22  # the client protocol recorded in the KRI-971 replay fixture
CARD_BUILD = 44
WEB = None
TAKE_S = 8.2
MESSAGE = "Use the whole clip as it is, don't trim anything."


def _keep_whole(scope: str = "global") -> BriefUpdate:
    return BriefUpdate(
        kind="style",
        scope=scope,
        description="Use the whole clip as it is, don't trim anything",
        facts={"keep_whole_take": True},
    )


def _clean_up() -> BriefUpdate:
    return BriefUpdate(
        kind="audio",
        scope="global",
        description="Cut the long pauses",
        facts={"speech_cleanup": True},
    )


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    # The speech-check worker never runs here; `_checked_take` finishes the row instead.
    monkeypatch.setattr(
        "app.services.plan_item_media.publish_preflight_after_commit", lambda _id: True
    )


def _seed_talking_take() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    user_id, persona_id, plan_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    item_id, session_id, thread_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    take = f"users/{user_id}/don-julio-take.mov"
    with sync_session() as db:
        db.add(User(id=user_id, email=f"{user_id}@test.local"))
        db.flush()
        db.add(Persona(id=persona_id, user_id=user_id, persona_status="ready", persona={}))
        db.flush()
        db.add(ContentPlan(id=plan_id, user_id=user_id, persona_id=persona_id))
        db.flush()
        db.add(
            PlanItem(
                id=item_id,
                content_plan_id=plan_id,
                position=1,
                idea="Don Julio lunch",
                item_status="awaiting_clips",
                clip_gcs_paths=[take],
                clip_assignments=[
                    {
                        "gcs_path": take,
                        "media_id": "take-1",
                        "storage_generation": "1761490000000000",
                        "duration_s": TAKE_S,
                        "has_audio": True,
                    }
                ],
            )
        )
        db.flush()
        db.add(CreatorAgentSession(id=session_id, creator_id=user_id, plan_item_id=item_id))
        db.flush()
        db.add(
            CreationThread(
                id=thread_id,
                creator_id=user_id,
                runtime_version=2,
                content_plan_id=plan_id,
                active_plan_item_id=item_id,
                active_creator_agent_session_id=session_id,
                title="Don Julio lunch",
                revision=2,
                status="active",
                state={
                    "media": [{"media_id": "take-1", "filename": "don-julio-take.mov"}],
                    "media_count": 1,
                    "edit_format": "subtitled",
                },
            )
        )
        db.commit()
    return user_id, thread_id, item_id


async def _await_approval(
    monkeypatch: pytest.MonkeyPatch,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    brief_updates: tuple[BriefUpdate, ...],
    *,
    revision: int,
) -> tuple[uuid.UUID, ApprovalDecisionBody]:
    plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="native",
                edit_format="subtitled",
                audio_strategy="original_audio",
                pacing="balanced",
                render_program="native",
                selected_media_ids=["take-1"],
                rationale="Keeps the creator's own take whole with its original audio.",
            ),
            summary="Your whole take, as filmed, with captions.",
        )
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(
            plan=plan,
            manifest_hash="e" * 64,
            context_hash="f" * 64,
            brief_updates=brief_updates,
            brief_route="replan",
            brief_clip_ids=("take-1",),
        )

    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=MESSAGE,
                client_event_id=f"keep-{uuid.uuid4().hex}",
                expected_thread_revision=revision,
            ),
        )
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "awaiting_approval", result
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
            )
        ).scalar_one()
        pins = ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, thread_id).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
            speech_cleanup_aware=True,
        )
        return approval.id, pins


async def _approve(
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    approval_id: uuid.UUID,
    pins: ApprovalDecisionBody,
    **cleanup: object,
) -> None:
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=thread_id,
            approval_id=approval_id,
            creator_id=user_id,
            decision="approve",
            body=pins.model_copy(update=cleanup),
        )


async def _checked_take(
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    approval_id: uuid.UUID,
    pins: ApprovalDecisionBody,
    *,
    recorded: str | None = None,
) -> uuid.UUID:
    """The first approve schedules the speech check (409, approval stays pending); the check
    then finishes with the incident's 0.785 s of findings, optionally on an earlier decision."""
    with pytest.raises(RuntimeFailure) as first:
        await _approve(user_id, thread_id, approval_id, pins)
    assert first.value.code == "speech_cleanup_analysis_changed"
    with sync_session() as db:
        row = db.execute(
            select(SpeechCleanupAnalysis).where(
                SpeechCleanupAnalysis.plan_item_id == item_id,
                SpeechCleanupAnalysis.superseded_at.is_(None),
            )
        ).scalar_one()
        assert (row.source_kind, row.window_end_s) == ("embedded_spine", TAKE_S)
        row.status = "ready"
        row.candidate_count = 1
        row.category_counts = {"filler_sounds": 1, "long_pauses": 0, "retakes": 0}
        row.estimated_removed_ms = 785
        row.analysis_payload = {"language": "en"}
        row.completed_at = datetime.now(UTC)
        if recorded is not None:
            row.decision = recorded
            row.decision_at = datetime.now(UTC)
        db.commit()
        return row.id


async def _card(thread_id: uuid.UUID, protocol: int | None) -> dict:
    """The thread read a build at ``protocol`` polls (`GET ...?projection=full`)."""
    clear_request_context()
    if protocol is not None:
        set_client_protocol(protocol)
    try:
        async with AsyncSessionLocal() as db:
            projected = await _response(db, await db.get(CreationThread, thread_id))
    finally:
        clear_request_context()
    assert projected.speech_cleanup is not None
    return projected.speech_cleanup


def _recorded(analysis_id: uuid.UUID) -> tuple[str | None, object]:
    with sync_session() as db:
        row = db.get(SpeechCleanupAnalysis, analysis_id)
        return row.decision, row.decision_at


def _stash(approval_id: uuid.UUID) -> dict | None:
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, approval_id)
        execution = db.get(CreatorAgentExecution, uuid.UUID(approval.execution_ids[0]))
        return (execution.result or {}).get("speech_cleanup")


def _dispatch_recording_decision(
    monkeypatch: pytest.MonkeyPatch, user_id: uuid.UUID, item_id: uuid.UUID
) -> list[tuple]:
    """The render enqueue is faked; the step that records the decision under the item lock and
    returns the Job's cleanup contract is the real one."""
    dispatched: list[tuple] = []

    def _dispatch(*_args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        with sync_session() as db:
            item = db.get(PlanItem, item_id, with_for_update=True)
            contract, _snapshot, outcome = _speech_cleanup_dispatch_snapshot(
                db,
                item,
                analysis_id=kwargs["speech_cleanup_analysis_id"],
                choice=kwargs["speech_cleanup_choice"],
            )
            job = Job(
                user_id=user_id,
                status="queued",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                assembly_plan={
                    "variants": [],
                    "speech_cleanup_contract": contract,
                    "speech_cleanup_outcome": outcome,
                },
            )
            db.add(job)
            db.flush()
            item.current_job_id = job.id
            db.commit()
            dispatched.append((kwargs["speech_cleanup_choice"], contract, outcome))
            return DispatchResult("dispatched", job_id=str(job.id))

    monkeypatch.setattr("app.tasks.content_plan_build.dispatch_item_render_for", _dispatch)
    return dispatched


@pytest.mark.asyncio
@pytest.mark.parametrize("earlier", [None, "clean"], ids=["first-render", "after-a-clean"])
async def test_old_build_plain_create_keeps_the_whole_take(
    monkeypatch: pytest.MonkeyPatch, earlier: str | None
) -> None:
    """Modes 1-4, 6, 9-11: the incident request on the incident's build keeps all 8.2 s."""
    user_id, thread_id, item_id = _seed_talking_take()
    try:
        approval_id, pins = await _await_approval(
            monkeypatch, user_id, thread_id, (_keep_whole(),), revision=2
        )
        analysis_id = await _checked_take(
            user_id, thread_id, item_id, approval_id, pins, recorded=earlier
        )

        old = await _card(thread_id, INCIDENT_BUILD)
        new = await _card(thread_id, CARD_BUILD)
        web = await _card(thread_id, WEB)
        assert old["requires_choice"] is False, "the old card must not lead with Clean up"
        assert new["requires_choice"] is True and new["recommended_choice"] == "keep_original"
        assert web["requires_choice"] is (earlier is None), "the web card is unchanged"
        assert _recorded(analysis_id)[0] == earlier, "a thread read never records a decision"
        assert _stash(approval_id) is None

        # The old build's plain "Create this video": the analysis id, no choice.
        await _approve(
            user_id, thread_id, approval_id, pins, speech_cleanup_analysis_id=analysis_id
        )
        assert _stash(approval_id) == {
            "analysis_id": str(analysis_id),
            "choice": "keep_original",
            "origin": "brief",
        }
        with pytest.raises(RuntimeFailure) as again:
            await _approve(
                user_id, thread_id, approval_id, pins, speech_cleanup_analysis_id=analysis_id
            )
        assert again.value.code == "thread_revision_stale"
        assert _stash(approval_id)["choice"] == "keep_original"
        with sync_session() as db:
            prepared = db.execute(
                select(CreatorPreparedExecution).where(
                    CreatorPreparedExecution.approval_id == approval_id
                )
            ).scalar_one()
            assert prepared.snapshot["decisions"]["speech_cleanup"]["origin"] == "model_default"

        dispatched = _dispatch_recording_decision(monkeypatch, user_id, item_id)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert dispatched == [
            ("keep_original", "off_v1", {"status": "declined", "removal_count": 0, "removed_ms": 0})
        ]
        assert _recorded(analysis_id)[0] == "keep_original"
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_clip_scoped_keep_is_not_an_answer_for_the_whole_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode 7, the negative control: the same flow, with "keep this one clip whole", asks."""
    user_id, thread_id, item_id = _seed_talking_take()
    try:
        approval_id, pins = await _await_approval(
            monkeypatch, user_id, thread_id, (_keep_whole("clip:take-1"),), revision=2
        )
        analysis_id = await _checked_take(user_id, thread_id, item_id, approval_id, pins)

        assert (await _card(thread_id, INCIDENT_BUILD))["requires_choice"] is True
        with pytest.raises(RuntimeFailure) as refused:
            await _approve(
                user_id, thread_id, approval_id, pins, speech_cleanup_analysis_id=analysis_id
            )
        assert refused.value.code == "speech_cleanup_choice_required"
        assert _recorded(analysis_id) == (None, None)
        assert _stash(approval_id) is None
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_explicit_clean_tap_on_a_keep_whole_brief_is_honoured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode 5: the server never overrides a tap; "Clean up speech and create" still cleans."""
    user_id, thread_id, item_id = _seed_talking_take()
    try:
        approval_id, pins = await _await_approval(
            monkeypatch, user_id, thread_id, (_keep_whole(),), revision=2
        )
        analysis_id = await _checked_take(user_id, thread_id, item_id, approval_id, pins)
        await _approve(
            user_id,
            thread_id,
            approval_id,
            pins,
            speech_cleanup_analysis_id=analysis_id,
            speech_cleanup_choice="clean",
        )
        assert _stash(approval_id) == {"analysis_id": str(analysis_id), "choice": "clean"}
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
async def test_cleanup_ask_after_a_kept_take_asks_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mode 8: "clean up my pauses" after the take was kept whole puts the question back on
    every build, and the creator's tap cleans."""
    user_id, thread_id, item_id = _seed_talking_take()
    try:
        approval_id, pins = await _await_approval(
            monkeypatch, user_id, thread_id, (_keep_whole(), _clean_up()), revision=2
        )
        analysis_id = await _checked_take(
            user_id, thread_id, item_id, approval_id, pins, recorded="keep_original"
        )

        for build in (INCIDENT_BUILD, CARD_BUILD):
            card = await _card(thread_id, build)
            assert card["requires_choice"] is True, build
            assert "recommended_choice" not in card
        assert (await _card(thread_id, WEB))["requires_choice"] is False, "web unchanged"

        await _approve(
            user_id,
            thread_id,
            approval_id,
            pins,
            speech_cleanup_analysis_id=analysis_id,
            speech_cleanup_choice="clean",
        )
        assert _stash(approval_id) == {"analysis_id": str(analysis_id), "choice": "clean"}
    finally:
        await async_engine.dispose()
