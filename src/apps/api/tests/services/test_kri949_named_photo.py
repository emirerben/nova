"""KRI-949: "the ribeye photo" is asked as a moment "in your video", the photo never offered.

Don Julio walk-in M2 (prod 5a96e1d58, thread 545F53A9, 2026-10-26): one 137 s take plus the
ribeye photo as a Visual. The resolver matched "the ribeye photo" to the photo in rounds 1
and 3 (0.80) and returned nothing for it in rounds 4 and 6. KRI-779's one-video filter then
dropped the photo from the picker (its record was not empty and nothing flagged it), so the
only candidate was the video and the copy said "I couldn't find "the ribeye photo" in your
video". No answer could be right; the chat looped and no video was made.

The fix reads no words: when the resolver put NOTHING forward for an intent, its silence about
a photo is no evidence the photo does not answer it, so the photos stay in the picker. Kria
never picks the photo by itself: the only photo is not always the one named (KRI-692's Tigre
fixture names "ışıklı ev fotoğrafı" with a bathtub photo as the only image).

The replay fakes only the model transport (helpers from the KRI-779 test): resolver verdicts
are rebuilt from each round's prod ``intent_stats`` (n=0 = the model returned nothing for that
intent). Prod does not store the planner's wording, so the non-photo attributes are stand-ins;
the photo rows carry the label prod showed ("the ribeye photo"). The photo record is a
stand-in too (prod does not expose it); any non-empty record takes the same path. On main the
round-4 and round-6 replays reproduce prod's question and ``clip_question`` word for word.

Ways this could fail, each pinned below:
- rounds 4/6 still offer only the video, or still say "in your video",
- the photo row has no thumbnail entry (KRI-693 ``candidate_media``),
- the photo is used without the creator choosing it (a guess, possibly the wrong photo),
- a video-only project loses KRI-779's plain "in your video" question,
- a moment the resolver put the take forward for (KRI-779's Merope thumbs-up) starts asking
  instead of settling on the take, or starts offering the photo,
- several photos are not all offered,
- a Turkish chat still gets "Videonda … bulamadım" with a photo left out,
- an exclude asks (it never asks), or a technical failure (vision still pending) becomes a
  question.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.kria.reply_language import reply_language_for
from app.schemas.clip_intents import ClipIntent
from app.services.clip_intent_resolution import IntentClip
from tests.services.test_kri779_single_take_moments import _by_id, _patch, _resolve

pytestmark = pytest.mark.asyncio

TAKE = "analysis-proxy-ios-155481DC-92B9-431A-BD9B-6D69972E2894.mp4"
PHOTO = "asset-d17aeec6-7858-4de2-9f12-1e598efca785"
SECOND_PHOTO = "asset-00000000-0000-4000-8000-000000000949"

# The kit prompt Yasin sent in round 1 (kria-julio2-test M2_00_PROMPT.txt).
REQUEST = (
    "This is one 2-minute take of our walk into Don Julio. Pull out six moments, about 2 "
    "seconds each, in this order: the DON JULIO window sign from the street, the queue under "
    "the striped awning, the butcher holding a cut of beef, the cook at the grill with his "
    "tongs, the dried flowers hanging from the ceiling, and the two red Michelin plaques on "
    "the brick wall. Right after the butcher moment, show the ribeye photo full screen with "
    'the text "Ojo de bife 🥩". End on Atakan (black T-shirt) saying "Super excited!" with '
    'his sound; everything else silent. Title: "Don Julio, Palermo". Make it 15 seconds.'
)

TAKE_ANALYSIS = {
    "subject": "walk into a steakhouse",
    "description": "street sign, queue, kitchen, butcher, grill, dried flowers, plaques, friend",
    "duration_s": 136.93,
}
# The photo is a frame grab of the kitchen: a cook seasoning raw cuts on a white board.
PHOTO_ANALYSIS = {
    "subject": "cook seasoning raw steaks",
    "description": "a woman in a black apron seasons raw beef cuts on a white cutting board",
}

WHICH_CLIPS = (
    'I couldn\'t tell which of your clips show "the ribeye photo". '
    "Tap the clips that do, or tell me there aren't any."
)


def _clips(*photos: str) -> list[IntentClip]:
    return [
        IntentClip(media_id=TAKE, kind="video", analysis=dict(TAKE_ANALYSIS), gcs_path=f"u/{TAKE}"),
        *(
            IntentClip(
                media_id=p, kind="image", analysis=dict(PHOTO_ANALYSIS), gcs_path=f"u/{p}.jpg"
            )
            for p in photos
        ),
    ]


def _match(intent_id: str, *pairs: tuple[str, float]) -> dict[str, Any]:
    return {
        "intent_id": intent_id,
        "assignments": [{"media": m, "confidence": c, "evidence": "record"} for m, c in pairs],
    }


def _round4() -> tuple[list[ClipIntent], dict[str, Any]]:
    """Round 4 (turn b80c1052): include + caption for the photo, both n=0."""
    intents = [
        ClipIntent(intent_id="include_sign", op="include", attribute="the DON JULIO window sign"),
        ClipIntent(intent_id="include_ribeye_photo", op="include", attribute="the ribeye photo"),
        ClipIntent(intent_id="include_cook", op="include", attribute="the cook at the grill"),
        ClipIntent(intent_id="include_flowers", op="include", attribute="the dried flowers"),
        ClipIntent(intent_id="include_michelin", op="include", attribute="the Michelin plaques"),
        ClipIntent(intent_id="include_atakan", op="include", attribute="Atakan, Super excited"),
        ClipIntent(intent_id="order_sequence", op="order", attribute="the moments in order"),
        ClipIntent(intent_id="order_atakan_last", op="order", attribute="Atakan", position="last"),
        ClipIntent(
            intent_id="caption_ribeye_photo",
            op="caption",
            attribute="the ribeye photo",
            creator_text="Ojo de bife 🥩",
        ),
    ]
    resolver = {
        "intents": [
            _match("include_sign", ("m001", 0.7)),
            {"intent_id": "include_ribeye_photo"},
            _match("include_cook", ("m001", 0.9)),
            _match("include_flowers", ("m001", 0.9)),
            _match("include_michelin", ("m001", 0.9)),
            _match("include_atakan", ("m001", 0.8)),
            _match("order_sequence", ("m001", 0.9), ("m002", 0.7)),
            _match("order_atakan_last", ("m001", 0.8)),
            {"intent_id": "caption_ribeye_photo"},
        ]
    }
    return intents, resolver


_ROUND6 = {
    "clip_order_1": ("the DON JULIO window sign", 0.8),
    "clip_order_4": ("the ribeye photo", None),
    "clip_order_5": ("the cook at the grill", 0.9),
    "clip_order_6": ("the dried flowers", 1.0),
    "clip_order_7": ("the Michelin plaques", 1.0),
    "clip_order_8": ("Atakan, Super excited", 0.9),
}


def _round6(attribute: str = "the ribeye photo", op: str = "order"):
    """Round 6 (turn d8f1e9dd): six order intents, the photo one (clip_order_4) n=0."""
    intents = [
        ClipIntent(
            intent_id=i,
            op=op if i == "clip_order_4" else "order",
            attribute=attribute if i == "clip_order_4" else words,
        )
        for i, (words, _) in _ROUND6.items()
    ]
    resolver = {
        "intents": [
            _match(i, ("m001", conf)) if conf is not None else {"intent_id": i}
            for i, (_, conf) in _ROUND6.items()
        ]
    }
    return intents, resolver


async def test_round_four_offers_the_photo_for_both_rows(monkeypatch) -> None:
    intents, resolver = _round4()
    _patch(monkeypatch, resolver)

    result = await _resolve(intents, _clips(PHOTO), request=REQUEST)

    assert result.status == "needs_creator"
    categories = result.clip_question["categories"]
    # KRI-951: the include and caption asks about the same photo are one row; the caption
    # rides along, so one tap answers both.
    assert [(c["key"], c["candidate_media_ids"]) for c in categories] == [
        ("include:the-ribeye-photo", [TAKE, PHOTO]),
    ]
    assert [(a["key"], a["creator_text"]) for a in categories[0]["also_answers"]] == [
        ("caption:the-ribeye-photo", "Ojo de bife 🥩"),
    ]
    # KRI-693: the photo row is drawn with its thumbnail, named as a photo.
    assert result.clip_question["candidate_media"] == [
        {"media_id": PHOTO, "kind": "image", "label": "Photo 1"}
    ]
    assert "in your video" not in result.question
    # Nothing is chosen for the creator: the photo is offered, never assumed.
    assert _by_id(result)["include_ribeye_photo"].assignments == []
    assert _by_id(result)["caption_ribeye_photo"].assignments == []
    # What the resolver matched keeps its own clips.
    assert [a.media_id for a in _by_id(result)["include_cook"].assignments] == [TAKE]


async def test_round_six_offers_the_photo(monkeypatch) -> None:
    intents, resolver = _round6()
    _patch(monkeypatch, resolver)

    result = await _resolve(intents, _clips(PHOTO), request=REQUEST)

    assert result.status == "needs_creator"
    (category,) = result.clip_question["categories"]
    assert category["key"] == "order:the-ribeye-photo"
    assert category["candidate_media_ids"] == [TAKE, PHOTO]
    assert result.question == WHICH_CLIPS
    assert _by_id(result)["clip_order_4"].assignments == []
    assert result.diagnostics["single_take_settled"] == 0


async def test_a_video_only_project_keeps_the_plain_question(monkeypatch) -> None:
    intents, resolver = _round6()
    _patch(monkeypatch, resolver)

    result = await _resolve(intents, _clips(), request=REQUEST)

    (category,) = result.clip_question["categories"]
    assert category["candidate_media_ids"] == [TAKE]
    assert result.question == (
        'I couldn\'t find "the ribeye photo" in your video. '
        "Tap the video if it's in there, or tell me it isn't."
    )


async def test_a_moment_the_resolver_put_on_the_take_still_settles_there(monkeypatch) -> None:
    # KRI-779's Merope case: the resolver sent the take to the whole-take check, which said
    # "no" for a moment that is in the take. Still settled on the take; the photo stays out.
    intents, resolver = _round6("his thumbs-up")
    resolver["intents"][1] = {
        "intent_id": "clip_order_4",
        "needs_vision": [{"media": "m001", "question": "Is the thumbs-up shown?"}],
    }
    _patch(monkeypatch, resolver, vision={"his thumbs-up": ("no", 1.0, "points with a finger")})

    result = await _resolve(intents, _clips(PHOTO), request=REQUEST)

    assert result.status == "resolved" and result.clip_question is None
    assert [a.media_id for a in _by_id(result)["clip_order_4"].assignments] == [TAKE]
    assert result.diagnostics["single_take_settled"] == 1


async def test_several_photos_are_all_offered(monkeypatch) -> None:
    intents, resolver = _round6()
    _patch(monkeypatch, resolver)

    result = await _resolve(intents, _clips(PHOTO, SECOND_PHOTO), request=REQUEST)

    (category,) = result.clip_question["categories"]
    assert category["candidate_media_ids"] == [TAKE, PHOTO, SECOND_PHOTO]
    assert [c["media_id"] for c in result.clip_question["candidate_media"]] == [
        PHOTO,
        SECOND_PHOTO,
    ]
    assert result.question == WHICH_CLIPS


async def test_a_turkish_chat_offers_the_photo_too(monkeypatch) -> None:
    intents, resolver = _round6("kaburga fotoğrafı")
    _patch(monkeypatch, resolver)

    with reply_language_for("tr"):
        result = await _resolve(intents, _clips(PHOTO), request=REQUEST)

    (category,) = result.clip_question["categories"]
    assert category["candidate_media_ids"] == [TAKE, PHOTO]
    assert result.clip_question["candidate_media"][0]["label"] == "Fotoğraf 1"
    assert not result.question.startswith("Videonda")


async def test_an_exclude_never_asks(monkeypatch) -> None:
    intents, resolver = _round6(op="exclude")
    _patch(monkeypatch, resolver)

    result = await _resolve(intents, _clips(PHOTO), request=REQUEST)

    assert result.status == "resolved" and result.clip_question is None
    assert _by_id(result)["clip_order_4"].assignments == []


async def test_a_pending_check_is_not_turned_into_a_question(monkeypatch) -> None:
    intents, resolver = _round6()
    resolver["intents"][1] = {
        "intent_id": "clip_order_4",
        "needs_vision": [{"media": "m001", "question": "Is the ribeye photo shown?"}],
    }
    _patch(monkeypatch, resolver)

    result = await _resolve(intents, _clips(PHOTO), request=REQUEST, max_vision_requeries=0)

    assert result.status == "pending"
    assert result.clip_question is None
    assert _by_id(result)["clip_order_4"].assignments == []
