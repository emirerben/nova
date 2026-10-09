"""KRI-441/442: a scoped editor update re-renders the SAME Job; the feed must still show it.

Real SQL, real `GET /plan` reducer. Proves the changed/revision/previous bookkeeping that
`plan_blocks.annotate_blocks` cannot produce for a same-Job update (it compares to a previous
JOB), and that section Undo reads the same block the creator sees.
"""

from __future__ import annotations

import copy
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm.attributes import flag_modified

from app.config import settings
from app.database import AsyncSessionLocal, sync_session
from app.kria import plan_blocks, plan_review
from app.models import CreationThread, Job
from tests.kria.test_plan_snapshot_postgres import (  # noqa: F401 - autouse fixtures
    _dispatch,
    _events,
    _flags,
    _fresh_async_pool,
    _item_id,
    _job,
    _point_item_at,
    _snapshot,
)
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

_VARIANT = {
    "duration_s": 9.0,
    "caption_cues": [{"id": "c0", "text": "hello", "start_s": 0.5, "end_s": 1.5}],
    "sound_effects": [{"id": "s1", "label": "Pop", "at_s": 4.0}],
}


def _feed(thread_id: uuid.UUID, job_id: uuid.UUID, variant: dict, scope: list[str]) -> list[str]:
    """What `_finish_approval_dispatch` does for a scoped editor commit."""
    with sync_session() as db:
        thread = db.execute(
            select(CreationThread).where(CreationThread.id == thread_id).with_for_update()
        ).scalar_one()
        job = db.get(Job, job_id)
        changed = plan_review.emit_scoped_update_feed(
            db, thread, turn_id=None, job=job, variant=variant, scope=scope
        )
        db.commit()
    return changed


def _set_cues(job_id: uuid.UUID, text_value: str) -> dict:
    with sync_session() as db:
        job = db.get(Job, job_id)
        plan = copy.deepcopy(job.assembly_plan)
        plan["variants"][0]["caption_cues"][0]["text"] = text_value
        job.assembly_plan = plan
        flag_modified(job, "assembly_plan")
        db.commit()
        return copy.deepcopy(plan["variants"][0])


@pytest.mark.asyncio
async def test_same_job_scoped_update_and_undo_reach_the_feed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(plan_blocks, "_generate_post_caption", lambda hook, job_id: None)
    user_id, thread_id, _ = _seed_runtime_project()
    item_id = _item_id(thread_id)
    job = _job(user_id, item_id, variant=_VARIANT)
    _dispatch(thread_id, job, scope=None)
    plan_blocks.emit_plan_blocks(
        job, [plan_blocks.block("title", "decided", "Hi", payload={"text": "Hi"})]
    )
    plan_blocks.emit_skipped_remainder(job)  # captions/sfx/clips come from the variant
    _point_item_at(item_id, job)
    first = {blk.section_id: blk for blk in (await _snapshot(user_id, thread_id)).blocks}
    assert first["captions"].revision == 1 and not first["captions"].changed
    old_payload = first["captions"].payload

    # Update: only the captions moved (a re-render of the same Job). Nothing else may change.
    variant = _set_cues(job, "hi")
    assert _feed(thread_id, job, variant, ["captions"]) == ["captions"]
    # A second dispatch with nothing moved writes nothing (no fake "Updated").
    assert _feed(thread_id, job, variant, ["captions"]) == []
    snap = await _snapshot(user_id, thread_id)
    by_id = {blk.section_id: blk for blk in snap.blocks}
    assert (by_id["captions"].revision, by_id["captions"].changed) == (2, True)
    assert by_id["captions"].payload["lines"][0]["text"] == "hi"
    assert by_id["captions"].previous.payload == old_payload
    assert [s for s, blk in by_id.items() if blk.changed] == ["captions"]
    assert (by_id["title"].revision, by_id["title"].changed) == (1, False)
    assert snap.status == "ready"
    assert snap.update_summary is not None and snap.update_summary.text == "Updated captions."

    # The undo's successor dispatch restores the old words: the same feed path toggles back,
    # and what Undo reads (`current_block`) is the block the creator sees.
    async with AsyncSessionLocal() as db:
        current = await plan_review.current_block(db, thread_id, "captions")
    assert current is not None and current["revision"] == 2 and current["changed"] is True
    variant = _set_cues(job, "hello")
    assert _feed(thread_id, job, variant, ["captions"]) == ["captions"]
    undone = {blk.section_id: blk for blk in (await _snapshot(user_id, thread_id)).blocks}
    assert (undone["captions"].revision, undone["captions"].changed) == (3, True)
    assert undone["captions"].payload == old_payload
    assert undone["captions"].previous.payload["lines"][0]["text"] == "hi"
    assert len(_events(thread_id, plan_blocks.SUMMARY_EVENT_TYPE)) == 2

    # Out-of-scope sections never move, even when the variant differs from the feed.
    variant = _set_cues(job, "other")
    assert _feed(thread_id, job, variant, ["title"]) == []
