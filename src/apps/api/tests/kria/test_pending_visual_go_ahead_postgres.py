"""KRI-1470: one Visual still being checked no longer blocks the whole chat.

Q1 run 3, thread 77BA43A0 (2026-11-14): the talking take and 9 of 10 Visuals were ready, one
overlay screenshot stayed `queued` (KRI-1469). The prompt got 4 planning runs of a 45 s wait
each, then "I'm still checking 1 of your clips ... Ask me to continue once those clips are
ready". "Skip that clip and continue with the rest" got the same 4 minutes and the same reply.
The analysis gate in `planner._plan_live_turn` held the turn for every creator-visible pool
asset before anything else ran, so the skip never reached any handling.

Real Postgres, real `submit_turn` + `run_kria_turn` (claim, continuation, completion) and the
real planner up to the model: the Main Creator answer and the clip-intent resolution are
scripted; the gate, the clip loading and the reply copy are the production code.

Ways this can fail, written before the fix:
  P1  the incident: on the turn's last run, with only a Visual pending, the reply is still
      "I'm still checking 1 of your clips" and no draft is made.
  P2  the follow-up: "Skip that clip and continue with the rest" waits again and gets the
      same reply (it needs no words read: its own last run goes on, KRI-667).
  P3  over-reach: pending footage (a talking take / phone clip) is planned without; footage
      must still hold the turn.
  P4  over-reach: a Visual is dropped on an early run while it may still land within the
      turn's remaining runs.
  P5  the draft leaves the file out silently, or names the wrong file, or calls it a clip.
  P6  the left-out Visual blocks again later in the same turn (the clip-intent step waits on
      every clip it is handed and answers "still checking ... against that request").
  P7  the left-out Visual is taken out of the manifest, so the approval fence later rejects
      "Footage or capabilities changed".
  P8  any message ("continue", "skip it") drops the Visual on an early run; only the
      turn's last run may, so a Visual that lands a minute later is still used.
  P9  with the kill switch off the old behaviour changes.
  P10 a Turkish chat gets the English sentence.
  P11 every item on the edit is a pending Visual: nothing would be left to plan, so it waits.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select

from app.config import settings
from app.database import engine as async_engine
from app.database import sync_session
from app.kria import planner
from app.models import CreationThread, CreationThreadEvent, PlanItem, PlanItemAsset
from app.services.clip_intent_planning import PlannedIntentResolution
from app.services.clip_intent_resolution import IntentResolution
from app.tasks.kria_runtime import run_kria_turn
from tests._prod_profile import apply_prod_profile
from tests.kria import test_readiness_recheck_fixed_point_postgres as lyrics
from tests.kria.test_turn_auto_continue_postgres import _publishes, _submit

PENDING_NAME = "Screenshot 5 gamestudios.png"
# How the chat names a file (`clip_display_name`: no extension).
SHOWN = "“Screenshot 5 gamestudios”"
READY_NAMES = ("Screenshot 1 dashboard.png", "Recording 1.png")
STILL_CHECKING_EN = "I'm still checking 1 of your clips."
STILL_CHECKING_TR = "Kliplerinden 1 tanesini hâlâ kontrol ediyorum."
SKIP = "Skip that clip and continue with the rest"


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    apply_prod_profile(monkeypatch)
    for name in (
        "kria_runtime_v2_enabled",
        "main_creator_agent_enabled",
        "kria_creative_brief_enabled",
        "kria_brief_binding_enabled",
        "kria_approval_readiness_enabled",
        "kria_choice_questions_enabled",
        "kria_clip_understanding_enabled",
        "kria_pending_visual_go_ahead_enabled",
    ):
        monkeypatch.setattr(settings, name, True)
    for name in (
        "kria_creative_brief_user_ids",
        "kria_brief_binding_user_ids",
        "phone_render_user_ids",
    ):
        monkeypatch.setattr(settings, name, [])
    monkeypatch.setattr(settings, "kria_turn_auto_continue_max_runs", 4)
    # One re-read per run instead of 45 s: the row never changes inside a run here.
    monkeypatch.setattr(settings, "kria_clip_understanding_wait_s", 0.0)
    # Nothing in the background analyses anything: what is pending stays pending.
    monkeypatch.setattr(planner, "_kick_clip_understanding", AsyncMock())


@pytest_asyncio.fixture(autouse=True)
async def _dispose_async_engine():
    yield
    await async_engine.dispose()


@pytest.fixture()
def clip_intents(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """The clips each clip-intent resolution was handed (real planner, scripted resolver)."""
    seen: list[list[str]] = []

    async def resolve(*_args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        seen.append([clip.media_id for clip in kwargs["clips"]])
        return PlannedIntentResolution([], IntentResolution(intents=[]))

    monkeypatch.setattr(planner, "plan_and_resolve_clip_intents", resolve)
    return seen


def _seed(*, language: str = "en", footage_pending: bool = False, footage: bool = True):
    """The incident's shape: an analysed phone take, two ready Visuals, one `queued`."""
    user_id, thread_id = lyrics._seed()
    now = datetime.now(UTC)
    with sync_session() as db:
        thread = db.get(CreationThread, thread_id)
        thread.state = {**thread.state, "reply_language": language}
        item = db.get(PlanItem, thread.active_plan_item_id)
        if footage_pending:
            item.clip_assignments = [{**item.clip_assignments[0], "analysis": None}]
        if not footage:
            item.clip_assignments = []
            item.clip_gcs_paths = []
        for name in READY_NAMES:
            db.add(
                PlanItemAsset(
                    plan_item_id=item.id,
                    user_id=user_id,
                    kind="image",
                    gcs_path=f"users/{user_id}/plan/{item.id}/pool/{uuid.uuid4()}.png",
                    gcs_generation="1",
                    upload_content_type="image/png",
                    source_filename=name,
                    status="ready",
                    media_status="ready",
                    analysis={
                        "subject": "app screen",
                        "description": f"{name}: an app screen",
                        "understanding": {"subject": "app screen", "summary": "an app screen"},
                    },
                )
            )
        pending = PlanItemAsset(
            plan_item_id=item.id,
            user_id=user_id,
            kind="image",
            gcs_path=f"users/{user_id}/plan/{item.id}/pool/{uuid.uuid4()}.png",
            gcs_generation="1",
            upload_content_type="image/png",
            source_filename=PENDING_NAME,
            status="queued",
            media_status="pending",
            analysis_attempt_token=uuid.uuid4().hex,
            analysis_attempt_count=1,
            analysis_last_dispatched_at=now,
            analysis_deadline_at=now + timedelta(hours=1),
        )
        db.add(pending)
        db.commit()
        pending_id = f"asset-{pending.id}"
    return user_id, thread_id, pending_id


