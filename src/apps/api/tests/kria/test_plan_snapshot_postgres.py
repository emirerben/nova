"""KRI-439/447/448/453: the live plan feed against real SQL (scratch Postgres).

Drives the real writers (`emit_plan_blocks`, `dispatch_payload`, `emit_skipped_remainder`,
`emit_post_caption`) and the real `GET /creation-threads/{id}/plan` handler, so the revision
logic, the thread lock, the scoped-dispatch copy and the reduced snapshot are proven together.
Mocks cannot show that two jobs' events reduce to the right "Updated" state.
"""

from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
import structlog
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from starlette.requests import Request

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.database import engine as async_engine
from app.kria import plan_blocks
from app.kria.plan_contract import SECTION_ORDER, PlanSnapshotOut
from app.kria.plan_snapshot import read_plan_snapshot
from app.kria.runtime import RuntimeFailure
from app.models import CreationThread, CreationThreadEvent, Job, PlanItem
from app.routes.kria_runtime import get_creation_plan
from app.tasks.kria_runtime import _append_sync_event
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

b = plan_blocks.block


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "kria_runtime_v2_enabled", True)
    monkeypatch.setattr(settings, "live_plan_review_enabled", True)
    monkeypatch.setattr(
        "app.kria.plan_payloads.lookup_track_meta", lambda _tid: None
    )  # no MusicTrack rows in the scratch DB


@pytest.fixture(autouse=True)
async def _fresh_async_pool():
    """Each test has its own event loop; drop pooled asyncpg connections bound to the last."""
    yield
    await async_engine.dispose()


def _job(user_id: uuid.UUID, item_id: uuid.UUID, *, variant: dict | None = None) -> Job:
    job = Job(
        user_id=user_id,
        status="processing",
        mode="generative",
        raw_storage_path="",
        selected_platforms=["tiktok"],
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=0,
        assembly_plan={"variants": [{"variant_id": "v1", "ok": True, **(variant or {})}]},
    )
    with sync_session() as db:
        db.add(job)
        db.flush()
        job_id = job.id
        db.commit()
    return job_id  # type: ignore[return-value]


def _point_item_at(item_id: uuid.UUID, job_id: uuid.UUID) -> None:
    with sync_session() as db:
        item = db.get(PlanItem, item_id)
        item.current_job_id = job_id
        db.commit()


def _item_id(thread_id: uuid.UUID) -> uuid.UUID:
    with sync_session() as db:
        return db.get(CreationThread, thread_id).active_plan_item_id


async def _snapshot(user_id: uuid.UUID, thread_id: uuid.UUID) -> PlanSnapshotOut:
    async with AsyncSessionLocal() as db:
        return await read_plan_snapshot(db, thread_id=thread_id, creator_id=user_id)


def _decide_all(job_id: uuid.UUID, **payloads: dict) -> None:
    out = []
    for section in SECTION_ORDER:
        payload = payloads.get(section)
        out.append(
            b(section, "decided", f"{section} summary", payload=payload)
            if payload is not None or section in payloads
            else b(section, "decided", "Not used", skipped=True)
        )
    plan_blocks.emit_plan_blocks(job_id, out)


def _events(thread_id: uuid.UUID, kind: str) -> list[CreationThreadEvent]:
    with sync_session() as db:
        return list(
            db.execute(
                select(CreationThreadEvent)
                .where(
                    CreationThreadEvent.thread_id == thread_id,
                    CreationThreadEvent.event_type == kind,
                )
                .order_by(CreationThreadEvent.sequence)
            ).scalars()
        )


def _dispatch(thread_id: uuid.UUID, job_id: uuid.UUID, *, scope: list[str] | None) -> None:
    """What `_finish_approval_dispatch` does: the first plan_block event of the job, with the
    turn's `scope` read off its user_message event."""
    with sync_session() as db:
        thread = db.execute(
            select(CreationThread).where(CreationThread.id == thread_id).with_for_update()
        ).scalar_one()
        source = _append_sync_event(
            db,
            thread,
            role="user",
            event_type="user_message",
            content="Update: captions",
            payload={"scope": scope} if scope else {},
        )
        _append_sync_event(
            db,
            thread,
            role="system",
            event_type="plan_block",
            content=None,
            payload=plan_blocks.dispatch_payload(
                db, thread, turn_id=None, job_id=str(job_id), source_event_id=source.id
            ),
        )
        db.commit()


