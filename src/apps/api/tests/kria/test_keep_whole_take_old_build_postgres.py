"""KRI-1291: an app build without the "Keep original speech" card keeps the take whole.

Creator request (Don Julio lunch T1, KRI-971): "Use the whole clip as it is, don't trim
anything." on one 8.2 s Talking take. The brief records it as r1 ``{kind: style, scope: global,
facts: {keep_whole_take: true}}``. A build below protocol 44 ignores
``speech_cleanup.recommended_choice`` and leads with "Clean up speech and create", which sends an
explicit ``clean``. The thread read now offers that build no choice, so its card shows the plain
"Create this video" (analysis id, no choice), and the approval answers keep_original from that
approval's own pinned brief. That is the only change: the read only ever removes a question, and
the approval only answers the question the read removed.

Real Postgres, real turn/approval/claim code, real thread read (`_response`) and the real
dispatch step that records the decision (`_speech_cleanup_dispatch_snapshot`). Faked: the
planner's output (the incident's recorded brief and strategy), the speech-check worker (the row
is marked ready the way it finishes) and the render enqueue.

Failure modes, written before the code:

1. An old build still sees the choice, and one tap cuts the take (the incident).
2. A protocol-44 build loses the choice or its "Keep original speech" lead.
3. The web card (no protocol header) changes.
4. The old build's plain button dead-ends on 409 speech_cleanup_choice_required (KRI-292).
5. The server answers "clean", or overrides a choice submitted on this approval.
6. A polled thread read records a decision.
7. A clip-scoped "keep this clip whole" answers for the whole take.
8. A decision recorded at an earlier card is asked again on a later approval. "Clean up my
   pauses", switched off at the card, then "make the captions yellow": a native card shows Clean
   preselected and one tap cuts the take the creator kept.
9. A clean the creator tapped against a keep-whole brief is asked again on a later approval, or
   swapped for keep_original without a tap.
10. A retried approval, or a re-run of the dispatch claim, records a different answer.
11. The audit says the creator answered the cleanup question at the card.
12. A render-retry card differs from the stored projection: it offers "Keep original speech and
    create" over a retry that ignores the choice, or hides a question the stored card asks.
13. Protocol 43, the last build without the card, is asked; protocol 44 is not asked; or a
    plain approve from a build that was shown the question is answered for it.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

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
from app.kria.render_retry import RETRY_FIELD
from app.kria.runtime import RuntimeFailure, approval_fingerprint, decide_approval, submit_turn
from app.models import (
    ContentPlan,
    CreationThread,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
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
LAST_OLD_BUILD = 43
CARD_BUILD = 44
WEB = None
NATIVE_BUILDS = (INCIDENT_BUILD, LAST_OLD_BUILD, CARD_BUILD)
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


def _thread_revision(thread_id: uuid.UUID) -> int:
    with sync_session() as db:
        return int(db.get(CreationThread, thread_id).revision)


async def _await_approval(
    monkeypatch: pytest.MonkeyPatch,
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    brief_updates: tuple[BriefUpdate, ...],
    *,
    revision: int,
    message: str = MESSAGE,
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
                message=message,
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
    *,
    build: int | None,
    **cleanup: object,
) -> None:
    """The approve POST from the build at protocol ``build`` (`X-Kria-Client-Protocol`)."""
    clear_request_context()
    set_client_protocol(build)
    try:
        async with AsyncSessionLocal() as db:
            await decide_approval(
                db,
                thread_id=thread_id,
                approval_id=approval_id,
                creator_id=user_id,
                decision="approve",
                body=pins.model_copy(update=cleanup),
            )
    finally:
        clear_request_context()


async def _checked_take(
    user_id: uuid.UUID,
    thread_id: uuid.UUID,
    item_id: uuid.UUID,
    approval_id: uuid.UUID,
    pins: ApprovalDecisionBody,
) -> uuid.UUID:
    """The first approve schedules the speech check (409, approval stays pending); the check
    then finishes with the incident's 0.785 s of findings."""
    with pytest.raises(RuntimeFailure) as first:
        await _approve(user_id, thread_id, approval_id, pins, build=INCIDENT_BUILD)
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


async def _asks(thread_id: uuid.UUID) -> dict[int | None, bool]:
    """``requires_choice`` per reading build, the web included."""
    return {
        build: (await _card(thread_id, build))["requires_choice"] for build in (*NATIVE_BUILDS, WEB)
    }


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


