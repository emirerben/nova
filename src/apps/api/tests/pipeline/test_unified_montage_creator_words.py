"""KRI-282: on-screen text in a unified montage comes from the creator's words.

Shaped like a multi-sport tournament montage (synthetic clips and wording): the
creator names the sports, asks for the sport name on each clip, a text for two
chapters, and a stand-in name on shots of people. The plan must not print a
place, landmark or resolver-written sport instead, and the reply must say what
was and was not done.
"""

from __future__ import annotations

from app.kria.brief import BriefRequirement, CreativeBrief
from app.kria.brief_checks import (
    build_receipts,
    plan_facts_from_unified_montage,
    reply_from_receipts,
)
from app.pipeline.unified_montage import BriefView, plan_unified_montage
from app.schemas.clip_intents import chapter_name_caption
from tests.pipeline.test_unified_montage import clip

REQUEST = (
    "We played soccer, then dodgeball, then sand volleyball and went to the pub. Group by sport."
)


def _assign(*ids: str, **extra) -> list[dict]:
    return [{"media_id": i, "confidence": 0.9, **extra} for i in ids]


def _strategy() -> dict:
    return {
        "resolved_clip_intents": [
            {
                "op": "group",
                "status": "resolved",
                "intent_id": "g1",
                "attribute": "soccer",
                "assignments": _assign("c1", "c2", "c7"),
            },
            {
                "op": "group",
                "status": "resolved",
                "intent_id": "g2",
                "attribute": "dodgeball",
                "assignments": [],
            },
            {
                "op": "group",
                "status": "resolved",
                "intent_id": "g3",
                "attribute": "sand volleyball",
                "assignments": _assign("c3", "c4", "c7"),
            },
            {
                "op": "label",
                "status": "resolved",
                "intent_id": "l1",
                "attribute": "sport",
                "assignments": [
                    # A record-derived word the creator never used, on a clip that
                    # is not in any sport group.
                    {"media_id": "c0", "value": "Volleyballs", "grounding": "record_span"},
                    {"media_id": "c1", "value": "soccer", "grounding": "creator_text"},
                    {"media_id": "c2", "value": "football", "grounding": "record_span"},
                    {"media_id": "c3", "value": "sand volleyball", "grounding": "creator_text"},
                ],
            },
            {
                "op": "caption",
                "status": "resolved",
                "intent_id": "cap1",
                "attribute": "the pub",
                "caption_attribute": "The Pub",
                "caption_text": "The Pub",
                "caption_grounding": "creator_text",
                "assignments": _assign("c5", "c6"),
            },
            {
                "op": "label",
                "status": "resolved",
                "intent_id": "ph",
                "attribute": "individual shots of people",
                "placeholder": True,
                "assignments": _assign("c4", value="Name", grounding="placeholder"),
            },
        ]
    }


def _plan():
    clips = [
        clip(0, landmark="Some Common", place="Some Borough, London"),
        clip(1),
        clip(2),
        clip(3),
        clip(4),
        clip(5, place="Some Borough, London"),
        clip(6, place="Some Borough, London"),
        clip(7),
    ]
    return plan_unified_montage(
        clips,
        BriefView(wants_per_clip_text=True),
        strategy=_strategy(),
        clip_intents_enabled=True,
    )


def _texts(plan) -> dict[str, str]:
    return {label.media_id: label.text for label in plan.snapshot.clip_labels or []}


def test_sport_name_is_the_creators_group_name_never_the_record_or_a_place():
    plan = _plan()
    texts = _texts(plan)
    assert texts["c1"] == "soccer"
    assert texts["c2"] == "soccer"  # the resolver's "football" is not printed
    assert texts["c3"] == "sand volleyball"
    assert "c0" not in texts  # in no group: no "Volleyballs", no landmark or place
    assert "c7" not in texts  # in two groups: no guess
    assert all(
        label.provenance == "creator" and not label.inferred for label in plan.snapshot.clip_labels
    )


