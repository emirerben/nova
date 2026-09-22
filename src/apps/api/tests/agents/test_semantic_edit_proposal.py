import json
from pathlib import Path

import pytest

from app.agents._runtime import SchemaError
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
)
from app.agents.semantic_edit_proposal import (
    SemanticEditProposalAgent,
    semantic_plan_from_legacy,
)
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent


def _input(**updates: object) -> EditProposalAgentInput:
    values: dict[str, object] = {
        "direction": "guided_story",
        "pace": "balanced",
        "target_duration_s": 24,
        "media_scope": "all",
        "selected_media_ids": ["park", "pub"],
        "creator_request": "Group the pub clips and say “post match pub”.",
        "opening_title": "London day",
        "media": [
            EditProposalMedia(
                media_id="park",
                lane="clip",
                kind="video",
                summary="walk in the park",
                best_moments=[{"start_s": 1.0}],
            ),
            EditProposalMedia(
                media_id="pub",
                lane="clip",
                kind="video",
                setting="pub",
                activity="talking",
                best_moments=[{"start_s": 2.0}],
            ),
        ],
    }
    values.update(updates)
    return EditProposalAgentInput(**values)  # type: ignore[arg-type]


def _raw(**updates: object) -> str:
    payload: dict[str, object] = {
        "title": "invented title",
        "chapters": [
            {
                "chapter_id": "one",
                "topic": "Park",
                "thought": "A model caption",
                "role": "hook",
                "weight": 1,
                "layout": "fullscreen",
                "sources": [{"media_id": "m001", "candidate_index": 0, "weight": 1}],
            },
            {
                "chapter_id": "two",
                "topic": "Pub",
                "thought": "POST MATCH PUB",
                "role": "payoff",
                "weight": 2,
                "layout": "fullscreen",
                "sources": [{"media_id": "m002", "candidate_index": 0, "weight": 1}],
            },
        ],
    }
    payload.update(updates)
    return json.dumps(payload)


def test_semantic_parse_resolves_aliases_keeps_creator_caption_and_restores_audio() -> None:
    input = _input(montage_audio={"source_media_ids": ["pub"], "preserve_source_audio": True})
    plan = SemanticEditProposalAgent(None).parse(_raw(), input)  # type: ignore[arg-type]
    assert plan.title == "London day"
    assert [source.media_id for chapter in plan.chapters for source in chapter.sources] == [
        "park",
        "pub",
    ]
    assert [chapter.thought for chapter in plan.chapters] == ["", "post match pub"]
    assert plan.montage_audio == input.montage_audio


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"duration_s": 24}, "timeline fields"),
        (
            {
                "chapters": [
                    {
                        "chapter_id": "one",
                        "topic": "x",
                        "thought": "",
                        "role": "hook",
                        "weight": 1,
                        "sources": [{"media_id": "unknown"}],
                    }
                ]
            },
            "unknown media",
        ),
    ],
)
def test_semantic_parse_rejects_scheduler_fields_and_ungrounded_sources(
    payload: dict, message: str
) -> None:
    raw = json.loads(_raw())
    raw.update(payload)
    with pytest.raises(SchemaError, match=message):
        SemanticEditProposalAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]


def test_creator_labels_are_written_back_exactly() -> None:
    input = _input(creator_request="", shot_labels=["Park Walk", "Pub Talk"])
    plan = SemanticEditProposalAgent(None).parse(_raw(), input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["Park Walk", "Pub Talk"]


def test_prompt_rewrites_server_constraints_to_short_aliases() -> None:
    input = _input(
        montage_audio={"source_media_ids": ["pub"], "preserve_source_audio": True},
        clip_intents=[
            ResolvedClipIntent(
                intent_id="pub",
                op="include",
                attribute="pub",
                assignments=[ClipAssignment(media_id="pub")],
            )
        ],
    )
    prompt = SemanticEditProposalAgent(None).render_prompt(input)  # type: ignore[arg-type]
    assert '"required_media_ids": ["m001", "m002"]'.replace(" ", "") in prompt.replace(" ", "")
    assert '"source_media_ids": ["m002"]'.replace(" ", "") in prompt.replace(" ", "")
    assert '"media_id": "m002"' in prompt
    assert '"media_id": "pub"' not in prompt


def test_fast_caption_binding_resolves_aliases_to_scheduled_media() -> None:
    intents = [
        ResolvedClipIntent(
            intent_id="pub",
            op="group",
            attribute="pub clips",
            creator_text="post match pub",
            assignments=[ClipAssignment(media_id="pub")],
        ),
        ResolvedClipIntent(
            intent_id="sport",
            op="label",
            attribute="sport",
            assignments=[ClipAssignment(media_id="park", value="Running")],
        ),
    ]
    input = _input(direction="fast_montage", clip_intents=intents)
    raw = json.loads(_raw(text_bindings=[{"text": "post match pub", "media_ids": ["m002"]}]))
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert {binding.text for binding in plan.text_bindings} == {"post match pub"}
    assert all(set(binding.media_ids) <= {"park", "pub"} for binding in plan.text_bindings)


def test_resolved_label_cannot_enter_generic_text_binding() -> None:
    input = _input(
        creator_request="",
        clip_intents=[
            ResolvedClipIntent(
                intent_id="sport",
                op="label",
                attribute="sport",
                assignments=[ClipAssignment(media_id="park", value="Running")],
            )
        ],
    )
    raw = json.loads(_raw(text_bindings=[{"text": "Running", "media_ids": ["m001"]}]))
    with pytest.raises(SchemaError, match="grounded label lane"):
        SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]


