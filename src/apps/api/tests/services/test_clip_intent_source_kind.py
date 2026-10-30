"""Frozen provider outputs exercise source constraints, not model extraction accuracy."""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.agents._runtime import RunContext
from app.agents.clip_intent_planner import (
    ClipIntentPlannerAgent,
    ClipIntentPlannerInput,
    ClipIntentPlannerOutput,
    PlannedClipIntent,
)
from app.agents.clip_request_resolver import (
    ClipRequestResolverOutput,
    ResolverAssignment,
    ResolverIntentOut,
    ResolverVisionQuestion,
)
from app.schemas.clip_intents import ClipAssignment, ClipIntent, ResolvedClipIntent
from app.services import clip_intent_planning as planning
from app.services.clip_intent_resolution import IntentClip, resolve_clip_intents_for_turn
from app.services.clip_selection import ClipSelections, SelectionEntry, intent_key
from app.services.named_moments import named_moments
from app.services.spoken_quotes import regound_quoted_intents
from tests.services.test_clip_intent_resolution import (
    _patch_resolver,
    _patch_vision_forbidden,
)


def clips():
    return [
        IntentClip(media_id="take", kind="video", analysis={"subject": "building"}),
        IntentClip(media_id="photo", kind="image", analysis={"subject": "building"}),
    ]


def intent(source_kind="video", **kwargs):
    return ClipIntent(
        intent_id="building", op="order", attribute="building", source_kind=source_kind, **kwargs
    )


def test_optional_constraint_keeps_legacy_dump_and_exact_checkout():
    old = ClipIntent(intent_id="building", op="order", attribute="building")
    assert "source_kind" not in old.model_dump()
    assert "source_kind" not in ClipIntent.model_validate(old.model_dump()).model_dump()
    assert Path(planning.__file__).resolve().parents[2] == Path(__file__).resolve().parents[2]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "source_kind,alias,expected",
    [
        ("video", "m002", []),
        ("video", "m001", ["take"]),
        ("image", "m002", ["photo"]),
        (None, "m002", ["photo"]),
    ],
)
async def test_confident_membership_obeys_declared_kind(monkeypatch, source_kind, alias, expected):
    monkeypatch.setattr("app.config.settings.kria_clip_selection_questions_enabled", True)
    _patch_vision_forbidden(monkeypatch)
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="building",
                    assignments=[ResolverAssignment(media=alias, confidence=1.0)],
                )
            ]
        ),
    )
    result = await resolve_clip_intents_for_turn(
        intents=[intent(source_kind)],
        creator_request="Use the building",
        clips=clips(),
        run_context=RunContext(),
    )
    assert result.intents[0].media_ids() == expected
    assert result.intents[0].source_kind == source_kind
    assert result.needs_creator == (not expected)
    if not expected:
        assert result.clip_question is None
        assert "video" in result.question


@pytest.mark.asyncio
@pytest.mark.parametrize("source_kind,alias", [("video", "m002"), ("image", "m001")])
async def test_incompatible_vision_candidates_never_run(monkeypatch, source_kind, alias):
    _patch_vision_forbidden(monkeypatch)
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="building",
                    needs_vision=[
                        ResolverVisionQuestion(media=alias, question="Is the building here?")
                    ],
                )
            ]
        ),
    )
    result = await resolve_clip_intents_for_turn(
        intents=[intent(source_kind)],
        creator_request="Use the building",
        clips=clips(),
        run_context=RunContext(),
    )
    assert result.needs_creator
    assert result.intents[0].assignments == []
    assert result.deferred_queries == []


def wire_planner(monkeypatch, intents):
    agent = MagicMock()
    agent.run.return_value = ClipIntentPlannerOutput(intents=intents)
    monkeypatch.setattr(planning, "ClipIntentPlannerAgent", MagicMock(return_value=agent))
    return agent


def planned(key, kind, *, position=None):
    return PlannedClipIntent(
        intent_id=key,
        op="order",
        attribute=key,
        source_kind=kind,
        source_quote="From this video, use the building, then the photo",
        position=position,
    )


