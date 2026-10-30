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
                "gcs_path": "synthetic/analysis-proxy-take.mp4",
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


@pytest.mark.asyncio
@pytest.mark.parametrize("position", ["first", "last"])
async def test_answered_moments_keep_their_windows_and_photo_pin(monkeypatch, position):
    from dataclasses import replace

    import app.pipeline.unified_montage as unified
    from tests.kria import test_kri775_merope_m2_replay as worker_kit
    from tests.kria.test_kri723_moment_authority_m2 import answer_for, answered_events

    prod_flags(monkeypatch)
    strategy, media = fixture(photo_position=position)
    requested_brief = brief()
    requested_brief.requirements.append(
        BriefRequirement(
            id="photo-pin",
            kind="order",
            scope="global",
            description=f"Put my Cabildo photo {position}",
            facts={f"{position}_clip": "my Cabildo photo"},
        )
    )
    render = {
        "strategy": strategy,
        "media_snapshot": media,
        "draft_reply": "The requested sequence.",
    }
    asked = await gate(monkeypatch, render, brief=requested_brief)
    question = asked.plan.moment_order_question
    assert question is not None
    windows = {name: (10.0 + i * 20, 12.0 + i * 20) for i, name in enumerate(NAMES)}
    answer = answer_for(question, pointed=windows)
    result = await gate(
        monkeypatch, render, brief=requested_brief, events=answered_events(question, answer)
    )
    assert result.plan.mode == "act", result.plan.response
    assert result.plan.moment_order_question is None
    saved_media = result.media_snapshot
    assert [
        (row["source_start_s"], row["source_end_s"])
        for row in saved_media["length_plan"]["windows"]
    ] == list(windows.values())
    assert {row["origin"] for row in saved_media["length_plan"]["moments"]} == {"creator_answer"}
    saved_strategy = result.plan.intents[0].arguments["strategy"]
    original = copy.deepcopy(
        worker_kit.RENDERS[0]["media_snapshot"]["clip_assignments"][0]["upload_contract"]
    )
    original["proxy"]["original"]["duration_s"] = 244
    saved_media["clip_assignments"][0]["upload_contract"] = original
    visual = replace(
        worker_kit.m2_visual(), media_id=PHOTO.removeprefix("asset-"), manifest_id=PHOTO
    )
    monkeypatch.setattr(worker_kit, "m2_visual", lambda: visual)
    observed = {}
    build = unified.plan_unified_montage

    def capture(*args, **kwargs):
        observed["plan"] = build(*args, **kwargs)
        return observed["plan"]

    monkeypatch.setattr(unified, "plan_unified_montage", capture)
    outcome = worker_kit.run_worker(
        monkeypatch, render, brief=requested_brief, media=saved_media, strategy=saved_strategy
    )
    assert outcome.refusal is None, outcome.refusal
    cuts = [
        (cut.source_start_s, cut.source_end_s) if cut.media_id == TAKE else "PHOTO"
        for cut in observed["plan"].snapshot.fast_cuts
    ]
    expected = ["PHOTO", *windows.values()] if position == "first" else [*windows.values(), "PHOTO"]
    assert cuts == expected
    order_rows = [
        row for row in observed["plan"].record()["intent_outcomes"] if row.get("op") == "order"
    ]
    assert order_rows and all(row["status"] == "met" for row in order_rows), order_rows


def test_explicit_visual_position_conflicting_with_named_anchor_is_unresolved():
    from app.pipeline.unified_montage import _moment_outcomes
    from app.services.clip_order_sequence import moment_seats, sequence_rows

    strategy, _media = fixture(photo_position="first")
    strategy["resolved_clip_intents"][-1]["attribute"] = "the photo right after Maximo pointing"
    seats = moment_seats(
        sequence_rows(strategy["resolved_clip_intents"]), windows={TAKE: 5}, visuals={PHOTO}
    )
    assert seats is not None
    assert 5 in seats.conflicted
    assert PHOTO not in seats.after
    outcomes = _moment_outcomes(seats, [(TAKE, float(i)) for i in range(5)] + [(PHOTO, 0)])
    assert outcomes[5]["status"] != "met"


def test_unanchored_visual_listed_before_first_is_not_implicitly_pinned():
    from app.services.clip_order_sequence import moment_seats, sequence_rows

    strategy, _media = fixture()
    photo = strategy["resolved_clip_intents"].pop()
    photo.pop("position")
    strategy["resolved_clip_intents"].insert(0, photo)
    seats = moment_seats(
        sequence_rows(strategy["resolved_clip_intents"]), windows={TAKE: 5}, visuals={PHOTO}
    )
    assert seats is not None and PHOTO not in seats.after


def test_positional_take_row_still_uses_legacy_group_seating():
    from app.services.clip_order_sequence import moment_seats, sequence_rows

    strategy, _media = fixture()
    strategy["resolved_clip_intents"][0]["position"] = "first"
    assert (
        moment_seats(
            sequence_rows(strategy["resolved_clip_intents"]), windows={TAKE: 5}, visuals={PHOTO}
        )
        is None
    )


