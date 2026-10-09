"""Native slide-post route guards that must stay fail-closed."""

from __future__ import annotations

import copy
import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.config import settings
from app.kria.brief_binding import BriefBinding
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


def _roundtripped_binding_payload() -> tuple[SlidePostDraft, dict]:
    """A server-minted snapshot after native JSON turns a float into an int."""
    from app.schemas.slide_post import SlideEdits, SlideTextElement

    asset_id = uuid.uuid4()
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(
                id="slide",
                asset_id=asset_id,
                kind="image",
                edits=SlideEdits(
                    texts=[SlideTextElement(id="text", text="Title", shadow_opacity=1.0)]
                ),
            )
        ],
    )
    binding = BriefBinding.create(
        uuid.uuid4(),
        None,
        latest_message="Keep the title.",
        media_snapshot={"draft": draft.model_dump(mode="json", exclude={"brief_binding"})},
    )
    payload = binding.model_dump(mode="json")
    payload["media_snapshot"]["draft"]["slides"][0]["edits"]["texts"][0]["shadow_opacity"] = 1
    return draft, payload


def test_slide_post_binding_accepts_native_numeric_roundtrip_in_both_request_models() -> None:
    draft, binding_payload = _roundtripped_binding_payload()
    draft_payload = draft.model_dump(mode="json", exclude={"brief_binding"})

    parsed_draft = SlidePostDraft.model_validate(draft_payload | {"brief_binding": binding_payload})
    parsed_body = plan_items.SlidePostDraftBody.model_validate(
        {
            "platform_profile": draft.platform_profile,
            "slides": draft_payload["slides"],
            "brief_binding": binding_payload,
        }
    )

    assert parsed_draft.brief_binding is not None
    assert parsed_body.brief_binding is not None
    assert parsed_draft.brief_binding.digest == binding_payload["digest"]
    assert parsed_body.brief_binding.digest == binding_payload["digest"]


@pytest.mark.parametrize(
    "mutation",
    [
        lambda payload: payload.__setitem__("creator_request", "Changed request."),
        lambda payload: payload["media_snapshot"]["draft"]["slides"][0].__setitem__(
            "asset_id", str(uuid.uuid4())
        ),
    ],
)
def test_slide_post_binding_canonicalization_still_rejects_tampering(mutation) -> None:
    draft, binding_payload = _roundtripped_binding_payload()
    tampered = copy.deepcopy(binding_payload)
    mutation(tampered)

    with pytest.raises(ValidationError, match="Brief binding digest mismatch"):
        SlidePostDraft.model_validate(
            draft.model_dump(mode="json", exclude={"brief_binding"}) | {"brief_binding": tampered}
        )


def test_non_slide_bindings_remain_strict_for_numeric_changes() -> None:
    binding = BriefBinding.create(
        uuid.uuid4(), None, latest_message="Keep it", media_snapshot={"opacity": 1.0}
    )
    payload = binding.model_dump(mode="json")
    payload["media_snapshot"]["opacity"] = 1

    with pytest.raises(ValidationError, match="Brief binding digest mismatch"):
        BriefBinding.model_validate(payload)


def test_slide_post_binding_ownership_guard_rejects_redigested_media_id() -> None:
    asset_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4())
    asset = SimpleNamespace(
        id=asset_id, kind="image", gcs_generation="1", content_fingerprint="fingerprint"
    )
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="slide", asset_id=asset_id, kind="image")],
    )
    thread = SimpleNamespace(id=uuid.uuid4())
    snapshot = plan_items._slide_post_media_snapshot(item, draft, [asset])
    snapshot["assets"][0]["asset_id"] = str(uuid.uuid4())
    binding = BriefBinding.create(
        thread.id, None, latest_message="Keep it", media_snapshot=snapshot
    )

    assert not plan_items._binding_matches_slide_post(
        binding, thread=thread, item=item, draft=draft, owned_assets=[asset]
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


@pytest.mark.asyncio
async def test_put_accepts_server_staged_binding_and_roundtrips_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    asset_id = uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), edit_format="slides", slide_post=None)
    asset = SimpleNamespace(
        id=asset_id, kind="image", gcs_generation="7", content_fingerprint="fingerprint"
    )
    draft = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id="slide", asset_id=asset_id, kind="image")],
    )
    thread = SimpleNamespace(id=uuid.uuid4())
    binding = BriefBinding.create(
        thread.id,
        None,
        latest_message="Use this exact caption.",
        media_snapshot=plan_items._slide_post_media_snapshot(item, draft, [asset]),
    )
    _wire_put(monkeypatch, item)
    monkeypatch.setattr(plan_items, "_owned_ready_slide_assets", AsyncMock(return_value=[asset]))
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(plan_items, "_slide_post_thread_for_item", AsyncMock(return_value=thread))
    body = plan_items.SlidePostDraftBody(
        platform_profile="tiktok_photo",
        slides=draft.slides,
        brief_binding=binding,
    )

    await plan_items.put_slide_post_draft(str(item.id), body, _user(), AsyncMock())

    saved = SlidePostDraft.model_validate(item.slide_post)
    assert saved.brief_binding == binding


@pytest.mark.asyncio
async def test_put_old_client_preserves_stored_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    item = _item()
    stored = SlidePostDraft.model_validate(item.slide_post)
    binding = BriefBinding.create(uuid.uuid4(), None, latest_message="original request")
    item.slide_post = stored.model_copy(update={"brief_binding": binding}).model_dump(mode="json")
    _wire_put(monkeypatch, item)
    body = plan_items.SlidePostDraftBody(
        platform_profile="tiktok_photo",
        slides=stored.slides,
        expected_version=stored.version,
    )

    await plan_items.put_slide_post_draft(str(item.id), body, _user(), AsyncMock())

    assert SlidePostDraft.model_validate(item.slide_post).brief_binding == binding


@pytest.mark.asyncio
async def test_put_staged_binding_survives_slide_reorder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first, second = uuid.uuid4(), uuid.uuid4()
    item = SimpleNamespace(id=uuid.uuid4(), edit_format="slides", slide_post=None)
    assets = [
        SimpleNamespace(id=first, kind="image", gcs_generation="1", content_fingerprint="first"),
        SimpleNamespace(id=second, kind="image", gcs_generation="2", content_fingerprint="second"),
    ]
    before = SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(id="one", asset_id=first, kind="image"),
            SlideRef(id="two", asset_id=second, kind="image"),
        ],
    )
    thread = SimpleNamespace(id=uuid.uuid4())
    binding = BriefBinding.create(
        thread.id,
        None,
        latest_message="Put the second photo first.",
        media_snapshot=plan_items._slide_post_media_snapshot(item, before, assets),
    )
    _wire_put(monkeypatch, item)
    monkeypatch.setattr(plan_items, "_owned_ready_slide_assets", AsyncMock(return_value=assets))
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(plan_items, "_slide_post_thread_for_item", AsyncMock(return_value=thread))
    body = plan_items.SlidePostDraftBody(
        platform_profile="tiktok_photo",
        slides=list(reversed(before.slides)),
        brief_binding=binding,
    )

    await plan_items.put_slide_post_draft(str(item.id), body, _user(), AsyncMock())

    saved = SlidePostDraft.model_validate(item.slide_post)
    assert [slide.id for slide in saved.slides] == ["two", "one"]
    assert saved.brief_binding == binding
