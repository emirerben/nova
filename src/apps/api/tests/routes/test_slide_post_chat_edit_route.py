"""POST /plan-items/{id}/slide-post/chat-edit (KRI-301): gating, ownership, and the
"the editor's draft is the newest truth" contract (never a version conflict)."""

from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.kria.brief_binding import BriefBinding
from app.main import app
from app.routes import plan_items
from app.routes.creation_threads import CreationCapabilitiesOut
from app.schemas.slide_post import SlideEdits, SlidePostDraft, SlideRef, SlideTextElement
from app.services.slide_post_chat_edit import SlidePostChatEditResponse


@pytest.fixture()
def client() -> TestClient:
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    user = MagicMock()
    user.id = uuid.uuid4()
    app.dependency_overrides[get_current_user] = lambda: user
    app.dependency_overrides[get_db] = lambda: AsyncMock()
    monkeypatch.setattr(settings, "slide_post_chat_edit_enabled", True)
    monkeypatch.setattr(settings, "slide_post_rich_text_enabled", True)
    monkeypatch.setattr(settings, "slide_posts_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    yield
    app.dependency_overrides.clear()


def _draft(asset_ids: list[uuid.UUID], *, version: int, caption: str = "") -> SlidePostDraft:
    return SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[SlideRef(id=f"s{i}", asset_id=a, kind="image") for i, a in enumerate(asset_ids)],
        version=version,
        caption=caption,
    )


def _install(
    monkeypatch: pytest.MonkeyPatch,
    stored: SlidePostDraft | None,
    owned_ids: list[uuid.UUID],
    run: AsyncMock | None = None,
) -> tuple[SimpleNamespace, AsyncMock]:
    item = SimpleNamespace(
        id=uuid.uuid4(),
        edit_format="slides",
        slide_post=stored.model_dump(mode="json") if stored else None,
    )
    monkeypatch.setattr(plan_items, "_load_owned_item", AsyncMock(return_value=item))
    assets = [SimpleNamespace(id=i, kind="image", analysis=None, capture=None) for i in owned_ids]

    async def owned(_item, ids, _db):  # noqa: ANN001
        return [a for a in assets if not ids or a.id in ids]

    monkeypatch.setattr(plan_items, "_owned_ready_slide_assets", owned)
    run = run or AsyncMock(
        return_value=SlidePostChatEditResponse(outcome="no_effect", reply="ok", base_version=0)
    )
    monkeypatch.setattr(plan_items, "run_slide_post_chat_edit", run)
    return item, run


def _post(client: TestClient, item: SimpleNamespace, **body):  # noqa: ANN003, ANN202
    body.setdefault("message", "put them in order")
    return client.post(f"/plan-items/{item.id}/slide-post/chat-edit", json=body)


def test_flag_off_is_404_and_capability_defaults_false(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4()]
    item, run = _install(monkeypatch, _draft(ids, version=2), ids)
    monkeypatch.setattr(settings, "slide_post_chat_edit_enabled", False)
    assert _post(client, item).status_code == 404
    assert run.await_count == 0
    assert CreationCapabilitiesOut.model_fields["slide_post_chat_edit"].default is False
    assert type(settings).model_fields["slide_post_chat_edit_enabled"].default is False


