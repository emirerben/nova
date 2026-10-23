# ruff: noqa: F811 - pytest fixtures shared from the KRI-812 harness are parameters here
"""KRI-818: a compound reorder + text Slider edit binds to stable slides (real Postgres E2E).

Troya T12: "move the museum slide (5) first and put 'Inside the Troy Museum' on it; keep
the horse title, the ruins drawing and the current cover". The text landed on the horse
slide next to its title: the copilot wrote the text window in after-the-move numbering
while the compiler reads every number in before-the-move numbering.

The journey drives the real routes (chat-edit -> PUT -> GET -> chat-edit -> generate) on
the ``nova_test`` database with the KRI-812 harness. Only the model call (replaced by the
REAL parser over a fixed op list) and the broker publish are stubbed: no paid model.

Failure modes, written before the change:
F1  [reorder 4->0, add_text 0..1 slide_id=museum] puts the text on the horse.
F2  The same bundle with the window in before-the-move numbering ([4,5)) gives a
    different draft.
F3  A target-less one-slide text in a moving bundle silently binds to the horse.
F4  The order is not exactly museum/horse/video/road/ruins (by slide id).
F5  The cover leaves the horse; a "keep the cover" index in after-the-move numbering
    silently moves it.
F6  The horse title, the ruins drawing or its sticker change.
F12 Preview, Save, reopen, a follow-up AI turn and the export disagree; the brief
    binding is lost.
F13 A failed turn changes the stored draft or its version.

Repeatable artifact: set ``KRI818_EVIDENCE_DIR`` to write ``report.json`` (requests, ops
as parsed, slide ids + asset ids before/after, per-slide texts, cover, versions, API
revision, effective flags, and what the export reader resolved).
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.agents._schemas.drawing import DrawingStroke
from app.config import settings
from app.database import sync_session
from app.models import PlanItem
from app.schemas.slide_post import (
    SlideDrawing,
    SlideEdits,
    SlidePostDraft,
    SlideRef,
    SlideSticker,
    SlideTextElement,
)
from tests.routes.test_slide_post_binding_save_postgres import (  # noqa: F401 - fixtures
    _flags,
    _ParsedOps,
    _save_body,
    _stored,
    agent,
    client,
    seeded,
)

TROY = ["horse", "video", "road", "ruins", "museum"]
AFTER = ["museum", "horse", "video", "road", "ruins"]
MUSEUM_TEXT = "Inside the Troy Museum"
T12 = (
    "Move the museum slide (5) to the front and put 'Inside the Troy Museum' on it. "
    "Keep the horse title, the ruins drawing and the current cover."
)
EVIDENCE: dict[str, Any] = {"steps": []}


def _troy_draft(seeded: SimpleNamespace) -> SlidePostDraft:
    """The seeded five assets as the Troya post: titled horse cover, drawn-on ruins."""
    ruins = SlideEdits(
        drawing=SlideDrawing(
            canvas_aspect=0.5625,
            strokes=[
                DrawingStroke(color="#FF3B30", width_frac=0.02, points=[(0.1, 0.2), (0.6, 0.7)])
            ],
        ),
        stickers=[
            SlideSticker(
                id="pin",
                asset_id=str(seeded.extra_id),
                x_frac=0.3,
                y_frac=0.7,
                width_frac=0.25,
            )
        ],
    )
    horse = SlideEdits(
        texts=[SlideTextElement(id="horse-title", text="The Trojan Horse", color="#FFD60A")]
    )
    return SlidePostDraft(
        platform_profile="tiktok_photo",
        slides=[
            SlideRef(
                id=sid,
                asset_id=asset_id,
                kind="image",
                edits={"horse": horse, "ruins": ruins}.get(sid),
            )
            for sid, asset_id in zip(TROY, seeded.asset_ids, strict=True)
        ],
        cover_index=0,
        caption="Troy",
        version=1,
        brief_binding=seeded.binding,
    )


def _seed_troy(seeded: SimpleNamespace) -> SlidePostDraft:
    draft = _troy_draft(seeded)
    with sync_session() as db:
        db.get(PlanItem, seeded.item_id).slide_post = draft.model_dump(mode="json")
        db.commit()
    return _stored(seeded)


def _texts(draft: SlidePostDraft) -> dict[str, list[str]]:
    return {
        s.id: [t.text for t in (s.edits.effective_texts() if s.edits else [])] for s in draft.slides
    }


def _record(step: str, draft: SlidePostDraft | None, **extra: Any) -> None:
    EVIDENCE["steps"].append(
        {
            "step": step,
            **extra,
            "draft": None
            if draft is None
            else {
                "version": draft.version,
                "order": [s.id for s in draft.slides],
                "asset_ids": [str(s.asset_id) for s in draft.slides],
                "texts": _texts(draft),
                "cover_slide_id": draft.slides[draft.cover_index].id,
                "binding_digest": draft.brief_binding.digest if draft.brief_binding else None,
            },
        }
    )


def _chat(
    client: TestClient,
    seeded: SimpleNamespace,
    agent: type[_ParsedOps],
    draft: SlidePostDraft,
    message: str,
    ops: list[dict],
) -> dict:
    agent.ops = ops
    response = client.post(
        f"/plan-items/{seeded.item_id}/slide-post/chat-edit",
        json={
            "message": message,
            "expected_version": draft.version,
            "draft": draft.model_dump(mode="json"),
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    proposal = SlidePostDraft.model_validate(body["draft"]) if body.get("draft") else None
    _record(
        f"chat-edit: {message}",
        proposal,
        model_ops=ops,
        outcome=body["outcome"],
        reply=body["reply"],
        base_version=body["base_version"],
    )
    return body


def _put(client: TestClient, seeded: SimpleNamespace, draft: SlidePostDraft, **kw: Any):  # noqa: ANN202
    """The iPhone Save: a sticker-capable build (the ruins slide carries a sticker)."""
    protocol = str(settings.slide_post_sticker_min_client_protocol)
    return client.put(
        f"/plan-items/{seeded.item_id}/slide-post",
        json=_save_body(draft, **kw),
        headers={"X-Kria-Client-Protocol": protocol},
    )


def _assert_t12(result: SlidePostDraft, before: SlidePostDraft) -> None:
    assert [s.id for s in result.slides] == AFTER  # F4
    assert [s.asset_id for s in result.slides] == [
        before.slides[TROY.index(sid)].asset_id for sid in AFTER
    ]
    assert _texts(result) == {  # F1
        "museum": [MUSEUM_TEXT],
        "horse": ["The Trojan Horse"],
        "video": [],
        "road": [],
        "ruins": [],
    }
    old = {s.id: s for s in before.slides}
    for slide in result.slides:  # F6: everything not named is byte-identical
        if slide.id != "museum":
            assert slide.model_dump() == old[slide.id].model_dump(), slide.id
    assert result.slides[result.cover_index].id == "horse"  # F5


def test_t12_reorder_and_text_survive_save_reopen_follow_up_and_export(
    client: TestClient,
    seeded: SimpleNamespace,
    agent: type[_ParsedOps],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stored = _seed_troy(seeded)
    _record("seeded Troya post", stored)
    moved = {"op": "reorder_clip", "from_index": 4, "to_index": 0}

    # F3/F13: the bundle that produced the Troya bug (no slide target) fails closed.
    body = _chat(
        client,
        seeded,
        agent,
        stored,
        T12,
        [moved, {"op": "add_text", "text": MUSEUM_TEXT, "start_s": 0, "end_s": 1}],
    )
    assert body["outcome"] == "failed" and body["draft"] is None
    assert "Which slide" in body["reply"]
    assert _stored(seeded) == stored

    # F5/F13: "keep the cover" written in after-the-move numbering (horse = slide 2) fails.
    body = _chat(
        client, seeded, agent, stored, T12, [moved, {"op": "set_slide_cover", "slide_index": 1}]
    )
    assert body["outcome"] == "failed" and body["draft"] is None
    assert _stored(seeded) == stored

    # F1/F2: the targeted bundle lands on the museum in either numbering, identically.
    proposals = []
    for window in ((0, 1), (4, 5)):
        body = _chat(
            client,
            seeded,
            agent,
            stored,
            T12,
            [
                moved,
                {
                    "op": "add_text",
                    "text": MUSEUM_TEXT,
                    "start_s": window[0],
                    "end_s": window[1],
                    "slide_id": "museum",
                },
            ],
        )
        assert body["outcome"] == "edited", body
        proposal = SlidePostDraft.model_validate(body["draft"])
        _assert_t12(proposal, stored)
        assert proposal.brief_binding is not None
        assert T12 in proposal.brief_binding.creator_request
        proposals.append(proposal)

    def without_new_ids(draft: SlidePostDraft) -> dict:
        dumped = draft.model_dump(mode="json")
        for slide in dumped["slides"]:
            for el in (slide.get("edits") or {}).get("texts") or []:
                if el["text"] == MUSEUM_TEXT:
                    el["id"] = "<new>"
        return dumped

    assert without_new_ids(proposals[0]) == without_new_ids(proposals[1])
    assert _stored(seeded) == stored  # chat-edit never writes

    # F12: Save, reopen.
    proposal = proposals[0]
    response = _put(client, seeded, proposal, expected_version=stored.version)
    assert response.status_code == 200, response.text
    saved = _stored(seeded)
    _record("PUT save", saved, status=response.status_code)
    assert saved.version == stored.version + 1
    _assert_t12(saved, stored)
    assert saved.brief_binding is not None and T12 in saved.brief_binding.creator_request
    reopened = client.get(f"/plan-items/{seeded.item_id}/slide-post")
    assert reopened.status_code == 200, reopened.text
    reopened_draft = SlidePostDraft.model_validate(reopened.json()["draft"])
    _record("GET reopen", reopened_draft)
    assert reopened_draft == saved

    # F12: a follow-up AI turn sees and keeps the same source-bound text and cover.
    body = _chat(
        client,
        seeded,
        agent,
        reopened_draft,
        "Make the caption 'A day in Troy'",
        [{"op": "set_post_caption", "caption": "A day in Troy"}],
    )
    assert body["outcome"] == "edited", body
    follow_up = SlidePostDraft.model_validate(body["draft"])
    _assert_t12(follow_up, stored)
    assert follow_up.caption == "A day in Troy"
    response = _put(client, seeded, follow_up, expected_version=saved.version)
    assert response.status_code == 200, response.text
    saved = _stored(seeded)
    _assert_t12(saved, stored)

    # F12: Generate dispatches the saved revision; the export reader resolves it.
    published: list[tuple] = []
    monkeypatch.setattr(
        "app.services.job_dispatch.enqueue_orchestrator_sync",
        lambda task, job_id, **kw: published.append((job_id, kw)),
    )
    response = client.post(
        f"/plan-items/{seeded.item_id}/slide-post/generate",
        json={"expected_version": saved.version},
    )
    assert response.status_code == 200, response.text
    assert len(published) == 1
    with sync_session() as db:
        exported = SlidePostDraft.model_validate(db.get(PlanItem, seeded.item_id).slide_post)
    _assert_t12(exported, stored)
    EVIDENCE["export"] = {
        "job_id": str(published[0][0]),
        "order": [s.id for s in exported.slides],
        "cover_slide_id": exported.slides[exported.cover_index].id,
        "cover_index": exported.cover_index,
        "texts": _texts(exported),
    }
    _write_evidence(seeded)


def _write_evidence(seeded: SimpleNamespace) -> None:
    out = os.environ.get("KRI818_EVIDENCE_DIR")
    if not out:
        return
    revision = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent,
        check=False,
    ).stdout.strip()
    report = {
        "ticket": "KRI-818",
        "request": T12,
        "api_revision": revision,
        "effective_flags": {
            name: getattr(settings, name)
            for name in (
                "slide_posts_enabled",
                "slide_post_chat_edit_enabled",
                "slide_post_rich_text_enabled",
                "kria_brief_binding_enabled",
                "slide_post_drawing_enabled",
                "slide_post_stickers_enabled",
            )
        },
        "item_id": str(seeded.item_id),
        "source_identities": {
            sid: str(asset_id) for sid, asset_id in zip(TROY, seeded.asset_ids, strict=True)
        },
        **EVIDENCE,
    }
    path = Path(out)
    path.mkdir(parents=True, exist_ok=True)
    (path / "report.json").write_text(json.dumps(report, indent=2, default=str))
