"""KRI-809: the chat-time song match is recorded against the plan item, not the thread.

Prod 2026-10-21 (Don Julio M1, threads F4083F1D / 3E27DEF8): every montage chat turn ran
``nova.audio.music_matcher`` with the creation-thread id as ``RunContext.job_id``. The
light machine logged ``auto_music_matcher_done job_id=<thread id>`` and then
``agent_run_persist_failed ... error_type=IntegrityError``: ``agent_run.job_id`` is a
foreign key to ``jobs``, so the song-suggestion run never reached ``agent_run`` and was
missing from ``/admin/plan-items/<id>/proposal-trace`` (only ``nova.creator.main`` rows).

Ways this can fail (written before the fix):
1. The chat song match still binds the thread id as ``job_id``: the ``agent_run`` insert
   raises IntegrityError and the row is missing from the proposal trace (the incident).
2. The run persists under no owner, or an owner the trace does not read (session only),
   so it is dropped silently or invisible in ``proposal-trace``.
3. ``plan_live_turn`` stops handing the plan item to the gate, which is the same as 2.
4. With paid-call cost control on in production the match has no creator attribution
   (``jobs`` has no row for a thread id), the reservation is refused before the provider
   call, the matcher returns nothing and the turn silently falls back to the footage's
   own sound: the creator never gets a song.
5. The reservation is written with the thread id as ``job_id`` (also a ``jobs`` FK),
   fails, and cost control fails closed: same as 4.
6. A second identical song match in the same thread (the creator asks again with the
   same clips) reuses the first call's idempotency key, the ledger refuses it as already
   settled, and that turn falls back to the footage sound.
7. The render-time matcher callers lose their real ``job_id`` while the context is being
   threaded, so the render job-debug view stops showing the matcher run.
8. The matcher's log lines lose every id, so a chat match cannot be correlated with its
   plan item any more.

Everything runs the real code on the scratch Postgres: ``plan_live_turn`` and its gate,
the song freeze, ``_match_best_track``, the real ``MusicMatcherAgent`` runtime with the
real cost-control ledger and the real ``agent_run`` insert, and the real proposal-trace
route. Only the model client (canned matcher JSON), the main-creator call before the gate
and the track catalog read are fixed values.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from structlog.testing import capture_logs

from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
from app.config import settings
from app.database import AsyncSessionLocal, sync_engine, sync_session
from app.database import engine as async_engine
from app.kria import planner
from app.kria.planner import PlannedKriaTurn
from app.models import Job, PlanItem
from app.routes.admin_plan_items import get_plan_item_proposal_trace
from tests.agents.conftest import MockModelClient
from tests.agents.test_music_matcher import _labels
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

MATCHER = "nova.audio.music_matcher"
CLIPS = ("julio-1", "julio-2", "julio-3")


@pytest.fixture(autouse=True)
def _catalog_song_freeze_switch_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """KRI-1097 (owner rule: catalog songs never, uploads only): a creator draft freezes no
    catalog song by default, so the chat-time song match this file covers runs only behind
    ``CREATOR_JOBS_NO_CATALOG_SONG_FREEZE_ENABLED=false`` (the Assos scorecard's check 13
    covers the default)."""
    monkeypatch.setattr(settings, "creator_jobs_no_catalog_song_freeze_enabled", False)


@pytest.fixture
def song_catalog(monkeypatch: pytest.MonkeyPatch) -> tuple[SimpleNamespace, MockModelClient]:
    """One analyzed track and a model client that ranks it first on every call."""
    import app.agents._model_client as model_client
    import app.services.music_sections as music_sections
    import app.tasks.auto_music_orchestrate as amo

    track = SimpleNamespace(
        id=str(uuid.uuid4()),
        title="Sunday Lunch",
        artist="Test Band",
        duration_s=180.0,
        ai_labels={"labels": _labels(energy="medium", vibe_tags=["warm"])},
        lyrics_cached=None,
        beat_timestamps_s=[],
    )
    client = MockModelClient()
    for _ in range(4):
        client.queue(
            "gemini-2.5-flash",
            {"ranked": [{"track_id": track.id, "score": 9.0, "rationale": "warm lunch"}]},
        )
    monkeypatch.setattr(amo, "_load_matcher_candidates", lambda *a, **k: [track])
    monkeypatch.setattr(amo, "_track_slot_count", lambda *a, **k: 4)
    monkeypatch.setattr(model_client, "default_client", lambda: client)
    monkeypatch.setattr(
        music_sections,
        "track_config_with_rank_one",
        lambda _t: {"best_start_s": 10.0, "best_end_s": 40.0},
    )
    return track, client


@pytest.fixture
def production_ledger(monkeypatch: pytest.MonkeyPatch):
    """Paid-call cost control on, as production would run it (failure modes 4-6)."""
    monkeypatch.setattr(settings, "ai_cost_control_enabled", True)
    monkeypatch.setattr(settings, "ai_usage_environment", "production")
    monkeypatch.setattr(settings, "ai_production_monthly_budget_usd", 1_000.0)
    monkeypatch.setattr(settings, "ai_reservation_ttl_seconds", 300)
    creators: list[uuid.UUID] = []
    yield creators
    with sync_engine.begin() as conn:
        for creator_id in creators:
            conn.execute(
                text("DELETE FROM ai_cost_reservations WHERE creator_id = :id"),
                {"id": str(creator_id)},
            )


def _seed_chat_project() -> tuple[uuid.UUID, uuid.UUID, uuid.UUID]:
    """A runtime-v2 thread whose plan item carries three playable clips."""
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        thread_item = db.execute(
            text("SELECT active_plan_item_id FROM creation_threads WHERE id = :id"),
            {"id": str(thread_id)},
        ).scalar_one()
        item = db.get(PlanItem, thread_item, with_for_update=True)
        item.clip_assignments = [
            {
                "media_id": media_id,
                "kind": "video",
                "duration_s": 6.0,
                "gcs_path": f"users/u/analysis-proxy-{media_id}.mp4",
                "analysis": {"description": "A waiter brings sauces to the table"},
            }
            for media_id in CLIPS
        ]
        db.commit()
    return user_id, thread_id, thread_item


def _planned() -> PlannedKriaTurn:
    strategy = CreativeStrategy(
        direction="guided_story",
        edit_format="montage",
        audio_strategy="licensed_music",
        pacing="fast",
        render_program="guided",
        rationale="A lunch montage.",
    )
    plan = planner.adapt_creator_action(
        ProposeStrategy(kind="propose_strategy", strategy=strategy, summary="A lunch montage.")
    )
    return PlannedKriaTurn(plan=plan, manifest_hash="a" * 64, context_hash="b" * 64)


async def _chat_turn(
    monkeypatch: pytest.MonkeyPatch, *, thread_id: uuid.UUID, item_id: uuid.UUID, user_id
) -> dict:
    """One live chat turn through the real ``plan_live_turn``; returns the frozen song."""

    async def _main_creator_planned(db, **_kwargs):  # noqa: ANN001, ANN202
        return _planned()

    monkeypatch.setattr(planner, "_plan_live_turn", _main_creator_planned)
    monkeypatch.setattr(settings, "kria_choice_questions_enabled", True)
    async with AsyncSessionLocal() as db:
        planned = await planner.plan_live_turn(
            db,
            thread_id=thread_id,
            item_id=item_id,
            creator_id=user_id,
            user_message="Make a lunch montage of the waiter and the sauces",
        )
    strategy = planned.plan.intents[0].arguments["strategy"] if planned.plan.intents else {}
    music = strategy.get("selected_music_section")
    if music is None:
        # A later gate step may turn the plan into a question; the freeze already ran
        # and its result is what the length question would carry.
        music = (planned.plan.choice_question or {}).get("length_context", {}).get("licensed_music")
    return music or {}


async def _trace_runs(item_id: uuid.UUID) -> list:
    async with AsyncSessionLocal() as db:
        trace = await get_plan_item_proposal_trace(
            str(item_id), limit=100, cursor=None, db=db, _=None
        )
    return [run for run in trace.agent_runs if run.agent_name == MATCHER]


@pytest.mark.asyncio
async def test_chat_song_match_is_in_the_plan_item_trace_and_never_binds_the_thread(
    monkeypatch: pytest.MonkeyPatch, song_catalog, production_ledger
) -> None:
    track, client = song_catalog
    user_id, thread_id, item_id = _seed_chat_project()
    production_ledger.append(user_id)
    try:
        with capture_logs() as logs:
            first = await _chat_turn(
                monkeypatch, thread_id=thread_id, item_id=item_id, user_id=user_id
            )
            # Failure mode 6: the creator asks again with the same clips.
            second = await _chat_turn(
                monkeypatch, thread_id=thread_id, item_id=item_id, user_id=user_id
            )

        # The creator gets the matched song both times (failure modes 4-6).
        assert len(client.invocations) == 2
        for music in (first, second):
            assert music.get("status") == "selected", music
            assert music.get("track_id") == track.id

        # Failure modes 1-3: both runs are in the plan item's proposal trace, unbound
        # from any render job, and nothing failed to persist.
        assert not [e for e in logs if e["event"] == "agent_run_persist_failed"]
        runs = await _trace_runs(item_id)
        assert len(runs) == 2
        assert [run.outcome for run in runs] == ["ok", "ok"]
        # The trace payload has no owner columns; read them from the row itself.
        with sync_engine.connect() as conn:
            owners = conn.execute(
                text(
                    "SELECT job_id, plan_item_id FROM agent_run "
                    "WHERE agent_name = :agent AND plan_item_id = :item"
                ),
                {"agent": MATCHER, "item": str(item_id)},
            ).all()
            reservations = conn.execute(
                text(
                    "SELECT job_id, creator_id, status FROM ai_cost_reservations "
                    "WHERE creator_id = :creator"
                ),
                {"creator": str(user_id)},
            ).all()
        assert [(o.job_id, o.plan_item_id) for o in owners] == [(None, item_id)] * 2
        # Failure modes 4-5: the paid call is attributed to the creator, never a job.
        assert len(reservations) == 2
        assert {(r.job_id, r.creator_id, r.status) for r in reservations} == {
            (None, user_id, "settled")
        }

        # Failure mode 8: the log line still names the plan item, and no id is the thread.
        (done, _again) = [e for e in logs if e["event"] == "auto_music_matcher_done"]
        assert done["plan_item_id"] == str(item_id)
        assert str(thread_id) not in {str(done.get("job_id")), str(done.get("plan_item_id"))}
    finally:
        await async_engine.dispose()


def test_render_time_song_match_still_records_its_job(song_catalog) -> None:
    """Failure mode 7: a render job's matcher run stays on that job."""
    from app.tasks import generative_build as gb

    track, _client = song_catalog
    user_id, _thread_id, item_id = _seed_chat_project()
    job_id = uuid.uuid4()
    with sync_session() as db:
        db.add(
            Job(
                id=job_id,
                user_id=user_id,
                status="processing",
                mode="generative",
                raw_storage_path="",
                selected_platforms=["tiktok"],
                content_plan_item_id=item_id,
            )
        )
        db.commit()
    meta = SimpleNamespace(clip_id="julio-1", duration_s=6.0, detected_subject="sauces")
    with capture_logs() as logs:
        matched = gb._match_best_track([meta], job_id=str(job_id))

    assert matched is track
    assert not [e for e in logs if e["event"] == "agent_run_persist_failed"]
    with sync_engine.connect() as conn:
        owners = conn.execute(
            text(
                "SELECT job_id, plan_item_id FROM agent_run WHERE agent_name = :a AND job_id = :j"
            ),
            {"a": MATCHER, "j": str(job_id)},
        ).all()
    assert [(o.job_id, o.plan_item_id) for o in owners] == [(job_id, None)]