@pytest.mark.asyncio
async def test_first_render_reduces_to_ready_with_every_section() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    item_id = _item_id(thread_id)
    empty = await _snapshot(user_id, thread_id)
    assert empty.status == "empty" and empty.blocks == []

    job1 = _job(user_id, item_id)
    _dispatch(thread_id, job1, scope=None)
    waiting = await _snapshot(user_id, thread_id)
    assert waiting.status == "planning" and waiting.decided_count == 0
    assert [blk.section_id for blk in waiting.blocks] == list(SECTION_ORDER)

    _decide_all(
        job1,
        title={"text": "Day one", "bar_id": "t1"},
        clips={"total_duration_s": 6.0, "clips": [{"index": 0, "start_s": 0, "end_s": 6}]},
    )
    _point_item_at(item_id, job1)
    ready = await _snapshot(user_id, thread_id)
    assert ready.status == "ready" and ready.decided_count == 8 and ready.scope is None
    by_id = {blk.section_id: blk for blk in ready.blocks}
    assert by_id["title"].payload == {"text": "Day one", "bar_id": "t1"}
    assert (by_id["title"].revision, by_id["title"].changed, by_id["title"].previous) == (
        1,
        False,
        None,
    )
    # Editable only where the section was used and the thread has a rendered variant.
    assert by_id["title"].editable and by_id["clips"].editable
    assert not by_id["captions"].editable  # skipped: "Not used"
    assert not by_id["post_caption"].editable  # display-only in v2
    assert ready.draft is None  # the read never bootstraps a draft
    assert ready.next_after_sequence >= 0 and ready.previous_job_id is None


@pytest.mark.asyncio
async def test_scoped_update_copies_untouched_sections_and_reports_what_changed() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    item_id = _item_id(thread_id)
    old_captions = {
        "count": 1,
        "lines": [{"id": "cue-0", "kind": "cue", "text": "hello", "start_s": 0, "end_s": 1}],
    }
    job1 = _job(user_id, item_id)
    _dispatch(thread_id, job1, scope=None)
    _decide_all(job1, title={"text": "Day one"}, captions=old_captions)
    _point_item_at(item_id, job1)

    new_captions = {
        "count": 1,
        "lines": [{"id": "cue-0", "kind": "cue", "text": "hi", "start_s": 0, "end_s": 1}],
    }
    job2 = _job(user_id, item_id)
    _dispatch(thread_id, job2, scope=["captions"])
    mid = await _snapshot(user_id, thread_id)
    by_id = {blk.section_id: blk for blk in mid.blocks}
    assert mid.status == "updating" and mid.scope == ["captions"]
    assert mid.previous_job_id == str(job1)
    assert by_id["captions"].state == "deciding"
    # The feed never goes blank on an update: untouched sections are copied, unchanged.
    assert by_id["title"].state == "decided" and by_id["title"].payload == {"text": "Day one"}
    assert (by_id["title"].revision, by_id["title"].changed) == (1, False)
    assert mid.decided_count == 7

    plan_blocks.emit_plan_blocks(
        job2, [b("captions", "decided", "captions summary", payload=new_captions)]
    )
    # The render comes back with the OTHER sections decided again, equal: still not changed.
    plan_blocks.emit_plan_blocks(
        job2, [b("title", "decided", "title summary", payload={"text": "Day one"})]
    )
    plan_blocks.emit_skipped_remainder(job2)  # sweep + update summary (post_caption copied)
    done = await _snapshot(user_id, thread_id)
    by_id = {blk.section_id: blk for blk in done.blocks}
    assert done.status == "ready" and done.scope is None
    assert (by_id["captions"].revision, by_id["captions"].changed) == (2, True)
    assert by_id["captions"].previous.summary == "captions summary"
    assert by_id["captions"].previous.payload == old_captions
    assert by_id["captions"].previous.job_id == str(job1)
    assert [s for s, blk in ((k, v) for k, v in by_id.items()) if blk.changed] == ["captions"]
    assert done.update_summary is not None
    assert done.update_summary.text == "Updated captions."
    assert done.update_summary.changed_sections == ["captions"]
    assert len(_events(thread_id, plan_blocks.SUMMARY_EVENT_TYPE)) == 1
    plan_blocks.emit_skipped_remainder(job2)  # idempotent: one summary per job
    assert len(_events(thread_id, plan_blocks.SUMMARY_EVENT_TYPE)) == 1

    # Undoing toggles: the next job returns to the first value, and `previous` is what was undone.
    job3 = _job(user_id, item_id)
    _dispatch(thread_id, job3, scope=["captions"])
    plan_blocks.emit_plan_blocks(
        job3, [b("captions", "decided", "captions summary", payload=old_captions)]
    )
    undone = {blk.section_id: blk for blk in (await _snapshot(user_id, thread_id)).blocks}
    assert (undone["captions"].revision, undone["captions"].changed) == (3, True)
    assert undone["captions"].previous.payload == new_captions


