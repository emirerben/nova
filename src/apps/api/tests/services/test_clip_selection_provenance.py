"""Question-scoped provenance survives authoritative selection folding."""

import pytest

from app.schemas.clip_intents import ClipIntent
from app.services.clip_selection import fold_clip_selections, intent_key


@pytest.mark.parametrize("repeat", [1, 2])
@pytest.mark.parametrize("question_id", ["asked", "unrelated"])
def test_none_provenance_belongs_to_the_answered_question(repeat, question_id):
    intent = ClipIntent(intent_id="inside_cabildo", op="order", attribute="inside the Cabildo")
    key = intent_key(intent)
    events = [
        (
            "assistant",
            {
                "clip_question": {
                    "question_id": "asked",
                    "categories": [
                        {
                            "key": key,
                            "op": intent.op,
                            "intent_id": intent.intent_id,
                            "candidate_media_ids": ["take"],
                        }
                    ],
                }
            },
        )
    ]
    events += [
        (
            "user",
            {
                "clip_selection": {
                    "question_id": question_id,
                    "none_keys": [key],
                }
            },
        )
    ] * repeat
    entry = fold_clip_selections(events).match(intent)
    if question_id == "unrelated":
        assert entry is None
    else:
        assert entry.none
        assert entry.question_id == "asked"
        assert entry.category_key == key
        assert entry.seq == repeat


@pytest.mark.asyncio
async def test_real_planner_normalization_carries_none_to_checked_story_reconciliation(monkeypatch):
    from app.agents._runtime import RunContext
    from app.schemas.story_intents import StoryIntentRefs, StoryRowRef
    from app.services import clip_intent_planning as planning
    from app.services.clip_intent_resolution import IntentClip
    from app.services.story_intent_reconciliation import (
        inventory_revision,
        reconcile_story_intents,
        story_revision,
    )
    from tests.agents.conftest import MockModelClient

    request = (
        "Use the inside of the Cabildo, then the palace. "
        'Caption the final photo "inside the Cabildo".'
    )
    rows = [
        {
            "intent_id": "inside_cabildo",
            "op": "order",
            "attribute": "inside the Cabildo",
            "source_kind": "video",
            "source_quote": "inside of the Cabildo",
        },
        {
            "intent_id": "palace_scene",
            "op": "order",
            "attribute": "the palace",
            "source_kind": "video",
            "source_quote": "the palace",
        },
        {
            "intent_id": "photo_caption",
            "op": "caption",
            "attribute": "the final photo",
            "source_kind": "image",
            "creator_text": "inside the Cabildo",
            "source_quote": 'Caption the final photo "inside the Cabildo"',
        },
    ]
    client = MockModelClient()
    client.queue("gemini-2.5-flash", {"intents": rows})
    monkeypatch.setattr(planning, "default_client", lambda: client)
    categories = [
        {
            "key": intent_key(ClipIntent.model_validate(row)),
            "op": row["op"],
            "intent_id": row["intent_id"],
        }
        for row in rows
    ]
    events = [
        ("assistant", {"clip_question": {"question_id": "checked", "categories": categories}}),
        (
            "user",
            {
                "clip_selection": {
                    "question_id": "checked",
                    "none_keys": [categories[0]["key"]],
                    "answers": [
                        {"key": categories[1]["key"], "media_ids": ["take"]},
                        {"key": categories[2]["key"], "media_ids": ["photo"]},
                    ],
                }
            },
        ),
    ]
    result = await planning.plan_and_resolve_clip_intents(
        creator_request=request,
        latest_user_message=None,
        candidate_intents=None,
        clips=[
            IntentClip(media_id="take", kind="video", analysis={}),
            IntentClip(media_id="photo", kind="image", analysis={}),
        ],
        clip_selections=fold_clip_selections(events),
        run_context=RunContext(),
    )
    assert len(client.invocations) == 1
    assert [r.intent.intent_id for r in result.rejected_selections] == ["inside_cabildo"]
    assert result.rejected_selections[0].question_id == "checked"
    assert {i.intent_id for i in result.requested_intents} == {"palace_scene", "photo_caption"}
    story = ["Show the interior", "Show the palace", "inside the Cabildo"]
    refs = StoryIntentRefs(
        story_revision(story),
        inventory_revision(result.checked_intents),
        (
            StoryRowRef(0, ("inside_cabildo",)),
            StoryRowRef(1, ("palace_scene",)),
            StoryRowRef(2, ("photo_caption",), "literal"),
        ),
    )
    reconciled = reconcile_story_intents(
        story,
        refs=refs,
        checked_intents=result.checked_intents,
        rejected_intents=[row.intent for row in result.rejected_selections],
    )
    assert reconciled.status == "reconciled"
    assert reconciled.rows == ("Show the palace", "inside the Cabildo")
    assert (
        next(i for i in result.resolution.intents if i.intent_id == "photo_caption").caption_text
        == "inside the Cabildo"
    )