def test_no_place_or_landmark_text_when_the_creator_chose_the_text():
    texts = _texts(_plan())
    assert not {"Some Common", "Some Borough"} & set(texts.values())
    assert _plan().dropped_label_clip_ids == []


def test_chapter_text_is_the_creators_own_name_for_it_and_the_placeholder_wins():
    texts = _texts(_plan())
    assert texts["c5"] == texts["c6"] == "The Pub"
    assert texts["c4"] == "Name"  # a person shot inside a sport group keeps the stand-in


def test_outcomes_report_each_request_truthfully():
    outcomes = {row["name"]: row for row in _plan().record()["intent_outcomes"]}
    assert outcomes["dodgeball"]["status"] == "not_possible"
    assert outcomes["text for the pub"]["status"] == "met"
    assert outcomes["the sport name on its clips"]["status"] == "met"
    assert outcomes["group by soccer, sand volleyball"]["status"] == "met"


def test_grouping_is_partly_when_a_sport_is_split_in_the_final_order():
    strategy = _strategy()
    strategy["resolved_clip_intents"][0]["assignments"] = _assign("c1", "c5")
    strategy["resolved_clip_intents"][2]["assignments"] = _assign("c3", "c4")
    plan = plan_unified_montage(
        [clip(i) for i in range(8)],
        BriefView(wants_per_clip_text=True),
        strategy=strategy,
        clip_intents_enabled=True,
    )
    split = [r for r in plan.record()["intent_outcomes"] if r["status"] == "partial"]
    assert split and "soccer is in 2 stretches" in split[0]["reason"]


def test_reply_lists_every_request_and_says_what_was_guessed():
    plan = _plan()
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="text",
                scope="per_clip",
                literal="[Name]",
                description="placeholder for individual shots of people",
            )
        ],
    )
    facts = plan_facts_from_unified_montage(plan.record())
    receipts = build_receipts(brief.live(), facts)
    # The printed stand-in is "Name": a bracketed "[Name]" is met, not "Partly".
    assert receipts and receipts[0].status == "met"
    reply = reply_from_receipts(
        brief, receipts, summary="ok", outcomes=plan.record()["intent_outcomes"]
    )
    assert "Couldn't: dodgeball (I found no clips of it)" in reply
    assert "Done: text for the pub" in reply
    assert "Done: placeholder for individual shots of people" in reply
    assert reply.startswith("Not everything you asked for made it in:")


def test_inferred_text_is_explained_in_plain_words():
    strategy = {
        "resolved_clip_intents": [
            {
                "op": "label",
                "status": "resolved",
                "intent_id": "dish",
                "attribute": "dish",
                "assignments": [{"media_id": "c0", "value": "ramen", "grounding": "record_span"}],
            }
        ]
    }
    plan = plan_unified_montage(
        [clip(0), clip(1)],
        BriefView(wants_per_clip_text=True),
        strategy=strategy,
        clip_intents_enabled=True,
    )
    brief = CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(id="r1", kind="text", scope="per_clip", description="name each dish")
        ],
    )
    receipts = build_receipts(brief.live(), plan_facts_from_unified_montage(plan.record()))
    reply = reply_from_receipts(brief, receipts, summary="ok")
    assert "I guessed these, tell me if any is wrong: ramen (text I took from the footage" in reply


def test_chapter_name_caption_requires_the_creators_own_words_for_the_same_chapter():
    assert (
        chapter_name_caption(
            attribute="the pub", caption_attribute="The Pub", creator_request=REQUEST
        )
        == "The Pub"
    )
    # A caption ABOUT something (not the chapter's own name) stays the resolver's job.
    assert (
        chapter_name_caption(
            attribute="the park clips", caption_attribute="the weather", creator_request=REQUEST
        )
        is None
    )
    # The creator never wrote it.
    assert (
        chapter_name_caption(
            attribute="the cafe", caption_attribute="The Cafe", creator_request=REQUEST
        )
        is None
    )