@pytest.mark.asyncio
async def test_creator_conflict_stops_before_resolver_and_preserves_checked_constraint(monkeypatch):
    requested = planned("building", "video")
    wire_planner(monkeypatch, [requested])
    resolver = MagicMock(side_effect=AssertionError("conflicting answer must not be rematched"))
    monkeypatch.setattr(planning, "resolve_clip_intents_for_turn", resolver)
    selections = ClipSelections(
        by_key={intent_key(requested): SelectionEntry(media_ids=("photo",), seq=2)}
    )
    result = await planning.plan_and_resolve_clip_intents(
        creator_request=requested.source_quote,
        latest_user_message=None,
        candidate_intents=None,
        clips=clips(),
        run_context=RunContext(),
        clip_selections=selections,
    )
    assert result.resolution.needs_creator
    assert result.resolution.error_code == "source_kind_conflict"
    assert result.resolution.intents[0].assignments == []
    assert result.inventory["output"]["intents"][0]["source_kind"] == "video"
    assert selections.match(requested).media_ids == ("photo",)
    resolver.assert_not_called()


@pytest.mark.asyncio
async def test_repeated_explicit_photo_and_five_video_moments_reach_real_gate(monkeypatch):
    requests = [planned(f"moment-{i}", "video") for i in range(5)]
    requests += [
        planned("opening-photo", "image", position="first"),
        planned("closing-photo", "image", position="last"),
    ]
    wire_planner(monkeypatch, requests)
    _patch_vision_forbidden(monkeypatch)
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id=i.intent_id,
                    assignments=[
                        ResolverAssignment(
                            media="m001" if i.source_kind == "video" else "m002", confidence=1.0
                        )
                    ],
                )
                for i in requests
            ]
        ),
    )
    result = await planning.plan_and_resolve_clip_intents(
        creator_request=requests[0].source_quote,
        latest_user_message=None,
        candidate_intents=None,
        clips=clips(),
        run_context=RunContext(),
    )
    assert not result.resolution.needs_creator
    assert [i.media_ids() for i in result.resolution.intents] == [["take"]] * 5 + [["photo"]] * 2
    assert [i.source_kind for i in result.resolution.intents] == ["video"] * 5 + ["image"] * 2
    moments = named_moments(
        {
            "edit_format": "montage",
            "resolved_clip_intents": [i.model_dump() for i in result.resolution.intents],
        },
        {
            "clip_assignments": [
                {
                    "media_id": "take",
                    "kind": "video",
                    "duration_s": 244,
                    "storage_generation": "synthetic",
                }
            ]
        },
        moment_count=5,
    )
    assert moments is not None
    assert len(moments.moments) == 5
    assert moments.visual_rows == (5, 6)

    from tests.kria.test_cabildo_named_moment_gate import brief, fixture
    from tests.kria.test_kri723_moment_authority_m2 import gate
    from tests.kria.test_kri775_merope_m2_replay import prod_flags

    prod_flags(monkeypatch)
    strategy, media = fixture()
    strategy["resolved_clip_intents"] = [i.model_dump() for i in result.resolution.intents]
    gated = await gate(
        monkeypatch,
        {"strategy": strategy, "media_snapshot": media, "draft_reply": "Ready"},
        brief=brief(),
    )
    assert gated.plan.mode == "respond"
    assert gated.plan.turn_value == "question"
    assert not gated.plan.intents
    assert len(gated.plan.moment_order_question["moments"]) == 5


@pytest.mark.asyncio
async def test_picker_omits_wrong_kind_and_wrong_kind_suggestions(monkeypatch):
    monkeypatch.setattr("app.config.settings.kria_clip_selection_questions_enabled", True)
    _patch_vision_forbidden(monkeypatch)
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="building",
                    assignments=[ResolverAssignment(media="m002", confidence=0.2)],
                )
            ]
        ),
    )
    available = clips() + [
        IntentClip(media_id="other-take", kind="video", analysis={"subject": "park"})
    ]
    result = await resolve_clip_intents_for_turn(
        intents=[intent()],
        creator_request="Use the building",
        clips=available,
        run_context=RunContext(),
    )
    category = result.clip_question["categories"][0]
    assert category["candidate_media_ids"] == ["take", "other-take"]
    assert category["suggested_media_ids"] == []


