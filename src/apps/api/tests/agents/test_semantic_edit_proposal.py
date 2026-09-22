import json
from dataclasses import replace
from pathlib import Path

import pytest

from app.agents._runtime import ModelInvocation, SchemaError
from app.agents.edit_proposal import (
    EditProposalAgent,
    EditProposalAgentInput,
    EditProposalMedia,
)
from app.agents.semantic_edit_proposal import (
    _GROUP_RETRY_HINT,
    SemanticEditProposalAgent,
    semantic_plan_from_legacy,
)
from app.schemas.clip_intents import ClipAssignment, ResolvedClipIntent
from app.services.semantic_edit_scheduler import schedule_semantic_edit


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


def test_semantic_parse_drops_unavailable_default_candidate() -> None:
    input = _input(
        creator_request="",
        media_scope="selected",
        selected_media_ids=["park"],
        media=[
            EditProposalMedia(media_id="park", lane="clip", kind="video", summary="walk"),
        ],
    )
    raw = json.dumps(
        {
            "title": "Walk",
            "chapters": [
                {
                    "chapter_id": "one",
                    "topic": "Park",
                    "thought": "",
                    "role": "hook",
                    "weight": 1,
                    "layout": "fullscreen",
                    "sources": [{"media_id": "m001", "candidate_index": 0}],
                }
            ],
        }
    )
    plan = SemanticEditProposalAgent(None).parse(raw, input)  # type: ignore[arg-type]
    assert plan.chapters[0].sources[0].candidate_index is None
    assert plan.repairs == ["dropped_unavailable_candidate:0:0"]


def test_semantic_parse_drops_empty_title_only_chapter() -> None:
    raw = json.loads(_raw())
    raw["chapters"].insert(
        0,
        {
            "chapter_id": "server-title",
            "topic": "Title",
            "thought": "",
            "role": "hook",
            "weight": 1,
            "layout": "fullscreen",
            "sources": [],
        },
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]
    assert [chapter.chapter_id for chapter in plan.chapters] == ["one", "two"]
    assert "dropped_empty_chapter:0" in plan.repairs


def test_semantic_parse_drops_empty_server_closing_title_chapter() -> None:
    raw = json.loads(_raw())
    raw["chapters"][0] = {
        "chapter_id": "server-closing-title",
        "topic": "Closing title",
        "thought": "Wrap",
        "role": "hook",
        "weight": 1,
        "layout": "fullscreen",
        "sources": [],
    }
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        _input(
            creator_request="",
            closing_title="Wrap",
            media_scope="selected",
            selected_media_ids=["pub"],
        ),
    )
    assert [chapter.chapter_id for chapter in plan.chapters] == ["two"]
    assert "dropped_empty_server_title_chapter:0" in plan.repairs


def test_semantic_parse_rejects_empty_chapter_with_copy() -> None:
    raw = json.loads(_raw())
    raw["chapters"][0]["sources"] = []
    with pytest.raises(SchemaError, match="chapter 0 needs sources"):
        SemanticEditProposalAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]


def test_fast_montage_rejects_non_hook_after_dropping_empty_opening() -> None:
    raw = json.loads(_raw())
    raw["chapters"][0] = {
        "chapter_id": "server-title",
        "topic": "Title",
        "thought": "",
        "role": "hook",
        "weight": 1,
        "layout": "fullscreen",
        "sources": [],
    }
    raw["chapters"][1]["role"] = "build"
    with pytest.raises(SchemaError, match="first remaining chapter must be hook"):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(raw), _input(direction="fast_montage")
        )


def test_guided_structured_labels_allow_first_build_after_opening_title() -> None:
    raw = json.loads(_raw())
    raw["chapters"][0]["role"] = "build"
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        _input(creator_request="", shot_labels=["Park Walk", "Pub Talk"]),
    )
    assert [chapter.role for chapter in plan.chapters] == ["build", "payoff"]
    assert [chapter.thought for chapter in plan.chapters] == ["Park Walk", "Pub Talk"]