@pytest.mark.asyncio
async def test_event_payloads_never_store_signed_urls_and_the_snapshot_fills_them() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    item_id = _item_id(thread_id)
    job = _job(user_id, item_id)
    _dispatch(thread_id, job, scope=None)
    _decide_all(
        job,
        clips={
            "total_duration_s": 2.0,
            "clips": [{"index": 0, "kind": "image", "media_id": "m0", "start_s": 0, "end_s": 2}],
        },
    )
    with sync_session() as db:
        stored = [e.payload for e in _events(thread_id, "plan_block")]
        assert "thumbnail_url" not in str(stored) and "art_url" not in str(stored)
        row = db.get(Job, job)
        row.assembly_plan = {
            **row.assembly_plan,
            "guided_story_execution_plan": {
                "story_timeline": [
                    {"media_id": "m0", "kind": "image", "gcs_path": "uploads/m0.jpg"}
                ]
            },
        }
        db.commit()
    import app.storage as storage

    real = storage.signed_get_url
    storage.signed_get_url = lambda path, _ttl=5: f"https://signed.test/{path}"
    try:
        snap = await _snapshot(user_id, thread_id)
    finally:
        storage.signed_get_url = real
    clip = next(blk for blk in snap.blocks if blk.section_id == "clips").payload["clips"][0]
    assert clip["thumbnail_url"] == "https://signed.test/uploads/m0.jpg"


@pytest.mark.asyncio
async def test_route_errors_and_ownership() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    stranger = SimpleNamespace(id=uuid.uuid4())
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    async with AsyncSessionLocal() as db:
        response = await get_creation_plan(request, str(thread_id), stranger, db)
        assert response.status_code == 404 and b"thread_not_found" in response.body
        missing = await get_creation_plan(request, "not-a-uuid", stranger, db)
        assert missing.status_code == 404
        settings.live_plan_review_enabled = False
        try:
            off = await get_creation_plan(request, str(thread_id), SimpleNamespace(id=user_id), db)
        finally:
            settings.live_plan_review_enabled = True
        assert off.status_code == 404 and b"live_plan_review_unavailable" in off.body
        ok = await get_creation_plan(request, str(thread_id), SimpleNamespace(id=user_id), db)
        assert ok.status == "empty"
    with pytest.raises(RuntimeFailure):
        await _snapshot(stranger.id, thread_id)


@pytest.mark.asyncio
async def test_flag_off_writes_no_events() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    job = _job(user_id, _item_id(thread_id))
    settings.live_plan_review_enabled = False
    try:
        plan_blocks.emit_plan_blocks(job, plan_blocks.waiting_blocks())
        plan_blocks.emit_skipped_remainder(job)
        plan_blocks.emit_post_caption(job)
    finally:
        settings.live_plan_review_enabled = True
    assert _events(thread_id, "plan_block") == []


# ── post caption (KRI-448) ───────────────────────────────────────────────────


def _titled_job(user_id: uuid.UUID, thread_id: uuid.UUID, *, variant: dict | None = None):
    job = _job(user_id, _item_id(thread_id), variant=variant)
    plan_blocks.emit_plan_blocks(job, [b("title", "decided", "Athens", payload={"text": "Athens"})])
    return job


def _post_caption(job: uuid.UUID) -> dict:
    with sync_session() as db:
        variant = db.get(Job, job).assembly_plan["variants"][0]
    return variant


async def _post_caption_block(user_id, thread_id):
    snap = await _snapshot(user_id, thread_id)
    return next(blk for blk in snap.blocks if blk.section_id == "post_caption")


@pytest.mark.asyncio
async def test_post_caption_is_generated_persisted_and_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake(hook: str, job_id: str):
        calls.append(hook)
        return {"text": "Athens in a day", "hashtags": ["athens", "travel"], "platform": "tiktok"}

    monkeypatch.setattr(plan_blocks, "_generate_post_caption", fake)
    user_id, thread_id, _ = _seed_runtime_project()
    job = _titled_job(user_id, thread_id)  # the title block starts the copy in the background
    plan_blocks.emit_post_caption(job)
    plan_blocks.emit_post_caption(job)  # resolved once; the second call is a no-op
    assert calls == ["Athens"]
    blk = await _post_caption_block(user_id, thread_id)
    assert blk.state == "decided" and not blk.skipped
    assert blk.payload == {
        "text": "Athens in a day",
        "hashtags": ["athens", "travel"],
        "platform": "tiktok",
    }
    stored = _post_caption(job)["post_caption"]
    assert stored == {
        "text": "Athens in a day",
        "hashtags": ["athens", "travel"],
        "source": "platform_copy",
    }