@pytest.mark.asyncio
async def test_image_request_never_uses_single_video_fallback(monkeypatch):
    monkeypatch.setattr("app.config.settings.kria_clip_selection_questions_enabled", True)
    _patch_vision_forbidden(monkeypatch)
    _patch_resolver(monkeypatch, ClipRequestResolverOutput(intents=[]))
    result = await resolve_clip_intents_for_turn(
        intents=[intent("image")],
        creator_request="Use the photo",
        clips=clips()[:1],
        run_context=RunContext(),
    )
    assert result.needs_creator
    assert result.intents[0].assignments == []
    assert result.clip_question is None


def test_carried_match_cannot_restore_a_different_source_kind():
    saved = {
        "matches": [
            {
                "intent_id": "building",
                "op": "order",
                "attribute": "building",
                "position": None,
                "assignments": [{"media_id": "photo", "confidence": 1.0}],
            }
        ]
    }
    assert planning.carried_matches(saved, [intent()], clips()) == []
    assert planning.carried_matches(saved, [intent(None)], clips())[0].media_ids() == ["photo"]


@pytest.mark.parametrize("kind", ["video", "image", None])
def test_parser_preserves_optional_kind(kind):
    request = "Put the building first"
    raw = {
        "intents": [
            {
                "intent_id": "building",
                "op": "order",
                "attribute": "building",
                "source_quote": request,
                "source_kind": kind,
                "position": "first",
            }
        ]
    }
    output = ClipIntentPlannerAgent(None).parse(
        json.dumps(raw), ClipIntentPlannerInput(creator_request=request)
    )
    assert output.intents[0].source_kind == kind
    projected = ClipIntent.model_validate(output.intents[0].model_dump(exclude={"source_quote"}))
    assert projected.source_kind == kind


def test_spoken_quote_correction_cannot_change_declared_source_kind():
    available = clips()
    available[0].analysis["transcript"] = "welcome to the square"
    resolved = ResolvedClipIntent(
        intent_id="quote",
        op="order",
        attribute='end when I say "welcome to the square"',
        source_kind="image",
        assignments=[ClipAssignment(media_id="photo", confidence=1.0)],
    )
    result, moved = regound_quoted_intents([resolved], available)
    assert moved == 0
    assert result[0].media_ids() == ["photo"]
    legacy = resolved.model_copy(update={"source_kind": None})
    result, moved = regound_quoted_intents([legacy], available)
    assert moved == 1
    assert result[0].media_ids() == ["take"]


def test_file_name_conflict_is_unresolved():
    from dataclasses import replace

    available = [replace(c, file_name=f"IMG_000{i}.jpg") for i, c in enumerate(clips())]
    requested = intent().model_copy(update={"attribute": "IMG_0001.jpg"})
    settled, remaining, _split = planning.apply_file_name_references(
        [requested], clips=available, creator_request="Use IMG_0001.jpg"
    )
    assert remaining == []
    assert settled[0].status == "needs_creator"
    assert settled[0].assignments == []


def test_explicit_request_correction_accepts_selected_photo():
    requested = intent("image")
    selections = ClipSelections(
        by_key={intent_key(requested): SelectionEntry(media_ids=("photo",), seq=2)}
    )
    settled, remaining, dropped = planning.apply_creator_selections(
        [requested], clips=clips(), selections=selections, creator_request="Use the photo instead"
    )
    assert remaining == []
    assert dropped == set()
    assert settled[0].status == "resolved"
    assert settled[0].media_ids() == ["photo"]


