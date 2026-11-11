"""Failures specified before implementation: request reuse, target drift, mutable inputs.

The database race/commit boundary is exercised separately in the Postgres journey.
These tests require no database, renderer, provider, or broker.
"""

from __future__ import annotations

import copy
import json
import uuid
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.routes import plan_items
from app.schemas.slide_post import SlidePostDraft, SlideRef


def _service():
    from app.services import slide_post_generation_receipts

    return slide_post_generation_receipts


def _case():
    svc = _service()
    owner, item_id, job_id, asset_id = (uuid.uuid4() for _ in range(4))
    request = svc.SlidePostGenerationRequest(uuid.uuid4(), owner, item_id, 3)
    item = SimpleNamespace(id=item_id, current_job_id=job_id)
    job = SimpleNamespace(
        id=job_id,
        user_id=owner,
        content_plan_item_id=item_id,
        content_plan_ownership_epoch=4,
        status="variants_ready",
        assembly_plan={},
    )
    draft = SlidePostDraft(
        version=3,
        caption="Authorized",
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="one", asset_id=asset_id, kind="image")],
    )
    asset = SimpleNamespace(
        id=asset_id,
        user_id=owner,
        plan_item_id=item_id,
        media_status="ready",
        kind="image",
        gcs_path=f"users/{owner}/plan/{item_id}/one.jpg",
        gcs_generation="7",
    )
    db = Mock()
    db.execute.return_value.scalars.return_value.all.return_value = [asset]
    row = svc.capture_receipt(db, request, item=item, job=job, draft=draft, ownership_epoch=4)
    return svc, request, row, item, job, asset, db


def test_client_uuid_is_validated_and_state_advertises_retry_capability(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "slide_post_generation_recovery_enabled", True)
    from pydantic import ValidationError

    key = uuid.uuid4()
    body = plan_items.SlidePostGenerateBody(expected_version=3, client_request_id=str(key))
    assert body.client_request_id == key
    with pytest.raises(ValidationError):
        plan_items.SlidePostGenerateBody(expected_version=3, client_request_id="invalid")
    state = plan_items.SlidePostState(item_id="item", title="Post", render_status="draft")
    assert state.model_dump()["generation_idempotency_version"] == 1


def test_keyed_request_cannot_silently_ignore_different_payload():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        plan_items.SlidePostGenerationRequestBody.model_validate(
            {
                "expected_version": 3,
                "client_request_id": str(uuid.uuid4()),
                "expected_current_job_id": None,
                "draft": {"version": 4},
            }
        )


@pytest.mark.parametrize("field", ["owner_id", "item_id", "expected_version"])
def test_uuid_reuse_cannot_change_authority_or_payload(field):
    svc, request, row, *_ = _case()
    values = dict(
        client_request_id=request.client_request_id,
        owner_id=request.owner_id,
        item_id=request.item_id,
        expected_version=request.expected_version,
    )
    values[field] = 4 if field == "expected_version" else uuid.uuid4()
    with pytest.raises(svc.ReceiptConflict):
        svc.validate_receipt(row, svc.SlidePostGenerationRequest(**values))


@pytest.mark.parametrize("status", ["variants_ready", "processing_failed", "queued", None])
def test_historical_receipt_keeps_job_when_target_changes_or_job_disappears(status):
    svc, request, row, item, job, _, _db = _case()
    original = copy.deepcopy(row.draft_snapshot)
    item.current_job_id = uuid.uuid4()
    job.status = status
    out = svc.receipt_output(row, item=item, job=job if status else None)
    assert out.job_id == job.id
    assert out.draft_version == 3
    assert out.is_current_target is False
    assert out.job_status == status
    assert out.dispatch_state == "unconfirmed"
    assert row.draft_snapshot == original
    svc.validate_receipt(row, request)


def test_snapshot_is_independent_of_job_json_and_rejects_wrong_job_identity():
    svc, _, row, _, job, _, db = _case()
    db.execute.return_value.scalar_one_or_none.return_value = row
    job.assembly_plan = {"variants": [{"id": "slides"}], "unrelated": "rewrite"}
    draft, assets = svc.first_render_snapshot(db, job)
    assert draft.caption == "Authorized"
    assert draft.version == 3
    assert next(iter(assets.values()))["generation"] == "7"
    job.user_id = uuid.uuid4()
    with pytest.raises(svc.ReceiptConflict):
        svc.first_render_snapshot(db, job)


def test_snapshot_rejects_replaced_or_unowned_input_before_receipt_is_added():
    svc, request, _, item, job, asset, db = _case()
    db.reset_mock()
    asset.user_id = uuid.uuid4()
    draft = SlidePostDraft(
        version=3,
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="one", asset_id=asset.id, kind="image")],
    )
    with pytest.raises(svc.ReceiptConflict):
        svc.capture_receipt(db, request, item=item, job=job, draft=draft, ownership_epoch=4)
    db.add.assert_not_called()


def test_keyed_mobile_contract_requires_uuid_and_has_its_own_path():
    from app.cli.kria_contracts import mobile_openapi_json

    schema = json.loads(mobile_openapi_json())
    path = schema["paths"]["/plan-items/{item_id}/slide-post/generation-requests"]["post"]
    assert path["requestBody"]["required"] is True
    body = schema["components"]["schemas"]["SlidePostGenerationRequestBody"]
    assert set(body["required"]) == {
        "client_request_id",
        "expected_version",
        "expected_current_job_id",
    }
    assert (
        "generation_receipt"
        in schema["components"]["schemas"]["SlidePostMutationResponse"]["properties"]
    )


@pytest.mark.asyncio
async def test_access_denial_precedes_receipt_lookup(monkeypatch):
    from unittest.mock import AsyncMock

    from fastapi import HTTPException

    svc = _service()
    dispatch = Mock(side_effect=AssertionError("unauthorized receipt lookup"))
    monkeypatch.setattr(svc, "dispatch_generation", dispatch)
    monkeypatch.setattr(plan_items, "_require_slide_posts", lambda: None)
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(side_effect=HTTPException(status_code=404, detail="not found")),
    )
    with pytest.raises(HTTPException) as raised:
        await plan_items.request_slide_post_generation(
            str(uuid.uuid4()),
            plan_items.SlidePostGenerationRequestBody(
                expected_version=1, client_request_id=uuid.uuid4(), expected_current_job_id=None
            ),
            SimpleNamespace(id=uuid.uuid4()),
            AsyncMock(),
        )
    assert raised.value.status_code == 404
    dispatch.assert_not_called()