def _thread_replies(thread_id: uuid.UUID) -> list[CreationThreadEvent]:
    with sync_session() as db:
        return list(
            db.scalars(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type.in_(("assistant_response", "draft_applied")),
                )
                .order_by(CreationThreadEvent.sequence)
            )
        )


async def _turn(user_id, thread_id, message: str) -> list[str]:  # noqa: ANN001
    """Submit one message and run the turn the way the queue would: every continuation."""
    with sync_session() as db:
        revision = int(db.get(CreationThread, thread_id).revision)
    accepted = await _submit(user_id, thread_id, message, revision)
    statuses = []
    for _ in range(settings.kria_turn_auto_continue_max_runs):
        result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
        statuses.append(result["status"])
        if result["status"] != "continued":
            break
    return statuses


@pytest.mark.asyncio
async def test_last_run_drafts_without_the_pending_visual_and_names_it(
    monkeypatch, clip_intents
) -> None:
    """P1, P4, P5, P6, P7: the incident's first message."""
    user_id, thread_id, pending_id = _seed()
    lyrics._model(monkeypatch)
    _publishes(monkeypatch)

    statuses = await _turn(user_id, thread_id, lyrics.MESSAGE)

    # The three earlier runs still wait for it (it may land), only the last goes on.
    assert statuses == ["continued", "continued", "continued", "awaiting_approval"]
    (reply,) = _thread_replies(thread_id)
    assert reply.event_type == "draft_applied", reply.content
    assert SHOWN in reply.content
    assert "clips" not in reply.content.split(SHOWN)[1].split(".")[0]
    assert STILL_CHECKING_EN not in reply.content
    # The clip-intent step was handed every clip but the left-out Visual.
    assert clip_intents and all(pending_id not in clips for clips in clip_intents)
    assert any(len(clips) == 1 + len(READY_NAMES) for clips in clip_intents)
    # The manifest keeps it, so approving the draft is not refused as "footage changed".
    assert await lyrics._approve(user_id, thread_id) == "approved"


