"""KRI-1117: pinning a pool visual for a Talk-to-camera background at Save time (S19)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

import app.services.phone_visuals as phone_visuals
from app.services.phone_sources import PHONE_VISUALS_FIELD, PhoneVisualBinding
from app.services.phone_speaker_background import pin_pool_visual_for_background

ITEM_ID = uuid.uuid4()
ASSET_ID = uuid.uuid4()


def _binding(kind="image") -> PhoneVisualBinding:
    extra = (
        {"duration_s": 6.0, "width": 1080, "height": 1920, "orientation_degrees": 0}
        if kind == "video"
        else {}
    )
    return PhoneVisualBinding(
        media_id=str(ASSET_ID),
        gcs_path=f"users/u/plan/{ITEM_ID}/pool/a.jpg",
        generation="7",
        sha256="a" * 64,
        byte_count=10,
        kind=kind,
        **extra,
    )


def _db(row):
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = row
    db.execute = AsyncMock(return_value=result)
    return db


def _row(**changes):
    return SimpleNamespace(
        **{
            "id": ASSET_ID,
            "status": "ready",
            "kind": "image",
            "gcs_path": f"users/u/plan/{ITEM_ID}/pool/a.jpg",
            "gcs_generation": 7,
        }
        | changes
    )


def _job(plan=None):
    return SimpleNamespace(id=uuid.uuid4(), assembly_plan=plan if plan is not None else {})


async def _pin(db, job, **changes):
    await pin_pool_visual_for_background(
        db,
        job,
        plan_item_id=changes.pop("plan_item_id", ITEM_ID),
        media_id=changes.pop("media_id", str(ASSET_ID)),
        kind=changes.pop("kind", "image"),
    )


@pytest.mark.asyncio
async def test_a_ready_pool_row_is_bound_and_added_to_the_jobs_receipts(monkeypatch):
    seen = {}

    def bind(_open, *, job_id, pins):
        seen.update(job_id=job_id, pins=pins)
        return (_binding(),)

    monkeypatch.setattr(phone_visuals, "bind_phone_visual_assets", bind)
    job = _job({"keep": 1})
    await _pin(_db(_row()), job)

    assert seen["pins"] == {str(ASSET_ID): ("image", f"users/u/plan/{ITEM_ID}/pool/a.jpg", "7")}
    assert job.assembly_plan["keep"] == 1
    assert [r["media_id"] for r in job.assembly_plan[PHONE_VISUALS_FIELD]] == [str(ASSET_ID)]


@pytest.mark.asyncio
async def test_an_already_pinned_visual_is_not_bound_again(monkeypatch):
    monkeypatch.setattr(
        phone_visuals,
        "bind_phone_visual_assets",
        lambda *a, **k: pytest.fail("a pinned visual must not be downloaded again"),
    )
    plan = {PHONE_VISUALS_FIELD: [_binding().model_dump(mode="json")]}
    db = _db(None)
    await _pin(db, _job(plan))
    db.execute.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ["missing_row", "not_ready", "wrong_kind", "not_a_uuid"])
async def test_a_visual_outside_this_items_ready_pool_is_refused(monkeypatch, case):
    monkeypatch.setattr(
        phone_visuals,
        "bind_phone_visual_assets",
        lambda *a, **k: pytest.fail("a refused visual must not be downloaded"),
    )
    kwargs = {}
    row = _row()
    if case == "missing_row":
        row = None
    elif case == "not_ready":
        row = _row(status="analyzing")
    elif case == "wrong_kind":
        row = _row(kind="video")
    else:
        kwargs["media_id"] = "../../someone-else"
    job = _job()
    with pytest.raises(HTTPException) as refused:
        await _pin(_db(row), job, **kwargs)
    assert (refused.value.status_code, refused.value.detail) == (
        422,
        {"code": "speaker_background_media_not_in_pool"},
    )
    assert PHONE_VISUALS_FIELD not in job.assembly_plan


@pytest.mark.asyncio
async def test_unreadable_bytes_are_a_named_422_and_pin_nothing(monkeypatch):
    def bind(*_a, **_k):
        raise ValueError("approved photo bytes are unavailable")

    monkeypatch.setattr(phone_visuals, "bind_phone_visual_assets", bind)
    job = _job()
    with pytest.raises(HTTPException) as refused:
        await _pin(_db(_row()), job)
    assert refused.value.detail == {"code": "speaker_background_media_unavailable"}
    assert PHONE_VISUALS_FIELD not in job.assembly_plan
