"""KRI-693: a Visuals photo in the clip picker must read as a photo, not "Clip 9".

The picker question lists every owned clip as a candidate id, including pool photos
(``asset-<uuid>``). The ids carry no kind and no wording, so the iPhone drew the photo as
"Clip 9" with no thumbnail. The payload now names the photo candidates (additive
``clip_question.candidate_media``, wording in the chat's language); videos are untouched.

Ways this could fail, each pinned below:
- the photo is missing from ``candidate_media`` (the iPhone falls back to "Clip N"),
- a video gets an entry (video rows must not change),
- the wording ignores the chat language (Turkish chat shows "Photo 1"),
- a payload with no photo grows a ``candidate_media`` key (existing payloads change),
- numbering counts photos that are not in the question, or a confirmed photo,
- the photo list breaks the candidate ids the answer is matched on.
"""

from __future__ import annotations

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_request_resolver import ClipRequestResolverAgent, ClipRequestResolverOutput
from app.kria.reply_language import reply_language_for
from app.schemas.clip_intents import ClipIntent
from app.services import clip_intent_planning as service
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn
from app.services.clip_selection import build_clip_question

PHOTO_A = "asset-d9627a79-b228-4db0-9cbb-201f6f921bb2"
PHOTO_B = "asset-0a1b2c3d-0000-4000-8000-000000000002"


def _clip(media_id: str, kind: str = "video") -> IntentClip:
    return IntentClip(
        media_id=media_id,
        kind=kind,
        analysis={"subject": "a boat at night", "description": "boat"},
        gcs_path=f"users/u/clips/{media_id}",
    )


def _boat_clips(*photos: str) -> list[IntentClip]:
    videos = [_clip(f"analysis-proxy-ios-{i}.mp4") for i in range(1, 4)]
    return [*videos, *(_clip(p, "image") for p in photos)]


def _lit_house() -> ClipIntent:
    return ClipIntent(intent_id="g1", op="group", attribute="lit house")


async def _question(monkeypatch, clips: list[IntentClip]) -> dict:
    monkeypatch.setattr(service.settings, "kria_clip_selection_questions_enabled", True)
    monkeypatch.setattr(
        ClipRequestResolverAgent,
        "run",
        lambda self, input, *, ctx=None: ClipRequestResolverOutput(),
    )
    result = await resolve_clip_intents_for_turn(
        intents=[_lit_house()],
        creator_request="the lit house photo",
        clips=clips,
        run_context=RunContext(),
    )
    assert result.clip_question is not None
    return result.clip_question


@pytest.mark.asyncio
async def test_pool_photo_is_named_as_a_photo(monkeypatch) -> None:
    question = await _question(monkeypatch, _boat_clips(PHOTO_A))
    assert question["candidate_media"] == [
        {"media_id": PHOTO_A, "kind": "image", "label": "Photo 1"}
    ]
    # The ids the answer is matched on are exactly what they were before.
    (cat,) = question["categories"]
    assert cat["candidate_media_ids"][-1] == PHOTO_A and len(cat["candidate_media_ids"]) == 4


@pytest.mark.asyncio
async def test_photo_wording_follows_the_chat_language(monkeypatch) -> None:
    with reply_language_for("tr"):
        question = await _question(monkeypatch, _boat_clips(PHOTO_A, PHOTO_B))
    labels = [entry["label"] for entry in question["candidate_media"]]
    assert labels == ["Fotoğraf 1", "Fotoğraf 2"]


@pytest.mark.asyncio
async def test_videos_get_no_entry_and_a_photo_free_payload_is_unchanged(monkeypatch) -> None:
    question = await _question(monkeypatch, _boat_clips())
    assert "candidate_media" not in question
    assert set(question) == {"version", "question_id", "categories", "allow_none"}


@pytest.mark.asyncio
async def test_two_photos_are_numbered_in_clip_order(monkeypatch) -> None:
    question = await _question(monkeypatch, _boat_clips(PHOTO_A, PHOTO_B))
    assert [(e["media_id"], e["label"]) for e in question["candidate_media"]] == [
        (PHOTO_A, "Photo 1"),
        (PHOTO_B, "Photo 2"),
    ]


def test_photo_that_is_not_a_candidate_is_not_named_or_counted() -> None:
    categories = [
        {
            "key": "group:lit-house",
            "label": "lit house",
            "op": "group",
            "candidate_media_ids": ["v1.mp4", PHOTO_B],
            "suggested_media_ids": [],
        }
    ]
    question = build_clip_question(
        categories, media_kinds={"v1.mp4": "video", PHOTO_A: "image", PHOTO_B: "image"}
    )
    assert question is not None
    # PHOTO_A is not offered in this question, so PHOTO_B is the first photo the creator sees.
    assert question["candidate_media"] == [
        {"media_id": PHOTO_B, "kind": "image", "label": "Photo 1"}
    ]


def test_builder_without_kinds_is_unchanged() -> None:
    categories = [
        {
            "key": "group:x",
            "label": "x",
            "op": "group",
            "candidate_media_ids": ["a", PHOTO_A],
            "suggested_media_ids": [],
        }
    ]
    question = build_clip_question(categories)
    assert question is not None and "candidate_media" not in question