@pytest.mark.asyncio
async def test_skip_after_the_still_checking_reply_drafts_on_its_last_run(
    monkeypatch, clip_intents
) -> None:
    """P2, P9: the switch off reproduces 11-14 exactly; on, the skip turn drafts."""
    user_id, thread_id, _pending_id = _seed()
    lyrics._model(monkeypatch)
    _publishes(monkeypatch)
    monkeypatch.setattr(settings, "kria_pending_visual_go_ahead_enabled", False)

    assert await _turn(user_id, thread_id, lyrics.MESSAGE) == ["continued"] * 3 + ["completed"]
    assert _thread_replies(thread_id)[-1].content.startswith(STILL_CHECKING_EN)
    assert await _turn(user_id, thread_id, SKIP) == ["continued"] * 3 + ["completed"]
    assert _thread_replies(thread_id)[-1].content.startswith(STILL_CHECKING_EN)

    monkeypatch.setattr(settings, "kria_pending_visual_go_ahead_enabled", True)
    assert await _turn(user_id, thread_id, SKIP) == ["continued"] * 3 + ["awaiting_approval"]
    reply = _thread_replies(thread_id)[-1]
    assert reply.event_type == "draft_applied"
    assert SHOWN in reply.content


@pytest.mark.asyncio
@pytest.mark.parametrize("message", ["continue", "skip it", SKIP])
async def test_no_message_drops_the_visual_on_an_early_run(
    monkeypatch, clip_intents, message
) -> None:
    """P8."""
    user_id, thread_id, _pending_id = _seed()
    lyrics._model(monkeypatch)
    _publishes(monkeypatch)
    with sync_session() as db:
        revision = int(db.get(CreationThread, thread_id).revision)
    accepted = await _submit(user_id, thread_id, message, revision)
    result = await asyncio.to_thread(run_kria_turn.run, accepted.turn_id)
    assert result["status"] == "continued"
    assert _thread_replies(thread_id) == []


@pytest.mark.asyncio
async def test_pending_footage_still_holds_the_turn(monkeypatch, clip_intents) -> None:
    """P3: the phone take is not analysed yet; the last run keeps the old reply."""
    user_id, thread_id, _pending_id = _seed(footage_pending=True)
    lyrics._model(monkeypatch)
    _publishes(monkeypatch)

    assert await _turn(user_id, thread_id, lyrics.MESSAGE) == ["continued"] * 3 + ["completed"]
    (reply,) = _thread_replies(thread_id)
    assert reply.event_type == "assistant_response"
    assert reply.content.startswith("I'm still checking 2 of your clips.")
    assert SHOWN not in reply.content
    assert await _turn(user_id, thread_id, SKIP) == ["continued"] * 3 + ["completed"]
    assert _thread_replies(thread_id)[-1].content.startswith("I'm still checking 2 of your clips.")


@pytest.mark.asyncio
async def test_only_pending_visuals_left_still_waits(monkeypatch, clip_intents) -> None:
    """P11: without the phone take and the ready Visuals nothing would be left to plan."""
    user_id, thread_id, _pending_id = _seed(footage=False)
    with sync_session() as db:
        for row in db.scalars(select(PlanItemAsset).where(PlanItemAsset.status == "ready")):
            if row.user_id == user_id:
                db.delete(row)
        db.commit()
    lyrics._model(monkeypatch)
    _publishes(monkeypatch)

    statuses = await _turn(user_id, thread_id, SKIP)

    assert statuses[-1] == "completed"
    assert _thread_replies(thread_id)[-1].content.startswith(STILL_CHECKING_EN)


@pytest.mark.asyncio
async def test_a_turkish_chat_hears_it_in_turkish(monkeypatch, clip_intents) -> None:
    """P10."""
    user_id, thread_id, _pending_id = _seed(language="tr")
    lyrics._model(monkeypatch)
    _publishes(monkeypatch)

    assert await _turn(user_id, thread_id, "O klibi atla, kalanlarla devam et") == [
        "continued"
    ] * 3 + ["awaiting_approval"]
    reply = _thread_replies(thread_id)[-1]
    assert SHOWN in reply.content
    assert "olmadan planladım" in reply.content
    assert STILL_CHECKING_TR not in reply.content