@pytest.mark.asyncio
async def test_post_caption_prefers_the_variant_copy_and_skips_on_failure_or_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    boom = []
    monkeypatch.setattr(plan_blocks, "_generate_post_caption", lambda *a: boom.append(a))
    job = _titled_job(
        user_id,
        thread_id,
        variant={"post_caption": {"text": "From the template", "hashtags": ["#fyp"]}},
    )
    plan_blocks.emit_post_caption(job)
    blk = await _post_caption_block(user_id, thread_id)
    assert blk.payload["text"] == "From the template" and blk.payload["hashtags"] == ["fyp"]

    def raising(hook: str, job_id: str):
        raise RuntimeError("model down")

    monkeypatch.setattr(plan_blocks, "_generate_post_caption", raising)
    user2, thread2, _ = _seed_runtime_project()
    failed = _titled_job(user2, thread2)
    plan_blocks.emit_post_caption(failed)
    blk = await _post_caption_block(user2, thread2)
    assert blk.state == "decided" and blk.skipped and blk.payload is None

    monkeypatch.setattr(plan_blocks, "POST_CAPTION_TIMEOUT_S", 0.0)
    monkeypatch.setattr(
        plan_blocks,
        "_generate_post_caption",
        lambda hook, job_id: time.sleep(1.5) or {"text": "late", "hashtags": []},
    )
    user3, thread3, _ = _seed_runtime_project()
    slow = _titled_job(user3, thread3)
    started = time.monotonic()
    plan_blocks.emit_post_caption(slow)
    assert time.monotonic() - started < 1.4  # bounded: it does not wait for the slow model
    blk = await _post_caption_block(user3, thread3)
    assert blk.skipped
    assert "post_caption" not in _post_caption(slow)


# ── metrics (KRI-453) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_metrics_events_carry_ids_and_never_text() -> None:
    user_id, thread_id, _ = _seed_runtime_project()
    job = _job(user_id, _item_id(thread_id))
    with structlog.testing.capture_logs() as logs:
        plan_blocks.emit_plan_blocks(
            job, [b("title", "decided", "Secret caption text", payload={"text": "Secret"})]
        )
        await _snapshot(user_id, thread_id)
    emitted = next(e for e in logs if e["event"] == "plan_block_emitted")
    assert emitted["job_id"] == str(job)
    assert emitted["sections"] == ["title"] and emitted["states"] == ["decided"]
    assert emitted["changed_sections"] == []
    read = next(e for e in logs if e["event"] == "plan_snapshot_read")
    assert read["thread_id"] == str(thread_id) and read["status"] == "planning"
    assert "Secret" not in str(logs)


@pytest.mark.asyncio
async def test_cloud_path_is_filled_from_the_rendered_variant_at_finalize(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The non-guided cloud path only knows a clip count mid-render; the finalize sweep
    fills clips/captions/sfx from the rendered variant instead of calling them 'Not used'."""
    monkeypatch.setattr(
        plan_blocks, "_generate_post_caption", lambda hook, job_id: None
    )  # the model returns nothing
    user_id, thread_id, _ = _seed_runtime_project()
    job = _job(
        user_id,
        _item_id(thread_id),
        variant={
            "duration_s": 9.0,
            "ai_timeline": {
                "slots": [
                    {"order": 0, "duration_s": 5.0, "in_s": 0.0},
                    {"order": 1, "duration_s": 4.0, "in_s": 3.0},
                ]
            },
            "caption_cues": [{"text": "hello there", "start_s": 0.5, "end_s": 1.5}],
            "sound_effects": [{"id": "s1", "label": "Pop", "at_s": 4.0}],
        },
    )
    plan_blocks.emit_plan_blocks(
        job,
        [
            b("clips", "decided", "2 clips · 9s"),
            b("title", "decided", "Hi", payload={"text": "Hi"}),
            b("look", "decided", "Not used", skipped=True),
            b("music", "decided", "Not used", skipped=True),
        ],
    )
    plan_blocks.emit_skipped_remainder(job)
    _point_item_at(_item_id(thread_id), job)
    snap = await _snapshot(user_id, thread_id)
    by_id = {blk.section_id: blk for blk in snap.blocks}
    assert snap.status == "ready"
    assert by_id["clips"].summary == "2 clips · 9s"  # the old-client summary is kept
    assert [(c["start_s"], c["end_s"]) for c in by_id["clips"].payload["clips"]] == [
        (0.0, 5.0),
        (5.0, 9.0),
    ]
    assert by_id["clips"].revision == 1  # filled in at the same revision, not a new value
    assert by_id["captions"].payload["lines"][0]["text"] == "hello there"
    assert by_id["captions"].summary == "1 caption"
    assert by_id["sfx"].payload["count"] == 1
    assert by_id["music"].skipped and by_id["post_caption"].skipped
