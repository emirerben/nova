"""KRI-824: the brief ledger stamps authority from the creator's own stored turn.

The E2E (``test_kri824_photo_hold_e2e``) proves a stamped hold plays. This pins where the
stamp comes from in production: ``persist_brief_version_sync`` reads the turn's source
event (role ``user``) from the transcript and stamps from that text only.

Ways this can fail:
1. The stamp reads model output (the update's facts/description) instead of the transcript.
2. A turn of another thread, or a non-creator event, supplies the text.
3. A stored, stamped brief does not round-trip (authority lost on reload, approval digest
   changes for briefs without authority).
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

from app.config import settings
from app.database import sync_session
from app.kria.brief import BriefUpdate, load_latest_brief_sync, persist_brief_version_sync

PHOTO = "asset-00000000-0000-4000-8000-000000000001"


def _hold(seconds: float, description: str) -> BriefUpdate:
    return BriefUpdate(
        kind="timing",
        scope=f"clip:{PHOTO}",
        description=description,
        facts={"duration_s": seconds},
    )


async def _persist(message: str, update: BriefUpdate, monkeypatch: pytest.MonkeyPatch):
    from tests.kria.test_runtime_postgres_integration import (  # noqa: PLC0415
        _seed_runtime_project,
        _submit,
    )

    user_id, thread_id, _session_id = _seed_runtime_project()
    monkeypatch.setattr(settings, "kria_creative_brief_enabled", True)
    accepted = await _submit(user_id, thread_id, message, 2)
    turn_id = uuid.UUID(accepted.turn_id)

    def run():
        with sync_session() as db:
            persist_brief_version_sync(db, thread_id=thread_id, turn_id=turn_id, updates=(update,))
            db.commit()
        with sync_session() as db:
            return load_latest_brief_sync(db, thread_id), thread_id, turn_id

    return await asyncio.to_thread(run)


async def test_the_creators_stored_words_stamp_the_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    brief, _thread, _turn = await _persist(
        "Keep the beach photo on screen for 1 second", _hold(1, "beach photo"), monkeypatch
    )
    [req] = brief.live()
    assert req.authority == "explicit"
    # 3: it survives the store and is part of the dump approval digests.
    assert req.model_dump(mode="json")["authority"] == "explicit"


async def test_a_number_only_the_model_wrote_is_not_stamped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # 1: the update's own description says "1 second"; the creator never did.
    brief, _thread, _turn = await _persist(
        "Make a montage of these, photos quick",
        _hold(1, "beach photo on screen for 1 second"),
        monkeypatch,
    )
    [req] = brief.live()
    assert req.authority is None
    assert "authority" not in req.model_dump(mode="json")


async def test_another_threads_turn_supplies_no_text(monkeypatch: pytest.MonkeyPatch) -> None:
    _brief, _thread, turn_id = await _persist(
        "Keep the beach photo on screen for 1 second", _hold(1, "beach photo"), monkeypatch
    )
    from tests.kria.test_runtime_postgres_integration import (  # noqa: PLC0415
        _seed_runtime_project,
    )

    _user, other_thread, _session = _seed_runtime_project()

    def run():
        with sync_session() as db:
            persist_brief_version_sync(
                db, thread_id=other_thread, turn_id=turn_id, updates=(_hold(1, "beach photo"),)
            )
            db.commit()
        with sync_session() as db:
            return load_latest_brief_sync(db, other_thread)

    [req] = (await asyncio.to_thread(run)).live()
    # 2: the turn belongs to another thread, so nothing is stamped.
    assert req.authority is None
