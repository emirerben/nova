"""KRI-812: Save after deleting a slide from an AI-bound slide draft (real Postgres E2E).

Troya F03: the creator removed the titled horse slide from a five-slide draft an AI edit
had produced. iOS resends the stored brief binding with every Save, the binding's media
snapshot still listed five assets, and PUT /slide-post answered 422
``slide_post_binding_invalid``. Save failed twice; the server kept five slides.

Every test drives the real routes (chat-edit -> PUT -> GET -> chat-edit -> generate)
through ``TestClient(app)`` against the ``nova_test`` database. Only the edit-copilot
model call (replaced by the REAL parser over a fixed op list) and the broker publish are
stubbed: no paid model, no network.

Failure modes, written before the change:
F1  Delete the horse from an AI-bound 5-slide draft, Save with the stored binding -> 200,
    4 slides; GET returns 4; the horse never returns on reopen, next AI edit or export.
F2  The next chat-edit accepts the re-derived binding and keeps the full prior
    creator_request, brief and choice_answers.
F3  Cover, per-slide titles stay attached to the same asset ids when a slide before the
    cover is removed; an out-of-range cover_index is rejected 422.
F4  Deleting the cover slide itself saves.
F5  Manually adding a new owned ready asset saves; the snapshot names its generation.
F6  Reorder-only Save stores the binding verbatim (digest unchanged).
F7  An AI proposal that removes a slide (binding minted on the pre-edit draft) saves.
F8  A binding from a foreign thread -> 422, stored draft unchanged.
F9  A binding for another item on the same thread -> 422, stored draft unchanged.
F10 A tampered digest -> 422 from request validation, stored draft unchanged.
F11 A stale expected_version -> 409, stored draft unchanged.
F12 A slide referencing an unowned / not-ready / wrong-kind asset -> 422.
F13 An old client omitting brief_binding keeps the stored binding.
F14 With the brief-binding writer flag off the body binding is ignored.
F15 The re-derived snapshot comes only from server asset rows, never the client's snapshot.
F16 Generate dispatches the saved 4-slide revision.
F17 choice_answers survive every chat-edit turn.
F18 A legacy binding without media_snapshot.item_id -> 422.
F19 A stale device resends an OLDER binding after a newer one was saved (409 rebase): the
    delete saves and the stored, newer request/brief/answers survive (none dropped).
F20 A divergent binding (request neither extends nor is extended by the stored one), a
    different pinned brief, or fabricated choice_answers -> 422, stored draft unchanged.
F21 Same asset ids but a re-uploaded source (new generation) -> 422: approved source
    bytes changed, which is not a slide add/delete.
F22 A post-render delete Save rebuilds the rendered slides variant from the saved 2-slide
    draft; under the iPhone-only fence the draft saves and the old render is kept.
F23 Same-asset reorder/next chat from an older device keeps newer stored instructions.
F24 Same-asset PUT and chat reject divergent requests, pinned briefs and frozen answers.
F25 Same-asset valid extensions keep earlier instructions, brief and frozen answers.

Repeatable artifact: set ``KRI812_EVIDENCE_DIR`` to write ``report.json`` (requests,
binding before/after, asset ids + generations, API revision, effective flags, the slides
the generate dispatch saw).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app import storage
from app.agents._runtime import ModelClient
from app.agents.edit_copilot import EditCopilotAgent, EditCopilotInput
from app.auth import get_current_user, is_native_client
from app.config import settings
from app.database import get_db, sync_session
from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_binding import BriefBinding
from app.main import app
from app.models import Job, PlanItem, PlanItemAsset
from app.routes import plan_items
from app.schemas.slide_post import SlideEdits, SlidePostDraft, SlideRef, SlideTextElement
from app.services import slide_post_chat_edit as chat_svc
from tests.kria.test_runtime_postgres_integration import _seed_runtime_project

_db_name = make_url(settings.database_url).database or ""
if not _db_name.endswith("_test"):
    pytest.skip(f"refusing to write to non-test database {_db_name!r}", allow_module_level=True)
try:
    with sync_session() as _probe:
        _probe.execute(text("select 1"))
except OperationalError:
    pytest.skip("nova_test Postgres not reachable", allow_module_level=True)

TITLES = ["Sunrise", "Market", "Horse", "Harbour", "Dinner"]
HORSE = 2
COVER = 3  # "Harbour": after the horse, so deleting the horse shifts it to index 2
CHOICE_ANSWERS = [
    {"conflict": "order_basis", "kind": "order_basis", "option": "unordered", "input_digest": "d"}
]
EVIDENCE: dict[str, Any] = {"steps": []}


# ------------------------------------------------------------------ fixtures


@pytest.fixture(autouse=True)
def _flags(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "slide_posts_enabled", True)
    monkeypatch.setattr(settings, "slide_post_chat_edit_enabled", True)
    monkeypatch.setattr(settings, "slide_post_rich_text_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_enabled", True)
    monkeypatch.setattr(settings, "kria_brief_binding_user_ids", [])
    monkeypatch.setattr(settings, "thought_summaries_enabled", False)
    monkeypatch.setattr(plan_items.limiter, "enabled", False)
    # GET /slide-post signs preview URLs; no GCS credentials in a test run.
    for name in ("signed_get_url", "signed_get_url_for_generation"):
        monkeypatch.setattr(storage, name, lambda *_a, **_kw: "https://signed.test/x")


class _ParsedOps:
    """The edit copilot with its model call replaced by fixed ops through the REAL parser."""

    ops: list[dict] = []

    def __init__(self, _client: object) -> None:
        pass

    def run(self, agent_input: EditCopilotInput, ctx: object = None) -> Any:
        raw = json.dumps(
            {"intent": "edit", "ops": self.ops, "confidence": 0.9, "reply": "ok", "suggestions": []}
        )
        return EditCopilotAgent(ModelClient()).parse(raw, agent_input)


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> type[_ParsedOps]:
    monkeypatch.setattr("app.agents._model_client.default_client", lambda: object())
    monkeypatch.setattr(chat_svc, "EditCopilotAgent", _ParsedOps)
    return _ParsedOps


@pytest.fixture
def seeded() -> SimpleNamespace:
    """An AI-bound 5-slide draft on a thread-owned slide item, plus spare assets."""
    user_id, thread_id, _session_id = _seed_runtime_project()
    with sync_session() as db:
        thread_item_id = db.execute(
            text("select active_plan_item_id from creation_threads where id = :id"),
            {"id": thread_id},
        ).scalar_one()
        item = db.get(PlanItem, thread_item_id)
        item.edit_format = "slides"

        def asset(name: str, *, ready: bool = True, plan_item_id: uuid.UUID | None = None):
            row = PlanItemAsset(
                plan_item_id=plan_item_id or item.id,
                user_id=user_id,
                gcs_path=f"users/{user_id}/pool/{name}.jpg",
                kind="image",
                status="ready" if ready else "analyzing",
                media_status="ready" if ready else "pending",
                gcs_generation=str(abs(hash(name)) % 10**12),
                content_fingerprint=f"fp-{name}-{uuid.uuid4().hex[:6]}",
                source_filename=f"{name}.jpg",
            )
            db.add(row)
            return row

        slides = [asset(t.lower()) for t in TITLES]
        extra = asset("lighthouse")
        not_ready = asset("still-uploading", ready=False)
        # Another item of the same creator: its asset is not this item's.
        other_item = PlanItem(
            content_plan_id=item.content_plan_id,
            position=2,
            idea="another post",
            item_status="awaiting_clips",
            edit_format="slides",
        )
        db.add(other_item)
        db.flush()
        foreign = asset("foreign", plan_item_id=other_item.id)
        db.flush()

        draft = SlidePostDraft(
            platform_profile="tiktok_photo",
            slides=[
                SlideRef(
                    id=f"s{i}",
                    asset_id=row.id,
                    kind="image",
                    edits=SlideEdits(texts=[SlideTextElement(id=f"t{i}", text=TITLES[i])]),
                )
                for i, row in enumerate(slides)
            ],
            cover_index=COVER,
            caption="Five photos",
            version=1,
        )
        brief = CreativeBrief(
            version=1,
            requirements=[
                BriefRequirement(
                    id="r1", kind="order", scope="global", description="harbour as the cover"
                )
            ],
        )
        binding = BriefBinding.create(
            thread_id,
            brief,
            latest_message="Make a five-photo post of my trip with the harbour as the cover",
            media_snapshot=plan_items._slide_post_media_snapshot(item, draft, slides),
            choice_answers=CHOICE_ANSWERS,
        )
        item.slide_post = draft.model_copy(update={"brief_binding": binding}).model_dump(
            mode="json"
        )
        db.commit()
        return SimpleNamespace(
            user_id=user_id,
            thread_id=thread_id,
            item_id=item.id,
            other_item_id=other_item.id,
            asset_ids=[row.id for row in slides],
            generations={str(r.id): r.gcs_generation for r in [*slides, extra]},
            extra_id=extra.id,
            not_ready_id=not_ready.id,
            foreign_id=foreign.id,
            binding=binding,
        )


@pytest.fixture
def client(seeded: SimpleNamespace):
    engine = create_async_engine(settings.asyncpg_database_url, poolclass=NullPool)
    sessions = async_sessionmaker(engine, expire_on_commit=False)

    async def real_db():  # noqa: ANN202
        async with sessions() as db:
            yield db

    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=seeded.user_id)
    app.dependency_overrides[get_db] = real_db
    try:
        with TestClient(app) as test_client:
            yield test_client
    finally:
        app.dependency_overrides.clear()


# ------------------------------------------------------------------ helpers


def _stored(seeded: SimpleNamespace) -> SlidePostDraft:
    with sync_session() as db:
        raw = db.get(PlanItem, seeded.item_id).slide_post
    return SlidePostDraft.model_validate(raw)


def _snapshot_ids(binding: BriefBinding | dict) -> list[str]:
    snap = binding.media_snapshot if isinstance(binding, BriefBinding) else binding
    return [row["asset_id"] for row in snap["assets"]]


def _save_body(draft: SlidePostDraft, *, expected_version: int, **overrides: Any) -> dict:
    """What iOS ``SlidePostSaveRequest.init(draft:)`` sends: the binding always rides along."""
    dumped = draft.model_dump(mode="json")
    body = {
        "platform_profile": dumped["platform_profile"],
        "slides": dumped["slides"],
        "cover_index": dumped["cover_index"],
        "caption": dumped["caption"],
        "brief_binding": dumped.get("brief_binding"),
        "expected_version": expected_version,
    }
    body.update(overrides)
    return body


def _without(draft: SlidePostDraft, index: int, *, cover_index: int) -> SlidePostDraft:
    slides = [s for i, s in enumerate(draft.slides) if i != index]
    return draft.model_copy(update={"slides": slides, "cover_index": cover_index})


def _put(client: TestClient, seeded: SimpleNamespace, body: dict, step: str):  # noqa: ANN202
    response = client.put(f"/plan-items/{seeded.item_id}/slide-post", json=body)
    EVIDENCE["steps"].append(
        {
            "step": step,
            "request": {
                "method": "PUT",
                "slides": [s["asset_id"] for s in body["slides"]],
                "cover_index": body.get("cover_index"),
                "expected_version": body.get("expected_version"),
                "binding_snapshot": _snapshot_ids(body["brief_binding"]["media_snapshot"])
                if body.get("brief_binding")
                else None,
                "binding_digest": (body.get("brief_binding") or {}).get("digest"),
            },
            "status": response.status_code,
            "detail": response.json().get("detail") if response.status_code >= 400 else None,
        }
    )
    return response


def _chat(client: TestClient, seeded: SimpleNamespace, draft: SlidePostDraft, message: str):  # noqa: ANN202
    response = client.post(
        f"/plan-items/{seeded.item_id}/slide-post/chat-edit",
        json={
            "message": message,
            "expected_version": draft.version,
            "draft": draft.model_dump(mode="json"),
        },
    )
    EVIDENCE["steps"].append(
        {
            "step": f"chat-edit: {message}",
            "status": response.status_code,
            "outcome": response.json().get("outcome") if response.status_code == 200 else None,
        }
    )
    return response


def _assert_unchanged(seeded: SimpleNamespace, before: SlidePostDraft) -> None:
    after = _stored(seeded)
    assert after == before


# ------------------------------------------------------------------ the journey


def test_delete_horse_then_reopen_ai_edit_and_export(
    client: TestClient,
    seeded: SimpleNamespace,
    agent: type[_ParsedOps],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [str(a) for a in seeded.asset_ids]
    horse, harbour = ids[HORSE], ids[COVER]

    # 1. AI edit on the bound five-slide draft, applied with Save (asset set unchanged).
    agent.ops = [{"op": "set_post_caption", "caption": "Summer by the sea"}]
    stored = _stored(seeded)
    chat = _chat(client, seeded, stored, "Make the caption summery")
    assert chat.status_code == 200, chat.text
    proposal = SlidePostDraft.model_validate(chat.json()["draft"])
    assert proposal.brief_binding is not None
    # F17: the frozen conflict answers survive the chat turn.
    assert proposal.brief_binding.choice_answers == CHOICE_ANSWERS
    saved = _put(client, seeded, _save_body(proposal, expected_version=1), "apply AI caption")
    assert saved.status_code == 200, saved.text
    stored = _stored(seeded)
    assert stored.version == 2 and len(stored.slides) == 5
    bound = stored.brief_binding
    assert bound is not None and _snapshot_ids(bound) == ids

    # 2. F1/F3: the creator deletes the horse by hand; the cover (harbour) shifts 3 -> 2.
    deleted = _without(stored, HORSE, cover_index=COVER - 1)
    response = _put(client, seeded, _save_body(deleted, expected_version=2), "delete horse")
    assert response.status_code == 200, response.text
    stored = _stored(seeded)
    assert [str(s.asset_id) for s in stored.slides] == [a for a in ids if a != horse]
    assert str(stored.slides[stored.cover_index].asset_id) == harbour
    titles = {str(s.asset_id): s.edits.texts[0].text for s in stored.slides}
    assert titles == {ids[i]: TITLES[i] for i in range(5) if i != HORSE}
    rebound = stored.brief_binding
    assert rebound is not None
    assert horse not in _snapshot_ids(rebound)
    assert _snapshot_ids(rebound) == [str(s.asset_id) for s in stored.slides]
    # F2: nothing the creator asked for was dropped.
    assert rebound.creator_request == bound.creator_request
    assert rebound.brief == bound.brief
    assert rebound.choice_answers == CHOICE_ANSWERS
    assert rebound.thread_id == str(seeded.thread_id)
    # F15: generations come from the server rows.
    assert {r["asset_id"]: r["gcs_generation"] for r in rebound.media_snapshot["assets"]} == {
        a: seeded.generations[a] for a in _snapshot_ids(rebound)
    }
    EVIDENCE["binding_before_delete"] = bound.model_dump(mode="json")
    EVIDENCE["binding_after_delete"] = rebound.model_dump(mode="json")

    # 3. Reopen: GET returns the four slides, the horse is gone.
    reopened = client.get(f"/plan-items/{seeded.item_id}/slide-post")
    assert reopened.status_code == 200, reopened.text
    reopened_draft = SlidePostDraft.model_validate(reopened.json()["draft"])
    assert [str(s.asset_id) for s in reopened_draft.slides] == [a for a in ids if a != horse]
    assert reopened_draft.brief_binding == rebound

    # 4. F2/F7: another AI edit on the reopened draft removes the first slide; Save applies it
    # with the binding minted on the pre-edit (four-slide) draft.
    agent.ops = [{"op": "remove_clip", "slot_index": 0}]
    chat = _chat(client, seeded, reopened_draft, "Drop the sunrise photo")
    assert chat.status_code == 200, chat.text
    assert chat.json()["outcome"] == "edited", chat.json()
    proposal = SlidePostDraft.model_validate(chat.json()["draft"])
    assert proposal.brief_binding is not None
    assert proposal.brief_binding.creator_request.startswith(rebound.creator_request)
    assert "Drop the sunrise photo" in proposal.brief_binding.creator_request
    assert proposal.brief_binding.brief == bound.brief
    assert proposal.brief_binding.choice_answers == CHOICE_ANSWERS
    assert len(proposal.slides) == 3
    response = _put(
        client,
        seeded,
        _save_body(proposal, expected_version=stored.version),
        "apply AI remove_clip",
    )
    assert response.status_code == 200, response.text
    stored = _stored(seeded)
    survivors = [ids[1], ids[3], ids[4]]
    assert [str(s.asset_id) for s in stored.slides] == survivors
    assert str(stored.slides[stored.cover_index].asset_id) == harbour
    assert _snapshot_ids(stored.brief_binding) == survivors
    assert "Drop the sunrise photo" in stored.brief_binding.creator_request

    # 5. F5: the creator adds a new photo by hand.
    added = stored.model_copy(
        update={
            "slides": [
                *stored.slides,
                SlideRef(id="s-new", asset_id=seeded.extra_id, kind="image"),
            ]
        }
    )
    response = _put(client, seeded, _save_body(added, expected_version=stored.version), "add photo")
    assert response.status_code == 200, response.text
    stored = _stored(seeded)
    assert _snapshot_ids(stored.brief_binding) == [*survivors, str(seeded.extra_id)]
    assert (
        stored.brief_binding.media_snapshot["assets"][-1]["gcs_generation"]
        == seeded.generations[str(seeded.extra_id)]
    )

    # 6. F6: a reorder-only Save keeps the binding byte-identical.
    before = stored.brief_binding
    reordered = stored.model_copy(
        update={"slides": list(reversed(stored.slides)), "cover_index": 0}
    )
    response = _put(
        client, seeded, _save_body(reordered, expected_version=stored.version), "reorder"
    )
    assert response.status_code == 200, response.text
    stored = _stored(seeded)
    assert stored.brief_binding == before

    # 7. F4: delete the cover slide itself.
    cover_asset = str(stored.slides[stored.cover_index].asset_id)
    no_cover = _without(stored, stored.cover_index, cover_index=0)
    response = _put(
        client, seeded, _save_body(no_cover, expected_version=stored.version), "delete cover"
    )
    assert response.status_code == 200, response.text
    stored = _stored(seeded)
    assert cover_asset not in [str(s.asset_id) for s in stored.slides]
    assert cover_asset not in _snapshot_ids(stored.brief_binding)
    assert len(stored.slides) == 3

    # 8. F16: Generate dispatches exactly the saved revision.
    published: list[tuple] = []
    monkeypatch.setattr(
        "app.services.job_dispatch.enqueue_orchestrator_sync",
        lambda task, job_id, **kw: published.append((job_id, kw)),
    )
    response = client.post(
        f"/plan-items/{seeded.item_id}/slide-post/generate",
        json={"expected_version": stored.version},
    )
    assert response.status_code == 200, response.text
    assert len(published) == 1
    with sync_session() as db:
        item = db.get(PlanItem, seeded.item_id)
        job = db.get(Job, item.current_job_id)
        assert job is not None and str(job.id) == str(published[0][0])
        exported = [s["asset_id"] for s in item.slide_post["slides"]]
    assert exported == [str(s.asset_id) for s in stored.slides]
    assert horse not in exported
    EVIDENCE["export"] = {"job_id": str(job.id), "slides": exported}

    # 9. F22: post-render. The job now has a rendered slides variant; a manual delete
    # Save with the stored binding must rebuild it from the saved draft.
    with sync_session() as db:
        job = db.get(Job, item.current_job_id)
        job.assembly_plan = {
            **(job.assembly_plan or {}),
            "variants": [
                {"variant_id": "slides", "resolved_archetype": "slides", "status": "ready"}
            ],
        }
        db.commit()
    rebuilds: list[dict] = []

    def _record_rebuild(job_id: str, **kw: Any) -> None:
        rebuilds.append({"job_id": job_id, "draft": _stored(seeded)})

    from app.tasks import generative_build  # noqa: PLC0415

    monkeypatch.setattr(generative_build.rebuild_slide_post_variant, "delay", _record_rebuild)
    stored = _stored(seeded)
    post_render = _without(stored, 0, cover_index=0)
    response = _put(
        client,
        seeded,
        _save_body(post_render, expected_version=stored.version),
        "post-render delete",
    )
    assert response.status_code == 200, response.text
    assert len(rebuilds) == 1, rebuilds
    rebuilt = rebuilds[0]["draft"]
    assert len(rebuilt.slides) == 2
    assert _snapshot_ids(rebuilt.brief_binding) == [str(s.asset_id) for s in rebuilt.slides]
    assert rebuilt.brief_binding.creator_request == stored.brief_binding.creator_request
    EVIDENCE["post_render_rebuild"] = {
        "job_id": rebuilds[0]["job_id"],
        "slides": [str(s.asset_id) for s in rebuilt.slides],
    }

    # ...and under the iPhone-only fence the draft saves, the cloud render is not replaced.
    monkeypatch.setattr(settings, "ios_native_device_only_enabled", True)
    app.dependency_overrides[is_native_client] = lambda: True
    stored = _stored(seeded)
    device_only = _without(stored, 0, cover_index=0)
    response = _put(
        client,
        seeded,
        _save_body(device_only, expected_version=stored.version),
        "device-only delete",
    )
    assert response.status_code == 200, response.text
    assert len(rebuilds) == 1
    stored = _stored(seeded)
    assert len(stored.slides) == 1
    assert _snapshot_ids(stored.brief_binding) == [str(stored.slides[0].asset_id)]
    EVIDENCE["final_binding"] = stored.brief_binding.model_dump(mode="json")
    _write_evidence(seeded)


# ------------------------------------------------------------------ guards that stay strict


def _rebinding(binding: BriefBinding, **changes: Any) -> dict:
    """A self-consistent (re-digested) binding with some fields changed."""
    fields = {
        "thread_id": binding.thread_id,
        "state": binding.state,
        "brief": binding.brief,
        "creator_request": binding.creator_request,
        "media_snapshot": copy.deepcopy(binding.media_snapshot),
        "choice_answers": list(binding.choice_answers),
    }
    fields.update(changes)
    digest = BriefBinding._digest(
        fields["thread_id"],
        fields["state"],
        fields["brief"],
        fields["creator_request"],
        fields["media_snapshot"],
        fields["choice_answers"],
    )
    return BriefBinding(**fields, digest=digest).model_dump(mode="json")


@pytest.mark.parametrize("route", ["put", "chat"])
def test_same_assets_keep_newer_stored_instructions(
    client: TestClient, seeded: SimpleNamespace, agent: type[_ParsedOps], route: str
) -> None:
    old = _stored(seeded)
    agent.ops = [{"op": "set_post_caption", "caption": "Keep every original title"}]
    chat = _chat(client, seeded, old, "Keep every original title")
    assert chat.status_code == 200, chat.text
    proposal = SlidePostDraft.model_validate(chat.json()["draft"])
    saved = _put(client, seeded, _save_body(proposal, expected_version=1), "F23 newer save")
    assert saved.status_code == 200, saved.text
    newer = _stored(seeded).brief_binding
    assert "Keep every original title" in newer.creator_request
    reordered = old.model_copy(update={"slides": list(reversed(old.slides)), "cover_index": 0})
    if route == "put":
        stale = _put(client, seeded, _save_body(reordered, expected_version=1), "F23 stale")
        assert stale.status_code == 409
        response = _put(client, seeded, _save_body(reordered, expected_version=2), "F23 rebase")
        assert response.status_code == 200, response.text
        result = _stored(seeded).brief_binding
        assert result.digest == newer.digest
    else:
        response = _chat(client, seeded, reordered, "Use my caption")
        assert response.status_code == 200, response.text
        result = SlidePostDraft.model_validate(response.json()["draft"]).brief_binding
        assert result.creator_request.endswith("Latest message: Use my caption")
    assert result.creator_request.startswith(newer.creator_request)
    assert result.brief == newer.brief and result.choice_answers == newer.choice_answers


@pytest.mark.parametrize("route", ["put", "chat"])
@pytest.mark.parametrize("change", ["request", "brief", "answers"])
def test_same_assets_reject_unanchored_authority(
    client: TestClient, seeded: SimpleNamespace, agent: type[_ParsedOps], route: str, change: str
) -> None:
    before = _stored(seeded)
    changes = {
        "request": {"creator_request": "Only horses"},
        "brief": {
            "brief": CreativeBrief(
                version=1,
                requirements=[
                    BriefRequirement(
                        id="r1", kind="order", scope="global", description="horse first"
                    )
                ],
            )
        },
        "answers": {"choice_answers": [{**CHOICE_ANSWERS[0], "option": "chronological"}]},
    }
    binding = BriefBinding.model_validate(_rebinding(before.brief_binding, **changes[change]))
    draft = before.model_copy(update={"brief_binding": binding})
    agent.ops = [{"op": "set_post_caption", "caption": "A normal edit"}]
    response = (
        _put(client, seeded, _save_body(draft, expected_version=1), "F24")
        if route == "put"
        else _chat(client, seeded, draft, "A normal edit")
    )
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "slide_post_binding_invalid"
    _assert_unchanged(seeded, before)


@pytest.mark.parametrize("route", ["put", "chat"])
def test_same_assets_accept_anchored_extension(
    client: TestClient, seeded: SimpleNamespace, agent: type[_ParsedOps], route: str
) -> None:
    before = _stored(seeded)
    request = before.brief_binding.creator_request + "\n\nLatest message: Keep all titles"
    binding = BriefBinding.model_validate(_rebinding(before.brief_binding, creator_request=request))
    draft = before.model_copy(update={"brief_binding": binding})
    agent.ops = [{"op": "set_post_caption", "caption": "A normal edit"}]
    if route == "put":
        response = _put(client, seeded, _save_body(draft, expected_version=1), "F25")
        assert response.status_code == 200, response.text
        result = _stored(seeded).brief_binding
        assert result.digest == binding.digest
    else:
        response = _chat(client, seeded, draft, "A normal edit")
        assert response.status_code == 200, response.text
        result = SlidePostDraft.model_validate(response.json()["draft"]).brief_binding
    assert result.creator_request.startswith(request)
    assert result.brief == before.brief_binding.brief
    assert result.choice_answers == before.brief_binding.choice_answers


def test_foreign_or_other_item_or_legacy_binding_is_rejected(
    client: TestClient, seeded: SimpleNamespace
) -> None:
    before = _stored(seeded)
    deleted = _without(before, HORSE, cover_index=COVER - 1)
    snapshot = seeded.binding.media_snapshot
    cases = {
        "F8 foreign thread": _rebinding(seeded.binding, thread_id=str(uuid.uuid4())),
        "F9 other item": _rebinding(
            seeded.binding, media_snapshot={**snapshot, "item_id": str(seeded.other_item_id)}
        ),
        "F18 legacy no item_id": _rebinding(
            seeded.binding, media_snapshot={k: v for k, v in snapshot.items() if k != "item_id"}
        ),
    }
    for step, binding in cases.items():
        body = _save_body(deleted, expected_version=1, brief_binding=binding)
        response = _put(client, seeded, body, step)
        assert response.status_code == 422, (step, response.text)
        assert response.json()["detail"]["code"] == "slide_post_binding_invalid", step
        _assert_unchanged(seeded, before)


def test_tampered_binding_is_rejected(client: TestClient, seeded: SimpleNamespace) -> None:
    before = _stored(seeded)
    body = _save_body(_without(before, HORSE, cover_index=COVER - 1), expected_version=1)
    body["brief_binding"]["creator_request"] += " and make it about horses"
    response = _put(client, seeded, body, "F10 tampered digest")
    assert response.status_code == 422, response.text
    _assert_unchanged(seeded, before)


def test_client_snapshot_never_becomes_the_saved_snapshot(
    client: TestClient, seeded: SimpleNamespace
) -> None:
    before = _stored(seeded)
    snapshot = copy.deepcopy(seeded.binding.media_snapshot)
    for row in snapshot["assets"]:
        row["gcs_generation"] = "client-forged"
    snapshot["assets"].append({"asset_id": str(seeded.foreign_id), "kind": "image"})
    forged = _rebinding(seeded.binding, media_snapshot=snapshot)
    deleted = _without(before, HORSE, cover_index=COVER - 1)
    response = _put(
        client, seeded, _save_body(deleted, expected_version=1, brief_binding=forged), "F15"
    )
    assert response.status_code == 200, response.text
    saved = _stored(seeded).brief_binding
    assert _snapshot_ids(saved) == [str(s.asset_id) for s in deleted.slides]
    assert str(seeded.foreign_id) not in _snapshot_ids(saved)
    assert all(r["gcs_generation"] != "client-forged" for r in saved.media_snapshot["assets"])


def test_stale_device_binding_keeps_the_newer_stored_request(
    client: TestClient, seeded: SimpleNamespace, agent: type[_ParsedOps]
) -> None:
    """F19: device B saves turn 2; device A deletes a slide with its older binding."""
    device_a = _stored(seeded)
    agent.ops = [{"op": "set_post_caption", "caption": "Keep the horse title"}]
    chat = _chat(client, seeded, device_a, "Keep the horse title")
    assert chat.status_code == 200, chat.text
    proposal = SlidePostDraft.model_validate(chat.json()["draft"])
    saved = _put(client, seeded, _save_body(proposal, expected_version=1), "device B turn 2")
    assert saved.status_code == 200, saved.text
    newer = _stored(seeded).brief_binding
    assert "Keep the horse title" in newer.creator_request

    # Device A: deletes a slide, gets 409, rebases by resending the same value (B1).
    deleted = _without(device_a, HORSE, cover_index=COVER - 1)
    stale = _put(client, seeded, _save_body(deleted, expected_version=1), "device A stale")
    assert stale.status_code == 409
    rebased = _put(client, seeded, _save_body(deleted, expected_version=2), "device A rebase")
    assert rebased.status_code == 200, rebased.text
    stored = _stored(seeded).brief_binding
    assert stored.creator_request == newer.creator_request
    assert stored.brief == newer.brief and stored.choice_answers == CHOICE_ANSWERS
    assert _snapshot_ids(stored) == [str(s.asset_id) for s in deleted.slides]


def test_unanchored_binding_cannot_replace_stored_authority(
    client: TestClient, seeded: SimpleNamespace
) -> None:
    """F20: a divergent request, a different brief or fabricated answers -> 422."""
    before = _stored(seeded)
    deleted = _without(before, HORSE, cover_index=COVER - 1)
    forged_brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="order", scope="global", description="horse first")
        ],
    )
    cases = {
        "F20 divergent request": _rebinding(seeded.binding, creator_request="Only horses"),
        "F20 forged brief": _rebinding(seeded.binding, brief=forged_brief),
        "F20 forged answers": _rebinding(
            seeded.binding,
            choice_answers=[{**CHOICE_ANSWERS[0], "option": "chronological"}],
        ),
    }
    for step, binding in cases.items():
        response = _put(
            client, seeded, _save_body(deleted, expected_version=1, brief_binding=binding), step
        )
        assert response.status_code == 422, (step, response.text)
        assert response.json()["detail"]["code"] == "slide_post_binding_invalid", step
        _assert_unchanged(seeded, before)


def test_replaced_source_bytes_are_not_a_slide_change(
    client: TestClient, seeded: SimpleNamespace
) -> None:
    """F21: same asset ids, new generation -> 422 (no silent re-stamp of approval)."""
    before = _stored(seeded)
    with sync_session() as db:
        row = db.get(PlanItemAsset, seeded.asset_ids[HORSE])
        row.gcs_generation = "999999999999"
        db.commit()
    reordered = before.model_copy(
        update={"slides": list(reversed(before.slides)), "cover_index": 0}
    )
    response = _put(
        client, seeded, _save_body(reordered, expected_version=1), "F21 re-uploaded source"
    )
    assert response.status_code == 422, response.text
    _assert_unchanged(seeded, before)


def test_stale_version_and_bad_assets_change_nothing(
    client: TestClient, seeded: SimpleNamespace
) -> None:
    before = _stored(seeded)
    deleted = _without(before, HORSE, cover_index=COVER - 1)

    stale = _put(client, seeded, _save_body(deleted, expected_version=0), "F11 stale version")
    assert stale.status_code == 409, stale.text
    assert stale.json()["detail"]["code"] == "slide_post_version_conflict"
    _assert_unchanged(seeded, before)

    out_of_range = _put(
        client, seeded, _save_body(deleted, expected_version=1, cover_index=9), "F3 cover range"
    )
    assert out_of_range.status_code == 422, out_of_range.text
    _assert_unchanged(seeded, before)

    for step, asset_id, kind in (
        ("F12 unowned", seeded.foreign_id, "image"),
        ("F12 not ready", seeded.not_ready_id, "image"),
        ("F12 wrong kind", seeded.asset_ids[0], "video"),
    ):
        slides = deleted.model_dump(mode="json")["slides"]
        slides[0] = {**slides[0], "asset_id": str(asset_id), "kind": kind}
        response = _put(
            client, seeded, _save_body(deleted, expected_version=1, slides=slides), step
        )
        assert response.status_code == 422, (step, response.text)
        _assert_unchanged(seeded, before)


def test_old_client_and_flag_off_keep_the_stored_binding(
    client: TestClient, seeded: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    before = _stored(seeded)
    deleted = _without(before, HORSE, cover_index=COVER - 1)
    body = _save_body(deleted, expected_version=1)
    del body["brief_binding"]
    response = _put(client, seeded, body, "F13 old client")
    assert response.status_code == 200, response.text
    assert _stored(seeded).brief_binding == seeded.binding

    monkeypatch.setattr(settings, "kria_brief_binding_enabled", False)
    current = _stored(seeded)
    forged = _rebinding(seeded.binding, creator_request="something else entirely")
    response = _put(
        client,
        seeded,
        _save_body(
            _without(current, 0, cover_index=0),
            expected_version=current.version,
            brief_binding=forged,
        ),
        "F14 flag off",
    )
    assert response.status_code == 200, response.text
    assert _stored(seeded).brief_binding == seeded.binding


# ------------------------------------------------------------------ evidence


def _write_evidence(seeded: SimpleNamespace) -> None:
    out = os.environ.get("KRI812_EVIDENCE_DIR")
    if not out:
        return
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            cwd=Path(__file__).parent,
            check=False,
        ).stdout.strip()
    except OSError:
        revision = None
    report = {
        "ticket": "KRI-812",
        "api_revision": revision,
        "effective_flags": {
            name: getattr(settings, name)
            for name in (
                "slide_posts_enabled",
                "slide_post_chat_edit_enabled",
                "slide_post_rich_text_enabled",
                "kria_brief_binding_enabled",
                "slide_post_framing_enabled",
                "slide_post_sizes_enabled",
                "slide_post_drawing_enabled",
                "slide_post_stickers_enabled",
            )
        },
        "item_id": str(seeded.item_id),
        "thread_id": str(seeded.thread_id),
        "source_assets": [
            {"asset_id": str(a), "title": TITLES[i], "gcs_generation": seeded.generations[str(a)]}
            for i, a in enumerate(seeded.asset_ids)
        ],
        **EVIDENCE,
    }
    path = Path(out)
    path.mkdir(parents=True, exist_ok=True)
    (path / "report.json").write_text(json.dumps(report, indent=2, default=str))