def test_semantic_parse_rejects_duplicate_video_under_once_reuse() -> None:
    raw = json.loads(_raw())
    raw["chapters"][1]["sources"] = [{"media_id": "m001", "candidate_index": 0}]
    with pytest.raises(SchemaError, match="appears more than once under once reuse"):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(raw),
            _input(video_reuse_policy="once", media_scope="selected", selected_media_ids=["park"]),
        )


def test_creator_labels_are_written_back_exactly() -> None:
    input = _input(creator_request="", shot_labels=["Park Walk", "Pub Talk"])
    plan = SemanticEditProposalAgent(None).parse(_raw(), input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["Park Walk", "Pub Talk"]


def test_structured_shot_labels_replace_long_model_rationale_before_validation() -> None:
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = "r" * 281
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        _input(creator_request="", shot_labels=["Park Walk", "Pub Talk"]),
    )
    assert [chapter.thought for chapter in plan.chapters] == ["Park Walk", "Pub Talk"]
    assert plan.repairs == [
        "replaced_server_owned_shot_label_thought:0",
        "replaced_server_owned_shot_label_thought:1",
    ]


def test_nonlabel_long_thought_still_fails_schema_validation() -> None:
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = "r" * 281
    with pytest.raises(SchemaError, match="at most 280"):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(raw), _input(creator_request="")
        )


def test_server_owned_title_and_shot_label_bindings_are_removed_with_repairs() -> None:
    raw = json.loads(
        _raw(
            text_bindings=[
                {"text": "London day", "chapter_ids": ["one"]},
                {"text": "Park Walk", "chapter_ids": ["one"]},
                {"text": "Wrap", "chapter_ids": ["two"]},
                {"text": "post match pub", "chapter_ids": ["two"]},
            ]
        )
    )
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw), _input(shot_labels=["Park Walk", "Pub Talk"], closing_title="Wrap")
    )
    assert [binding.text for binding in plan.text_bindings] == ["post match pub"]
    assert plan.repairs == [
        "replaced_server_owned_shot_label_thought:0",
        "replaced_server_owned_shot_label_thought:1",
        "dropped_server_owned_text_binding:0",
        "dropped_server_owned_text_binding:1",
        "dropped_server_owned_text_binding:2",
    ]


def test_normalized_server_title_echo_drops_without_explicit_caption_variant() -> None:
    raw = _raw(text_bindings=[{"text": "LONDON DAY", "chapter_ids": ["one"]}])
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        raw, _input(creator_request="")
    )
    assert plan.text_bindings == []
    assert "dropped_server_owned_text_binding:0" in plan.repairs


def test_distinct_caption_variant_of_server_title_is_preserved() -> None:
    raw = _raw(text_bindings=[{"text": "us", "chapter_ids": ["one"]}])
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        raw,
        _input(opening_title="US", creator_request='Show "us" on the first clip.'),
    )
    assert [binding.text for binding in plan.text_bindings] == ["us"]


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


def test_fast_caption_intents_keep_distinct_exact_variants() -> None:
    intents = [
        ResolvedClipIntent(
            intent_id="upper",
            op="include",
            attribute="park",
            creator_text="US",
            assignments=[ClipAssignment(media_id="park")],
        ),
        ResolvedClipIntent(
            intent_id="lower",
            op="include",
            attribute="pub",
            creator_text="us",
            assignments=[ClipAssignment(media_id="pub")],
        ),
    ]
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = ""
    raw["chapters"][1]["thought"] = ""
    plan = SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
        json.dumps(raw),
        _input(
            direction="fast_montage",
            creator_request='Show "US" on the first clip and show "us" on the second clip.',
            clip_intents=intents,
        ),
    )
    assert [binding.text for binding in plan.text_bindings] == ["US", "us"]


