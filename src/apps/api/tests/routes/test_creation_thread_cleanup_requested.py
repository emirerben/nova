"""Qendresa 13: the speech-cleanup card learns what the creator asked for in chat.

Failure modes written down before the code:

1. Flag off still adds `requested_in_chat` / `pause_style` keys (old clients and the pinned
   projection shapes would change).
2. A cleanup ask in chat does not mark the card `requested_in_chat`.
3. "Keep my pauses" or a plain "cut the long pauses" marks the card tight (a 0.3 s promise the
   render does not make).
4. A tight ask on a kind of edit that cannot cut tight (several clips, a non-Talking format, a
   cloud render) says `pause_style: tight`.
5. A broken/missing pending approval or brief breaks the thread read instead of adding nothing.
6. The keys leak through `_response` with the flag off.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.models import ContentPlan, Job, PlanItem
from app.routes import creation_threads as routes

PROXY = "users/u/analysis-proxy-clip.mp4"
TIGHT = "Cut out all my pauses so it's tight"


def _snapshot(text: str, *, cleanup_fact: bool = True) -> dict:
    req = BriefRequirement(
        id="r1",
        kind="audio",
        scope="global",
        description=text,
        facts={"speech_cleanup": True} if cleanup_fact else {},
    )
    binding = BriefBinding.create(uuid.uuid4(), CreativeBrief(version=1, requirements=[req]))
    return {
        "kind": "strategy",
        "strategy": {"edit_format": "subtitled", "audio_strategy": "original_audio"},
        "brief_binding": binding.model_dump(mode="json"),
    }


def _db(snapshot: object) -> Mock:
    db = Mock(spec=AsyncSession)
    result = Mock()
    result.scalar_one_or_none.return_value = snapshot
    db.execute = AsyncMock(return_value=result)
    return db


def _thread(runtime_version: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(), creator_id=uuid.uuid4(), runtime_version=runtime_version
    )


def _item(paths=(PROXY,), edit_format="subtitled") -> SimpleNamespace:
    return SimpleNamespace(edit_format=edit_format, clip_gcs_paths=list(paths))


@pytest.fixture(autouse=True)
def _phone_on(monkeypatch):
    monkeypatch.setattr(settings, "phone_rendering_enabled", True)
    monkeypatch.setattr(settings, "phone_render_user_ids", [])
    monkeypatch.setattr(
        "app.services.phone_rollout.phone_render_supported_formats",
        lambda: frozenset({"subtitled"}),
    )


@pytest.mark.asyncio
async def test_tight_ask_on_phone_talking_single_clip_is_requested_and_tight():
    assert await routes._cleanup_ask_projection(_db(_snapshot(TIGHT)), _thread(), _item()) == (
        True,
        True,
    )


@pytest.mark.asyncio
async def test_plain_cleanup_ask_is_requested_but_not_tight():
    snapshot = _snapshot("cut the long pauses")
    assert await routes._cleanup_ask_projection(_db(snapshot), _thread(), _item()) == (True, False)


@pytest.mark.asyncio
async def test_keep_my_pauses_is_neither():
    snapshot = _snapshot("keep my pauses", cleanup_fact=False)
    assert await routes._cleanup_ask_projection(_db(snapshot), _thread(), _item()) == (
        False,
        False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "item",
    [
        _item(paths=(PROXY, "users/u/analysis-proxy-two.mp4")),  # several clips
        _item(paths=("users/u/raw.mp4",)),  # not a phone analysis proxy
        _item(edit_format="montage"),  # not Talking
    ],
)
async def test_tight_ask_on_an_edit_that_cannot_cut_tight_is_requested_only(item):
    snapshot = _snapshot(TIGHT)
    snapshot["strategy"]["edit_format"] = item.edit_format
    assert await routes._cleanup_ask_projection(_db(snapshot), _thread(), item) == (True, False)


@pytest.mark.asyncio
@pytest.mark.parametrize("snapshot", [None, {"kind": "strategy"}, {"brief_binding": {"x": 1}}])
async def test_missing_or_unreadable_approval_adds_nothing(snapshot):
    assert await routes._cleanup_ask_projection(_db(snapshot), _thread(), _item()) == (
        False,
        False,
    )


@pytest.mark.asyncio
async def test_database_error_never_breaks_the_read():
    db = Mock(spec=AsyncSession)
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    assert await routes._cleanup_ask_projection(db, _thread(), _item()) == (False, False)


@pytest.mark.asyncio
async def test_runtime_v1_thread_reads_nothing():
    db = _db(_snapshot(TIGHT))
    assert await routes._cleanup_ask_projection(db, _thread(1), _item()) == (False, False)
    db.execute.assert_not_called()


# -- through `_response` -------------------------------------------------------------------------


async def _response_with(monkeypatch, *, flag: bool):
    owner_id = uuid.uuid4()
    plan_id, item_id = uuid.uuid4(), uuid.uuid4()
    item = SimpleNamespace(
        id=item_id,
        content_plan_id=plan_id,
        current_job_id=None,
        clip_gcs_paths=[PROXY],
        clip_assignments=[],
        edit_format="subtitled",
        audio_mode="kria",
        voiceover_gcs_path=None,
    )
    plan = SimpleNamespace(id=plan_id, user_id=owner_id, ownership_epoch=0)
    thread = SimpleNamespace(
        id=uuid.uuid4(),
        creator_id=owner_id,
        content_plan_id=plan_id,
        active_plan_item_id=item_id,
        active_creator_agent_session_id=None,
        active_job_id=None,
        status="active",
        revision=1,
        runtime_version=2,
        state={"media_count": 1},
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    async def get(model, _identifier, **_kwargs):
        return {PlanItem: item, ContentPlan: plan, Job: None}.get(model)

    count_result = Mock()
    count_result.scalar_one.return_value = 0
    events_result = Mock()
    events_result.scalars.return_value.all.return_value = []
    snapshot_result = Mock()
    snapshot_result.scalar_one_or_none.return_value = _snapshot(TIGHT)
    db = Mock(spec=AsyncSession)
    db.get = AsyncMock(side_effect=get)

    async def execute(statement, *_a, **_k):
        text = str(statement)
        if "creator_edit_drafts.snapshot_json" in text:
            return snapshot_result
        if "count(" in text.lower():
            return count_result
        return events_result

    db.execute = AsyncMock(side_effect=execute)
    monkeypatch.setattr(settings, "tight_pause_cut_enabled", flag)
    monkeypatch.setattr(settings, "speech_cleanup_preflight_mode", "enforce")
    monkeypatch.setattr(settings, "speech_cleanup_preflight_rollout_percent", 100)
    monkeypatch.setattr(
        "app.services.plan_item_media.resolve_item_narration",
        lambda *_a, **_k: SimpleNamespace(
            source=SimpleNamespace(source_policy_fingerprint="fp"), reason=None, video_present=True
        ),
    )
    row = SimpleNamespace(
        id=uuid.uuid4(),
        plan_item_id=item_id,
        status="queued",
        superseded_at=None,
        decision=None,
        decision_at=None,
        candidate_count=None,
        category_counts={},
        estimated_removed_ms=None,
        failure_code=None,
        failure_retryable=None,
        detector_version="mixed-gap-v2",
        engine_version="v2",
        source_kind="embedded_spine",
        source_media_identity="m",
        source_storage_path=PROXY,
        source_generation="1",
        window_start_s=0.0,
        window_end_s=12.0,
        source_policy_fingerprint="fp",
        analysis_payload={},
        diagnostic_receipt={},
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.current_analysis_async", AsyncMock(return_value=row)
    )
    monkeypatch.setattr(
        "app.services.speech_cleanup_preflight.preflight_enabled_for_source", lambda *a, **k: True
    )
    return await routes._response(db, thread)


@pytest.mark.asyncio
async def test_response_carries_the_two_keys_only_with_the_flag_on(monkeypatch):
    on = await _response_with(monkeypatch, flag=True)
    assert on.speech_cleanup is not None
    assert on.speech_cleanup["requested_in_chat"] is True
    assert on.speech_cleanup["pause_style"] == "tight"

    off = await _response_with(monkeypatch, flag=False)
    assert off.speech_cleanup is not None
    assert "requested_in_chat" not in off.speech_cleanup
    assert "pause_style" not in off.speech_cleanup
