"""Sanitized Cabildo shape at the existing real named-moment/approval gate.

Recorded shape: five named moments, first incorrectly bound to a photo, final
photo pinned last. Source-corrected variants are explicit synthetic controls.
"""

import copy

import pytest

from app.kria.brief import BriefRequirement, CreativeBrief
from app.services.named_moments import named_moments
from tests.kria.test_kri723_moment_authority_m2 import gate
from tests.kria.test_kri775_merope_m2_replay import prod_flags

TAKE = "take"
PHOTO = "asset-00000000-0000-0000-0000-000000000001"
NAMES = [
    "the Cabildo alone",
    "Maximo pointing",
    "Maximo and Ani",
    "the pink Casa Rosada",
    "the inside of the Cabildo",
]


def fixture(*, wrong_first=False, photo_position="last"):
    rows = [
        {
            "intent_id": f"moment-{i}",
            "op": "order",
            "attribute": words,
            "status": "resolved",
            "assignments": [{"media_id": PHOTO if i == 0 and wrong_first else TAKE}],
        }
        for i, words in enumerate(NAMES)
    ]
    rows.append(
        {
            "intent_id": "photo",
            "op": "order",
            "attribute": "my Cabildo photo",
            "position": photo_position,
            "status": "resolved",
            "assignments": [{"media_id": PHOTO}],
        }
    )
    strategy = {
        "edit_format": "montage",
        "direction": "guided_story",
        "render_program": "guided",
        "audio_strategy": "original_audio",
        "media_scope": "all",
        "selected_media_ids": [],
        "target_duration_s": 15,
        "resolved_clip_intents": rows,
    }
    media = {
        "clip_assignments": [
            {
                "media_id": TAKE,
                "kind": "video",
                "duration_s": 244,
                "storage_generation": "g1",
                "gcs_path": "synthetic/take.mp4",
                "analysis": {
                    "understanding": {"kind": "video", "summary": "Buildings in a square"}
                },
            }
        ]
    }
    return strategy, media


def brief():
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="moments",
                kind="select",
                scope="global",
                description="five named moments",
                facts={"moment_count": 5, "moment_length_s": 2.5},
            )
        ],
    )


@pytest.mark.parametrize("position", ["first", "last"])
def test_photo_pin_does_not_bypass_video_moment_location(position):
    strategy, media = fixture(photo_position=position)
    moments = named_moments(strategy, media, moment_count=5)
    assert moments is not None
    assert len(moments.moments) == 5 and moments.visual_rows == (5,)


@pytest.mark.asyncio
@pytest.mark.parametrize("wrong_first", [False, True])
async def test_unlocated_or_inconsistent_moments_never_reach_approval(monkeypatch, wrong_first):
    prod_flags(monkeypatch)
    strategy, media = fixture(wrong_first=wrong_first)
    result = await gate(
        monkeypatch,
        {
            "strategy": strategy,
            "media_snapshot": media,
            "draft_reply": "I'll make the requested sequence.",
        },
        brief=brief(),
    )
    assert result.plan.mode == "respond" and result.plan.turn_value == "question"
    assert not result.plan.intents
    if wrong_first:
        assert result.plan.moment_order_question is None
        assert "5" in result.plan.response and "4" in result.plan.response
    else:
        assert len(result.plan.moment_order_question["moments"]) == 5


def test_ordinary_video_first_last_pin_is_not_a_named_moment_list():
    strategy, media = fixture()
    strategy["resolved_clip_intents"] = [copy.deepcopy(strategy["resolved_clip_intents"][0])]
    strategy["resolved_clip_intents"][0]["position"] = "first"
    assert named_moments(strategy, media, moment_count=None) is None


def captured_brief():
    return CreativeBrief(
        version=1,
        requirements=[
            BriefRequirement(
                id="r1",
                kind="timing",
                scope="global",
                description="about 15 seconds, 2-3 seconds per moment",
                facts={"duration_s": 15},
            ),
            BriefRequirement(
                id="r2",
                kind="order",
                scope="global",
                description=(
                "the Cabildo on its own, Maximo pointing at it, Maximo and Ani standing "
                "together, the pink Casa Rosada, the inside of the Cabildo, then the "
                "Cabildo photo at the end"
            ),
                facts={"key": "any_order", "last_clip": "my Cabildo photo"},
            ),
        ],
    )


def test_actual_captured_brief_still_reaches_location_question_without_inventing_count(monkeypatch):
    from app.kria.planner import _moment_decision
    from app.kria.style_asks import brief_moment_ask

    prod_flags(monkeypatch)
    strategy, media = fixture(wrong_first=True)
    ask = brief_moment_ask(captured_brief())
    assert ask is None or ask.count is None
    decision = _moment_decision(strategy, media, captured_brief(), [], can_ask=True)
    assert decision.question is not None
    assert len(decision.question["moments"]) == 4
    assert all(row["words"] != NAMES[0] for row in decision.question["moments"])
    assert strategy["resolved_clip_intents"][0]["assignments"][0]["media_id"] == PHOTO


@pytest.mark.asyncio
async def test_unsupported_client_keeps_legacy_count_mismatch_path(monkeypatch):
    prod_flags(monkeypatch)
    strategy, media = fixture(wrong_first=True)
    result = await gate(
        monkeypatch,
        {"strategy": strategy, "media_snapshot": media, "draft_reply": "A draft."},
        brief=brief(),
        answers=False,
    )
    assert result.plan.mode == "act"
    assert result.plan.moment_order_question is None


@pytest.mark.asyncio
async def test_captured_brief_and_wrong_first_source_do_not_approve_filler(monkeypatch):
    prod_flags(monkeypatch)
    strategy, media = fixture(wrong_first=True)
    result = await gate(
        monkeypatch,
        {
            "strategy": strategy,
            "media_snapshot": media,
            "draft_reply": "I'll pull those five moments.",
        },
        brief=captured_brief(),
    )
    assert result.plan.mode == "respond"
    assert result.plan.turn_value == "question"
    assert not result.plan.intents
    assert len(result.plan.moment_order_question["moments"]) == 4