def _exact_creator_label_input() -> EditProposalAgentInput:
    return EditProposalAgentInput(
        direction="fast_montage",
        pace="fast",
        target_duration_s=4,
        media_scope="all",
        creator_request='Label the clip "Volleyball".',
        media=[
            EditProposalMedia(
                media_id="volleyball",
                lane="clip",
                kind="video",
                duration_s=4,
                summary="people playing volleyball",
                best_moments=[{"start_s": 0, "end_s": 4}],
            )
        ],
        clip_intents=[
            ResolvedClipIntent(
                intent_id="sport",
                op="label",
                attribute="sport",
                creator_text="Volleyball",
                assignments=[
                    ClipAssignment(
                        media_id="volleyball",
                        value="Volleyball",
                        confidence=1,
                        grounding="creator_text",
                    )
                ],
            )
        ],
    )


def _exact_creator_label_raw(*, thought: str = "", text_bindings: list[dict] | None = None) -> str:
    return json.dumps(
        {
            "title": "Sport",
            "chapters": [
                {
                    "chapter_id": "one",
                    "topic": "Sport",
                    "thought": thought,
                    "role": "hook",
                    "weight": 1,
                    "layout": "fullscreen",
                    "sources": [{"media_id": "m001", "candidate_index": 0, "weight": 1}],
                }
            ],
            "text_bindings": text_bindings or [],
        }
    )


def test_exact_creator_label_succeeds_without_generic_text() -> None:
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        _exact_creator_label_raw(), _exact_creator_label_input()
    )
    assert [chapter.thought for chapter in plan.chapters] == [""]
    assert plan.text_bindings == []


def test_model_label_thought_is_blank_and_does_not_bypass_grounded_lane() -> None:
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        _exact_creator_label_raw(thought="Volleyball"), _exact_creator_label_input()
    )
    assert [chapter.thought for chapter in plan.chapters] == [""]
    assert plan.text_bindings == []


def test_semantic_prompt_aliases_every_input_media_in_stable_order() -> None:
    media = [
        EditProposalMedia(media_id=f"clip-{index:03d}", lane="clip", kind="video", summary="x")
        for index in range(1, 35)
    ]
    input = _input(media_scope="all", selected_media_ids=None, media=media)
    prompt = SemanticEditProposalAgent(None).render_prompt(input)  # type: ignore[arg-type]
    assert '"media_id": "m001"' in prompt
    assert '"media_id": "m034"' in prompt
    assert "clip-034" not in prompt


def test_clip_intent_group_and_order_require_every_source_in_exact_sequence() -> None:
    intents = [
        ResolvedClipIntent(
            intent_id="first",
            op="order",
            position="first",
            attribute="park first",
            assignments=[ClipAssignment(media_id="park")],
        ),
        ResolvedClipIntent(
            intent_id="pub",
            op="group",
            attribute="pub together",
            assignments=[ClipAssignment(media_id="pub")],
        ),
    ]
    bad = json.loads(_raw())
    bad["chapters"][0]["sources"] = [{"media_id": "m002"}, {"media_id": "m001"}]
    bad["chapters"][1]["sources"] = [{"media_id": "m002"}]
    with pytest.raises(SchemaError, match="order changed"):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(bad), _input(creator_request="", clip_intents=intents)
        )


@pytest.mark.parametrize(
    "fixture_path",
    sorted(
        Path(__file__)
        .parents[1]
        .joinpath("fixtures", "agent_evals", "edit_proposal", "golden")
        .glob("*.json")
    ),
    ids=lambda path: path.stem,
)
def test_legacy_goldens_project_to_timing_free_semantic_intent(fixture_path: Path) -> None:
    """Keep every existing proposal fixture useful during the scheduler migration."""
    fixture = json.loads(fixture_path.read_text())
    input = EditProposalAgentInput.model_validate(fixture["input"])
    output = EditProposalAgent(None).parse(fixture["raw_text"], input)  # type: ignore[arg-type]
    plan = semantic_plan_from_legacy(output, input)
    assert plan.chapters
    assert all(source.weight > 0 for chapter in plan.chapters for source in chapter.sources)
    assert not any(
        "duration" in field for chapter in plan.chapters for field in chapter.model_dump()
    )
