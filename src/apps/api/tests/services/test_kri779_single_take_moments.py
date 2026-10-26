"""KRI-779: one take never gets a "which of your clips?" picker for a moment inside it.

Merope M2 (prod 6b689783a, thread EE7B4DCA, 2026-10-21): one 68 s take + one Cotopaxi photo,
six moments named in order. Five moments resolved to the take; "his thumbs-up" did not,
because the whole-take check `Does this clip match this description: "his thumbs-up"?`
answered "no" at confidence 1.0 ("man points with index finger") although the thumbs-up is
at ~1:01.5. Turn 1 then asked "I couldn't tell which of your clips show "his thumbs-up"" with
the take AND the photo as candidates: a question whose only real answer is the take, and one
a clip picker cannot even ask properly (the open question is WHERE in the take).

The replay fakes only the model transport: the real resolver and vision agents render their
prompts and parse raw JSON rebuilt from the recorded turn 1 (resolver verdicts from the turn's
``intent_stats``, vision answers verbatim from the take's prod answer cache). On main it
reproduces prod's turn-1 question and ``clip_question`` payload exactly.

Ways this could fail, each pinned below:
- the M2 turn still asks a clip question (one video + an unflagged photo),
- the forced answer lands on the photo, or on the take AND the photo,
- a multi-video project loses today's picker,
- a photo the analysis flagged for the moment (a real second answer) is dropped,
- a photo we know nothing about (empty record) is ruled out,
- an `exclude` is forced onto the take (it would remove the creator's only footage),
- a technical failure (vision still pending) is turned into an answer,
- a moment the take's record rules out (the resolver put nothing forward) is forced onto the
  take instead of asked, or, in a video-only project, is asked as "which of your clips"
  (KRI-949: with a photo in the project, the photo is offered),
- a creator caption is forced without its on-screen text (resolved caption, no caption_text),
- the forced evidence reads as the creator's own word (``CREATOR_EVIDENCE``),
- an intent the resolver already matched is changed.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.agents._runtime import ModelClient, ModelInvocation, RunContext
from app.config import settings
from app.kria.reply_language import reply_language_for
from app.schemas.clip_intents import CREATOR_EVIDENCE, ONLY_VIDEO_EVIDENCE, ClipIntent
from app.services import clip_intent_resolution
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn

pytestmark = pytest.mark.asyncio

TAKE = "analysis-proxy-ios-11CF148F-2374-4097-B299-FC3926E878BD.mp4"
PHOTO = "asset-6ae61e1a-96ff-4776-8f95-54ace669db23"
SECOND_TAKE = "analysis-proxy-ios-22222222-0000-4000-8000-000000000002.mp4"

REQUEST = (
    "One 68-second take at Merope, a Colombian café: Caro brings us the house oblea and "
    "Atakan takes the first bite. Make a montage of six moments from it, about 2 seconds "
    "each, in this order: Caro arriving with the black tray, her hello wave, the plate pushed "
    "close to the camera, Atakan's first bite, his thumbs-up, Caro's bye-bye wave. Put the "
    'Cotopaxi photo right after the hello wave for 1 second with the text "From Ecuador 🇪🇨". '
    "Freeze the frame for one second on Atakan's bite. Keep the original sound, no music. "
    'Title: "Colombian dessert, Ecuadorian hands". About 14 seconds.'
)

# The take's stored record (prod clip_metadata, trimmed to the fields the resolver reads).
TAKE_ANALYSIS = {
    "subject": "waitress presenting a dessert",
    "description": (
        "waitress introduces herself waitress describes dessert ingredients waitress "
        "explains restaurant name meaning waitress hands over dessert man takes first bite "
        "of dessert"
    ),
    "duration_s": 68.37,
    "best_moments": [
        {"start_s": start, "end_s": end, "energy": energy, "description": what}
        for start, end, energy, what in (
            (10.74, 15.34, 6.0, "waitress introduces herself"),
            (15.34, 22.84, 7.0, "waitress describes dessert ingredients"),
            (42.44, 45.74, 6.0, "waitress explains restaurant name meaning"),
            (50.04, 52.04, 5.0, "waitress hands over dessert"),
            (57.04, 59.84, 8.0, "man takes first bite of dessert"),
        )
    ],
}
PHOTO_ANALYSIS = {"subject": "Snowy mountain at sunrise", "description": "snow-capped volcano"}

# Turn-1 intents (ids from the turn's intent_stats, wording from its vision questions).
MOMENTS = {
    "order_1": "Caro arriving with the black tray",
    "order_2": "her hello wave",
    "order_3": "the plate pushed close to the camera",
    "order_4": "Atakan's first bite",
    "order_5": "his thumbs-up",
    "order_6": "Caro's bye-bye wave",
}

# The take's cached answers to the whole-take check (prod, generation 1792587128136153).
VISION = {
    "caro arriving with the black tray": ("yes", 1.0, "Caro arrives holding a black tray"),
    "her hello wave": ("yes", 0.8, "woman waves goodbye at 0:07"),
    "the plate pushed close to the camera": ("yes", 0.9, "woman moves plate closer to camera"),
    "his thumbs-up": ("no", 1.0, "man points with index finger"),
    "caro's bye-bye wave": ("yes", 1.0, "Caro waves at 0:49-0:50 and 1:07"),
}


def _take(media_id: str = TAKE) -> IntentClip:
    return IntentClip(
        media_id=media_id,
        kind="video",
        analysis=dict(TAKE_ANALYSIS),
        gcs_path=f"users/u/clips/{media_id}",
    )


def _photo(analysis: dict | None = None) -> IntentClip:
    return IntentClip(
        media_id=PHOTO,
        kind="image",
        analysis=dict(PHOTO_ANALYSIS) if analysis is None else analysis,
        gcs_path=f"users/u/visuals/{PHOTO}.jpg",
    )


def _m2_intents() -> list[ClipIntent]:
    orders = [ClipIntent(intent_id=i, op="order", attribute=a) for i, a in MOMENTS.items()]
    return [
        *orders[:2],
        ClipIntent(intent_id="include_cotopaxi", op="include", attribute="the Cotopaxi photo"),
        ClipIntent(
            intent_id="caption_cotopaxi",
            op="caption",
            attribute="the Cotopaxi photo",
            creator_text="From Ecuador 🇪🇨",
        ),
        *orders[2:],
    ]


def _m2_resolver(take_alias: str = "m001", photo_alias: str = "m002") -> dict[str, Any]:
    """Turn 1's resolver verdicts as raw model JSON: order_4 matched (0.90), the photo matched
    its two intents (0.80), every other moment was sent to the whole-take vision check."""

    def vision(intent_id: str) -> dict[str, Any]:
        return {
            "intent_id": intent_id,
            "needs_vision": [{"media": take_alias, "question": "Is this moment shown?"}],
        }

    def match(intent_id: str, media: str, confidence: float, evidence: str) -> dict[str, Any]:
        return {
            "intent_id": intent_id,
            "assignments": [{"media": media, "confidence": confidence, "evidence": evidence}],
        }

    return {
        "intents": [
            vision("order_1"),
            vision("order_2"),
            match("include_cotopaxi", photo_alias, 0.8, "Snowy mountain at sunrise"),
            match("caption_cotopaxi", photo_alias, 0.8, "Snowy mountain at sunrise"),
            vision("order_3"),
            match("order_4", take_alias, 0.9, "man takes first bite of dessert"),
            vision("order_5"),
            vision("order_6"),
        ]
    }


def _set_intent(resolver: dict[str, Any], entry: dict[str, Any]) -> dict[str, Any]:
    resolver["intents"] = [
        entry if e["intent_id"] == entry["intent_id"] else e for e in resolver["intents"]
    ]
    return resolver


class _ReplayTransport(ModelClient):
    """The provider boundary only: the resolver call (no media) gets ``resolver``; a vision
    call (a video reference) gets the recorded answer for the description it asks about."""

    def __init__(self, resolver: dict[str, Any], vision: dict[str, tuple[str, float, str]]):
        self.resolver = resolver
        self.vision = vision
        self.vision_prompts: list[str] = []

    def invoke(self, *, model: str, prompt: str, media_uri: str | None = None, **_: Any):
        if media_uri is None:
            return ModelInvocation(raw_text=json.dumps(self.resolver), tokens_in=10, tokens_out=20)
        self.vision_prompts.append(prompt)
        answer: dict[str, Any] = {"answer": "unknown", "confidence": 0.0, "evidence": ""}
        for attribute, (said, confidence, evidence) in self.vision.items():
            if f'"{attribute}"' in prompt.casefold():
                answer = {"answer": said, "confidence": confidence, "evidence": evidence}
        return ModelInvocation(raw_text=json.dumps(answer), tokens_in=10, tokens_out=20)


def _patch(monkeypatch, resolver: dict[str, Any], vision=VISION) -> _ReplayTransport:
    """Wire the recorded model outputs at the transport; storage/upload are no-ops."""
    monkeypatch.setattr(settings, "kria_clip_selection_questions_enabled", True)
    transport = _ReplayTransport(resolver, vision)
    monkeypatch.setattr(clip_intent_resolution, "default_client", lambda: transport)

    def _download(object_path, local_path):  # noqa: ANN001
        with open(local_path, "wb") as fh:
            fh.write(b"fake")

    monkeypatch.setattr("app.storage.download_to_file", _download)
    monkeypatch.setattr(
        "app.pipeline.agents.gemini_analyzer.gemini_upload_and_wait",
        lambda path, timeout=120: SimpleNamespace(uri="files/fake", mime_type="video/mp4"),
    )
    return transport


async def _resolve(
    intents: list[ClipIntent], clips: list[IntentClip], request: str = REQUEST, **kwargs
):
    return await resolve_clip_intents_for_turn(
        intents=intents,
        creator_request=request,
        clips=clips,
        run_context=RunContext(),
        max_vision_requeries=kwargs.pop("max_vision_requeries", 18),
        vision_deadline_s=30.0,
        **kwargs,
    )


def _by_id(result):
    return {intent.intent_id: intent for intent in result.intents}


async def test_m2_turn_one_resolves_the_thumbs_up_to_the_take_without_asking(monkeypatch) -> None:
    transport = _patch(monkeypatch, _m2_resolver())

    result = await _resolve(_m2_intents(), [_take(), _photo()])

    assert result.status == "resolved"
    assert result.clip_question is None and result.question is None
    thumbs_up = _by_id(result)["order_5"]
    assert thumbs_up.status == "resolved"
    assert [(a.media_id, a.evidence) for a in thumbs_up.assignments] == [
        (TAKE, ONLY_VIDEO_EVIDENCE)
    ]
    assert ONLY_VIDEO_EVIDENCE not in CREATOR_EVIDENCE
    # Every moment is on the take, nothing on the photo; the photo keeps its own intents.
    for intent_id in MOMENTS:
        assert [a.media_id for a in _by_id(result)[intent_id].assignments] == [TAKE]
    assert [a.media_id for a in _by_id(result)["include_cotopaxi"].assignments] == [PHOTO]
    assert _by_id(result)["caption_cotopaxi"].caption_text == "From Ecuador 🇪🇨"
    # Matched moments keep their own evidence: only the unlocated one is forced.
    assert _by_id(result)["order_6"].assignments[0].evidence == "Caro waves at 0:49-0:50 and 1:07"
    assert len(transport.vision_prompts) == 5  # the same paid checks as before
    assert result.diagnostics["single_take_settled"] == 1


async def test_two_videos_keep_todays_picker(monkeypatch) -> None:
    _patch(monkeypatch, _m2_resolver(photo_alias="m003"))

    result = await _resolve(_m2_intents(), [_take(), _take(SECOND_TAKE), _photo()])

    assert result.status == "needs_creator"
    (category,) = result.clip_question["categories"]
    assert category["intent_id"] == "order_5"
    assert category["candidate_media_ids"] == [TAKE, SECOND_TAKE, PHOTO]


async def test_a_photo_the_analysis_flagged_is_still_a_real_choice(monkeypatch) -> None:
    question = "Is this moment shown?"
    resolver = _set_intent(
        _m2_resolver(),
        {
            "intent_id": "order_5",
            "needs_vision": [
                {"media": "m001", "question": question},
                {"media": "m002", "question": question},
            ],
        },
    )
    _patch(monkeypatch, resolver)

    result = await _resolve(_m2_intents(), [_take(), _photo()])

    assert result.status == "needs_creator"
    (category,) = result.clip_question["categories"]
    assert category["candidate_media_ids"] == [TAKE, PHOTO]
    assert category["suggested_media_ids"] == [PHOTO]


async def test_a_photo_with_no_record_is_not_ruled_out(monkeypatch) -> None:
    _patch(monkeypatch, _m2_resolver())

    result = await _resolve(_m2_intents(), [_take(), _photo(analysis={})])

    assert result.status == "needs_creator"
    assert result.clip_question["categories"][0]["intent_id"] == "order_5"


async def test_an_exclude_is_never_forced_onto_the_only_take(monkeypatch) -> None:
    intent = ClipIntent(intent_id="exclude_dog", op="exclude", attribute="the dog")
    _patch(monkeypatch, {"intents": [{"intent_id": "exclude_dog"}]})

    result = await _resolve([intent], [_take()])

    assert result.status == "resolved"
    assert _by_id(result)["exclude_dog"].assignments == []


async def test_a_pending_check_is_not_turned_into_an_answer(monkeypatch) -> None:
    _patch(monkeypatch, _m2_resolver())

    result = await _resolve(_m2_intents(), [_take(), _photo()], max_vision_requeries=0)

    assert result.status == "pending"
    assert _by_id(result)["order_5"].status == "needs_creator"
    assert result.diagnostics["single_take_settled"] == 0


def _video_only_thumbs_up() -> tuple[list[ClipIntent], dict[str, Any]]:
    """M2 without the Cotopaxi photo, the resolver putting nothing forward for the thumbs-up.

    KRI-949: with a photo in the project that silence rules nothing out, so the photo is
    offered too (tests/services/test_kri949_named_photo.py); the plain question below is
    for a video-only project.
    """
    resolver = _set_intent(_m2_resolver(), {"intent_id": "order_5"})
    resolver["intents"] = [e for e in resolver["intents"] if "cotopaxi" not in e["intent_id"]]
    return [i for i in _m2_intents() if "cotopaxi" not in i.intent_id], resolver


async def test_a_moment_the_takes_record_rules_out_asks_plainly(monkeypatch) -> None:
    # The resolver read the take's record and put nothing forward for the moment: that is
    # the "not in this video" case. It still asks, says so plainly, offers only the video,
    # and never says "which of your clips".
    intents, resolver = _video_only_thumbs_up()
    _patch(monkeypatch, resolver)

    result = await _resolve(intents, [_take()])

    assert result.status == "needs_creator"
    (category,) = result.clip_question["categories"]
    assert category["intent_id"] == "order_5"
    assert category["candidate_media_ids"] == [TAKE]
    assert "candidate_media" not in result.clip_question
    assert result.question == (
        'I couldn\'t find "his thumbs-up" in your video. '
        "Tap the video if it's in there, or tell me it isn't."
    )
    assert result.diagnostics["single_take_settled"] == 0


async def test_the_plain_question_follows_the_chat_language(monkeypatch) -> None:
    intents, resolver = _video_only_thumbs_up()
    _patch(monkeypatch, resolver)

    with reply_language_for("tr"):
        result = await _resolve(intents, [_take()])

    assert result.question == (
        'Videonda "his thumbs-up" olan yeri bulamadım. Varsa videoya dokun, yoksa olmadığını söyle.'
    )


async def test_a_creator_caption_on_the_only_take_keeps_its_text(monkeypatch) -> None:
    intent = ClipIntent(
        intent_id="caption_wave", op="caption", attribute="her hello wave", creator_text="Hola!"
    )
    resolver = {
        "intents": [
            {
                "intent_id": "caption_wave",
                "needs_vision": [{"media": "m001", "question": "Is the wave shown?"}],
            }
        ]
    }
    _patch(monkeypatch, resolver, vision={"her hello wave": ("no", 1.0, "no wave visible")})

    result = await _resolve([intent], [_take()], request='Put "Hola!" on her hello wave.')

    assert result.status == "resolved" and result.clip_question is None
    caption = _by_id(result)["caption_wave"]
    assert [(a.media_id, a.evidence) for a in caption.assignments] == [(TAKE, ONLY_VIDEO_EVIDENCE)]
    assert caption.caption_text == "Hola!"