def test_stale_expected_version_with_client_draft_still_succeeds(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4(), uuid.uuid4()]
    stored = _draft(ids, version=9, caption="stored")
    item, run = _install(monkeypatch, stored, ids)
    run.return_value = SlidePostChatEditResponse(outcome="no_effect", reply="ok", base_version=9)
    client_draft = _draft(ids, version=1, caption="from the editor")
    resp = _post(
        client,
        item,
        expected_version=1,  # stale on purpose
        draft=client_draft.model_dump(mode="json"),
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["base_version"] == 9  # the SERVER's current version
    kwargs = run.await_args.kwargs
    # the edit is built from the client's draft, not the stored one
    assert kwargs["draft"].caption == "from the editor"
    assert kwargs["server_version"] == 9


def test_without_client_draft_the_stored_draft_is_used(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4()]
    stored = _draft(ids, version=4, caption="stored")
    item, run = _install(monkeypatch, stored, ids)
    assert _post(client, item, expected_version=0).status_code == 200
    assert run.await_args.kwargs["draft"].caption == "stored"
    assert run.await_args.kwargs["draft"].version == 4


def test_no_draft_anywhere_is_409(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    item, run = _install(monkeypatch, None, [])
    resp = _post(client, item)
    assert resp.status_code == 409
    assert resp.json()["detail"]["code"] == "slide_post_no_draft"
    assert run.await_count == 0


def test_foreign_asset_in_client_draft_is_rejected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mine = uuid.uuid4()
    foreign = uuid.uuid4()
    item, run = _install(monkeypatch, _draft([mine], version=2), [mine])
    resp = _post(client, item, draft=_draft([mine, foreign], version=2).model_dump(mode="json"))
    assert resp.status_code == 422
    assert run.await_count == 0


def test_request_schema_rejects_an_empty_message_and_long_history(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4()]
    item, _ = _install(monkeypatch, _draft(ids, version=2), ids)
    assert _post(client, item, message="").status_code == 422
    assert _post(client, item, message="x" * 12_001).status_code == 422
    assert _post(client, item, turns=[{"role": "user"}] * 13).status_code == 422


def test_turns_are_size_bounded_and_role_checked(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4()]
    item, run = _install(monkeypatch, _draft(ids, version=2), ids)
    big = [{"role": "user", "content": "x" * 2001}]
    assert _post(client, item, turns=big).status_code == 422
    assert _post(client, item, turns=[{"role": "system", "content": "hi"}]).status_code == 422
    ok = [{"role": "user", "content": "x" * 2000}, {"role": "assistant", "content": "ok"}]
    assert _post(client, item, turns=ok).status_code == 200
    assert run.await_args.kwargs["turns"][1]["role"] == "assistant"


def test_chat_edit_needs_rich_text_flag_too(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4()]
    item, run = _install(monkeypatch, _draft(ids, version=2), ids)
    monkeypatch.setattr(settings, "slide_post_rich_text_enabled", False)
    assert _post(client, item).status_code == 404
    assert run.await_count == 0


async def test_capability_requires_both_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.routes.creation_threads import capabilities  # noqa: PLC0415

    user = MagicMock()
    user.id = uuid.uuid4()
    for chat, rich, expected in [
        (True, True, True),
        (True, False, False),
        (False, True, False),
    ]:
        monkeypatch.setattr(settings, "slide_post_chat_edit_enabled", chat)
        monkeypatch.setattr(settings, "slide_post_rich_text_enabled", rich)
        caps = await capabilities(user, False)
        assert caps["slide_post_chat_edit"] is expected


def test_per_user_hourly_limit_is_stacked_on_the_ip_limit() -> None:
    import inspect  # noqa: PLC0415

    src = inspect.getsource(plan_items)
    start = src.index("async def chat_edit_slide_post")
    decorators = src[src.rindex("@router.post", 0, start) : start]
    assert '"20/minute", key_func=get_real_ip' in decorators
    assert '"30/hour", key_func=_edit_conversation_rate_key' in decorators


def test_route_never_writes_to_the_database(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4()]
    stored = _draft(ids, version=2)
    item, run = _install(monkeypatch, stored, ids)
    db = AsyncMock()
    app.dependency_overrides[get_db] = lambda: db
    edited = _draft(ids, version=3, caption="new")
    run.return_value = SlidePostChatEditResponse(
        outcome="edited", reply="Caption updated.", draft=edited, base_version=2, changes=["x"]
    )
    resp = _post(client, item)
    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "edited" and body["draft"]["version"] == 3
    assert set(body) == {"outcome", "reply", "draft", "base_version", "changes", "suggestions"}
    assert db.commit.await_count == 0 and db.add.call_count == 0
    assert item.slide_post == stored.model_dump(mode="json")


def test_rich_elements_roundtrip_through_the_client_draft(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    ids = [uuid.uuid4()]
    draft = _draft(ids, version=2)
    draft.slides[0].edits = SlideEdits(
        texts=[SlideTextElement(id="a", text="Hi", role="label", label_source="place")]
    )
    item, run = _install(monkeypatch, draft, ids)
    assert _post(client, item, draft=draft.model_dump(mode="json")).status_code == 200
    sent = run.await_args.kwargs["draft"]
    assert sent.slides[0].edits.texts[0].label_source == "place"


def test_writer_mints_binding_from_prior_request_and_unsaved_draft(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = uuid.uuid4()
    stored = _draft([asset_id], version=4, caption="saved")
    item, run = _install(monkeypatch, stored, [asset_id])
    thread = SimpleNamespace(id=uuid.uuid4())
    assets = [
        SimpleNamespace(id=asset_id, kind="image", gcs_generation=None, content_fingerprint=None)
    ]
    prior = BriefBinding.create(
        thread.id,
        None,
        latest_message="Keep the first caption.",
        media_snapshot=plan_items._slide_post_media_snapshot(item, stored, assets),
    )
    stored = stored.model_copy(update={"brief_binding": prior})
    item.slide_post = stored.model_dump(mode="json")
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(plan_items, "_slide_post_thread_for_item", AsyncMock(return_value=thread))
    unsaved = _draft([asset_id], version=1, caption="editor-only caption")

    response = _post(
        client,
        item,
        message="Change only the first caption.",
        draft=unsaved.model_dump(mode="json"),
    )

    assert response.status_code == 200, response.text
    binding = run.await_args.kwargs["brief_binding"]
    assert binding is not None
    assert binding.creator_request == (
        "Keep the first caption.\n\nLatest message: Change only the first caption."
    )
    assert binding.media_snapshot["item_id"] == str(item.id)
    assert binding.media_snapshot["draft"]["caption"] == "editor-only caption"


def test_old_client_draft_keeps_stored_binding_when_writers_are_disabled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = uuid.uuid4()
    stored = _draft([asset_id], version=2)
    item, run = _install(monkeypatch, stored, [asset_id])
    binding = BriefBinding.create(uuid.uuid4(), None, latest_message="original request")
    stored = stored.model_copy(update={"brief_binding": binding})
    item.slide_post = stored.model_dump(mode="json")

    response = _post(client, item, draft=_draft([asset_id], version=1).model_dump(mode="json"))

    assert response.status_code == 200, response.text
    assert run.await_args.kwargs["draft"].brief_binding == binding
    assert run.await_args.kwargs["brief_binding"] is None


def test_writer_rejects_context_over_12k_without_running_agent(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    asset_id = uuid.uuid4()
    stored = _draft([asset_id], version=2)
    item, run = _install(monkeypatch, stored, [asset_id])
    thread = SimpleNamespace(id=uuid.uuid4())
    assets = [
        SimpleNamespace(id=asset_id, kind="image", gcs_generation=None, content_fingerprint=None)
    ]
    prior = BriefBinding.create(
        thread.id,
        None,
        latest_message="x" * 11_999,
        media_snapshot=plan_items._slide_post_media_snapshot(item, stored, assets),
    )
    item.slide_post = stored.model_copy(update={"brief_binding": prior}).model_dump(mode="json")
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(plan_items, "_slide_post_thread_for_item", AsyncMock(return_value=thread))

    response = _post(client, item, message="small")

    assert response.status_code == 200, response.text
    assert response.json()["outcome"] == "unsupported"
    assert run.await_count == 0


def test_writer_keeps_prior_request_when_assets_changed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_asset, new_asset = uuid.uuid4(), uuid.uuid4()
    stored = _draft([new_asset], version=2)
    item, run = _install(monkeypatch, stored, [new_asset])
    thread = SimpleNamespace(id=uuid.uuid4())
    old_assets = [
        SimpleNamespace(id=old_asset, kind="image", gcs_generation=None, content_fingerprint=None)
    ]
    old_draft = _draft([old_asset], version=1)
    prior = BriefBinding.create(
        thread.id,
        None,
        latest_message="Keep my original title.",
        media_snapshot=plan_items._slide_post_media_snapshot(item, old_draft, old_assets),
    )
    item.slide_post = stored.model_copy(update={"brief_binding": prior}).model_dump(mode="json")
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(plan_items, "_slide_post_thread_for_item", AsyncMock(return_value=thread))

    response = _post(client, item, message="Add a label to the new photo.")

    assert response.status_code == 200, response.text
    assert run.await_args.kwargs["brief_binding"].creator_request == (
        "Keep my original title.\n\nLatest message: Add a label to the new photo."
    )