def test_creator_caption_binding_is_canonicalized_before_scheduler_deduplication() -> None:
    raw = json.loads(_raw(text_bindings=[{"text": "POST-MATCH PUB", "chapter_ids": ["two"]}]))
    input = _input(
        direction="fast_montage",
        target_duration_s=4,
        media=[
            EditProposalMedia(
                media_id="park",
                lane="clip",
                kind="video",
                duration_s=4,
                best_moments=[{"start_s": 0, "end_s": 4}],
            ),
            EditProposalMedia(
                media_id="pub",
                lane="clip",
                kind="video",
                duration_s=4,
                best_moments=[{"start_s": 0, "end_s": 4}],
            ),
        ],
    )
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [binding.text for binding in plan.text_bindings] == ["post match pub", "post match pub"]
    assert "canonicalized_creator_caption_binding:0" in plan.repairs

    scheduled = schedule_semantic_edit(plan, input)
    assert [(binding.media_id, binding.text) for binding in scheduled.montage_text_bindings] == [
        ("pub", "post match pub")
    ]


def test_distinct_exact_creator_caption_variants_survive_thoughts_and_bindings() -> None:
    raw = json.loads(
        _raw(
            text_bindings=[
                {"text": "US", "chapter_ids": ["one"]},
                {"text": "us", "chapter_ids": ["two"]},
            ]
        )
    )
    raw["chapters"][0]["thought"] = "US"
    raw["chapters"][1]["thought"] = "us"
    input = _input(creator_request='Show "US" on first clip and show "us" on second clip.')
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["US", "us"]
    assert [binding.text for binding in plan.text_bindings] == ["US", "us"]


def test_distinct_creator_caption_variant_must_not_be_missing_or_ambiguous() -> None:
    input = _input(creator_request='Show "US" on first clip and show "us" on second clip.')
    missing = json.loads(_raw())
    missing["chapters"][0]["thought"] = "US"
    missing["chapters"][1]["thought"] = "US"
    with pytest.raises(SchemaError, match="creator caption was dropped"):
        SemanticEditProposalAgent(None).parse(json.dumps(missing), input)  # type: ignore[arg-type]

    ambiguous = json.loads(_raw())
    ambiguous["chapters"][0]["thought"] = "Us"
    ambiguous["chapters"][1]["thought"] = "us"
    with pytest.raises(SchemaError, match="variants are ambiguous"):
        SemanticEditProposalAgent(None).parse(json.dumps(ambiguous), input)  # type: ignore[arg-type]


def test_quoted_caption_without_cue_words_is_enforced_for_non_english_request() -> None:
    input = _input(creator_request="“maç sonrası pub” pub çekimleri için.")
    raw = json.loads(_raw())
    raw["chapters"][0]["thought"] = "Invented copy"
    raw["chapters"][1]["thought"] = "maç sonrası pub"
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert [chapter.thought for chapter in plan.chapters] == ["", "maç sonrası pub"]

    missing = json.loads(_raw())
    missing["chapters"][0]["thought"] = "Invented copy"
    missing["chapters"][1]["thought"] = ""
    with pytest.raises(SchemaError, match="creator caption was dropped"):
        SemanticEditProposalAgent(None).parse(json.dumps(missing), input)  # type: ignore[arg-type]


def test_resolved_label_generic_text_binding_is_dropped_for_grounded_lane() -> None:
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
    plan = SemanticEditProposalAgent(None).parse(json.dumps(raw), input)  # type: ignore[arg-type]
    assert plan.text_bindings == []
    assert "dropped_grounded_label_text_binding:0" in plan.repairs


def test_unrequested_generic_text_binding_is_dropped_when_captions_are_explicit() -> None:
    raw = _raw(text_bindings=[{"text": "invented", "chapter_ids": ["one"]}])
    plan = SemanticEditProposalAgent(None).parse(raw, _input())  # type: ignore[arg-type]
    assert plan.text_bindings == []
    assert "dropped_unrequested_text_binding:0" in plan.repairs


def test_unrequested_binding_with_unknown_target_still_rejects() -> None:
    raw = _raw(text_bindings=[{"text": "invented", "chapter_ids": ["missing"]}])
    with pytest.raises(SchemaError, match="references unknown target"):
        SemanticEditProposalAgent(None).parse(raw, _input())  # type: ignore[arg-type]


def test_missing_explicit_creator_caption_still_rejects_after_binding_repair() -> None:
    raw = json.loads(_raw(text_bindings=[{"text": "invented", "chapter_ids": ["one"]}]))
    raw["chapters"][1]["thought"] = ""
    with pytest.raises(SchemaError, match="creator caption was dropped"):
        SemanticEditProposalAgent(None).parse(json.dumps(raw), _input())  # type: ignore[arg-type]


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


