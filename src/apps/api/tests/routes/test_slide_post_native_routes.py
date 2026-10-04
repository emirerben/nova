"""Native slide-post route guards that must stay fail-closed."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from app.routes import plan_items
from app.schemas.slide_post import SlidePostDraft, SlideRef
from app.tasks.content_plan_build import DispatchResult


def _user() -> SimpleNamespace:
    return SimpleNamespace(id=uuid.uuid4())


def _item() -> SimpleNamespace:
    asset_id = uuid.uuid4()
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="slide", asset_id=asset_id, kind="image")],
        version=2,
    )
    return SimpleNamespace(
        id=uuid.uuid4(), edit_format="slides", slide_post=draft.model_dump(mode="json")
    )


def test_native_state_schema_cannot_serialize_storage_paths() -> None:
    asset_fields = set(plan_items.SlidePostStateAsset.model_fields)
    state_fields = set(plan_items.SlidePostState.model_fields)
    assert "gcs_path" not in asset_fields
    assert "asset_gcs_path" not in state_fields
    assert "assembly_plan" not in state_fields


@pytest.mark.parametrize("status", ["cancelled", "superseded"])
def test_native_slide_state_never_exposes_outputs_for_terminal_job(status: str) -> None:
    assert not plan_items._slide_post_job_outputs_allowed(SimpleNamespace(status=status))
    assert plan_items._slide_post_job_outputs_allowed(SimpleNamespace(status="template_ready"))


@pytest.mark.asyncio
async def test_native_state_does_not_continue_after_ownership_rejection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    denied = HTTPException(status_code=404, detail="Plan item not found")
    load = AsyncMock(side_effect=denied)
    monkeypatch.setattr(plan_items, "_load_owned_item", load)
    with pytest.raises(HTTPException) as raised:
        await plan_items.get_slide_post_state(str(uuid.uuid4()), _user(), AsyncMock())
    assert raised.value.status_code == 404
    assert load.await_count == 1


@pytest.mark.asyncio
async def test_put_version_conflict_happens_before_asset_lookup_or_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    db = AsyncMock()
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    assets = AsyncMock()
    monkeypatch.setattr(plan_items, "_owned_ready_slide_assets", assets)
    body = plan_items.SlidePostDraftBody(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="slide", asset_id=uuid.uuid4(), kind="image")],
        expected_version=1,
    )
    with pytest.raises(HTTPException) as raised:
        await plan_items.put_slide_post_draft(str(item.id), body, _user(), db)
    assert raised.value.status_code == 409
    assert assets.await_count == 0
    assert db.commit.await_count == 0


@pytest.mark.asyncio
async def test_proposal_conflict_does_not_mutate_or_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    db = AsyncMock()
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    compose = AsyncMock()
    monkeypatch.setattr("app.services.slide_post_compose.propose_slide_post_draft", compose)
    body = plan_items.SlidePostProposeBody(
        expected_version=1,
        platform_profile="tiktok_photo",
        instruction="Make it bright",
    )
    with pytest.raises(HTTPException) as raised:
        await plan_items.propose_slide_post(SimpleNamespace(), str(item.id), body, _user(), db)
    assert raised.value.status_code == 409
    assert compose.await_count == 0
    assert db.commit.await_count == 0


@pytest.mark.asyncio
async def test_proposal_rejects_unknown_profile_before_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    db = AsyncMock()
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "_owned_ready_slide_assets", AsyncMock(return_value=[]))
    body = plan_items.SlidePostProposeBody(
        expected_version=2,
        platform_profile="not-a-profile",
        instruction="Make it bright",
    )
    with pytest.raises(HTTPException) as raised:
        await plan_items.propose_slide_post(SimpleNamespace(), str(item.id), body, _user(), db)
    assert raised.value.status_code == 422
    assert db.commit.await_count == 0


@pytest.mark.asyncio
async def test_slide_generate_uses_versioned_dispatch_without_creator_session_fence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    plan = SimpleNamespace(ownership_epoch=7)
    db = AsyncMock()
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    run_sync = AsyncMock(return_value=DispatchResult("already_active"))
    monkeypatch.setattr("anyio.to_thread.run_sync", run_sync)
    response = SimpleNamespace(ok=True)
    monkeypatch.setattr(plan_items, "_respond_to_dispatch_result", AsyncMock(return_value=response))

    result = await plan_items.generate_slide_post(
        str(item.id), plan_items.SlidePostGenerateBody(expected_version=2), _user(), db
    )
    assert result is response
    dispatched = run_sync.await_args.args[0]
    assert dispatched.keywords["expected_slide_post_version"] == 2
    assert dispatched.keywords["reject_active_creator_session"] is False


@pytest.mark.asyncio
async def test_slide_generate_rejects_stale_or_non_slide_draft(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    plan = SimpleNamespace(ownership_epoch=0)
    db = AsyncMock()
    monkeypatch.setattr(
        plan_items,
        "_load_owned_item_context",
        AsyncMock(return_value=(item, plan, SimpleNamespace())),
    )
    with pytest.raises(HTTPException) as stale:
        await plan_items.generate_slide_post(
            str(item.id), plan_items.SlidePostGenerateBody(expected_version=1), _user(), db
        )
    assert stale.value.status_code == 409
    item.edit_format = "montage"
    with pytest.raises(HTTPException) as wrong_format:
        await plan_items.generate_slide_post(
            str(item.id), plan_items.SlidePostGenerateBody(expected_version=2), _user(), db
        )
    assert wrong_format.value.status_code == 409


# ---- Rich per-slide text (KRI-299) ---------------------------------------------------


def _rich_item(asset_id: uuid.UUID) -> SimpleNamespace:
    from app.schemas.slide_post import SlideEdits, SlideTextElement

    edits = SlideEdits(
        look_preset="none",
        texts=[SlideTextElement(id="t1", text="Lisbon", color="#FF0000", role="label")],
    )
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="slide", asset_id=asset_id, kind="image", edits=edits)],
        version=2,
    )
    return SimpleNamespace(
        id=uuid.uuid4(), edit_format="slides", slide_post=draft.model_dump(mode="json")
    )


def _wire_put(monkeypatch: pytest.MonkeyPatch, item: SimpleNamespace) -> None:
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    monkeypatch.setattr(plan_items, "_owned_ready_slide_assets", AsyncMock(return_value=[]))
    monkeypatch.setattr(plan_items, "_validate_slide_ref_ownership", lambda *_a: None)
    monkeypatch.setattr(plan_items, "_maybe_rebuild_slide_post", AsyncMock())
    monkeypatch.setattr(plan_items, "flag_modified", lambda *_a: None)
    monkeypatch.setattr(plan_items, "plan_item_response", lambda it: it)


@pytest.mark.asyncio
async def test_legacy_put_without_texts_preserves_stored_rich_texts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = uuid.uuid4()
    item = _rich_item(asset_id)
    _wire_put(monkeypatch, item)
    # Old client: no `texts` key, mirror text round-tripped.
    body = plan_items.SlidePostDraftBody.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "slides": [
                {
                    "id": "slide",
                    "asset_id": str(asset_id),
                    "kind": "image",
                    "edits": {"text": {"content": "Porto", "position": "bottom"}},
                }
            ],
            "expected_version": 2,
        }
    )
    await plan_items.put_slide_post_draft(str(item.id), body, _user(), AsyncMock())
    saved = item.slide_post["slides"][0]["edits"]
    assert saved["texts"][0]["text"] == "Porto"
    assert saved["texts"][0]["color"] == "#FF0000"
    assert saved["texts"][0]["role"] == "label"
    assert saved["text"]["content"] == "Porto"
    assert item.slide_post["version"] == 3


@pytest.mark.asyncio
async def test_new_client_put_with_texts_is_saved_and_mirrored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = uuid.uuid4()
    item = _rich_item(asset_id)
    _wire_put(monkeypatch, item)
    body = plan_items.SlidePostDraftBody.model_validate(
        {
            "platform_profile": "tiktok_photo",
            "slides": [
                {
                    "id": "slide",
                    "asset_id": str(asset_id),
                    "kind": "image",
                    "edits": {
                        "texts": [
                            {"id": "a", "text": "One", "position": "top"},
                            {"id": "b", "text": "Two", "font_family": "Playfair Display"},
                        ]
                    },
                }
            ],
            "expected_version": 2,
        }
    )
    await plan_items.put_slide_post_draft(str(item.id), body, _user(), AsyncMock())
    saved = item.slide_post["slides"][0]["edits"]
    assert [t["id"] for t in saved["texts"]] == ["a", "b"]
    assert saved["text"] == {"content": "One", "position": "top"}


@pytest.mark.parametrize(
    "edits",
    [
        {"texts": [{"id": "a", "text": "x", "font_family": "Comic Sans"}]},
        {"texts": [{"id": str(i), "text": "x"} for i in range(5)]},
        {"texts": [{"id": "a", "text": "x", "color": "red"}]},
    ],
)
def test_put_body_rejects_invalid_rich_texts(edits: dict) -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        plan_items.SlidePostDraftBody.model_validate(
            {
                "platform_profile": "tiktok_photo",
                "slides": [
                    {"id": "s", "asset_id": str(uuid.uuid4()), "kind": "image", "edits": edits}
                ],
            }
        )


def test_capability_flag_is_exposed_and_defaults_off() -> None:
    from app.config import settings
    from app.routes.creation_threads import CreationCapabilitiesOut

    assert settings.slide_post_rich_text_enabled is False
    assert CreationCapabilitiesOut.model_fields["slide_post_rich_text"].default is False