@pytest.mark.parametrize("mapped", [False, True])
def test_strategy_adapter_keeps_unresolved_prose_and_creator_copy_without_a_stale_promise(mapped):
    from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
    from app.kria.planner import adapt_creator_action
    from app.schemas.story_intents import RejectedClipSelection, StoryIntentRefs, StoryRowRef
    from app.services.story_intent_reconciliation import inventory_revision, story_revision

    interior = ClipIntent(intent_id="inside_cabildo", op="order", attribute="inside the Cabildo")
    caption = ClipIntent(
        intent_id="literal_photo",
        op="caption",
        attribute="final photo",
        creator_text="inside the Cabildo",
    )
    story = ["Show the interior", "inside the Cabildo"]
    refs = (
        StoryIntentRefs(
            story_revision(story),
            inventory_revision([interior, caption]),
            (
                StoryRowRef(0, (interior.intent_id,)),
                StoryRowRef(1, (caption.intent_id,), "literal"),
            ),
        )
        if mapped
        else None
    )
    action = ProposeStrategy(
        kind="propose_strategy",
        summary="All the requested scenes will appear.",
        strategy=CreativeStrategy(
            story_structure=story,
            clip_intents=[interior, caption],
            opening_title="inside the Cabildo",
            rationale="All the requested scenes will appear.",
        ),
    )
    plan = adapt_creator_action(
        action,
        server_clip_intents=[caption],
        server_resolved_clip_intents=[],
        server_rejected_selections=[
            RejectedClipSelection(interior, "checked", intent_key(interior), 1)
        ],
        server_checked_intents=[interior, caption],
        server_story_refs=refs,
    )
    arguments = plan.intents[0].arguments
    saved = CreativeStrategy.model_validate(arguments["strategy"])
    assert "All the requested scenes will appear." not in arguments["summary"]
    assert "All the requested scenes will appear." not in saved.rationale
    assert saved.opening_title == "inside the Cabildo"
    assert saved.clip_intents[0].creator_text == "inside the Cabildo"
    assert saved.selection_reconciliation.rejected_selections[0].question_id == "checked"
    assert saved.story_structure == (["inside the Cabildo"] if mapped else [])
    assert saved.selection_reconciliation.unresolved_story_rows == (() if mapped else tuple(story))
    assert saved.selection_reconciliation.status == ("reconciled" if mapped else "unresolved")
    if not mapped:
        assert "saved for review" in arguments["summary"]


def test_model_cannot_author_omission_provenance():
    from app.agents._schemas.creator_agent import CreativeStrategy, ProposeStrategy
    from app.kria.planner import adapt_creator_action
    from app.schemas.story_intents import SelectionReconciliation

    strategy = CreativeStrategy(selection_reconciliation=SelectionReconciliation((), "unresolved"))
    plan = adapt_creator_action(
        ProposeStrategy(kind="propose_strategy", strategy=strategy, summary="Draft")
    )
    assert "selection_reconciliation" not in plan.intents[0].arguments["strategy"]


def test_main_creator_and_legacy_route_clear_model_omission_metadata():
    import json

    from app.agents._schemas.creator_agent import CreativeStrategy
    from app.agents.main_creator import MainCreatorAgent
    from app.routes.creator_agent import _model_strategy_hygiene
    from app.schemas.story_intents import SelectionReconciliation
    from tests.kria.test_kri950_tap_keeps_checked_list import _agent_input

    strategy = CreativeStrategy(selection_reconciliation=SelectionReconciliation((), "unresolved"))
    assert _model_strategy_hygiene(strategy).selection_reconciliation is None
    output = MainCreatorAgent(model_client=None).parse(
        json.dumps(
            {
                "action": {
                    "kind": "propose_strategy",
                    "strategy": {
                        **strategy.model_dump(mode="json"),
                        "target_duration_s": 15,
                        "rationale": "Fifteen seconds keeps the selected moments concise.",
                    },
                    "summary": "Draft",
                },
                "brief_updates": [],
            }
        ),
        _agent_input("Make a simple montage"),
    )
    assert output.action.strategy.selection_reconciliation is None