async def _first_render(
    monkeypatch: pytest.MonkeyPatch,
    brief_updates: tuple[BriefUpdate, ...],
    *,
    tapped: str,
) -> tuple[uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID, uuid.UUID]:
    """A first approval answered with ``tapped`` on a protocol-44 card, rendered: the dispatch
    records the creator's answer on the analysis."""
    user_id, thread_id, item_id = _seed_talking_take()
    approval_id, pins = await _await_approval(
        monkeypatch, user_id, thread_id, brief_updates, revision=2
    )
    analysis_id = await _checked_take(user_id, thread_id, item_id, approval_id, pins)
    assert (await _card(thread_id, CARD_BUILD))["requires_choice"] is True
    await _approve(
        user_id,
        thread_id,
        approval_id,
        pins,
        build=CARD_BUILD,
        speech_cleanup_analysis_id=analysis_id,
        speech_cleanup_choice=tapped,
    )
    dispatched = _dispatch_recording_decision(monkeypatch, user_id, item_id)
    await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
    assert [choice for choice, *_ in dispatched] == [tapped]
    assert _recorded(analysis_id)[0] == tapped
    with sync_session() as db:
        # The faked render never finishes; the turn closes the way the ready observer closes it.
        turn = db.get(CreatorAgentTurn, db.get(CreatorAgentApproval, approval_id).turn_id)
        assert turn.status == "observing"
        turn.status, turn.completed_at = "completed", datetime.now(UTC)
        db.commit()
    return user_id, thread_id, item_id, analysis_id, approval_id


@pytest.mark.asyncio
async def test_old_build_plain_create_keeps_the_whole_take(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Modes 1-4, 6, 10, 11: the incident request on the incident's build keeps all 8.2 s."""
    user_id, thread_id, item_id = _seed_talking_take()
    try:
        approval_id, pins = await _await_approval(
            monkeypatch, user_id, thread_id, (_keep_whole(),), revision=2
        )
        analysis_id = await _checked_take(user_id, thread_id, item_id, approval_id, pins)

        old = await _card(thread_id, INCIDENT_BUILD)
        new = await _card(thread_id, CARD_BUILD)
        web = await _card(thread_id, WEB)
        assert old["requires_choice"] is False, "the old card must not lead with Clean up"
        assert new["requires_choice"] is True and new["recommended_choice"] == "keep_original"
        assert web["requires_choice"] is True, "the web card is unchanged"
        assert _recorded(analysis_id) == (None, None), "a thread read never records a decision"
        assert _stash(approval_id) is None

        # The old build's plain "Create this video": the analysis id, no choice.
        await _approve(
            user_id,
            thread_id,
            approval_id,
            pins,
            build=INCIDENT_BUILD,
            speech_cleanup_analysis_id=analysis_id,
        )
        assert _stash(approval_id) == {
            "analysis_id": str(analysis_id),
            "choice": "keep_original",
            "origin": "brief",
        }
        with pytest.raises(RuntimeFailure) as again:
            await _approve(
                user_id,
                thread_id,
                approval_id,
                pins,
                build=INCIDENT_BUILD,
                speech_cleanup_analysis_id=analysis_id,
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
@pytest.mark.parametrize("build", [LAST_OLD_BUILD, CARD_BUILD])
async def test_the_keep_card_protocol_is_the_boundary(
    monkeypatch: pytest.MonkeyPatch, build: int
) -> None:
    """Mode 13: 43 is offered no choice and its plain create keeps; 44 is asked, and a plain
    approve from it is refused as before rather than answered for the creator."""
    user_id, thread_id, item_id = _seed_talking_take()
    try:
        approval_id, pins = await _await_approval(
            monkeypatch, user_id, thread_id, (_keep_whole(),), revision=2
        )
        analysis_id = await _checked_take(user_id, thread_id, item_id, approval_id, pins)
        card_build = build >= CARD_BUILD
        assert (await _card(thread_id, build))["requires_choice"] is card_build

        if card_build:
            with pytest.raises(RuntimeFailure) as refused:
                await _approve(
                    user_id,
                    thread_id,
                    approval_id,
                    pins,
                    build=build,
                    speech_cleanup_analysis_id=analysis_id,
                )
            assert refused.value.code == "speech_cleanup_choice_required"
            assert _stash(approval_id) is None
        else:
            await _approve(
                user_id,
                thread_id,
                approval_id,
                pins,
                build=build,
                speech_cleanup_analysis_id=analysis_id,
            )
            assert _stash(approval_id) == {
                "analysis_id": str(analysis_id),
                "choice": "keep_original",
                "origin": "brief",
            }
        assert _recorded(analysis_id) == (None, None)
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
                user_id,
                thread_id,
                approval_id,
                pins,
                build=INCIDENT_BUILD,
                speech_cleanup_analysis_id=analysis_id,
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
    """Mode 5: a choice submitted on this approval wins, even from the build whose plain create
    the brief answers."""
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
            build=INCIDENT_BUILD,
            speech_cleanup_analysis_id=analysis_id,
            speech_cleanup_choice="clean",
        )
        assert _stash(approval_id) == {"analysis_id": str(analysis_id), "choice": "clean"}
    finally:
        await async_engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("brief_updates", "tapped", "approving_build"),
    [
        ((_clean_up(),), "keep_original", CARD_BUILD),
        ((_keep_whole(),), "clean", INCIDENT_BUILD),
    ],
    ids=["cleanup-ask-switched-off", "clean-tapped-on-a-keep-brief"],
)
async def test_a_recorded_answer_is_not_asked_again_on_a_later_approval(
    monkeypatch: pytest.MonkeyPatch,
    brief_updates: tuple[BriefUpdate, ...],
    tapped: str,
    approving_build: int,
) -> None:
    """Modes 8 and 9: the brief's requirement stays live after the creator answered the card
    against it. The follow-up approval ("make the captions yellow") asks nobody, and its plain
    create renders the recorded answer, not the brief's."""
    user_id, thread_id, item_id, analysis_id, _first = await _first_render(
        monkeypatch, brief_updates, tapped=tapped
    )
    try:
        approval_id, pins = await _await_approval(
            monkeypatch,
            user_id,
            thread_id,
            (),
            revision=_thread_revision(thread_id),
            message="Make the captions yellow",
        )
        assert await _asks(thread_id) == {build: False for build in (*NATIVE_BUILDS, WEB)}

        await _approve(
            user_id,
            thread_id,
            approval_id,
            pins,
            build=approving_build,
            speech_cleanup_analysis_id=analysis_id,
        )
        assert _stash(approval_id) == {"analysis_id": str(analysis_id), "choice": tapped}
        dispatched = _dispatch_recording_decision(monkeypatch, user_id, item_id)
        await asyncio.to_thread(execute_kria_approval.run, str(approval_id))
        assert [choice for choice, *_ in dispatched] == [tapped]
        assert _recorded(analysis_id)[0] == tapped
    finally:
        await async_engine.dispose()