def test_semantic_prompt_marks_only_resolved_group_members_per_media() -> None:
    input = _input(
        clip_intents=[
            ResolvedClipIntent(
                intent_id="park-only",
                op="group",
                attribute="park together",
                assignments=[ClipAssignment(media_id="park")],
            ),
            ResolvedClipIntent(
                intent_id="ignored",
                op="group",
                attribute="pub maybe",
                status="needs_creator",
                assignments=[ClipAssignment(media_id="pub")],
            ),
        ]
    )
    prompt = SemanticEditProposalAgent(None).render_prompt(input)  # type: ignore[arg-type]
    media_json = prompt.split("AVAILABLE MEDIA: ", 1)[1].split("\n\nUse only", 1)[0]
    rows = {row["media_id"]: row["server_exclusive_group_ids"] for row in json.loads(media_json)}
    assert rows == {"m001": ["park-only"], "m002": []}


def test_semantic_prompt_makes_images_eligible_and_once_video_rule_explicit() -> None:
    prompt = SemanticEditProposalAgent(None).render_prompt(  # type: ignore[arg-type]
        _input(video_reuse_policy="once")
    )
    assert "Every listed video and image is an eligible editorial source." in prompt
    assert "each video may appear in at most one chapter" in prompt
    assert "For fast_montage, the first source chapter must have role hook." in prompt
    assert "first source chapter may be build when the opening title provides the hook" in prompt


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


def test_exclusive_group_rejects_mixed_chapter_even_with_server_annotation() -> None:
    group = ResolvedClipIntent(
        intent_id="park-only",
        op="group",
        attribute="park together",
        assignments=[ClipAssignment(media_id="park")],
    )
    raw = json.loads(_raw())
    raw["chapters"][0]["sources"] = [{"media_id": "m001"}, {"media_id": "m002"}]
    with pytest.raises(SchemaError, match="group has unrelated sources"):
        SemanticEditProposalAgent(None).parse(  # type: ignore[arg-type]
            json.dumps(raw), _input(creator_request="", clip_intents=[group])
        )


def test_group_schema_retry_names_exclusive_aliases_without_model_output() -> None:
    class FakeClient:
        def __init__(self, responses: list[str]) -> None:
            self.responses = responses
            self.prompts: list[str] = []

        def invoke(self, **kwargs: object) -> ModelInvocation:
            self.prompts.append(str(kwargs["prompt"]))
            return ModelInvocation(raw_text=self.responses.pop(0))

    group = ResolvedClipIntent(
        intent_id="park-only",
        op="group",
        attribute="park together",
        assignments=[ClipAssignment(media_id="park")],
    )
    invalid = json.loads(_raw())
    invalid["chapters"][0]["topic"] = "MODEL_ONLY_POLLUTION"
    invalid["chapters"][0]["sources"] = [{"media_id": "m001"}, {"media_id": "m002"}]
    client = FakeClient([json.dumps(invalid), _raw()])
    agent = SemanticEditProposalAgent(client)  # type: ignore[arg-type]
    agent.spec = replace(agent.spec, model="test-model", max_attempts=2)

    plan = agent.run(_input(creator_request="", clip_intents=[group]))

    assert [chapter.chapter_id for chapter in plan.chapters] == ["one", "two"]
    assert len(client.prompts) == 2
    assert (
        '"exclusive_group_aliases": [{"intent_id": "park-only", "aliases": ["m001"]}]'
        in (client.prompts[0])
    )
    assert _GROUP_RETRY_HINT not in client.prompts[0]
    assert _GROUP_RETRY_HINT in client.prompts[1]
    assert "MODEL_ONLY_POLLUTION" not in client.prompts[1]
    assert _GROUP_RETRY_HINT not in agent.schema_clarification()


def test_semantic_schema_clarification_is_safe_before_any_parse() -> None:
    clarification = SemanticEditProposalAgent(None).schema_clarification()  # type: ignore[arg-type]
    assert _GROUP_RETRY_HINT not in clarification


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
