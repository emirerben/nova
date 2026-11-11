"""KRI-1330: a clip-picker tap answers the intent it was asked about, not its look-alikes.

Prod incident: the creator asked for titles on four events (Football, dodgeball, beach
volleyball, pub). Kria asked which clips show "the dodgeball event"; the creator tapped one
clip (answer key `caption:the-dodgeball-event`). Football and pub were then also settled on
that one clip ("creator selected", confidence 1.0), discarding the resolver's 7 football and
13 pub clips. Beach volleyball (4 tokens) was spared by luck.

Cause: `ClipSelections.match` falls back to token-set overlap (>= 0.5) between intent
attributes, and caption <-> caption was op-compatible, so the filler words "the" and "event"
carried {the, football, event} vs {the, dodgeball, event} over the threshold.

Ways this can fail, each pinned below (written before the fix):
 1. Wrong-intent leak: one tap settles other events' captions (the incident).
 2. A re-plan rewords the SAME intent: the tap must still bind (KRI-697 identity route).
 3. Two taps for two intents with overlapping words: each keeps its own clips.
 4. A tap whose key matches no intent settles nothing.
 5. include/group rows keep word-overlap matching (their picker answers stand in for each other).
 6. An include tap never becomes an exclude (KRI-667).

The thread fold, the selection match and the settle step are the real code; no network.
"""

from __future__ import annotations

from typing import Any

from app.schemas.clip_intents import ClipIntent
from app.services.clip_intent_planning import apply_creator_selections
from app.services.clip_intent_resolution import IntentClip
from app.services.clip_selection import build_clip_question, fold_clip_selections

TAPPED = "analysis-proxy-ios-9BFEFBF8-1DB0-4B1B-AAA1-A933BB5DDB1E.mp4"
FOOTBALL_CLIP = "analysis-proxy-ios-football.mp4"
PUB_CLIP = "analysis-proxy-ios-pub.mp4"
REQUEST = "Add titles for the football, dodgeball, beach volleyball and pub events."

INTENTS = [
    ClipIntent(
        intent_id="caption_intro_title",
        op="caption",
        attribute="the intro title",
        creator_text="Event day",
    ),
    ClipIntent(
        intent_id="caption_football",
        op="caption",
        attribute="the football event",
        creator_text="Football",
    ),
    ClipIntent(
        intent_id="caption_dodgeball",
        op="caption",
        attribute="the dodgeball event",
        creator_text="Dodgeball",
    ),
    ClipIntent(
        intent_id="caption_beach_volleyball",
        op="caption",
        attribute="the beach volleyball event",
        creator_text="Beach volleyball",
    ),
    ClipIntent(
        intent_id="caption_pub",
        op="caption",
        attribute="the pub event",
        creator_text="Pub",
    ),
]


def _clips() -> list[IntentClip]:
    return [
        IntentClip(
            media_id=m, kind="video", analysis={"subject": "event footage"}, gcs_path=f"u/{m}"
        )
        for m in (TAPPED, FOOTBALL_CLIP, PUB_CLIP)
    ]


def _category(intent: ClipIntent) -> dict[str, Any]:
    key = f"{intent.op}:{'-'.join(intent.attribute.split())}"
    return {
        "key": key,
        "label": intent.attribute,
        "op": intent.op,
        "intent_id": intent.intent_id,
        "creator_text": intent.creator_text,
        "candidate_media_ids": [TAPPED, FOOTBALL_CLIP, PUB_CLIP],
    }


def _selections(asked: list[ClipIntent], taps: dict[str, list[str]]):
    """The folded thread after the creator answered the picker for ``asked``."""
    question = build_clip_question([_category(i) for i in asked], question_id="q1")
    assert question is not None
    answers = [
        {"key": cat["key"], "media_ids": taps[cat["intent_id"]]}
        for cat in question["categories"]
        if cat["intent_id"] in taps
    ]
    return fold_clip_selections(
        [
            ("assistant", {"clip_question": question}),
            ("user", {"clip_selection": {"question_id": "q1", "answers": answers}}),
        ]
    )


def _apply(intents: list[ClipIntent], selections):
    settled, remaining, dropped = apply_creator_selections(
        intents, clips=_clips(), selections=selections, creator_request=REQUEST
    )
    return {r.intent_id: [a.media_id for a in r.assignments] for r in settled}, {
        i.intent_id for i in remaining
    }


def test_dodgeball_tap_does_not_settle_the_other_events() -> None:
    selections = _selections([INTENTS[2]], {"caption_dodgeball": [TAPPED]})
    settled, remaining = _apply(INTENTS, selections)
    assert settled == {"caption_dodgeball": [TAPPED]}
    assert remaining == {
        "caption_intro_title",
        "caption_football",
        "caption_beach_volleyball",
        "caption_pub",
    }


def test_reworded_replan_of_the_same_intent_keeps_its_tap() -> None:
    selections = _selections([INTENTS[2]], {"caption_dodgeball": [TAPPED]})
    reworded = ClipIntent(
        intent_id="caption_dodgeball",
        op="caption",
        attribute="the clip with the dodgeball game",
        creator_text="Dodgeball",
    )
    settled, remaining = _apply([reworded, INTENTS[1]], selections)
    assert settled == {"caption_dodgeball": [TAPPED]}
    assert remaining == {"caption_football"}


def test_two_taps_for_two_overlapping_intents_stay_separate() -> None:
    selections = _selections(
        [INTENTS[1], INTENTS[2]],
        {"caption_football": [FOOTBALL_CLIP], "caption_dodgeball": [TAPPED]},
    )
    settled, remaining = _apply(INTENTS, selections)
    assert settled == {"caption_football": [FOOTBALL_CLIP], "caption_dodgeball": [TAPPED]}
    assert remaining == {"caption_intro_title", "caption_beach_volleyball", "caption_pub"}


def test_a_tap_matching_no_intent_settles_nothing() -> None:
    selections = _selections([INTENTS[2]], {"caption_dodgeball": [TAPPED]})
    settled, remaining = _apply([INTENTS[1], INTENTS[4]], selections)
    assert settled == {}
    assert remaining == {"caption_football", "caption_pub"}


def test_include_taps_still_stand_in_for_a_reworded_include() -> None:
    asked = ClipIntent(intent_id="include_1", op="include", attribute="the dodgeball game clips")
    selections = _selections([asked], {"include_1": [TAPPED]})
    replanned = ClipIntent(intent_id="inc", op="group", attribute="dodgeball game clips")
    settled, _remaining = _apply([replanned], selections)
    assert settled == {"inc": [TAPPED]}


def test_an_include_tap_never_becomes_an_exclude() -> None:
    asked = ClipIntent(intent_id="include_1", op="include", attribute="the dodgeball game clips")
    selections = _selections([asked], {"include_1": [TAPPED]})
    skip = ClipIntent(intent_id="skip_1", op="exclude", attribute="the dodgeball game clips")
    settled, remaining = _apply([skip], selections)
    assert settled == {}
    assert remaining == {"skip_1"}