def _pending_render_retry(source_approval_id: uuid.UUID) -> None:
    """The retry approval `prepare_render_retry` writes after the render failed: the same strategy
    draft, a new render execution carrying ``render_retry``."""
    with sync_session() as db:
        source_approval = db.get(CreatorAgentApproval, source_approval_id)
        source = db.get(CreatorAgentExecution, uuid.UUID(source_approval.execution_ids[0]))
        now = datetime.now(UTC)
        execution = CreatorAgentExecution(
            session_id=source.session_id,
            turn_id=source.turn_id,
            idempotency_key=f"render-retry:{source.id}",
            request_digest=source.request_digest,
            expected_revision=source.expected_revision,
            tool_name="render.request",
            tool_version=1,
            risk="approval_required",
            dependency_group=1,
            group_order=1,
            target_thread_id=source.target_thread_id,
            target_draft_id=source.target_draft_id,
            target_draft_revision=source.target_draft_revision,
            target_job_id=source.target_job_id,
            target_ownership_epoch=source.target_ownership_epoch,
            status="awaiting_approval",
            started_at=now,
            awaiting_approval_at=now,
            result={RETRY_FIELD: {"source_execution_id": str(source.id)}},
        )
        db.add(execution)
        db.flush()
        db.add(
            CreatorAgentApproval(
                creator_id=source_approval.creator_id,
                thread_id=source_approval.thread_id,
                session_id=source_approval.session_id,
                turn_id=source_approval.turn_id,
                draft_id=source_approval.draft_id,
                draft_revision=source_approval.draft_revision,
                target_job_id=source.target_job_id,
                target_ownership_epoch=source_approval.target_ownership_epoch,
                execution_ids=[str(execution.id)],
                consequence_summary=(
                    "Retry this exact approved edit. Keep its clips, text, timing and audio "
                    "unchanged."
                ),
                status="pending",
                expires_at=now + timedelta(minutes=30),
            )
        )
        db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("pending", "recorded"),
    [("render-retry", "clean"), ("render-retry", None), ("outside-the-cohort", None)],
    ids=["retry-over-a-recorded-clean", "retry-unanswered", "follow-up-outside-the-cohort"],
)
async def test_a_card_the_approval_does_not_gate_is_the_stored_projection(
    monkeypatch: pytest.MonkeyPatch, pending: str, recorded: str | None
) -> None:
    """Mode 12: a render retry re-renders the approved edit and ignores any cleanup choice, and
    an approval outside the enforce cohort is not gated at all. Neither plain create is answered
    from the brief, so their card is the stored projection on every build: "Retry approved edit"
    over a recorded clean, and the stored question when the current check is unanswered."""
    user_id, thread_id, _item_id, analysis_id, approval_id = await _first_render(
        monkeypatch, (_keep_whole(),), tapped="clean"
    )
    try:
        if recorded is None:
            with sync_session() as db:
                row = db.get(SpeechCleanupAnalysis, analysis_id)
                row.decision, row.decision_at = None, None
                db.commit()
        if pending == "render-retry":
            _pending_render_retry(approval_id)
        else:
            monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 0)
            await _await_approval(
                monkeypatch,
                user_id,
                thread_id,
                (),
                revision=_thread_revision(thread_id),
                message="Make the captions yellow",
            )

        stored = recorded is None
        assert await _asks(thread_id) == {build: stored for build in (*NATIVE_BUILDS, WEB)}
    finally:
        await async_engine.dispose()
