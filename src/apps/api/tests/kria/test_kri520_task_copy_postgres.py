"""KRI-520: every task entry point that writes a chat event binds the chat's language.

The turn task already did. The approval claim, the dispatch outcome, the render observer,
the expiry sweep and the approval decision write creator-visible text from other
transactions, so each one reads the language off the thread row it holds. English stays
the old string; a Turkish chat gets Turkish; the binding never outlives the work.
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
from app.kria.planner import PlannedKriaTurn, adapt_creator_action
from app.kria.reply_language import current_reply_language
from app.kria.runtime import (
    RuntimeFailure,
    _expire_blocking_approval,
    approval_fingerprint,
    decide_approval,
    submit_turn,
)
from app.models import (
    CreationThread,
    CreationThreadEvent,
    CreatorAgentApproval,
    CreatorAgentExecution,
    CreatorAgentSession,
    CreatorAgentTurn,
    Job,
    PlanItem,
)
from app.tasks import kria_runtime as kr
from app.tasks.content_plan_build import DispatchResult
from app.tasks.kria_runtime import (
    _expire_pending_approvals,
    _observe_dispatched_execution,
    execute_kria_approval,
    run_kria_turn,
)

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

from tests.kria.test_runtime_postgres_integration import (  # noqa: E402
    _seed_runtime_project,
    _seed_user_turn,
)

_LANGUAGES = [
    pytest.param(None, id="english"),
    pytest.param("tr", id="turkish"),
]


@pytest.fixture(autouse=True)
def _runtime_enabled(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)


@pytest.fixture(autouse=True)
async def _fresh_async_pool():  # noqa: ANN202
    yield
    await async_engine.dispose()


def _set_language(thread_id: uuid.UUID, language: str | None) -> None:
    if language is None:
        return
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        assert thread is not None
        thread.state = {**(thread.state or {}), "reply_language": language}
        db.commit()


def _in_thread(fn, *args):  # noqa: ANN001, ANN002, ANN202
    """Run a sync task off the loop and report the language still bound when it returned."""

    def _call():  # noqa: ANN202
        return fn(*args), current_reply_language()

    return asyncio.to_thread(_call)


def _last_event(thread_id: uuid.UUID, *, event_type: str) -> CreationThreadEvent:
    with sync_session() as db:
        return db.execute(
            select(CreationThreadEvent)
            .where(
                CreationThreadEvent.thread_id == thread_id,
                CreationThreadEvent.event_type == event_type,
            )
            .order_by(CreationThreadEvent.sequence.desc())
            .limit(1)
        ).scalar_one()


def _strategy_turn(monkeypatch: pytest.MonkeyPatch) -> None:
    plan = adapt_creator_action(
        ProposeStrategy(
            kind="propose_strategy",
            strategy=CreativeStrategy(
                direction="guided_story",
                edit_format="day_vlog",
                audio_strategy="licensed_music",
                pacing="fast",
                render_program="guided",
                selected_media_ids=[],
                rationale="Open on the whisk and end on the packed order.",
            ),
            summary="Open on the whisk and finish on the packed order.",
        )
    )

    async def _planned(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _planned)


async def _draft_with_approval(monkeypatch: pytest.MonkeyPatch, language: str | None) -> dict:
    """A project whose strategy draft is waiting on a pinned approval."""
    user_id, thread_id, session_id = _seed_runtime_project()
    _set_language(thread_id, language)
    _strategy_turn(monkeypatch)
    message = (
        "Matcha güncellemesini hızlı ama kişisel yap"
        if language == "tr"
        else "Make the matcha update feel quick but personal"
    )
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message=message,
                client_event_id=f"kri520-tasks-{uuid.uuid4().hex}",
                expected_thread_revision=2,
            ),
        )
    result, still_bound = await _in_thread(run_kria_turn.run, accepted.turn_id)
    assert result == {"turn_id": accepted.turn_id, "status": "awaiting_approval"}
    assert still_bound is None
    with sync_session() as db:
        approval = db.execute(
            select(CreatorAgentApproval).where(
                CreatorAgentApproval.turn_id == uuid.UUID(accepted.turn_id)
            )
        ).scalar_one()
        item_id = db.get(CreatorAgentSession, session_id).plan_item_id
        return {
            "user_id": user_id,
            "thread_id": thread_id,
            "session_id": session_id,
            "item_id": item_id,
            "turn_id": uuid.UUID(accepted.turn_id),
            "approval_id": approval.id,
            "draft_revision": approval.draft_revision,
            "cost_summary": approval.cost_summary,
            "consequence_summary": approval.consequence_summary,
        }


def _decide_body(ctx: dict) -> ApprovalDecisionBody:
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, ctx["approval_id"])
        return ApprovalDecisionBody(
            expected_thread_revision=db.get(CreationThread, ctx["thread_id"]).revision,
            expected_draft_revision=approval.draft_revision,
            expected_approval_fingerprint=approval_fingerprint(approval),
        )


def _fake_dispatch(ctx: dict, outcome: str):  # noqa: ANN202
    def _dispatch(*_args, **_kwargs) -> DispatchResult:  # noqa: ANN002, ANN003
        if outcome != "dispatched":
            return DispatchResult(outcome)
        with sync_session() as db:
            item = db.get(PlanItem, ctx["item_id"], with_for_update=True)
            job = Job(
                user_id=ctx["user_id"],
                status="queued",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item.id,
                content_plan_ownership_epoch=0,
                assembly_plan={"variants": []},
            )
            db.add(job)
            db.flush()
            item.current_job_id = job.id
            db.commit()
            return DispatchResult("dispatched", job_id=str(job.id))

    return _dispatch


async def _approve(ctx: dict) -> None:
    async with AsyncSessionLocal() as db:
        await decide_approval(
            db,
            thread_id=ctx["thread_id"],
            approval_id=ctx["approval_id"],
            creator_id=ctx["user_id"],
            decision="approve",
            body=_decide_body(ctx),
        )


# -- draft, approval card, dispatch, review: one flow in both languages ---------------


@pytest.mark.asyncio
@pytest.mark.parametrize("language", _LANGUAGES)
async def test_draft_card_and_ready_review_follow_the_chat_language(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    ctx = await _draft_with_approval(monkeypatch, language)
    turkish = language == "tr"

    draft = _last_event(ctx["thread_id"], event_type="draft_applied")
    assert draft.payload["changes"][1:] == (
        ["Tempo: Hızlı", "Format: Günlük vlog"] if turkish else ["Fast pacing", "Day Vlog format"]
    )
    assert ctx["cost_summary"] == ("Tek bir video" if turkish else "One render")
    # The prefix is a contract with the iOS card, in every language.
    assert ctx["consequence_summary"].startswith("Render this draft: ")

    await _approve(ctx)
    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for", _fake_dispatch(ctx, "dispatched")
    )
    dispatched, still_bound = await _in_thread(execute_kria_approval.run, str(ctx["approval_id"]))
    assert dispatched["status"] == "dispatched"
    assert still_bound is None

    with sync_session() as db:
        execution = db.execute(
            select(CreatorAgentExecution).where(
                CreatorAgentExecution.id
                == uuid.UUID(db.get(CreatorAgentApproval, ctx["approval_id"]).execution_ids[0])
            )
        ).scalar_one()
        execution_id, job_id = execution.id, execution.target_job_id
        job = db.get(Job, job_id, with_for_update=True)
        job.status = "variants_ready"
        job.assembly_plan = {
            "variants": [
                {
                    "variant_id": "original_text",
                    "render_generation_id": "generation-1",
                    "render_status": "ready",
                    "output_url": "https://example.test/output.mp4",
                }
            ]
        }
        db.commit()

    (observed, _), still_bound = await _in_thread(_observe_dispatched_execution, execution_id)
    assert observed == "completed"
    assert still_bound is None
    review = _last_event(ctx["thread_id"], event_type="assistant_review")
    if turkish:
        assert review.content.startswith("Orijinal sesli versiyon hazır. Onayladığın video")
    else:
        assert review.content == (
            "The original text cut is ready. The approved render finished; review the "
            "opening, pacing, and text, then tell me what you want changed."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", _LANGUAGES)
async def test_a_refused_dispatch_is_worded_in_the_chat_language(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    ctx = await _draft_with_approval(monkeypatch, language)
    await _approve(ctx)
    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        _fake_dispatch(ctx, "visuals_processing"),
    )
    refused, still_bound = await _in_thread(execute_kria_approval.run, str(ctx["approval_id"]))
    assert refused["status"] == "failed"
    assert still_bound is None

    event = _last_event(ctx["thread_id"], event_type="assistant_render_failed")
    assert event.payload["dispatch_outcome"] == "visuals_processing"
    assert event.payload["code"] == "render_dispatch_failed"
    if language == "tr":
        assert event.content == kr._DISPATCH_REFUSALS_TR["visuals_processing"]
    else:
        assert event.content == kr._VISUALS_DISPATCH_REFUSALS["visuals_processing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("language", _LANGUAGES)
async def test_a_generic_dispatch_failure_is_worded_in_the_chat_language(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    ctx = await _draft_with_approval(monkeypatch, language)
    await _approve(ctx)
    monkeypatch.setattr(
        "app.tasks.content_plan_build.dispatch_item_render_for",
        _fake_dispatch(ctx, "publish_failed"),
    )
    await _in_thread(execute_kria_approval.run, str(ctx["approval_id"]))
    event = _last_event(ctx["thread_id"], event_type="assistant_render_failed")
    if language == "tr":
        assert event.content.startswith("Videoyu başlatamadım. Taslağın hâlâ kayıtlı")
    else:
        assert event.content == (
            "I couldn't start the render. Your draft is still saved, "
            "so you can retry without repeating the edit."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", _LANGUAGES)
async def test_a_claim_that_finds_the_project_changed_says_so_in_the_chat_language(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    ctx = await _draft_with_approval(monkeypatch, language)
    await _approve(ctx)
    with sync_session() as db:
        db.get(CreatorAgentSession, ctx["session_id"]).manifest_hash = "c" * 64
        db.commit()

    ignored, still_bound = await _in_thread(execute_kria_approval.run, str(ctx["approval_id"]))
    assert ignored["status"] == "ignored"
    assert still_bound is None

    event = _last_event(ctx["thread_id"], event_type="assistant_error")
    assert event.payload["code"] == "approval_target_stale"
    if language == "tr":
        assert event.content.startswith("Videoyu başlatamadan önce proje değişti.")
    else:
        assert event.content == (
            "The project changed before I could start that render. "
            "I kept your draft; ask me to prepare it again."
        )


# -- stale / expired approvals ---------------------------------------------------------


def _overdue(approval_id: uuid.UUID, *, ago: timedelta = timedelta(minutes=1)) -> None:
    with sync_session() as db:
        approval = db.get(CreatorAgentApproval, approval_id)
        approval.expires_at = datetime.now(UTC) - ago
        db.commit()


@pytest.mark.asyncio
@pytest.mark.parametrize("language", _LANGUAGES)
async def test_a_stale_approval_tells_the_chat_in_its_language_but_the_409_stays_english(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    ctx = await _draft_with_approval(monkeypatch, language)
    with sync_session() as db:
        db.get(CreatorAgentSession, ctx["session_id"]).manifest_hash = "c" * 64
        db.commit()

    with pytest.raises(RuntimeFailure) as failure:
        await _approve(ctx)
    assert failure.value.code == "approval_target_stale"
    assert failure.value.message == kr_runtime_stale_copy()

    event = _last_event(ctx["thread_id"], event_type="assistant_error")
    assert event.payload["code"] == "approval_target_stale"
    if language == "tr":
        assert event.content.startswith("Bu onay videonun eski bir sürümü için hazırlanmıştı")
    else:
        assert event.content == kr_runtime_stale_copy()
    assert current_reply_language() is None


def kr_runtime_stale_copy() -> str:
    from app.kria.runtime import _APPROVAL_STALE_COPY

    return _APPROVAL_STALE_COPY


@pytest.mark.asyncio
@pytest.mark.parametrize("language", _LANGUAGES)
async def test_approving_an_expired_approval_closes_it_in_the_chat_language(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    ctx = await _draft_with_approval(monkeypatch, language)
    _overdue(ctx["approval_id"])

    with pytest.raises(RuntimeFailure) as failure:
        await _approve(ctx)
    assert failure.value.code == "approval_expired"
    assert failure.value.message == "This approval expired. Ask Kria to prepare it again."

    event = _last_event(ctx["thread_id"], event_type="assistant_error")
    assert event.content == (
        "Bu onayın süresi doldu. Kria'dan yeniden hazırlamasını iste."
        if language == "tr"
        else "This approval expired. Ask Kria to prepare it again."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", _LANGUAGES)
async def test_the_lazy_expiry_before_a_new_message_uses_the_chat_language(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    ctx = await _draft_with_approval(monkeypatch, language)
    _overdue(ctx["approval_id"])

    async with AsyncSessionLocal() as db:
        revision = await _expire_blocking_approval(
            db,
            thread_id=ctx["thread_id"],
            creator_id=ctx["user_id"],
            approval_id=ctx["approval_id"],
        )
    assert revision is not None
    event = _last_event(ctx["thread_id"], event_type="assistant_error")
    assert event.payload["code"] == "approval_expired"
    if language == "tr":
        assert event.content.startswith("Bu onay karara bağlanmadan süresi doldu")
    else:
        assert event.content == (
            "That approval expired before it was decided, so nothing was rendered. "
            "Tell me what you want and I'll prepare it again."
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("language", _LANGUAGES)
async def test_the_expiry_sweep_uses_the_chat_language(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    ctx = await _draft_with_approval(monkeypatch, language)
    # The sweep takes the 25 longest-overdue approvals in the whole database; overdue
    # rows other tests leave behind must not push this one out of the batch.
    _overdue(ctx["approval_id"], ago=timedelta(days=365 * 20))

    await _in_thread(_expire_pending_approvals)

    with sync_session() as db:
        assert db.get(CreatorAgentApproval, ctx["approval_id"]).status == "expired"
    event = _last_event(ctx["thread_id"], event_type="assistant_error")
    assert event.payload["code"] == "approval_expired"
    if language == "tr":
        assert event.content.startswith("Bu onay karara bağlanmadan süresi doldu")
    else:
        assert event.content.startswith("That approval expired before it was decided")


# -- the turn task: failures follow the language, and the binding is released ---------


@pytest.mark.asyncio
@pytest.mark.parametrize("language", _LANGUAGES)
async def test_a_failed_turn_says_so_in_the_chat_language_and_releases_the_binding(
    monkeypatch: pytest.MonkeyPatch, language: str | None
) -> None:
    _user_id, thread_id, session_id = _seed_runtime_project()
    _set_language(thread_id, language)
    turn_id = _seed_user_turn(thread_id, session_id, content="Plan this edit", status="pending")

    async def _boom(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("planner exploded")

    monkeypatch.setattr(settings, "main_creator_agent_enabled", True)
    monkeypatch.setattr("app.tasks.kria_runtime._plan_with_live_agent", _boom)

    def _run() -> str | None:
        with pytest.raises(RuntimeError, match="planner exploded"):
            run_kria_turn.run(str(turn_id))
        return current_reply_language()

    assert await asyncio.to_thread(_run) is None
    assert current_reply_language() is None

    event = _last_event(thread_id, event_type="assistant_error")
    assert event.payload["code"] == "runtime_turn_failed"
    assert event.payload["error_class"] == "RuntimeError"
    if language == "tr":
        assert event.content == (
            "Bu adımı tamamlayamadım ama projen ve kayıtlı taslağın güvende. İsteği tekrar dene."
        )
    else:
        assert event.content == (
            "I couldn't finish that step, but your project and saved draft are safe. "
            "Try the request again."
        )
    with sync_session() as db:
        assert db.get(CreatorAgentTurn, turn_id).status == "failed"


@pytest.mark.asyncio
async def test_the_inspect_reply_of_a_turkish_chat_is_turkish_and_releases_the_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _session_id = _seed_runtime_project()
    _set_language(thread_id, "tr")
    async with AsyncSessionLocal() as db:
        accepted, _ = await submit_turn(
            db,
            thread_id=thread_id,
            creator_id=user_id,
            body=SubmitTurnBody(
                message="Çekimlerimdeki en güçlü açılışı bul",
                client_event_id=f"kri520-inspect-{uuid.uuid4().hex}",
                expected_thread_revision=2,
            ),
        )
    monkeypatch.setattr(settings, "main_creator_agent_enabled", False)
    result, still_bound = await _in_thread(run_kria_turn.run, accepted.turn_id)
    assert result == {"turn_id": accepted.turn_id, "status": "completed"}
    assert still_bound is None
    reply = _last_event(thread_id, event_type="assistant_response")
    assert reply.content == (
        "whisking.mov ile aç; hikâye ilk andan net bir görsel bakış açısı kazanır."
    )