@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize("corruption", ["misplaced", "unlocated"])
def test_photo_pin_receipt_rejects_wrong_placement_or_missing_moment_evidence(position, corruption):
    from app.pipeline.unified_montage import _moment_outcomes
    from app.services.clip_order_sequence import moment_seats, sequence_rows

    strategy, _media = fixture(photo_position=position)
    seats = moment_seats(
        sequence_rows(strategy["resolved_clip_intents"]), windows={TAKE: 5}, visuals={PHOTO}
    )
    assert seats is not None
    spans = [(TAKE, float(i * 10), float(i * 10 + 2)) for i in range(5)]
    record = [
        {
            "window_index": i,
            "origin": "creator_answer",
            "source_start_s": start,
            "source_end_s": end,
        }
        for i, (_ref, start, end) in enumerate(spans)
    ]
    photo = (PHOTO, 0.0, 1.0)
    actual = [photo, *spans] if position == "first" else [*spans, photo]
    if corruption == "misplaced":
        actual = [*spans, photo] if position == "first" else [photo, *spans]
    outcomes = _moment_outcomes(
        seats,
        [(ref, start) for ref, start, _end in actual],
        spans=actual,
        record=None if corruption == "unlocated" else record,
    )
    assert outcomes[5]["status"] != "met"


@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize("unrelated_outside", [False, True])
def test_explicit_photo_receipt_requires_absolute_boundary(position, unrelated_outside):
    from app.pipeline.unified_montage import _moment_outcomes
    from app.services.clip_order_sequence import moment_seats

    rows = [
        (None, "first moment", [TAKE], "resolved"),
        (None, "second moment", [TAKE], "resolved"),
        (position, "the photo", [PHOTO], "resolved"),
    ]
    seats = moment_seats(rows, windows={TAKE: 2}, visuals={PHOTO, "other"})
    assert seats is not None
    take_spans = [(TAKE, 0.0, 1.0), (TAKE, 2.0, 3.0)]
    photo, other = (PHOTO, 0.0, 1.0), ("other", 0.0, 1.0)
    if position == "first":
        actual = [other, photo, *take_spans] if unrelated_outside else [photo, other, *take_spans]
    else:
        actual = [*take_spans, photo, other] if unrelated_outside else [*take_spans, other, photo]
    record = [
        {
            "window_index": i,
            "origin": "creator_answer",
            "source_start_s": start,
            "source_end_s": end,
        }
        for i, (_ref, start, end) in enumerate(take_spans)
    ]
    outcomes = _moment_outcomes(
        seats, [(ref, start) for ref, start, _end in actual], spans=actual, record=record
    )
    assert (outcomes[2]["status"] == "met") == (not unrelated_outside)


@pytest.mark.parametrize("position", ["first", "last"])
def test_explicit_photo_seating_surrounds_unrelated_visuals(position):
    from dataclasses import replace

    from app.pipeline.unified_montage import _seat_among_moments
    from app.services.clip_order_sequence import moment_seats
    from tests.pipeline.test_unified_montage import clip

    rows = [
        (None, "first moment", [TAKE], "resolved"),
        (None, "second moment", [TAKE], "resolved"),
        (position, "the photo", [PHOTO], "resolved"),
    ]
    seats = moment_seats(rows, windows={TAKE: 2}, visuals={PHOTO, "other"})
    assert seats is not None
    take = replace(clip(0), media_id=TAKE)
    photo = replace(clip(1), media_id=PHOTO)
    other = replace(clip(2), media_id="other")
    slots = [(0, take, 30, None), (1, photo, 30, None), (2, take, 30, None), (3, other, 30, None)]
    placed = _seat_among_moments(slots, seats)
    refs = [slot[1].ref_id for slot in placed]
    assert refs[0 if position == "first" else -1] == PHOTO
    assert len(placed) == len(slots)


@pytest.mark.parametrize("position", ["first", "last"])
@pytest.mark.parametrize("reversed_groups", [False, True])
def test_multiple_explicit_boundary_groups_keep_row_order(position, reversed_groups):
    from app.pipeline.unified_montage import _moment_outcomes
    from app.services.clip_order_sequence import moment_seats

    rows = [
        (None, "first moment", [TAKE], "resolved"),
        (None, "second moment", [TAKE], "resolved"),
        (position, "photo P", [PHOTO], "resolved"),
        (position, "photo Q", ["other"], "resolved"),
    ]
    seats = moment_seats(rows, windows={TAKE: 2}, visuals={PHOTO, "other"})
    assert seats is not None
    takes = [(TAKE, 0.0, 1.0), (TAKE, 2.0, 3.0)]
    group = [(PHOTO, 0.0, 1.0), ("other", 0.0, 1.0)]
    if reversed_groups:
        group.reverse()
    actual = [*group, *takes] if position == "first" else [*takes, *group]
    record = [
        {
            "window_index": i,
            "origin": "creator_answer",
            "source_start_s": start,
            "source_end_s": end,
        }
        for i, (_ref, start, end) in enumerate(takes)
    ]
    outcomes = _moment_outcomes(
        seats, [(ref, start) for ref, start, _end in actual], spans=actual, record=record
    )
    assert all((outcomes[i]["status"] == "met") == (not reversed_groups) for i in [2, 3])
