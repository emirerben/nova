"""The exact JSON the iOS app sends to POST /plan-items/{id}/slide-post/chat-edit for a
brand-new post (locally seeded, unsaved draft) must validate against the server model.

Prod 2026-10-05: the seeded draft carried ``version: 0`` and ``SlidePostDraft.version`` is
``ge=1``, so every first AI message on a new post was a 422. The iOS app now clamps the wire
copy to >= 1 (``SlidePostChatEditRequest.init``); the server ignores the client's version for
a client draft (``max(server_version, 1)``). Payloads below are dumped from
``SlidePostChatEditTests.testBrandNewPostChatEditSendsAServerValidDraft``.
"""

from __future__ import annotations

import copy

import pytest
from pydantic import ValidationError

from app.routes.plan_items import SlidePostChatEditBody

_IDS = [f"0b7f6d6e-1111-4a5b-8c11-00000000000{i}" for i in (1, 2, 3)]

IOS_NEW_POST_BODY = {
    "client_request_id": "6F0C2B53-8C3B-4E55-9B0B-2D1D8C9A7A10",
    "draft": {
        "cover_index": 0,
        "caption": "",
        "platform_profile": "instagram_carousel",
        "schema_version": 1,
        "slides": [
            {"asset_id": a, "id": f"7A1E0C3E-0000-4000-8000-00000000000{i}", "kind": "image"}
            for i, a in enumerate(_IDS, start=1)
        ],
        "user_edited": False,
        "version": 1,
    },
    "expected_version": 0,
    "message": "make a slideshow",
    "turns": [],
}


def test_ios_new_post_chat_edit_payload_validates() -> None:
    body = SlidePostChatEditBody.model_validate(IOS_NEW_POST_BODY)
    assert body.draft is not None and body.draft.version == 1
    assert len(body.draft.slides) == 3


def test_unsaved_seed_with_version_zero_is_the_422() -> None:
    legacy = copy.deepcopy(IOS_NEW_POST_BODY)
    legacy["draft"]["version"] = 0  # what the app sent before the fix
    with pytest.raises(ValidationError) as err:
        SlidePostChatEditBody.model_validate(legacy)
    assert any(e["loc"] == ("draft", "version") for e in err.value.errors())


def test_prior_turns_shape_from_ios_validates() -> None:
    body = copy.deepcopy(IOS_NEW_POST_BODY)
    body["turns"] = [
        {"role": "user", "content": "sort them"},
        {"role": "assistant", "content": "Done.", "applied": ["Reordered 3"]},
    ]
    assert len(SlidePostChatEditBody.model_validate(body).turns) == 2