@pytest.mark.asyncio
@pytest.mark.parametrize("alias,expected", [("m002", []), ("m001", ["take"])])
async def test_literal_label_cannot_bind_to_wrong_kind(monkeypatch, alias, expected):
    _patch_vision_forbidden(monkeypatch)
    requested = ClipIntent(
        intent_id="label",
        op="label",
        attribute="building",
        source_kind="video",
        creator_text="Cabildo",
    )
    _patch_resolver(
        monkeypatch,
        ClipRequestResolverOutput(
            intents=[
                ResolverIntentOut(
                    intent_id="label",
                    assignments=[ResolverAssignment(media=alias, value="Cabildo", confidence=1.0)],
                )
            ]
        ),
    )
    result = await resolve_clip_intents_for_turn(
        intents=[requested],
        creator_request='Show "Cabildo" on the video',
        clips=clips(),
        run_context=RunContext(),
    )
    assert result.intents[0].media_ids() == expected
    assert result.needs_creator == (not expected)
    if expected:
        assert result.intents[0].assignments[0].value == "Cabildo"


@pytest.mark.asyncio
@pytest.mark.parametrize("selected,unresolved", [("photo", True), ("take", False)])
async def test_keep_as_written_conversion_keeps_source_constraint(
    monkeypatch, selected, unresolved
):
    text = "The beautiful old building beside the square and the trees at sunset"
    request = f'Label the video "{text}"'
    requested = PlannedClipIntent(
        intent_id="building",
        op="label",
        attribute="building",
        source_kind="video",
        creator_text=text,
        source_quote=request,
    )
    wire_planner(monkeypatch, [requested])
    resolver = MagicMock(
        side_effect=AssertionError("the authoritative selection must settle or conflict")
    )
    monkeypatch.setattr(planning, "resolve_clip_intents_for_turn", resolver)
    selections = ClipSelections(
        by_key={
            intent_key(requested.model_copy(update={"op": "caption"})): SelectionEntry(
                media_ids=(selected,), seq=2
            )
        }
    )
    result = await planning.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message=f'Keep "{text}" exactly as written',
        candidate_intents=None,
        clips=clips(),
        run_context=RunContext(),
        clip_selections=selections,
    )
    assert result.requested_intents[0].op == "caption"
    assert result.requested_intents[0].source_kind == "video"
    assert result.resolution.needs_creator is unresolved
    assert result.resolution.intents[0].media_ids() == ([] if unresolved else ["take"])
    if unresolved:
        assert result.resolution.error_code == "source_kind_conflict"
    resolver.assert_not_called()


def description_caption(key, kind):
    return ClipIntent(
        intent_id=key,
        op="caption",
        attribute="the scenery",
        caption_attribute="describe what happens",
        source_kind=kind,
    )


def test_description_conversion_preserves_distinct_source_scopes():
    requested = [
        description_caption("video-description", "video"),
        description_caption("photo-description", "image"),
    ]
    result = planning.with_clip_descriptions(requested)
    assert [(i.intent_id, i.op, i.source_kind) for i in result] == [
        ("video-description", "label", "video"),
        ("photo-description", "label", "image"),
    ]
    assert all(i.caption_attribute is None for i in result)


def test_global_description_still_survives_alongside_scoped_description():
    legacy = planning.with_clip_descriptions([description_caption("global", None)])
    result = planning.with_clip_descriptions(
        [description_caption("video", "video"), description_caption("global", None)]
    )
    assert result[0].source_kind == "video"
    assert [i.model_dump() for i in result[1:]] == [i.model_dump() for i in legacy]
    assert "source_kind" not in legacy[0].model_dump()


def test_description_conversion_at_capacity_keeps_all_requests():
    from app.schemas.clip_intents import MAX_CLIP_INTENTS

    requested = [
        description_caption(f"description-{i}", "video" if i % 2 else "image")
        for i in range(MAX_CLIP_INTENTS)
    ]
    result = planning.with_clip_descriptions(requested)
    assert len(result) == MAX_CLIP_INTENTS
    assert [(i.intent_id, i.source_kind) for i in result] == [
        (i.intent_id, i.source_kind) for i in requested
    ]
